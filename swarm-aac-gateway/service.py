"""Paid, read-only Swarm station retrieval over session-scoped Trickle."""

import asyncio
import logging

from aiohttp import web
from eth_utils import is_address
from livepeer_gateway.trickle_publisher import TricklePublisher
from livepeer_swarm.bee import BeeClient
from livepeer_swarm_radio.radio import Radio

APP_ID = "livepeer-example/swarm-aac-gateway"
log = logging.getLogger(__name__)


class Playback:
    def __init__(self, sid, owner, station, channels, bee_url, idle_timeout):
        self.sid, self.owner, self.station = sid, owner, station
        self.channels = channels
        self.bee = BeeClient(bee_url)
        self.radio = Radio(self.bee, owner, station)
        self.queue = self.radio.subscribe()
        out = channels[0]
        self.publisher = TricklePublisher(
            out.get("internal_url", out["url"]), "audio/aac"
        )
        self.idle_timeout = idle_timeout
        self.error = None
        self.closed = False
        self.poller = asyncio.create_task(self.radio.run())
        self.sender = asyncio.create_task(self.send())

    async def send(self):
        try:
            while True:
                audio = await asyncio.wait_for(self.queue.get(), self.idle_timeout)
                if audio is None:
                    break
                # One complete ADTS segment per Trickle sequence; no re-encoding.
                async with asyncio.timeout(10):
                    async with await self.publisher.next() as segment:
                        await segment.write(audio)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = type(exc).__name__
            log.exception("AAC playback stopped session=%s", self.sid)
        finally:
            await self.shutdown()

    async def shutdown(self):
        self.closed = True
        self.radio.unsubscribe(self.queue)
        self.poller.cancel()
        await asyncio.gather(self.poller, return_exceptions=True)
        await self.publisher.close()
        await self.bee.close()

    async def close(self):
        if not self.sender.done():
            self.sender.cancel()
        await asyncio.gather(self.sender, return_exceptions=True)
        # Also covers cancellation before the sender coroutine first executes.
        await self.shutdown()

    def response(self):
        return {
            "session": self.sid,
            "protocol": 1,
            "out": self.channels[0]["url"],
            "mime_type": "audio/aac",
            "codec": "aac-lc",
            "container": "adts",
        }


class Gateway:
    def __init__(self, bee_url, funding=None, idle_timeout=30):
        self.bee_url, self.funding = bee_url, funding
        self.idle_timeout = idle_timeout
        self.playback = None
        self.lock = asyncio.Lock()

    def attach(self, app):
        app.router.add_post("/start", self.start)
        app.router.add_get("/status", self.status)
        app.router.add_get("/healthz", self.health)
        app.on_cleanup.append(self.cleanup)

    @staticmethod
    def session_id(request):
        sid = request.headers.get("Livepeer-Session-Id")
        if not sid:
            raise web.HTTPBadRequest(text="missing Livepeer session ID")
        return sid

    async def start(self, request):
        sid = self.session_id(request)
        try:
            body = await request.json()
            if set(body) != {"owner", "station"}:
                raise ValueError()
            owner, station = body["owner"], body["station"]
            if not is_address(owner) or not isinstance(station, str):
                raise ValueError()
            if not 1 <= len(station.encode()) <= 256 or any(
                ord(c) < 32 for c in station
            ):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise web.HTTPBadRequest(
                text="provide only a valid owner and station"
            ) from None
        async with self.lock:
            if self.playback:
                p = self.playback
                if (p.sid, p.owner.lower(), p.station) != (
                    sid,
                    owner.lower(),
                    station,
                ) or p.closed:
                    raise web.HTTPConflict(
                        text="runner already used; release session before changing station"
                    )
                return web.json_response(p.response())
            if self.funding:
                try:
                    await self.funding.require_funding()
                except Exception:
                    raise web.HTTPServiceUnavailable(
                        text="operator retrieval funding unavailable"
                    ) from None
            channels = await request.app["registration"].create_trickle_channels(
                request, [{"name": "out", "mime_type": "audio/aac"}]
            )
            self.playback = Playback(
                sid, owner, station, channels, self.bee_url, self.idle_timeout
            )
            return web.json_response(self.playback.response())

    async def status(self, request):
        sid = self.session_id(request)
        p = self.playback
        if not p or p.sid != sid:
            raise web.HTTPNotFound()
        return web.json_response(
            {**p.radio.status(), "closed": p.closed, "error": p.error}
        )

    async def health(self, request):
        return web.json_response(
            {
                "ok": True,
                "capability": APP_ID,
                "sessions": int(self.playback is not None),
            }
        )

    async def released(self, event):
        async with self.lock:
            if self.playback and self.playback.sid == event.session_id:
                await self.playback.close()
                self.playback = None

    async def cleanup(self, app):
        if self.playback:
            await self.playback.close()
            self.playback = None
