import os

import pytest

from partd import File
from partd.window import (
    DATA,
    EMPTY,
    MISSING,
    WINDOW_END,
    ReadResult,
    ReadStatus,
    ReadWindow,
)


def collect(window, blocksize, position=0):
    """Read a window from ``position`` to its captured end."""
    chunks = []
    positions = []
    while True:
        result = window.read(blocksize, position)
        if result.status == WINDOW_END:
            break
        chunks.append(result.data)
        positions.append(result.position)
        position = result.position
    return b"".join(chunks), positions


def test_window_append_does_not_leak_into_old_window():
    with File() as p:
        p.append({'x': b'0123456789'})

        old = p.open_window('x', blocksize=4)
        assert old.size == 10
        first = old.read()
        assert first.data == b'0123'
        assert first.position == 4

        p.append({'x': b'abcdefghij'})

        # Continue in the original window from the returned position.
        assert old.read(4, first.position).data == b'4567'
        rest, positions = collect(old, 4, 4)
        assert rest == b'456789'
        assert positions == [8, 10]
        assert old.read(4, 10).status == WINDOW_END
        assert old.read(4, 10).position == 10

        # A new window sees the appended data.
        new = p.open_window('x', blocksize=4)
        assert new.size == 20
        data, positions = collect(new, 4)
        assert data == b'0123456789abcdefghij'
        assert positions == [4, 8, 12, 16, 20]
        assert new.read(4, 20).status == WINDOW_END

        old.close()
        new.close()


def test_window_delete_and_recreate_is_isolated():
    with File() as p:
        p.append({'x': b'old-version'})
        window = p.open_window('x')
        assert window.read(3, 0).data == b'old'

        p.delete('x')
        p.append({'x': b'new-version'})

        # The open window still reads the unlinked original file.
        assert window.read(3, 3).data == b'-ve'
        assert window.read(-1, 0).data == b'old-version'
        assert window.read(-1, 11).status == WINDOW_END

        # A new window sees the replacement.
        new = p.open_window('x')
        assert new.read(-1, 0).data == b'new-version'
        assert new.size == len(b'new-version')

        window.close()
        new.close()


def test_window_statuses_for_missing_and_empty():
    with File() as p:
        missing = p.open_window('absent')
        assert missing.missing
        assert missing.size == 0
        result = missing.read()
        assert (result.data, result.position, result.status) == (
            b'', 0, MISSING)
        assert list(missing.iter_chunks()) == []

        p.iset('empty', b'')
        empty = p.open_window('empty')
        assert empty.empty
        assert not empty.missing
        assert empty.size == 0
        result = empty.read()
        assert (result.data, result.position, result.status) == (
            b'', 0, EMPTY)
        assert list(empty.iter_chunks()) == []

        missing.close()
        empty.close()


def test_window_end_position_and_status_aliases():
    with File() as p:
        p.append({'x': b'abc'})
        window = p.open_window('x', blocksize=2)

        first = window.read()
        assert first == (b'ab', 2, DATA)
        assert first.status == ReadStatus.DATA
        assert first.status == "data"

        second = window.read(position=first.position)
        assert second == (b'c', 3, DATA)

        end = window.read(position=second.position)
        assert end == (b'', 3, WINDOW_END)
        assert end.status == "window_end"
        assert end.window_end and end.at_end and end.eof

        # Reading past the captured end stays at the captured end.
        beyond = window.read(10, 100)
        assert beyond == (b'', 3, WINDOW_END)
        assert window.position == 3

        window.close()


def test_window_read_result_shape():
    with File() as p:
        p.append({'x': b'hello'})
        window = p.open_window('x', blocksize=2)
        result = window.read()
        assert isinstance(result, ReadResult)
        assert result.data == b'hello'[:2]
        assert result.bytes == result.data
        assert result.position == result.offset == result.next_position == 2
        assert result.nbytes == 2
        assert result.as_pair() == (b'he', 2)
        assert result.as_dict() == {
            'data': b'he', 'position': 2, 'status': ReadStatus.DATA}
        assert result == b'he'
        assert result == (b'he', 2)
        window.close()


def test_window_get_regular_get_unchanged():
    with File() as p:
        p.append({'x': b'Hello', 'y': b'abc'})
        p.append({'x': b'World!', 'y': b'def'})
        assert p.get(['x', 'y']) == [b'HelloWorld!', b'abcdef']

        window = p.open_window('x', blocksize=3)
        chunks = [data for data, position in window.iter_chunks()]
        assert b''.join(chunks) == b'HelloWorld!'
        window.close()


def test_window_stops_without_waiting_for_writer():
    with File() as p:
        p.append({'x': b'a' * 10})
        window = p.open_window('x', blocksize=4)
        first = window.read()
        assert first.position == 4

        # Simulate a writer proceeding while the reader is paused.
        p.append({'x': b'b' * 10})

        remaining = []
        position = first.position
        while True:
            result = window.read(4, position)
            if result.status != DATA:
                assert result.status == WINDOW_END
                break
            remaining.append(result.data)
            position = result.position
        assert b''.join(remaining) == b'a' * 6
        assert position == 10


def test_window_only_reads_requested_block():
    with File() as p:
        p.append({'x': b'a' * 1024})
        window = p.open_window('x', blocksize=16)
        result = window.read()
        assert len(result.data) == 16
        assert result.position == 16
        assert window.size == 1024
        window.close()


def test_window_context_manager_and_seek():
    with File() as p:
        p.append({'x': b'0123456789'})
        with p.open_window('x', blocksize=3) as window:
            assert window.tell() == 0
            assert window.seek(4) == 4
            assert window.read().data == b'456'
            assert window.remaining == 3
            assert window.seek(-2, os.SEEK_END) == 8
            assert window.read().data == b'89'
        assert window.closed
        with pytest.raises(ValueError):
            window.read()


def test_window_recreated_file_is_a_new_window_version():
    with File() as p:
        p.append({'x': b'first-generation'})
        old = p.open_window('x', blocksize=5)

        p.delete('x')
        p.append({'x': b'second'})

        new = p.open_window('x', blocksize=5)
        assert new.read().data == b'secon'
        assert new.read(5, 5).data == b'd'

        old_data = b''.join(data for data, position in old.iter_chunks())
        assert old_data == b'first-generation'

        old.close()
        new.close()


def test_direct_read_window_construction():
    with File() as p:
        p.append({'x': b'12345'})
        window = ReadWindow(p, 'x', blocksize=2)
        assert window.read().data == b'12'
        assert window.read(position=2).data == b'34'
        assert window.read(2, 4).data == b'5'
        assert window.read(2, 5).status == WINDOW_END
        window.close()
