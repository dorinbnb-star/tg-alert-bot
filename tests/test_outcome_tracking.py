from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from ai_crypto_monitor import entry_monitor as monitor  # noqa: E402
from ai_crypto_monitor import outcome_tracking as tracking  # noqa: E402
from ai_crypto_monitor import scan_diagnostics  # noqa: E402


BRUSSELS = ZoneInfo("Europe/Brussels")


def milliseconds(year: int, month: int, day: int, hour: int, minute: int, second: int = 0, microsecond: int = 0) -> int:
    value = datetime(year, month, day, hour, minute, second, microsecond, tzinfo=BRUSSELS)
    return int(value.timestamp() * 1000)


def signal(
    symbol: str = "AAVE-USDT-SWAP",
    direction: str = "LONG",
    entry: float = 179.12,
    stop: float = 177.72,
    target: float = 182.746,
) -> monitor.EntrySignal:
    opened = milliseconds(2026, 10, 4, 20, 0)
    return monitor.EntrySignal(
        symbol=symbol, direction=direction, reference_level=178.0,
        reference_confirmed_at_ms=opened - 3_600_000,
        sweep_start_ms=opened - 900_000, sweep_extreme=177.9,
        reentry_start_ms=opened - 600_000,
        confirmation_start_ms=opened - 300_000,
        confirmation_end_ms=opened,
        entry=entry, stop=stop, target=target,
        rr=2.59, expires_at_ms=milliseconds(2026, 10, 4, 20, 7),
        confluence_score=81, probability="NECALIBRATA",
        context_close=180.0, context_ema50=175.0, context_ema200=170.0,
        structure_close=180.0, structure_ema50=176.0,
        sweep_depth_percent=0.2, reentry_level=179.0, min_rr=1.8,
    )


def alert(
    *, direction: str = "LONG", opened_at_ms: int | None = None,
    entry: float = 100.0, stop: float = 99.0, target: float = 102.0,
) -> tracking.TrackedAlert:
    opened = opened_at_ms or milliseconds(2026, 10, 4, 10, 1)
    return tracking.new_alert(
        alert_id="alert-1", symbol="AAVE-USDT-SWAP", direction=direction,
        theoretical_entry=entry, verified_entry=entry, stop=stop, target=target,
        strategy_rr=2.0, opened_at_ms=opened, valid_until_ms=opened + 420_000,
        timeout_hours=72, telegram_message_id=11,
    )


