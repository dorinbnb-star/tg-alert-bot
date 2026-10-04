from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from ai_crypto_monitor import entry_monitor as monitor  # noqa: E402

ALIGNED_NOW_MS = 1_800_000_000_000 + 30_000
OKX_BAR_MINUTES = {"15m": 15, "1H": 60, "4H": 240}


def okx_row(start_ms: int, close: float = 100.0, confirm: str = "1") -> list[str]:
    return [str(start_ms), str(close), str(close + 0.2), str(close - 0.2), str(close), "10", "0.1", "1000", confirm]


def okx_rows(minutes: int, now_ms: int, count: int) -> list[list[str]]:
    duration = minutes * 60_000
    open_start = (now_ms // duration) * duration
    return [okx_row(open_start - index * duration, confirm="0" if index == 0 else "1") for index in range(count)]


def fake_okx(candle_now_ms: int, server_now_ms: int):
    def fake_http_json(url: str, *, params: dict | None = None, **_: object) -> dict:
        if url.endswith("/api/v5/public/time"):
            return {"code": "0", "data": [{"ts": str(server_now_ms)}]}
        if url.endswith("/api/v5/market/candles"):
            assert params is not None
            rows = okx_rows(OKX_BAR_MINUTES[params["bar"]], candle_now_ms, int(params["limit"]))
            return {"code": "0", "msg": "", "data": rows}
        raise AssertionError(f"Unexpected URL {url}")
    return fake_http_json


def copy_config(target: Path) -> None:
    package = target / "ai_crypto_monitor"
    package.mkdir(parents=True)
    for name in ("config-v0.1.json", "rules-v0.1.json"):
        shutil.copy(PROJECT_ROOT / "ai_crypto_monitor" / name, package / name)
    config_path = package / "config-v0.1.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["symbols"] = ["BTC-USDT-SWAP"]
    config_path.write_text(json.dumps(config), encoding="utf-8")


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

    def test_configured_watchlist_is_exact_and_duplicate_free(self) -> None:
        config = monitor.read_json(PROJECT_ROOT / "ai_crypto_monitor" / "config-v0.1.json")
        rules = monitor.read_json(PROJECT_ROOT / "ai_crypto_monitor" / "rules-v0.1.json")
        expected = [
            "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP",
            "BNB-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "LTC-USDT-SWAP",
            "SUI-USDT-SWAP", "UNI-USDT-SWAP", "AAVE-USDT-SWAP", "TIA-USDT-SWAP",
            "WLD-USDT-SWAP", "WIF-USDT-SWAP", "STRK-USDT-SWAP", "VIRTUAL-USDT-SWAP",
            "ENA-USDT-SWAP", "YGG-USDT-SWAP",
        ]
        self.assertEqual(expected, config["symbols"])
        self.assertEqual(len(expected), len(set(config["symbols"])))
        self.assertEqual(72, config["outcome_timeout_hours"])
        self.assertEqual(3.0, rules["minimum_rr_for_enter"])

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

    def test_okx_network_error_names_endpoint(self) -> None:
        network_error = urllib.error.URLError("offline")
        with patch("urllib.request.urlopen", side_effect=network_error):
            with self.assertRaises(monitor.MonitorError) as caught:
                monitor.OkxClient().server_time_ms()
        self.assertEqual("Eroare retea: URLError la /api/v5/public/time", str(caught.exception))

    def test_okx_http_error_names_endpoint_without_query(self) -> None:
        http_error = urllib.error.HTTPError("redacted", 403, "Forbidden", {}, None)
        with patch("urllib.request.urlopen", side_effect=http_error):
            with self.assertRaises(monitor.MonitorError) as caught:
                monitor.OkxClient().klines("BTC-USDT-SWAP", "15", 3)
        message = str(caught.exception)
        self.assertEqual("HTTP 403 la /api/v5/market/candles", message)
        for fragment in ("?", "instId", "www.okx.com"):
            self.assertNotIn(fragment, message)

    def test_okx_requests_send_diagnostic_headers(self) -> None:
        sent: list = []

        class FakeResponse:
            def __init__(self, body: bytes):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def read(self) -> bytes:
                return self.body

        def fake_urlopen(request, timeout: int):
            sent.append(request)
            if request.full_url.startswith("https://www.okx.com/api/v5/public/time"):
                return FakeResponse(b'{"code":"0","data":[{"ts":"1800000030000"}]}')
            if "/api/v5/market/ticker" in request.full_url:
                return FakeResponse(b'{"code":"0","data":[{"last":"100.5"}]}')
            return FakeResponse(b'{"code":"0","data":[]}')

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            client = monitor.OkxClient()
            client.server_time_ms()
            client.klines("BTC-USDT-SWAP", "240", 3)
            client.klines("BTC-USDT-SWAP", "60", 3)
            client.klines("BTC-USDT-SWAP", "15", 3)
            client.last_price("BTC-USDT-SWAP")
        self.assertEqual(5, len(sent))
        for request in sent:
            self.assertEqual("ai-crypto-trader-diagnostic/1.0", request.get_header("User-agent"))
            self.assertEqual("application/json", request.get_header("Accept"))

    def test_telegram_errors_never_carry_endpoint_label(self) -> None:
        token = "123456:VERY_SECRET_TOKEN"
        http_error = urllib.error.HTTPError("redacted", 403, "Forbidden", {}, None)
        with patch("urllib.request.urlopen", side_effect=http_error):
            with self.assertRaises(monitor.TelegramError) as caught:
                monitor.telegram_call(token, "sendMessage", {"chat_id": "1", "text": "x"})
        self.assertNotIn(token, str(caught.exception))
        self.assertNotIn(" la ", str(caught.exception))

    def test_okx_api_error_code_is_rejected(self) -> None:
        with patch.object(monitor, "http_json", return_value={"code": "51001", "msg": "Instrument ID does not exist", "data": []}):
            with self.assertRaisesRegex(monitor.MonitorError, "code=51001"):
                monitor.OkxClient().klines("BTC-USDT-SWAP", "60", 3)

    def test_okx_candles_map_to_internal_format(self) -> None:
        payload = {"code": "0", "msg": "", "data": [
            ["1800000900000", "101", "102", "100", "101.5", "7", "0.07", "700", "1"],
            ["1800000000000", "100", "101", "99", "100.5", "5", "0.05", "500", "1"],
        ]}
        with patch.object(monitor, "http_json", return_value=payload) as request:
            client = monitor.OkxClient()
            candles = client.klines("BTC-USDT-SWAP", "15", 2)
            client.klines("BTC-USDT-SWAP", "60", 2)
            client.klines("BTC-USDT-SWAP", "240", 2)
        self.assertEqual(
            ["https://www.okx.com/api/v5/market/candles"] * 3,
            [call.args[0] for call in request.call_args_list],
        )
        self.assertEqual(["15m", "1H", "4H"], [call.kwargs["params"]["bar"] for call in request.call_args_list])
        self.assertEqual({"BTC-USDT-SWAP"}, {call.kwargs["params"]["instId"] for call in request.call_args_list})
        self.assertEqual({"/api/v5/market/candles"}, {call.kwargs["endpoint"] for call in request.call_args_list})
        self.assertEqual(
            [
                monitor.Candle(1_800_000_000_000, 100.0, 101.0, 99.0, 100.5, 5.0),
                monitor.Candle(1_800_000_900_000, 101.0, 102.0, 100.0, 101.5, 7.0),
            ],
            candles,
        )

    def test_okx_rejects_timestamps_not_strictly_descending(self) -> None:
        ascending = [okx_row(1_800_000_000_000), okx_row(1_800_000_900_000)]
        duplicated = [okx_row(1_800_000_900_000), okx_row(1_800_000_900_000)]
        for rows in (ascending, duplicated):
            with self.assertRaisesRegex(monitor.MonitorError, "descrescatoare"):
                monitor.parse_okx_candles(rows, "15")

    def test_okx_rejects_incomplete_or_non_numeric_rows(self) -> None:
        with self.assertRaisesRegex(monitor.MonitorError, "incompleta"):
            monitor.parse_okx_candles([["1800000000000", "100", "101"]], "15")
        bad = okx_row(1_800_000_000_000)
        bad[2] = "n/a"
        with self.assertRaisesRegex(monitor.MonitorError, "nenumerice"):
            monitor.parse_okx_candles([bad], "15")

    def test_okx_open_candle_is_excluded(self) -> None:
        rows = [okx_row(1_800_001_800_000, confirm="0"), okx_row(1_800_000_900_000), okx_row(1_800_000_000_000)]
        candles = monitor.parse_okx_candles(rows, "15")
        self.assertEqual([1_800_000_000_000, 1_800_000_900_000], [c.start_ms for c in candles])

    def test_fetch_closed_market_uses_only_closed_okx_candles(self) -> None:
        config = monitor.read_json(PROJECT_ROOT / "ai_crypto_monitor" / "config-v0.1.json")
        config["symbol"] = "BTC-USDT-SWAP"
        with patch.object(monitor, "http_json", side_effect=fake_okx(ALIGNED_NOW_MS, ALIGNED_NOW_MS)):
            now_ms, series = monitor.fetch_closed_market(monitor.OkxClient(), config)
        self.assertEqual(ALIGNED_NOW_MS, now_ms)
        for key, minutes in (("context", 240), ("structure", 60), ("trigger", 15)):
            duration = minutes * 60_000
            open_start = (ALIGNED_NOW_MS // duration) * duration
            self.assertEqual(open_start - duration, series[key][-1].start_ms)
            self.assertTrue(all(c.start_ms + duration <= now_ms for c in series[key]))
            starts = [c.start_ms for c in series[key]]
            self.assertEqual(sorted(set(starts)), starts)

    def test_stale_okx_data_is_rejected(self) -> None:
        config = monitor.read_json(PROJECT_ROOT / "ai_crypto_monitor" / "config-v0.1.json")
        config["symbol"] = "BTC-USDT-SWAP"
        two_hours_later = ALIGNED_NOW_MS + 2 * 60 * 60_000
        with patch.object(monitor, "http_json", side_effect=fake_okx(ALIGNED_NOW_MS, two_hours_later)):
            with self.assertRaisesRegex(monitor.MonitorError, "vechi"):
                monitor.fetch_closed_market(monitor.OkxClient(), config)

    def test_okx_no_entry_scan_is_silent_in_send_mode(self) -> None:
        sender_calls: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy_config(root)
            with patch.object(monitor, "http_json", side_effect=fake_okx(ALIGNED_NOW_MS, ALIGNED_NOW_MS)):
                result = monitor.scan_once(root, monitor.OkxClient(), False, sender=sender_calls.append)
            self.assertFalse((root / "ai_crypto_monitor" / "state" / "dedup.json").exists())
        self.assertEqual("NO_ENTRY", result["status"])
        self.assertEqual([], sender_calls)

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
