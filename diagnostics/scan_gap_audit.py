#!/usr/bin/env python3
"""Audit real gaps between successful dry-run scans inside the Brussels scan window."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


BRUSSELS = ZoneInfo("Europe/Brussels")
WINDOW_START = time(8, 0)
WINDOW_END = time(22, 0)
DEFAULT_MAX_GAP_MINUTES = 15
WORKFLOW_FILE = "ai-crypto-trader.yml"
SCAN_STEP_PREFIXES = ("Dry-run scan", "Live scan")
API_BASE = "https://api.github.com"
# A tick's run is created before its wait timer; the longest (overnight) wait is 670 min, so the first
# morning scan belongs to a run created the previous evening.
RUN_LOOKBACK = timedelta(hours=12)
DEFAULT_REPOSITORY = "dorinbnb-star/tg-alert-bot"


class AuditError(RuntimeError):
    pass


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def local_iso(moment: datetime) -> str:
    return moment.astimezone(BRUSSELS).replace(microsecond=0).isoformat()


def local_bounds(day: date) -> tuple[datetime, datetime, datetime, datetime]:
    """Return UTC bounds of the local day and of the 08:00-22:00 window on that day."""
    def at(value: date, clock: time) -> datetime:
        return datetime.combine(value, clock, BRUSSELS).astimezone(timezone.utc)

    return at(day, time(0)), at(day + timedelta(days=1), time(0)), at(day, WINDOW_START), at(day, WINDOW_END)


def scan_completed_at(jobs: list[dict]) -> datetime | None:
    for job in jobs:
        for step in job.get("steps") or []:
            if (
                str(step.get("name", "")).startswith(SCAN_STEP_PREFIXES)
                and step.get("conclusion") == "success"
                and step.get("completed_at")
            ):
                return parse_timestamp(step["completed_at"])
    return None


def evaluate(scans: list[datetime], day: date, now: datetime, max_gap_minutes: int = DEFAULT_MAX_GAP_MINUTES) -> dict:
    day_start, day_end, window_start, window_end = local_bounds(day)
    limit = timedelta(minutes=max_gap_minutes)
    on_day = sorted(moment for moment in scans if day_start <= moment < day_end and moment <= now)
    inside = [moment for moment in on_day if window_start <= moment < window_end]
    outside = [moment for moment in on_day if not window_start <= moment < window_end]
    result: dict = {
        "date": day.isoformat(),
        "window": f"{local_iso(window_start)} .. {local_iso(window_end)}",
        "max_allowed_gap_minutes": max_gap_minutes,
        "successful_scans_in_window": len(inside),
        "scans_outside_window": [local_iso(moment) for moment in outside],
        "gaps_over_limit": [],
        "max_gap_minutes": None,
        "evaluated_until": None,
    }
    if now < window_start:
        result["verdict"] = "FAIL" if outside else "NOT_STARTED"
        return result
    evaluated_until = min(window_end, now)
    points = [window_start, *inside, evaluated_until]
    widest = timedelta(0)
    for earlier, later in zip(points, points[1:]):
        gap = later - earlier
        widest = max(widest, gap)
        if gap > limit:
            result["gaps_over_limit"].append({
                "from": local_iso(earlier),
                "to": local_iso(later),
                "minutes": round(gap.total_seconds() / 60, 1),
            })
    result["max_gap_minutes"] = round(widest.total_seconds() / 60, 1)
    result["evaluated_until"] = local_iso(evaluated_until)
    result["verdict"] = "FAIL" if result["gaps_over_limit"] or outside else "PASS"
    return result


def api_get(path: str, params: dict, token: str | None) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "tg-alert-bot-scan-gap-audit",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{API_BASE}{path}?{urllib.parse.urlencode(params)}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AuditError(f"GitHub API HTTP {exc.code} la {path}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise AuditError(f"GitHub API eroare retea {type(exc).__name__} la {path}") from None
    if not isinstance(payload, dict):
        raise AuditError(f"GitHub API raspuns invalid la {path}")
    return payload


def fetch_runs(repository: str, day: date, branch: str, token: str | None) -> list[dict]:
    day_start, day_end, _, _ = local_bounds(day)
    created = f"{day_start - RUN_LOOKBACK:%Y-%m-%dT%H:%M:%SZ}..{day_end:%Y-%m-%dT%H:%M:%SZ}"
    runs: list[dict] = []
    page = 1
    while True:
        payload = api_get(
            f"/repos/{repository}/actions/workflows/{WORKFLOW_FILE}/runs",
            {"branch": branch, "created": created, "per_page": 100, "page": page},
            token,
        )
        batch = payload.get("workflow_runs") or []
        runs.extend(batch)
        if len(batch) < 100:
            return runs
        page += 1


def collect(repository: str, day: date, branch: str, token: str | None) -> tuple[list[datetime], list[dict]]:
    day_start, day_end, _, _ = local_bounds(day)
    scans: list[datetime] = []
    rows: list[dict] = []
    for run in fetch_runs(repository, day, branch, token):
        scanned_at = None
        if run.get("status") == "completed":
            jobs = api_get(f"/repos/{repository}/actions/runs/{run['id']}/jobs", {"per_page": 100}, token)
            scanned_at = scan_completed_at(jobs.get("jobs") or [])
        if scanned_at is not None:
            scans.append(scanned_at)
        created_at = parse_timestamp(run["created_at"])
        belongs_to_day = day_start <= created_at < day_end or (scanned_at is not None and day_start <= scanned_at < day_end)
        if not belongs_to_day:
            continue
        rows.append({
            "run_id": run.get("id"),
            "event": run.get("event"),
            "created_at": local_iso(created_at),
            "conclusion": run.get("conclusion") or run.get("status"),
            "scan_completed_at": local_iso(scanned_at) if scanned_at else None,
        })
    rows.sort(key=lambda row: row["created_at"])
    return scans, rows


def write_summary(report: dict) -> None:
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = [
        f"## Scan gap audit {report['date']} (Europe/Brussels)",
        "",
        f"Verdict: **{report['verdict']}**",
        "",
        f"- Fereastra: {report['window']}",
        f"- Evaluat pana la: {report['evaluated_until']}",
        f"- Scanari reusite in fereastra: {report['successful_scans_in_window']}",
        f"- Pauza maxima: {report['max_gap_minutes']} min (limita {report['max_allowed_gap_minutes']} min)",
        f"- Scanari in afara ferestrei: {len(report['scans_outside_window'])}",
        "",
    ]
    if report["gaps_over_limit"]:
        lines += ["| De la | Pana la | Minute |", "|---|---|---:|"]
        lines += [f"| {gap['from']} | {gap['to']} | {gap['minutes']} |" for gap in report["gaps_over_limit"]]
        lines.append("")
    with Path(summary_path).open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit gaps between successful dry-run scans")
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY") or DEFAULT_REPOSITORY)
    parser.add_argument("--date", help="Local Europe/Brussels date YYYY-MM-DD (default: today)")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--max-gap-minutes", type=int, default=DEFAULT_MAX_GAP_MINUTES)
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    day = date.fromisoformat(args.date) if args.date else now.astimezone(BRUSSELS).date()
    try:
        scans, runs = collect(args.repo, day, args.branch, os.getenv("GITHUB_TOKEN"))
    except AuditError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    report = evaluate(scans, day, now, args.max_gap_minutes)
    report["runs"] = runs
    write_summary(report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["verdict"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
