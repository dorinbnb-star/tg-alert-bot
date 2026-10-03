from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from ai_crypto_monitor import entry_monitor as em  # noqa: E402
from backtest import download, report, simulate, variants  # noqa: E402

RULES = json.loads((PROJECT_ROOT / "ai_crypto_monitor" / "rules-v0.1.json").read_text())
COSTS = json.loads((PROJECT_ROOT / "backtest" / "costs.json").read_text())
MINUTE = 60_000
QUARTER = 15 * MINUTE
HOUR = 60 * MINUTE
T0 = download.parse_utc("2026-03-10T10:00:00Z")


def candle(start: int, o: float, h: float, l: float, c: float) -> em.Candle:
    return em.Candle(start, o, h, l, c, 1.0)


def signal(direction: str = "LONG", entry: float = 100.0, stop: float = 99.0, target: float = 102.0) -> em.EntrySignal:
    return em.EntrySignal(
        symbol="BTC-USDT-SWAP", direction=direction, reference_level=99.5, reference_confirmed_at_ms=0,
        sweep_start_ms=0, sweep_extreme=99.1, reentry_start_ms=0, confirmation_start_ms=T0 - QUARTER,
        confirmation_end_ms=T0, entry=entry, stop=stop, target=target, rr=abs(target - entry) / abs(entry - stop),
        expires_at_ms=T0 + 420_000, confluence_score=81, probability="NECALIBRATA",
    )


class CadenceTests(unittest.TestCase):
    def test_scan_follows_the_live_grid(self) -> None:
        self.assertEqual(T0 + 45_000, simulate.scan_time(T0))  # close at :00
        self.assertEqual(T0 + QUARTER + 345_000, simulate.scan_time(T0 + QUARTER))  # close at :15 -> :20:45
        self.assertEqual(T0 + 2 * QUARTER + 45_000, simulate.scan_time(T0 + 2 * QUARTER))  # :30 -> :30:45
        self.assertEqual(T0 + 3 * QUARTER + 345_000, simulate.scan_time(T0 + 3 * QUARTER))  # :45 -> :50:45
        grid = simulate.scan_grid(T0, T0 + HOUR)
        self.assertEqual(4, len(grid))
        for scan_ms in grid:  # every scan lands on the :x0:45 grid and within entry_valid_seconds of its close
            self.assertEqual(45_000, scan_ms % (10 * MINUTE))

    def test_brussels_window_follows_dst(self) -> None:
        self.assertTrue(simulate.in_live_window(download.parse_utc("2026-01-15T07:00:45Z")))  # 08:00 CET
        self.assertFalse(simulate.in_live_window(download.parse_utc("2026-01-15T06:50:45Z")))
        self.assertTrue(simulate.in_live_window(download.parse_utc("2026-07-15T06:00:45Z")))  # 08:00 CEST
        self.assertTrue(simulate.in_live_window(download.parse_utc("2026-07-15T19:50:45Z")))  # 21:50 CEST
        self.assertFalse(simulate.in_live_window(download.parse_utc("2026-07-15T20:00:45Z")))  # 22:00 CEST


class ExitTests(unittest.TestCase):
    def path(self, minutes: list[em.Candle], quarters: list[em.Candle], scan_ms: int = T0 + 45_000,
             sig: em.EntrySignal | None = None) -> simulate.ExitPath:
        sig = sig or signal()
        return simulate.find_exit(sig.direction, sig.stop, sig.target, scan_ms, minutes, [c.start_ms for c in minutes],
                                  quarters, [c.start_ms for c in quarters], T0 + 10 * HOUR, 100.0)

    def flat_minutes(self) -> list[em.Candle]:
        return [candle(T0 + i * MINUTE, 100, 100.2, 99.8, 100) for i in range(15)]

    def test_tp_and_sl_in_the_same_candle_count_as_sl(self) -> None:
        quarters = [candle(T0 + QUARTER, 100, 102.5, 98.5, 100)]
        result = self.path(self.flat_minutes(), quarters)
        self.assertEqual(("SL", 99.0, True), (result.outcome, result.price, result.ambiguous))

    def test_minute_containing_t_can_only_stop_out(self) -> None:
        minutes = self.flat_minutes()
        minutes[0] = candle(T0, 100, 103, 99.5, 100)  # TP touched in the minute that started before entry
        result = self.path(minutes, [candle(T0 + QUARTER, 100, 100.5, 99.5, 100)])
        self.assertEqual("OPEN", result.outcome)
        minutes[0] = candle(T0, 100, 100.5, 98.9, 100)
        self.assertEqual("SL", self.path(minutes, []).outcome)

    def test_take_profit_on_a_later_minute_and_on_15m(self) -> None:
        minutes = self.flat_minutes()
        minutes[3] = candle(T0 + 3 * MINUTE, 100, 102.1, 99.9, 102)
        result = self.path(minutes, [])
        self.assertEqual(("TP", 102.0, T0 + 4 * MINUTE), (result.outcome, result.price, result.exit_ms))
        result = self.path(self.flat_minutes(), [candle(T0 + QUARTER, 100, 102.1, 99.5, 102)])
        self.assertEqual(("TP", T0 + 2 * QUARTER), (result.outcome, result.exit_ms))

    def test_gap_through_stop_fills_at_the_open(self) -> None:
        result = self.path(self.flat_minutes(), [candle(T0 + QUARTER, 98.5, 99, 98, 98.6)])
        self.assertEqual(("SL", 98.5, True), (result.outcome, result.price, result.gap))

    def test_short_mirror(self) -> None:
        sig = signal("SHORT", entry=100, stop=101, target=98)
        result = self.path(self.flat_minutes(), [candle(T0 + QUARTER, 100, 100.5, 97.9, 98)], sig=sig)
        self.assertEqual(("TP", 98), (result.outcome, result.price))

    def test_missing_minutes_fall_back_to_15m_stop_only(self) -> None:
        quarters = [candle(T0, 100, 103, 99.5, 100), candle(T0 + QUARTER, 100, 100.5, 99.5, 100)]
        result = self.path([], quarters)
        self.assertEqual(("OPEN", True), (result.outcome, result.minute_fallback))


