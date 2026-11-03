from __future__ import annotations

import unittest

from backtest import v3_strategy as v3


def candle(index: int, o: float = 100, h: float = 101, l: float = 99, c: float = 100,
           step: int = v3.FOUR_HOURS_MS) -> v3.Candle:
    return v3.Candle(index * step, o, h, l, c, 10.0)


def flat_channel(symbol: str = "BTC-USDT-SWAP") -> tuple[list[v3.Candle], list[v3.Pivot], list[float | None]]:
    candles = [candle(index) for index in range(61)]
    pivots = [
        v3.Pivot("HIGH", 10, candles[10].start_ms, candles[12].start_ms + v3.FOUR_HOURS_MS, 105.0),
        v3.Pivot("HIGH", 30, candles[30].start_ms, candles[32].start_ms + v3.FOUR_HOURS_MS, 105.1),
        v3.Pivot("LOW", 20, candles[20].start_ms, candles[22].start_ms + v3.FOUR_HOURS_MS, 95.0),
        v3.Pivot("LOW", 40, candles[40].start_ms, candles[42].start_ms + v3.FOUR_HOURS_MS, 95.1),
    ]
    atr = [2.0] * len(candles)
    return candles, pivots, atr


class PivotTests(unittest.TestCase):
    def test_pivot_is_unavailable_until_both_right_candles_close(self) -> None:
        rows = [candle(i, h=110 if i == 2 else 101) for i in range(5)]
        self.assertEqual([], v3.confirmed_pivots(rows[:4], 2))
        pivots = v3.confirmed_pivots(rows, 2)
        self.assertEqual(1, len(pivots))
        self.assertEqual(rows[4].start_ms + v3.FOUR_HOURS_MS, pivots[0].confirmed_ms)

    def test_equal_high_is_not_a_pivot(self) -> None:
        rows = [candle(i, h=110 if i in (2, 3) else 101) for i in range(6)]
        self.assertFalse(any(pivot.start_ms == rows[2].start_ms for pivot in v3.confirmed_pivots(rows, 2)))


class ChannelTests(unittest.TestCase):
    def test_channel_uses_only_candles_before_breakout_and_freezes_values(self) -> None:
        candles, pivots, atr = flat_channel()
        params = v3.Parameters()
        first = v3.channel_before("BTC-USDT-SWAP", candles, 60, params, atr, pivots)
        self.assertIsNotNone(first)
        candles[60] = candle(60, o=100, h=500, l=1, c=490)
        second = v3.channel_before("BTC-USDT-SWAP", candles, 60, params, atr, pivots)
        self.assertEqual(first, second)
        self.assertAlmostEqual(105.05, first.upper)
        self.assertAlmostEqual(95.05, first.lower)

    def test_breakout_retest_confirmation_and_stop_are_symmetric(self) -> None:
        candles, pivots, atr = flat_channel()
        params = v3.Parameters()
        channel = v3.channel_before("BTC-USDT-SWAP", candles, 60, params, atr, pivots)
        breakout = v3.Candle(candles[60].start_ms, 105, 108, 104, 106.1, 10)
        setup = v3.breakout_setup(channel, breakout, params)
        self.assertEqual("LONG", setup.direction)
        retest = v3.Candle(setup.breakout_close_ms, 106, 106, 104.8, 105.2, 1)
        confirm = v3.Candle(retest.start_ms + v3.HOUR_MS, 105.2, 107, 105, 106.5, 1)
        self.assertTrue(v3.touches_retest(setup, retest, params))
        self.assertTrue(v3.confirms(setup, retest, confirm))
        entry = v3.planned_entry(setup, params)
        stop, distance, wide = v3.initial_stop(setup, entry, 1.0, params)
        self.assertFalse(wide)
        self.assertGreater(distance, 0)
        self.assertGreaterEqual(v3.gross_rr(entry, stop, setup.reference_tp), 3.5454)

    def test_gap_resets_atr_warmup(self) -> None:
        rows = [candle(i, step=v3.HOUR_MS) for i in range(15)]
        rows[10] = v3.Candle(rows[9].start_ms + 2 * v3.HOUR_MS, 100, 101, 99, 100, 1)
        values = v3.wilder_atr(rows, 14, v3.HOUR_MS)
        self.assertIsNone(values[-1])


class TargetAndDailyTests(unittest.TestCase):
    def test_repeated_close_beyond_the_same_structure_level_emits_one_choch(self) -> None:
        rows = [
            candle(0, c=94),
            candle(1, c=93),
        ]
        pivots = [
            v3.Pivot("HIGH", 0, -4, 0, 100),
            v3.Pivot("HIGH", 1, -3, 0, 110),
            v3.Pivot("LOW", 0, -2, 0, 90),
            v3.Pivot("LOW", 1, -1, 0, 95),
        ]
        events = v3.choch_events(rows, pivots)
        self.assertEqual(1, len(events))
        self.assertEqual("BEARISH", events[0].direction)

    def test_structural_targets_are_known_only_after_confirmation(self) -> None:
        candles, pivots, atr = flat_channel()
        channel = v3.channel_before("BTC-USDT-SWAP", candles, 60, v3.Parameters(), atr, pivots)
        setup = v3.breakout_setup(channel, v3.Candle(candles[60].start_ms, 105, 108, 104, 106.1, 1), v3.Parameters())
        future = [
            v3.Pivot("HIGH", 50, 0, 1_000, 108),
            v3.Pivot("HIGH", 51, 0, 2_000, 112),
            v3.Pivot("HIGH", 52, 0, 4_000, 115),
        ]
        self.assertEqual((108, 112), v3.structural_targets(setup, 106, future, 3_000, 1080, 0.1))

    def test_daily_context_is_na_until_both_emas_exist(self) -> None:
        daily = [candle(i, step=v3.DAY_MS) for i in range(199)]
        context = v3.daily_context(daily, daily[-1].start_ms + v3.DAY_MS)
        self.assertTrue(all(value == "NA" for value in context.values()))


if __name__ == "__main__":
    unittest.main()
