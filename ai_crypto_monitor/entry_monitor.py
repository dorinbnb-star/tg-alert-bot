#!/usr/bin/env python3
"""Strict Bybit v0.1 entry monitor with gated Telegram delivery."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


LOGGER = logging.getLogger("entry-monitor")
DEFAULT_BYBIT_BASE_URL = "https://api-demo.bybit.com"
TELEGRAM_BASE = "https://api.telegram.org"
TECHNICAL_TEST_MESSAGE = (
    "TEST AI Crypto Trader: conexiunea Telegram functioneaza. "
    "Acesta nu este un semnal de tranzactionare."
)


class MonitorError(RuntimeError):
    pass


class TelegramError(MonitorError):
    pass


@dataclass(frozen=True)
class Candle:
    start_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Pivot:
    kind: str
    level: float
    candle_start_ms: int
    confirmed_at_ms: int


@dataclass(frozen=True)
class EntrySignal:
    symbol: str
    direction: str
    reference_level: float
    reference_confirmed_at_ms: int
    sweep_start_ms: int
    sweep_extreme: float
    reentry_start_ms: int
    confirmation_start_ms: int
    confirmation_end_ms: int
    entry: float
    stop: float
    target: float
    rr: float
    expires_at_ms: int
    confluence_score: int
    probability: str

    @property
    def signature(self) -> str:
        raw = (
            f"{self.symbol}|{self.direction}|{self.reference_confirmed_at_ms}|"
            f"{self.sweep_start_ms}|{self.confirmation_start_ms}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def utc_iso(milliseconds: int) -> str:
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MonitorError(f"Nu pot citi JSON valid: {path}") from exc
    if not isinstance(value, dict):
        raise MonitorError(f"JSON-ul trebuie sa fie obiect: {path}")
    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def package_dir(root: Path) -> Path:
    return root / "ai_crypto_monitor"


def state_dir(root: Path) -> Path:
    return package_dir(root) / "state"


def load_env_file(path: Path | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if path is None:
        return values
    if not path.is_file():
        raise MonitorError(f"Fisier env inexistent: {path}")
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def credentials(env_file: Path | None) -> tuple[str, str]:
    file_values = load_env_file(env_file)
    token = os.getenv("TELEGRAM_TOKEN") or file_values.get("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID") or file_values.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        raise MonitorError("Lipsesc TELEGRAM_TOKEN sau TELEGRAM_CHAT_ID")
    return token, chat_id


def http_json(url: str, *, params: dict | None = None, method: str = "GET", timeout: int = 15) -> dict:
    encoded = urllib.parse.urlencode(params or {}).encode("utf-8")
    request_url = url
    data = None
    if method == "GET" and encoded:
        request_url += "?" + encoded.decode("ascii")
    elif method == "POST":
        data = encoded
    request = urllib.request.Request(request_url, data=data, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise MonitorError(f"HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise MonitorError(f"Eroare retea: {type(exc).__name__}") from None
    if not isinstance(payload, dict):
        raise MonitorError("Raspuns API invalid")
    return payload


def bybit_base_url() -> str:
    return (os.getenv("BYBIT_BASE_URL") or DEFAULT_BYBIT_BASE_URL).rstrip("/")


class BybitClient:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or bybit_base_url()).rstrip("/")

    def server_time_ms(self) -> int:
        payload = http_json(f"{self.base_url}/v5/market/time")
        if payload.get("retCode") != 0:
            raise MonitorError(f"Bybit time retCode={payload.get('retCode')}")
        return int(payload["result"]["timeNano"]) // 1_000_000

    def klines(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        payload = http_json(
            f"{self.base_url}/v5/market/kline",
            params={"category": "linear", "symbol": symbol, "interval": interval, "limit": str(limit)},
        )
        if payload.get("retCode") != 0:
            raise MonitorError(f"Bybit kline retCode={payload.get('retCode')}")
        candles = []
        for row in reversed(payload.get("result", {}).get("list", [])):
            try:
                candles.append(Candle(
                    start_ms=int(row[0]), open=float(row[1]), high=float(row[2]),
                    low=float(row[3]), close=float(row[4]), volume=float(row[5]),
                ))
            except (IndexError, TypeError, ValueError):
                continue
        return candles

    def last_price(self, symbol: str) -> float:
        payload = http_json(
            f"{self.base_url}/v5/market/tickers",
            params={"category": "linear", "symbol": symbol},
        )
        if payload.get("retCode") != 0:
            raise MonitorError(f"Bybit ticker retCode={payload.get('retCode')}")
        try:
            return float(payload["result"]["list"][0]["lastPrice"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise MonitorError("Bybit ticker incomplet") from exc


def telegram_call(token: str, method: str, params: dict | None = None) -> dict:
    # Never log request_url: it contains the token.
    request_url = f"{TELEGRAM_BASE}/bot{token}/{method}"
    try:
        payload = http_json(request_url, params=params, method="POST" if params else "GET", timeout=10)
    except MonitorError as exc:
        raise TelegramError(f"Telegram {method} esuat: {exc}") from None
    if not payload.get("ok"):
        code = payload.get("error_code", "unknown")
        raise TelegramError(f"Telegram {method} refuzat: code={code}")
    return payload.get("result", {})


def safe_identity(token: str, chat_id: str) -> dict:
    bot = telegram_call(token, "getMe")
    destination = telegram_call(token, "getChat", {"chat_id": chat_id})
    fingerprint = hashlib.sha256(str(chat_id).encode("utf-8")).hexdigest()[:12]
    return {
        "bot_id": bot.get("id"),
        "bot_username": bot.get("username"),
        "bot_first_name": bot.get("first_name"),
        "is_bot": bot.get("is_bot"),
        "destination_fingerprint": fingerprint,
        "destination_type": destination.get("type"),
        "destination_title": destination.get("title"),
        "destination_username": destination.get("username"),
        "destination_first_name": destination.get("first_name"),
    }


def verify_telegram(
    root: Path,
    env_file: Path,
    accept: bool,
    expected_username: str | None,
    expected_fingerprint: str | None,
) -> dict:
    token, chat_id = credentials(env_file)
    identity = safe_identity(token, chat_id)
    if accept:
        if not expected_username or not expected_fingerprint:
            raise MonitorError("--accept necesita username si amprenta destinatiei confirmate")
        if str(identity.get("bot_username", "")).lower() != expected_username.lstrip("@").lower():
            raise MonitorError("Username-ul botului nu corespunde confirmarii")
        if identity["destination_fingerprint"] != expected_fingerprint:
            raise MonitorError("Destinatia nu corespunde amprentei confirmate")
        identity["verified_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        write_json(state_dir(root) / "telegram-identity.json", identity)
    return identity


def require_verified_identity(root: Path, token: str, chat_id: str) -> dict:
    path = state_dir(root) / "telegram-identity.json"
    if not path.exists():
        raise MonitorError("Trimiterea este blocata: identitatea Telegram nu a fost acceptata")
    expected = read_json(path)
    current = safe_identity(token, chat_id)
    for field in ("bot_id", "bot_username", "destination_fingerprint"):
        if current.get(field) != expected.get(field):
            raise MonitorError(f"Trimiterea este blocata: identitatea difera la {field}")
    return current


def require_delivery_identity(root: Path, token: str, chat_id: str, config: dict) -> dict:
    if os.getenv("GITHUB_ACTIONS", "").lower() != "true":
        return require_verified_identity(root, token, chat_id)
    current = safe_identity(token, chat_id)
    expected_username = str(config.get("expected_bot_username", "")).lstrip("@").lower()
    if not expected_username or str(current.get("bot_username", "")).lower() != expected_username:
        raise MonitorError("Trimiterea este blocata: botul nu corespunde configuratiei")
    if current.get("destination_type") != "private":
        raise MonitorError("Trimiterea este blocata: destinatia nu este chat privat")
    return current


def send_technical_test(root: Path, env_file: Path) -> dict:
    token, chat_id = credentials(env_file)
    identity = require_verified_identity(root, token, chat_id)
    marker_path = state_dir(root) / "telegram-test.json"
    if marker_path.exists():
        marker = read_json(marker_path)
        return {
            "status": "ALREADY_RECORDED",
            "state": marker.get("state"),
            "requested_at": marker.get("requested_at"),
            "sent_at": marker.get("sent_at"),
        }
    requested_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    marker = {
        "state": "PENDING",
        "requested_at": requested_at,
        "message_sha256": hashlib.sha256(TECHNICAL_TEST_MESSAGE.encode("utf-8")).hexdigest(),
        "bot_username": identity.get("bot_username"),
        "destination_fingerprint": identity.get("destination_fingerprint"),
    }
    write_json(marker_path, marker)
    try:
        result = telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": TECHNICAL_TEST_MESSAGE})
    except TelegramError:
        marker["state"] = "FAILED"
        marker["failed_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        write_json(marker_path, marker)
        raise
    marker["state"] = "SENT"
    marker["sent_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    marker["telegram_message_id"] = result.get("message_id")
    write_json(marker_path, marker)
    return {"status": "SENT", "sent_at": marker["sent_at"], "message_id": marker["telegram_message_id"]}


class DedupStore:
    def __init__(self, path: Path, ttl_hours: int):
        self.path = path
        self.ttl_ms = ttl_hours * 60 * 60 * 1000
        self.entries: dict[str, int] = {}
        if path.exists():
            data = read_json(path)
            self.entries = {str(key): int(value) for key, value in data.get("sent", {}).items()}

    def prune(self, now_ms: int) -> None:
        self.entries = {key: sent_at for key, sent_at in self.entries.items() if now_ms - sent_at <= self.ttl_ms}

    def contains(self, signature: str, now_ms: int) -> bool:
        self.prune(now_ms)
        return signature in self.entries

    def mark(self, signature: str, now_ms: int) -> None:
        self.prune(now_ms)
        self.entries[signature] = now_ms
        write_json(self.path, {"sent": self.entries})


def interval_ms(interval: str) -> int:
    mapping = {"15": 15 * 60_000, "60": 60 * 60_000, "240": 4 * 60 * 60_000}
    if interval not in mapping:
        raise MonitorError(f"Interval neacceptat: {interval}")
    return mapping[interval]


def closed_only(candles: list[Candle], interval: str, now_ms: int, grace_seconds: int) -> list[Candle]:
    cutoff = now_ms - grace_seconds * 1000
    duration = interval_ms(interval)
    return [candle for candle in candles if candle.start_ms + duration <= cutoff]


def validate_series(
    candles: list[Candle],
    interval: str,
    now_ms: int,
    minimum: int,
    maximum_age_seconds: int,
) -> None:
    if len(candles) < minimum:
        raise MonitorError(f"Date incomplete {interval}: {len(candles)} < {minimum}")
    last_end = candles[-1].start_ms + interval_ms(interval)
    age = now_ms - last_end
    if age < 0 or age > maximum_age_seconds * 1000:
        raise MonitorError(f"Date vechi {interval}: age_seconds={max(age, 0) // 1000}")


def ema(values: list[float], period: int) -> float:
    if len(values) < period:
        raise MonitorError(f"EMA{period} necesita mai multe date")
    factor = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = value * factor + result * (1 - factor)
    return result


def pivots(candles: list[Candle], side: int) -> list[Pivot]:
    found: list[Pivot] = []
    for index in range(side, len(candles) - side):
        current = candles[index]
        neighbors = candles[index - side:index] + candles[index + 1:index + side + 1]
        confirmed_at = candles[index + side].start_ms + interval_ms("60")
        if all(current.low < other.low for other in neighbors):
            found.append(Pivot("LOW", current.low, current.start_ms, confirmed_at))
        if all(current.high > other.high for other in neighbors):
            found.append(Pivot("HIGH", current.high, current.start_ms, confirmed_at))
    return found


def market_bias(context: list[Candle], structure: list[Candle]) -> str | None:
    context_closes = [candle.close for candle in context]
    structure_closes = [candle.close for candle in structure]
    context_50, context_200 = ema(context_closes, 50), ema(context_closes, 200)
    structure_50 = ema(structure_closes, 50)
    if context_closes[-1] > context_50 > context_200 and structure_closes[-1] > structure_50:
        return "LONG"
    if context_closes[-1] < context_50 < context_200 and structure_closes[-1] < structure_50:
        return "SHORT"
    return None


def opposing_target(all_pivots: list[Pivot], direction: str, entry: float, before_ms: int) -> float | None:
    available = [pivot for pivot in all_pivots if pivot.confirmed_at_ms <= before_ms]
    if direction == "LONG":
        levels = [pivot.level for pivot in available if pivot.kind == "HIGH" and pivot.level > entry]
        return min(levels) if levels else None
    levels = [pivot.level for pivot in available if pivot.kind == "LOW" and pivot.level < entry]
    return max(levels) if levels else None


def detect_entry(
    context: list[Candle],
    structure: list[Candle],
    trigger: list[Candle],
    rules: dict,
    config: dict,
) -> EntrySignal | None:
    direction = market_bias(context, structure)
    if direction is None:
        return None
    side = int(rules["reference_pivot_closed_candles_each_side"])
    all_pivots = pivots(structure, side)
    reference_kind = "LOW" if direction == "LONG" else "HIGH"
    references = [pivot for pivot in all_pivots if pivot.kind == reference_kind]
    if not references:
        return None
    recent_trigger = trigger[-int(config["signal_lookback_15m_candles"]):]
    min_sweep = float(rules["min_sweep_percent"]) / 100
    max_sweep = float(rules["max_sweep_percent"]) / 100
    reentry_window = int(rules["reentry_window_15m_candles"])
    confirmation_window = int(rules["confirmation_window_15m_candles"])
    buffer_fraction = float(rules["invalidation_buffer_percent"]) / 100
    min_rr = float(rules["minimum_rr_for_enter"])
    for reference in reversed(references):
        if reference.confirmed_at_ms >= recent_trigger[-1].start_ms:
            continue
        for sweep_index, sweep in enumerate(recent_trigger):
            if sweep.start_ms < reference.confirmed_at_ms:
                continue
            if direction == "LONG":
                depth = (reference.level - sweep.low) / reference.level
            else:
                depth = (sweep.high - reference.level) / reference.level
            if not min_sweep <= depth <= max_sweep:
                continue
            for reentry_index in range(sweep_index, min(len(recent_trigger), sweep_index + reentry_window)):
                reentry = recent_trigger[reentry_index]
                reentered = reentry.close > reference.level if direction == "LONG" else reentry.close < reference.level
                if not reentered:
                    continue
                end_confirmation = min(len(recent_trigger), reentry_index + confirmation_window + 1)
                for confirmation_index in range(reentry_index + 1, end_confirmation):
                    confirmation = recent_trigger[confirmation_index]
                    confirmed = confirmation.close > reentry.high if direction == "LONG" else confirmation.close < reentry.low
                    if not confirmed or confirmation is not trigger[-1]:
                        continue
                    relevant_sweeps = recent_trigger[sweep_index:reentry_index + 1]
                    extreme = min(c.low for c in relevant_sweeps) if direction == "LONG" else max(c.high for c in relevant_sweeps)
                    entry = confirmation.close
                    stop = extreme * (1 - buffer_fraction) if direction == "LONG" else extreme * (1 + buffer_fraction)
                    target = opposing_target(all_pivots, direction, entry, confirmation.start_ms)
                    if target is None:
                        continue
                    risk = abs(entry - stop)
                    reward = target - entry if direction == "LONG" else entry - target
                    if risk <= 0 or reward <= 0:
                        continue
                    rr = reward / risk
                    if rr < min_rr:
                        continue
                    confirmation_end = confirmation.start_ms + interval_ms("15")
                    return EntrySignal(
                        symbol=config["symbol"], direction=direction,
                        reference_level=reference.level,
                        reference_confirmed_at_ms=reference.confirmed_at_ms,
                        sweep_start_ms=sweep.start_ms, sweep_extreme=extreme,
                        reentry_start_ms=reentry.start_ms,
                        confirmation_start_ms=confirmation.start_ms,
                        confirmation_end_ms=confirmation_end,
                        entry=entry, stop=stop, target=target, rr=rr,
                        expires_at_ms=confirmation_end + int(config["entry_valid_seconds"]) * 1000,
                        confluence_score=81,
                        probability=config["probability_label"],
                    )
    return None


def fetch_closed_market(client: BybitClient, config: dict) -> tuple[int, dict[str, list[Candle]]]:
    now_ms = client.server_time_ms()
    series: dict[str, list[Candle]] = {}
    for key in ("context", "structure", "trigger"):
        interval = config["intervals"][key]
        raw = client.klines(config["symbol"], interval, int(config["history_limits"][key]))
        closed = closed_only(raw, interval, now_ms, int(config["closed_candle_grace_seconds"]))
        validate_series(
            closed, interval, now_ms,
            int(config["minimum_closed_candles"][key]),
            int(config["maximum_age_seconds"][key]),
        )
        series[key] = closed
    return now_ms, series


def final_entry_check(client: BybitClient, signal: EntrySignal, config: dict) -> tuple[int, float]:
    now_ms = client.server_time_ms()
    if now_ms > signal.expires_at_ms:
        raise MonitorError("Semnal expirat inainte de trimitere")
    age_ms = now_ms - signal.confirmation_end_ms
    if age_ms < 0 or age_ms > int(config["entry_valid_seconds"]) * 1000:
        raise MonitorError("Confirmarea nu mai este proaspata")
    price = client.last_price(signal.symbol)
    drift = abs(price - signal.entry) / signal.entry * 100
    if drift > float(config["maximum_entry_drift_percent"]):
        raise MonitorError(f"Pretul s-a deplasat prea mult: drift={drift:.3f}%")
    return now_ms, price


def format_entry_alert(signal: EntrySignal, live_price: float) -> str:
    return (
        f"ENTRY CONFIRMAT - {signal.symbol} {signal.direction}\n"
        f"Entry confirmare: {signal.entry:,.2f}\n"
        f"Pret verificat: {live_price:,.2f}\n"
        f"SL structural: {signal.stop:,.2f}\n"
        f"TP structural: {signal.target:,.2f}\n"
        f"R:R: {signal.rr:.2f}R\n"
        f"Confirmare 15m inchisa: {utc_iso(signal.confirmation_end_ms)}\n"
        f"Expira: {utc_iso(signal.expires_at_ms)}\n"
        f"Scor confluenta: {signal.confluence_score}/100 (nu este probabilitate)\n"
        f"Probabilitate: {signal.probability}\n"
        "Date: Bybit OHLC inchis 4H/1H/15m. Fara heatmap/OI/CVD. Decizie manuala."
    )


def scan_symbol(
    root: Path,
    client: BybitClient,
    dry_run: bool,
    config: dict,
    rules: dict,
    dedup: DedupStore,
    sender: Callable[[str], None] | None = None,
) -> dict:
    detected_at_ms, series = fetch_closed_market(client, config)
    signal = detect_entry(series["context"], series["structure"], series["trigger"], rules, config)
    if signal is None:
        return {"status": "NO_ENTRY", "detected_at": utc_iso(detected_at_ms)}
    checked_at_ms, live_price = final_entry_check(client, signal, config)
    if dedup.contains(signal.signature, checked_at_ms):
        return {"status": "DUPLICATE", "signature": signal.signature[:12]}
    message = format_entry_alert(signal, live_price)
    result = {
        "status": "ENTRY_READY_DRY_RUN" if dry_run else "ENTRY_READY",
        "checked_at": utc_iso(checked_at_ms),
        "live_price": live_price,
        "signal": asdict(signal),
        "signature": signal.signature[:12],
        "message": message,
    }
    if not dry_run:
        if sender is None:
            raise MonitorError("Sender lipsa")
        sender(message)
        dedup.mark(signal.signature, checked_at_ms)
        result["status"] = "SENT"
    return result


def scan_once(
    root: Path,
    client: BybitClient,
    dry_run: bool,
    sender: Callable[[str], None] | None = None,
) -> dict:
    config = read_json(package_dir(root) / "config-v0.1.json")
    rules = read_json(package_dir(root) / "rules-v0.1.json")
    if config.get("rules_version") != rules.get("rules_version"):
        raise MonitorError("Versiunea monitorului nu corespunde regulilor")
    symbols = config.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise MonitorError("Lista symbols lipseste sau este goala")
    dedup = DedupStore(state_dir(root) / "dedup.json", int(config["dedup_ttl_hours"]))
    results = []
    for raw_symbol in symbols:
        symbol = str(raw_symbol).upper().strip()
        if not symbol:
            continue
        symbol_config = dict(config)
        symbol_config["symbol"] = symbol
        results.append(scan_symbol(root, client, dry_run, symbol_config, rules, dedup, sender))
    if not results:
        raise MonitorError("Lista symbols nu contine simboluri valide")
    statuses = [result["status"] for result in results]
    if "SENT" in statuses:
        status = "SENT"
    elif "ENTRY_READY_DRY_RUN" in statuses:
        status = "ENTRY_READY_DRY_RUN"
    elif "DUPLICATE" in statuses:
        status = "DUPLICATE"
    else:
        status = "NO_ENTRY"
    return {"status": status, "results": results}


def write_github_outputs(result: dict) -> None:
    output_path = os.getenv("GITHUB_OUTPUT")
    if not output_path:
        return
    status = str(result.get("status", "UNKNOWN"))
    with Path(output_path).open("a", encoding="utf-8") as handle:
        handle.write(f"status={status}\n")
        handle.write(f"alert_sent={'true' if status == 'SENT' else 'false'}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI Crypto Trader entry-only monitor")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("scan", "run"):
        command = sub.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        mode = command.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true")
        mode.add_argument("--send", action="store_true")
        command.add_argument("--env-file", type=Path)
    verify = sub.add_parser("verify-telegram")
    verify.add_argument("--root", type=Path, required=True)
    verify.add_argument("--env-file", type=Path, required=True)
    verify.add_argument("--accept", action="store_true")
    verify.add_argument("--expected-username")
    verify.add_argument("--expected-destination-fingerprint")
    test_message = sub.add_parser("test-telegram")
    test_message.add_argument("--root", type=Path, required=True)
    test_message.add_argument("--env-file", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        if args.command == "verify-telegram":
            result = verify_telegram(
                args.root.resolve(), args.env_file.resolve(), args.accept,
                args.expected_username, args.expected_destination_fingerprint,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "test-telegram":
            print(json.dumps(send_technical_test(args.root.resolve(), args.env_file.resolve()), ensure_ascii=False, indent=2))
            return 0
        root = args.root.resolve()
        dry_run = not args.send
        sender = None
        if args.send:
            env_file = args.env_file.resolve() if args.env_file else None
            token, chat_id = credentials(env_file)
            config = read_json(package_dir(root) / "config-v0.1.json")
            require_delivery_identity(root, token, chat_id, config)
            sender = lambda message: telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": message})
        client = BybitClient()
        if args.command == "scan":
            result = scan_once(root, client, dry_run, sender)
            write_github_outputs(result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        interval = int(read_json(package_dir(root) / "config-v0.1.json")["scan_interval_seconds"])
        while True:
            try:
                result = scan_once(root, client, dry_run, sender)
                LOGGER.info("scan_status=%s", result["status"])
                if dry_run and result["status"] == "ENTRY_READY_DRY_RUN":
                    print(json.dumps(result, ensure_ascii=False, indent=2))
            except MonitorError as exc:
                LOGGER.error("scan_error=%s", exc)
            time.sleep(interval)
    except (MonitorError, TelegramError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
