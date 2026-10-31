"""Bounded Bee I/O using publisher-supplied stamps; never purchases postage."""

import asyncio
import base64
from collections import OrderedDict

import aiohttp
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_hash.auto import keccak

from .bmt import chunk_address, pyramid
from .feeds import feed_address, verify_soc


def verify_stamp(address: bytes, stamp: bytes, owner: str, batch: bytes):
    if len(stamp) != 113 or stamp[:32] != batch:
        raise ValueError("invalid postage stamp batch or length")
    recovered = Account.recover_message(
        encode_defunct(primitive=keccak(address + stamp[:48])), signature=stamp[48:]
    )
    if recovered.lower() != owner.lower():
        raise ValueError("postage signature does not match publisher")


class BeeClient:
    def __init__(self, url, *, concurrency=8, timeout=5):
        self.url = url.rstrip("/")
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        )
        self.slots = asyncio.Semaphore(concurrency)
        self.frontiers = OrderedDict()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def close(self):
        await self.session.close()

    async def put(self, route, data, stamp, expected):
        async with self.slots:
            for attempt in range(3):
                try:
                    async with self.session.post(
                        self.url + route,
                        data=data,
                        headers={
                            "Swarm-Postage-Stamp": stamp,
                            "Swarm-Deferred-Upload": "false",
                            "Content-Type": "application/octet-stream",
                        },
                    ) as response:
                        response.raise_for_status()
                        result = await response.json()
                        if result.get("reference", "").lower() != expected.hex():
                            raise ValueError("Bee returned a different reference")
                        return
                except (aiohttp.ClientError, TimeoutError) as exc:
                    if (
                        isinstance(exc, aiohttp.ClientResponseError)
                        and exc.status < 500
                        and exc.status != 429
                    ):
                        raise
                    if attempt == 2:
                        raise
                    await asyncio.sleep(0.1 * 2**attempt)

    async def put_chunk(self, body, stamp):
        address = chunk_address(int.from_bytes(body[:8], "little"), body[8:])
        await self.put("/chunks", body, stamp, address)

    async def put_soc(self, command):
        body = base64.b64decode(command["body"], validate=True)
        address = bytes.fromhex(command["address"])
        data = bytes.fromhex(command["id"]) + bytes.fromhex(command["sig"]) + body
        verify_soc(data, address)
        owner = command["owner"].removeprefix("0x").lower()
        if keccak(bytes.fromhex(command["id"]) + bytes.fromhex(owner)) != address:
            raise ValueError("SOC upload owner mismatch")
        await self.put(
            f"/soc/{owner}/{command['id']}?sig={command['sig']}",
            body,
            command["stamp"],
            address,
        )

    async def read(self, route, *, limit=2_000_000):
        async with (
            self.slots,
            self.session.get(
                self.url + route, headers={"Cache-Control": "no-cache"}
            ) as response,
        ):
            if response.status == 404:
                return None
            response.raise_for_status()
            data = bytearray()
            async for part in response.content.iter_chunked(65536):
                data.extend(part)
                if len(data) > limit:
                    raise ValueError("Bee response exceeds limit")
            return bytes(data)

    async def read_bytes(self, reference, *, limit=256 * 1024):
        data = await self.read("/bytes/" + reference, limit=limit)
        if data is None:
            return None

        def root():
            span, payload = pyramid(data)[-1]
            return chunk_address(span, payload).hex()

        if await asyncio.to_thread(root) != reference.lower():
            raise ValueError("media does not match its Swarm reference")
        return data

    async def feed_at(self, owner, topic, index):
        address = feed_address(owner, topic, index)
        data = await self.read("/chunks/" + address.hex(), limit=4201)
        return None if data is None else verify_soc(data, address)

    async def latest_feed(self, owner, topic, *, floor=0):
        """Probe contiguous raw SOC feeds; every accepted payload is authenticated.

        A missing chunk is not proof of nonexistence. Callers retain their last
        confirmed frontier and poll again rather than rolling it backwards.
        """
        key = (owner.lower(), topic)
        cached = self.frontiers.get(key)
        if cached and cached[0] >= floor:
            floor, payload = cached
        else:
            payload = await self.feed_at(owner, topic, floor)
        if payload is None:
            # Bee's frontier header is only a hint. Authenticate the actual SOC
            # at the hinted address before using it (never trust response content).
            route = f"/feeds/{owner.removeprefix('0x')}/{keccak(topic.encode()).hex()}?type=sequence"
            async with self.session.get(self.url + route) as response:
                hint = (
                    response.headers.get("Swarm-Feed-Index")
                    if response.status == 200
                    else None
                )
            if hint:
                index = int(hint, 16)
                if floor <= index < (1 << 63):
                    candidate = await self.feed_at(owner, topic, index)
                    if candidate is not None:
                        floor, payload = index, candidate
        if payload is None:
            return None

        async def probe(index):
            # A slow miss must not prevent playback of already authenticated
            # content. Retry this frontier on the next poll; never rewind it.
            try:
                async with asyncio.timeout(0.7):
                    return await self.feed_at(owner, topic, index)
            except (TimeoutError, aiohttp.ClientError):
                return None

        low, step = floor, 1
        for _ in range(32):
            high = floor + step
            candidate = await probe(high)
            if candidate is None:
                break
            low, payload = high, candidate
            step *= 2
        else:
            raise ValueError("feed search exceeded bound")
        while high - low > 1:
            mid = (low + high) // 2
            candidate = await probe(mid)
            if candidate is None:
                high = mid
            else:
                low, payload = mid, candidate
        self.frontiers[key] = (low, payload)
        self.frontiers.move_to_end(key)
        if len(self.frontiers) > 256:
            self.frontiers.popitem(last=False)
        return low, payload
