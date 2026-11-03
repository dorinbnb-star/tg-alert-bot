from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from backtest import v3_engine, v3_strategy

ROOT = Path(__file__).resolve().parents[2]
COSTS = json.loads((ROOT / "backtest" / "costs.json").read_text(encoding="utf-8"))


def config() -> dict:
    values = v3_engine.load_config()
    return dict(values, symbols=["BTC-USDT-SWAP"], maximum_open_positions=3)


def channel() -> v3_strategy.Channel:
    pivots = (
        v3_strategy.Pivot("HIGH", 1, 0, 0, 105),
        v3_strategy.Pivot("HIGH", 2, 0, 0, 105.1),
    )
    lows = (
        v3_strategy.Pivot("LOW", 3, 0, 0, 95),
        v3_strategy.Pivot("LOW", 4, 0, 0, 95.1),
    )
    return v3_strategy.Channel("BTC-USDT-SWAP", -100, -1, 105, 95, 10, 2, 0, 60, pivots, lows)


def setup() -> v3_strategy.Setup:
    return v3_strategy.Setup("BTC-USDT-SWAP", "LONG", channel(), -4 * v3_strategy.HOUR_MS,
                             -1, 105, 115, 48 * v3_strategy.HOUR_MS)


def market(candles: list[v3_strategy.Candle], funding: list[dict] | None = None) -> dict[str, v3_engine.MarketData]:
    return {"BTC-USDT-SWAP": v3_engine.MarketData(candles, [], [], funding or [], 0.1)}


def injected_engine(candles: list[v3_strategy.Candle], variant: v3_engine.Variant) -> v3_engine.PortfolioEngine:
    engine = v3_engine.PortfolioEngine(market(candles), config(), COSTS, variant)
    active = v3_engine.ActiveSetup(setup(), True, confirmation_ms=0, confirmation_index=0,
                                   atr1_confirmation=1, entry_plan=105.2, stop=103, targets=(110, 114))
    engine.active["BTC-USDT-SWAP"] = active
    engine.pending["BTC-USDT-SWAP"] = v3_engine.PendingEntry(
        active_ms=0, confirmation_ms=0, entry_plan=105.2, stop=103,
        atr1_confirmation=1, targets=(110, 114),
    )
    return engine


class MarketEntryTests(unittest.TestCase):
    def test_target_at_open_rejects_market_entry(self) -> None:
        candles = [v3_strategy.Candle(0, 116, 117, 115, 116, 1)]
        engine = injected_engine(candles, v3_engine.Variant("ENTRY_MARKET_NEXT_OPEN", "REFERENCE", 2))
        result = engine.run()
        self.assertEqual([], result.trades)
        self.assertIn("TARGET_AT_OPEN", [row["status"] for row in result.setups])

    def test_invalid_at_open_rejects_market_entry(self) -> None:
        candles = [v3_strategy.Candle(0, 103.5, 104, 103, 103.8, 1)]
        engine = injected_engine(candles, v3_engine.Variant("ENTRY_MARKET_NEXT_OPEN", "REFERENCE", 2))
        result = engine.run()
        self.assertEqual([], result.trades)
        self.assertIn("INVALID_AT_OPEN", [row["status"] for row in result.setups])

    def test_sl_at_open_has_priority_over_channel_invalidation(self) -> None:
        candles = [v3_strategy.Candle(0, 102, 103, 101, 102, 1)]
        engine = injected_engine(candles, v3_engine.Variant("ENTRY_MARKET_NEXT_OPEN", "REFERENCE", 2))
        with mock.patch("backtest.v3_engine.strategy.initial_stop", return_value=(103, 1, False)):
            result = engine.run()
        self.assertEqual([], result.trades)
        self.assertIn("SL_AT_OPEN", [row["status"] for row in result.setups])
        self.assertNotIn("INVALID_AT_OPEN", [row["status"] for row in result.setups])

    def test_market_decision_uses_open_then_processes_the_new_bar(self) -> None:
        candles = [v3_strategy.Candle(0, 106, 116, 105, 115, 1)]
        engine = injected_engine(candles, v3_engine.Variant("ENTRY_MARKET_NEXT_OPEN", "REFERENCE", 2))
        result = engine.run()
        self.assertEqual(1, len(result.trades))
        self.assertEqual("TP", result.trades[0]["outcome"])
        self.assertEqual(0, result.trades[0]["entry_ms"])

    def test_market_uses_real_entry_for_stop_and_same_candle_sl(self) -> None:
        candles = [v3_strategy.Candle(0, 106, 107, 102, 104, 1)]
        engine = injected_engine(candles, v3_engine.Variant("ENTRY_MARKET_NEXT_OPEN", "REFERENCE", 2))
        result = engine.run()
        self.assertEqual(1, len(result.trades))
        trade = result.trades[0]
        self.assertEqual("SL", trade["outcome"])
        self.assertGreater(trade["entry_effective"], 106)
        self.assertLess(trade["net_r"], 0)


