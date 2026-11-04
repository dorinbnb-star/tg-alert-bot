import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backtest import v3_universe_run as runner
from backtest.tests.test_v3_pipeline import synthetic_market, COSTS


class UniverseRunTests(unittest.TestCase):
    def seeds(self, value=0):
        return [dict(seed=i, mean_r=value) for i in range(200)]

    def metrics(self, **changes):
        return dict(dict(n=60, mean_r=.15, profit_factor=1.2, mean_r_cost_x2=.001), **changes)

    def test_all_five_boundaries_pass(self):
        rows, baseline = runner.criteria(self.metrics(), self.seeds())
        self.assertTrue(all(r['verdict']=='PASS' for r in rows))
        self.assertEqual(100, baseline['strict_percentile'])

    def test_each_numeric_criterion_fails_independently(self):
        for index, field, value in [(0,'n',59),(1,'mean_r',.1499),(2,'profit_factor',1.199),(3,'mean_r_cost_x2',0)]:
            rows, _ = runner.criteria(self.metrics(**{field:value}), self.seeds())
            self.assertEqual('FAIL', rows[index]['verdict'])
            self.assertEqual(1, sum(r['verdict']=='FAIL' for r in rows))

    def test_percentile_ties_and_strict_90_boundary(self):
        seeds = self.seeds(.15)
        for row in seeds[:180]: row['mean_r'] = 0
        rows, baseline = runner.criteria(self.metrics(), seeds)
        self.assertEqual(90, baseline['strict_percentile'])
        self.assertEqual('FAIL', rows[-1]['verdict'])
        seeds[180]['mean_r'] = 0
        self.assertEqual('PASS', runner.criteria(self.metrics(), seeds)[0][-1]['verdict'])

    def test_missing_baseline_mean_fails(self):
        seeds = self.seeds()
        seeds[19]['mean_r'] = None
        rows, baseline = runner.criteria(self.metrics(), seeds)
        self.assertEqual(199, baseline['valid_seeds'])
        self.assertIsNone(baseline['strict_percentile'])
        self.assertEqual('FAIL', rows[-1]['verdict'])

    def test_missing_duplicate_or_outside_seed_rejected(self):
        for seeds in [self.seeds()[:-1], self.seeds()+[dict(seed=200,mean_r=0)], [dict(seed=0,mean_r=0)]*200]:
            with self.assertRaises(ValueError): runner.criteria(self.metrics(), seeds)

    def test_no_trades_cannot_pass(self):
        rows, _ = runner.criteria(self.metrics(n=0,mean_r=None,profit_factor=None,mean_r_cost_x2=None),self.seeds())
        self.assertTrue(all(r['verdict']=='FAIL' for r in rows))

    def test_lock_is_exclusive_and_preserves_first_run(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'lock.json'
            runner.acquire_run_lock(path, {'run':1})
            with self.assertRaises(FileExistsError): runner.acquire_run_lock(path, {'run':2})
            self.assertEqual({'run':1},json.loads(path.read_text()))

    def test_data_hashes_checked_and_missing_reported_without_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            chosen = {'selected':[{'symbol':'NEW-USDT-SWAP'}]}
            manifest = {'NEW-USDT-SWAP':{bar:{'status':'MISSING'} for bar in runner.BARS}}
            self.assertEqual(4,len(runner.verify_data(chosen,manifest,root)))
            path = runner.data.series_path(root,'NEW-USDT-SWAP','4H')
            digest = runner.data.write_rows(path,[])
            manifest['NEW-USDT-SWAP']['4H'] = {'status':'EMPTY','sha256':digest}
            self.assertEqual(4,len(runner.verify_data(chosen,manifest,root)))
            runner.data.write_rows(path,[[1,2,3]])
            with self.assertRaisesRegex(ValueError,'Hash mismatch'):
                runner.verify_data(chosen,manifest,root)

    def test_unfinished_download_stops(self):
        with self.assertRaisesRegex(ValueError,'Download has not completed'):
            runner.verify_data({'selected':[{'symbol':'NEW'}]}, {}, Path('.'))

    def test_baseline_uses_existing_section22_mode(self):
        config = dict(runner.engine.load_config(),symbols=['BTC-USDT-SWAP'])
        market = synthetic_market()
        prepared = runner.engine.prepare_market(market,config)
        runner.init_baseline(market,config,COSTS,prepared)
        row = runner.baseline_seed(17)
        expected = runner.engine.run_variant(market,config,COSTS,runner.BASELINE,prepared,random_seed=17)
        self.assertEqual(dict(seed=17,**runner.report.metrics(expected.trades)),row)
        self.assertEqual('ENTRY_BREAKOUT_NEXT_OPEN__REF_RR2',runner.VARIANT.key)
        self.assertEqual('ENTRY_MARKET_NEXT_OPEN__REF_RR2',runner.BASELINE.key)

    def test_report_artifacts_from_synthetic_results(self):
        config = dict(runner.engine.load_config(),symbols=['BTC-USDT-SWAP'])
        result = runner.engine.run_variant(synthetic_market(),config,COSTS,runner.VARIANT)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner.write_report(result,self.seeds(),{'selected':[{'symbol':'BTC-USDT-SWAP'}], 'snapshot_utc':'synthetic'},
                                {},{'selection_commit':'test','scripts_commit':'test'},[],root)
            payload = json.loads((root/'report.json').read_text())
            self.assertEqual(5,len(payload['criteria']))
            self.assertEqual('FAIL',payload['verdict'])
            self.assertEqual(200,len(payload['baseline_seed_metrics']))
            self.assertIn('DATA_END:',(root/'report.md').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
