"""Do not advertise a paid publisher until its Bee upload node is ready."""

import asyncio
import os

from livepeer_swarm.operator import OperatorFunding
import sys
import time
import urllib.error
import urllib.request

url = os.environ["BEE_URL"].rstrip("/") + "/readiness"
while True:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            if response.status == 200:
                minimum = os.environ.get("BEE_MIN_AVAILABLE_BZZ")
                if minimum:
                    asyncio.run(
                        OperatorFunding(
                            os.environ["BEE_URL"], minimum
                        ).require_funding()
                    )
                break
    except Exception:
        pass
    print("Waiting for Bee readiness (funding / postage synchronization).", flush=True)
    time.sleep(10)
os.execvp("python", ["python", "/app/runner.py", *sys.argv[1:]])
