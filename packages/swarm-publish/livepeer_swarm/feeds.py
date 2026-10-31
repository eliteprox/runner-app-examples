"""Donkey-push raw-payload sequence feeds (not Bee reference envelopes)."""

from dataclasses import dataclass

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_hash.auto import keccak

from .bmt import chunk_address, chunk_data
from .stamp import personal_sign


def feed_id(topic: str, index: int) -> bytes:
    return keccak(keccak(topic.encode()) + index.to_bytes(8, "big"))


def feed_address(owner: str, topic: str, index: int) -> bytes:
    return keccak(feed_id(topic, index) + bytes.fromhex(owner.removeprefix("0x")))


@dataclass(frozen=True)
class SignedFeed:
    owner: str
    id: bytes
    signature: bytes
    body: bytes

    @property
    def address(self) -> bytes:
        return keccak(self.id + bytes.fromhex(self.owner.removeprefix("0x")))

    @property
    def data(self) -> bytes:
        return self.id + self.signature + self.body


def sign_feed(topic: str, index: int, payload: bytes, signer) -> SignedFeed:
    if len(payload) > 4096:
        raise ValueError("raw feed payload exceeds a Swarm chunk")
    ident = feed_id(topic, index)
    cac = chunk_address(len(payload), payload)
    return SignedFeed(
        signer.address,
        ident,
        personal_sign(keccak(ident + cac), signer),
        chunk_data(len(payload), payload),
    )


def verify_soc(data: bytes, address: bytes) -> bytes:
    if not 105 <= len(data) <= 4201:
        raise ValueError("invalid SOC length")
    ident, signature, body = data[:32], data[32:97], data[97:]
    payload = body[8:]
    span = int.from_bytes(body[:8], "little")
    if span != len(payload):
        raise ValueError("expected an inline feed payload")
    digest = keccak(ident + chunk_address(span, payload))
    owner = Account.recover_message(
        encode_defunct(primitive=digest), signature=signature
    )
    if keccak(ident + bytes.fromhex(owner[2:])) != address:
        raise ValueError("SOC owner/address mismatch")
    return payload
