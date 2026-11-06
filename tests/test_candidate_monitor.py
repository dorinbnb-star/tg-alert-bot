from dataclasses import replace
import csv
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_crypto_monitor import candidate_monitor as cm
from backtest import v2_swing as swing
from backtest.v3_strategy import Pivot

H = cm.H
SYMBOL = cm.SYMBOLS[1]
C = cm.Candidate(SYMBOL, 1, 8 * H, 20 * H, 22 * H,
                 100, 99, 102, 98, 110, 114, "preț 101.00 < EMA50 / < EMA200", "0.01")


def bar(index, high=105, low=100, close=103):
    return swing.Candle(index * H, close, high, low, close)


class CandidateTests(unittest.TestCase):
    def test_exact_message(self):
        self.assertEqual(
            "🟢 ETH LONG\n\n"
            "Entry teoretic: 102.00\n"
            "Preț verificat: 101.23\n"
            "SL: 98.00\n"
            "TP: 110.00\n"
            "R:R: 2.00\n"
            "Valabil până la: 01:00\n"
            "Status: PENDING",
            cm.candidate_message(C, 101.23),
        )

    def test_persistent_pivot_and_paper_are_each_sent_once(self):
        state, sent = {}, []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.json"

            def persist():
                cm.monitor.write_json(state_path, state)

            cm.update(SYMBOL, [C], [], C.confirmation_ms, state, persist, sent.append)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            duplicate = replace(C, confirmation_ms=23 * H)
            candles = [bar(22, high=111)]
            cm.update(SYMBOL, [duplicate], candles, 23 * H, state, persist, sent.append)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            cm.update(SYMBOL, [C], candles, 24 * H, state, persist, sent.append)
            self.assertEqual([cm.candidate_message(C), "✅ ETH LONG: TP HIT (+2.00R)"], sent)
            journal = root / "journal.csv"
            cm.write_journal(journal, state)
            with journal.open(encoding="utf-8") as handle:
                self.assertEqual(["SENT", "PAPER"], [r["status"] for r in csv.DictReader(handle)])

    def test_stale_candidate_is_journaled_and_never_sent(self):
        sent, state = [], {}
        cm.update(SYMBOL, [C], [bar(22, high=111)], 24 * H + 1, state, lambda: None, sent.append)
        self.assertEqual([], sent)
        self.assertEqual("MISSED_STALE", state[C.key]["status"])
        self.assertEqual("2R ATINS", state[C.key]["result"][1])
        state = {}
        cm.update(SYMBOL, [C], [], 24 * H, state, lambda: None, sent.append)
        self.assertEqual(1, len(sent))

    def test_paper_uses_only_closed_contiguous_bars_and_stop_wins(self):
        conflict = [bar(22, high=112, low=97)]
        self.assertIsNone(cm.paper_result(C, conflict, 23 * H - 1))
        self.assertEqual((23 * H, "SL ATINS", -1), cm.paper_result(C, conflict, 23 * H))
        self.assertIsNone(cm.paper_result(C, [bar(23, high=112)], 24 * H))
        candles = [bar(i) for i in range(22, 22 + 168)]
        self.assertEqual((190 * H, "TIMEOUT", .25), cm.paper_result(C, candles, 190 * H))
        short = replace(C, sign=-1, entry=102, stop=106, tp=94)
        self.assertEqual((23 * H, "2R ATINS", 2), cm.paper_result(short, [bar(22, low=93)], 23 * H))

    def test_dry_run_never_reads_secrets_or_calls_telegram(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(cm.monitor, "credentials", side_effect=AssertionError), \
                patch.object(cm.monitor, "telegram_call", side_effect=AssertionError), \
                patch.object(cm.monitor, "OkxClient") as factory, \
                patch.object(cm.PublicHistory, "candles", return_value=[bar(22)]), \
                patch.object(cm, "detect", return_value=[C]), patch("builtins.print"):
            factory.return_value.server_time_ms.return_value = 23 * H
            factory.return_value.tick_sizes.return_value = dict.fromkeys(cm.SYMBOLS, "0.01")
            self.assertEqual(0, cm.main(["--root", directory, "--dry-run"]))
            self.assertFalse((cm.monitor.state_dir(Path(directory)) / "candidate-dedup.json").exists())


class DetectorInterfaceTests(unittest.TestCase):
    def prepared(self, invert=False):
        bars = [bar(i) for i in range(30)]
        bars[20] = swing.Candle(20 * H, 101, 102, 99, 101)
        bars[21] = bar(21, high=105, low=100, close=104)
        h4 = [bar(i) for i in range(0, 28, 4)]
        pivots = [Pivot("LOW", 2, 8 * H, 16 * H, 100), Pivot("HIGH", 3, 12 * H, 20 * H, 114)]
        if invert:
            bars = [swing.Candle(c.start_ms, 200 - c.open, 200 - c.low, 200 - c.high, 200 - c.close)
                    for c in bars]
            pivots = [replace(p, kind="HIGH" if p.kind == "LOW" else "LOW", level=200 - p.level)
                      for p in pivots]
        return SimpleNamespace(data=SimpleNamespace(h1=bars, h4=h4), starts=[c.start_ms for c in bars],
                               atr=[2.] * len(bars), pivots=pivots, pivot_times=[p.confirmed_ms for p in pivots],
                               trend=lambda _: 0, daily_context=lambda _: {"daily_close": 100})

    def test_daily_informational_and_symmetric_with_closed_confirmation(self):
        for inverted, expected in ((False, 1), (True, -1)):
            with self.subTest(inverted=inverted):
                p = self.prepared(inverted)
                self.assertEqual([], swing.signals(SYMBOL, p, 20 * H, 22 * H + 1)[0])
                self.assertEqual([], swing.signals(SYMBOL, p, 20 * H, 22 * H, daily_filter=False)[0])
                signals, _ = swing.signals(SYMBOL, p, 20 * H, 22 * H + 1, daily_filter=False)
                self.assertEqual([(expected, 20 * H, 22 * H, {"daily_close": 100})],
                                 [(s.sign, s.sweep_ms, s.time, s.daily) for s in signals])
