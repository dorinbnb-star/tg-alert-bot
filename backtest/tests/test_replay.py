from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from ai_crypto_monitor import entry_monitor as em  # noqa: E402
from backtest import simulate  # noqa: E402
from backtest.replay import LookAheadError, ReplayOkxClient, parse_rows  # noqa: E402
from backtest.tests import synthetic  # noqa: E402

CONFIG = dict(json.loads((PROJECT_ROOT / "ai_crypto_monitor" / "config-v0.1.json").read_text()), symbol="BTC-USDT-SWAP")


class ReplayWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.series, cls.minute = synthetic.market(seed=3, days=45)

    def test_live_windows_are_239_159_159_closed_candles(self) -> None:
        client = ReplayOkxClient(self.series, self.minute)
        for scan_ms in simulate.scan_grid(synthetic.END_MS - synthetic.DAY, synthetic.END_MS)[:40]:
            now, market = em.fetch_closed_market(client.at(scan_ms), CONFIG)
            self.assertEqual(scan_ms, now)
            self.assertEqual([239, 159, 159], [len(market[k]) for k in ("context", "structure", "trigger")])
            for key, interval in (("context", "240"), ("structure", "60"), ("trigger", "15")):
                step = em.interval_ms(interval)
                last = market[key][-1]
                self.assertLessEqual(last.start_ms + step, scan_ms)
                self.assertGreater(last.start_ms + 2 * step, scan_ms, "ultima lumanare inchisa trebuie sa fie cea mai noua")

    def test_klines_returns_limit_minus_open_candle(self) -> None:
        client = ReplayOkxClient(self.series, self.minute)
        scan_ms = synthetic.END_MS - 3 * 3_600_000 + 45_000
        candles = client.at(scan_ms).klines("BTC-USDT-SWAP", "15", 160)
        self.assertEqual(159, len(candles))
        self.assertTrue(all(c.start_ms + 900_000 <= scan_ms for c in candles))

    def test_candle_ending_after_t_inside_the_window_is_rejected(self) -> None:
        scan_ms = synthetic.END_MS - 3_600_000
        bad = [em.Candle(scan_ms - 14 * 60_000, 1, 1, 1, 1, 1), em.Candle(scan_ms - 60_000, 1, 1, 1, 1, 1)]
        client = ReplayOkxClient({"15": bad})
        with self.assertRaises(LookAheadError):
            client.at(scan_ms).klines("BTC-USDT-SWAP", "15", 160)

    def test_last_price_is_the_open_of_the_minute_containing_t(self) -> None:
        client = ReplayOkxClient(self.series, self.minute)
        scan_ms = synthetic.END_MS - 3_600_000 + 45_000
        containing = next(c for c in self.minute if c.start_ms <= scan_ms < c.start_ms + 60_000)
        self.assertEqual(containing.open, client.at(scan_ms).last_price("BTC-USDT-SWAP"))
        with self.assertRaises(em.MonitorError):
            ReplayOkxClient(self.series, []).at(scan_ms).last_price("BTC-USDT-SWAP")

    def test_stored_rows_go_through_the_live_parser(self) -> None:
        candles = self.series["15"][:5]
        rows = synthetic.rows(candles)
        rows[-1][8] = "0"
        parsed = parse_rows(rows, "15")
        self.assertEqual(candles[:4], parsed)


if __name__ == "__main__":
    unittest.main()