class PriceFormattingTests(unittest.TestCase):
    def test_exact_entry_alert_uses_verified_price_rr(self) -> None:
        item = signal()
        formatter = monitor.PriceFormatter({"AAVE-USDT-SWAP": "0.01"})
        self.assertEqual(
            "\n".join([
                "🟢 AAVE LONG",
                "",
                "Entry teoretic: 179.12",
                "Preț verificat: 178.95",
                "SL: 177.72",
                "TP: 182.75",
                "R:R: 3.09",
                "Valabil până la: 20:07",
                "Status: PENDING",
            ]),
            monitor.format_entry_alert(item, 178.95, formatter),
        )

    def test_short_header_and_result_messages(self) -> None:
        item = signal(direction="SHORT", entry=180.0, stop=181.0, target=178.0)
        formatter = monitor.PriceFormatter({"AAVE-USDT-SWAP": "0.01"})
        self.assertTrue(monitor.format_entry_alert(item, 180.0, formatter).startswith("🔴 AAVE SHORT"))
        tracked = alert(direction="SHORT", entry=180.0, stop=181.0, target=178.0)
        tp = tracking.Outcome("TP_HIT", 2.0, 178.0, 1, 0)
        sl = tracking.Outcome("SL_HIT", -1.0, 181.0, 1, 0)
        timeout = tracking.Outcome("TIMEOUT", -0.25, 180.25, 1, None)
        self.assertEqual("✅ AAVE SHORT: TP HIT (+2.00R)", monitor.format_outcome_alert(tracked, tp, formatter))
        self.assertEqual("❌ AAVE SHORT: SL HIT (−1R)", monitor.format_outcome_alert(tracked, sl, formatter))
        self.assertEqual(
            "⏱ AAVE SHORT: ÎNCHIS LA TIMEOUT (−0.25R la prețul curent 180.25)",
            monitor.format_outcome_alert(tracked, timeout, formatter),
        )

    def test_official_tick_precision_for_requested_price_ranges(self) -> None:
        formatter = monitor.PriceFormatter({
            "WLD-USDT-SWAP": "0.0001",
            "BTC-USDT-SWAP": "0.1",
            "TIA-USDT-SWAP": "0.001",
            "LOW-USDT-SWAP": "0.000001",
        })
        self.assertEqual("0.5941", formatter.format("WLD-USDT-SWAP", 0.5941))
        self.assertEqual("85000.1", formatter.format("BTC-USDT-SWAP", 85000.14))
        self.assertEqual("0.537", formatter.format("TIA-USDT-SWAP", 0.5374))
        self.assertEqual("0.006543", formatter.format("LOW-USDT-SWAP", 0.00654321))

    def test_wld_reconstructed_alert_keeps_levels_distinct(self) -> None:
        item = signal(
            symbol="WLD-USDT-SWAP", entry=0.5941, stop=0.5916,
            target=0.599688,
        )
        message = monitor.format_entry_alert(
            item, 0.5940, monitor.PriceFormatter({"WLD-USDT-SWAP": "0.0001"}),
        )
        self.assertIn("Entry teoretic: 0.5941", message)
        self.assertIn("Preț verificat: 0.5940", message)
        self.assertIn("SL: 0.5916", message)
        self.assertIn("TP: 0.5997", message)
        self.assertIn("R:R: 2.37", message)

    def test_tick_size_api_failure_uses_six_significant_digit_fallback(self) -> None:
        class BrokenClient:
            def tick_sizes(self):
                raise monitor.MonitorError("OKX indisponibil")

        formatter = monitor.PriceFormatter.load(BrokenClient())
        self.assertEqual("0.594123", formatter.format("WLD-USDT-SWAP", 0.5941234))
        self.assertNotEqual(
            formatter.format("WLD-USDT-SWAP", 0.5941234),
            formatter.format("WLD-USDT-SWAP", 0.5916123),
        )

    def test_tick_sizes_are_loaded_by_one_public_request(self) -> None:
        payload = {"code": "0", "data": [
            {"instId": "BTC-USDT-SWAP", "tickSz": "0.1"},
            {"instId": "WLD-USDT-SWAP", "tickSz": "0.0001"},
        ]}
        with patch.object(monitor, "http_json", return_value=payload) as request:
            formatter = monitor.PriceFormatter.load(monitor.OkxClient())
        self.assertEqual(1, request.call_count)
        self.assertEqual("0.0001", formatter.tick_size("WLD-USDT-SWAP"))
        self.assertEqual("/api/v5/public/instruments", request.call_args.kwargs["endpoint"])

    def test_step_summary_uses_tick_precision_for_signal_and_diagnostic_prices(self) -> None:
        item = signal(symbol="WLD-USDT-SWAP", entry=0.5941, stop=0.5916, target=0.599688)
        formatter = monitor.PriceFormatter({"WLD-USDT-SWAP": "0.0001"})
        diagnostic = monitor.entry_diagnostic(item, 0.5940, 2.37, formatter)
        result = {
            "status": "ENTRY_READY_DRY_RUN",
            "results": [{
                "status": "ENTRY_READY_DRY_RUN", "signal": asdict(item),
                "live_price": 0.5940, "tracking_rr": 2.37,
                "strategy_rr": 2.59, "tick_size": "0.0001",
                "diagnostic": {"symbol": item.symbol, "last_passed": "ENTRY", "stopped_at": None, **diagnostic},
            }],
            "outcomes": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.md"
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}, clear=False):
                monitor.write_step_summary(result)
            text = summary.read_text(encoding="utf-8")
        for expected in ("0.5941", "0.5940", "0.5916", "0.5997"):
            self.assertIn(expected, text)


class OutcomeEvaluationTests(unittest.TestCase):
    def candle(self, start: int, high: float, low: float, close: float = 100.0) -> monitor.Candle:
        return monitor.Candle(start, close, high, low, close, 1.0)

    def test_tp_hit(self) -> None:
        item = alert()
        outcome = tracking.evaluate_candles(item, [self.candle(0, 102.1, 99.5)], tracking.MINUTE_MS)
        assert outcome is not None
        self.assertEqual(("TP_HIT", 2.0), (outcome.status, outcome.result_r))

    def test_sl_hit(self) -> None:
        item = alert()
        outcome = tracking.evaluate_candles(item, [self.candle(0, 100.5, 98.9)], tracking.MINUTE_MS)
        assert outcome is not None
        self.assertEqual(("SL_HIT", -1.0), (outcome.status, outcome.result_r))

    def test_tp_and_sl_in_same_candle_is_sl(self) -> None:
        item = alert()
        outcome = tracking.evaluate_candles(item, [self.candle(0, 102.1, 98.9)], tracking.MINUTE_MS)
        assert outcome is not None
        self.assertEqual("SL_HIT", outcome.status)
        self.assertTrue(outcome.same_candle_collision)

    def test_first_hit_is_chronological(self) -> None:
        item = alert()
        candles = [self.candle(60_000, 100.5, 98.9), self.candle(0, 102.1, 99.5)]
        outcome = tracking.evaluate_candles(item, candles, tracking.MINUTE_MS)
        assert outcome is not None
        self.assertEqual("TP_HIT", outcome.status)

    def test_timeout_r_uses_verified_entry(self) -> None:
        item = alert(entry=100.5, stop=99.5, target=102.5)
        self.assertAlmostEqual(0.75, tracking.timeout_r(item, 101.25))

    def test_store_persists_open_and_resolved_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alert-outcomes.json"
            store = tracking.OutcomeStore(path)
            store.add(alert())
            restored = tracking.OutcomeStore(path)
            self.assertTrue(restored.contains("alert-1"))
            outcome = tracking.Outcome("TP_HIT", 2.0, 102.0, 123, 60)
            restored.resolve("alert-1", outcome, 22)
            final = tracking.OutcomeStore(path)
            self.assertEqual({}, final.open)
            self.assertTrue(final.contains("alert-1"))
            self.assertEqual("TP_HIT", final.resolved["alert-1"]["status"])