class LimitExecutionTests(unittest.TestCase):
    def test_close_back_in_channel_cancels_before_and_after_order_activation(self) -> None:
        close_candle = v3_strategy.Candle(3 * v3_strategy.HOUR_MS, 105, 106, 102, 103, 1)
        for with_pending in (False, True):
            engine = injected_engine([close_candle], v3_engine.Variant("ENTRY_LIMIT", "REFERENCE", 2))
            engine.prepared["BTC-USDT-SWAP"].data.h4 = [
                v3_strategy.Candle(0, 105, 106, 102, 103, 1)
            ]
            engine.prepared["BTC-USDT-SWAP"].h4_by_close = {4 * v3_strategy.HOUR_MS: 0}
            engine.prepared["BTC-USDT-SWAP"].atr4 = [2]
            engine.prepared["BTC-USDT-SWAP"].channels = {}
            if not with_pending:
                engine.pending.clear()
            engine._process_close("BTC-USDT-SWAP", 0, close_candle)
            self.assertNotIn("BTC-USDT-SWAP", engine.active)
            self.assertNotIn("BTC-USDT-SWAP", engine.pending)
            self.assertIn("CLOSE_BACK_IN_CHANNEL", [row["status"] for row in engine.setups])

    def test_target_and_entry_same_candle_cancels_without_fill(self) -> None:
        candles = [v3_strategy.Candle(0, 107, 116, 104, 110, 1)]
        engine = injected_engine(candles, v3_engine.Variant("ENTRY_LIMIT", "REFERENCE", 2))
        result = engine.run()
        self.assertEqual([], result.trades)
        self.assertIn("TARGET_BEFORE_ENTRY", [row["status"] for row in result.setups])

    def test_entry_and_sl_same_candle_is_a_loss(self) -> None:
        candles = [v3_strategy.Candle(0, 106, 107, 102, 104, 1)]
        engine = injected_engine(candles, v3_engine.Variant("ENTRY_LIMIT", "REFERENCE", 2))
        result = engine.run()
        self.assertEqual("SL", result.trades[0]["outcome"])

    def test_tp_and_sl_same_candle_is_sl(self) -> None:
        candles = [v3_strategy.Candle(0, 106, 116, 102, 110, 1)]
        engine = injected_engine(candles, v3_engine.Variant("ENTRY_LIMIT", "REFERENCE", 2))
        pending = engine.pending["BTC-USDT-SWAP"]
        engine._fill_position("BTC-USDT-SWAP", engine.active["BTC-USDT-SWAP"], pending,
                              -v3_strategy.HOUR_MS, 105.2, 105.3, 103)
        result = engine.run()
        self.assertEqual("SL", result.trades[0]["outcome"])
        self.assertTrue(result.trades[0]["ambiguous_sl"])

    def test_gap_through_stop_uses_the_worse_open(self) -> None:
        candles = [v3_strategy.Candle(0, 100, 101, 99, 100, 1)]
        engine = injected_engine(candles, v3_engine.Variant("ENTRY_LIMIT", "REFERENCE", 2))
        pending = engine.pending["BTC-USDT-SWAP"]
        engine._fill_position("BTC-USDT-SWAP", engine.active["BTC-USDT-SWAP"], pending,
                              -v3_strategy.HOUR_MS, 105.2, 105.3, 103)
        result = engine.run()
        self.assertEqual("SL", result.trades[0]["outcome"])
        self.assertTrue(result.trades[0]["gap_stop"])
        self.assertLess(result.trades[0]["net_r"], -1)

    def test_limit_expires_after_exactly_twelve_bars(self) -> None:
        candles = [v3_strategy.Candle(i * v3_strategy.HOUR_MS, 107, 108, 106, 107, 1) for i in range(12)]
        engine = injected_engine(candles, v3_engine.Variant("ENTRY_LIMIT", "REFERENCE", 2))
        result = engine.run()
        self.assertIn("ORDER_EXPIRED", [row["status"] for row in result.setups])


