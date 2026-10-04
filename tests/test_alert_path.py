from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from ai_crypto_monitor import entry_monitor as monitor  # noqa: E402
from ai_crypto_monitor import outcome_tracking as tracking  # noqa: E402
from ai_crypto_monitor import scan_diagnostics as diagnostics  # noqa: E402

HOUR = 60 * 60_000
QUARTER = 15 * 60_000
END_MS = 100 * HOUR
TOKEN = "123456:SYNTHETIC_TEST_TOKEN"
RULES = {
    "reference_pivot_closed_candles_each_side": 2,
    "min_sweep_percent": 0.05,
    "max_sweep_percent": 0.75,
    "reentry_window_15m_candles": 4,
    "confirmation_window_15m_candles": 2,
    "invalidation_buffer_percent": 0.10,
    "minimum_rr_for_enter": 1.8,
}
CONFIG = {
    "symbol": "BTC-USDT-SWAP",
    "signal_lookback_15m_candles": 40,
    "entry_valid_seconds": 300,
    "maximum_entry_drift_percent": 0.15,
    "probability_label": "NECALIBRATA",
}


def candle(start_ms: int, close: float, *, high: float | None = None, low: float | None = None) -> monitor.Candle:
    return monitor.Candle(start_ms, close, high if high is not None else close + 0.2, low if low is not None else close - 0.2, close, 100.0)


def synthetic_market(confirm_close: float = 101.0) -> dict[str, list[monitor.Candle]]:
    """LONG bias, 1H pivot low swept on 15m, reentry, then the last closed 15m candle confirms (synthetic only)."""
    context = [candle(END_MS - (220 - i) * 4 * HOUR, 80 + i * 0.15) for i in range(220)]
    structure = [candle(END_MS - (80 - i) * HOUR, 100 + i * 0.08) for i in range(80)]
    for index, close, high, low in (
        (48, 103.7, 104.0, 103.2), (49, 103.8, 104.1, 103.3), (50, 104.0, 104.4, 100.0),
        (51, 104.1, 104.5, 103.4), (52, 104.2, 104.6, 103.5), (53, 104.4, 105.0, 104.0),
        (54, 104.6, 105.2, 104.1), (55, 105.0, 110.0, 104.4), (56, 104.8, 105.3, 104.2),
        (57, 104.9, 105.4, 104.3),
    ):
        structure[index] = candle(structure[index].start_ms, close, high=high, low=low)
    trigger = [candle(END_MS - (40 - i) * QUARTER, 102.0) for i in range(40)]
    trigger[-2] = candle(trigger[-2].start_ms, 100.2, high=100.4, low=99.9)
    trigger[-1] = candle(trigger[-1].start_ms, confirm_close, high=confirm_close + 0.2, low=100.1)
    return {"context": context, "structure": structure, "trigger": trigger}


def copy_config(target: Path) -> None:
    package = target / "ai_crypto_monitor"
    package.mkdir(parents=True)
    for name in ("config-v0.1.json", "rules-v0.1.json"):
        shutil.copy(PROJECT_ROOT / "ai_crypto_monitor" / name, package / name)
    config_path = package / "config-v0.1.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["symbols"] = ["BTC-USDT-SWAP"]
    config_path.write_text(json.dumps(config), encoding="utf-8")


