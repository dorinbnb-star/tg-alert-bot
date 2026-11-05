"""BTC/ETH candidate alerts and paper outcomes; public data only."""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

from backtest import v2_swing as swing
from . import entry_monitor as monitor

SYMBOLS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
H, DAY = swing.H, swing.DAY
LOOKBACK = 60 * DAY
CSV_FIELDS = ("data", "oră", "monedă", "direcție", "nivel-cheie", "intrare de referință",
              "SL", "TP 2R", "TP structural", "R:R", "trend Daily", "status", "rezultat paper", "R paper")


@dataclass(frozen=True)
class Candidate:
    symbol: str
    sign: int
    pivot_ms: int
    sweep_ms: int
    confirmation_ms: int
    level: float
    sweep: float
    entry: float
    stop: float
    tp: float
    structural: float | None
    daily: str
    tick: str

    @property
    def key(self):
        return f"{self.symbol}:{self.sign}:{self.pivot_ms}"

    @property
    def direction(self):
        return "LONG" if self.sign > 0 else "SHORT"

    @property
    def coin(self):
        return self.symbol.split("-")[0]

    @property
    def risk(self):
        return abs(self.entry - self.stop)

    def price(self, value):
        return "indisponibil" if value is None else monitor.price_with_tick(value, self.tick)

    @property
    def rr(self):
        return None if self.structural is None else abs(self.structural - self.entry) / self.risk


def candidate_message(c):
    rr = "indisponibil" if c.rr is None else f"{c.rr:.2f}"
    return "\n".join((
        f"🔔 CANDIDAT {c.coin} {c.direction} — nevalidat",
        f"Nivel-cheie (pivot 4H): {c.price(c.level)}",
        f"Sweep: {c.price(c.sweep)} la {monitor.local_hm(c.sweep_ms)} | Confirmare 1H: {monitor.local_hm(c.confirmation_ms)}",
        f"Intrare de referință: {c.price(c.entry)} (close-ul confirmării)",
        f"SL: {c.price(c.stop)} (maximul dintre SL structural și 1,5×ATR14 1H)",
        f"TP 2R: {c.price(c.tp)}",
        f"TP structural (primul pivot 4H opus): {c.price(c.structural)} (R:R {rr})",
        f"Trend Daily: {c.daily}",
        "Decizia e a ta. Notează în jurnal: intrat / refuzat + motiv.",
    ))


def detect(symbol, h1, h4, daily, tick, start, now):
    def closed(candles, span):
        return [swing.Candle(bar.start_ms, bar.open, bar.high, bar.low, bar.close, bar.volume)
                for bar in candles if bar.start_ms + span <= now]
    data = SimpleNamespace(h1=closed(h1, H), h4=closed(h4, 4 * H), daily=closed(daily, DAY), funding=[])
    prepared = swing.Prepared(data)
    found, _ = swing.signals(symbol, prepared, start, now + 1, daily_filter=False)
    closes = {bar.start_ms + H: bar.close for bar in data.h1}
    out = []
    for signal in found:
        sign, size = signal.sign, float(tick)
        entry = closes[signal.time]
        structural_stop = signal.extreme - sign * max(.1 * signal.atr, 2 * size)
        distance = max(sign * (entry - structural_stop), 1.5 * signal.atr)
        stop = swing.tick_price(entry - sign * distance, size, upward=sign < 0)
        target = swing.tick_price(entry + 2 * (entry - stop), size, upward=sign > 0)
        if min(stop, target) <= 0:
            raise monitor.MonitorError(f"Nivel paper invalid: {symbol}")
        level = signal.reference_target
        structural = None if level is None else swing.tick_price(level, size, upward=sign < 0)
        if structural is not None and sign * (structural - entry) <= 0:
            structural = None
        context = signal.daily
        if context:
            close = context["daily_close"]
            fmt = lambda value: monitor.price_with_tick(value, tick)
            cmp = lambda value: ">" if close > value else "<" if close < value else "="
            fast, slow = context["daily_ema50"], context["daily_ema200"]
            description = f"preț {fmt(close)} {cmp(fast)} EMA50 {fmt(fast)} / {cmp(slow)} EMA200 {fmt(slow)}"
        else:
            description = "preț vs EMA50 / EMA200 indisponibil"
        out.append(Candidate(symbol, sign, signal.pivot_ms, signal.sweep_ms, signal.time,
                             signal.level, signal.extreme, entry, stop, target, structural, description, tick))
    return out


