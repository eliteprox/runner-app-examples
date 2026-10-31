"""Postage stamps in the 113-byte layout produced by pub.swarmsign.StampIssuer."""

from __future__ import annotations

import time
from dataclasses import dataclass

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount
from eth_hash.auto import keccak

from livepeer_swarm.bmt import chunk_address, chunk_data, pyramid

STAMP_SIZE = 113
SLOT_MEMO_MAX = 65536


class BucketFull(Exception):
    def __init__(self, bucket: int) -> None:
        self.bucket = bucket
        super().__init__(f"bucket {bucket} is full on this batch")


def _as_account(signer: LocalAccount | str | bytes) -> LocalAccount:
    if isinstance(signer, LocalAccount):
        return signer
    return Account.from_key(signer)


def personal_sign(digest: bytes, signer: LocalAccount | str | bytes) -> bytes:
    """EIP-191 signature over an already-hashed 32-byte digest. 65 bytes, v = 27 or 28."""
    account = _as_account(signer)
    signed = Account.sign_message(encode_defunct(primitive=digest), account.key)
    signature = bytes(signed.signature)
    if len(signature) != 65:
        raise RuntimeError("signature must be 65 bytes")
    return signature


class StampIssuer:
    """Assigns bucket slots and signs one stamp per chunk address.

    Bucket depth defaults to 16, matching StampIssuer.load on the phone.
    """

    def __init__(
        self,
        batch_id: bytes,
        depth: int = 20,
        bucket_depth: int = 16,
        immutable: bool = False,
    ) -> None:
        if len(batch_id) != 32:
            raise ValueError("batch id is 32 bytes")
        if not 0 < bucket_depth < depth:
            raise ValueError("bucket depth must be between 0 and depth")
        self._batch_id = batch_id
        self._depth = depth
        self._bucket_depth = bucket_depth
        self._immutable = immutable
        self._buckets = [0] * (1 << bucket_depth)
        self._slots: dict[str, int] = {}
        self._bucket_upper_bound = 1 << (depth - bucket_depth)

    @property
    def batch_id(self) -> bytes:
        return self._batch_id

    def bucket_of(self, address: bytes) -> int:
        if len(address) != 32:
            raise ValueError("chunk address is 32 bytes")
        word = int.from_bytes(address[:4], "big")
        return word >> (32 - self._bucket_depth)

    def stamp(
        self,
        address: bytes,
        signer: LocalAccount | str | bytes,
        timestamp_ns: int | None = None,
    ) -> bytes:
        if len(address) != 32:
            raise ValueError("chunk address is 32 bytes")
        bucket = self.bucket_of(address)
        key = address.hex()
        slot = self._slots.get(key)
        if slot is None:
            slot = self._buckets[bucket]
            if slot == self._bucket_upper_bound:
                if self._immutable:
                    raise BucketFull(bucket)
                slot = 0
                self._buckets[bucket] = 0
            if self._buckets[bucket] < len(self._slots):
                self._forget(bucket, slot)
            self._buckets[bucket] += 1
            self._slots[key] = slot
            self._trim_slots()
        index = bucket.to_bytes(4, "big") + slot.to_bytes(4, "big")
        timestamp = (time.time_ns() if timestamp_ns is None else timestamp_ns).to_bytes(
            8, "big"
        )
        digest = keccak(address + self._batch_id + index + timestamp)
        signature = personal_sign(digest, signer)
        return self._batch_id + index + timestamp + signature

    def _forget(self, bucket: int, slot: int) -> None:
        for key, value in list(self._slots.items()):
            if value == slot and self.bucket_of(bytes.fromhex(key)) == bucket:
                del self._slots[key]
                return

    def _trim_slots(self) -> None:
        if len(self._slots) <= SLOT_MEMO_MAX:
            return
        drop = len(self._slots) - (SLOT_MEMO_MAX // 2)
        for key in list(self._slots)[:drop]:
            del self._slots[key]


@dataclass(frozen=True)
class StampedChunk:
    """One chunk the client can name before it is uploaded.

    chunk is the Bee body (span || payload). address is the Swarm reference.
    """

    address: bytes
    stamp: bytes
    chunk: bytes
    payload: bytes
    span: int


def chunk_and_stamp(
    payload: bytes,
    signer: LocalAccount | str | bytes,
    batch_id: bytes,
    issuer: StampIssuer | None = None,
    timestamp_ns: int | None = None,
) -> list[StampedChunk]:
    """Chunk payload and sign a stamp for every address, including parent chunks.

    The returned addresses are final. Nothing has been sent anywhere.
    Leaves come first, in file order. The last item is the root.
    """
    stamps = issuer if issuer is not None else StampIssuer(batch_id)
    if stamps.batch_id != batch_id:
        raise ValueError("issuer batch id does not match batch_id")
    stamped: list[StampedChunk] = []
    for span, body in pyramid(payload):
        address = chunk_address(span, body)
        stamp = stamps.stamp(address, signer, timestamp_ns)
        stamped.append(
            StampedChunk(
                address=address,
                stamp=stamp,
                chunk=chunk_data(span, body),
                payload=body,
                span=span,
            )
        )
    return stamped
