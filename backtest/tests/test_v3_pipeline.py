from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backtest import v3_data, v3_engine, v3_run, v3_strategy

ROOT = Path(__file__).resolve().parents[2]
COSTS = json.loads((ROOT / "backtest" / "costs.json").read_text(encoding="utf-8"))


def synthetic_market() -> dict[str, v3_engine.MarketData]:
    h4 = []
    for index in range(70):
        high, low, close = 100.5, 99.5, 100.0
        if index in (10, 30):
            high = 105.0
        if index in (20, 40):
            low = 95.0
        if index == 60:
            high, low, close = 106.5, 99.5, 106.0
        h4.append(v3_strategy.Candle(index * v3_strategy.FOUR_HOURS_MS, 100, high, low, close, 100))

    h1 = []
    for index in range(70 * 4):
        h1.append(v3_strategy.Candle(index * v3_strategy.HOUR_MS, 100, 100.2, 99.8, 100, 25))
    first_after_breakout = 61 * 4
    h1[first_after_breakout] = v3_strategy.Candle(first_after_breakout * v3_strategy.HOUR_MS,
                                                   105.4, 105.6, 104.8, 105.2, 25)
    h1[first_after_breakout + 1] = v3_strategy.Candle((first_after_breakout + 1) * v3_strategy.HOUR_MS,
                                                       105.2, 106.8, 105.0, 106.5, 25)
    h1[first_after_breakout + 2] = v3_strategy.Candle((first_after_breakout + 2) * v3_strategy.HOUR_MS,
                                                       106.0, 106.4, 105.8, 106.1, 25)
    h1[first_after_breakout + 3] = v3_strategy.Candle((first_after_breakout + 3) * v3_strategy.HOUR_MS,
                                                       106.1, 116.0, 106.0, 115.0, 25)
    end = h1[-1].start_ms + v3_strategy.HOUR_MS
    funding = [{"fundingTime": str(timestamp), "realizedRate": "0.0001"}
               for timestamp in range(0, end + 8 * v3_strategy.HOUR_MS, 8 * v3_strategy.HOUR_MS)]
    return {"BTC-USDT-SWAP": v3_engine.MarketData(h1, h4, [], funding, 0.1)}


class V3PipelineTests(unittest.TestCase):
    def test_funding_archive_windows_stay_below_ten_months_and_end_exclusive(self) -> None:
        start = v3_data.parse_utc("2022-03-01T00:00:00Z")
        end = v3_data.parse_utc("2026-11-01T00:00:00Z")
        windows = v3_data.month_windows(start, end)
        self.assertEqual(start, windows[0][0])
        self.assertEqual(end - 1, windows[-1][1])
        self.assertTrue(all(right < end for _, right in windows))
        self.assertTrue(all(right - left < 276 * v3_strategy.DAY_MS for left, right in windows))

    def test_closed_candle_pipeline_detects_breakout_retest_confirmation_and_tp(self) -> None:
        config = dict(v3_engine.load_config(), symbols=["BTC-USDT-SWAP"])
        market = synthetic_market()
        prepared = v3_engine.prepare_market(market, config)
        result = v3_engine.run_variant(
            market, config, COSTS,
            v3_engine.Variant("ENTRY_MARKET_NEXT_OPEN", "REFERENCE", 2), prepared,
        )
        statuses = [row["status"] for row in result.setups]
        self.assertIn("BREAKOUT", statuses)
        self.assertIn("CONFIRMED", statuses)
        self.assertIn("FILLED", statuses)
        self.assertEqual("TP", result.trades[0]["outcome"])
        self.assertTrue(result.trades[0]["funding_complete"])
        # Confirmation is stamped at the close; the next candle opens at that same timestamp.
        self.assertEqual(result.trades[0]["confirmation_ms"], result.trades[0]["entry_ms"])

    def test_same_data_produces_byte_equivalent_rows(self) -> None:
        config = dict(v3_engine.load_config(), symbols=["BTC-USDT-SWAP"])
        market = synthetic_market()
        prepared = v3_engine.prepare_market(market, config)
        variant = v3_engine.Variant("ENTRY_MARKET_NEXT_OPEN", "REFERENCE", 2)
        first = v3_engine.run_variant(market, config, COSTS, variant, prepared)
        second = v3_engine.run_variant(market, config, COSTS, variant, prepared)
        self.assertEqual(first.setups, second.setups)
        self.assertEqual(first.trades, second.trades)

    def test_random_baseline_is_deterministic_for_the_same_seed(self) -> None:
        config = dict(v3_engine.load_config(), symbols=["BTC-USDT-SWAP"])
        market = synthetic_market()
        prepared = v3_engine.prepare_market(market, config)
        variant = v3_engine.Variant("ENTRY_MARKET_NEXT_OPEN", "REFERENCE", 2)
        first = v3_engine.run_variant(market, config, COSTS, variant, prepared, random_seed=17)
        second = v3_engine.run_variant(market, config, COSTS, variant, prepared, random_seed=17)
        self.assertEqual(first.setups, second.setups)
        self.assertEqual(first.trades, second.trades)

    def test_runner_writes_the_four_declared_artifacts(self) -> None:
        market = synthetic_market()["BTC-USDT-SWAP"]
        one_symbol = dict(v3_engine.load_config(), symbols=["BTC-USDT-SWAP"], random_baseline_seeds=2)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            results = Path(temporary) / "results"
            v3_data.write_rows(v3_data.series_path(root, "BTC-USDT-SWAP", "1H"), [
                [str(c.start_ms), c.open, c.high, c.low, c.close, c.volume, "0", "0", "1"] for c in market.h1
            ])
            v3_data.write_rows(v3_data.series_path(root, "BTC-USDT-SWAP", "4H"), [
                [str(c.start_ms), c.open, c.high, c.low, c.close, c.volume, "0", "0", "1"] for c in market.h4
            ])
            v3_data.write_rows(v3_data.series_path(root, "BTC-USDT-SWAP", "1D"), [])
            v3_data.write_rows(v3_data.series_path(root, "BTC-USDT-SWAP", "funding"), market.funding)
            v3_data.save_manifest(root, {"BTC-USDT-SWAP": {"instrument": {"tickSz": "0.1"}}})
            with patch("backtest.v3_engine.load_config", return_value=one_symbol):
                code = v3_run.main(["--data-dir", str(root), "--results-dir", str(results),
                                    "--baseline-seeds", "2", "--skip-sensitivity", "--quiet"])
            self.assertEqual(0, code)
            for name in ("v3-setups.csv", "v3-trades.csv", "v3-metrics.json", "v3-report.md"):
                self.assertTrue((results / name).is_file(), name)
            payload = json.loads((results / "v3-metrics.json").read_text(encoding="utf-8"))
            self.assertIn("selection", payload["diagnostics"])


if __name__ == "__main__":
    unittest.main()
