from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
V3_WORKFLOW = ROOT / ".github" / "workflows" / "backtest-v3.yml"
V1_WORKFLOW = ROOT / ".github" / "workflows" / "backtest.yml"
V3_CODE = sorted((ROOT / "backtest").glob("v3_*.py"))


class V3IsolationTests(unittest.TestCase):
    def test_v3_has_no_live_bot_telegram_secrets_or_order_endpoint(self) -> None:
        for path in V3_CODE:
            text = path.read_text(encoding="utf-8")
            for forbidden in (
                "entry_monitor", "rules-v0.1", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID",
                "sendMessage", "/api/v5/trade", "/api/v5/account", "detect_entry",
            ):
                self.assertNotIn(forbidden, text, f"{path.name}: {forbidden}")

    def test_v3_workflow_is_read_only_branch_scoped_and_secret_free(self) -> None:
        text = V3_WORKFLOW.read_text(encoding="utf-8")
        head = text.split("permissions:")[0]
        self.assertIn("codex/github-actions-monitor", head)
        self.assertNotIn("schedule:", head)
        self.assertNotIn("pull_request:", head)
        self.assertIn("contents: read", text)
        self.assertNotIn("secrets.", text)
        self.assertNotIn("TELEGRAM", text)
        self.assertIn("if: github.ref != 'refs/heads/main'", text)
        self.assertIn("group: backtest-v3-${{ github.ref }}", text)

    def test_v1_workflow_does_not_trigger_for_v3_files(self) -> None:
        head = V1_WORKFLOW.read_text(encoding="utf-8").split("permissions:")[0]
        self.assertNotIn('      - "backtest/**"', head)
        self.assertNotIn("v3_", head)

    def test_v3_outputs_use_existing_ignored_roots(self) -> None:
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("backtest_data/", ignored)
        self.assertIn("backtest_results/", ignored)


if __name__ == "__main__":
    unittest.main()
