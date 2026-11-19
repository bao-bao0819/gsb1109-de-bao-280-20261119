import pytest
from bitformat import Reader, Field, Tibs, Dtype, DtypeTuple, Mutibs, Format


def test_creation():
    r = Reader(Tibs())
    assert len(r.bits) == 0
    assert r.pos == 0

    r = Reader(Tibs.from_string("0x12345"), 4)
    assert len(r.bits) == 20
    assert r.pos == 4

    r = Reader(Tibs.from_bytes(b"hello"))
    assert len(r.bits) == 40

    with pytest.raises(TypeError):
        _ = Reader('0x234')
    with pytest.raises(TypeError):
        _ = Reader(b'hello')

def test_read():
    r = Reader(Tibs("0x12345"), 4)
    assert r.pos == 4
    assert r.read("bits4") == "0x2"
    assert r.pos == 8
    assert r.read("u4") == 3
    assert r.pos == 12
    r.pos = 0
    b = r.read(8)
    assert b == '0x12'

def test_peek():
    r = Reader(Tibs("0xff000001"))
    assert r.peek('hex8') == 'ff'
    assert r.peek(Dtype('hex8')) == 'ff'
    assert r.pos == 0
    r.pos = len(r) - 1
    assert r.peek(1) == '0b1'
    with pytest.raises(ValueError):
        _ = r.peek(2)
    assert r.peek(1) == '0b1'

def test_parse():
    r = Reader(Tibs.from_bytes(b"hello_world"))
    r.pos = 6 * 8
    f = Field("bytes24")
    assert r.parse(f) == 24
    assert f.value == b"wor"
    r.parse(g := Field("bool"))
    assert g.value is False
    with pytest.raises(TypeError):
        _ = r.parse('bin2')

def test_read_tuple():
    r = Reader(Tibs('0x00ffff'))
    x = r.peek(DtypeTuple('(hex12, u4)'))
    assert x == ('00f', 15)
    x = r.read('(u8, bool, bool)')
    assert x == (0, True, True)

def test_with_mutable_bits():
    mb = Mutibs('0x00ff')
    r = Reader(mb)
    assert r.pos == 0
    assert r.read('u9') == 1
    assert r.pos == 9
    mb.extend_left('0b111111111')
    assert r.pos == 9
    assert r.read('u9') == 1
    x = r.bits
    assert isinstance(x, Mutibs)
    assert x is mb

def test_open_ended_read():
    a = Tibs.from_zeros(100)
    r = Reader(a)
    r.pos = 1
    x = r.peek('[u11;]')
    assert x == (0, 0, 0, 0, 0, 0, 0, 0, 0)
    x = r.read('[u11;]')
    assert x == (0, 0, 0, 0, 0, 0, 0, 0, 0)
    assert r.pos == 100


def test_try_parse_success():
    r = Reader(Tibs('0x0102'))
    f = Format('(x: u8, y: u4)')
    assert r.try_parse(f) == 12
    assert r.pos == 12
    assert f['x'].value == 1
    assert f['y'].value == 0


def test_try_parse_rolls_back_position_and_values():
    r = Reader(Tibs('0xab322334'))
    v1 = Format('(sync: u8, kind: u4, version: const u4 = 1, payload: u8)')
    v2 = Format('(sync: u8, kind: u4, version: const u4 = 2, payload: u16)')
    assert r.try_parse(v1) is None
    assert r.pos == 0
    assert v1['sync'].value is None
    assert v1['kind'].value is None
    assert v1['payload'].value is None
    assert r.try_parse(v2) == 32
    assert r.pos == 32
    assert v2['sync'].value == 0xab
    assert v2['kind'].value == 3
    assert v2['version'].value == 2
    assert v2['payload'].value == 0x2334


def test_try_parse_restores_previous_state():
    fresh = Format('(a: u8, b: u16)')
    fresh.parse('0x123456')
    previous_a = fresh['a'].value
    previous_b = fresh['b'].value
    previous_vars = dict(fresh.vars)
    fields = fresh._fields
    r = Reader(Tibs('0x01'))
    assert r.try_parse(fresh) is None
    assert r.pos == 0
    assert fresh._fields is fields
    assert fresh['a'].value == previous_a
    assert fresh['b'].value == previous_b
    assert fresh.vars == previous_vars


def test_try_parse_restores_previous_vars():
    fresh = Format('(a: u8, repeat {a}: u8)')
    fresh.parse('0x010202', a=2)
    previous_a = fresh['a'].value
    previous_values = fresh[1].value
    previous_vars = dict(fresh.vars)
    r = Reader(Tibs('0x01'))
    assert r.try_parse(fresh) is None
    assert r.pos == 0
    assert fresh['a'].value == previous_a
    assert fresh[1].value == previous_values
    assert fresh.vars == previous_vars


def test_try_parse_requires_field_type():
    r = Reader(Tibs('0xff'))
    with pytest.raises(TypeError):
        _ = r.try_parse('u8')


def test_try_parse_with_field():
    r = Reader(Tibs('0x12'))
    f = Field('u8')
    assert r.try_parse(f) == 8
    assert f.value == 0x12
    r.pos = 0
    g = Field('const u8 = 0x13')
    assert r.try_parse(g) is None
    assert r.pos == 0
