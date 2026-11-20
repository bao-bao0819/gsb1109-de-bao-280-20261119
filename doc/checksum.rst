Checksums and frame boundaries
==============================

A checksum field verifies part of the frame that contains it.  It is declared
like any other field, so its position comes from the surrounding
:class:`Format`, and its coverage is resolved from the actual frame rather than
from a fixed byte offset.  This means a repeated region whose length comes from
a count is handled without hard-coding its size.

Two independent layers
----------------------

Each :class:`Checksum` has a ``layer`` label and keeps its own result.  A
typical archive frame declares an inner checksum after the measurements and an
outer checksum at the end of the frame::

    from bitformat import Checksum, Field, Format, Repeat, Tibs

    def crc8(bits: Tibs) -> int:
        return sum(bits.to_bytes()) & 0xff

    inner = Checksum.from_params(8, layer="inner", rule=crc8, name="inner")
    outer = Checksum.from_params(8, layer="outer", rule=crc8, name="outer")

The rule and width are supplied by the caller.  A layer may be declared more
than once, and there may be any number of layers; they are never combined into
a single pass/fail flag.

Coverage
--------

``start`` and ``end`` select the protected half-open bit range.  Each can be a
bit offset, an :class:`Expression`, or the name of a field in the containing
format.  For a field name, ``start`` means the field's first bit and ``end``
means the last bit.

The default ``start`` is the beginning of the containing format and the default
``end`` is the start of the checksum field.  A checksum's own bits are always
excluded, even when an explicit range extends past the field; the protected
data is the remaining segments joined in order.

If the range ends before the checksum field, the checksum covers a prefix and
does not include later padding.  If it ends after the checksum field, it covers
both the earlier data and the later data (such as padding), but not the
checksum itself.

Parsing and results
-------------------

:meth:`Format.parse` verifies every checksum after all fields have been read.
If any layer fails, :class:`ChecksumFailure` is raised.  Its ``results`` list
has one :class:`ChecksumResult` per checksum, in declaration order, with
``layer``, ``field``, ``ok``, ``expected``, ``actual`` and the checksum
field's covered range::

    try:
        fmt.parse(frame)
    except ChecksumFailure as failure:
        for result in failure.results:
            print(result.layer, result.ok, result.expected, result.actual)

The individual fields are also available through ``fmt.checksums`` and
``fmt.checksum_results``.

Rebuilding
----------

A parsed format keeps the values of every field, including checksums, so
:meth:`Format.to_tibs` can rebuild the frame after a measurement is corrected.
Writable checksums are recalculated in declaration order: inner layers first,
then outer layers, so an outer checksum sees the newly written inner value and
the preserved padding.

Two convenient forms are available::

    values = fmt.measured_value()          # no checksum entries
    values[2] = [101, 2000]
    frame = fmt.pack_measured(values)

or, when the full field list is easier to work with, set the field value and
call ``fmt.to_tibs()``.

Padding received in a frame is kept as it was, so an untouched frame
round-trips exactly.  Padding written by a caller is zero.

Frame length and truncation
---------------------------

Archive records often know the number of valid bits in each frame.  Pass that
length when parsing so that a following frame can never be used to complete a
short one::

    fmt.parse(buffer, valid_bit_length=record.valid_bits)

or use :meth:`Format.parse_frame`, which does the same and records
``fmt.valid_bit_length``.

If the input ends too early, :class:`TruncationError` is raised.  It reports the
first missing field and the number of bits needed and available, and never
requests data beyond the declared frame boundary.  If the format consumes fewer
bits than the declared valid length, :class:`FrameError` reports the unused
bits instead of silently ignoring them.

Formats without checksums continue to parse and pack exactly as before.
