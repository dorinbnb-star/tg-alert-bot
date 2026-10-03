#!/usr/bin/env python3
"""Replay the live scan on the live cadence and simulate the trades of every variant.

detect  For each closed 15m candle C the live chain scans at C+45 s (closes at :00/:30) or C+345 s (:15/:45);
        the other ticks (:10:45, :40:45) can only see an expired confirmation. At each scan time T the replay
        client feeds the real `fetch_closed_market` (same windows, same closed_only/validate_series) and the
        real `detect_entry` (with trace for the stage distribution).
trades  Real `final_entry_check` (expiry + 0.15% drift against the price at T) and the real `DedupStore`
        (signature, 168 h TTL), then the exit path with costs. Modes: main (08:00-22:00 Brussels, every
        alert independent), 24h, one open position per symbol.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import dataclasses
import json
import math
import multiprocessing
import re
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_crypto_monitor import entry_monitor as em  # noqa: E402
from backtest import download, variants  # noqa: E402
from backtest.replay import ReplayOkxClient, load_series, parse_rows  # noqa: E402

MINUTE = 60_000
QUARTER = 15 * MINUTE
HOUR = 60 * MINUTE
COSTS_PATH = Path(__file__).resolve().parent / "costs.json"
MODES = ("main", "24h", "onepos")


# ---------------------------------------------------------------- cadence

def scan_time(close_ms: int) -> int:
    """First live tick (:x0:45 grid) after the 15m close: +45 s at :00/:30, +345 s at :15/:45."""
    return close_ms + (45_000 if (close_ms // MINUTE) % 30 == 0 else 345_000)


def scan_grid(start_ms: int, end_ms: int) -> list[int]:
    first_close = -(-start_ms // QUARTER) * QUARTER
    times = [scan_time(close) for close in range(first_close, end_ms, QUARTER)]
    return [t for t in times if start_ms <= t < end_ms]


def in_live_window(scan_ms: int) -> bool:
    return 8 <= datetime.fromtimestamp(scan_ms / 1000, tz=em.BRUSSELS).hour < 22


def reason_key(exc: Exception) -> str:
    return re.sub(r"\d+(\.\d+)?", "N", str(exc))


# ---------------------------------------------------------------- detect

def detect_symbol(job: tuple[str, str, int, int]) -> dict:
    data_dir, symbol, start_ms, end_ms = job
    root = Path(data_dir)
    config = dict(download.load_config(), symbol=symbol)
    rules = download.load_rules()
    series = {config["intervals"][key]: load_series(root, symbol, config["intervals"][key])
              for key in ("context", "structure", "trigger")}
    client = ReplayOkxClient(series)
    detections = []
    stages = {variant: {"main": Counter(), "24h": Counter()} for variant in variants.DETECTED_VARIANTS}
    errors = {"main": Counter(), "24h": Counter()}
    scans = Counter()
    trace_mismatch = 0
    for scan_ms in scan_grid(start_ms, end_ms):
        window = in_live_window(scan_ms)
        scans["24h"] += 1
        scans["main"] += window
        client.at(scan_ms)
        try:
            _, market = em.fetch_closed_market(client, config)
        except em.MonitorError as exc:
            key = reason_key(exc)
            errors["24h"][key] += 1
            if window:
                errors["main"][key] += 1
            continue
        for variant in variants.DETECTED_VARIANTS:
            trace: dict = {}
            signal = variants.detect(variant, market, rules, config, trace)
            stage = trace.get("stopped_at") or "ENTRY"
            stages[variant]["24h"][stage] += 1
            if window:
                stages[variant]["main"][stage] += 1
            if signal is None:
                continue
            row = {"symbol": symbol, "variant": variant, "scan_ms": scan_ms, "in_window": window,
                   "signal": dataclasses.asdict(signal)}
            if variant == "a":
                second = variants.c1_target(signal, market, rules)
                row["c1_target"] = second
                if second != trace.get("shadow_second_target"):
                    trace_mismatch += 1
            detections.append(row)
    return {
        "symbol": symbol,
        "detections": detections,
        "stages": {v: {m: dict(c) for m, c in item.items()} for v, item in stages.items()},
        "errors": {m: dict(c) for m, c in errors.items()},
        "scans": dict(scans),
        "c1_trace_mismatch": trace_mismatch,
    }


def run_pool(function, jobs: list, processes: int) -> list:
    if processes <= 1:
        return [function(job) for job in jobs]
    with multiprocessing.get_context("fork").Pool(processes) as pool:
        return pool.map(function, jobs, chunksize=1)


def command_detect(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    start_ms, end_ms = download.parse_utc(args.start), download.parse_utc(args.end)
    jobs = [(args.data_dir, symbol, start_ms, end_ms) for symbol in download.symbols()]
    results = run_pool(detect_symbol, jobs, args.processes)
    with (out / "detections.jsonl").open("w", encoding="utf-8") as handle:
        for result in results:
            for row in sorted(result["detections"], key=lambda r: (r["scan_ms"], r["variant"])):
                handle.write(json.dumps(row, sort_keys=True) + "\n")
    summary = {result["symbol"]: {key: result[key] for key in ("stages", "errors", "scans", "c1_trace_mismatch")}
               for result in results}
    (out / "detect_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    total = sum(len(result["detections"]) for result in results)
    print(f"detect: {len(results)} simboluri, {total} detectii (toate variantele)", flush=True)
    return 0


# ---------------------------------------------------------------- exits and costs

@dataclasses.dataclass(frozen=True)
class ExitPath:
    outcome: str  # TP, SL, OPEN
    price: float
    exit_ms: int
    ambiguous: bool
    gap: bool
    minute_fallback: bool


def check_candle(candle: em.Candle, direction: str, stop: float, target: float, end_ms: int,
                 stop_only: bool) -> ExitPath | None:
    long = direction == "LONG"
    stop_hit = candle.low <= stop if long else candle.high >= stop
    target_hit = (candle.high >= target if long else candle.low <= target) and not stop_only
    if stop_hit:  # TP and SL in the same candle count as SL
        gap = candle.open < stop if long else candle.open > stop
        return ExitPath("SL", candle.open if gap else stop, end_ms, bool(target_hit), gap, False)
    if target_hit:
        return ExitPath("TP", target, end_ms, False, False, False)
    return None


def find_exit(direction: str, stop: float, target: float, scan_ms: int, minute: list[em.Candle],
              minute_starts: list[int], quarter: list[em.Candle], quarter_starts: list[int], end_ms: int,
              entry_price: float) -> ExitPath:
    """1m from the minute containing T to the end of the 15m candle containing T, then closed 15m candles.

    The minute (or, without 1m data, the 15m candle) that contains T started before the entry, so it can only
    stop the trade out, never take profit (conservative).
    """
    block = (scan_ms // QUARTER) * QUARTER
    block_end = block + QUARTER
    first_minute = (scan_ms // MINUTE) * MINUTE
    index = bisect.bisect_left(minute_starts, first_minute)
    minutes = minute[index:bisect.bisect_left(minute_starts, block_end)]
    expected = list(range(first_minute, block_end, MINUTE))
    fallback = [candle.start_ms for candle in minutes] != expected
    if not fallback:
        for candle in minutes:
            hit = check_candle(candle, direction, stop, target, candle.start_ms + MINUTE,
                               stop_only=candle.start_ms <= scan_ms)
            if hit:
                return hit
    else:
        position = bisect.bisect_left(quarter_starts, block)
        if position < len(quarter) and quarter[position].start_ms == block:
            hit = check_candle(quarter[position], direction, stop, target, block_end, stop_only=True)
            if hit:
                return dataclasses.replace(hit, minute_fallback=True)
    last_price = entry_price
    for position in range(bisect.bisect_left(quarter_starts, block_end), len(quarter)):
        candle = quarter[position]
        if candle.start_ms + QUARTER > end_ms:
            break
        hit = check_candle(candle, direction, stop, target, candle.start_ms + QUARTER, stop_only=False)
        if hit:
            return dataclasses.replace(hit, minute_fallback=fallback)
        last_price = candle.close
    return ExitPath("OPEN", last_price, end_ms, False, False, fallback)


class Funding:
    """Real OKX funding where the API returns it; before that a conservative ESTIMARE charged against the position."""

    def __init__(self, items: list[dict], settings: dict):
        self.real: list[tuple[int, float]] = []
        for item in items:
            raw = item.get("realizedRate") or item.get("fundingRate")
            if raw in (None, ""):
                continue
            self.real.append((int(item["fundingTime"]), float(raw)))
        self.real_times = [ts for ts, _ in self.real]
        rates = sorted(abs(rate) for _, rate in self.real)
        quantile = float(settings["quantile_of_abs_real_rates"])
        observed = rates[min(len(rates) - 1, int(math.floor(quantile * (len(rates) - 1))))] if rates else 0.0
        self.estimate_rate = max(float(settings["floor_rate"]), observed)
        default_step = int(settings["default_interval_hours"]) * HOUR
        if len(self.real) >= 2:
            self.estimate_step = self.real[1][0] - self.real[0][0]
            self.real_start = self.real[0][0]
        else:
            self.estimate_step = default_step
            self.real_start = None

    def events(self, start_ms: int, end_ms: int) -> tuple[list[float], int]:
        """Real rates with start < t < end, and how many estimated funding times fall in that span."""
        low = bisect.bisect_right(self.real_times, start_ms)
        high = bisect.bisect_left(self.real_times, end_ms)
        real = [rate for _, rate in self.real[low:high]]
        # Estimated times: real_start - n*step (n >= 1) before the real history, or an 8h UTC grid without it.
        anchor = self.real_start if self.real_start is not None else 0
        n = (anchor - end_ms) // self.estimate_step + 1
        if self.real_start is not None:
            n = max(n, 1)
        estimated = 0
        while anchor - n * self.estimate_step > start_ms:
            estimated += 1
            n += 1
        return real, estimated


def trade_result(signal: em.EntrySignal, scan_ms: int, price_at_t: float, path: ExitPath, funding: Funding,
                 costs: dict, multiplier: float) -> dict:
    sign = 1 if signal.direction == "LONG" else -1
    slip = float(costs["slippage_rate"].get(signal.symbol, costs["slippage_rate"]["default"])) * multiplier
    fee = float(costs["taker_fee_rate"]) * multiplier
    entry_fill = price_at_t * (1 + sign * slip)
    exit_slip = slip * (float(costs["stop_slippage_multiplier"]) if path.outcome == "SL" else 1)
    exit_fill = path.price * (1 - sign * exit_slip)
    risk = abs(signal.entry - signal.stop)  # 1R = planned |entry - SL|
    gross = sign * (exit_fill - entry_fill)
    fees = fee * (entry_fill + exit_fill)
    real_rates, estimated = funding.events(scan_ms, path.exit_ms)
    real_cost = 0.0
    for rate in real_rates:
        cost = sign * rate * entry_fill  # long pays a positive rate, short receives it
        real_cost += cost * multiplier if cost > 0 else cost
    estimated_cost = estimated * funding.estimate_rate * entry_fill * multiplier
    net = gross - fees - real_cost - estimated_cost
    return {
        "r": net / risk,
        "fee_r": fees / risk,
        "slippage_r": (abs(entry_fill - price_at_t) + abs(exit_fill - path.price)) / risk,
        "funding_real_r": real_cost / risk,
        "funding_est_r": estimated_cost / risk,
        "funding_est_n": estimated,
        "funding_real_n": len(real_rates),
    }


# ---------------------------------------------------------------- trades

def load_detections(path: Path) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            grouped[row["symbol"]].append(row)
    return grouped


def variant_signal(variant: str, row: dict) -> em.EntrySignal | None:
    signal = em.EntrySignal(**row["signal"])
    if variant != "c1":
        return signal
    target = row.get("c1_target")
    if target is None:
        return None
    reward = target - signal.entry if signal.direction == "LONG" else signal.entry - target
    return dataclasses.replace(signal, target=target, rr=reward / abs(signal.entry - signal.stop))


def trades_symbol(job: tuple[str, str, list[dict], int, int, int]) -> dict:
    data_dir, symbol, rows, start_ms, split_ms, end_ms = job
    root = Path(data_dir)
    config = dict(download.load_config(), symbol=symbol)
    costs = json.loads(COSTS_PATH.read_text(encoding="utf-8"))
    quarter = load_series(root, symbol, "15")
    quarter_starts = [candle.start_ms for candle in quarter]
    minute = parse_rows(download.read_rows(download.series_path(root, symbol, "1m")), "1")
    minute_starts = [candle.start_ms for candle in minute]
    funding = Funding(download.read_rows(download.series_path(root, symbol, "funding")), costs["funding_estimate"])
    client = ReplayOkxClient({}, minute)
    by_variant = {variant: sorted((row for row in rows if row["variant"] == ("a" if variant == "c1" else variant)),
                                  key=lambda row: row["scan_ms"])
                  for variant in variants.TRADED_VARIANTS}
    trades = []
    counters: dict[str, dict[str, Counter]] = {v: {m: Counter() for m in MODES} for v in variants.TRADED_VARIANTS}
    with tempfile.TemporaryDirectory() as state:
        for variant in variants.TRADED_VARIANTS:
            for mode in MODES:
                count = counters[variant][mode]
                dedup = em.DedupStore(Path(state) / f"{variant}-{mode}.json", int(config["dedup_ttl_hours"]))
                busy_until = -1
                for row in by_variant[variant]:
                    scan_ms = int(row["scan_ms"])
                    if mode != "24h" and not row["in_window"]:
                        continue
                    count["detected"] += 1
                    signal = variant_signal(variant, row)
                    if signal is None:
                        count["no_second_pivot"] += 1
                        continue
                    try:
                        checked_at, price_at_t = em.final_entry_check(client.at(scan_ms), signal, config)
                    except em.EntrySkipped as exc:
                        count["skipped:" + reason_key(exc)] += 1
                        continue
                    except em.MonitorError as exc:
                        count["error:" + reason_key(exc)] += 1
                        continue
                    if dedup.contains(signal.signature, checked_at):
                        count["duplicate"] += 1
                        continue
                    if mode == "onepos" and scan_ms < busy_until:
                        count["position_open"] += 1
                        continue
                    dedup.mark(signal.signature, checked_at)
                    path = find_exit(signal.direction, signal.stop, signal.target, scan_ms, minute, minute_starts,
                                     quarter, quarter_starts, end_ms, price_at_t)
                    busy_until = path.exit_ms if path.outcome != "OPEN" else end_ms + 1
                    count["traded"] += 1
                    base = trade_result(signal, scan_ms, price_at_t, path, funding, costs, 1.0)
                    double = trade_result(signal, scan_ms, price_at_t, path, funding, costs,
                                          float(costs["sensitivity_multiplier"]))
                    trades.append({
                        "variant": variant, "mode": mode, "symbol": symbol, "direction": signal.direction,
                        "period": "8m" if scan_ms < split_ms else "4m",
                        "scan_utc": download.iso(scan_ms), "scan_ms": scan_ms,
                        "exit_utc": download.iso(path.exit_ms), "exit_ms": path.exit_ms,
                        "outcome": path.outcome, "ambiguous_sl": int(path.ambiguous), "gap": int(path.gap),
                        "minute_fallback": int(path.minute_fallback),
                        "entry_signal": signal.entry, "price_at_scan": price_at_t, "stop": signal.stop,
                        "target": signal.target, "rr_planned": round(signal.rr, 6), "exit_price": path.price,
                        "r": round(base["r"], 6), "r_cost_x2": round(double["r"], 6),
                        "fee_r": round(base["fee_r"], 6), "slippage_r": round(base["slippage_r"], 6),
                        "funding_real_r": round(base["funding_real_r"], 6),
                        "funding_est_r": round(base["funding_est_r"], 6),
                        "funding_est_n": base["funding_est_n"], "funding_real_n": base["funding_real_n"],
                        "signature": signal.signature[:12],
                    })
    return {
        "symbol": symbol,
        "trades": trades,
        "counters": {v: {m: dict(c) for m, c in item.items()} for v, item in counters.items()},
        "funding": {"estimate_rate": funding.estimate_rate, "estimate_step_hours": funding.estimate_step / HOUR,
                    "real_count": len(funding.real),
                    "real_first": download.iso(funding.real[0][0]) if funding.real else None,
                    "real_last": download.iso(funding.real[-1][0]) if funding.real else None},
    }


TRADE_FIELDS = [
    "variant", "mode", "symbol", "direction", "period", "scan_utc", "scan_ms", "exit_utc", "exit_ms", "outcome",
    "ambiguous_sl", "gap", "minute_fallback", "entry_signal", "price_at_scan", "stop", "target", "rr_planned",
    "exit_price", "r", "r_cost_x2", "fee_r", "slippage_r", "funding_real_r", "funding_est_r", "funding_est_n",
    "funding_real_n", "signature",
]


def command_trades(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    grouped = load_detections(Path(args.detections))
    start_ms, split_ms, end_ms = (download.parse_utc(x) for x in (args.start, args.split, args.end))
    jobs = [(args.data_dir, symbol, grouped.get(symbol, []), start_ms, split_ms, end_ms)
            for symbol in download.symbols()]
    results = run_pool(trades_symbol, jobs, args.processes)
    trades = [trade for result in results for trade in result["trades"]]
    trades.sort(key=lambda t: (t["variant"], t["mode"], t["scan_ms"], t["symbol"]))
    with (out / "trades.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRADE_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(trades)
    summary = {result["symbol"]: {"counters": result["counters"], "funding": result["funding"]} for result in results}
    (out / "trade_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"trades: {len(trades)} randuri (toate variantele si modurile)", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default=str(download.DATA_DIR))
    parser.add_argument("--out", default=str(download.RESULTS_DIR))
    parser.add_argument("--processes", type=int, default=max(1, (multiprocessing.cpu_count() or 1)))
    parser.add_argument("--start", default=download.START)
    parser.add_argument("--split", default=download.SPLIT)
    parser.add_argument("--end", default=download.END)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("detect")
    trades = sub.add_parser("trades")
    trades.add_argument("--detections", default=str(download.RESULTS_DIR / "detections.jsonl"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return {"detect": command_detect, "trades": command_trades}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
