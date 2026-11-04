import unittest
from backtest import v3_universe_select as u


class UniverseSelectionTests(unittest.TestCase):
    def snapshot(self):
        instruments, tickers = [], []
        for name, volume, price in [('AAA', '100', '1'), ('BBB', '10', '20'), ('CCC','100','1'),
                                    ('BTC','9999','100'), ('USDC','9999','1'), ('XAU','9999','100')]:
            symbol = name+'-USDT-SWAP'
            instruments.append(dict(instId=symbol, instType='SWAP', ctType='linear', settleCcy='USDT',
                                    state='live', listTime=str(u.CUTOFF), instCategory='4' if name=='XAU' else '1',tickSz='0.01'))
            tickers.append(dict(instId=symbol, volCcy24h=volume, last=price, ts='1'))
        return dict(snapshot_utc='2026-11-04T00:00:00Z',instruments=instruments,tickers=tickers)

    def test_notional_volume_order_and_tie_break(self):
        result = u.select(self.snapshot(),True,3)
        self.assertEqual(['BBB-USDT-SWAP','AAA-USDT-SWAP','CCC-USDT-SWAP'],[r['symbol'] for r in result['selected']])

    def test_previous_stable_and_noncrypto_excluded(self):
        result = u.select(self.snapshot(),True,3)
        reasons = {r['symbol']:r['reasons'] for r in result['excluded']}
        self.assertIn('PREVIOUS_UNIVERSE', reasons['BTC-USDT-SWAP'])
        self.assertIn('STABLECOIN_OR_FIAT_LINKED', reasons['USDC-USDT-SWAP'])
        self.assertIn('NON_CRYPTO_OR_UNKNOWN_CATEGORY', reasons['XAU-USDT-SWAP'])

    def test_boundary_is_inclusive_and_newer_listing_excluded(self):
        snapshot=self.snapshot(); snapshot['instruments'][0]['listTime']=str(u.CUTOFF+1)
        result=u.select(snapshot,True,2)
        self.assertNotIn('AAA-USDT-SWAP',[r['symbol'] for r in result['selected']])

    def test_insufficient_candidates_stops_without_replacement(self):
        with self.assertRaises(ValueError):u.select(self.snapshot(),True,40)

    def test_invalid_volume_is_not_ranked(self):
        snapshot=self.snapshot(); snapshot['tickers'][0]['volCcy24h']='NaN'
        result=u.select(snapshot,True,2)
        self.assertNotIn('AAA-USDT-SWAP',[r['symbol'] for r in result['selected']])

    def test_duplicate_tickers_are_rejected(self):
        snapshot=self.snapshot();snapshot['tickers'].append(snapshot['tickers'][0])
        with self.assertRaises(ValueError):u.select(snapshot,True,2)


if __name__=='__main__':unittest.main()
