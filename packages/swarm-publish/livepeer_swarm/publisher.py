"""POST pre-signed chunks to Bee. Nothing here is sent back through Livepeer."""

from __future__ import annotations

import logging

import aiohttp

from livepeer_swarm.frame import unpack

log = logging.getLogger("livepeer_swarm")


class SwarmPublishError(Exception):
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"bee rejected chunk: HTTP {status}")


class SwarmPublisher:
    """Uploads packed frames when a Bee URL is configured.

    Construct with no URL, or an empty string, and put returns without doing
    any work. The HTTP status is logged for the operator. There is no return
    channel for references, segment events, or the bytes themselves.
    """

    def __init__(self, bee_url: str | None) -> None:
        self._bee_url = (bee_url or "").rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._bee_url)

    async def put(self, frame: bytes) -> None:
        if not self.enabled:
            return
        stamped = unpack(frame)
        session = await self._client()
        url = f"{self._bee_url}/chunks"
        headers = {"Swarm-Postage-Stamp": stamped.stamp.hex()}
        async with session.post(url, data=stamped.chunk, headers=headers) as response:
            body = await response.text()
            log.info("bee %s %s", response.status, stamped.address.hex())
            if response.status >= 300:
                log.error("bee rejected chunk: %s", body[:200])
                raise SwarmPublishError(response.status, body[:200])

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _client(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=60)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session
