from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from diagnostics import scan_gap_audit as audit  # noqa: E402

WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ai-crypto-trader.yml"
AUDIT_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "scan-gap-audit.yml"
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


def active_lines(path: Path = WORKFLOW) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line for line in lines if line.strip() and not line.lstrip().startswith("#")]


def permission_lines(lines: list[str]) -> list[str]:
    start = lines.index("permissions:")
    permissions = []
    for line in lines[start + 1:]:
        if not line.startswith(" "):
            break
        permissions.append(line.strip())
    return permissions


class WorkflowDryRunOnlyTests(unittest.TestCase):
    def test_fallback_schedule_runs_every_ten_minutes_off_the_hour_in_utc_union_window(self) -> None:
        lines = active_lines()
        crons = [match.group(1) for line in lines if (match := re.fullmatch(r'\s*-\s*cron:\s*"([^"]*)"\s*', line))]
        self.assertEqual(["5,15,25,35,45,55 6-20 * * *"], crons)
        self.assertIn("  schedule:", lines)
        self.assertIn("  workflow_dispatch:", lines)

    def test_scan_step_name_matches_gap_audit(self) -> None:
        scan_steps = [line for line in active_lines() if line.strip().startswith("- name: Dry-run scan")]
        self.assertEqual(1, len(scan_steps))
        self.assertIn(f"- name: {audit.SCAN_STEP_PREFIX}", scan_steps[0])
        self.assertEqual(WORKFLOW.name, audit.WORKFLOW_FILE)

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
        self.assertEqual(["contents: read"], permission_lines(lines))
        self.assertIn("concurrency:", lines)
        self.assertIn("  group: ai-crypto-trader-entry-monitor", lines)
        self.assertIn("  cancel-in-progress: false", lines)
        self.assertIn("    timeout-minutes: 5", lines)


class GapAuditWorkflowTests(unittest.TestCase):
    def test_audit_is_read_only_and_uses_only_the_ephemeral_token(self) -> None:
        lines = active_lines(AUDIT_WORKFLOW)
        text = "\n".join(lines)
        self.assertEqual(["actions: read", "contents: read"], permission_lines(lines))
        for fragment in FORBIDDEN + ("entry_monitor.py",):
            self.assertNotIn(fragment, text)
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", text)
        self.assertIn("python diagnostics/scan_gap_audit.py", text)

    def test_audit_runs_daily_after_the_window_and_on_demand(self) -> None:
        lines = active_lines(AUDIT_WORKFLOW)
        crons = [match.group(1) for line in lines if (match := re.fullmatch(r'\s*-\s*cron:\s*"([^"]*)"\s*', line))]
        self.assertEqual(["17 21 * * *"], crons)
        self.assertIn("  workflow_dispatch:", lines)
        self.assertIn("    timeout-minutes: 5", lines)


if __name__ == "__main__":
    unittest.main()