class AlertDecisionTests(unittest.TestCase):
    def scan(self, market: dict, directory: str, sender, *, check=(END_MS + 60_000, 101.02), rules=RULES) -> dict:
        dedup = monitor.DedupStore(Path(directory) / "dedup.json", ttl_hours=168)
        formatter = monitor.PriceFormatter({"BTC-USDT-SWAP": "0.01"})
        with patch.object(monitor, "fetch_closed_market", return_value=(END_MS + 60_000, market)):
            if isinstance(check, Exception):
                with patch.object(monitor, "final_entry_check", side_effect=check):
                    return monitor.scan_symbol(
                        Path(directory), object(), False, CONFIG, rules, dedup, sender,
                        formatter=formatter,
                    )
            with patch.object(monitor, "final_entry_check", return_value=check):
                return monitor.scan_symbol(
                    Path(directory), object(), False, CONFIG, rules, dedup, sender,
                    formatter=formatter,
                )

    def scan_fixed_signal(
        self,
        signal: monitor.EntrySignal,
        directory: str,
        sender,
        *,
        outcome_store: tracking.OutcomeStore | None = None,
    ) -> dict:
        dedup = monitor.DedupStore(Path(directory) / "dedup.json", ttl_hours=168)
        config = dict(CONFIG, symbol=signal.symbol)
        formatter = monitor.PriceFormatter({signal.symbol: "0.01"})
        with patch.object(monitor, "fetch_closed_market", return_value=(END_MS + 60_000, synthetic_market())), \
                patch.object(monitor, "detect_entry", return_value=signal), \
                patch.object(monitor, "final_entry_check", return_value=(END_MS + 60_000, signal.entry)):
            return monitor.scan_symbol(
                Path(directory), object(), False, config, RULES, dedup, sender,
                formatter=formatter, outcome_store=outcome_store,
            )

    def test_confirmed_entry_sends_one_complete_alert(self) -> None:
        sent: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            result = self.scan(synthetic_market(), directory, sent.append)
            self.assertTrue((Path(directory) / "dedup.json").exists())
        self.assertEqual("SENT", result["status"])
        self.assertEqual(1, len(sent))
        message = sent[0]
        self.assertEqual(
            "\n".join([
                "🟢 BTC LONG",
                "",
                "Entry teoretic: 101.00",
                "Preț verificat: 101.02",
                "SL: 99.80",
                "TP: 110.00",
                "R:R: 7.50",
                f"Valabil până la: {monitor.local_hm(result['signal']['expires_at_ms'])}",
                "Status: PENDING",
            ]),
            message,
        )
        for forbidden in ("WATCH", "NO_ENTRY", TOKEN, "BIAS", "SETUP", "TRIGGER", "Pro:", "Contra:"):
            self.assertNotIn(forbidden, message)

    def test_same_confirmed_candle_never_alerts_twice_across_runs(self) -> None:
        sent: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            first = self.scan(synthetic_market(), directory, sent.append)
            second = self.scan(synthetic_market(), directory, sent.append)
        self.assertEqual(["SENT", "DUPLICATE"], [first["status"], second["status"]])
        self.assertEqual(1, len(sent))

    def test_setup_without_confirmation_is_silent(self) -> None:
        sent: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            result = self.scan(synthetic_market(confirm_close=100.3), directory, sent.append)
        self.assertEqual("NO_ENTRY", result["status"])
        self.assertEqual([], sent)

    def test_insufficient_structural_rr_is_rejected_silently(self) -> None:
        sent: list[str] = []
        strict = dict(RULES, minimum_rr_for_enter=8.0)
        with tempfile.TemporaryDirectory() as directory:
            result = self.scan(synthetic_market(), directory, sent.append, rules=strict)
        self.assertEqual("NO_ENTRY", result["status"])
        self.assertEqual([], sent)

    def test_strategy_rr_2_59_does_not_send_at_three_minimum(self) -> None:
        market = synthetic_market()
        risk = 101.0 - (99.9 * (1 - RULES["invalidation_buffer_percent"] / 100))
        target = 101.0 + risk * 2.59
        sent: list[str] = []
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(monitor, "opposing_target", return_value=target):
            result = self.scan(
                market, directory, sent.append,
                rules=dict(RULES, minimum_rr_for_enter=3.0),
            )
        self.assertEqual("NO_ENTRY", result["status"])
        self.assertEqual([], sent)

    def test_strategy_rr_exactly_three_sends_and_displays_three(self) -> None:
        market = synthetic_market()
        risk = 101.0 - (99.9 * (1 - RULES["invalidation_buffer_percent"] / 100))
        target = 101.0 + risk * 3.0
        sent: list[str] = []
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(monitor, "opposing_target", return_value=target):
            result = self.scan(
                market, directory, sent.append,
                rules=dict(RULES, minimum_rr_for_enter=3.0),
            )
        self.assertEqual("SENT", result["status"])
        self.assertEqual(1, len(sent))
        self.assertIn("R:R: 3.00", sent[0])

    def test_bnb_same_direction_and_pivot_alerts_only_once(self) -> None:
        market = synthetic_market()
        base = monitor.detect_entry(market["context"], market["structure"], market["trigger"], RULES, CONFIG)
        assert base is not None
        first = replace(base, symbol="BNB-USDT-SWAP", reference_level=786.50)
        second = replace(
            first,
            sweep_start_ms=first.sweep_start_ms + QUARTER,
            reentry_start_ms=first.reentry_start_ms + QUARTER,
            confirmation_start_ms=first.confirmation_start_ms + QUARTER,
            confirmation_end_ms=first.confirmation_end_ms + QUARTER,
        )
        sent: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            first_result = self.scan_fixed_signal(first, directory, sent.append)
            second_result = self.scan_fixed_signal(second, directory, sent.append)
        self.assertEqual(first.signature, second.signature)
        self.assertEqual(["SENT", "DUPLICATE"], [first_result["status"], second_result["status"]])
        self.assertEqual(1, len(sent))

    def test_open_and_resolved_outcome_state_block_same_structural_pivot(self) -> None:
        market = synthetic_market()
        item = monitor.detect_entry(market["context"], market["structure"], market["trigger"], RULES, CONFIG)
        assert item is not None
        for state in ("open", "resolved"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                store = tracking.OutcomeStore(Path(directory) / "alert-outcomes.json")
                store.add(tracking.new_alert(
                    alert_id=item.signature,
                    symbol=item.symbol,
                    direction=item.direction,
                    theoretical_entry=item.entry,
                    verified_entry=item.entry,
                    stop=item.stop,
                    target=item.target,
                    strategy_rr=item.rr,
                    opened_at_ms=END_MS,
                    valid_until_ms=item.expires_at_ms,
                    timeout_hours=72,
                    telegram_message_id=7,
                ))
                if state == "resolved":
                    store.resolve(
                        item.signature,
                        tracking.Outcome("TP_HIT", item.rr, item.target, END_MS, END_MS),
                        8,
                    )
                result = self.scan_fixed_signal(
                    replace(item, sweep_start_ms=item.sweep_start_ms + QUARTER),
                    directory,
                    lambda _message: self.fail("duplicate must not send"),
                    outcome_store=store,
                )
                self.assertFalse((Path(directory) / "dedup.json").exists())
                self.assertEqual("DUPLICATE", result["status"])

    def test_expired_or_drifted_entry_is_skipped_silently_without_error(self) -> None:
        for reason in ("Semnal expirat inainte de trimitere", "Pretul s-a deplasat prea mult: drift=0.300%"):
            sent: list[str] = []
            with tempfile.TemporaryDirectory() as directory:
                result = self.scan(synthetic_market(), directory, sent.append, check=monitor.EntrySkipped(reason))
                self.assertFalse((Path(directory) / "dedup.json").exists())
            self.assertEqual("ENTRY_SKIPPED", result["status"])
            self.assertEqual(reason, result["reason"])
            self.assertEqual([], sent)

    def test_telegram_failure_does_not_mark_dedup(self) -> None:
        def failing_sender(message: str) -> None:
            raise monitor.TelegramError("Telegram sendMessage esuat: HTTP 502")

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(monitor.TelegramError):
                self.scan(synthetic_market(), directory, failing_sender)
            self.assertFalse((Path(directory) / "dedup.json").exists())
            sent: list[str] = []
            retry = self.scan(synthetic_market(), directory, sent.append)
        self.assertEqual("SENT", retry["status"])
        self.assertEqual(1, len(sent))

    def test_uncalibrated_label_stays_in_diagnostic_not_telegram(self) -> None:
        market = synthetic_market()
        signal = monitor.detect_entry(market["context"], market["structure"], market["trigger"], RULES, CONFIG)
        assert signal is not None
        message = monitor.format_entry_alert(signal, 101.0)
        diagnostic = monitor.entry_diagnostic(signal, 101.0, monitor.tracked_rr(signal, 101.0))
        self.assertNotIn("Probabilitate", message)
        self.assertEqual("NECALIBRATA", diagnostic["probability"])


class SendModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        copy_config(self.root)
        self.output = self.root / "github_output"
        self.env = patch.dict(os.environ, {"GITHUB_OUTPUT": str(self.output), "GITHUB_ACTIONS": "true"})
        self.env.start()
        for key in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "GITHUB_STEP_SUMMARY"):
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        self.env.stop()
        self.directory.cleanup()

    def run_send(self) -> int:
        return monitor.main(["scan", "--root", str(self.root), "--send"])

    def test_missing_secrets_fail_closed_before_any_network_call(self) -> None:
        with patch.object(monitor, "OkxClient", side_effect=AssertionError("no network expected")), \
                patch.object(monitor, "telegram_call", side_effect=AssertionError("no telegram expected")):
            self.assertEqual(2, self.run_send())
        self.assertFalse(self.output.exists())

    def test_no_entry_in_send_mode_makes_no_telegram_call(self) -> None:
        os.environ.update({"TELEGRAM_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"})
        with patch.object(monitor, "OkxClient", return_value=object()), \
                patch.object(monitor, "fetch_closed_market", return_value=(END_MS, synthetic_market(confirm_close=100.3))), \
                patch.object(monitor, "telegram_call", side_effect=AssertionError("no telegram expected")):
            self.assertEqual(0, self.run_send())
        self.assertIn("alert_sent=false", self.output.read_text())

    def test_confirmed_entry_in_send_mode_checks_identity_then_sends_once(self) -> None:
        os.environ.update({"TELEGRAM_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"})
        calls: list[tuple[str, dict | None]] = []

        def fake_telegram(token: str, method: str, params: dict | None = None) -> dict:
            self.assertEqual(TOKEN, token)
            calls.append((method, params))
            return {"message_id": 7}

        with patch.object(monitor, "OkxClient", return_value=object()), \
                patch.object(monitor, "fetch_closed_market", return_value=(END_MS, synthetic_market())), \
                patch.object(monitor, "final_entry_check", return_value=(END_MS + 60_000, 101.02)), \
                patch.object(monitor, "require_delivery_identity", return_value={}) as identity, \
                patch.object(monitor, "telegram_call", side_effect=fake_telegram):
            self.assertEqual(0, self.run_send())
        identity.assert_called_once()
        self.assertEqual(["sendMessage"], [method for method, _ in calls])
        self.assertEqual("42", calls[0][1]["chat_id"])
        self.assertTrue(calls[0][1]["text"].startswith("🟢 BTC LONG"))
        self.assertIn("alert_sent=true", self.output.read_text())
        self.assertTrue((self.root / "ai_crypto_monitor" / "state" / "dedup.json").exists())

    def test_rejected_delivery_identity_blocks_the_alert(self) -> None:
        os.environ.update({"TELEGRAM_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"})
        with patch.object(monitor, "OkxClient", return_value=object()), \
                patch.object(monitor, "fetch_closed_market", return_value=(END_MS, synthetic_market())), \
                patch.object(monitor, "final_entry_check", return_value=(END_MS + 60_000, 101.02)), \
                patch.object(monitor, "require_delivery_identity", side_effect=monitor.MonitorError("botul nu corespunde")), \
                patch.object(monitor, "telegram_call", side_effect=AssertionError("must not send")):
            self.assertEqual(2, self.run_send())
        self.assertFalse((self.root / "ai_crypto_monitor" / "state" / "dedup.json").exists())


class MultiSymbolIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        copy_config(self.root)
        source_config = monitor.read_json(PROJECT_ROOT / "ai_crypto_monitor" / "config-v0.1.json")
        config_path = self.root / "ai_crypto_monitor" / "config-v0.1.json"
        config = monitor.read_json(config_path)
        config["symbols"] = source_config["symbols"]
        monitor.write_json(config_path, config)
        self.symbols = list(config["symbols"])
        self.output = self.root / "github-output"
        self.summary = self.root / "step-summary.md"
        self.log = self.root / "scan-diagnostics.jsonl"
        self.current = self.root / "current-scan-diagnostic.jsonl"
        self.meta = self.root / "scan-diagnostics-meta.json"
        diagnostics.prepare(self.log, self.meta, self.current, "")
        self.env = patch.dict(os.environ, {
            "GITHUB_OUTPUT": str(self.output),
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "GITHUB_ACTIONS": "true",
            "GITHUB_RUN_ID": "500",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_REF_NAME": "codex/expand-watchlist-18",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "SCAN_DIAGNOSTICS_FILE": str(self.log),
            "SCAN_DIAGNOSTICS_CURRENT": str(self.current),
            "SCAN_DIAGNOSTICS_META": str(self.meta),
            "SCAN_DIAGNOSTICS_SYMBOL": "BTC-USDT-SWAP",
        }, clear=False)
        self.env.start()
        os.environ.pop("TELEGRAM_TOKEN", None)
        os.environ.pop("TELEGRAM_CHAT_ID", None)

    def tearDown(self) -> None:
        self.env.stop()
        self.directory.cleanup()

    def diagnostic_rows(self) -> list[dict]:
        return [json.loads(line) for line in self.current.read_text(encoding="utf-8").splitlines() if line]

    def entry_signature(self, symbol: str) -> str:
        config = monitor.read_json(self.root / "ai_crypto_monitor" / "config-v0.1.json")
        config["symbol"] = symbol
        signal = monitor.detect_entry(
            synthetic_market()["context"], synthetic_market()["structure"], synthetic_market()["trigger"],
            monitor.read_json(self.root / "ai_crypto_monitor" / "rules-v0.1.json"), config,
        )
        assert signal is not None
        return signal.signature

    def test_third_symbol_error_keeps_other_seventeen_and_all_diagnostics(self) -> None:
        failed_symbol = self.symbols[2]

        def fetch(_client, config):
            if config["symbol"] == failed_symbol:
                raise monitor.MonitorError("date incomplete")
            return END_MS + 60_000, synthetic_market(confirm_close=100.3)

        with patch.object(monitor, "fetch_closed_market", side_effect=fetch):
            result = monitor.scan_once(self.root, object(), True)
        monitor.write_step_summary(result)
        rows = self.diagnostic_rows()
        self.assertEqual(18, len(result["results"]))
        self.assertEqual(17, sum(item["status"] != "SCAN_ERROR" for item in result["results"]))
        self.assertEqual(18, len(rows))
        self.assertEqual(
            {"status": "SCAN_ERROR", "symbol": failed_symbol, "reason": "date incomplete"},
            {key: next(item for item in result["results"] if item["status"] == "SCAN_ERROR")[key]
             for key in ("status", "symbol", "reason")},
        )
        self.assertEqual("SCAN_ERROR", next(row for row in rows if row["symbol"] == failed_symbol)["status"])
        self.assertIn(f"{failed_symbol}: **SCAN_ERROR** - `date incomplete`", self.summary.read_text(encoding="utf-8"))

    def test_sent_second_symbol_then_third_symbol_error_persists_output_and_dedup(self) -> None:
        sent_symbol, failed_symbol = self.symbols[1], self.symbols[2]
        os.environ.update({"TELEGRAM_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"})

        def fetch(_client, config):
            if config["symbol"] == failed_symbol:
                raise monitor.MonitorError("HTTP 503 la /api/v5/market/candles")
            confirm = 101.0 if config["symbol"] == sent_symbol else 100.3
            return END_MS + 60_000, synthetic_market(confirm_close=confirm)

        with patch.object(monitor, "OkxClient", return_value=object()), \
                patch.object(monitor, "fetch_closed_market", side_effect=fetch), \
                patch.object(monitor, "final_entry_check", return_value=(END_MS + 60_000, 101.02)), \
                patch.object(monitor, "require_delivery_identity", return_value={}), \
                patch.object(monitor, "telegram_call", return_value={"message_id": 7}):
            self.assertEqual(0, monitor.main(["scan", "--root", str(self.root), "--send"]))

        rows = self.diagnostic_rows()
        dedup = monitor.read_json(self.root / "ai_crypto_monitor" / "state" / "dedup.json")
        self.assertEqual(18, len(rows))
        self.assertEqual("SCAN_ERROR", next(row for row in rows if row["symbol"] == failed_symbol)["status"])
        self.assertIn("alert_sent=true", self.output.read_text(encoding="utf-8"))
        self.assertIn(self.entry_signature(sent_symbol), dedup["sent"])

    def test_prior_alert_survives_later_telegram_error(self) -> None:
        sent_symbol, failed_symbol = self.symbols[1], self.symbols[2]
        os.environ.update({"TELEGRAM_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"})

        def fetch(_client, config):
            confirm = 101.0 if config["symbol"] in {sent_symbol, failed_symbol} else 100.3
            return END_MS + 60_000, synthetic_market(confirm_close=confirm)

        telegram_calls = 0

        def send(_token, method, _params=None):
            nonlocal telegram_calls
            if method != "sendMessage":
                raise AssertionError(f"unexpected Telegram method: {method}")
            telegram_calls += 1
            if telegram_calls == 2:
                raise monitor.TelegramError("Telegram sendMessage esuat: HTTP 502")
            return {"message_id": 7}

        with patch.object(monitor, "OkxClient", return_value=object()), \
                patch.object(monitor, "fetch_closed_market", side_effect=fetch), \
                patch.object(monitor, "final_entry_check", return_value=(END_MS + 60_000, 101.02)), \
                patch.object(monitor, "require_delivery_identity", return_value={}), \
                patch.object(monitor, "telegram_call", side_effect=send):
            self.assertEqual(2, monitor.main(["scan", "--root", str(self.root), "--send"]))

        rows = self.diagnostic_rows()
        by_symbol = {row["symbol"]: row for row in rows}
        dedup = monitor.read_json(self.root / "ai_crypto_monitor" / "state" / "dedup.json")
        self.assertEqual(3, len(rows))
        self.assertEqual("NO_ENTRY", by_symbol[self.symbols[0]]["status"])
        self.assertEqual("SENT", by_symbol[sent_symbol]["status"])
        self.assertEqual("SCAN_ERROR", by_symbol[failed_symbol]["status"])
        self.assertEqual("Telegram sendMessage esuat: HTTP 502", by_symbol[failed_symbol]["reason"])
        self.assertIn("alert_sent=true", self.output.read_text(encoding="utf-8"))
        self.assertIn(self.entry_signature(sent_symbol), dedup["sent"])
        self.assertNotIn(self.entry_signature(failed_symbol), dedup["sent"])

    def test_all_symbols_error_returns_exit_code_two(self) -> None:
        def fetch(_client, config):
            raise monitor.MonitorError(f"date vechi pentru {config['symbol']}")

        with patch.object(monitor, "OkxClient", return_value=object()), \
                patch.object(monitor, "fetch_closed_market", side_effect=fetch):
            self.assertEqual(2, monitor.main(["scan", "--root", str(self.root), "--dry-run"]))

        rows = self.diagnostic_rows()
        self.assertEqual(18, len(rows))
        self.assertTrue(all(row["status"] == "SCAN_ERROR" for row in rows))
        output = self.output.read_text(encoding="utf-8")
        self.assertIn("status=SCAN_ERROR", output)
        self.assertIn("alert_sent=false", output)


if __name__ == "__main__":
    unittest.main()
