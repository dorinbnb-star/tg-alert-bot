from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from ai_crypto_monitor import entry_monitor as monitor  # noqa: E402

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


class AlertDecisionTests(unittest.TestCase):
    def scan(self, market: dict, directory: str, sender, *, check=(END_MS + 60_000, 101.02), rules=RULES) -> dict:
        dedup = monitor.DedupStore(Path(directory) / "dedup.json", ttl_hours=168)
        with patch.object(monitor, "fetch_closed_market", return_value=(END_MS + 60_000, market)):
            if isinstance(check, Exception):
                with patch.object(monitor, "final_entry_check", side_effect=check):
                    return monitor.scan_symbol(Path(directory), object(), False, CONFIG, rules, dedup, sender)
            with patch.object(monitor, "final_entry_check", return_value=check):
                return monitor.scan_symbol(Path(directory), object(), False, CONFIG, rules, dedup, sender)

    def test_confirmed_entry_sends_one_complete_alert(self) -> None:
        sent: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            result = self.scan(synthetic_market(), directory, sent.append)
            self.assertTrue((Path(directory) / "dedup.json").exists())
        self.assertEqual("SENT", result["status"])
        self.assertEqual(1, len(sent))
        message = sent[0]
        for fragment in (
            "NOW=ENTER LONG BTC-USDT-SWAP", "BIAS: LONG", "SETUP: sweep low 1H 100.00",
            "TRIGGER: lumanarea 15m", "peste maximul lumanarii de reintrare 100.40",
            "Entry: 101.00", "SL / invalidare: 99.80", "TP: 110.00", "R:R: 7.50",
            "Pro: 4H close", "Contra: ", "Probabilitate: necalibrata", "fara ordine automate",
        ):
            self.assertIn(fragment, message)
        for forbidden in ("WATCH", "NO_ENTRY", TOKEN, "probabilitate calibrata"):
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

    def test_uncalibrated_label_is_never_shown_as_a_probability_value(self) -> None:
        market = synthetic_market()
        signal = monitor.detect_entry(market["context"], market["structure"], market["trigger"], RULES, CONFIG)
        assert signal is not None
        self.assertIn("Probabilitate: necalibrata.", monitor.format_entry_alert(signal, 101.0))
        self.assertNotIn("%", monitor.format_entry_alert(signal, 101.0).split("Probabilitate:")[1])


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
        self.assertTrue(calls[0][1]["text"].startswith("NOW=ENTER LONG BTC-USDT-SWAP"))
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


if __name__ == "__main__":
    unittest.main()