class OutcomeIntegrationTests(unittest.TestCase):
    class FakeClient:
        def __init__(self, now_ms: int, hit_at: int | None = None, timeout_price: float = 100.5):
            self.now_ms = now_ms
            self.hit_at = hit_at
            self.timeout_price = timeout_price
            self.calls: list[tuple[str, int, int]] = []

        def server_time_ms(self) -> int:
            return self.now_ms

        def last_price(self, _symbol: str) -> float:
            return self.timeout_price

        def history_klines(self, _symbol: str, bar: str, start_ms: int, end_ms: int):
            self.calls.append((bar, start_ms, end_ms))
            duration = monitor.OKX_TRACKING_BARS[bar]
            values = []
            for current in range(start_ms, end_ms, duration):
                high = 102.1 if current == self.hit_at else 100.5
                values.append(monitor.Candle(current, 100.0, high, 99.5, 100.0, 1.0))
            return values

    def test_overnight_touch_is_sent_at_first_morning_tick(self) -> None:
        opened = milliseconds(2026, 10, 4, 21, 59)
        item = alert(opened_at_ms=opened)
        item.cursor_ms = milliseconds(2026, 10, 4, 22, 0)
        hit = milliseconds(2026, 10, 5, 2, 15)
        morning = milliseconds(2026, 10, 5, 8, 1)
        client = self.FakeClient(morning, hit_at=hit)
        sent: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            store = tracking.OutcomeStore(Path(directory) / "alert-outcomes.json")
            store.add(item)
            results = monitor.track_open_alerts(
                client, store, monitor.PriceFormatter({"AAVE-USDT-SWAP": "0.01"}),
                3, lambda message: sent.append(message),
            )
        self.assertEqual("TP_HIT", results[0]["status"])
        self.assertEqual(["✅ AAVE LONG: TP HIT (+2.00R)"], sent)
        self.assertIn(("15m", milliseconds(2026, 10, 4, 22, 0), milliseconds(2026, 10, 5, 8, 0)), client.calls)

    def test_no_candle_formed_before_alert_is_requested(self) -> None:
        opened = milliseconds(2026, 10, 4, 10, 1, 0, 500_000)
        item = alert(opened_at_ms=opened)
        now = milliseconds(2026, 10, 4, 10, 2, 5)
        client = self.FakeClient(now)
        with tempfile.TemporaryDirectory() as directory:
            store = tracking.OutcomeStore(Path(directory) / "alert-outcomes.json")
            store.add(item)
            monitor.track_open_alerts(
                client, store, monitor.PriceFormatter({}), 3, lambda _message: {},
            )
        self.assertTrue(client.calls)
        first_bar, first_start, _ = client.calls[0]
        self.assertEqual("1s", first_bar)
        self.assertEqual(milliseconds(2026, 10, 4, 10, 1, 1), first_start)
        self.assertGreater(first_start, opened)

    def test_timeout_sends_current_price_and_resolves(self) -> None:
        item = alert()
        item.cursor_ms = tracking.floor_boundary(item.timeout_at_ms, tracking.SECOND_MS)
        client = self.FakeClient(item.timeout_at_ms + 60_000, timeout_price=100.75)
        sent: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            store = tracking.OutcomeStore(Path(directory) / "alert-outcomes.json")
            store.add(item)
            results = monitor.track_open_alerts(
                client, store, monitor.PriceFormatter({"AAVE-USDT-SWAP": "0.01"}),
                3, lambda message: sent.append(message),
            )
            self.assertEqual({}, store.open)
        self.assertEqual("TIMEOUT", results[0]["status"])
        self.assertEqual(
            ["⏱ AAVE LONG: ÎNCHIS LA TIMEOUT (+0.75R la prețul curent 100.75)"], sent,
        )

    def test_telegram_failure_keeps_alert_open_for_retry(self) -> None:
        opened = milliseconds(2026, 10, 4, 10, 0)
        item = alert(opened_at_ms=opened)
        item.cursor_ms = opened
        client = self.FakeClient(opened + tracking.QUARTER_MS + 10_000, hit_at=opened)
        with tempfile.TemporaryDirectory() as directory:
            store = tracking.OutcomeStore(Path(directory) / "alert-outcomes.json")
            store.add(item)

            def fail(_message: str):
                raise monitor.TelegramError("Telegram indisponibil")

            with self.assertRaises(monitor.TelegramError):
                monitor.track_open_alerts(client, store, monitor.PriceFormatter({}), 3, fail)
            self.assertIn("alert-1", store.open)
            self.assertEqual({}, store.resolved)


