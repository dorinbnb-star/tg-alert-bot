from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from diagnostics import scan_gap_audit as audit  # noqa: E402

SUMMER_DAY = date(2026, 9, 28)
DST_END_DAY = date(2026, 10, 25)


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def every(start: datetime, minutes: int, count: int) -> list[datetime]:
    return [start + timedelta(minutes=minutes * index) for index in range(count)]


class GapEvaluationTests(unittest.TestCase):
    def test_ten_minute_cadence_for_whole_window_passes(self) -> None:
        scans = every(utc("2026-09-28T06:00:30"), 10, 84)
        report = audit.evaluate(scans, SUMMER_DAY, utc("2026-09-28T23:00:00"))
        self.assertEqual("PASS", report["verdict"])
        self.assertEqual(84, report["successful_scans_in_window"])
        self.assertLessEqual(report["max_gap_minutes"], 10.0)

    def test_gap_over_fifteen_minutes_fails_with_exact_interval(self) -> None:
        scans = [moment for moment in every(utc("2026-09-28T06:00:30"), 10, 84) if moment.strftime("%H:%M") not in ("10:10", "10:20")]
        report = audit.evaluate(scans, SUMMER_DAY, utc("2026-09-28T23:00:00"))
        self.assertEqual("FAIL", report["verdict"])
        self.assertEqual(
            [{"from": "2026-09-28T12:00:30+02:00", "to": "2026-09-28T12:30:30+02:00", "minutes": 30.0}],
            report["gaps_over_limit"],
        )

    def test_exactly_fifteen_minutes_is_allowed(self) -> None:
        scans = every(utc("2026-09-28T06:00:00"), 15, 56)
        report = audit.evaluate(scans, SUMMER_DAY, utc("2026-09-28T23:00:00"))
        self.assertEqual("PASS", report["verdict"])
        self.assertEqual(15.0, report["max_gap_minutes"])

    def test_day_without_scans_fails_for_whole_window(self) -> None:
        report = audit.evaluate([], SUMMER_DAY, utc("2026-09-28T23:00:00"))
        self.assertEqual("FAIL", report["verdict"])
        self.assertEqual(840.0, report["max_gap_minutes"])

    def test_window_start_counts_as_anchor(self) -> None:
        scans = every(utc("2026-09-28T06:20:00"), 10, 30)
        report = audit.evaluate(scans, SUMMER_DAY, utc("2026-09-28T11:00:00"))
        self.assertEqual("FAIL", report["verdict"])
        self.assertEqual("2026-09-28T08:00:00+02:00", report["gaps_over_limit"][0]["from"])

    def test_mid_window_audit_only_evaluates_until_now(self) -> None:
        scans = every(utc("2026-09-28T06:00:30"), 10, 37)
        self.assertEqual("PASS", audit.evaluate(scans, SUMMER_DAY, utc("2026-09-28T12:05:00"))["verdict"])
        ongoing = audit.evaluate(scans[:-3], SUMMER_DAY, utc("2026-09-28T12:05:00"))
        self.assertEqual("FAIL", ongoing["verdict"])
        self.assertEqual("2026-09-28T14:05:00+02:00", ongoing["gaps_over_limit"][-1]["to"])

    def test_scan_outside_window_fails(self) -> None:
        scans = every(utc("2026-09-28T06:00:30"), 10, 84) + [utc("2026-09-28T20:05:00")]
        report = audit.evaluate(scans, SUMMER_DAY, utc("2026-09-28T23:00:00"))
        self.assertEqual("FAIL", report["verdict"])
        self.assertEqual(["2026-09-28T22:05:00+02:00"], report["scans_outside_window"])

    def test_before_window_is_not_started(self) -> None:
        report = audit.evaluate([], SUMMER_DAY, utc("2026-09-28T05:30:00"))
        self.assertEqual("NOT_STARTED", report["verdict"])

    def test_window_follows_cet_after_dst_ends(self) -> None:
        scans = every(utc("2026-10-25T07:00:30"), 10, 84)
        report = audit.evaluate(scans, DST_END_DAY, utc("2026-10-25T23:00:00"))
        self.assertEqual("PASS", report["verdict"])
        self.assertEqual("2026-10-25T08:00:00+01:00 .. 2026-10-25T22:00:00+01:00", report["window"])
        summer_grid = audit.evaluate(every(utc("2026-10-25T06:00:30"), 10, 84), DST_END_DAY, utc("2026-10-25T23:00:00"))
        self.assertEqual("FAIL", summer_grid["verdict"])
        self.assertEqual(6, len(summer_grid["scans_outside_window"]))


class RunParsingTests(unittest.TestCase):
    def jobs(self, conclusion: str, name: str = "Dry-run scan (OKX public candles, no Telegram, writes Step Summary)") -> list[dict]:
        return [{"steps": [
            {"name": "Check Brussels scan window", "conclusion": "success", "completed_at": "2026-09-28T10:00:01Z"},
            {"name": name, "conclusion": conclusion, "completed_at": "2026-09-28T10:00:09Z"},
        ]}]

    def test_only_successful_scan_step_counts(self) -> None:
        self.assertEqual(utc("2026-09-28T10:00:09"), audit.scan_completed_at(self.jobs("success")))
        self.assertIsNone(audit.scan_completed_at(self.jobs("skipped")))
        self.assertIsNone(audit.scan_completed_at(self.jobs("failure")))
        self.assertIsNone(audit.scan_completed_at(self.jobs("success", name="Run tests")))

    def test_collect_queries_branch_and_local_day_and_reads_jobs(self) -> None:
        runs = {"workflow_runs": [
            {"id": 1, "event": "workflow_dispatch", "status": "completed", "conclusion": "success", "created_at": "2026-09-28T10:00:00Z"},
            {"id": 2, "event": "schedule", "status": "completed", "conclusion": "success", "created_at": "2026-09-28T20:05:00Z"},
            {"id": 3, "event": "schedule", "status": "in_progress", "conclusion": None, "created_at": "2026-09-28T10:05:00Z"},
        ]}
        calls: list[tuple[str, dict]] = []

        def fake_get(path: str, params: dict, token: str | None) -> dict:
            calls.append((path, params))
            if path.endswith("/runs"):
                return runs
            return {"jobs": self.jobs("success" if "/runs/1/" in path else "skipped")}

        with patch.object(audit, "api_get", side_effect=fake_get):
            scans, rows = audit.collect("owner/repo", SUMMER_DAY, "main", None)
        self.assertEqual([utc("2026-09-28T10:00:09")], scans)
        self.assertEqual(3, len(rows))
        runs_call = calls[0]
        self.assertEqual("/repos/owner/repo/actions/workflows/ai-crypto-trader.yml/runs", runs_call[0])
        self.assertEqual("main", runs_call[1]["branch"])
        self.assertEqual("2026-09-27T22:00:00Z..2026-09-28T22:00:00Z", runs_call[1]["created"])
        self.assertEqual(
            ["/repos/owner/repo/actions/runs/1/jobs", "/repos/owner/repo/actions/runs/2/jobs"],
            [path for path, _ in calls[1:]],
        )


if __name__ == "__main__":
    unittest.main()
