from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scheduler import cadence  # noqa: E402


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


class CadencePlanTests(unittest.TestCase):
    def test_chained_tick_waits_for_slot_and_plans_next_ten_minutes(self) -> None:
        decision = cadence.plan(utc("2026-09-28T10:09:42"), "2026-09-28T10:10:00Z", True, "")
        self.assertEqual(18, decision["wait_seconds"])
        self.assertTrue(decision["active"])
        self.assertTrue(decision["chain"])
        self.assertEqual("2026-09-28T10:20:00Z", decision["next_slot"])
        self.assertEqual("", decision["next_ticks"])

    def test_seed_scans_now_and_aligns_to_next_boundary(self) -> None:
        decision = cadence.plan(utc("2026-09-28T10:05:31"), "", True, "")
        self.assertEqual(0, decision["wait_seconds"])
        self.assertTrue(decision["active"])
        self.assertEqual("2026-09-28T10:10:00Z", decision["next_slot"])

    def test_late_slot_scans_immediately(self) -> None:
        decision = cadence.plan(utc("2026-09-28T10:13:00"), "2026-09-28T10:10:00Z", True, "")
        self.assertEqual(0, decision["wait_seconds"])
        self.assertEqual("2026-09-28T10:20:00Z", decision["next_slot"])

    def test_wait_is_capped(self) -> None:
        decision = cadence.plan(utc("2026-09-28T10:00:00"), "2026-09-28T12:00:00Z", True, "")
        self.assertEqual(11 * 60, decision["wait_seconds"])

    def test_last_scan_is_2150_and_chain_stops_at_2200(self) -> None:
        decision = cadence.plan(utc("2026-09-28T19:49:50"), "2026-09-28T19:50:00Z", True, "")
        self.assertTrue(decision["active"])
        self.assertFalse(decision["chain"])

    def test_morning_seed_waits_for_0800_without_scanning(self) -> None:
        summer = cadence.plan(utc("2026-09-28T05:52:10"), "", True, "")
        self.assertFalse(summer["active"])
        self.assertTrue(summer["chain"])
        self.assertEqual("2026-09-28T06:00:00Z", summer["next_slot"])
        winter = cadence.plan(utc("2026-10-26T06:52:10"), "", True, "")
        self.assertFalse(winter["active"])
        self.assertEqual("2026-10-26T07:00:00Z", winter["next_slot"])
        self.assertFalse(cadence.plan(utc("2026-10-26T05:52:10"), "", True, "")["chain"])

    def test_night_neither_scans_nor_chains(self) -> None:
        decision = cadence.plan(utc("2026-09-28T23:10:00"), "", True, "")
        self.assertFalse(decision["active"])
        self.assertFalse(decision["chain"])

    def test_non_default_branch_chains_only_for_bounded_ticks(self) -> None:
        now = utc("2026-09-28T10:05:00")
        self.assertFalse(cadence.plan(now, "", False, "")["chain"])
        self.assertFalse(cadence.plan(now, "", False, "1")["chain"])
        bounded = cadence.plan(now, "", False, "3")
        self.assertTrue(bounded["chain"])
        self.assertEqual("2", bounded["next_ticks"])


if __name__ == "__main__":
    unittest.main()