class CostTests(unittest.TestCase):
    def test_costs_in_r(self) -> None:
        funding = simulate.Funding([], COSTS["funding_estimate"])
        path = simulate.ExitPath("TP", 102.0, T0 + QUARTER, False, False, False)
        result = simulate.trade_result(signal(), T0 + 45_000, 100.0, path, funding, COSTS, 1.0)
        entry_fill = 100 * 1.0002
        exit_fill = 102 * (1 - 0.0002)
        expected = (exit_fill - entry_fill - 0.0005 * (entry_fill + exit_fill)) / 1.0
        self.assertAlmostEqual(expected, result["r"], places=9)
        self.assertEqual(0, result["funding_est_n"])

    def test_stop_slippage_is_doubled_and_x2_sensitivity_costs_more(self) -> None:
        funding = simulate.Funding([], COSTS["funding_estimate"])
        path = simulate.ExitPath("SL", 99.0, T0 + QUARTER, False, False, False)
        base = simulate.trade_result(signal(), T0 + 45_000, 100.0, path, funding, COSTS, 1.0)
        double = simulate.trade_result(signal(), T0 + 45_000, 100.0, path, funding, COSTS, 2.0)
        self.assertAlmostEqual((100 * 0.0002 + 99 * 0.0004) / 1.0, base["slippage_r"], places=9)
        self.assertLess(double["r"], base["r"])

    def test_real_funding_and_conservative_estimate_before_history(self) -> None:
        real_start = T0 + 8 * HOUR
        items = [{"fundingTime": str(real_start + i * 8 * HOUR), "fundingRate": "0.0003", "realizedRate": ""}
                 for i in range(10)]
        funding = simulate.Funding(items, COSTS["funding_estimate"])
        self.assertAlmostEqual(0.0003, funding.estimate_rate)
        real, estimated = funding.events(T0 - 9 * HOUR, real_start + 9 * HOUR)
        self.assertEqual([0.0003, 0.0003], real)  # real_start and real_start + 8h
        self.assertEqual(2, estimated)  # real_start - 8h and real_start - 16h
        short = signal("SHORT", entry=100, stop=101, target=98)
        path = simulate.ExitPath("TP", 98.0, real_start + 9 * HOUR, False, False, False)
        result = simulate.trade_result(short, T0 - 9 * HOUR, 100.0, path, funding, COSTS, 1.0)
        self.assertLess(result["funding_real_r"], 0)  # short receives a positive rate
        self.assertGreater(result["funding_est_r"], 0)  # the estimate is always charged against the position

    def test_estimate_grid_without_any_history(self) -> None:
        funding = simulate.Funding([], COSTS["funding_estimate"])
        self.assertEqual(([], 3), funding.events(download.parse_utc("2026-03-10T00:00:00Z"),
                                                 download.parse_utc("2026-03-11T00:00:01Z")))


class VariantTests(unittest.TestCase):
    def test_variant_b_copies_rules_and_c2_patch_is_restored(self) -> None:
        rules = dict(RULES)
        self.assertEqual(2.0, variants.variant_rules("b", rules)["minimum_rr_for_enter"])
        self.assertEqual(1.8, rules["minimum_rr_for_enter"])
        original = em.opposing_target
        with variants.second_pivot_target():
            self.assertIs(em.shadow_target, em.opposing_target)
        self.assertIs(original, em.opposing_target)

    def test_c1_moves_only_the_target(self) -> None:
        row = {"signal": json.loads(json.dumps(signal().__dict__)), "c1_target": 103.0}
        moved = simulate.variant_signal("c1", row)
        self.assertEqual((100.0, 99.0, 103.0, 3.0), (moved.entry, moved.stop, moved.target, moved.rr))
        self.assertEqual(signal().signature, moved.signature)
        self.assertIsNone(simulate.variant_signal("c1", dict(row, c1_target=None)))


class ReportTests(unittest.TestCase):
    def test_drawdown_uses_exit_order_and_unit_r(self) -> None:
        trades = [
            {"r": 2.0, "exit_ms": 3, "scan_ms": 1, "symbol": "A"},
            {"r": -1.0, "exit_ms": 1, "scan_ms": 2, "symbol": "A"},
            {"r": -1.0, "exit_ms": 2, "scan_ms": 3, "symbol": "A"},
            {"r": -1.5, "exit_ms": 4, "scan_ms": 4, "symbol": "A"},
        ]
        self.assertEqual(2.0, report.max_drawdown(trades, "r"))

    def test_bootstrap_is_reproducible(self) -> None:
        values = [1.0, -1.0, 2.0, -1.0, 0.5]
        self.assertEqual(report.bootstrap_ci(values), report.bootstrap_ci(values))


if __name__ == "__main__":
    unittest.main()
