"""The backtest stays separate from the live bot: no secrets, no Telegram, no orders, never on main."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "backtest.yml"
LIVE_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ai-crypto-trader.yml"
CODE = sorted((PROJECT_ROOT / "backtest").glob("*.py"))


class IsolationTests(unittest.TestCase):
    def test_backtest_code_never_touches_telegram_secrets_or_orders(self) -> None:
        for path in CODE:
            text = path.read_text(encoding="utf-8")
            for forbidden in ("TELEGRAM", "telegram_call", "sendMessage", "/api/v5/trade", "/api/v5/account",
                              "credentials(", "scan_once", "write_json(", "state_dir("):
                self.assertNotIn(forbidden, text, f"{path.name}: {forbidden}")

    def test_workflow_runs_only_on_push_to_the_backtest_branch_and_never_on_main(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        head = text.split("permissions:")[0]
        self.assertIn("on:\n  push:\n    branches:\n      - codex/github-actions-monitor\n", head)
        self.assertIn('      - "backtest/**"\n      - ".github/workflows/backtest.yml"\n', head)
        for other in ("schedule", "workflow_dispatch", "pull_request", "main\n"):
            self.assertNotIn(other, head)
        self.assertIn("    if: github.ref != 'refs/heads/main'\n", text)

    def test_workflow_has_no_secrets_own_concurrency_timeout_and_read_only_token(self) -> None:
        text = "\n".join(line for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
                         if not line.lstrip().startswith("#")) + "\n"
        self.assertNotIn("secrets", text)
        self.assertNotIn("TELEGRAM", text)
        self.assertNotIn("actions: write", text)
        self.assertIn("permissions:\n  contents: read\n", text)
        self.assertIn("  group: backtest-v1-${{ github.ref }}\n", text)
        live_group = [line for line in LIVE_WORKFLOW.read_text(encoding="utf-8").splitlines() if "group:" in line]
        self.assertTrue(live_group and all("backtest" not in line for line in live_group))
        self.assertIn("    timeout-minutes: 150\n", text)
        self.assertIn("GITHUB_STEP_SUMMARY", (PROJECT_ROOT / "backtest" / "report.py").read_text(encoding="utf-8"))
        self.assertIn("actions/upload-artifact@v4", text)

    def test_market_data_and_results_are_git_ignored(self) -> None:
        ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("backtest_data/", ignored)
        self.assertIn("backtest_results/", ignored)


if __name__ == "__main__":
    unittest.main()
