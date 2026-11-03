from __future__ import annotations

import unittest

from backtest import v3_report
from backtest import v3_run


def trade(value: float, exit_ms: int, complete: bool = True) -> dict:
    return {
        "net_r": value, "net_r_cost_x2": value - 0.1, "exit_ms": exit_ms,
        "entry_ms": exit_ms - 1, "symbol": "BTC-USDT-SWAP", "funding_complete": complete,
        "duration_hours": 24, "mfe_r": max(value, 0), "mae_r": min(value, 0),
        "fees": 1, "slippage": 1, "funding": 0,
    }


class V3ReportTests(unittest.TestCase):
    def test_development_selection_excludes_outcomes_after_the_boundary(self) -> None:
        bounds = {"start_ms": 0, "development_end_ms": 100}
        completed = dict(trade(1, 90), entry_ms=50)
        crosses = dict(trade(1000, 110), entry_ms=80)
        self.assertEqual([completed], v3_run.development_trades([completed, crosses], bounds))

    def test_plateau_requires_same_sign_and_both_neighbors_within_delta(self) -> None:
        rows = [
            {"parameter": "channel_window_4h", "value": 48, "mean_r": 0.20},
            {"parameter": "channel_window_4h", "value": 60, "mean_r": 0.25},
            {"parameter": "channel_window_4h", "value": 72, "mean_r": 0.15},
        ]
        config = {"plateau_max_delta_r": 0.15}
        result = v3_run.plateau_evaluation(rows, config)[0]
        self.assertTrue(result["plateau"])

    def test_setup_csv_collapse_keeps_one_row_and_the_full_status_history(self) -> None:
        base = {"variant": "V", "channel_id": "C", "symbol": "BTC-USDT-SWAP",
                "direction": "LONG", "breakout_ms": 1, "split": "development"}
        rows = [
            {**base, "status": "BREAKOUT", "status_ms": 1},
            {**base, "status": "RETESTED", "status_ms": 2},
            {**base, "status": "CONFIRMED", "status_ms": 3, "entry_plan": 100},
            {**base, "status": "FILLED", "status_ms": 4, "entry_raw": 100},
        ]
        collapsed = v3_run.collapse_setups(rows)
        self.assertEqual(1, len(collapsed))
        self.assertEqual("FILLED", collapsed[0]["final_status"])
        self.assertTrue(collapsed[0]["retested"])
        self.assertEqual("BREAKOUT>RETESTED>CONFIRMED>FILLED", collapsed[0]["status_history"])

    def test_primary_metrics_exclude_incomplete_funding(self) -> None:
        item = v3_report.metrics([trade(2, 1), trade(-10, 2, False), trade(-1, 3)])
        self.assertEqual(2, item["n"])
        self.assertEqual(1, item["funding_incomplete"])
        self.assertAlmostEqual(0.5, item["mean_r"])

    def test_drawdown_uses_exit_order(self) -> None:
        item = v3_report.metrics([trade(2, 3), trade(-1, 1), trade(-1, 2), trade(-1, 4)])
        self.assertEqual(2, item["max_drawdown_r"])
        self.assertEqual(2, item["longest_loss_streak"])

    def test_funnel_keeps_rejection_reasons_separate(self) -> None:
        rows = [{"status": "MISSING_STRUCTURAL_TARGET"}, {"status": "TP1_TOO_CLOSE"},
                {"status": "MISSING_STRUCTURAL_TARGET"}]
        self.assertEqual({"MISSING_STRUCTURAL_TARGET": 2, "TP1_TOO_CLOSE": 1}, v3_report.funnel(rows))


if __name__ == "__main__":
    unittest.main()
