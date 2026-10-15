#!/usr/bin/env python3
"""swarm-publish-demo client: stamp a file, send it, print the JSON result.

The audio (or any other bytes) are not read back. The socket returns one JSON
object from the cap.

Livepeer integration (grep `# Livepeer:`):
  1. reserve_session()      — discover the runner, reserve a session
  2. ws_connect()           — open the proxied WebSocket to the session URL
  3. stop_runner_session()  — end the session (settles payment on-chain)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import secrets
import ssl
import sys
from contextlib import suppress
from pathlib import Path

import aiohttp
from eth_account import Account
from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.live_runner import stop_runner_session
from livepeer_gateway.selection import reserve_session
from livepeer_swarm import chunk_and_stamp, pack

DEFAULT_DISCOVERY = "https://localhost:8935/discovery"
APP_ID = "livepeer-example/swarm-publish-demo"

log = logging.getLogger("swarm-publish-demo-client")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send a stamped file to the swarm-publish demo and print the result."
    )
    parser.add_argument("input", help="File to chunk, stamp, and send, or - for stdin.")
    parser.add_argument("--discovery", default=DEFAULT_DISCOVERY)
    parser.add_argument(
        "--signer", default="", help="Remote signer base URL (on-chain/paid path)."
    )
    return parser.parse_args()


def _read_input(path: str) -> bytes:
    if path == "-":
        return sys.stdin.buffer.read()
    file_path = Path(path).expanduser()
    if not file_path.exists():
        raise SystemExit(f"input file does not exist: {file_path}")
    return file_path.read_bytes()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()
    payload = _read_input(args.input.strip())
    account = Account.create()
    batch_id = secrets.token_bytes(32)
    chunks = chunk_and_stamp(payload, account, batch_id)
    log.info(
        "chunks=%s root=%s signer=%s",
        len(chunks),
        chunks[-1].address.hex(),
        account.address,
    )
    session = None
    try:
        session = await reserve_session(  # Livepeer: 1
            discovery_url=args.discovery,
            app=APP_ID,
            signer_url=args.signer.strip() or None,
        )
        log.info("session_id=%s app_url=%s", session.session_id, session.app_url)
        ws_url = (
            session.app_url.replace("https://", "wss://")
            .replace("http://", "ws://")
            .rstrip("/")
            + "/ingest"
        )
        ctx: ssl.SSLContext | None = None
        if ws_url.startswith("wss://"):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        async with (
            session,
            aiohttp.ClientSession() as client,
            client.ws_connect(
                ws_url, ssl=ctx or True, heartbeat=20
            ) as ws,  # Livepeer: 2
        ):
            for chunk in chunks:
                await ws.send_bytes(pack(chunk))
            await ws.send_str("eos")
            reply = await ws.receive()
            if reply.type != aiohttp.WSMsgType.TEXT:
                raise SystemExit(f"ERROR: expected a JSON result, got {reply.type}")
            print(json.dumps(reply.json()))
    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    finally:
        if session is not None:
            with suppress(Exception):
                await stop_runner_session(session)  # Livepeer: 3


if __name__ == "__main__":
    asyncio.run(main())
