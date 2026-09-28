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
    def test_seed_and_fallback_crons(self) -> None:
        lines = active_lines()
        crons = [match.group(1) for line in lines if (match := re.fullmatch(r'\s*-\s*cron:\s*"([^"]*)"\s*', line))]
        self.assertEqual(["52 5,6 * * *", "5,15,25,35,45,55 6-20 * * *"], crons)
        self.assertIn("  schedule:", lines)
        self.assertIn("  workflow_dispatch:", lines)

    def test_next_tick_dispatches_same_workflow_on_same_ref_with_ephemeral_token_only(self) -> None:
        text = "\n".join(active_lines())
        dispatches = [line for line in active_lines() if "gh workflow run" in line]
        self.assertEqual(1, len(dispatches))
        self.assertIn('gh workflow run ai-crypto-trader.yml --repo "$GITHUB_REPOSITORY" --ref "$GITHUB_REF_NAME"', dispatches[0])
        self.assertIn("GH_TOKEN: ${{ github.token }}", text)
        self.assertIn("if: always() && needs.monitor.outputs.chain == 'true'", text)

    def test_job_permissions_are_minimal(self) -> None:
        text = "\n".join(active_lines())
        monitor, next_tick = text.split("\n  next-tick:\n")
        self.assertIn("    permissions:\n      contents: read\n", monitor)
        self.assertNotIn(": write", monitor)
        self.assertIn("    permissions:\n      actions: write\n", next_tick)
        self.assertNotIn("entry_monitor.py", next_tick)
        self.assertNotIn("contents:", next_tick)

    def test_scan_step_name_matches_gap_audit(self) -> None:
        scan_steps = [line for line in active_lines() if line.strip().startswith("- name: Dry-run scan")]
        self.assertEqual(1, len(scan_steps))
        self.assertIn(f"- name: {audit.SCAN_STEP_PREFIX}", scan_steps[0])
        self.assertEqual(WORKFLOW.name, audit.WORKFLOW_FILE)

    def test_brussels_window_gates_tests_and_scan(self) -> None:
        text = "\n".join(active_lines())
        self.assertIn("python scheduler/cadence.py", text)
        self.assertEqual(2, text.count("if: steps.cadence.outputs.active == 'true'"))
        self.assertIn("- name: Run tests\n        if: steps.cadence.outputs.active == 'true'", text)
        self.assertIn("- name: Dry-run scan (OKX public candles, no Telegram, writes Step Summary)\n        if: steps.cadence.outputs.active == 'true'", text)

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
        self.assertIn("    timeout-minutes: 15", lines)
        self.assertIn("    timeout-minutes: 2", lines)


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
