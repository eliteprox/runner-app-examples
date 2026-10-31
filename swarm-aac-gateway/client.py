#!/usr/bin/env python3
"""Reserve a playback session and write continuous ADTS AAC to stdout."""

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from pathlib import Path
from contextlib import aclosing

from aiohttp import web

from livepeer_gateway.discovery import discover_orchestrator_runners
from livepeer_gateway.http import post_json
from livepeer_gateway.selection import reserve_session
from livepeer_gateway.trickle_subscriber import TrickleSubscriber
from livepeer_swarm_radio.client import validate_price

from service import APP_ID


async def audio_stream(args):
    started = time.monotonic()
    if not args.offchain:
        if not args.signer or args.max_hourly_usd is None:
            raise ValueError("paid playback requires --signer and --max-hourly-usd")
        entries = await discover_orchestrator_runners(args.orchestrator, app=APP_ID)
        validate_price(entries, args.max_hourly_usd, wei_cap=args.max_wei_per_second)
    logging.info("playback discovery_s=%.3f", time.monotonic() - started)
    headers = (
        json.loads(Path(args.signer_headers_file).read_text())
        if args.signer_headers_file
        else None
    )
    async with asyncio.timeout(args.seconds):
        async with await reserve_session(
            orchestrators=args.orchestrator,
            app=APP_ID,
            signer_url=args.signer or None,
            signer_headers=headers,
        ) as session:
            logging.info("playback reserved_after_s=%.3f", time.monotonic() - started)
            channels = await post_json(
                session.app_url.rstrip("/") + "/start",
                {"owner": args.owner, "station": args.station},
            )
            logging.info(
                "playback channels_ready_after_s=%.3f", time.monotonic() - started
            )
            first = True
            async with TrickleSubscriber(
                channels["out"], start_seq=0, max_bytes=256 * 1024
            ) as sub:
                while True:
                    async with asyncio.timeout(40):
                        segment = await sub.next()
                        if segment is None:
                            return
                        try:
                            reader = segment.make_reader()
                            while data := await reader.read():
                                if first:
                                    logging.info(
                                        "playback first_audio_after_s=%.3f",
                                        time.monotonic() - started,
                                    )
                                    first = False
                                yield data
                        finally:
                            await segment.close()


async def run(args):
    task = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    if not args.listen_port:
        async with aclosing(audio_stream(args)) as audio:
            async for data in audio:
                await asyncio.to_thread(sys.stdout.buffer.write, data)
                await asyncio.to_thread(sys.stdout.buffer.flush)
        return
    lock = asyncio.Lock()

    async def stream(request):
        if lock.locked():
            raise web.HTTPConflict(text="one VLC listener per playback session")
        async with lock:
            response = web.StreamResponse(
                headers={"Content-Type": "audio/aac", "Cache-Control": "no-store"}
            )
            try:
                async with aclosing(audio_stream(args)) as audio:
                    async for data in audio:
                        if not response.prepared:
                            await response.prepare(request)
                        await asyncio.wait_for(response.write(data), 5)
            except (ConnectionError, TimeoutError):
                if not response.prepared:
                    raise web.HTTPServiceUnavailable(
                        text="station unavailable"
                    ) from None
            return response

    app = web.Application()
    app.router.add_get("/stream.aac", stream)
    server = web.AppRunner(app, handler_cancellation=True, shutdown_timeout=5)
    await server.setup()
    try:
        await web.TCPSite(server, args.listen_host, args.listen_port).start()
        logging.info(
            "VLC URL: http://%s:%s/stream.aac (lifetime limit %.0fs)",
            args.listen_host,
            args.listen_port,
            args.seconds,
        )
        # No payment session until a listener connects. Global lifetime also caps reconnects.
        await asyncio.sleep(args.seconds)
    finally:
        await server.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orchestrator", action="append", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--station", required=True)
    parser.add_argument("--signer")
    parser.add_argument("--signer-headers-file")
    parser.add_argument("--max-hourly-usd", type=float)
    parser.add_argument("--max-wei-per-second", type=int)
    parser.add_argument("--seconds", type=float, default=300)
    parser.add_argument("--offchain", action="store_true")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument(
        "--listen-port", type=int, help="serve /stream.aac for one VLC listener"
    )
    args = parser.parse_args()
    if not 0 < args.seconds <= 86400:
        parser.error("--seconds must be between 0 and 86400")
    if args.offchain and args.signer:
        parser.error("--offchain cannot be combined with --signer")
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        asyncio.run(run(args))
    except (TimeoutError, KeyboardInterrupt, asyncio.CancelledError, BrokenPipeError):
        pass


if __name__ == "__main__":
    main()
