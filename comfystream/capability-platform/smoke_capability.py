"""Run a catalog capability offline through the embedded workflow executor."""

import argparse
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

from comfystream import CapabilityClient


async def main(args):
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "FAL_KEY"):
        assert not os.environ.get(name), (
            "Acceptance requires provider credentials to be absent"
        )
    inputs = json.loads(Path(args.input).read_text())
    started = time.monotonic()
    async with CapabilityClient(
        workspace="/workspace/ComfyUI",
        input_dir="/state/inputs",
        output_dir="/state/outputs",
        capacity_file="/state/gpu.lock",
        gpu=os.environ.get("CUDA_VISIBLE_DEVICES", "0"),
    ) as client:
        result = await client.execute(args.capability, inputs, timeout=args.timeout)
        if args.expect_text:
            assert args.expect_text.casefold() in result.get("text", "").casefold(), (
                "Unexpected transcription"
            )
        elapsed = time.monotonic() - started
        artifacts = []
        for entry in result["artifacts"]:
            path = Path(entry["path"])
            assert (
                path.is_relative_to(Path("/state/outputs")) and path.stat().st_size > 0
            )
            with path.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            details = {
                "media_type": entry["media_type"],
                "bytes": path.stat().st_size,
                "sha256": digest,
            }
            if entry["media_type"] == "image/png":
                from PIL import Image

                with Image.open(path) as image:
                    image.verify()
                with Image.open(path) as image:
                    details["dimensions"] = list(image.size)
            if entry["media_type"] == "audio/wav":
                import numpy as np
                import soundfile

                audio, rate = soundfile.read(path)
                assert (
                    audio.size and np.isfinite(audio).all() and np.abs(audio).max() > 0
                )
                details["duration_seconds"] = len(audio) / rate
                details["sample_rate"] = rate
            artifacts.append(details)
        report = {
            "capability": args.capability,
            "model": client.catalog[args.capability].document["model"],
            "descriptor_sha256": client.catalog[args.capability].digest,
            "execution_seconds": elapsed,
            "artifacts": artifacts,
            "status": "passed",
        }
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("capability")
    parser.add_argument("--input", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--expect-text")
    asyncio.run(main(parser.parse_args()))
