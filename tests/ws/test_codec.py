"""Binary frame codec: round trip, strict validation, error → close-code mapping (spec §6.2)."""

import struct

import pytest
from hypothesis import given
from hypothesis import strategies as st

from chartwire.ws import codec
from chartwire.ws.actions import CloseCode

HDR = struct.Struct("<HBBII")


def frame(*, magic=codec.MAGIC, ver=codec.VERSION, flags=0, seq=1, offset_ms=0, payload=b"\x00\x01") -> bytes:
    return HDR.pack(magic, ver, flags, seq, offset_ms) + payload


def test_header_is_twelve_bytes_little_endian():
    b = codec.encode_frame(codec.FrameHeader(seq=0x01020304, offset_ms=0x0A0B0C0D, flags=0x05), b"\xff")
    assert len(b) == codec.HEADER_LEN + 1
    assert b[:2] == b"\x57\x43"  # 0x4357 LE
    assert b[2] == 1 and b[3] == 0x05
    assert b[4:8] == b"\x04\x03\x02\x01" and b[8:12] == b"\x0d\x0c\x0b\x0a"


def test_roundtrip_and_flag_accessors():
    payload = bytes(range(64))
    h = codec.FrameHeader(seq=7, offset_ms=1400, flags=codec.FLAG_LAST_CHUNK | codec.FLAG_SIM)
    f = codec.decode(codec.encode_frame(h, payload))
    assert f.header == h and f.payload == payload
    assert f.header.last_chunk and f.header.sim and not f.header.silence


@given(
    seq=st.integers(1, 0xFFFF_FFFF),
    offset_ms=st.integers(0, 0xFFFF_FFFF),
    flags=st.integers(0, 7),
    payload=st.binary(min_size=1, max_size=codec.MAX_PAYLOAD),
)
def test_roundtrip_property(seq, offset_ms, flags, payload):
    h = codec.FrameHeader(seq=seq, offset_ms=offset_ms, flags=flags)
    raw = codec.encode_frame(h, payload)
    assert codec.decode_frame(raw) == h
    assert raw[codec.HEADER_LEN :] == payload


@pytest.mark.parametrize(
    ("raw", "exc"),
    [
        (b"\x57\x43\x01\x00\x01\x00\x00", codec.FrameTooShort),
        (frame(magic=0x4358), codec.BadMagic),
        (frame(ver=2), codec.UnsupportedVersion),
        (frame(flags=0x08), codec.UnknownFlags),
        (frame(seq=0), codec.BadSeq),
        (frame(payload=b""), codec.EmptyPayload),
        (frame(payload=b"\x00" * (codec.MAX_PAYLOAD + 1)), codec.PayloadTooLarge),
    ],
)
def test_malformed_frames_map_to_4010(raw, exc):
    with pytest.raises(exc) as info:
        codec.decode_frame(raw)
    assert isinstance(info.value, codec.FrameError)
    assert info.value.close_code is CloseCode.BAD_FRAME
    assert info.value.reason == exc.reason


def test_max_payload_is_accepted_exactly():
    h = codec.decode_frame(frame(payload=b"\x00" * codec.MAX_PAYLOAD))
    assert h.seq == 1


def test_sim_frame_refused_when_not_allowed_maps_to_4005():
    raw = frame(flags=codec.FLAG_SIM)
    assert codec.decode_frame(raw, allow_sim=True).sim
    with pytest.raises(codec.SimNotAllowed) as info:
        codec.decode_frame(raw, allow_sim=False)
    assert info.value.close_code is CloseCode.BAD_HELLO


def test_decode_accepts_memoryview_and_bytearray():
    raw = frame(seq=3)
    assert codec.decode_frame(memoryview(raw)).seq == 3
    assert codec.decode(bytearray(raw)).payload == b"\x00\x01"


@pytest.mark.parametrize(
    ("header", "payload", "exc"),
    [
        (codec.FrameHeader(seq=0, offset_ms=0), b"x", codec.BadSeq),
        (codec.FrameHeader(seq=1 << 32, offset_ms=0), b"x", codec.BadSeq),
        (codec.FrameHeader(seq=1, offset_ms=1 << 32), b"x", codec.FrameError),
        (codec.FrameHeader(seq=1, offset_ms=0, flags=0x80), b"x", codec.UnknownFlags),
        (codec.FrameHeader(seq=1, offset_ms=0), b"", codec.EmptyPayload),
        (codec.FrameHeader(seq=1, offset_ms=0), b"x" * (codec.MAX_PAYLOAD + 1), codec.PayloadTooLarge),
    ],
)
def test_encoder_rejects_what_decoder_would_reject(header, payload, exc):
    with pytest.raises(exc):
        codec.encode_frame(header, payload)
