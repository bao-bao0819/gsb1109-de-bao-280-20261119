from bitformat import Repeat, Tibs, Field, Format, Expression
import pytest

def test_creation():
    p = Repeat.from_params(3, "u8")
    assert p.count == 3
    assert p.field == Field("u8")


def test_from_string():
    p = Repeat.from_string('repeat{3}: u8')
    assert p.count == 3
    assert p.field == Field('u8')
    s = """repeat {2}:
        fred: (
            bool,
            john: i7
        )
    """
    q = Repeat(s)
    assert q.count == 2
    assert q.unpack(Tibs("0x8710")) == [[True, 7], [False, 16]]


def test_pack():
    f = Repeat("repeat {4}: bool")
    f.pack([True, False, True, False])
    assert f.value == [True, False, True, False]


def test_simple_parse_and_unpack():
    p = Repeat.from_params("{x}", "u8")
    p.parse("0x010203", x=3)
    assert p.value == [1, 2, 3]
    p.count = p.count.evaluate(x=3)
    assert p.unpack("0x030201") == [3, 2, 1]

def test_parsing_repeat():
    f = Format('(repeat {4}: u8)')
    g = f[:]
    assert f.bit_length == 32
    assert str(f[0]) == 'repeat{4}:\n    u8\n    u8\n    u8\n    u8'
    f.parse("0x01020304")
    assert f != g
    assert f.value[0] == [1, 2, 3, 4]
    assert str(f[0]) == 'repeat{4}:\n    u8 = 1\n    u8 = 2\n    u8 = 3\n    u8 = 4'

def test_repeat_str_with_expression():
    r1 = Repeat.from_params(4, 'bool')
    r2 = Repeat.from_params('{4}', 'bool')
    r3 = Repeat.from_params(Expression('{4}'), 'bool')
    assert str(r1) == str(r2)
    assert str(r2) == str(r3)
    assert repr(r1) == repr(r2)
    assert repr(r2) == repr(r3)
    with pytest.raises(ValueError):
        _ = Repeat("repeat4: bool")
    r4 = Repeat("repeat{4}: bool")
    r5 = Repeat(" repeat{4}:bool")
    assert str(r1) == str(r4)
    assert repr(r1) == repr(r4)
    assert str(r4) == str(r5)
    assert repr(r4) == repr(r5)


def test_pack_errors():
    r = Repeat("repeat {x}: u8")
    with pytest.raises(ValueError):
        r.pack([1, 2, 3])

def test_not_using_all_values():
    f = Format("(repeat {3}: u8, u8)")
    _ = repr(f)
    s = "(repeat {3}: u8 = [1, 2, 3], u8 = 4)"
    g = Format(s)
    r1 = Repeat.from_params(3, 'u8', [1, 2, 3])
    r2 = Repeat.from_string("repeat{3}: u8 = [1, 2, 3]")
    assert r1.value == [1, 2, 3]
    assert r2.value == [1, 2, 3]

    gp = Format.from_params([r1, 'u8'])
    assert gp[0].value == [1, 2, 3]
    f.pack([[1, 2, 3], 4])
    assert f[0].value == [1, 2, 3]
    assert g[0].value == [1, 2, 3]
    assert f == g
    assert str(f) == str(g)
    assert f.to_tibs() == '0x01020304'
    f.clear()
    f.pack([[1, 2, 3], 5])

def test_thing():
    r = Repeat.from_params(1, 'u16', [7])
    x = repr(r)
    assert x == "Repeat.from_params(Expression('1'), 'u16', [7])"
    x = repr(r)
    assert x == "Repeat.from_params(Expression('1'), 'u16', [7])"


class TestReusedPacketFormat:
    """Regression tests for a decoder reusing one Format to read consecutive packets.

    Packet layout: a header 'count' gives the number of blocks. Each block starts
    with its own 'count' of samples, and an outer state list with the header
    'count' length follows the blocks.
    """

    spec = ("packet: (count: u8, "
            "repeat {count}: (count: u8, repeat {count}: (sample: u8)), "
            "repeat {count}: (state: u8))")
    # Header count 2; block counts 0 and 2; samples 10, 11; states 101, 102.
    packet1 = Tibs("0x0200020a0b6566")
    packet1_value = [2, [[0, []], [2, [[10], [11]]]], [[101], [102]]]
    # Header count 1; one block with count 1; sample 20; state 201.
    packet2 = Tibs("0x010114c9")
    packet2_value = [1, [[1, [[20]]]], [[201]]]

    def test_sequential_packets_with_nested_repeats(self):
        f = Format(self.spec)
        # First packet: empty and non-empty blocks each keep their own hierarchy,
        # and the outer state list length comes only from the header count.
        assert f.parse(self.packet1) == len(self.packet1)
        assert f.value == self.packet1_value
        assert f.value[1][0] == [0, []]  # Empty block keeps its (count, samples) structure.
        assert f.value[1][1] == [2, [[10], [11]]]
        assert len(f.value[2]) == 2  # Two states from the header count.
        # The block-level 'count' fields are scoped to their block: once the
        # blocks are done the outer (header) count is the one in effect.
        assert f.vars["count"] == 2
        assert f.to_tibs() == self.packet1

        # A truncated input (cut before the second block is complete) only
        # delivers its own bits and must fail.
        truncated = self.packet1[:8 + 8 + 8 + 8]  # One of block two's two samples.
        with pytest.raises(ValueError, match="sample"):
            f.parse(truncated)

        # The next packet is parsed cleanly: no counts or samples leak through.
        assert f.parse(self.packet2) == len(self.packet2)
        assert f.value == self.packet2_value
        assert len(f.value[2]) == 1
        assert f.vars["count"] == 1
        assert f.to_tibs() == self.packet2

    def test_repack_and_reread(self):
        f = Format(self.spec)
        for packet, value in [(self.packet1, self.packet1_value),
                              (self.packet2, self.packet2_value)]:
            assert f.unpack(packet) == value
            # Packing the values back regenerates the exact declared bit string,
            # and reading that again gives the same layout.
            repacked = f.pack(value)
            assert repacked == packet
            assert f.unpack(repacked) == value

    def test_truncation_failures_are_distinguishable(self):
        f = Format(self.spec)
        # Missing the header count.
        with pytest.raises(ValueError, match="Field 'count: u8'"):
            f.parse(Tibs())
        # Missing a block's samples.
        with pytest.raises(ValueError, match="Field 'sample: u8'"):
            f.parse(self.packet1[:8 + 8 + 8])
        # Missing the outer states.
        with pytest.raises(ValueError, match="Field 'state: u8'"):
            f.parse(self.packet1[:8 + 8 + 8 + 8 + 8])