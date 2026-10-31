"""Four-channel Live Runner adapter for the keyless upload pipeline."""

import asyncio
import base64
import logging
import math
from contextlib import suppress

from aiohttp import web
from eth_utils import is_address
from livepeer_gateway.channel_reader import JSONLReader
from livepeer_gateway.channel_writer import JSONLWriter
from livepeer_gateway.trickle_subscriber import TrickleSubscriber

from .bee import BeeClient, verify_stamp
from .feeds import feed_address, feed_id
from .live import MAX_UPLOAD_BYTES, UploadPipeline

log = logging.getLogger(__name__)


class RunnerSession:
    def __init__(self, session_id, channels, owner, batch, bee_url):
        self.id, self.channels, self.owner, self.batch = (
            session_id,
            channels,
            owner,
            batch,
        )
        urls = {c["name"]: c.get("internal_url", c["url"]) for c in channels}
        self.events = JSONLWriter(urls["events"])
        self.bee = BeeClient(bee_url)
        self.pipeline = UploadPipeline(self.bee, owner, batch, self.emit)
        self.metadata = {}
        self.metadata_changed = asyncio.Event()
        self.closed = False
        self.closing = None
        self.tasks = [
            asyncio.create_task(self.guard(self.control(urls["control"]))),
            asyncio.create_task(self.guard(self.stamps(urls["stamps"]))),
            asyncio.create_task(self.guard(self.media(urls["in"]))),
        ]

    async def emit(self, message):
        await self.events.write({**message, "session": self.id})

    async def guard(self, coro):
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Swarm session failed")
            with suppress(Exception):
                await self.emit({"type": "error", "detail": str(exc)[:200]})
            asyncio.create_task(self.close())

    async def control(self, url):
        async for message in JSONLReader(url)(start_seq=0):
            if message.get("session") != self.id:
                raise ValueError("control session mismatch")
            op = message.get("op")
            if op == "ready":
                await self.emit({"type": "ready", "request_id": message["request_id"]})
            elif op == "meta":
                uid = message["upload_id"]
                duration = float(message.get("duration", 0))
                if (
                    len(uid) != 32
                    or not math.isfinite(duration)
                    or not 0 <= duration <= 10
                ):
                    raise ValueError("invalid upload metadata")
                if len(self.metadata) >= 8 and uid not in self.metadata:
                    raise ValueError("too many pending uploads")
                self.metadata[uid] = message
                self.metadata_changed.set()
            elif op == "soc":
                if message["owner"].lower() != self.owner.lower():
                    raise ValueError("feed owner differs from configured publisher")
                if bytes.fromhex(message["id"]) != feed_id(
                    message["topic"], int(message["index"])
                ):
                    raise ValueError("feed topic/index mismatch")
                address = feed_address(
                    self.owner, message["topic"], int(message["index"])
                )
                if address.hex() != message["address"]:
                    raise ValueError("feed address mismatch")
                verify_stamp(
                    address, bytes.fromhex(message["stamp"]), self.owner, self.batch
                )
                if len(base64.b64decode(message["body"], validate=True)) > 4104:
                    raise ValueError("oversized feed")
                await self.bee.put_soc(message)
                await self.emit(
                    {
                        "type": "feed",
                        "request_id": message["request_id"],
                        "index": message["index"],
                        "ref": address.hex(),
                    }
                )
            elif op == "stop":
                await self.emit(
                    {"type": "stopped", "request_id": message["request_id"]}
                )
                return
            else:
                raise ValueError("unknown control operation")

    async def stamps(self, url):
        async for message in JSONLReader(url)(start_seq=0):
            if message.get("session") != self.id:
                raise ValueError("stamp session mismatch")
            self.pipeline.answer(message)

    async def media(self, url):
        async with TrickleSubscriber(
            url, start_seq=0, max_bytes=MAX_UPLOAD_BYTES + 32
        ) as subscriber:
            while (segment := await subscriber.next()) is not None:
                try:
                    reader, body = segment.make_reader(), bytearray()
                    while part := await reader.read():
                        body.extend(part)
                finally:
                    await segment.close()
                uid = bytes(body[:32]).decode("ascii")
                async with asyncio.timeout(10):
                    while uid not in self.metadata:
                        self.metadata_changed.clear()
                        await self.metadata_changed.wait()
                meta = self.metadata.pop(uid)
                result = await self.pipeline.publish(uid, bytes(body[32:]))
                await self.emit({**result, "kind": meta["kind"]})

    async def close(self):
        if self.closing is None:
            self.closing = asyncio.create_task(self._close())
        await asyncio.shield(self.closing)

    async def _close(self):
        self.closed = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.events.close()
        await self.bee.close()


