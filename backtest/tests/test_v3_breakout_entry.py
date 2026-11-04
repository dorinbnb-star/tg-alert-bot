from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backtest import v3_engine as e, v3_strategy as s, v3_run
from backtest.tests.test_v3_engine import injected_engine
from backtest.tests.test_v3_pipeline import synthetic_market, COSTS


class BreakoutEntryTests(unittest.TestCase):
    def engine(self, opening=105, rr=2):
        candle = s.Candle(0, opening, opening+1, opening-1, opening, 1)
        return injected_engine([candle], e.Variant('ENTRY_BREAKOUT_NEXT_OPEN', 'REFERENCE', rr)), candle

    def test_open_only_no_future_ohlc(self):
        engine, candle = self.engine()
        first = engine._market_candidate('BTC-USDT-SWAP', candle)
        second = engine._market_candidate('BTC-USDT-SWAP', replace(candle, high=1000, low=1, close=2))
        self.assertIsNotNone(first)
        self.assertEqual(first, second)

    def test_rejection_priority(self):
        cases = [(116, 117, True, 'TARGET_AT_OPEN'),
                 (103, 104, True, 'SL_AT_OPEN'),
                 (103.5, 102, True, 'INVALID_AT_OPEN'),
                 (105, 100, True, 'SL_TOO_WIDE'),
                 (105, 100, False, 'RR_REJECTED')]
        for opening, stop, wide, expected in cases:
            with self.subTest(expected=expected):
                engine, candle = self.engine(opening, rr=3)
                with patch.object(s, 'initial_stop', return_value=(stop, abs(opening-stop), wide)):
                    self.assertIsNone(engine._market_candidate('BTC-USDT-SWAP', candle))
                self.assertEqual(expected, engine.setups[-1]['status'])
                self.assertFalse(engine.trades)
                self.assertFalse(engine.pending)

    def test_stop_and_rr_receive_price_with_slippage(self):
        engine, candle = self.engine()
        with patch.object(s, 'initial_stop', wraps=s.initial_stop) as stop, patch.object(s, 'gross_rr', wraps=s.gross_rr) as rr:
            candidate = engine._market_candidate('BTC-USDT-SWAP', candle)
        actual = candle.open*(1+float(COSTS['slippage_rate']['BTC-USDT-SWAP']))
        self.assertEqual(actual, candidate[4])
        self.assertEqual(actual, stop.call_args.args[1])
        self.assertEqual(actual, rr.call_args.args[0])

    def test_long_short_symmetric(self):
        for direction, opening in [('LONG', 105), ('SHORT', 95)]:
            engine, candle = self.engine(opening)
            active = engine.active['BTC-USDT-SWAP']
            active.setup = replace(active.setup, direction=direction,
                                   reference_level=105 if direction=='LONG' else 95,
                                   reference_tp=115 if direction=='LONG' else 85)
            candidate = engine._market_candidate('BTC-USDT-SWAP', candle)
            self.assertIsNotNone(candidate)
            self.assertTrue(candidate[4]>opening if direction=='LONG' else candidate[4]<opening)

    def test_slippage_alone_can_make_stop_too_wide(self):
        engine, candle = self.engine(109)
        active = engine.active['BTC-USDT-SWAP']
        self.assertFalse(s.initial_stop(active.setup, candle.open, 1, engine.params)[2])
        self.assertIsNone(engine._market_candidate('BTC-USDT-SWAP', candle))
        self.assertEqual('SL_TOO_WIDE', engine.setups[-1]['status'])

    def test_rr_threshold_is_checked_after_slippage(self):
        engine, candle = self.engine(107)
        self.assertEqual(2, s.gross_rr(candle.open, 103, 115))
        self.assertIsNone(engine._market_candidate('BTC-USDT-SWAP', candle))
        self.assertEqual('RR_REJECTED', engine.setups[-1]['status'])

    def test_position_limit_after_open_checks(self):
        engine, _ = self.engine()
        engine.config['maximum_open_positions'] = 0
        result = engine.run()
        self.assertFalse(result.trades)
        self.assertEqual('REJECTED_POSITION_LIMIT', result.setups[-1]['status'])

    def test_missing_first_hour_does_not_enter_later(self):
        engine, candle = self.engine()
        self.assertIsNone(engine._market_candidate('BTC-USDT-SWAP', replace(candle, start_ms=s.HOUR_MS)))
        self.assertEqual('DATA_MISSING', engine.setups[-1]['status'])

    def test_pipeline_never_calls_retest_or_confirmation(self):
        market = synthetic_market()
        config = dict(e.load_config(), symbols=['BTC-USDT-SWAP'])
        prepared = e.prepare_market(market, config)
        with patch.object(s, 'touches_retest', side_effect=AssertionError('retest called')), \
             patch.object(e.PortfolioEngine, '_confirm', side_effect=AssertionError('confirmation called')):
            result = e.run_variant(market, config, COSTS,
                                   e.Variant('ENTRY_BREAKOUT_NEXT_OPEN','REFERENCE',2), prepared)
        self.assertTrue(result.trades)
        trade = result.trades[0]
        self.assertEqual(61*s.FOUR_HOURS_MS, trade['entry_ms'])
        self.assertEqual(trade['entry_ms'], trade['signal_ms'])
        self.assertIsNone(trade['confirmation_ms'])
        pending = next(r for r in result.setups if r['status']=='MARKET_PENDING')
        self.assertEqual(prepared['BTC-USDT-SWAP'].atr1[61*4-1], pending['atr1_at_signal'])
        self.assertFalse(any(r['status'] in ('RETESTED','CONFIRMED') for r in result.setups))

    def test_comparison_pairs_exact_channels_and_exclusive_lists(self):
        market = synthetic_market(); config = dict(e.load_config(), symbols=['BTC-USDT-SWAP'])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            v3_run.development_only(market,config,COSTS,{'development_end_ms':65*s.FOUR_HOURS_MS},path,True)
            report = json.loads((path/'v3-entry-comparison.json').read_text())
            self.assertEqual(4,len(report['variants']))
            for row in report['common']:
                self.assertAlmostEqual(row['breakout_net_r']-row['retest_net_r'],row['difference_r'])
            self.assertTrue((path/'v3-exclusive-channels.csv').exists())


if __name__ == '__main__':
    unittest.main()