def paper_result(candidate, candles, now):
    cursor = candidate.confirmation_ms
    expires = cursor + 7 * DAY
    for bar in candles:
        if bar.start_ms < cursor:
            continue
        close_ms = bar.start_ms + H
        if bar.start_ms != cursor or close_ms > now:
            return None
        sign = candidate.sign
        adverse, favorable = (bar.low, bar.high) if sign > 0 else (bar.high, bar.low)
        if sign * (adverse - candidate.stop) <= 0:
            price = min(bar.open, candidate.stop) if sign > 0 else max(bar.open, candidate.stop)
            return close_ms, "SL ATINS", sign * (price - candidate.entry) / candidate.risk
        if sign * (favorable - candidate.tp) >= 0:
            return close_ms, "2R ATINS", abs(candidate.tp - candidate.entry) / candidate.risk
        if close_ms >= expires:
            return close_ms, "TIMEOUT", sign * (bar.close - candidate.entry) / candidate.risk
        cursor = close_ms
    return None


def journal_row(c, timestamp, status, result="", r=""):
    local = datetime.fromtimestamp(timestamp / 1000, monitor.BRUSSELS)
    return (local.date().isoformat(), local.strftime("%H:%M"), c.coin, c.direction,
            c.price(c.level), c.price(c.entry), c.price(c.stop), c.price(c.tp),
            "" if c.structural is None else c.price(c.structural), "" if c.rr is None else f"{c.rr:.2f}",
            c.daily, status, result, r)


def write_journal(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for item in state.values():
        c = Candidate(**item["candidate"])
        rows.append((c.confirmation_ms, journal_row(c, c.confirmation_ms, item["status"])))
        if item.get("result"):
            when, result, r = item["result"]
            rows.append((when, journal_row(c, when, "PAPER", result, f"{r:.6f}")))
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_FIELDS)
        writer.writerows(row for _, row in sorted(rows))
    temporary.replace(path)


def update(symbol, candidates, candles, now, state, persist, sender=None, historical=False):
    messages = []
    for candidate in candidates:
        if candidate.key in state:
            continue
        if not historical and now - candidate.confirmation_ms > 2 * H:
            status = "MISSED_STALE"
        else:
            message = candidate_message(candidate)
            if sender is not None:
                sender(message)
            messages.append(message)
            status = "SENT" if sender is not None else "CANDIDAT_DRY_RUN"
        state[candidate.key] = {"candidate": asdict(candidate), "status": status}
        persist()
    for item in state.values():
        if item["candidate"]["symbol"] != symbol or item.get("result"):
            continue
        candidate = Candidate(**item["candidate"])
        result = paper_result(candidate, candles, now)
        if result is None:
            continue
        if item["status"] == "SENT" and sender is not None:
            sender(f"📊 CANDIDAT {candidate.coin} {candidate.direction}: {result[1]} (paper)")
        item["result"] = result
        persist()
    return messages


