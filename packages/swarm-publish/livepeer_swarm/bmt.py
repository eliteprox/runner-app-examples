"""Swarm binary Merkle tree, matching pub.swarmsign.Cac."""

from __future__ import annotations

from eth_hash.auto import keccak

CHUNK_SIZE = 4096
SEGMENT_SIZE = 32
SEGMENT_COUNT = CHUNK_SIZE // SEGMENT_SIZE


def bmt_root(payload: bytes) -> bytes:
    """Hash one chunk payload. Shorter payloads are zero-padded to 128 segments."""
    if len(payload) > CHUNK_SIZE:
        raise ValueError("payload exceeds 4096 bytes")
    level = []
    for index in range(SEGMENT_COUNT):
        start = index * SEGMENT_SIZE
        segment = bytearray(SEGMENT_SIZE)
        if start < len(payload):
            end = min(start + SEGMENT_SIZE, len(payload))
            segment[: end - start] = payload[start:end]
        level.append(bytes(segment))
    while len(level) > 1:
        level = [
            keccak(level[index] + level[index + 1]) for index in range(0, len(level), 2)
        ]
    return level[0]


def span_bytes(span: int) -> bytes:
    if span < 0:
        raise ValueError("span must be non-negative")
    return span.to_bytes(8, "little")


def chunk_address(span: int, payload: bytes) -> bytes:
    return keccak(span_bytes(span) + bmt_root(payload))


def chunk_data(span: int, payload: bytes) -> bytes:
    """Bee chunk body: 8-byte little-endian span, then the payload."""
    return span_bytes(span) + payload


def pyramid(data: bytes) -> list[tuple[int, bytes]]:
    """Split data into (span, payload) pairs, leaves first and the root last.

    A file that fits in one chunk is a single pair. Larger files add parent
    chunks whose payload is the concatenation of child addresses and whose
    span is the number of original bytes those children cover.
    """
    if len(data) <= CHUNK_SIZE:
        return [(len(data), data)]

    level: list[tuple[int, bytes]] = []
    for start in range(0, len(data), CHUNK_SIZE):
        piece = data[start : start + CHUNK_SIZE]
        level.append((len(piece), piece))

    chunks = list(level)
    while len(level) > 1:
        parents: list[tuple[int, bytes]] = []
        for start in range(0, len(level), SEGMENT_COUNT):
            group = level[start : start + SEGMENT_COUNT]
            payload = b"".join(chunk_address(span, body) for span, body in group)
            span = sum(span for span, _body in group)
            parents.append((span, payload))
        chunks.extend(parents)
        level = parents
    return chunks
