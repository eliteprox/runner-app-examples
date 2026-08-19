#!/usr/bin/env python3
"""Call the ffmpeg tool runner: pick an op, send a file, save the result.

Mirrors hello-world: discover a runner, then one `call_runner` POST to /run.
The input file is base64'd into the JSON request, so the standard buffered
call path (and its payment) carries it — fine for short clips.

  uv run client.py --op transcode --height 480 --input clip.mp4 --output out.mp4
  uv run client.py --op clip --start 1 --end 3   --input clip.mp4 --output cut.mp4
  uv run client.py --op thumbnail --at 1.5        --input clip.mp4 --output thumb.jpg

Livepeer integration (grep `# Livepeer:`):
  1. runner_selector()  — discover orchestrators advertising the app
  2. call_runner()      — call the app through the orchestrator; on the paid path it
                          answers the 402 payment challenge inline. Single-shot needs
                          no reserve/stop: the orchestrator reserves the session for
                          this one request and releases it when the response returns.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.live_runner import call_runner
from livepeer_gateway.selection import runner_selector

DEFAULT_DISCOVERY = "https://localhost:8935/discovery"
APP_ID = "livepeer/ffmpeg"
DEFAULT_OUTPUT = {
    "transcode": "out.mp4",
    "clip": "out.mp4",
    "thumbnail": "out.jpg",
    "extract_audio": "out.m4a",
    "gif": "out.gif",
    "crop": "out.mp4",
}  # convert -> out.<format> (computed); probe writes no file

log = logging.getLogger("ffmpeg-client")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an ffmpeg op through a Live Runner."
    )
    parser.add_argument("--discovery", default=DEFAULT_DISCOVERY)
    parser.add_argument(
        "--op",
        default="transcode",
        choices=[
            "transcode",
            "clip",
            "thumbnail",
            "extract_audio",
            "gif",
            "crop",
            "convert",
            "probe",
        ],
    )
    parser.add_argument("--input", required=True, help="Input media file.")
    parser.add_argument(
        "--output", default=None, help="Output path (default depends on op)."
    )
    # op params (only the ones relevant to --op are sent)
    parser.add_argument("--height", type=int, help="transcode/gif/crop: output height.")
    parser.add_argument(
        "--encoder", help="transcode: libx264 (cpu) or h264_nvenc (gpu)."
    )
    parser.add_argument(
        "--quality",
        type=int,
        help="transcode/convert: CRF/CQ, lower=higher quality (18 high, 23 default).",
    )
    parser.add_argument("--start", type=float, help="clip: start seconds.")
    parser.add_argument("--end", type=float, help="clip: end seconds.")
    parser.add_argument("--at", type=float, help="thumbnail: timestamp seconds.")
    parser.add_argument("--fps", type=int, help="gif: frames per second.")
    parser.add_argument("--width", type=int, help="crop: width (px).")
    parser.add_argument("--x", type=int, help="crop: left offset (px).")
    parser.add_argument("--y", type=int, help="crop: top offset (px).")
    parser.add_argument("--format", help="convert: target container (mkv/mov).")
    parser.add_argument(
        "--signer", default="", help="Remote signer base URL (on-chain/paid path)."
    )
    return parser.parse_args()


def _payload(args: argparse.Namespace, input_b64: str) -> dict:
    payload: dict = {"op": args.op, "input_b64": input_b64}
    for key in (
        "height",
        "encoder",
        "quality",
        "start",
        "end",
        "at",
        "fps",
        "width",
        "x",
        "y",
        "format",
    ):
        val = getattr(args, key)
        if val is not None:
            payload[key] = val
    return payload


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()
    signer_url = args.signer.strip() or None
    output = args.output or (
        f"out.{args.format or 'mp4'}"
        if args.op == "convert"
        else DEFAULT_OUTPUT.get(args.op)
    )
    with open(args.input, "rb") as f:
        input_b64 = base64.b64encode(f.read()).decode()
    try:
        cursor = await runner_selector(  # Livepeer: 1
            discovery_url=args.discovery,  # omit if the signer does discovery itself
            app=APP_ID,
        )
        runner = cursor.candidates[0]
        log.info("app_url=%s op=%s", runner.url, args.op)

        result = await call_runner(  # Livepeer: 2
            runner=runner,  # discovery metadata tells call_runner the price unit
            runner_url=runner.url.rstrip("/") + "/run",
            payload=_payload(args, input_b64),
            signer_url=signer_url,
            timeout=600.0,  # a media op can take a while
        )
        data = result.data
        if "analysis" in data:  # probe — print metadata, write no file
            print(json.dumps(data["analysis"], indent=2))
            return
        if "output_b64" not in data:
            raise SystemExit(f"ERROR: {data.get('error', data)}")
        with open(output, "wb") as f:
            f.write(base64.b64decode(data["output_b64"]))
        log.info(
            "wrote %s (%d bytes, %s)",
            output,
            data.get("bytes", 0),
            data.get("media_type", "?"),
        )
    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc


if __name__ == "__main__":
    asyncio.run(main())
