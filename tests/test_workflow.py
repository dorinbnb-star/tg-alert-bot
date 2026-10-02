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
NEVER = ("api.telegram.org", "--env-file", "verify-telegram", "test-telegram", "sleep")
LIVE_PATH = "github.ref == 'refs/heads/main' && (github.event_name == 'schedule' || github.triggering_actor == 'github-actions[bot]')"
LIVE_STEP = "Live scan (OKX public candles, Telegram only on NOW=ENTER, writes Step Summary)"
DRY_STEP = "Dry-run scan (OKX public candles, no Telegram, writes Step Summary)"


def active_lines(path: Path = WORKFLOW) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line for line in lines if line.strip() and not line.lstrip().startswith("#")]


def jobs(path: Path = WORKFLOW) -> dict[str, str]:
    body = "\n".join(active_lines(path)).split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([a-z-]+):$", body, flags=re.MULTILINE)
    return {parts[index]: parts[index + 1] + "\n" for index in range(1, len(parts), 2)}


def monitor_steps() -> dict[str, str]:
    parts = jobs()["monitor"].split("\n      - name: ")[1:]
    return {part.split("\n", 1)[0]: part for part in parts}


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

    def test_scan_step_names_match_gap_audit(self) -> None:
        scans = [name for name in monitor_steps() if name.startswith(audit.SCAN_STEP_PREFIXES)]
        self.assertEqual([DRY_STEP, LIVE_STEP], scans)
        self.assertEqual(WORKFLOW.name, audit.WORKFLOW_FILE)

    def test_brussels_window_gates_tests_and_both_scan_paths(self) -> None:
        steps = monitor_steps()
        self.assertIn("python scheduler/cadence.py", steps["Plan next tick and check Brussels scan window"])
        self.assertIn("if: steps.cadence.outputs.active == 'true'\n", steps["Run tests"])
        for name in (DRY_STEP, "Restore persistent dedup state", LIVE_STEP):
            self.assertIn("if: ${{ steps.cadence.outputs.active == 'true' && ", steps[name], name)

    def test_dry_run_and_live_paths_are_mutually_exclusive(self) -> None:
        steps = monitor_steps()
        self.assertIn(f"if: ${{{{ steps.cadence.outputs.active == 'true' && !({LIVE_PATH}) }}}}\n", steps[DRY_STEP])
        self.assertIn(f"if: ${{{{ steps.cadence.outputs.active == 'true' && {LIVE_PATH} }}}}\n", steps[LIVE_STEP])
        self.assertIn(f"if: ${{{{ steps.cadence.outputs.active == 'true' && {LIVE_PATH} }}}}\n", steps["Restore persistent dedup state"])

    def test_dry_run_path_never_sees_secrets_or_sends(self) -> None:
        dry = monitor_steps()[DRY_STEP]
        self.assertIn('entry_monitor.py scan --root "$GITHUB_WORKSPACE" --dry-run', dry)
        for fragment in ("secrets.", "TELEGRAM", "--send"):
            self.assertNotIn(fragment, dry)
        env_names = re.findall(r"^\s{10}([A-Z_]+):", dry, flags=re.MULTILINE)
        self.assertEqual(
            ["SCAN_DIAGNOSTICS_FILE", "SCAN_DIAGNOSTICS_CURRENT", "SCAN_DIAGNOSTICS_META", "SCAN_DIAGNOSTICS_SYMBOL"],
            env_names,
        )

    def test_only_the_live_step_reads_exactly_the_two_telegram_secrets(self) -> None:
        text = "\n".join(active_lines())
        live = monitor_steps()[LIVE_STEP]
        self.assertEqual(
            ["secrets.TELEGRAM_TOKEN", "secrets.TELEGRAM_CHAT_ID"],
            re.findall(r"secrets\.[A-Z_]+", text),
        )
        self.assertIn("TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}", live)
        self.assertIn("TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}", live)
        self.assertIn('entry_monitor.py scan --root "$GITHUB_WORKSPACE" --send', live)
        self.assertEqual(1, text.count("--send"))
        for name in ("wait", "next-tick"):
            self.assertNotIn("secrets.", jobs()[name], name)

    def test_every_scanner_call_is_a_scan_in_dry_run_or_live_mode(self) -> None:
        calls = [line.strip() for line in active_lines() if "entry_monitor.py" in line]
        self.assertEqual(
            [
                'run: python ai_crypto_monitor/entry_monitor.py scan --root "$GITHUB_WORKSPACE" --dry-run',
                'run: python ai_crypto_monitor/entry_monitor.py scan --root "$GITHUB_WORKSPACE" --send',
            ],
            calls,
        )

    def test_dedup_state_is_restored_and_saved_only_on_the_live_path(self) -> None:
        steps = monitor_steps()
        restore, save = steps["Restore persistent dedup state"], steps["Save dedup state after Telegram accepted an alert"]
        self.assertIn("uses: actions/cache/restore@v4", restore)
        self.assertIn("restore-keys: |\n            ai-crypto-dedup-${{ github.ref_name }}-", restore)
        self.assertIn("if: ${{ always() && steps.live.outputs.alert_sent == 'true' }}", save)
        self.assertIn("uses: actions/cache/save@v4", save)
        for body in (restore, save):
            self.assertIn("path: ai_crypto_monitor/state/dedup.json", body)
            self.assertIn("key: ai-crypto-dedup-${{ github.ref_name }}-${{ github.run_id }}", body)
        self.assertIn("id: live\n", steps[LIVE_STEP])

    def test_diagnostics_are_separate_persistent_and_uploaded_for_every_active_run(self) -> None:
        steps = monitor_steps()
        restore = steps["Restore cumulative scan diagnostics"]
        prepare = steps["Prepare scan diagnostics"]
        ensure = steps["Ensure every active run has a diagnostic row"]
        save = steps["Save cumulative scan diagnostics"]
        upload = steps["Upload this run diagnostic"]
        self.assertIn("uses: actions/cache/restore@v4", restore)
        self.assertIn("ai-crypto-diag-v1-${{ github.ref_name }}-${{ github.run_id }}-${{ github.run_attempt }}", restore)
        self.assertIn("ai_crypto_monitor/state/scan-diagnostics.jsonl", restore)
        self.assertNotIn("dedup.json", restore)
        self.assertIn("scan_diagnostics prepare", prepare)
        self.assertIn("if: ${{ always() && steps.cadence.outputs.active == 'true' }}", ensure)
        self.assertIn("scan_diagnostics ensure-error", ensure)
        self.assertIn("steps.live.outcome || steps.dry.outcome", ensure)
        self.assertIn("uses: actions/cache/save@v4", save)
        self.assertIn("github.run_attempt", save)
        self.assertIn("uses: actions/upload-artifact@v4", upload)
        self.assertIn("scan-diag-${{ github.run_id }}-${{ github.run_attempt }}", upload)
        self.assertIn("retention-days: 14", upload)
        self.assertIn("current-scan-diagnostic.jsonl", upload)
        self.assertNotIn("SCAN_DIAGNOSTICS_", steps["Run tests"])
        self.assertNotIn("    env:\n      SCAN_DIAGNOSTICS_", jobs()["monitor"].split("    steps:", 1)[0])
        for name in ("Prepare scan diagnostics", DRY_STEP, LIVE_STEP, "Ensure every active run has a diagnostic row"):
            self.assertIn("SCAN_DIAGNOSTICS_FILE:", steps[name], name)

    def test_never_calls_telegram_directly_or_uses_local_credentials(self) -> None:
        text = "\n".join(active_lines())
        for fragment in NEVER:
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
