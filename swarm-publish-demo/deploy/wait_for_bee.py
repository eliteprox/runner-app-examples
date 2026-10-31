"""Do not advertise a paid publisher until its Bee upload node is ready."""

import os
import sys
import time
import urllib.error
import urllib.request

url = os.environ["BEE_URL"].rstrip("/") + "/readiness"
while True:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            if response.status == 200:
                break
    except (urllib.error.URLError, TimeoutError):
        pass
    print("Waiting for Bee readiness (funding / postage synchronization).", flush=True)
    time.sleep(10)
os.execvp("python", ["python", "/app/runner.py", *sys.argv[1:]])
