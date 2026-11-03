import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import replace

from backtest import v3_strategy as s, v3_engine as e, v3_run
from backtest.tests.test_v3_pipeline import synthetic_market, COSTS


class BreakoutStateTests(unittest.TestCase):
    def setUp(self):
        self.channel = s.Channel('TEST', 0, 60*s.FOUR_HOURS_MS, 110, 90, 20, 10, 0, 60, (), ())
        self.params = s.Parameters()

    def step(self, state, index, close, previous=100, channel=True):
        candle = s.Candle(index*s.FOUR_HOURS_MS, close, close, close, close)
        return s.advance_breakout(state, self.channel if channel else None, previous, candle, self.params)

    def test_previous_close_outside_cannot_arm(self):
        for previous, close in [(111, 116), (89, 84)]:
            state, setup, events = self.step(s.BreakoutState(), 60, close, previous)
            self.assertIsNone(setup)
            self.assertEqual([], events)

    def test_wif_august_2025_regression(self):
        self.channel = replace(self.channel, symbol='WIF-USDT-SWAP', upper=1.0386,
                               lower=.8942, width=.1444, atr_ref=.0337340806461268)
        _, setup, events = self.step(s.BreakoutState(), 60, .8677, previous=.8811)
        self.assertIsNone(setup)
        self.assertEqual([], events)

    def test_small_exit_then_threshold_freezes_channel(self):
        state, setup, _ = self.step(s.BreakoutState(), 60, 111)
        self.assertIsNone(setup)
        state, setup, events = self.step(state, 61, 115, previous=111, channel=False)
        self.assertEqual(self.channel, setup.channel)
        self.assertEqual(62*s.FOUR_HOURS_MS, setup.breakout_close_ms)
        self.assertEqual('BREAKOUT', events[-1]['status'])

    def test_return_inclusive_and_rearm(self):
        for close in [90, 100, 110]:
            state, _, _ = self.step(s.BreakoutState(), 60, 111)
            state, setup, events = self.step(state, 61, close)
            self.assertEqual('ARMED', state.phase)
            self.assertIsNone(setup)
            self.assertEqual('EXIT_RETURNED', events[-1]['status'])

    def test_third_bar_expires_and_requires_inside_close(self):
        state = s.BreakoutState()
        for i in [60, 61, 62]:
            state, setup, events = self.step(state, i, 111)
        self.assertEqual('EXIT_NO_BREAKOUT', events[-1]['status'])
        self.assertEqual('BLOCKED', state.phase)
        state, setup, _ = self.step(state, 63, 116)
        self.assertIsNone(setup)
        state, _, _ = self.step(state, 64, 100)
        _, setup, _ = self.step(state, 65, 116)
        self.assertIsNotNone(setup)

    def test_threshold_on_third_bar_wins_over_expiration(self):
        state = s.BreakoutState()
        for i, close in [(60, 111), (61, 112), (62, 115)]:
            state, setup, events = self.step(state, i, close)
        self.assertIsNotNone(setup)
        self.assertEqual(['BREAKOUT'], [r['status'] for r in events])

    def test_opposite_exit_resets_count_preserving_channel(self):
        state, _, _ = self.step(s.BreakoutState(), 60, 111)
        state, _, _ = self.step(state, 61, 112)
        state, setup, events = self.step(state, 62, 89, channel=False)
        self.assertEqual(1, state.bars)
        self.assertEqual('SHORT', state.direction)
        self.assertEqual(self.channel, state.channel)
        _, setup, _ = self.step(state, 63, 85, channel=False)
        self.assertEqual('SHORT', setup.direction)

    def test_long_short_symmetry_and_immediate_breakout(self):
        for close, direction in [(115, 'LONG'), (85, 'SHORT')]:
            _, setup, events = self.step(s.BreakoutState(), 60, close)
            self.assertEqual(direction, setup.direction)
            self.assertEqual(['ARMED', 'EXITED', 'BREAKOUT'], [r['status'] for r in events])

    def test_gap_resets_frozen_exit(self):
        state, _, _ = self.step(s.BreakoutState(), 60, 111)
        _, setup, events = self.step(state, 63, 116, previous=111)
        self.assertIsNone(setup)
        self.assertEqual([], events)

    def test_armed_count_is_not_per_candle(self):
        state = s.BreakoutState()
        statuses = []
        for i in range(60, 65):
            state, _, events = self.step(state, i, 100)
            statuses.extend(r['status'] for r in events)
        self.assertEqual(['ARMED'], statuses)

    def test_development_clips_before_engine_and_preserves_boundary(self):
        market = synthetic_market()
        config = dict(e.load_config(), symbols=['BTC-USDT-SWAP'])
        end = 65*s.FOUR_HOURS_MS
        original = e.prepare_market
        def checked(data, cfg):
            self.assertTrue(all(c.start_ms+s.HOUR_MS <= end for d in data.values() for c in d.h1))
            self.assertTrue(all(c.start_ms+s.FOUR_HOURS_MS <= end for d in data.values() for c in d.h4))
            return original(data, cfg)
        with tempfile.TemporaryDirectory() as tmp, patch.object(e, 'prepare_market', side_effect=checked):
            v3_run.development_only(market, config, COSTS, {'development_end_ms': end}, Path(tmp))
            payload = json.loads((Path(tmp)/'v3-metrics.json').read_text())
            self.assertEqual('development_only', payload['scope'])
            self.assertEqual(end, payload['original_bounds']['development_end_ms'])

    def test_engine_keeps_frozen_channel_when_next_candidate_missing(self):
        market = synthetic_market()
        data = market['BTC-USDT-SWAP']
        data.h4[60] = replace(data.h4[60], close=105.1)
        data.h4[61] = replace(data.h4[61], close=106)
        config = dict(e.load_config(), symbols=['BTC-USDT-SWAP'])
        prepared = e.prepare_market(market, config)
        prepared['BTC-USDT-SWAP'].channels.pop(61, None)
        engine = e.PortfolioEngine(market, config, COSTS,
                                  e.Variant('ENTRY_MARKET_NEXT_OPEN', 'REFERENCE', 2), prepared)
        engine._maybe_breakout('BTC-USDT-SWAP', 60)
        self.assertFalse(engine.active)
        frozen = engine.breakout_states['BTC-USDT-SWAP'].channel
        engine._maybe_breakout('BTC-USDT-SWAP', 61)
        self.assertEqual(frozen, engine.active['BTC-USDT-SWAP'].setup.channel)
        self.assertEqual(['ARMED', 'EXITED', 'BREAKOUT'], [r['status'] for r in engine.transitions])

    def test_changing_candidate_does_not_change_frozen_geometry(self):
        state, _, _ = self.step(s.BreakoutState(), 60, 111)
        other = replace(self.channel, upper=120, lower=80, width=40)
        candle = s.Candle(61*s.FOUR_HOURS_MS, 115, 115, 115, 115)
        _, setup, _ = s.advance_breakout(state, other, 111, candle, self.params)
        self.assertEqual(self.channel, setup.channel)


if __name__ == '__main__':
    unittest.main()
