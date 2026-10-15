#!/usr/bin/env python3
"""swarm-publish-demo: a tiny cap that returns a result and can publish to Swarm.

The WebSocket carries packed chunks up. The only message that comes back is a
short JSON result. When BEE_URL is set, the same frames are posted to Bee and
are not written to the socket.

Livepeer integration (grep `# Livepeer:`):
  1. register_runner()     — announce the app to the orchestrator (startup)
  2. registration.close()  — deregister (cleanup)
"""

from __future__ import annotations

import argparse
import logging
import os
from contextlib import suppress

import aiohttp
from aiohttp import web
from livepeer_gateway.live_runner import register_runner
from livepeer_swarm import SwarmPublisher, SwarmPublishError, unwrap

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8989
APP_ID = "livepeer-example/swarm-publish-demo"

log = logging.getLogger("swarm-publish-demo")
publisher = SwarmPublisher(os.environ.get("BEE_URL"))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live Runner swarm-publish demo.")
    parser.add_argument("--orchestrator", default="https://localhost:8935")
    parser.add_argument("--orchSecret", default="abcdef")
    parser.add_argument("--runner-url", default=f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help="Bind address (use 0.0.0.0 in containers)."
    )
    parser.add_argument(
        "--price",
        type=float,
        default=0,
        help="Runner price in USD per hour (0 = free, the offchain default).",
    )
    return parser.parse_args()


async def _handle_ingest(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    received = bytearray()
    async for msg in ws:
        if msg.type == web.WSMsgType.BINARY:
            frame = msg.data
            payload = unwrap(frame) if publisher.enabled else frame
            received.extend(payload)
            try:
                await publisher.put(frame)
            except (SwarmPublishError, aiohttp.ClientError):
                log.exception("swarm publish failed")
                await ws.close()
                return ws
        elif msg.type == web.WSMsgType.TEXT and msg.data.strip() == "eos":
            count = len(received)
            await ws.send_json({"text": f"received {count} bytes", "byte_count": count})
            break
        elif msg.type == web.WSMsgType.ERROR:
            log.warning("ws error: %s", ws.exception())
            break
    log.info("ingest closed, %s bytes", len(received))
    return ws


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()
    if publisher.enabled:
        log.info("swarm publish on %s", os.environ.get("BEE_URL"))
    else:
        log.info("swarm publish off")

    async def _on_startup(app: web.Application) -> None:
        app["registration"] = await register_runner(  # Livepeer: 1
            args.orchestrator,
            secret=args.orchSecret,
            runner_url=args.runner_url,
            app=APP_ID,
            mode="persistent",
            price=args.price,  # USD per hour
        )
        log.info(
            "registered runner_id=%s orchestrator=%s",
            app["registration"].runner_id,
            app["registration"].orchestrator_url,
        )

    async def _on_cleanup(app: web.Application) -> None:
        await publisher.aclose()
        with suppress(Exception):
            await app["registration"].close()  # Livepeer: 2

    app = web.Application()
    app.router.add_get("/ingest", _handle_ingest)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    web.run_app(app, host=args.host, port=DEFAULT_PORT)


if __name__ == "__main__":
    main()