class ArtifactRecoveryTests(unittest.TestCase):
    def test_missing_cache_and_artifact_bootstraps_empty_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alert-outcomes.json"
            with patch.object(tracking, "_request_json", return_value={"artifacts": []}):
                status = tracking.restore_latest_artifact(path, "owner/repo", "main", "token")
            self.assertEqual("EMPTY_BOOTSTRAP", status)
            self.assertEqual({}, tracking.OutcomeStore(path).open)

    def test_artifact_restore_preserves_resolved_tombstones(self) -> None:
        state = {
            "version": 1, "recovery_status": "SAVED", "open": {},
            "resolved": {"old": {"status": "TP_HIT"}},
        }
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("alert-outcomes.json", json.dumps(state))

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _size: int) -> bytes:
                return archive.getvalue()

        payload = {"artifacts": [{
            "name": tracking.ARTIFACT_NAME, "expired": False,
            "created_at": "2026-10-04T08:00:00Z",
            "archive_download_url": "https://api.github.com/archive/1",
            "workflow_run": {"head_branch": "main"},
        }]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alert-outcomes.json"
            with patch.object(tracking, "_request_json", return_value=payload), \
                    patch("urllib.request.urlopen", return_value=Response()):
                status = tracking.restore_latest_artifact(path, "owner/repo", "main", "token")
            restored = tracking.OutcomeStore(path)
        self.assertEqual("ARTIFACT", status)
        self.assertTrue(restored.contains("old"))

    def test_lost_state_is_diagnosed_once_without_replaying_old_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "ai_crypto_monitor"
            package.mkdir()
            for name in ("config-v0.1.json", "rules-v0.1.json"):
                shutil.copy(PROJECT_ROOT / "ai_crypto_monitor" / name, package / name)
            config_path = package / "config-v0.1.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["symbols"] = ["BTC-USDT-SWAP"]
            config_path.write_text(json.dumps(config), encoding="utf-8")
            state_path = package / "state" / "alert-outcomes.json"
            with patch.object(tracking, "_request_json", return_value={"artifacts": []}):
                tracking.restore_latest_artifact(state_path, "owner/repo", "main", "token")
            log = root / "diagnostics.jsonl"
            current = root / "current.jsonl"
            meta = root / "meta.json"
            scan_diagnostics.prepare(log, meta, current, "")
            environment = {
                "SCAN_DIAGNOSTICS_FILE": str(log),
                "SCAN_DIAGNOSTICS_CURRENT": str(current),
                "SCAN_DIAGNOSTICS_META": str(meta),
                "GITHUB_RUN_ID": "77",
                "GITHUB_RUN_ATTEMPT": "1",
            }
            with patch.dict(os.environ, environment, clear=False), \
                    patch.object(monitor.PriceFormatter, "load", return_value=monitor.PriceFormatter({})), \
                    patch.object(monitor, "fetch_closed_market", return_value=(1_000_000, {"context": [], "structure": [], "trigger": []})), \
                    patch.object(monitor, "detect_entry", return_value=None):
                result = monitor.scan_once(root, object(), False, sender=lambda _message: {})
            rows = [json.loads(line) for line in current.read_text(encoding="utf-8").splitlines()]
            restored = tracking.OutcomeStore(state_path)
        self.assertEqual("EMPTY_BOOTSTRAP", result["outcome_recovery"])
        self.assertEqual([], result["outcomes"])
        self.assertEqual(1, sum(row["status"] == "TRACKING_STATE_LOST" for row in rows))
        self.assertEqual("ACTIVE_AFTER_EMPTY_BOOTSTRAP", restored.recovery_status)


if __name__ == "__main__":
    unittest.main()
