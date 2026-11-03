"""Visual audit of saved v3 results from Actions run 41682660630.

Usage (matplotlib is required only for rendering)::

    python -m backtest.v3_charts --data-dir PATH --results-dir PATH

This module never downloads data, runs the engine, or evaluates trade outcomes.
It checks the original CSV fingerprints and decompressed candle SHA256 values.
Only channel geometry/pivot membership is reconstructed using the unchanged
pure strategy module, then checked against the saved setup. All trade events,
levels and results come from the CSVs. Exit prices are recovered algebraically
from saved PnL, not simulated. PNG metadata and stdout retain source row IDs.
CSV row IDs are 1-based data records (the header is not counted).
ATR period 14 is inherited from channel_before(), not from the configuration.
"""

from __future__ import annotations

import argparse
import ast
from bisect import bisect_right
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import json
from math import isclose
from pathlib import Path

from backtest import v3_strategy as strategy


ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "41682660630"
SOURCE_COMMIT = "ec4e9694cfd86c27a92ae2d7570dbcb494af57db"
VARIANT = "ENTRY_MARKET_NEXT_OPEN__REF_RR2"
TRADE_RANKS = (1, 6, 11, 16, 21, 26, 31, 36, 41, 46)
DEFAULT_OUTPUT = Path(
    r"C:\Users\User\.codex\.chatgpt-projects"
    r"\g-p-6a946d84ecc881919657e77f627031ee\v3-results-audit\charts"
)
CSV_SHA256 = {
    "v3-trades.csv": "4585f7f325cbde87ce62bfbbcbebc7593f60aaa13883bb5e70ad5c0222c18015",
    "v3-setups.csv": "508d1ff66493602c776eb1bf49ef6082878b1f070ca3d0ce246dc77f7e1d9459",
}
CODE_SHA256 = {
    "v3_strategy.py": "7cba00080b80b425ea14b0544da122362c762a6a61f275204b0fc91bb3133d26",
    "v3_config.json": "48dd0643db9141e2e562823ce8947f390d50a439751b686ba14460d8cd05843a",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def iso(ms: int) -> str:
    return utc(ms).strftime("%Y-%m-%d %H:%M UTC")


def price(value: float) -> str:
    # Display only; preserve median half-ticks and effective slippage prices.
    return f"{value:.10g}"


def read_csv(path: Path) -> list[dict]:
    require(hashlib.sha256(path.read_bytes()).hexdigest() == CSV_SHA256[path.name],
            f"CSV does not match run {RUN_ID}: {path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row, source_row=index)
                for index, row in enumerate(csv.DictReader(handle), 1)
                if row["variant"] == VARIANT]


def setup_key(row: dict) -> tuple:
    return int(row["breakout_ms"]), row["symbol"], row["channel_id"]


@dataclass(frozen=True)
class Selection:
    number: int
    rank: int
    setup: dict
    trade: dict | None = None

    @property
    def filename(self) -> str:
        short = self.setup["symbol"].removesuffix("-USDT-SWAP")
        day = utc(int(self.setup["breakout_ms"])).strftime("%Y%m%d")
        return f"{self.number:02d}_{short}_{self.setup['direction']}_{day}.png"

    def identity(self) -> dict:
        return {
            "file": self.filename, "rank": self.rank,
            "kind": "trade" if self.trade else "TARGET_BEFORE_ENTRY",
            "trade_csv_row": self.trade["source_row"] if self.trade else None,
            "setup_csv_row": self.setup["source_row"],
            "channel_id": self.setup["channel_id"],
            "entry_ms": int(self.trade["entry_ms"]) if self.trade else None,
            "breakout_utc": iso(int(self.setup["breakout_ms"])),
        }


def select(results_dir: Path) -> tuple[list[Selection], list[dict]]:
    trades = sorted(read_csv(results_dir / "v3-trades.csv"),
                    key=lambda r: (int(r["entry_ms"]), r["symbol"], r["channel_id"]))
    setups = sorted(read_csv(results_dir / "v3-setups.csv"), key=setup_key)
    require(len(trades) == 53, f"Expected 53 trades, found {len(trades)}")
    by_channel = {row["channel_id"]: row for row in setups}
    require(len(by_channel) == len(setups), "Duplicate setup identity")
    require(len({r['channel_id'] for r in trades}) == len(trades), "Duplicate trade identity")
    chosen = []
    for number, rank in enumerate(TRADE_RANKS, 1):
        trade = trades[rank - 1]
        setup = by_channel[trade["channel_id"]]
        require(setup["final_status"] == "FILLED", "Trade without FILLED setup")
        require((setup['symbol'], setup['direction']) == (trade['symbol'], trade['direction']),
                "Trade/setup mismatch")
        chosen.append(Selection(number, rank, setup, trade))
    cancelled = [r for r in setups if r["final_status"] == "TARGET_BEFORE_ENTRY"]
    require(len(cancelled) == 19, f"Expected 19 cancelled setups, found {len(cancelled)}")
    # The actual source has 19 records: ranks 1, 10, 19, no median ambiguity.
    for number, index in enumerate((0, (len(cancelled) - 1) // 2, len(cancelled) - 1), 11):
        chosen.append(Selection(number, index + 1, cancelled[index]))
    return chosen, [r for r in setups if r["symbol"] == "ETH-USDT-SWAP"]


def checked_candles(root: Path, manifest: dict, symbol: str, bar: str) -> list:
    path = root / symbol / f"{bar}.jsonl.gz"
    with gzip.open(path, "rb") as handle:
        raw = handle.read()
    meta = manifest[symbol][bar]
    require(hashlib.sha256(raw).hexdigest() == meta["sha256"], f"Candle SHA256 mismatch: {path}")
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    candles = strategy.candles_from_rows(rows)
    require(len(candles) == len(rows) == meta["count"], f"Invalid/duplicate candles: {path}")
    return candles


def recover_exit(trade: dict) -> tuple[float, float]:
    """Recover raw/effective exit of this single-exit REFERENCE trade."""
    require(trade["exit_family"] == "REFERENCE", "Partial exits are not supported")
    qty = float(trade["qty"])
    require(qty > 0, "Nonpositive quantity")
    sign = 1 if trade["direction"] == "LONG" else -1
    raw = float(trade["entry_raw"]) + sign * float(trade["gross_pnl"]) / qty
    effective = float(trade["entry_effective"]) + sign * (
        float(trade["net_pnl"]) + float(trade["fees"]) - float(trade["funding"])) / qty
    return raw, effective


def prepare(item: Selection, h4: list, h1: list, params: strategy.Parameters) -> dict:
    row, trade = item.setup, item.trade
    breakout_close = int(row["breakout_ms"])
    breakout_start = breakout_close - strategy.FOUR_HOURS_MS
    starts = [c.start_ms for c in h4]
    index = bisect_right(starts, breakout_start) - 1
    n = params.channel_window_4h
    require(index >= n and starts[index] == breakout_start, "Missing breakout/window candles")
    # Only the history before the breakout can determine pivots and ATR(ref).
    channel = strategy.channel_before(row["symbol"], h4[:index + 1], index, params)
    require(channel is not None, f"Cannot reconstruct channel: {row['channel_id']}")
    require(channel.identity == ast.literal_eval(row["channel_id"]), "Channel identity mismatch")
    for field, value in (("upper", channel.upper), ("lower", channel.lower),
                         ("width", channel.width), ("atr_ref", channel.atr_ref)):
        require(isclose(value, float(row[field]), rel_tol=1e-12, abs_tol=1e-12),
                f"Saved {field} differs from reconstructed channel")
    require(all(p.confirmed_ms <= breakout_start for p in channel.high_pivots + channel.low_pivots),
            "Future pivot in channel")
    end_ms = int(trade["exit_ms"] if trade else row["status_ms"])
    # TP/SL/cancellation timestamps represent the end of the detecting 1H bar.
    # TIMEOUT is at open; include that candle instead of the preceding candle.
    event_instant = end_ms if trade and trade["outcome"] == "TIMEOUT" else end_ms - 1
    last_index = bisect_right(starts, event_instant) - 1
    require(last_index >= index, "Final event precedes breakout")
    require(last_index + 10 < len(h4), "Fewer than 10 candles after final event")
    visible = h4[index - n:last_index + 11]
    require(strategy.is_contiguous(visible, strategy.FOUR_HOURS_MS), "Gap inside chart window")
    hours = {c.start_ms: c for c in h1}
    setup = strategy.breakout_setup(channel, h4[index], params)
    require(setup is not None and setup.direction == row['direction'], "Saved breakout mismatch")
    reference = setup.reference_level
    sign = 1 if row["direction"] == "LONG" else -1
    target = setup.reference_tp
    events = []
    if trade:
        require(int(trade["entry_ms"]) == int(trade["confirmation_ms"]),
                "Market-next-open entry/confirmation timestamp mismatch")
        confirmation = hours[int(trade["confirmation_ms"]) - strategy.HOUR_MS]
        entry = hours[int(trade["entry_ms"])]
        require(isclose(entry.open, float(trade["entry_raw"])), "Saved entry does not match 1H open")
        require(isclose(target, float(trade["target"])), "Saved TP differs from reference target")
        exit_raw, exit_effective = recover_exit(trade)
        exit_hour = hours[end_ms if trade['outcome'] == 'TIMEOUT' else end_ms - strategy.HOUR_MS]
        epsilon = max(abs(exit_raw), 1) * 1e-10
        require(exit_hour.low - epsilon <= exit_raw <= exit_hour.high + epsilon,
                "Recovered raw exit outside saved 1H candle")
        events = [
            ("C", "Confirmare 1H (close)", int(trade["confirmation_ms"]), confirmation.close, "#7c3aed", "D"),
            ("E", "Intrare efectivă", int(trade["entry_ms"]), float(trade["entry_effective"]), "#2563eb", ">"),
            ("X", "Ieșire efectivă", end_ms, exit_effective, "#111827", "X"),
        ]
    else:
        # These three selected records have no confirmation or order. Never
        # invent an entry/SL/exit or present a cancelled setup as a zero-R trade.
        require(row["confirmed"] == "False" and row["retested"] == "False",
                "Cancelled setup has events not timestamped by the collapsed CSV")
        cancellation_hour = hours[end_ms - strategy.HOUR_MS]
        require(cancellation_hour.high >= target if sign == 1 else cancellation_hour.low <= target,
                "Cancellation candle does not touch the reference target")
        exit_raw = exit_effective = None
        events = [("A", "Anulare: ținta atinsă", end_ms, target, "#c2410c", "X")]
    return dict(channel=channel, candles=visible, target=target, reference=reference,
                retest_zone=strategy.retest_zone(setup, params), n=n,
                retest_multiplier=params.retest_tolerance_atr,
                events=events, end_ms=end_ms, exit_raw=exit_raw, exit_effective=exit_effective)


def render(item: Selection, chart: dict, path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as dates
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    from matplotlib.ticker import FuncFormatter

    row, trade = item.setup, item.trade
    channel, candles = chart["channel"], chart["candles"]
    x = lambda ms: dates.date2num(utc(ms))
    bar_width = strategy.FOUR_HOURS_MS / 86_400_000
    fig = plt.figure(figsize=(19, 10), facecolor="white")
    ax = fig.add_axes((0.065, 0.25, 0.69, 0.62))
    panel = fig.add_axes((0.78, 0.20, 0.21, 0.68))
    panel.axis("off")
    for candle in candles:
        center = x(candle.start_ms) + bar_width / 2
        color = "#008577" if candle.close >= candle.open else "#d44b48"
        ax.vlines(center, candle.low, candle.high, color=color, linewidth=0.85, zorder=3)
        body = abs(candle.close - candle.open)
        if body:
            ax.add_patch(Rectangle((center - bar_width * .34, min(candle.open, candle.close)),
                                   bar_width * .68, body, facecolor=color, edgecolor=color, zorder=3))
        else:
            ax.hlines(candle.close, center - bar_width * .34, center + bar_width * .34, color=color)
    ax.axvspan(x(channel.window_start_ms), x(channel.window_end_ms),
               color="#94a3b8", alpha=.13, label=f"Fereastra canalului: {chart['n']} × 4H")
    for label, ms in (("Început canal", channel.window_start_ms), ("Sfârșit canal", channel.window_end_ms)):
        ax.axvline(x(ms), color="#64748b", linestyle=":", linewidth=1)
        ax.text(x(ms), 1.012, label, transform=ax.get_xaxis_transform(), fontsize=9,
                ha="left" if ms == channel.window_start_ms else "right", color="#475569")
    breakout_close = int(row["breakout_ms"])
    ax.axvspan(x(breakout_close - strategy.FOUR_HOURS_MS), x(breakout_close),
               color="#f59e0b", alpha=.28, label="Lumânarea de spargere 4H")
    for name, value, color in (("U", channel.upper, "#5b21b6"), ("L", channel.lower, "#0369a1")):
        ax.axhline(value, color=color, linewidth=1.2, label=f"{name} = {price(value)}")
    for pivots, marker, color, label in ((channel.high_pivots, "v", "#5b21b6", "Pivoți grup U"),
                                        (channel.low_pivots, "^", "#0369a1", "Pivoți grup L")):
        ax.scatter([x(p.start_ms) + bar_width / 2 for p in pivots], [p.level for p in pivots],
                   marker=marker, color=color, edgecolor="white", linewidth=.6, s=65, zorder=5, label=label)
    ax.fill_between([x(breakout_close), x(candles[-1].start_ms + strategy.FOUR_HOURS_MS)],
                    *chart['retest_zone'], color="#f59e0b", alpha=.14,
                    label=f"Zona retest ±{chart['retest_multiplier']} ATR(ref)")
    start_level = int(trade["entry_ms"]) if trade else breakout_close
    ax.hlines(chart["target"], x(start_level), x(chart["end_ms"]),
              color="#15803d", linestyle="--", linewidth=1.4, label=f"TP = {price(chart['target'])}")
    if trade:
        ax.hlines(float(trade["initial_stop"]), x(start_level), x(chart["end_ms"]),
                  color="#dc2626", linestyle="--", linewidth=1.4, label=f"SL = {price(float(trade['initial_stop']))}")
    for index, (code, label, ms, value, color, marker) in enumerate(chart["events"]):
        ax.scatter(x(ms), value, marker=marker, color=color, edgecolor="white", s=95, linewidth=.8, zorder=7)
        ax.annotate(code, (x(ms), value), xytext=(-12 if index == 0 else 10, 16 if index != 1 else -24),
                    textcoords="offset points", color=color, weight="bold", fontsize=10,
                    arrowprops={"arrowstyle": "-", "color": color, "lw": .7})
    y_values = [v for c in candles for v in (c.low, c.high)] + [chart['target']]
    y_values += [e[3] for e in chart['events']]
    if trade:
        y_values.append(float(trade['initial_stop']))
    padding = (max(y_values) - min(y_values)) * .10
    ax.set_ylim(min(y_values) - padding, max(y_values) + padding)
    ax.set_xlim(x(candles[0].start_ms) - bar_width, x(candles[-1].start_ms) + 2 * bar_width)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: price(value)))
    locator = dates.AutoDateLocator(minticks=5, maxticks=9, tz=timezone.utc)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(dates.DateFormatter("%d %b\n%H:%M", tz=timezone.utc))
    ax.set_ylabel("Preț (USDT)")
    ax.set_xlabel("UTC • corpul fiecărei lumânări 4H este centrat în intervalul său")
    ax.grid(alpha=.16)
    ax.spines[["top", "right"]].set_visible(False)
    handles, labels = ax.get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower left", bbox_to_anchor=(.065, .075),
               ncol=3, frameon=False, fontsize=9)
    status = trade['outcome'] if trade else row['final_status']
    result = f"{float(trade['net_r']):+.4f} R net" if trade else "R net: NA (fără tranzacție)"
    fig.suptitle(f"{row['symbol']}  •  {row['direction']}  •  Spargere: {iso(breakout_close)}\n"
                 f"{VARIANT}  |  {status}  |  {result}", fontsize=15, weight="bold", y=.975)
    details = [f"Selecție {'A' if trade else 'B'} • rang {item.rank}",
               f"U  {price(channel.upper)}", f"L  {price(channel.lower)}", f"W  {price(channel.width)}",
               f"ATR(ref)  {price(channel.atr_ref)}", ""]
    for code, label, ms, value, _, _ in chart['events']:
        details.extend([f"{code}  {label}: {price(value)}", iso(ms), ""])
    if trade:
        details.extend([f"Entry open: {price(float(trade['entry_raw']))}",
                        f"SL: {price(float(trade['initial_stop']))}", f"TP: {price(chart['target'])}",
                        f"Ieșire brută: {price(chart['exit_raw'])}", "",
                        "Entry/ieșire efective includ slippage.",
                        "Prețul ieșirii: derivat din PnL salvat.",
                        "C și E pot avea aceeași oră."])
    else:
        details.extend(["Retest / confirmare: absente", "Intrare / SL / ieșire: NA", "Niciun ordin executat.",
                        f"TP de referință: {price(chart['target'])}"])
    details.extend(["", "Timpul TP/SL/anulării: închiderea", "lumânării 1H care detectează atingerea;",
                    "minutul exact intrabar nu este cunoscut.", "", "10 lumânări 4H după eveniment."])
    panel.text(0, 1, "\n".join(details), va="top", fontsize=10, linespacing=1.4)
    source = f"Run {RUN_ID} • trades CSV #{trade['source_row'] if trade else 'NA'} • setups CSV #{row['source_row']}"
    fig.text(.065, .035, source + "\nchannel_id = " + row['channel_id'], fontsize=9, color="#475569")
    metadata = dict(item.identity(), run_id=RUN_ID, source_commit=SOURCE_COMMIT,
                    variant=VARIANT, csv_sha256=CSV_SHA256,
                    pivot_high_ms=[p.start_ms for p in channel.high_pivots],
                    pivot_low_ms=[p.start_ms for p in channel.low_pivots],
                    candles_4h=len(candles), channel_candles=chart['n'], candles_after=10)
    try:
        fig.savefig(path, dpi=160, metadata={"Description": json.dumps(metadata, ensure_ascii=True)})
    finally:
        plt.close(fig)


def write_eth(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["setup_id", "breakout_utc", "direction", "U", "L", "W", "final_status"])
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(setup_id=row['channel_id'], breakout_utc=utc(int(row['breakout_ms'])).isoformat().replace('+00:00', 'Z'),
                                 direction=row['direction'], U=row['upper'], L=row['lower'],
                                 W=row['width'], final_status=row['final_status']))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "backtest_data" / "v3")
    parser.add_argument("--results-dir", type=Path, default=ROOT / "backtest_results" / "v3")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--list-only", action="store_true", help="Print selection IDs without writing files")
    parser.add_argument("--eth-only", action="store_true", help="Write only the ETH table (no candle data needed)")
    args = parser.parse_args(argv)
    chosen, eth = select(args.results_dir)
    for item in chosen:
        print(json.dumps(item.identity(), ensure_ascii=True))
    print(f"Selected 10/53 trades; cancelled ranks 1/10/19 of 19; ETH setups: {len(eth)}", flush=True)
    if args.list_only:
        return 0
    output = args.output_dir.resolve()
    require(not output.is_relative_to(ROOT), "Generated images/tables must stay outside the repository")
    if not args.eth_only:
        for filename, expected in CODE_SHA256.items():
            raw = (ROOT / "backtest" / filename).read_bytes().replace(b"\r\n", b"\n")
            require(hashlib.sha256(raw).hexdigest() == expected, f"Source differs from audited run: {filename}")
        params = strategy.Parameters.from_dict(json.loads((ROOT / 'backtest/v3_config.json').read_text()))
        manifest = json.loads((args.data_dir / 'manifest-v3.json').read_text())
        markets = {}
        missing = {}
        for symbol in sorted({item.setup['symbol'] for item in chosen}):
            try:
                markets[symbol] = tuple(checked_candles(args.data_dir, manifest, symbol, bar) for bar in ('4H', '1H'))
            except FileNotFoundError as exc:
                missing[symbol] = str(exc)
        # Preflight every input and reconstruction before producing any PNG.
        charts = []
        data_missing = []
        for item in chosen:
            symbol = item.setup['symbol']
            try:
                if symbol in missing:
                    raise FileNotFoundError(missing[symbol])
                charts.append((item, prepare(item, *markets[symbol], params)))
            except (FileNotFoundError, KeyError) as exc:
                data_missing.append(dict(item.identity(), reason=str(exc)))
            except ValueError as exc:
                if not str(exc).startswith(('Missing breakout', 'Fewer than 10', 'Gap inside')):
                    raise
                data_missing.append(dict(item.identity(), reason=str(exc)))
        print('DATA_MISSING ' + json.dumps(data_missing, ensure_ascii=True), flush=True)
        output.mkdir(parents=True, exist_ok=True)
        for item, chart in charts:
            render(item, chart, output / item.filename)
            print(f"PNG {output / item.filename}", flush=True)
    output.mkdir(parents=True, exist_ok=True)
    write_eth(output / 'eth-setups.csv', eth)
    print(f"CSV {output / 'eth-setups.csv'} ({len(eth)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
