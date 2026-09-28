#!/usr/bin/env python3
"""Plan one tick of the 10-minute dry-scan chain (08:00-22:00 Europe/Brussels) without sleeping on the runner.

The delay between ticks is an Environment wait timer on the next run's `wait` job; this module only decides
whether to scan now and which wait environment the successor must use.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


BRUSSELS = ZoneInfo("Europe/Brussels")
WINDOW_START = time(8, 0)
WINDOW_END = time(22, 0)
GRID = timedelta(minutes=10)
# Typical time from this planner to the successor's scan, excluding the wait timer (dispatch, runner pickup, setup).
OVERHEAD = timedelta(seconds=45)
LANDING_MARGIN = timedelta(seconds=30)
LANDING_TOLERANCE = timedelta(minutes=11)
EARLY_TOLERANCE = timedelta(seconds=30)
DAY_WAITS = {9: "scan-wait-9m", 10: "scan-wait-10m"}
NIGHT_WAITS = {550: "scan-overnight-550m", 610: "scan-overnight-610m", 670: "scan-overnight-670m"}
WAIT_ENVIRONMENTS = {**DAY_WAITS, **NIGHT_WAITS}


def in_window(moment: datetime) -> bool:
    return WINDOW_START <= moment.astimezone(BRUSSELS).time() < WINDOW_END


def next_window_start(moment: datetime) -> datetime:
    local_day = moment.astimezone(BRUSSELS).date()
    today = datetime.combine(local_day, WINDOW_START, BRUSSELS).astimezone(timezone.utc)
    if moment < today:
        return today
    return datetime.combine(local_day + timedelta(days=1), WINDOW_START, BRUSSELS).astimezone(timezone.utc)


def arrival(now: datetime, minutes: int) -> datetime:
    return now + timedelta(minutes=minutes) + OVERHEAD


def grid_distance(moment: datetime) -> float:
    offset = moment.timestamp() % GRID.total_seconds()
    return min(offset, GRID.total_seconds() - offset)


def nearest_slot(moment: datetime) -> datetime:
    step = GRID.total_seconds()
    return datetime.fromtimestamp(round(moment.timestamp() / step) * step, tz=timezone.utc)


def choose_wait(now: datetime) -> int:
    if in_window(now):
        aligned = min(DAY_WAITS, key=lambda minutes: (grid_distance(arrival(now, minutes)), -minutes))
        if in_window(arrival(now, aligned)) and in_window(nearest_slot(arrival(now, aligned))):
            return aligned
    target = next_window_start(now) + LANDING_MARGIN
    landing = [m for m in sorted(WAIT_ENVIRONMENTS) if target <= arrival(now, m) <= target + LANDING_TOLERANCE]
    if landing:
        return landing[0]
    return max(m for m in WAIT_ENVIRONMENTS if arrival(now, m) < target)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def plan(now: datetime, wait_env: str, not_before: str, on_default_branch: bool, ticks: str) -> dict:
    problem = ""
    if wait_env and wait_env not in WAIT_ENVIRONMENTS.values():
        problem = f"Environment de asteptare necunoscut: {wait_env}"
    elif not_before and now < datetime.fromisoformat(not_before.replace("Z", "+00:00")) - EARLY_TOLERANCE:
        problem = f"Wait timer-ul din {wait_env or '?'} nu a fost aplicat: rularea a pornit inainte de {not_before}"
    remaining = int(ticks) if ticks.strip() else 0
    minutes = choose_wait(now)
    return {
        "problem": problem,
        "active": not problem and in_window(now),
        "chain": not problem and (on_default_branch or remaining > 1),
        "next_wait_env": WAIT_ENVIRONMENTS[minutes],
        "next_not_before": iso(now + timedelta(minutes=minutes)),
        "next_ticks": "" if on_default_branch else str(max(remaining - 1, 0)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan the current tick and its successor")
    parser.add_argument("--wait-env", default="")
    parser.add_argument("--not-before", default="")
    parser.add_argument("--ticks", default="")
    parser.add_argument("--default-branch", required=True)
    parser.add_argument("--ref-name", required=True)
    args = parser.parse_args()
    decision = plan(datetime.now(timezone.utc), args.wait_env, args.not_before, args.ref_name == args.default_branch, args.ticks)
    lines = [f"{key}={str(value).lower() if isinstance(value, bool) else value}" for key, value in decision.items()]
    print("\n".join(lines))
    output_path = os.getenv("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    if decision["problem"]:
        print(f"ERROR: {decision['problem']}. Lantul se opreste.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
