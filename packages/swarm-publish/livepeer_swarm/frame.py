"""Frame one stamped chunk as stamp || chunk. No orchestrator types in here."""

from __future__ import annotations

from livepeer_swarm.bmt import CHUNK_SIZE, chunk_address
from livepeer_swarm.stamp import STAMP_SIZE, StampedChunk


def pack(stamped: StampedChunk) -> bytes:
    """113-byte stamp followed by the Bee chunk body."""
    if len(stamped.stamp) != STAMP_SIZE:
        raise ValueError("stamp must be 113 bytes")
    return stamped.stamp + stamped.chunk


def unpack(frame: bytes) -> StampedChunk:
    """Split a frame. The address is recomputed from the chunk body."""
    if len(frame) < STAMP_SIZE + 8:
        raise ValueError("frame is shorter than a stamp plus a span")
    stamp = frame[:STAMP_SIZE]
    chunk = frame[STAMP_SIZE:]
    payload = chunk[8:]
    if len(payload) > CHUNK_SIZE:
        raise ValueError("payload exceeds 4096 bytes")
    span = int.from_bytes(chunk[:8], "little")
    return StampedChunk(
        address=chunk_address(span, payload),
        stamp=stamp,
        chunk=chunk,
        payload=payload,
        span=span,
    )


def unwrap(frame: bytes) -> bytes:
    """File bytes carried by this frame.

    Data chunks return their payload. Parent chunks, whose span is the covered
    file size rather than the payload length, return empty bytes so a pipeline
    that concatenates frames rebuilds the original input.
    """
    stamped = unpack(frame)
    if stamped.span == len(stamped.payload):
        return stamped.payload
    return b""
