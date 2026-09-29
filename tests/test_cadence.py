from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scheduler import cadence  # noqa: E402


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def local(moment: datetime) -> str:
    return moment.astimezone(cadence.BRUSSELS).strftime("%Y-%m-%d %H:%M:%S")


def simulate(start: datetime, end: datetime, overhead: timedelta) -> tuple[list[datetime], list[int]]:
    """Run the chain as GitHub would: each tick starts `wait + overhead` after the previous planner call."""
    scans: list[datetime] = []
    waits: list[int] = []
    now = start
    while now < end:
        decision = cadence.plan(now, "", "", True, "")
        if decision["active"]:
            scans.append(now)
        minutes = next(m for m, name in cadence.WAIT_ENVIRONMENTS.items() if name == decision["next_wait_env"])
        waits.append(minutes)
        now = now + timedelta(minutes=minutes) + overhead
    return scans, waits


class ChainSimulationTests(unittest.TestCase):
    def assert_day(self, scans: list[datetime], day: str, utc_offset_hours: int) -> None:
        window_start = utc(f"{day}T08:00:00") - timedelta(hours=utc_offset_hours)
        window_end = window_start + timedelta(hours=14)
        todays = [scan for scan in scans if window_start - timedelta(hours=2) <= scan < window_end + timedelta(hours=2)]
        self.assertTrue(todays, day)
        self.assertTrue(all(window_start <= scan < window_end for scan in todays), [local(s) for s in todays])
        self.assertLessEqual(todays[0] - window_start, timedelta(minutes=11), local(todays[0]))
        self.assertGreaterEqual(todays[-1], window_end - timedelta(minutes=12), local(todays[-1]))
        gaps = [later - earlier for earlier, later in zip(todays, todays[1:])]
        self.assertLessEqual(max(gaps), timedelta(minutes=15))

    def run_nights(self, start: str, end: str, days: list[tuple[str, int]], overhead_seconds: int) -> list[int]:
        scans, waits = simulate(utc(start), utc(end), timedelta(seconds=overhead_seconds))
        for day, offset in days:
            self.assert_day(scans, day, offset)
        return waits

    def test_normal_cest_days_scan_every_ten_minutes_and_sleep_once_per_night(self) -> None:
        for overhead in (20, 30, 45, 75):
            waits = self.run_nights(
                "2026-09-28T06:00:40", "2026-09-30T20:05:00",
                [("2026-09-28", 2), ("2026-09-29", 2), ("2026-09-30", 2)], overhead,
            )
            self.assertEqual([610, 610, 610], [w for w in waits if w >= 550], overhead)

    def test_normal_day_has_84_scans_aligned_to_ten_minutes(self) -> None:
        scans, _ = simulate(utc("2026-09-28T06:00:40"), utc("2026-09-28T20:00:00"), timedelta(seconds=45))
        day = [s for s in scans if s.date().isoformat() == "2026-09-28"]
        self.assertEqual(84, len(day))
        self.assertTrue(all(cadence.grid_distance(s) <= 60 for s in day))

    def test_autumn_dst_night_uses_670_minutes(self) -> None:
        waits = self.run_nights(
            "2026-10-24T06:00:40", "2026-10-25T21:05:00",
            [("2026-10-24", 2), ("2026-10-25", 1)], 45,
        )
        self.assertEqual([670, 610], [w for w in waits if w >= 550])

    def test_spring_dst_night_uses_550_minutes(self) -> None:
        waits = self.run_nights(
            "2027-03-27T07:00:40", "2027-03-28T20:05:00",
            [("2027-03-27", 1), ("2027-03-28", 2)], 45,
        )
        self.assertEqual([550, 610], [w for w in waits if w >= 550])

    def test_normal_cet_night_uses_610_minutes(self) -> None:
        waits = self.run_nights(
            "2026-11-02T07:00:40", "2026-11-03T21:05:00",
            [("2026-11-02", 1), ("2026-11-03", 1)], 45,
        )
        self.assertEqual([610, 610], [w for w in waits if w >= 550])


class TickDecisionTests(unittest.TestCase):
    def test_aligned_day_tick_scans_and_picks_day_wait(self) -> None:
        decision = cadence.plan(utc("2026-09-28T08:00:45"), "scan-wait-9m", "2026-09-28T07:51:00Z", True, "")
        self.assertEqual("", decision["problem"])
        self.assertTrue(decision["active"])
        self.assertTrue(decision["chain"])
        self.assertEqual("scan-wait-9m", decision["next_wait_env"])
        self.assertEqual("2026-09-28T08:09:45Z", decision["next_not_before"])

    def test_last_evening_scan_switches_to_overnight_wait(self) -> None:
        decision = cadence.plan(utc("2026-09-28T19:50:45"), "scan-wait-9m", "", True, "")
        self.assertTrue(decision["active"])
        self.assertEqual("scan-overnight-610m", decision["next_wait_env"])

    def test_no_scan_between_2200_and_0759(self) -> None:
        for moment in ("2026-09-28T20:00:30", "2026-09-28T23:59:00", "2026-09-29T03:00:00", "2026-09-29T05:59:59"):
            decision = cadence.plan(utc(moment), "", "", True, "")
            self.assertFalse(decision["active"], moment)
            self.assertTrue(decision["chain"], moment)

    def test_morning_seed_lands_just_after_0800_without_scanning(self) -> None:
        decision = cadence.plan(utc("2026-09-29T05:52:10"), "", "", True, "")
        self.assertFalse(decision["active"])
        self.assertEqual("scan-wait-9m", decision["next_wait_env"])

    def test_early_start_means_wait_timer_missing_and_stops_chain(self) -> None:
        decision = cadence.plan(utc("2026-09-28T10:00:40"), "scan-wait-10m", "2026-09-28T10:10:40Z", True, "")
        self.assertIn("nu a fost aplicat", decision["problem"])
        self.assertFalse(decision["active"])
        self.assertFalse(decision["chain"])

    def test_unknown_wait_environment_stops_chain(self) -> None:
        decision = cadence.plan(utc("2026-09-28T10:00:40"), "production", "", True, "")
        self.assertIn("necunoscut", decision["problem"])
        self.assertFalse(decision["chain"])

    def test_start_within_tolerance_of_not_before_is_accepted(self) -> None:
        decision = cadence.plan(utc("2026-09-28T10:10:20"), "scan-wait-10m", "2026-09-28T10:10:40Z", True, "")
        self.assertEqual("", decision["problem"])

    def test_non_default_branch_chains_only_for_bounded_ticks(self) -> None:
        now = utc("2026-09-28T10:05:00")
        self.assertFalse(cadence.plan(now, "", "", False, "")["chain"])
        self.assertFalse(cadence.plan(now, "", "", False, "1")["chain"])
        bounded = cadence.plan(now, "", "", False, "3")
        self.assertTrue(bounded["chain"])
        self.assertEqual("2", bounded["next_ticks"])

    def test_planner_never_sleeps(self) -> None:
        source = (PROJECT_ROOT / "scheduler" / "cadence.py").read_text(encoding="utf-8")
        self.assertNotIn("sleep(", source)
        self.assertNotIn("import time\n", source)


if __name__ == "__main__":
    unittest.main()
