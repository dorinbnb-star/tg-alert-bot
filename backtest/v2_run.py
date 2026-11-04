"""Run the swing v2 experiment against a pinned OKX v3 data snapshot."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path

from . import v2_swing as swing, v3_data, v3_report, v3_run

SYMBOLS = tuple(f"{symbol}-USDT-SWAP" for symbol in
                "BTC ETH SOL XRP BNB AVAX LINK LTC SUI UNI AAVE TIA WLD WIF STRK VIRTUAL ENA YGG".split())


def verified_market(root, expected_sha):
    manifest_file = root / "manifest-v3.json"
    actual = hashlib.sha256(manifest_file.read_bytes()).hexdigest()
    if actual != expected_sha.lower():
        raise ValueError("Pinned manifest SHA-256 does not match")
    manifest = v3_data.load_manifest(root)
    for symbol in SYMBOLS:
        item = manifest[symbol]
        if not math.isfinite(float(item["instrument"]["tickSz"])) or float(item["instrument"]["tickSz"]) <= 0:
            raise ValueError(f"Invalid tick size: {symbol}")
        for interval in ("1D", "4H", "1H", "funding"):
            if v3_data.rows_sha(v3_data.series_path(root, symbol, interval)) != item[interval]["sha256"]:
                raise ValueError(f"Series SHA-256 mismatch: {symbol} {interval}")
    return v3_data.load_market(root, list(SYMBOLS)), manifest


def verdict(metrics, p90):
    if metrics["n"] < 30:
        return "NECONCLUDENT"
    mean, pf, doubled = (metrics[k] for k in ("mean_r", "profit_factor", "mean_r_cost_x2"))
    return ("PASS" if None not in (mean, pf, doubled, p90)
            and mean >= .15 and pf >= 1.2 and doubled > 0 and mean > p90 else "FAIL")


def eligible_from(p):
    from bisect import bisect_left

    pivots = {}
    for pivot in p.pivots:
        pivots.setdefault(pivot.kind, pivot.confirmed_ms)
    if len(pivots) < 2:
        return None
    first = max(pivots.values())
    return next((time for i, time in enumerate(p.starts[1:], 1)
                 if time >= first and p.atr[i - 1] and p.starts[i - 1] == time - swing.H
                 and (d := bisect_left(p.daily_times, time + 1) - 1) >= 0
                 and p.ema200[d] is not None and time - p.daily_times[d] < swing.DAY), None)


def selection_key(item):
    name, metric = item
    mean = metric["mean_r"]
    return (-(mean if mean is not None else -math.inf), metric["max_drawdown_r"], -metric["n"], name)


def run_experiment(market, costs):
    prepared = {symbol: swing.Prepared(market[symbol]) for symbol in SYMBOLS}
    eligibility = {symbol: eligible_from(p) for symbol, p in prepared.items()}
    if not any(t is not None for t in eligibility.values()):
        raise ValueError("No symbol has enough history")
    start = min(t for t in eligibility.values() if t is not None)
    end = max(p.starts[-1] + swing.H for p in prepared.values() if p.starts)
    hours = (end - start) // swing.H
    bounds = (start, start + (hours * 60 // 100) * swing.H,
              start + (hours * 80 // 100) * swing.H, end)
    all_trades, all_events, summaries = [], [], {}

    def detect(a, b, seed=None):
        signal_map, stages = {}, []
        for symbol, p in prepared.items():
            eligible = eligibility[symbol]
            if eligible is None:
                continue
            found, events = swing.signals(symbol, p, max(a, eligible), b, seed)
            signal_map[symbol] = found
            stages.extend(dict(symbol=symbol, time=t, status=s) for t, s in events)
        return signal_map, stages

    def evaluate(variant, split, a, b, signals, stages):
        trades, events = swing.replay(prepared, signals, a, b, variant, costs)
        all_trades.extend(dict(row, split=split, variant=variant.key) for row in trades)
        all_events.extend(dict(row, split=split, variant=variant.key) for row in (*stages, *events))
        groups = {"all": trades, "BTC_ETH": [r for r in trades if r["symbol"] in SYMBOLS[:2]]}
        groups.update({symbol: [r for r in trades if r["symbol"] == symbol] for symbol in SYMBOLS})
        groups.update({d: [r for r in trades if r["direction"] == d] for d in ("LONG", "SHORT")})
        summary = {name: v3_report.metrics(rows) for name, rows in groups.items()}
        funnel = Counter(row["status"] for row in (*stages, *events))
        summary["funnel"] = dict(funnel)
        summary["fill_rate"] = funnel["FILLED"] / funnel["ORDER_CREATED"] if funnel["ORDER_CREATED"] else None
        summaries.setdefault(split, {})[variant.key] = summary

    a, b, c, d = bounds
    choices = {variant.key: variant for variant in swing.variants()}
    development, stages = detect(a, b)
    for variant in choices.values():
        evaluate(variant, "development", a, b, development, stages)
    chosen = min(((key, value["all"]) for key, value in summaries["development"].items()), key=selection_key)[0]
    if not summaries["development"][chosen]["all"]["n"]:
        return dict(verdict="NECONCLUDENT", chosen=None, bounds=bounds, eligibility=eligibility,
                    summaries=summaries, reason="No eligible development trades"), all_trades, all_events
    for split, left, right in (("walk_forward", b, c), ("final", c, d)):
        signals, stages = detect(left, right)
        evaluate(choices[chosen], split, left, right, signals, stages)
    baseline = []
    for seed in range(200):
        signals, _ = detect(c, d, seed)
        trades, _ = swing.replay(prepared, signals, c, d, choices[chosen], costs)
        baseline.append(v3_report.metrics(trades)["mean_r"])
    valid = sorted(x for x in baseline if x is not None)
    p90 = valid[math.ceil(.9 * len(valid)) - 1] if valid else None
    final = summaries["final"][chosen]["all"]
    result = dict(verdict=verdict(final, p90), chosen=chosen, bounds=bounds, eligibility=eligibility,
                  summaries=summaries, baseline=dict(seed_mean_r=baseline, valid=len(valid), p90=p90))
    return result, all_trades, all_events


def render(result):
    lines = ["# Backtest swing v2", "", f"Verdict: **{result['verdict']}**",
             f"Varianta aleasa: {result['chosen']}", "",
             "Perioada finala a fost observata anterior pentru v3.", "",
             "| Perioada | Varianta | N eligibil | R mediu net | PF | R net costuri x2 |",
             "|---|---|---:|---:|---:|---:|"]
    for split, variants in result["summaries"].items():
        for name, group in variants.items():
            m = group["all"]
            lines.append(f"| {split} | {name} | {m['n']} | {v3_report.fmt(m['mean_r'])} | "
                         f"{v3_report.fmt(m['profit_factor'])} | {v3_report.fmt(m['mean_r_cost_x2'])} |")
    if "baseline" in result:
        lines += ["", f"Baseline P90: {v3_report.fmt(result['baseline']['p90'])}; "
                  f"seed-uri eligibile: {result['baseline']['valid']}/200."]
    lines += ["", "Detalii pe simbol, directie, BTC/ETH, costuri si funnel in v2-metrics.json."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    market, manifest = verified_market(args.data_dir, args.manifest_sha256)
    costs_path = Path(__file__).with_name("costs.json")
    result, trades, events = run_experiment(market, json.loads(costs_path.read_text(encoding="utf-8")))
    result["manifest_sha256"] = args.manifest_sha256.lower()
    result["data_coverage"] = manifest
    result["costs_sha256"] = hashlib.sha256(costs_path.read_bytes()).hexdigest()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    v3_run.write_csv(args.output_dir / "v2-trades.csv", trades)
    v3_run.write_csv(args.output_dir / "v2-setups.csv", events)
    (args.output_dir / "v2-metrics.json").write_text(
        json.dumps(v3_run.json_safe(result), indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (args.output_dir / "v2-report.md").write_text(render(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
