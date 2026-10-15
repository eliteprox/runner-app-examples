"""A Bee stand-in that accepts pre-signed chunks and keeps them in memory.

Run with `python -m livepeer_swarm.stub`. It checks that Swarm-Postage-Stamp
is 113 bytes and answers 201 with the chunk address. It does not talk to a
postage contract, so any well-formed stamp is stored.
"""

from __future__ import annotations

import argparse
import logging

from aiohttp import web

from livepeer_swarm.bmt import CHUNK_SIZE, chunk_address
from livepeer_swarm.stamp import STAMP_SIZE

log = logging.getLogger("livepeer_swarm.stub")
CHUNKS: web.AppKey[dict[str, bytes]] = web.AppKey("chunks", dict)


def build_app() -> web.Application:
    app = web.Application()
    app[CHUNKS] = {}
    app.router.add_post("/chunks", _handle_chunks)
    return app


async def _handle_chunks(request: web.Request) -> web.Response:
    stamp_hex = request.headers.get("Swarm-Postage-Stamp", "")
    try:
        stamp = bytes.fromhex(stamp_hex)
    except ValueError:
        stamp = b""
    if len(stamp) != STAMP_SIZE:
        return web.Response(status=400, text="stamp must be 113 bytes")
    body = await request.read()
    if len(body) < 8 or len(body) > 8 + CHUNK_SIZE:
        return web.Response(
            status=400, text="chunk must be span plus at most 4096 bytes"
        )
    payload = body[8:]
    span = int.from_bytes(body[:8], "little")
    address = chunk_address(span, payload)
    request.app[CHUNKS][address.hex()] = body
    log.info("stored %s (%s bytes)", address.hex(), len(body))
    return web.json_response({"reference": address.hex()}, status=201)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(
        description="Stub Bee for pre-signed chunk uploads."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1633)
    args = parser.parse_args()
    web.run_app(build_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
