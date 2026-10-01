from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from ai_crypto_monitor import entry_monitor as monitor  # noqa: E402
from ai_crypto_monitor import scan_diagnostics as diagnostics  # noqa: E402
from test_alert_path import CONFIG, END_MS, RULES, synthetic_market  # noqa: E402


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class TraceTests(unittest.TestCase):
    def test_entry_decision_is_identical_with_and_without_trace(self) -> None:
        market = synthetic_market()
        plain = monitor.detect_entry(market["context"], market["structure"], market["trigger"], RULES, CONFIG)
        trace: dict = {}
        traced = monitor.detect_entry(
            market["context"], market["structure"], market["trigger"], RULES, CONFIG, trace,
        )
        self.assertIsNotNone(plain)
        self.assertEqual(plain, traced)
        self.assertEqual("ENTRY", trace["last_passed"])
        self.assertIsNone(trace["stopped_at"])
        self.assertEqual("LONG", trace["bias"])
        self.assertAlmostEqual(7.500625052087677, trace["rr"])

    def test_short_entry_is_identical_with_and_without_trace(self) -> None:
        market = synthetic_market()

        def invert(item: monitor.Candle) -> monitor.Candle:
            return monitor.Candle(
                item.start_ms, 200 - item.open, 200 - item.low, 200 - item.high, 200 - item.close, item.volume,
            )

        inverted = {name: [invert(item) for item in candles] for name, candles in market.items()}
        plain = monitor.detect_entry(
            inverted["context"], inverted["structure"], inverted["trigger"], RULES, CONFIG,
        )
        trace: dict = {}
        traced = monitor.detect_entry(
            inverted["context"], inverted["structure"], inverted["trigger"], RULES, CONFIG, trace,
        )
        self.assertIsNotNone(plain)
        self.assertEqual(plain, traced)
        self.assertEqual("SHORT", trace["bias"])
        self.assertEqual("ENTRY", trace["last_passed"])

    def test_rejected_rr_is_explained_without_changing_decision(self) -> None:
        market = synthetic_market()
        strict = dict(RULES, minimum_rr_for_enter=8.0)
        trace: dict = {}
        self.assertIsNone(monitor.detect_entry(
            market["context"], market["structure"], market["trigger"], strict, CONFIG, trace,
        ))
        self.assertEqual("TARGET", trace["last_passed"])
        self.assertEqual("RR", trace["stopped_at"])
        self.assertLess(trace["rr"], trace["minimum_rr"])

    def test_trace_failure_cannot_suppress_an_entry(self) -> None:
        market = synthetic_market()
        original = monitor.detect_entry

        def detector(context, structure, trigger, rules, config, trace=None):
            if trace is not None:
                raise ValueError("synthetic trace failure")
            return original(context, structure, trigger, rules, config)

        sent: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "scan.jsonl"
            current = root / "current.jsonl"
            meta = root / "meta.json"
            diagnostics.prepare(log, meta, current, "")
            env = {
                "SCAN_DIAGNOSTICS_FILE": str(log),
                "SCAN_DIAGNOSTICS_CURRENT": str(current),
                "SCAN_DIAGNOSTICS_META": str(meta),
                "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "1",
            }
            dedup = monitor.DedupStore(root / "dedup.json", ttl_hours=168)
            with patch.dict(os.environ, env, clear=False), \
                    patch.object(monitor, "fetch_closed_market", return_value=(END_MS + 60_000, market)), \
                    patch.object(monitor, "detect_entry", side_effect=detector), \
                    patch.object(monitor, "final_entry_check", return_value=(END_MS + 60_000, 101.02)):
                result = monitor.scan_symbol(root, object(), False, CONFIG, RULES, dedup, sent.append)
            row = read_rows(current)[0]
        self.assertEqual("SENT", result["status"])
        self.assertEqual(1, len(sent))
        self.assertEqual("TRACE_ERROR", row["stopped_at"])
        self.assertNotIn("TRACE_ERROR", sent[0])


class PersistenceTests(unittest.TestCase):
    def test_prepare_restores_cleans_and_marks_cache_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log, meta, current = root / "scan.jsonl", root / "meta.json", root / "current.jsonl"
            log.write_text(
                '{"record_id":"1:1:BTC","status":"NO_ENTRY"}\nnot-json\n'
                '{"record_id":"1:1:BTC","status":"NO_ENTRY"}\n',
                encoding="utf-8",
            )
            result = diagnostics.prepare(log, meta, current, "ai-crypto-diag-main-1-1")
            self.assertEqual(1, result["restored_rows"])
            self.assertTrue(result["cache_reset"])
            self.assertEqual([], read_rows(current))
            self.assertEqual(1, len(read_rows(log)))

    def test_second_run_reports_restored_row_without_reset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log, meta, current = root / "scan.jsonl", root / "meta.json", root / "current.jsonl"
            diagnostics.prepare(log, meta, current, "")
            with patch.dict(os.environ, {"GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "1"}, clear=False):
                diagnostics.append_row(log, current, meta, {"symbol": "BTC", "status": "NO_ENTRY"})
            state = diagnostics.prepare(log, meta, current, "ai-crypto-diag-main-1-1")
            with patch.dict(os.environ, {"GITHUB_RUN_ID": "2", "GITHUB_RUN_ATTEMPT": "1"}, clear=False):
                row = diagnostics.append_row(log, current, meta, {"symbol": "BTC", "status": "NO_ENTRY"})
            self.assertEqual(1, state["restored_rows"])
            self.assertFalse(state["cache_reset"])
            self.assertEqual(1, row["restored_rows"])
            self.assertFalse(row["cache_reset"])

    def test_finalizer_writes_scan_error_only_when_scanner_wrote_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log, meta, current = root / "scan.jsonl", root / "meta.json", root / "current.jsonl"
            diagnostics.prepare(log, meta, current, "")
            env = {"GITHUB_RUN_ID": "9", "GITHUB_RUN_ATTEMPT": "1"}
            with patch.dict(os.environ, env, clear=False):
                first = diagnostics.ensure_error_row(log, current, meta, "BTC", "failure", "success")
                second = diagnostics.ensure_error_row(log, current, meta, "BTC", "failure", "success")
            self.assertEqual("SCAN_ERROR", first["status"])
            self.assertIsNone(second)
            self.assertEqual(1, len(read_rows(log)))

    def test_summary_reconstructs_unique_rows_from_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            one, two = root / "one.jsonl", root / "two.jsonl"
            one.write_text('{"record_id":"1","symbol":"BTC","status":"NO_ENTRY","stopped_at":"RR"}\n', encoding="utf-8")
            two.write_text(
                '{"record_id":"1","symbol":"BTC","status":"NO_ENTRY","stopped_at":"RR"}\n'
                '{"record_id":"2","symbol":"BTC","status":"SCAN_ERROR","stopped_at":"WORKFLOW"}\n',
                encoding="utf-8",
            )
            result = diagnostics.summarize([one, two])
        self.assertEqual(2, result["rows"])
        self.assertEqual({"NO_ENTRY": 1, "SCAN_ERROR": 1}, result["statuses"])


if __name__ == "__main__":
    unittest.main()
