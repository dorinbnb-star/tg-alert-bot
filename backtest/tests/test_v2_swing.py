from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json
import unittest

from backtest import v2_swing as v2
from backtest.v2_run import verdict
from backtest.v3_strategy import Pivot

H = v2.H
SYMBOL = "BTC-USDT-SWAP"
COSTS = json.loads(Path(v2.__file__).with_name("costs.json").read_text())


def candle(i, o=101, h=102, l=100.5, c=101):
    return v2.Candle(i * H, o, h, l, c)


def market(bars, pivot_confirmed=16 * H):
    pivots = [Pivot("LOW", 2, 8 * H, pivot_confirmed, 100),
              Pivot("HIGH", 3, 12 * H, 20 * H, 112)]
    pivots.sort(key=lambda pivot: pivot.confirmed_ms)
    return SimpleNamespace(data=SimpleNamespace(h1=bars, h4=[candle(i) for i in range(0, 32, 4)],
                                                tick_size=.01),
                           starts=[bar.start_ms for bar in bars], atr=[2.] * len(bars),
                           pivots=pivots, pivot_times=[p.confirmed_ms for p in pivots],
                           rate_times=[i * H for i in range(0, 48, 4)],
                           rates=[(i * H, .001) for i in range(0, 48, 4)],
                           trend=lambda _: 1, daily_context=lambda _: {})


class SignalTests(unittest.TestCase):
    def test_same_bar_reclaim_separate_confirmation_and_pivot_consumption(self):
        bars = [candle(i) for i in range(32)]
        bars[20] = candle(20, 101, 102, 99, 101)
        bars[21] = candle(21, 101, 104, 100, 103)
        bars[24] = candle(24, 101, 102, 99, 101)
        bars[25] = candle(25, 101, 104, 100, 103)
        p = market(bars)
        found, events = v2.signals(SYMBOL, p, 20*H, 32*H)
        self.assertEqual([22*H], [s.time for s in found])
        self.assertEqual((112,), found[0].targets)
        self.assertEqual(["SWEEP", "RECLAIM", "CONFIRMED"], [stage for _, stage in events])
        self.assertEqual([], v2.signals(SYMBOL, market(bars[:24], 21*H), 20*H, 24*H)[0])

    def test_deep_sweep_and_late_confirmation_do_not_create_signal(self):
        bars = [candle(i) for i in range(32)]
        bars[20] = candle(20, 101, 102, 97, 101)
        bars[21] = candle(21, 101, 104, 100, 103)
        self.assertEqual([], v2.signals(SYMBOL, market(bars), 20*H, 32*H)[0])
        bars[20] = candle(20, 101, 102, 99, 101)
        bars[21] = candle(21)
        bars[24] = candle(24, 101, 104, 100, 103)
        self.assertEqual([], v2.signals(SYMBOL, market(bars), 20*H, 32*H)[0])

    def test_atr_floor_and_second_pivot(self):
        signal = v2.Signal(SYMBOL, 1, 8*H, 100, 99.95, 2, 22*H, (104, 112))
        self.assertEqual((None, "RR_REJECTED"), v2.order_for(signal, .01, v2.Variant(1, False, 2)))
        order, _ = v2.order_for(signal, .01, v2.Variant(2, False, 2))
        self.assertEqual((100, 97, 112), (order.entry, order.stop, order.target))
        self.assertEqual(20, len({variant.key for variant in v2.variants()}))


class ExecutionTests(unittest.TestCase):
    def replay(self, bars, split=False, end=32*H):
        s = v2.Signal(SYMBOL, 1, 8*H, 100, 99, 2, 22*H, (112,))
        return v2.replay({SYMBOL: market(bars)}, {SYMBOL: [s]}, 20*H, end,
                         v2.Variant(1, split, 0), COSTS)

    def test_no_retroactive_fill_and_stop_wins_after_fill(self):
        bars = [candle(i) for i in range(32)]
        bars[21] = candle(21, 101, 103, 99, 102)
        bars[22] = candle(22, 101, 103, 96, 100)
        trades, _ = self.replay(bars)
        self.assertEqual([(22*H, "SL")], [(row["entry_ms"], row["outcome"]) for row in trades])

    def test_partial_exit_updates_stop_on_next_bar(self):
        bars = [candle(i) for i in range(32)]
        bars[22] = candle(22, 100, 107, 99, 106)
        bars[23] = candle(23, 104, 105, 99, 101)
        trades, _ = self.replay(bars, split=True)
        self.assertEqual("SL", trades[0]["outcome"])
        self.assertEqual(24*H, trades[0]["exit_ms"])
        self.assertGreater(trades[0]["tp1_net_r"], 0)
        self.assertGreater(trades[0]["net_r"], 0)

    def test_partial_target_cannot_be_claimed_before_intrabar_fill(self):
        bars = [candle(i) for i in range(32)]
        bars[22] = candle(22, 101, 107, 99, 106)
        trades, events = self.replay(bars, split=True)
        self.assertEqual([], trades)
        self.assertEqual("TARGET_BEFORE_ENTRY", events[-1]["status"])

    def test_boundary_censors_position_without_using_later_bar(self):
        bars = [candle(i) for i in range(32)]
        bars[22] = candle(22, 101, 104, 99, 102)
        bars[23] = candle(23, 102, 114, 101, 112)
        trades, events = self.replay(bars, end=23*H)
        self.assertEqual([], trades)
        self.assertEqual("CENSORED", events[-1]["status"])

    def test_verdict_strict_percentile_and_sample_floor(self):
        metrics = dict(n=29, mean_r=.15, profit_factor=1.2, mean_r_cost_x2=.01)
        self.assertEqual("NECONCLUDENT", verdict(metrics, .12))
        metrics["n"] = 30
        self.assertEqual("PASS", verdict(metrics, .12))
        self.assertEqual("FAIL", verdict(metrics, .15))
        self.assertEqual("FAIL", verdict(metrics, None))
