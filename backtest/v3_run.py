#!/usr/bin/env python3
"""Run the isolated v3 portfolio simulation and create all report artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import os
import math
import statistics
import sys
from pathlib import Path

from backtest import v3_data, v3_engine, v3_report

ROOT = Path(__file__).resolve().parents[1]
COSTS_PATH = Path(__file__).with_name("costs.json")
SENSITIVITY = {
    "channel_window_4h": [48, 60, 72],
    "breakout_atr": [0.4, 0.5, 0.6],
    "retest_tolerance_atr": [0.20, 0.25, 0.30],
    "entry_buffer_atr": [0.0, 0.10, 0.20],
    "limit_expiry_1h": [8, 12, 16],
}


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or ["empty"], lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def split_bounds(prepared: dict[str, v3_engine.Prepared], config: dict) -> dict:
    starts = []
    ends = []
    eligible_by_symbol = {}
    for symbol, item in prepared.items():
        first_atr1 = next((index for index, value in enumerate(item.atr1) if value is not None), None)
        first_channel = min(item.channels, default=None)
        if first_atr1 is None or first_channel is None or not item.data.h1:
            continue
        eligible = max(
            item.data.h1[first_atr1].start_ms,
            item.data.h4[first_channel].start_ms + 4 * 3_600_000,
        )
        starts.append(eligible)
        ends.append(item.data.h1[-1].start_ms + 3_600_000)
        eligible_by_symbol[symbol] = eligible
    if not starts:
        raise RuntimeError("No symbol completed the v3 warm-up")
    start = min(starts)
    end = min(ends)
    span = end - start
    development_end = start + int(span * float(config["development_fraction"]))
    walk_forward_end = development_end + int(span * float(config["walk_forward_fraction"]))
    return {
        "start_ms": start, "development_end_ms": development_end,
        "walk_forward_end_ms": walk_forward_end, "end_ms": end,
        "eligible_by_symbol_ms": eligible_by_symbol,
        "start": v3_data.iso(start), "development_end": v3_data.iso(development_end),
        "walk_forward_end": v3_data.iso(walk_forward_end), "end": v3_data.iso(end),
    }


def split_name(timestamp: int, bounds: dict) -> str:
    if timestamp < bounds["development_end_ms"]:
        return "development"
    if timestamp < bounds["walk_forward_end_ms"]:
        return "walk_forward"
    return "final"


def add_splits(rows: list[dict], bounds: dict, timestamp_key: str) -> None:
    for row in rows:
        row["split"] = split_name(int(row[timestamp_key]), bounds)


def development_trades(trades: list[dict], bounds: dict) -> list[dict]:
    # Selection must not consume an outcome observed after development ended.
    return [row for row in trades
            if bounds["start_ms"] <= int(row["entry_ms"]) < bounds["development_end_ms"]
            and int(row["exit_ms"]) <= bounds["development_end_ms"]]


def choose_variant(results: dict[str, v3_engine.RunResult], bounds: dict, tie_r: float) -> str:
    order = {key: index for index, key in enumerate(results)}
    scored = []
    for key, result in results.items():
        rows = development_trades(result.trades, bounds)
        item = v3_report.metrics(rows)
        if item["mean_r"] is not None:
            scored.append((key, item))
    if not scored:
        raise RuntimeError("No completed, funding-valid development trades")
    scored.sort(key=lambda item: (-item[1]["mean_r"], item[1]["max_drawdown_r"], order[item[0]]))
    best_key, best = scored[0]
    near = [item for item in scored if best["mean_r"] - item[1]["mean_r"] <= tie_r]
    near.sort(key=lambda item: (item[1]["max_drawdown_r"], order[item[0]]))
    return near[0][0]


def aggregate_metrics(trades: list[dict]) -> list[dict]:
    output = []
    variants = sorted({row["variant"] for row in trades})
    for variant in variants:
        for split in ("development", "walk_forward", "final", "total"):
            for symbol in sorted({row["symbol"] for row in trades if row["variant"] == variant} | {"TOTAL"}):
                for direction in ("LONG", "SHORT", "BOTH"):
                    rows = [row for row in trades if row["variant"] == variant]
                    if split != "total":
                        rows = [row for row in rows if row["split"] == split]
                    if symbol != "TOTAL":
                        rows = [row for row in rows if row["symbol"] == symbol]
                    if direction != "BOTH":
                        rows = [row for row in rows if row["direction"] == direction]
                    item = v3_report.metrics(rows)
                    output.append({"variant": variant, "split": split, "symbol": symbol,
                                   "direction": direction, **item})
    return output


def aggregate_funnel(setups: list[dict]) -> list[dict]:
    counts: dict[tuple, int] = {}
    for row in setups:
        key = (row["variant"], row["split"], row["symbol"], row["direction"], row["status"])
        counts[key] = counts.get(key, 0) + 1
    return [
        {"variant": key[0], "split": key[1], "symbol": key[2], "direction": key[3],
         "status": key[4], "count": count}
        for key, count in sorted(counts.items())
    ]


def collapse_setups(events: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in events:
        grouped.setdefault((row["variant"], row["channel_id"]), []).append(row)
    output = []
    for key in sorted(grouped):
        rows = grouped[key]
        combined = dict(rows[0])
        statuses = [row["status"] for row in rows]
        combined.update(rows[-1])
        combined["final_status"] = rows[-1]["status"]
        combined["status_history"] = ">".join(statuses)
        combined["retested"] = "RETESTED" in statuses
        combined["confirmed"] = "CONFIRMED" in statuses
        combined["order_created"] = "ORDER_CREATED" in statuses
        combined["market_pending"] = "MARKET_PENDING" in statuses
        combined["filled"] = "FILLED" in statuses
        for row in rows:
            for field in ("shadow_outcome", "shadow_net_r", "entry_plan", "entry_raw",
                          "entry_effective", "stop", "targets"):
                if row.get(field) is not None:
                    combined[field] = row[field]
        output.append(combined)
    return output


def rr3_eliminated(results: dict[str, v3_engine.RunResult]) -> list[dict]:
    output = []
    for variant in v3_engine.variants():
        if variant.minimum_rr != 2.0:
            continue
        rr3 = v3_engine.Variant(variant.entry_mode, variant.exit_family, 3.0, variant.breakeven)
        two, three = results[variant.key], results[rr3.key]
        accepted = {row["channel_id"] for row in two.setups if row["status"] == "CONFIRMED"}
        rejected = {row["channel_id"]: row for row in three.setups if row["status"] == "RR_REJECTED"}
        trades = {row["channel_id"]: row for row in two.trades}
        for channel_id in sorted(accepted & rejected.keys()):
            source = rejected[channel_id]
            trade = trades.get(channel_id, {})
            output.append({
                "status": "ELIMINATED_BY_RR3", "variant_rr2": variant.key, "variant_rr3": rr3.key,
                "channel_id": channel_id, "symbol": source["symbol"], "direction": source["direction"],
                "rr2_result": trade.get("outcome", "NO_FILL"), "net_r": trade.get("net_r"),
                "mae_r": trade.get("mae_r"), "mfe_r": trade.get("mfe_r"),
                "duration_hours": trade.get("duration_hours"),
            })
    return output


def data_coverage(manifest: dict, bounds: dict, symbols: list[str]) -> dict:
    output = {}
    eligible = bounds.get("eligible_by_symbol_ms", {})
    for symbol in symbols:
        source = manifest.get(symbol, {})
        output[symbol] = {
            "eligible_from": v3_data.iso(eligible.get(symbol)),
            **{name: {field: source.get(name, {}).get(field) for field in ("first", "last", "count", "gap_count")}
               for name in ("1H", "4H", "1D", "funding")},
        }
    return output


def sensitivity_runs(market: dict, base_config: dict, costs: dict, chosen: v3_engine.Variant,
                     bounds: dict, enabled: bool) -> list[dict]:
    if not enabled:
        return []
    output = []
    for parameter, values in SENSITIVITY.items():
        for value in values:
            config = dict(base_config, **{parameter: value})
            prepared = v3_engine.prepare_market(market, config)
            result = v3_engine.run_variant(market, config, costs, chosen, prepared)
            rows = development_trades(result.trades, bounds)
            item = v3_report.metrics(rows)
            output.append({"parameter": parameter, "value": value, **item})
    return output


def plateau_evaluation(rows: list[dict], config: dict) -> list[dict]:
    output = []
    limit = float(config["plateau_max_delta_r"])
    for parameter, values in SENSITIVITY.items():
        items = {row["value"]: row for row in rows if row["parameter"] == parameter}
        center = items.get(values[1])
        neighbors = [items.get(values[0]), items.get(values[2])]
        means = [item.get("mean_r") if item else None for item in [center, *neighbors]]
        same_sign = all(value is not None for value in means) and (
            all(value >= 0 for value in means) or all(value <= 0 for value in means)
        )
        within = bool(
            center and center.get("mean_r") is not None
            and all(item and item.get("mean_r") is not None
                    and abs(item["mean_r"] - center["mean_r"]) <= limit for item in neighbors)
        )
        output.append({
            "parameter": parameter, "center": values[1], "same_sign": same_sign,
            "within_max_delta": within, "plateau": same_sign and within,
        })
    return output


def random_baseline(market: dict, config: dict, costs: dict, chosen: v3_engine.Variant,
                    prepared: dict, bounds: dict, seeds: int, strategy_mean: float | None) -> dict:
    means = []
    for seed in range(seeds):
        result = v3_engine.run_variant(market, config, costs, chosen, prepared, random_seed=seed)
        rows = development_trades(result.trades, bounds)
        mean = v3_report.metrics(rows)["mean_r"]
        if mean is not None:
            means.append(mean)
    percentile = None
    if means and strategy_mean is not None:
        percentile = 100.0 * sum(value <= strategy_mean for value in means) / len(means)
    return {
        "seeds": seeds, "valid_seeds": len(means), "mean_r_values": means,
        "median_mean_r": statistics.median(means) if means else None,
        "strategy_percentile": percentile,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=str(v3_data.DATA_DIR))
    parser.add_argument("--results-dir", default=str(v3_data.RESULTS_DIR))
    parser.add_argument("--baseline-seeds", type=int)
    parser.add_argument("--skip-sensitivity", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    config = v3_engine.load_config()
    costs = json.loads(COSTS_PATH.read_text(encoding="utf-8"))
    market = v3_data.load_market(Path(args.data_dir), config["symbols"])
    prepared = v3_engine.prepare_market(market, config)
    bounds = split_bounds(prepared, config)

    base_results: dict[str, v3_engine.RunResult] = {}
    for variant in v3_engine.variants():
        if not args.quiet:
            print(f"run {variant.key}", flush=True)
        base_results[variant.key] = v3_engine.run_variant(market, config, costs, variant, prepared)
    base_key = choose_variant(base_results, bounds, float(config["selection_tie_r"]))
    chosen_base = next(variant for variant in v3_engine.variants() if variant.key == base_key)

    filter_results = {base_key: base_results[base_key]}
    for filter_mode in ("VOLUME", "CHOCH_PAUSE_12", "CHOCH_NEW_STRUCTURE"):
        variant = v3_engine.Variant(
            chosen_base.entry_mode, chosen_base.exit_family, chosen_base.minimum_rr,
            chosen_base.breakeven, filter_mode,
        )
        if not args.quiet:
            print(f"run {variant.key}", flush=True)
        filter_results[variant.key] = v3_engine.run_variant(market, config, costs, variant, prepared)
    final_key = choose_variant(filter_results, bounds, float(config["selection_tie_r"]))
    chosen_final = next(
        v3_engine.Variant(chosen_base.entry_mode, chosen_base.exit_family, chosen_base.minimum_rr,
                          chosen_base.breakeven, mode)
        for mode in ("BASE", "VOLUME", "CHOCH_PAUSE_12", "CHOCH_NEW_STRUCTURE")
        if v3_engine.Variant(chosen_base.entry_mode, chosen_base.exit_family, chosen_base.minimum_rr,
                             chosen_base.breakeven, mode).key == final_key
    )

    kept_results = dict(base_results)
    kept_results.update(filter_results)
    all_setups = [dict(row) for result in kept_results.values() for row in result.setups]
    all_trades = [dict(row) for result in kept_results.values() for row in result.trades]
    add_splits(all_setups, bounds, "breakout_ms")
    add_splits(all_trades, bounds, "entry_ms")

    chosen_development = development_trades(filter_results[final_key].trades, bounds)
    chosen_mean = v3_report.metrics(chosen_development)["mean_r"]
    seeds = int(config["random_baseline_seeds"]) if args.baseline_seeds is None else args.baseline_seeds
    baseline = random_baseline(market, config, costs, chosen_final, prepared, bounds, seeds, chosen_mean)
    sensitivity = sensitivity_runs(market, config, costs, chosen_final, bounds, not args.skip_sensitivity)
    plateau = plateau_evaluation(sensitivity, config) if sensitivity else []
    selection = {"base_variant": base_key, "final_variant": final_key}
    metric_rows = aggregate_metrics(all_trades)
    funnel_rows = aggregate_funnel(all_setups)
    setup_rows = collapse_setups(all_setups)
    eliminated = rr3_eliminated(base_results)
    market_variant = v3_engine.Variant(
        "ENTRY_MARKET_NEXT_OPEN", chosen_base.exit_family, chosen_base.minimum_rr, chosen_base.breakeven,
    )
    limit_variant = v3_engine.Variant(
        "ENTRY_LIMIT", chosen_base.exit_family, chosen_base.minimum_rr, chosen_base.breakeven,
    )
    market_ids = {row["channel_id"] for row in base_results[market_variant.key].trades}
    limit_ids = {row["channel_id"] for row in base_results[limit_variant.key].trades}
    coverage = data_coverage(v3_data.load_manifest(Path(args.data_dir)), bounds, config["symbols"])
    ref_differences = {}
    limit_fill_rates = {}
    for entry_mode in ("ENTRY_LIMIT", "ENTRY_MARKET_NEXT_OPEN"):
        two_key = v3_engine.Variant(entry_mode, "REFERENCE", 2).key
        three_key = v3_engine.Variant(entry_mode, "REFERENCE", 3).key
        two_ids = {row["channel_id"] for row in base_results[two_key].setups if row["status"] == "CONFIRMED"}
        three_ids = {row["channel_id"] for row in base_results[three_key].setups if row["status"] == "CONFIRMED"}
        ref_differences[entry_mode] = len(two_ids ^ three_ids)
    for key, result in base_results.items():
        if not key.startswith("ENTRY_LIMIT"):
            continue
        created = sum(row["status"] == "ORDER_CREATED" for row in result.setups)
        filled = sum(row["status"] == "FILLED" for row in result.setups)
        limit_fill_rates[key] = {"orders": created, "fills": filled, "fill_rate": filled / created if created else None}
    shadow_counts = {}
    for row in all_setups:
        if row.get("shadow_outcome"):
            key = f"{row['status']}::{row['shadow_outcome']}"
            shadow_counts[key] = shadow_counts.get(key, 0) + 1
    diagnostics = {
        "selection": selection,
        "split_bounds": bounds,
        "market_only_channels": len(market_ids - limit_ids),
        "market_only_channel_ids": sorted(market_ids - limit_ids),
        "limit_fill_count": sum(row["entry_mode"] == "ENTRY_LIMIT" for row in all_trades),
        "market_fill_count": sum(row["entry_mode"] == "ENTRY_MARKET_NEXT_OPEN" for row in all_trades),
        "valid_channel_candidates": {symbol: len(item.channels) for symbol, item in prepared.items()},
        "reference_rr2_vs_rr3_setup_differences": ref_differences,
        "limit_fill_rates": limit_fill_rates,
        "shadow_counts": dict(sorted(shadow_counts.items())),
    }
    payload = {"diagnostics": diagnostics, "coverage": coverage, "metrics": metric_rows,
               "funnel": funnel_rows, "eliminated_by_rr3": eliminated,
               "baseline": baseline, "sensitivity": sensitivity, "plateau": plateau}
    results_dir = Path(args.results_dir)
    write_csv(results_dir / "v3-setups.csv", setup_rows)
    write_csv(results_dir / "v3-trades.csv", all_trades)
    (results_dir / "v3-metrics.json").write_text(
        json.dumps(json_safe(payload), indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8",
    )
    report = v3_report.render(
        all_setups, all_trades, bounds, selection, baseline, sensitivity, coverage, plateau, eliminated,
    )
    (results_dir / "v3-report.md").write_text(report, encoding="utf-8")
    if not args.quiet:
        encoding = sys.stdout.encoding or "utf-8"
        sys.stdout.buffer.write(report.encode(encoding, errors="replace"))
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
