#!/usr/bin/env python3
"""Plan one tick of the self-chaining 10-minute scan cadence (08:00-22:00 Europe/Brussels)."""

from __future__ import annotations

import argparse
import os
import time as clock
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


BRUSSELS = ZoneInfo("Europe/Brussels")
WINDOW_START = time(8, 0)
WINDOW_END = time(22, 0)
STEP = timedelta(minutes=10)
MAX_WAIT = timedelta(minutes=11)


def in_window(moment: datetime) -> bool:
    return WINDOW_START <= moment.astimezone(BRUSSELS).time() < WINDOW_END


def next_boundary(moment: datetime) -> datetime:
    floored = moment.replace(minute=moment.minute - moment.minute % 10, second=0, microsecond=0)
    return floored + STEP


def plan(now: datetime, slot: str, on_default_branch: bool, ticks: str) -> dict:
    target = datetime.fromisoformat(slot.replace("Z", "+00:00")) if slot else now
    wait = min(max(target - now, timedelta(0)), MAX_WAIT)
    scan_at = now + wait
    upcoming = next_boundary(scan_at)
    remaining = int(ticks) if ticks.strip() else 0
    chain_allowed = on_default_branch or remaining > 1
    return {
        "wait_seconds": int(wait.total_seconds()),
        "active": in_window(scan_at),
        "chain": chain_allowed and in_window(upcoming),
        "next_slot": upcoming.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "next_ticks": "" if on_default_branch else str(max(remaining - 1, 0)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Wait for this tick's slot and plan the next tick")
    parser.add_argument("--slot", default="")
    parser.add_argument("--ticks", default="")
    parser.add_argument("--default-branch", required=True)
    parser.add_argument("--ref-name", required=True)
    args = parser.parse_args()
    decision = plan(datetime.now(timezone.utc), args.slot, args.ref_name == args.default_branch, args.ticks)
    if decision["wait_seconds"]:
        print(f"Waiting {decision['wait_seconds']}s for slot {args.slot}")
        clock.sleep(decision["wait_seconds"])
    decision["active"] = in_window(datetime.now(timezone.utc))
    for key, value in decision.items():
        print(f"{key}={str(value).lower() if isinstance(value, bool) else value}")
    output_path = os.getenv("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as handle:
            for key, value in decision.items():
                handle.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
