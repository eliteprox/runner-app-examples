"""Unpaid HTTP/model acceptance against a pinned installed runner image."""

import asyncio
import base64
import io
import json
import time
from pathlib import Path

import aiohttp
from aiohttp import web
from comfystream.server.catalog_runner import DEFAULT_CAPABILITIES, create_app
from openai import AsyncOpenAI
from PIL import Image

from comfystream import CapabilityClient


async def main():
    config = json.loads(Path("/state/deployment.json").read_text())
    client = CapabilityClient(
        workspace="/workspace/ComfyUI",
        input_dir="/state/inputs",
        output_dir="/state/outputs",
        capacity_file="/state/gpu.lock",
    )
    registered = []

    async def register(*args, **kwargs):
        registered.append(kwargs)

        class Registration:
            async def close(self):
                pass

        return Registration()

    app = create_app(
        client,
        config,
        orchestrator="https://test.invalid",
        secret="test",
        runner_url="http://127.0.0.1:8993",
        register_fn=register,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 18994)
    await site.start()
    report = []
    image = (
        "data:image/png;base64,"
        + base64.b64encode(Path("/fixtures/inputs/subject.png").read_bytes()).decode()
    )
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=900)
        ) as http:
            from comfystream.server.catalog_runner import PROFILES

            for name in DEFAULT_CAPABILITIES:
                spec = client.catalog[name]
                model = spec.document["model"]["source"]
                if name == "whisper":
                    body = aiohttp.FormData()
                    body.add_field("model", model)
                    body.add_field(
                        "file",
                        Path("/fixtures/inputs/reference.wav").read_bytes(),
                        filename="audio.wav",
                        content_type="audio/wav",
                    )
                    kwargs = {"data": body}
                else:
                    values = {
                        "flux-image": {
                            "prompt": "A red teapot on a wooden table",
                            "size": "256x256",
                        },
                        "birefnet": {"image": image},
                        "triposr": {"image": image},
                        "ace-music": {
                            "prompt": "Upbeat instrumental acoustic guitar",
                            "seconds": 5,
                        },
                    }[name]
                    kwargs = {"json": {"model": model, **values}}
                started = time.monotonic()
                path = "/catalog/" + name + PROFILES[spec.document["operation"]][0]
                if name in {"flux-image", "whisper"}:
                    async with AsyncOpenAI(
                        api_key="local-fixture",
                        base_url="http://127.0.0.1:18994/catalog/" + name + "/v1",
                    ) as sdk:
                        if name == "flux-image":
                            value = (
                                await sdk.images.generate(
                                    model=model,
                                    prompt=values["prompt"],
                                    size="256x256",
                                    response_format="b64_json",
                                )
                            ).model_dump()
                        else:
                            value = (
                                await sdk.audio.transcriptions.create(
                                    model=model,
                                    file=(
                                        "audio.wav",
                                        Path(
                                            "/fixtures/inputs/reference.wav"
                                        ).read_bytes(),
                                        "audio/wav",
                                    ),
                                )
                            ).model_dump()
                else:
                    async with http.post(
                        "http://127.0.0.1:18994" + path, **kwargs
                    ) as response:
                        value = await response.json()
                        assert response.status == 200, (name, response.status, value)
                if name == "whisper":
                    assert "quick brown fox" in value["text"].lower()
                    size = len(value["text"])
                else:
                    data = base64.b64decode(value["data"][0]["b64_json"], validate=True)
                    size = len(data)
                    assert size > 100
                    if name in {"flux-image", "birefnet"}:
                        Image.open(io.BytesIO(data)).verify()
                    if name == "ace-music":
                        import soundfile

                        audio, rate = soundfile.read(io.BytesIO(data))
                        assert len(audio) / rate >= 4
                    if name == "triposr":
                        assert b"\nv " in data and b"\nf " in data
                report.append(
                    {
                        "capability": name,
                        "status": "passed",
                        "http_seconds": time.monotonic() - started,
                        "output_bytes": size,
                    }
                )
                print(json.dumps(report[-1]), flush=True)
        assert len(registered) == 5
        Path("/state/catalog-http-report.json").write_text(json.dumps(report, indent=2))
        Path("/state/registration-candidates.json").write_text(
            json.dumps([json.loads(r["metadata"]) for r in registered], indent=2)
        )
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