class PublicHistory:
    def __init__(self, client):
        self.client = client
        self.last_request = 0.0

    def candles(self, symbol, bar, span, start, end):
        cursor, found = end, {}
        while cursor > start:
            time.sleep(max(0, self.last_request + .15 - time.monotonic()))
            self.last_request = time.monotonic()
            payload = self.client._get("/api/v5/market/history-candles", {
                "instId": symbol, "bar": bar, "after": str(cursor), "limit": "300",
            })
            rows = monitor.okx_data(payload, f"history-candles {bar}")
            if not rows:
                break
            for candle in monitor.parse_okx_candles(rows, bar):
                if candle.start_ms >= start and candle.start_ms + span <= end:
                    found[candle.start_ms] = candle
            oldest = int(rows[-1][0])
            if oldest >= cursor:
                raise monitor.MonitorError(f"Paginare OKX invalidă: {symbol} {bar}")
            cursor = oldest
        return [found[key] for key in sorted(found)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--send", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--historical", action="store_true", help="60-day replay, never sends")
    parser.add_argument("--as-of-ms", type=int, help="Fixed UTC end for historical replay")
    args = parser.parse_args(argv)
    if args.as_of_ms is not None and not args.historical:
        parser.error("--as-of-ms requires --historical")
    if args.send and (os.getenv("GITHUB_ACTIONS") != "true" or os.getenv("GITHUB_REF") != "refs/heads/main"
                      or os.getenv("GITHUB_RUN_ATTEMPT") != "1"
                      or not (os.getenv("GITHUB_EVENT_NAME") == "schedule"
                              or os.getenv("GITHUB_TRIGGERING_ACTOR") == "github-actions[bot]")):
        parser.error("Telegram este permis numai rulărilor live pe main")
    root = args.root.resolve()
    directory = monitor.state_dir(root)
    state_file = directory / "candidate-dedup.json"
    journal = directory / ("candidate-journal.csv" if args.send else "candidate-dry-run.csv")
    if args.send and journal.exists() and not state_file.exists():
        print("ERROR: Jurnalul există, dar starea de deduplicare lipsește. Telegram oprit.", file=sys.stderr)
        return 2
    state = monitor.read_json(state_file) if args.send and state_file.exists() else {}
    sender = None
    try:
        if args.send:
            token, chat_id = monitor.credentials(None)
            config = monitor.read_json(monitor.package_dir(root) / "config-v0.1.json")
            verified = False

            def sender(message):
                nonlocal verified
                if not verified:
                    monitor.require_delivery_identity(root, token, chat_id, config)
                    verified = True
                monitor.telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": message})

            if not state_file.exists():
                monitor.write_json(state_file, state)

        def persist():
            if args.send:
                monitor.write_json(state_file, state)

        client = monitor.OkxClient()
        now = client.server_time_ms() if args.as_of_ms is None else args.as_of_ms
        ticks = client.tick_sizes()
        history = PublicHistory(client)
        errors = []
        for symbol in SYMBOLS:
            try:
                h1 = history.candles(symbol, "1H", H, now - LOOKBACK - 2 * DAY, now)
                h4 = history.candles(symbol, "4H", 4 * H, now - LOOKBACK - 30 * DAY, now)
                daily = history.candles(symbol, "1Dutc", DAY, now - LOOKBACK - 220 * DAY, now)
                if not h1 or not h4 or symbol not in ticks:
                    raise monitor.MonitorError(f"Date OKX incomplete: {symbol}")
                start = now - LOOKBACK
                found = detect(symbol, h1, h4, daily, ticks[symbol], start - 4 * H, now)
                found = [c for c in found if c.confirmation_ms >= start]
                if args.historical:
                    for candidate in found:
                        print(f"\n{monitor.utc_iso(candidate.confirmation_ms)} | {candidate.symbol}")
                        print(candidate_message(candidate))
                update(symbol, found, h1, now, state, persist, sender, args.historical)
                current = [item for item in state.values() if item["candidate"]["symbol"] == symbol]
                stale = sum(item["status"] == "MISSED_STALE" for item in current)
                monitor.append_diagnostic({"candidates": len(found), "MISSED_STALE": stale}, "CANDIDATE_SCAN", symbol)
                print(f"{symbol}: {len(found)} candidați, {stale} MISSED_STALE în jurnal")
            except monitor.TelegramError:
                raise
            except monitor.MonitorError as exc:
                errors.append(str(exc))
                monitor.symbol_scan_error(symbol, str(exc))
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2 if errors else 0
    except monitor.MonitorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        write_journal(journal, state)


if __name__ == "__main__":
    raise SystemExit(main())
