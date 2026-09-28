from __future__ import annotations

import re
import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ai-crypto-trader.yml"
FORBIDDEN = (
    "TELEGRAM_TOKEN",
    "TELEGRAM_CHAT_ID",
    "secrets.",
    "api.telegram.org",
    "--send",
    "--env-file",
    "verify-telegram",
    "test-telegram",
    "actions/cache",
    "alert_sent",
)


def active_lines() -> list[str]:
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    return [line for line in lines if line.strip() and not line.lstrip().startswith("#")]


class WorkflowDryRunOnlyTests(unittest.TestCase):
    def test_schedule_runs_every_ten_minutes_in_utc_union_window(self) -> None:
        lines = active_lines()
        crons = [match.group(1) for line in lines if (match := re.fullmatch(r'\s*-\s*cron:\s*"([^"]*)"\s*', line))]
        self.assertEqual(["*/10 6-20 * * *"], crons)
        self.assertIn("  schedule:", lines)
        self.assertIn("  workflow_dispatch:", lines)

    def test_brussels_window_blocks_scans_from_22_to_08(self) -> None:
        text = "\n".join(active_lines())
        self.assertIn("TZ=Europe/Brussels date +%H", text)
        self.assertIn('" -ge 8 ]', text)
        self.assertIn('" -lt 22 ]', text)
        self.assertEqual(4, text.count("if: steps.window.outputs.active == 'true'"))

    def test_every_scanner_call_is_dry_run(self) -> None:
        calls = [line for line in active_lines() if "entry_monitor.py" in line]
        self.assertEqual(1, len(calls))
        self.assertIn(" scan ", calls[0])
        self.assertIn("--dry-run", calls[0])

    def test_no_step_is_limited_to_one_trigger(self) -> None:
        self.assertFalse([line for line in active_lines() if "github.event_name" in line])

    def test_no_telegram_secrets_or_dedup_persistence(self) -> None:
        text = "\n".join(active_lines())
        for fragment in FORBIDDEN:
            self.assertNotIn(fragment, text)

    def test_read_only_permissions_concurrency_and_timeout(self) -> None:
        lines = active_lines()
        start = lines.index("permissions:")
        permissions = []
        for line in lines[start + 1:]:
            if not line.startswith(" "):
                break
            permissions.append(line.strip())
        self.assertEqual(["contents: read"], permissions)
        self.assertIn("concurrency:", lines)
        self.assertIn("  group: ai-crypto-trader-entry-monitor", lines)
        self.assertIn("  cancel-in-progress: false", lines)
        self.assertIn("    timeout-minutes: 5", lines)


if __name__ == "__main__":
    unittest.main()
