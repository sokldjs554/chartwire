"""Binary audio frame codec (spec §6.2).

Wire layout, little-endian, 12-byte header followed by the PCM payload::

    u16 magic = 0x4357 ('CW') | u8 ver = 1 | u8 flags | u32 seq (1-based) | u32 offset_ms | payload…

Every validation failure raises a :class:`FrameError` subclass that carries the WebSocket
close code the shell must use (``4010 bad_frame`` for malformed frames, ``4005`` when a
``SIM`` frame arrives while simulator frames are disabled).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from chartwire.ws.actions import CloseCode

MAGIC = 0x4357
VERSION = 1
HEADER_LEN = 12
MAX_PAYLOAD = 65_536
"""Largest accepted payload in bytes; a 200 ms PCM16LE mono 16 kHz chunk is 6,400 B."""

FLAG_LAST_CHUNK = 0x01
FLAG_SILENCE = 0x02
FLAG_SIM = 0x04
KNOWN_FLAGS = FLAG_LAST_CHUNK | FLAG_SILENCE | FLAG_SIM

_HEADER = struct.Struct("<HBBII")
_U32_MAX = 0xFFFF_FFFF


class FrameError(ValueError):
    """Base class of all frame validation errors; ``close_code`` is the protocol close code."""

    close_code: CloseCode = CloseCode.BAD_FRAME
    reason: str = "bad_frame"

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or self.reason)


class FrameTooShort(FrameError):
    reason = "frame_too_short"


class BadMagic(FrameError):
    reason = "bad_magic"


class UnsupportedVersion(FrameError):
    reason = "unsupported_version"


class UnknownFlags(FrameError):
    reason = "unknown_flags"


class BadSeq(FrameError):
    reason = "bad_seq"


class EmptyPayload(FrameError):
    reason = "empty_payload"


class PayloadTooLarge(FrameError):
    reason = "payload_too_large"


class SimNotAllowed(FrameError):
    close_code = CloseCode.BAD_HELLO
    reason = "sim_not_allowed"


@dataclass(frozen=True, slots=True)
class FrameHeader:
    """Decoded header. ``magic``/``ver`` are fixed by the protocol and therefore not stored."""

    seq: int
    offset_ms: int
    flags: int = 0

    @property
    def last_chunk(self) -> bool:
        return bool(self.flags & FLAG_LAST_CHUNK)

    @property
    def silence(self) -> bool:
        return bool(self.flags & FLAG_SILENCE)

    @property
    def sim(self) -> bool:
        return bool(self.flags & FLAG_SIM)


@dataclass(frozen=True, slots=True)
class Frame:
    header: FrameHeader
    payload: bytes


def encode_frame(h: FrameHeader, payload: bytes) -> bytes:
    """Serialise ``h`` + ``payload``; validates the same limits the decoder enforces."""
    _check_header(h)
    _check_payload_len(len(payload))
    return _HEADER.pack(MAGIC, VERSION, h.flags, h.seq, h.offset_ms) + payload


def decode_frame(b: bytes | bytearray | memoryview, *, allow_sim: bool = True) -> FrameHeader:
    """Validate a whole frame and return its header (the payload is ``b[HEADER_LEN:]``)."""
    if len(b) < HEADER_LEN:
        raise FrameTooShort
    magic, ver, flags, seq, offset_ms = _HEADER.unpack_from(b)
    if magic != MAGIC:
        raise BadMagic
    if ver != VERSION:
        raise UnsupportedVersion(f"ver={ver}")
    if flags & ~KNOWN_FLAGS:
        raise UnknownFlags(f"flags=0x{flags:02x}")
    if seq == 0:
        raise BadSeq("seq must be >= 1")
    _check_payload_len(len(b) - HEADER_LEN)
    if flags & FLAG_SIM and not allow_sim:
        raise SimNotAllowed
    return FrameHeader(seq=seq, offset_ms=offset_ms, flags=flags)


def decode(b: bytes | bytearray | memoryview, *, allow_sim: bool = True) -> Frame:
    """Convenience wrapper: header plus a ``bytes`` copy of the payload."""
    header = decode_frame(b, allow_sim=allow_sim)
    return Frame(header, bytes(b[HEADER_LEN:]))


def _check_header(h: FrameHeader) -> None:
    if h.flags & ~KNOWN_FLAGS or not 0 <= h.flags <= 0xFF:
        raise UnknownFlags(f"flags=0x{h.flags:02x}")
    if not 1 <= h.seq <= _U32_MAX:
        raise BadSeq(f"seq={h.seq}")
    if not 0 <= h.offset_ms <= _U32_MAX:
        raise FrameError(f"offset_ms={h.offset_ms} out of u32 range")


def _check_payload_len(n: int) -> None:
    if n == 0:
        raise EmptyPayload
    if n > MAX_PAYLOAD:
        raise PayloadTooLarge(f"payload={n} > {MAX_PAYLOAD}")
