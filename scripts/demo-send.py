"""Sign and send one crash payload to the local live-demo server (docs/DEMO.md).

    uv run python scripts/demo-send.py [--bug werkzeug-3110] [--port 8765] [--wait]

Plays the part of Sentry: it takes the benchmark's payload for the bug, adds the routing tag,
signs the body with DEMO_WEBHOOK_SECRET from .env, and posts it to 127.0.0.1. With --wait it
then follows the job until it ends. It sends to this machine only.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

from tracerepro.dataset.manifest import PROJECT_ROOT
from tracerepro.live.env_file import read_env_file, require
from tracerepro.live.server import (
    HOST,
    MIN_SECRET,
    PORT,
    ROUTING_TAG,
    SECRET_KEY,
    SIGNATURE_HEADER,
    signature,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bug", default="werkzeug-3110")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--wait", action="store_true", help="follow the job until it ends")
    args = parser.parse_args()
    secret = require(read_env_file({SECRET_KEY}), SECRET_KEY, min_length=MIN_SECRET)
    payload = json.loads((PROJECT_ROOT / "benchmark" / "payloads" / f"{args.bug}.json").read_text())
    payload.setdefault("tags", {})[ROUTING_TAG] = args.bug
    body = json.dumps(payload).encode()
    base = f"http://{HOST}:{args.port}"
    headers = {
        "content-type": "application/json",
        SIGNATURE_HEADER: signature(secret.encode(), body),
    }
    reply = httpx.post(f"{base}/webhook", content=body, headers=headers, timeout=30)
    print(reply.status_code, reply.text)
    if reply.status_code != 202 or not args.wait:
        return 0 if reply.status_code == 202 else 1
    status = reply.json()["status"]
    while True:
        time.sleep(5)
        job = httpx.get(f"{base}{status}", timeout=30).json()
        print(f"{job['seconds']:>7}s  {job['state']}  {job['message']}")
        if job["state"] in ("done", "failed"):
            print(json.dumps(job, indent=2))
            return 0 if job["state"] == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
