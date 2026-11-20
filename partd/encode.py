from .core import Interface
from .file import File
from toolz import valmap
from .utils import (frame, framesplit, inspect_frames, flatten, nested_get)


class FrameError(Exception):
    """ Base class for corruption found while checking framed values.

    A failure witness always names the original ``key`` it came from, the
    zero-based ``frame_index`` within that key and the byte ``offset`` where
    that frame's length header starts.
    """

    def __init__(self, key, frame_index, offset):
        self.key = key
        self.frame_index = frame_index
        self.offset = offset
        super().__init__(self.describe())

    def describe(self):
        raise NotImplementedError


class TruncatedHeader(FrameError):
    """ Trailing bytes do not hold a complete length header. """

    def __init__(self, key, frame_index, offset, available):
        self.available = available
        super().__init__(key, frame_index, offset)

    def describe(self):
        return (
            "key %r: frame %d at offset %d has a truncated length header "
            "(%d of 8 header bytes present)"
            % (self.key, self.frame_index, self.offset, self.available)
        )


class TruncatedFrame(FrameError):
    """ Length header is complete but the payload is missing bytes. """

    def __init__(self, key, frame_index, offset, declared, available):
        self.declared = declared
        self.available = available
        super().__init__(key, frame_index, offset)

    def describe(self):
        return (
            "key %r: frame %d at offset %d declares %d payload bytes but "
            "only %d are present"
            % (self.key, self.frame_index, self.offset,
               self.declared, self.available)
        )


class FrameDecodeError(FrameError):
    """ The frame is complete but the codec rejected its payload. """

    def __init__(self, key, frame_index, offset, length, original):
        self.length = length
        self.original = original
        super().__init__(key, frame_index, offset)

    def describe(self):
        return (
            "key %r: frame %d at offset %d (%d payload bytes) failed to "
            "decode with %s: %s"
            % (self.key, self.frame_index, self.offset, self.length,
               type(self.original).__name__, self.original)
        )


class Encode(Interface):
    def __init__(self, encode, decode, join, partd=None):
        if not partd or isinstance(partd, str):
            partd = File(partd)
        self.partd = partd
        self.encode = encode
        self.decode = decode
        self.join = join
        Interface.__init__(self)

    def __getstate__(self):
        return self.__dict__

    __setstate__ = Interface.__setstate__

    def append(self, data, **kwargs):
        data = valmap(self.encode, data)
        data = valmap(frame, data)
        self.partd.append(data, **kwargs)

    def _get(self, keys, **kwargs):
        raw = self.partd._get(keys, **kwargs)
        return [self.join([self.decode(frame) for frame in framesplit(chunk)])
                for chunk in raw]

    def _check_one(self, key, chunk):
        """ Verify every frame of one key and decode it in order.

        Returns the joined decoded value for a fully readable key, or a
        :class:`FrameError` witness naming the key, frame and frame offset.
        Truncated frames stay truncated even when the codec could make sense
        of the partial payload, and a partially readable key never yields a
        partial decoded value.
        """
        decoded = []
        for info in inspect_frames(chunk):
            if info.length is None:
                return TruncatedHeader(key, info.index, info.offset,
                                       len(info.payload))
            if not info.complete:
                return TruncatedFrame(key, info.index, info.offset,
                                      info.length, len(info.payload))
            try:
                value = self.decode(info.payload)
            except Exception as exception:
                return FrameDecodeError(key, info.index, info.offset,
                                        info.length, exception)
            decoded.append(value)
        return self.join(decoded)

    def check(self, keys, **kwargs):
        """ Check each key independently, one result per requested slot.

        Keys may be passed interleaved with healthy ones and may repeat; each
        request slot gets its own result.  A readable key returns its decoded
        value (zero-length frames accepted by the codec are simply decoded in
        place).  A corrupt key returns a :class:`FrameError` witness instead of
        raising, so one bad key cannot block recovery of the other keys.

        Missing keys decode to the join of an empty frame list, exactly as the
        regular read does.
        """
        if not isinstance(keys, list):
            return self.check([keys], **kwargs)[0]
        if any(isinstance(key, list) for key in keys):
            flatkeys = list(flatten(keys))
            result = self.check(flatkeys, **kwargs)
            return nested_get(keys, dict(zip(flatkeys, result)))
        raw = self.partd._get(keys, **kwargs)
        return [self._check_one(key, chunk) for key, chunk in zip(keys, raw)]

    def delete(self, keys, **kwargs):
        return self.partd.delete(keys, **kwargs)

    def _iset(self, key, value, **kwargs):
        return self.partd.iset(key, frame(self.encode(value)), **kwargs)

    def drop(self):
        return self.partd.drop()

    @property
    def lock(self):
        return self.partd.lock

    def __exit__(self, *args):
        self.drop()
        self.partd.__exit__(*args)
