"""Chunk, stamp, frame, and stub-Bee upload. No Livepeer session in here."""

from __future__ import annotations

import asyncio
import unittest

from aiohttp.test_utils import TestServer
from eth_account import Account
from eth_account.messages import encode_defunct
from livepeer_swarm import (
    STAMP_SIZE,
    SwarmPublisher,
    chunk_and_stamp,
    pack,
    unpack,
    unwrap,
)
from livepeer_swarm.stub import CHUNKS, build_app


class ChunkAndStampTest(unittest.TestCase):
    def setUp(self) -> None:
        self.account = Account.create()
        self.batch_id = bytes(range(32))
        self.timestamp_ns = 1_700_000_000_000_000_000

    def test_stamp_layout_and_recovery(self) -> None:
        payload = b"hello swarm"
        stamped = chunk_and_stamp(
            payload,
            self.account,
            self.batch_id,
            timestamp_ns=self.timestamp_ns,
        )
        self.assertEqual(len(stamped), 1)
        chunk = stamped[0]
        self.assertEqual(len(chunk.stamp), STAMP_SIZE)
        self.assertEqual(chunk.stamp[:32], self.batch_id)
        self.assertIn(chunk.stamp[-1], (27, 28))
        digest = chunk.stamp[48:113]
        self.assertEqual(len(digest), 65)
        recovered = Account.recover_message(
            encode_defunct(primitive=_digest(chunk)),
            signature=chunk.stamp[48:],
        )
        self.assertEqual(recovered, self.account.address)
        self.assertEqual(unwrap(pack(chunk)), payload)

    def test_large_file_reassembles_from_leaves_only(self) -> None:
        payload = bytes(range(256)) * 20  # 5120 bytes, two leaves and a parent
        stamped = chunk_and_stamp(
            payload,
            self.account,
            self.batch_id,
            timestamp_ns=self.timestamp_ns,
        )
        self.assertEqual(len(stamped), 3)
        self.assertGreater(stamped[-1].span, len(stamped[-1].payload))
        rebuilt = b"".join(unwrap(pack(chunk)) for chunk in stamped)
        self.assertEqual(rebuilt, payload)
        self.assertEqual(unpack(pack(stamped[0])).address, stamped[0].address)

    def test_disabled_publisher_does_not_read_the_frame(self) -> None:
        publisher = SwarmPublisher(None)

        async def _put() -> None:
            await publisher.put(b"not a frame")

        self.assertFalse(publisher.enabled)
        asyncio.run(_put())


def _digest(chunk) -> bytes:
    from eth_hash.auto import keccak

    return keccak(
        chunk.address + chunk.stamp[:32] + chunk.stamp[32:40] + chunk.stamp[40:48]
    )


class StubUploadTest(unittest.IsolatedAsyncioTestCase):
    async def test_put_stores_chunk_and_returns_nothing(self) -> None:
        server = TestServer(build_app())
        await server.start_server()
        publisher = SwarmPublisher(str(server.make_url("")).rstrip("/"))
        try:
            account = Account.create()
            stamped = chunk_and_stamp(b"audio-bytes", account, bytes(range(32)))
            result = await publisher.put(pack(stamped[0]))
            self.assertIsNone(result)
            stored = server.app[CHUNKS]
            self.assertIn(stamped[0].address.hex(), stored)
        finally:
            await publisher.aclose()
            await server.close()


if __name__ == "__main__":
    unittest.main()