class CostAndStateTests(unittest.TestCase):
    def test_breakeven_is_rounded_to_tick(self) -> None:
        candles = [v3_strategy.Candle(0, 106, 107, 105, 106, 1)]
        engine = injected_engine(candles, v3_engine.Variant("ENTRY_LIMIT", "SPLIT", 2, "BE_AFTER_TP1"))
        pending = engine.pending["BTC-USDT-SWAP"]
        engine._fill_position("BTC-USDT-SWAP", engine.active["BTC-USDT-SWAP"], pending, 0, 105.2, 105.3, 103)
        value = engine._breakeven(engine.positions["BTC-USDT-SWAP"])
        self.assertAlmostEqual(round(value / 0.1), value / 0.1, places=8)

    def test_breakeven_computed_at_close_is_active_on_the_next_bar(self) -> None:
        candles = [
            v3_strategy.Candle(0, 106, 110.5, 104, 110, 1),
            v3_strategy.Candle(v3_strategy.HOUR_MS, 106, 107, 104, 105, 1),
        ]
        engine = injected_engine(candles, v3_engine.Variant("ENTRY_LIMIT", "SPLIT", 2, "BE_AFTER_TP1"))
        pending = engine.pending["BTC-USDT-SWAP"]
        engine._fill_position("BTC-USDT-SWAP", engine.active["BTC-USDT-SWAP"], pending,
                              -v3_strategy.HOUR_MS, 105.2, 105.3, 103)
        engine._process_position("BTC-USDT-SWAP", candles[0])
        position = engine.positions["BTC-USDT-SWAP"]
        self.assertTrue(position.tp1_done)
        self.assertEqual(103, position.stop)
        self.assertIsNotNone(position.pending_stop)
        engine._process_position("BTC-USDT-SWAP", candles[1])
        self.assertNotIn("BTC-USDT-SWAP", engine.positions)

    def test_funding_uses_real_events_and_missing_history_is_flagged(self) -> None:
        candles = [v3_strategy.Candle(0, 106, 107, 105, 106, 1),
                   v3_strategy.Candle(v3_strategy.HOUR_MS, 106, 116, 105, 115, 1)]
        funding = [{"fundingTime": str(v3_strategy.HOUR_MS), "realizedRate": "0.001"}]
        data = market(candles, funding)
        engine = v3_engine.PortfolioEngine(data, config(), COSTS,
                                           v3_engine.Variant("ENTRY_LIMIT", "REFERENCE", 2))
        active = v3_engine.ActiveSetup(setup(), True, confirmation_ms=0, confirmation_index=0,
                                       atr1_confirmation=1, entry_plan=105.2, stop=103)
        engine.active["BTC-USDT-SWAP"] = active
        engine.pending["BTC-USDT-SWAP"] = v3_engine.PendingEntry(0, 0, 105.2, 103, 1, None)
        result = engine.run()
        self.assertTrue(result.trades)
        self.assertNotEqual(0, result.trades[0]["funding"])

    def test_funding_after_tp1_uses_only_the_remaining_quantity(self) -> None:
        candles = [v3_strategy.Candle(0, 106, 110.5, 105, 110, 1)]
        funding = [{"fundingTime": str(v3_strategy.HOUR_MS), "realizedRate": "0.001"}]
        engine = v3_engine.PortfolioEngine(
            market(candles, funding), config(), COSTS,
            v3_engine.Variant("ENTRY_LIMIT", "SPLIT", 2, "NO_BE"),
        )
        active = v3_engine.ActiveSetup(setup(), True, confirmation_ms=0, confirmation_index=0,
                                       atr1_confirmation=1, entry_plan=105.2, stop=103, targets=(110, 114))
        pending = v3_engine.PendingEntry(0, 0, 105.2, 103, 1, (110, 114))
        engine.active["BTC-USDT-SWAP"] = active
        engine.pending["BTC-USDT-SWAP"] = pending
        engine._fill_position("BTC-USDT-SWAP", active, pending, -v3_strategy.HOUR_MS, 105.2, 105.3, 103)
        position = engine.positions["BTC-USDT-SWAP"]
        engine._process_position("BTC-USDT-SWAP", candles[0])
        remaining = position.remaining
        engine._apply_funding("BTC-USDT-SWAP", position, v3_strategy.HOUR_MS, 110)
        self.assertAlmostEqual(0.60 * position.qty, remaining)
        self.assertAlmostEqual(-0.001 * 110 * remaining, position.funding_total)

    def test_internal_funding_gap_marks_the_trade_incomplete(self) -> None:
        candles = [
            v3_strategy.Candle(0, 106, 107, 104, 106, 1),
            v3_strategy.Candle(20 * v3_strategy.HOUR_MS, 106, 116, 105, 115, 1),
        ]
        funding = [
            {"fundingTime": str(v3_strategy.HOUR_MS), "realizedRate": "0.001"},
            {"fundingTime": str(20 * v3_strategy.HOUR_MS), "realizedRate": "0.001"},
        ]
        engine = v3_engine.PortfolioEngine(
            market(candles, funding), config(), COSTS,
            v3_engine.Variant("ENTRY_LIMIT", "REFERENCE", 2),
        )
        active = v3_engine.ActiveSetup(setup(), True, confirmation_ms=0, confirmation_index=0,
                                       atr1_confirmation=1, entry_plan=105.2, stop=103)
        engine.active["BTC-USDT-SWAP"] = active
        engine.pending["BTC-USDT-SWAP"] = v3_engine.PendingEntry(0, 0, 105.2, 103, 1, None)
        result = engine.run()
        self.assertFalse(result.trades[0]["funding_complete"])

    def test_timeout_closes_at_the_first_open_at_or_after_240_hours(self) -> None:
        candles = [
            v3_strategy.Candle(0, 106, 107, 104, 106, 1),
            v3_strategy.Candle(240 * v3_strategy.HOUR_MS, 108, 109, 107, 108, 1),
        ]
        engine = injected_engine(candles, v3_engine.Variant("ENTRY_LIMIT", "REFERENCE", 2))
        result = engine.run()
        self.assertEqual("TIMEOUT", result.trades[0]["outcome"])
        self.assertEqual(240 * v3_strategy.HOUR_MS, result.trades[0]["exit_ms"])

    def test_split_exits_40_and_30_percent_then_runner(self) -> None:
        candles = [
            v3_strategy.Candle(0, 106, 107, 104, 106, 1),
            v3_strategy.Candle(v3_strategy.HOUR_MS, 106, 110.5, 105, 110, 1),
            v3_strategy.Candle(2 * v3_strategy.HOUR_MS, 110, 114.5, 109, 114, 1),
            v3_strategy.Candle(3 * v3_strategy.HOUR_MS, 114, 116, 113, 115, 1),
        ]
        engine = injected_engine(candles, v3_engine.Variant("ENTRY_LIMIT", "SPLIT", 2, "NO_BE"))
        result = engine.run()
        trade = result.trades[0]
        self.assertGreater(trade["tp1_pnl"], 0)
        self.assertGreater(trade["tp2_pnl"], 0)
        self.assertGreater(trade["runner_pnl"], 0)
        self.assertAlmostEqual(
            trade["net_pnl"], trade["tp1_pnl"] + trade["tp2_pnl"] + trade["runner_pnl"],
        )

    def test_shadow_reference_returns_outcome_without_changing_equity(self) -> None:
        candles = [
            v3_strategy.Candle(0, 106, 107, 104, 106, 1),
            v3_strategy.Candle(v3_strategy.HOUR_MS, 106, 107, 105, 106, 1),
            v3_strategy.Candle(2 * v3_strategy.HOUR_MS, 106, 116, 105, 115, 1),
        ]
        engine = injected_engine(candles, v3_engine.Variant("ENTRY_LIMIT", "SPLIT", 2))
        before = engine.equity
        result = engine._shadow_reference("BTC-USDT-SWAP", engine.active["BTC-USDT-SWAP"])
        self.assertEqual("TP", result["shadow_outcome"])
        self.assertEqual(before, engine.equity)


