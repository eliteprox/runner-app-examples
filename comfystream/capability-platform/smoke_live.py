"""Exercise a real spawned workflow worker without registering paid supply."""

import asyncio
import json
from fractions import Fraction

import av
import numpy as np

from comfystream import Pipeline


async def main():
    pipeline = Pipeline(
        width=64,
        height=64,
        cwd="/workspace/ComfyUI",
        bootstrap_default_prompt=False,
        preview_method="none",
    )
    workflow = {
        "1": {"class_type": "LoadTensor", "inputs": {}},
        "2": {"class_type": "InvertImage", "inputs": {"images": ["1", 0]}},
        "3": {"class_type": "SaveTensor", "inputs": {"images": ["2", 0]}},
    }
    try:
        await pipeline.initialize()
        await pipeline.apply_prompts([workflow], skip_warmup=True)
        await pipeline.start_streaming()
        for value in (64, 96):
            frame = av.VideoFrame.from_ndarray(
                np.full((64, 64, 3), value, dtype=np.uint8), format="rgb24"
            )
            frame.pts, frame.time_base = value, Fraction(1, 30)
            await pipeline.put_video_frame(frame)
            output = await asyncio.wait_for(
                pipeline.get_processed_video_frame(), timeout=120
            )
            actual = output.to_ndarray(format="rgb24")
            assert abs(float(actual.mean()) - (255 - value)) <= 1
            assert output.pts == value
        print(
            json.dumps(
                {
                    "status": "passed",
                    "frames": 2,
                    "workflow": "invert",
                    "transport": "spawn/shared-memory",
                }
            )
        )
    finally:
        await pipeline.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
