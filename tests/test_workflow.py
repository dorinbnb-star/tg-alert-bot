from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from diagnostics import scan_gap_audit as audit  # noqa: E402
from scheduler import cadence  # noqa: E402

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


def jobs(path: Path = WORKFLOW) -> dict[str, str]:
    body = "\n".join(active_lines(path)).split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([a-z-]+):$", body, flags=re.MULTILINE)
    return {parts[index]: parts[index + 1] + "\n" for index in range(1, len(parts), 2)}


def permission_lines(lines: list[str]) -> list[str]:
    start = lines.index("permissions:")
    permissions = []
    for line in lines[start + 1:]:
        if not line.startswith(" "):
            break
        permissions.append(line.strip())
    return permissions


class WorkflowDryRunOnlyTests(unittest.TestCase):
    def test_seed_and_rare_recovery_crons(self) -> None:
        lines = active_lines()
        crons = [match.group(1) for line in lines if (match := re.fullmatch(r'\s*-\s*cron:\s*"([^"]*)"\s*', line))]
        self.assertEqual(["52 5,6 * * *", "7 6-20 * * *"], crons)
        self.assertIn("  schedule:", lines)
        self.assertIn("  workflow_dispatch:", lines)

    def test_only_the_wait_job_uses_an_environment_and_it_does_nothing_else(self) -> None:
        wait = jobs()["wait"]
        self.assertIn("    if: inputs.wait_env != ''\n", wait)
        self.assertIn("    environment: ${{ inputs.wait_env }}\n", wait)
        self.assertIn("    permissions: {}\n", wait)
        self.assertIn("    timeout-minutes: 2\n", wait)
        self.assertEqual(1, wait.count("run:"))
        self.assertIn('run: echo "Environment wait timer elapsed"', wait)
        for name, body in jobs().items():
            if name != "wait":
                self.assertNotIn("environment:", body, name)

    def test_no_job_sleeps_on_the_runner(self) -> None:
        text = "\n".join(active_lines())
        self.assertNotIn("sleep", text)
        for name, body in jobs().items():
            minutes = int(re.search(r"timeout-minutes: (\d+)", body).group(1))
            self.assertLessEqual(minutes, 5, name)
        dispatch_source = (PROJECT_ROOT / "scheduler" / "dispatch.py").read_text(encoding="utf-8")
        self.assertEqual(1, dispatch_source.count("sleeper(RETRY_DELAYS"))

    def test_monitor_runs_after_wait_or_seed_and_is_read_only(self) -> None:
        monitor = jobs()["monitor"]
        self.assertIn("    needs: wait\n", monitor)
        self.assertIn("    if: ${{ !cancelled() && (needs.wait.result == 'success' || needs.wait.result == 'skipped') }}\n", monitor)
        self.assertIn("    permissions:\n      contents: read\n", monitor)
        self.assertNotIn(": write", monitor)

    def test_next_tick_dispatches_same_workflow_on_same_ref_with_ephemeral_token_only(self) -> None:
        next_tick = jobs()["next-tick"]
        self.assertIn("    if: ${{ !cancelled() && needs.monitor.outputs.chain == 'true' }}\n", next_tick)
        self.assertIn("    permissions:\n      actions: write\n      contents: read\n", next_tick)
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", next_tick)
        self.assertIn(
            'python3 scheduler/dispatch.py --repo "$GITHUB_REPOSITORY" --ref "$GITHUB_REF_NAME" --workflow ai-crypto-trader.yml',
            next_tick,
        )
        self.assertNotIn("entry_monitor.py", next_tick)
        self.assertNotIn("gh workflow run", "\n".join(active_lines()))

    def test_documented_environments_match_the_planner(self) -> None:
        docs = (PROJECT_ROOT / "AI_CRYPTO_GITHUB_ACTIONS.md").read_text(encoding="utf-8")
        for minutes, name in cadence.WAIT_ENVIRONMENTS.items():
            self.assertIn(f"| `{name}` | {minutes} |", docs)

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
        self.assertIn("  group: ai-crypto-trader-entry-monitor-${{ github.ref }}", lines)
        self.assertEqual(1, sum(1 for line in lines if line.strip().startswith("group:")))
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