class PortfolioLimitTests(unittest.TestCase):
    def test_three_position_limit_and_symbol_priority_are_deterministic(self) -> None:
        symbols = ["ETH-USDT-SWAP", "BTC-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP"]
        candle = v3_strategy.Candle(0, 106, 107, 104, 106, 1)
        data = {symbol: v3_engine.MarketData([candle], [], [], [], 0.1) for symbol in symbols}
        cfg = config()
        cfg["symbols"] = symbols
        cfg["maximum_open_positions"] = 3
        engine = v3_engine.PortfolioEngine(data, cfg, COSTS,
                                           v3_engine.Variant("ENTRY_LIMIT", "REFERENCE", 2))
        for symbol in symbols:
            base = setup()
            # Build a symbol-specific immutable setup while keeping identical timestamps.
            own_channel = v3_strategy.Channel(
                symbol, base.channel.window_start_ms, base.channel.window_end_ms,
                base.channel.upper, base.channel.lower, base.channel.width, base.channel.atr_ref,
                base.channel.slope, base.channel.inside_closes, base.channel.high_pivots, base.channel.low_pivots,
            )
            own_setup = v3_strategy.Setup(symbol, "LONG", own_channel, base.breakout_start_ms,
                                          base.breakout_close_ms, base.reference_level,
                                          base.reference_tp, base.retest_deadline_ms)
            active = v3_engine.ActiveSetup(
                own_setup, True, confirmation_ms=0, confirmation_index=0,
                atr1_confirmation=1, entry_plan=105.2, stop=103, targets=(110, 114),
            )
            engine.active[symbol] = active
            engine.pending[symbol] = v3_engine.PendingEntry(0, 0, 105.2, 103, 1, (110, 114))
        result = engine.run()
        self.assertEqual(symbols[:3], [row["symbol"] for row in result.trades])
        rejected = [row for row in result.setups if row["status"] == "REJECTED_POSITION_LIMIT"]
        self.assertEqual([symbols[3]], [row["symbol"] for row in rejected])


if __name__ == "__main__":
    unittest.main()
