"""Golden test: replaying the run 369 scan from OKX history gives exactly what the live scan logged, on all 18 symbols.

Needs `python -m backtest.download golden` (runner only). Locally the test is skipped; with BACKTEST_REQUIRE_GOLDEN=1
(set by backtest.yml) missing data is a failure, never a skip.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from ai_crypto_monitor import entry_monitor as em  # noqa: E402
from backtest import download  # noqa: E402
from backtest.replay import ReplayOkxClient, load_series  # noqa: E402

GOLDEN = json.loads(download.GOLDEN_PATH.read_text(encoding="utf-8"))
GOLDEN_DIR = Path(os.getenv("BACKTEST_DATA_DIR", str(download.DATA_DIR))) / "golden"


def live_diagnostic(client: ReplayOkxClient, config: dict, rules: dict) -> dict:
    """The diagnostic scan_symbol logs: decision, traced decision, trigger close/start."""
    _, series = em.fetch_closed_market(client, config)
    signal = em.detect_entry(series["context"], series["structure"], series["trigger"], rules, config)
    trace: dict = {}
    traced = em.detect_entry(series["context"], series["structure"], series["trigger"], rules, config, trace)
    trace["trace_matches_decision"] = (signal.signature if signal else None) == (traced.signature if traced else None)
    trace["trigger_close"] = series["trigger"][-1].close
    trace["trigger_start_ms"] = series["trigger"][-1].start_ms
    return trace


class GoldenRun369Tests(unittest.TestCase):
    def test_golden_covers_all_config_symbols(self) -> None:
        self.assertEqual(download.symbols(), list(GOLDEN["symbols"]))

    def test_replay_matches_live_run_369_on_every_symbol(self) -> None:
        missing = [s for s in GOLDEN["symbols"] if not download.series_path(GOLDEN_DIR, s, "15m").exists()]
        if missing:
            if os.getenv("BACKTEST_REQUIRE_GOLDEN") == "1":
                self.fail(f"date golden lipsa pentru {missing}")
            self.skipTest("date golden nedescarcate (ruleaza `python -m backtest.download golden`)")
        rules = download.load_rules()
        identical = []
        for symbol, item in GOLDEN["symbols"].items():
            config = dict(download.load_config(), symbol=symbol)
            series = {config["intervals"][k]: load_series(GOLDEN_DIR, symbol, config["intervals"][k])
                      for k in ("context", "structure", "trigger")}
            client = ReplayOkxClient(series).at(download.parse_utc(item["detected_at"]))
            with self.subTest(symbol=symbol):
                self.assertEqual(item["diagnostic"], live_diagnostic(client, config, rules))
                identical.append(symbol)
        print(f"\ngolden run 369: {len(identical)}/{len(GOLDEN['symbols'])} monede identice: "
              + ", ".join(s.replace('-USDT-SWAP', '') for s in identical), flush=True)
        self.assertEqual(len(GOLDEN["symbols"]), len(identical))


if __name__ == "__main__":
    unittest.main()
