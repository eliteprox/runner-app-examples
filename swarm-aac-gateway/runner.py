#!/usr/bin/env python3
"""Host the Swarm AAC gateway behind any Livepeer Live Runner orchestrator."""

import logging
import math
import os
from pathlib import Path

from aiohttp import web
from livepeer_gateway.live_runner import register_runner
from livepeer_swarm.operator import OperatorFunding
from service import APP_ID, Gateway


def main():
    logging.basicConfig(level=logging.INFO)
    price = float(os.environ["PRICE_USD_PER_HOUR"])
    if not math.isfinite(price) or price < 0:
        raise ValueError("PRICE_USD_PER_HOUR must be finite and nonnegative")
    bee = os.environ["BEE_READ_URL"]
    minimum = os.environ.get("BEE_MIN_AVAILABLE_BZZ")
    funding = OperatorFunding(bee, minimum) if minimum else None
    gateway = Gateway(bee, funding)
    app = web.Application(client_max_size=4096)
    gateway.attach(app)

    async def start(app):
        if funding:
            await funding.require_funding()
        app["registration"] = await register_runner(
            os.environ["ORCHESTRATOR_URL"],
            secret=Path(os.environ["ORCH_SECRET_FILE"]).read_text().strip(),
            runner_url=os.environ["RUNNER_URL"],
            app=APP_ID,
            mode="persistent",
            capacity=1,
            price=price,
            auto_detect_gpu=False,
            on_session_release=gateway.released,
        )

    async def stop(app):
        if "registration" in app:
            await app["registration"].close()

    app.on_startup.append(start)
    app.on_cleanup.append(stop)
    web.run_app(
        app,
        host=os.environ.get("RUNNER_HOST", "127.0.0.1"),
        port=int(os.environ.get("RUNNER_PORT", "8993")),
    )


if __name__ == "__main__":
    main()
