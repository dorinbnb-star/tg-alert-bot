from __future__ import annotations

import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from ai_crypto_monitor import entry_monitor as monitor  # noqa: E402


def candle(start_ms: int, close: float, *, high: float | None = None, low: float | None = None) -> monitor.Candle:
    return monitor.Candle(
        start_ms=start_ms,
        open=close,
        high=high if high is not None else close + 0.2,
        low=low if low is not None else close - 0.2,
        close=close,
        volume=100.0,
    )


class EntryMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = {
            "reference_pivot_closed_candles_each_side": 2,
            "min_sweep_percent": 0.05,
            "max_sweep_percent": 0.75,
            "reentry_window_15m_candles": 4,
            "confirmation_window_15m_candles": 2,
            "invalidation_buffer_percent": 0.10,
            "minimum_rr_for_enter": 1.8,
        }
        self.config = {
            "symbol": "BTCUSDT",
            "signal_lookback_15m_candles": 40,
            "entry_valid_seconds": 300,
            "probability_label": "NECALIBRATA",
        }

    def test_no_trigger_produces_no_entry(self) -> None:
        hour = 60 * 60_000
        quarter = 15 * 60_000
        context = [candle(index * 4 * hour, 100.0) for index in range(220)]
        structure = [candle(index * hour, 100.0) for index in range(80)]
        trigger = [candle((70 * hour) + index * quarter, 100.0) for index in range(40)]
        self.assertIsNone(monitor.detect_entry(context, structure, trigger, self.rules, self.config))

    def test_no_entry_never_calls_sender(self) -> None:
        sender_calls: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            dedup = monitor.DedupStore(Path(directory) / "dedup.json", ttl_hours=24)
            with patch.object(monitor, "fetch_closed_market", return_value=(1_000_000, {"context": [], "structure": [], "trigger": []})), patch.object(monitor, "detect_entry", return_value=None):
                result = monitor.scan_symbol(
                    Path(directory), object(), False,
                    {"symbol": "BTCUSDT"}, self.rules, dedup,
                    sender=sender_calls.append,
                )
        self.assertEqual("NO_ENTRY", result["status"])
        self.assertEqual([], sender_calls)

    def test_closed_series_rejects_missing_and_stale_data(self) -> None:
        now_ms = 10_000_000
        with self.assertRaisesRegex(monitor.MonitorError, "incomplete"):
            monitor.validate_series([], "15", now_ms, minimum=3, maximum_age_seconds=1200)
        stale = [candle(0, 100), candle(900_000, 100), candle(1_800_000, 100)]
        with self.assertRaisesRegex(monitor.MonitorError, "vechi"):
            monitor.validate_series(stale, "15", now_ms, minimum=3, maximum_age_seconds=1200)

    def test_confirmed_entry_uses_last_closed_trigger_and_structural_stop(self) -> None:
        hour = 60 * 60_000
        quarter = 15 * 60_000
        end_ms = 100 * hour
        context = [candle(end_ms - (220 - index) * 4 * hour, 80 + index * 0.15) for index in range(220)]
        structure = [candle(end_ms - (80 - index) * hour, 100 + index * 0.08) for index in range(80)]
        structure[50] = candle(structure[50].start_ms, 104.0, high=104.4, low=100.0)
        structure[48] = candle(structure[48].start_ms, 103.7, high=104.0, low=103.2)
        structure[49] = candle(structure[49].start_ms, 103.8, high=104.1, low=103.3)
        structure[51] = candle(structure[51].start_ms, 104.1, high=104.5, low=103.4)
        structure[52] = candle(structure[52].start_ms, 104.2, high=104.6, low=103.5)
        structure[55] = candle(structure[55].start_ms, 105.0, high=110.0, low=104.4)
        structure[53] = candle(structure[53].start_ms, 104.4, high=105.0, low=104.0)
        structure[54] = candle(structure[54].start_ms, 104.6, high=105.2, low=104.1)
        structure[56] = candle(structure[56].start_ms, 104.8, high=105.3, low=104.2)
        structure[57] = candle(structure[57].start_ms, 104.9, high=105.4, low=104.3)
        trigger = [candle(end_ms - (40 - index) * quarter, 102.0) for index in range(40)]
        trigger[-2] = candle(trigger[-2].start_ms, 100.2, high=100.4, low=99.9)
        trigger[-1] = candle(trigger[-1].start_ms, 101.0, high=101.2, low=100.1)
        signal = monitor.detect_entry(context, structure, trigger, self.rules, self.config)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual("LONG", signal.direction)
        self.assertLess(signal.stop, signal.sweep_extreme)
        self.assertGreaterEqual(signal.rr, 1.8)
        self.assertEqual(trigger[-1].start_ms, signal.confirmation_start_ms)
        self.assertEqual("NECALIBRATA", signal.probability)

    def test_dedup_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dedup.json"
            first = monitor.DedupStore(path, ttl_hours=24)
            first.mark("abc", 1_000_000)
            second = monitor.DedupStore(path, ttl_hours=24)
            self.assertTrue(second.contains("abc", 1_000_100))

    def test_duplicate_alert_never_calls_sender(self) -> None:
        signal = monitor.EntrySignal(
            symbol="BTCUSDT", direction="LONG", reference_level=100.0,
            reference_confirmed_at_ms=100, sweep_start_ms=200,
            sweep_extreme=99.8, reentry_start_ms=300,
            confirmation_start_ms=400, confirmation_end_ms=500,
            entry=101.0, stop=99.5, target=104.0, rr=2.0,
            expires_at_ms=2_000_000, confluence_score=81,
            probability="NECALIBRATA",
        )
        sender_calls: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            dedup = monitor.DedupStore(Path(directory) / "dedup.json", ttl_hours=24)
            dedup.mark(signal.signature, 1_000_000)
            with patch.object(monitor, "fetch_closed_market", return_value=(1_000_000, {"context": [], "structure": [], "trigger": []})), patch.object(monitor, "detect_entry", return_value=signal), patch.object(monitor, "final_entry_check", return_value=(1_000_100, 101.0)):
                result = monitor.scan_symbol(
                    Path(directory), object(), False,
                    {"symbol": "BTCUSDT"}, self.rules, dedup,
                    sender=sender_calls.append,
                )
        self.assertEqual("DUPLICATE", result["status"])
        self.assertEqual([], sender_calls)

    def test_telegram_error_never_contains_token(self) -> None:
        token = "123456:VERY_SECRET_TOKEN"
        http_error = urllib.error.HTTPError("redacted", 401, "Unauthorized", {}, None)
        with patch("urllib.request.urlopen", side_effect=http_error):
            with self.assertRaises(monitor.TelegramError) as caught:
                monitor.telegram_call(token, "getMe")
        self.assertNotIn(token, str(caught.exception))
        self.assertIn("HTTP 401", str(caught.exception))

    def test_bybit_unavailable_is_safe_error(self) -> None:
        network_error = urllib.error.URLError("offline")
        with patch("urllib.request.urlopen", side_effect=network_error):
            with self.assertRaisesRegex(monitor.MonitorError, "Eroare retea"):
                monitor.http_json("https://api.bybit.com/v5/market/time")

    def test_bybit_base_url_and_public_candle_intervals_are_configurable(self) -> None:
        empty_klines = {"retCode": 0, "result": {"list": []}}
        with patch.dict(os.environ, {"BYBIT_BASE_URL": ""}):
            self.assertEqual("https://api-demo.bybit.com", monitor.BybitClient().base_url)

        with patch.dict(os.environ, {"BYBIT_BASE_URL": "https://market.example/"}), patch.object(
            monitor, "http_json", return_value=empty_klines,
        ) as request:
            client = monitor.BybitClient()
            client.klines("BTCUSDT", "60", 160)
            client.klines("BTCUSDT", "15", 160)

        self.assertEqual(
            ["https://market.example/v5/market/kline"] * 2,
            [call.args[0] for call in request.call_args_list],
        )
        self.assertEqual(
            ["60", "15"],
            [call.kwargs["params"]["interval"] for call in request.call_args_list],
        )

    def test_technical_message_is_one_shot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / "external.env"
            env_file.write_text("TELEGRAM_TOKEN=test-token\nTELEGRAM_CHAT_ID=123\n", encoding="utf-8")
            identity = {"bot_username": "DorianTradingSignals_bot", "destination_fingerprint": "abc123"}
            sent_messages: list[str] = []

            def fake_call(token: str, method: str, params: dict | None = None) -> dict:
                self.assertEqual("sendMessage", method)
                assert params is not None
                sent_messages.append(params["text"])
                return {"message_id": 99}

            with patch.object(monitor, "require_verified_identity", return_value=identity), patch.object(monitor, "telegram_call", side_effect=fake_call):
                first = monitor.send_technical_test(root, env_file)
                second = monitor.send_technical_test(root, env_file)
            self.assertEqual("SENT", first["status"])
            self.assertEqual("ALREADY_RECORDED", second["status"])
            self.assertEqual([monitor.TECHNICAL_TEST_MESSAGE], sent_messages)


if __name__ == "__main__":
    unittest.main()
