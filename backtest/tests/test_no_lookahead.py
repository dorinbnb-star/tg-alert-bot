"""Mutation test: changing anything that happens after T must not change the decision taken at T."""

from __future__ import annotations

import dataclasses
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from ai_crypto_monitor import entry_monitor as em  # noqa: E402
from backtest import download, simulate, variants  # noqa: E402
from backtest.replay import ReplayOkxClient  # noqa: E402
from backtest.tests import synthetic  # noqa: E402

CONFIG = dict(json.loads((PROJECT_ROOT / "ai_crypto_monitor" / "config-v0.1.json").read_text()), symbol="BTC-USDT-SWAP")
RULES = json.loads((PROJECT_ROOT / "ai_crypto_monitor" / "rules-v0.1.json").read_text())
SEED = 11


def decide(series: dict, minute: list, scan_ms: int) -> dict:
    """Everything the backtest decides at T: windows, every variant's signal, stage and final entry check."""
    client = ReplayOkxClient(series, minute).at(scan_ms)
    try:
        _, market = em.fetch_closed_market(client, CONFIG)
    except em.MonitorError as exc:
        return {"error": str(exc)}
    out = {}
    for variant in variants.DETECTED_VARIANTS:
        trace: dict = {}
        signal = variants.detect(variant, market, RULES, CONFIG, trace)
        item = {"trace": json.dumps(trace, sort_keys=True), "signal": dataclasses.asdict(signal) if signal else None}
        if signal:
            try:
                item["check"] = em.final_entry_check(client, signal, CONFIG)
            except em.EntrySkipped as exc:
                item["check"] = str(exc)
            if variant == "a":
                item["c1"] = variants.c1_target(signal, market, RULES)
        out[variant] = item
    return out


def mutate_after(candles: list[em.Candle], step: int, scan_ms: int, rnd: random.Random) -> list[em.Candle]:
    changed = []
    for candle in candles:
        if candle.start_ms + step <= scan_ms:
            changed.append(candle)  # closed by T: untouched
            continue
        factor = 1 + rnd.uniform(-0.05, 0.05)
        opened = candle.open if candle.start_ms <= scan_ms else candle.open * factor  # open is known at the start
        close = candle.close * factor
        changed.append(em.Candle(candle.start_ms, opened, max(opened, close) * 1.03, min(opened, close) * 0.97, close,
                                 candle.volume * 2))
    return changed


class NoLookAheadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.series, cls.minute = synthetic.market(seed=SEED, days=46)
        cls.grid = simulate.scan_grid(synthetic.END_MS - 4 * synthetic.DAY, synthetic.END_MS)
        cls.decisions = {t: decide(cls.series, cls.minute, t) for t in cls.grid}
        cls.with_signal = [t for t, d in cls.decisions.items() if any(v.get("signal") for k, v in d.items() if k != "error")]

    def test_fixture_contains_detections(self) -> None:
        self.assertGreaterEqual(len(self.with_signal), 3)
        self.assertTrue(any(self.decisions[t]["a"]["signal"] for t in self.with_signal))

    def test_mutating_candles_after_t_never_changes_the_decision_at_t(self) -> None:
        rnd = random.Random(5)
        others = [t for t in self.grid if t not in self.with_signal]
        sample = self.with_signal + rnd.sample(others, 40)
        for scan_ms in sample:
            mutated = {interval: mutate_after(candles, em.interval_ms(interval), scan_ms, rnd)
                       for interval, candles in self.series.items()}
            minute = mutate_after(self.minute, 60_000, scan_ms, rnd)
            self.assertEqual(self.decisions[scan_ms], decide(mutated, minute, scan_ms), download.iso(scan_ms))

    def test_control_mutating_the_closed_confirmation_candle_changes_the_decision(self) -> None:
        changed = 0
        for scan_ms in self.with_signal:
            trigger = list(self.series["15"])
            index = max(i for i, c in enumerate(trigger) if c.start_ms + 900_000 <= scan_ms)
            last = trigger[index]
            trigger[index] = em.Candle(last.start_ms, last.open, last.high * 1.2, last.low * 0.8, last.close * 0.9, 1)
            if decide(dict(self.series, **{"15": trigger}), self.minute, scan_ms) != self.decisions[scan_ms]:
                changed += 1
        self.assertEqual(len(self.with_signal), changed)


class LivePipelineEquivalenceTests(unittest.TestCase):
    """The backtest pipeline (detect + final_entry_check + DedupStore) matches the live scan_symbol, scan by scan."""

    def test_traded_scans_equal_live_scan_symbol_sent_scans(self) -> None:
        series, minute = synthetic.market(seed=SEED, days=46)
        start, end = synthetic.END_MS - 4 * synthetic.DAY, synthetic.END_MS
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for interval, candles in series.items():
                download.write_rows(download.series_path(root, "BTC-USDT-SWAP", download.bar(interval)),
                                    synthetic.rows(candles))
            download.write_rows(download.series_path(root, "BTC-USDT-SWAP", "1m"), synthetic.rows(minute))
            download.write_rows(download.series_path(root, "BTC-USDT-SWAP", "funding"), [])
            detected = simulate.detect_symbol((str(root), "BTC-USDT-SWAP", start, end))
            traded = simulate.trades_symbol((str(root), "BTC-USDT-SWAP", detected["detections"], start, end, end))
            backtest_sent = sorted(t["scan_ms"] for t in traded["trades"] if t["variant"] == "a" and t["mode"] == "24h")

            client = ReplayOkxClient(series, minute)
            dedup = em.DedupStore(root / "live-dedup.json", int(CONFIG["dedup_ttl_hours"]))
            live_sent = []
            statuses = []
            for scan_ms in simulate.scan_grid(start, end):
                sent: list[str] = []
                result = em.scan_symbol(root, client.at(scan_ms), False, CONFIG, RULES, dedup, sent.append)
                statuses.append(result["status"])
                if result["status"] == "SENT":
                    self.assertEqual(1, len(sent))
                    live_sent.append(scan_ms)
        self.assertTrue(live_sent, statuses)
        self.assertEqual(live_sent, backtest_sent)


if __name__ == "__main__":
    unittest.main()
