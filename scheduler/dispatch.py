#!/usr/bin/env python3
"""Dispatch the next tick of the scan chain with bounded retries (ephemeral GITHUB_TOKEN only)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Callable


API_BASE = "https://api.github.com"
RETRY_DELAYS = (5, 15, 30)
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class DispatchError(RuntimeError):
    pass


def dispatch(
    repository: str,
    workflow: str,
    ref: str,
    inputs: dict[str, str],
    token: str,
    *,
    opener: Callable = urllib.request.urlopen,
    sleeper: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> int:
    request = urllib.request.Request(
        f"{API_BASE}/repos/{repository}/actions/workflows/{workflow}/dispatches",
        data=json.dumps({"ref": ref, "inputs": inputs}).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "tg-alert-bot-scan-chain",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    last_error = "unknown"
    for attempt in range(1, len(RETRY_DELAYS) + 2):
        try:
            with opener(request, timeout=20) as response:
                status = response.status
            if 200 <= status < 300:
                return attempt
            last_error = f"HTTP {status}"
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRYABLE_STATUS:
                raise DispatchError(f"Dispatch refuzat: HTTP {exc.code}") from None
            last_error = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = f"eroare retea {type(exc).__name__}"
        if attempt <= len(RETRY_DELAYS):
            log(f"Dispatch incercarea {attempt} esuata ({last_error}); reincerc in {RETRY_DELAYS[attempt - 1]}s")
            sleeper(RETRY_DELAYS[attempt - 1])
    raise DispatchError(f"Dispatch esuat dupa {len(RETRY_DELAYS) + 1} incercari: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Dispatch the next scan-chain tick")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--wait-env", required=True)
    parser.add_argument("--not-before", required=True)
    parser.add_argument("--ticks", default="")
    args = parser.parse_args()
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        print("ERROR: GITHUB_TOKEN lipseste", file=sys.stderr)
        return 1
    inputs = {"wait_env": args.wait_env, "not_before": args.not_before, "ticks": args.ticks}
    try:
        attempts = dispatch(args.repo, args.workflow, args.ref, inputs, token)
    except DispatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Next tick dispatched after {attempts} attempt(s): wait_env={args.wait_env} not_before={args.not_before}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
