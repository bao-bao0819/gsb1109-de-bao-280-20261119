import struct
import zlib

import pytest

from partd.dict import Dict
from partd.encode import (
    Encode,
    FrameDecodeError,
    FrameError,
    TruncatedFrame,
    TruncatedHeader,
)
from partd.file import File
from partd.utils import frame, inspect_frames


def test_partd():
    with Encode(zlib.compress, zlib.decompress, b''.join) as p:
        p.append({'x': b'Hello', 'y': b'abc'})
        p.append({'x': b'World!', 'y': b'def'})

        result = p.get(['y', 'x'])
        assert result == [b'abcdef', b'HelloWorld!']

        assert p.get('z') == b''

        with p.lock:  # uh oh, possible deadlock
            result = p.get(['x'], lock=False)


def test_ensure():
    with Encode(zlib.compress, zlib.decompress, b''.join) as p:
        p.iset('x', b'123')
        p.iset('x', b'123')
        p.iset('x', b'123')
        assert p.get('x') == b'123'


# A codec that is deliberately lenient: zero-length frames and short payloads
# are perfectly decodable, while one sentinel payload is rejected.  This lets
# the check mode prove that short frames fail on their own merits instead of
# leaning on the codec to complain.
def lenient_encode(value):
    return value


def lenient_decode(payload):
    if payload.startswith(b'BAD'):
        raise ValueError('codec rejects this payload')
    return payload


def make_partd():
    return Encode(lenient_encode, lenient_decode, b''.join, partd=Dict())


def corrupt_dataset():
    p = make_partd()
    # Healthy key, including a zero-length frame the current codec accepts.
    normal = frame(b'alpha') + frame(b'') + frame(b'beta')
    # Two readable frames followed by a complete header promising five
    # payload bytes while only three are on disk.
    short_payload = (frame(b'one') + frame(b'two')
                     + struct.pack('Q', 5) + b'abc')
    # A trailing fragment that does not even fill a length header.
    header_short = frame(b'one') + b'\x05\x00\x00'
    # Every frame is complete, but the codec rejects the middle one.
    decode_bad = frame(b'good') + frame(b'BADFRAME') + frame(b'tail')
    p.partd.append({
        'normal': normal,
        'short-payload': short_payload,
        'header-short': header_short,
        'decode-bad': decode_bad,
    })
    return p


def test_check_returns_one_witness_per_request_slot():
    p = corrupt_dataset()
    keys = ['short-payload', 'normal', 'decode-bad', 'normal', 'header-short']

    results = p.check(keys)

    assert len(results) == len(keys)
    # The repeated normal key occupies its own slot and decodes both times.
    assert results[1] == results[3] == b'alphabeta'
    assert results[1] is not results[3]

    short = results[0]
    assert isinstance(short, TruncatedFrame)
    assert short.key == 'short-payload'
    assert short.frame_index == 2
    assert short.offset == len(frame(b'one')) + len(frame(b'two'))
    assert short.declared == 5
    assert short.available == 3

    decode_failure = results[2]
    assert isinstance(decode_failure, FrameDecodeError)
    assert decode_failure.key == 'decode-bad'
    assert decode_failure.frame_index == 1
    assert decode_failure.offset == len(frame(b'good'))
    assert isinstance(decode_failure.original, ValueError)

    header_failure = results[4]
    assert isinstance(header_failure, TruncatedHeader)
    assert header_failure.key == 'header-short'
    assert header_failure.frame_index == 1
    assert header_failure.offset == len(frame(b'one'))
    assert header_failure.available == 3


def test_check_keeps_three_failure_kinds_distinct():
    p = corrupt_dataset()
    results = dict(zip(
        ['short-payload', 'header-short', 'decode-bad'],
        p.check(['short-payload', 'header-short', 'decode-bad']),
    ))

    assert isinstance(results['short-payload'], TruncatedFrame)
    assert isinstance(results['header-short'], TruncatedHeader)
    assert isinstance(results['decode-bad'], FrameDecodeError)
    assert not isinstance(results['short-payload'], TruncatedHeader)
    assert not isinstance(results['decode-bad'], TruncatedFrame)
    for witness in results.values():
        assert isinstance(witness, FrameError)


def test_short_frame_fails_even_when_codec_accepts_remainder():
    p = corrupt_dataset()
    # The lenient codec would happily decode the partial payload, so the old
    # read silently returns it.  The check mode must still reject the key.
    assert p.get(['short-payload']) == [b'onetwoabc']

    result = p.check(['short-payload'])[0]
    assert isinstance(result, TruncatedFrame)
    assert not isinstance(result, (bytes, bytearray))


def test_check_never_merges_partial_read():
    p = corrupt_dataset()
    result = p.check(['short-payload'])[0]
    assert isinstance(result, FrameError)
    # Earlier frames are readable, but no partial join is offered.
    assert result != b'onetwo'


def test_zero_length_frame_is_legal():
    p = make_partd()
    p.partd.append({'z': frame(b'') + frame(b'data') + frame(b'')})

    assert p.check(['z']) == [b'data']
    infos = list(inspect_frames(frame(b'') + frame(b'data') + frame(b'')))
    assert [(info.index, info.length, info.complete) for info in infos] == [
        (0, 0, True), (1, 4, True), (2, 0, True)
    ]


def test_check_scalar_missing_and_nested_keys():
    p = corrupt_dataset()

    assert p.check('normal') == b'alphabeta'
    assert p.check('missing') == b''

    nested = p.check([['normal', 'decode-bad'], ['normal']])
    assert nested[0][0] == b'alphabeta'
    assert isinstance(nested[0][1], FrameDecodeError)
    assert nested[1] == [b'alphabeta']


def test_old_get_exception_contract_is_unchanged():
    with Encode(zlib.compress, zlib.decompress, b''.join, partd=Dict()) as p:
        p.partd.append({
            'header-short': frame(zlib.compress(b'one')) + b'\x05\x00\x00',
            'short-payload': (frame(zlib.compress(b'one'))
                              + struct.pack('Q', 99) + b'abc'),
            'decode-bad': frame(b'not-a-zlib-stream'),
        })

        with pytest.raises(struct.error):
            p.get(['header-short'])
        with pytest.raises(zlib.error):
            p.get(['short-payload'])
        with pytest.raises(zlib.error):
            p.get(['decode-bad'])

        # Healthy keys in the same store still read normally.
        p.append({'healthy': b'still readable'})
        assert p.get('healthy') == b'still readable'


def test_check_with_file_backend(tmp_path):
    with Encode(lenient_encode, lenient_decode, b''.join,
                partd=File(str(tmp_path))) as p:
        p.partd.append({
            'normal': frame(b'alpha') + frame(b''),
            'header-short': frame(b'one') + b'\x05\x00\x00',
        })

        results = p.check(['header-short', 'normal'])
        assert isinstance(results[0], TruncatedHeader)
        assert results[0].key == 'header-short'
        assert results[1] == b'alpha'