class LiveService:
    def __init__(self, bee_url, operator_funding=None):
        self.operator_funding = operator_funding
        self.bee_url = bee_url
        self.sessions = {}
        self.lock = asyncio.Lock()

    def attach(self, app):
        app.router.add_post("/start", self.start)
        app.router.add_get("/healthz", self.health)
        if self.operator_funding:
            app.router.add_get("/operator/bandwidth", self.bandwidth)
        app.on_cleanup.append(self.cleanup)

    async def bandwidth(self, request):
        # Administrative route: keep the runner bound to loopback.
        try:
            snapshot = await self.operator_funding.snapshot()
        except Exception:
            raise web.HTTPServiceUnavailable(
                text="operator accounting unavailable"
            ) from None
        return web.json_response(snapshot, status=200 if snapshot["funded"] else 503)

    async def health(self, request):
        return web.json_response(
            {
                "ok": True,
                "sessions": len(self.sessions),
                "bee_configured": bool(self.bee_url),
            }
        )

    async def start(self, request):
        if not self.bee_url:
            raise web.HTTPServiceUnavailable(
                text="BEE_URL is required for live publishing"
            )
        sid = request.headers.get("Livepeer-Session-Id")
        if not sid:
            raise web.HTTPBadRequest(text="missing session ID")
        payload = await request.json()
        owner = payload.get("owner", "")
        try:
            batch = bytes.fromhex(payload.get("batch_id", ""))
            if not is_address(owner) or len(batch) != 32:
                raise ValueError()
        except ValueError:
            raise web.HTTPBadRequest(text="invalid publisher owner/batch") from None
        async with self.lock:
            if sid not in self.sessions:
                if self.sessions:
                    raise web.HTTPConflict(
                        text="runner already has an active broadcast"
                    )
                if self.operator_funding:
                    try:
                        snapshot = await self.operator_funding.require_funding()
                    except Exception:
                        log.exception("operator funding check failed")
                        raise web.HTTPServiceUnavailable(
                            text="operator bandwidth funding unavailable; contact service operator"
                        ) from None
                    log.info(
                        "operator_bandwidth_start session=%s accounting=%s",
                        sid,
                        snapshot,
                    )
                channels = await request.app["registration"].create_trickle_channels(
                    request,
                    [
                        {
                            "name": name,
                            "mime_type": "application/octet-stream"
                            if name == "in"
                            else "application/jsonl",
                        }
                        for name in ("in", "events", "stamps", "control")
                    ],
                )
                self.sessions[sid] = RunnerSession(
                    sid, channels, owner, batch, self.bee_url
                )
            session = self.sessions[sid]
            if (
                session.closed
                or session.owner.lower() != owner.lower()
                or session.batch != batch
            ):
                raise web.HTTPConflict(text="session is closed or publisher changed")
            return web.json_response(
                {
                    "session": sid,
                    "protocol": 1,
                    **{c["name"]: c["url"] for c in session.channels},
                }
            )

    async def released(self, event):
        session = self.sessions.get(event.session_id)
        if session:
            await session.close()
            self.sessions.pop(event.session_id, None)
            if self.operator_funding:
                try:
                    snapshot = await self.operator_funding.snapshot()
                    log.info(
                        "operator_bandwidth_end session=%s accounting=%s",
                        event.session_id,
                        snapshot,
                    )
                except Exception:
                    log.exception("could not record final operator accounting")

    async def cleanup(self, app):
        await asyncio.gather(*(s.close() for s in self.sessions.values()))
        self.sessions.clear()
