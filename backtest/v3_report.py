"""Metrics and Markdown report for the v3 backtest."""

from __future__ import annotations

import math
import statistics
from collections import Counter
from typing import Iterable


def max_drawdown(values: Iterable[float]) -> float:
    peak = 0.0
    equity = 0.0
    drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def percent_drawdown(values: Iterable[float], risk_fraction: float = 0.02) -> float:
    equity = 1.0
    peak = 1.0
    drawdown = 0.0
    for value in values:
        equity *= max(1.0 + risk_fraction * value, 1e-12)
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
    return drawdown


def longest_loss_streak(values: Iterable[float]) -> int:
    longest = current = 0
    for value in values:
        current = current + 1 if value < 0 else 0
        longest = max(longest, current)
    return longest


def metrics(rows: list[dict], key: str = "net_r") -> dict:
    ordered = sorted(rows, key=lambda row: (int(row["exit_ms"]), row["symbol"], int(row["entry_ms"])))
    eligible = [row for row in ordered if row.get("funding_complete", True)]
    values = [float(row[key]) for row in eligible]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    durations = [float(row["duration_hours"]) for row in eligible]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "n": len(values),
        "n_all": len(rows),
        "funding_incomplete": len(rows) - len(eligible),
        "win_rate": sum(value > 0 for value in values) / len(values) if values else None,
        "mean_r": statistics.fmean(values) if values else None,
        "total_r": sum(values),
        "profit_factor": gross_profit / gross_loss if gross_loss else (math.inf if gross_profit else None),
        "max_drawdown_r": max_drawdown(values),
        "max_drawdown_pct": percent_drawdown(values),
        "longest_loss_streak": longest_loss_streak(values),
        "duration_mean_h": statistics.fmean(durations) if durations else None,
        "duration_median_h": statistics.median(durations) if durations else None,
        "mfe_mean_r": statistics.fmean(float(row.get("mfe_r", 0)) for row in eligible) if eligible else None,
        "mae_mean_r": statistics.fmean(float(row.get("mae_r", 0)) for row in eligible) if eligible else None,
        "fees": sum(float(row.get("fees", 0)) for row in eligible),
        "slippage": sum(float(row.get("slippage", 0)) for row in eligible),
        "funding": sum(float(row.get("funding", 0)) for row in eligible),
        "gross_pnl": sum(float(row.get("gross_pnl", 0)) for row in eligible),
        "tp1_mean_r": statistics.fmean(float(row.get("tp1_net_r", 0)) for row in eligible) if eligible else None,
        "tp2_mean_r": statistics.fmean(float(row.get("tp2_net_r", 0)) for row in eligible) if eligible else None,
        "runner_mean_r": statistics.fmean(float(row.get("runner_net_r", 0)) for row in eligible) if eligible else None,
        "mean_r_cost_x2": statistics.fmean(float(row["net_r_cost_x2"]) for row in eligible) if eligible else None,
    }


def fmt(value, digits: int = 3) -> str:
    if value is None:
        return "-"
    if value == math.inf:
        return "∞"
    return f"{value:.{digits}f}"


def funnel(setups: list[dict]) -> dict[str, int]:
    return dict(sorted(Counter(row["status"] for row in setups).items()))


def render(
    all_setups: list[dict],
    all_trades: list[dict],
    split_bounds: dict,
    selection: dict,
    baseline: dict,
    sensitivity: list[dict],
    coverage: dict | None = None,
    plateau: list[dict] | None = None,
    eliminated: list[dict] | None = None,
) -> str:
    lines = [
        "# Backtest v3 canal",
        "",
        "Strategie: canal orizontal 4H → spargere → retest/confirmare 1H. "
        "Metrica principală este R mediu net după comisioane, slippage și funding real complet.",
        "",
        "## Perioade",
        "",
        f"- dezvoltare: `{split_bounds['start']}` – `{split_bounds['development_end']}`",
        f"- walk-forward: `{split_bounds['development_end']}` – `{split_bounds['walk_forward_end']}`",
        f"- final neatins: `{split_bounds['walk_forward_end']}` – `{split_bounds['end']}`",
        "",
        "## Selecție pe dezvoltare",
        "",
        f"Varianta de bază aleasă: `{selection.get('base_variant', 'NONE')}`. "
        f"După filtre: `{selection.get('final_variant', 'NONE')}`.",
        "",
        "| Variantă | Split | N | Win rate | R mediu net | PF | DD max R | DD max % | Durată medie h | Costuri ×2 R |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    variants = sorted({row["variant"] for row in all_trades})
    for variant in variants:
        for split in ("development", "walk_forward", "final", "total"):
            chosen = [row for row in all_trades if row["variant"] == variant and (split == "total" or row["split"] == split)]
            item = metrics(chosen)
            win = "-" if item["win_rate"] is None else f"{item['win_rate'] * 100:.1f}%"
            lines.append(
                f"| {variant} | {split} | {item['n']} | {win} | {fmt(item['mean_r'])} | "
                f"{fmt(item['profit_factor'], 2)} | {fmt(item['max_drawdown_r'], 2)} | "
                f"{item['max_drawdown_pct'] * 100:.1f}% | {fmt(item['duration_mean_h'], 1)} | "
                f"{fmt(item['mean_r_cost_x2'])} |"
            )
    final_variant = selection.get("final_variant")
    if final_variant:
        lines += [
            "",
            "## Varianta finală pe simbol și direcție",
            "",
            "| Simbol | Direcție | N | Win rate | R mediu net | DD max R | TP1 R | TP2 R | Runner R |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        symbols = sorted({row["symbol"] for row in all_trades if row["variant"] == final_variant})
        for symbol in symbols:
            for direction in ("LONG", "SHORT", "BOTH"):
                chosen = [row for row in all_trades if row["variant"] == final_variant and row["symbol"] == symbol]
                if direction != "BOTH":
                    chosen = [row for row in chosen if row["direction"] == direction]
                item = metrics(chosen)
                win = "-" if item["win_rate"] is None else f"{item['win_rate'] * 100:.1f}%"
                lines.append(
                    f"| {symbol} | {direction} | {item['n']} | {win} | {fmt(item['mean_r'])} | "
                    f"{fmt(item['max_drawdown_r'])} | {fmt(item['tp1_mean_r'])} | "
                    f"{fmt(item['tp2_mean_r'])} | {fmt(item['runner_mean_r'])} |"
                )
    lines += ["", "## Funnel", "", "| Status | Număr |", "|---|---:|"]
    for status, count in funnel(all_setups).items():
        lines.append(f"| {status} | {count} |")
    if coverage:
        lines += [
            "",
            "## Acoperirea datelor",
            "",
            "| Simbol | Eligibil de la | 1H | 4H | Daily | Funding |",
            "|---|---|---|---|---|---|",
        ]
        for symbol, item in sorted(coverage.items()):
            lines.append(
                f"| {symbol} | {item.get('eligible_from', '-')} | "
                f"{item.get('1H', {}).get('first', '-')} → {item.get('1H', {}).get('last', '-')} | "
                f"{item.get('4H', {}).get('first', '-')} → {item.get('4H', {}).get('last', '-')} | "
                f"{item.get('1D', {}).get('first', '-')} → {item.get('1D', {}).get('last', '-')} | "
                f"{item.get('funding', {}).get('first', '-')} → {item.get('funding', {}).get('last', '-')} |"
            )
    shadow = [row for row in all_setups if row.get("shadow_outcome")]
    lines += [
        "",
        "## Ținte structurale respinse și SHADOW",
        "",
        f"Respingeri cu rezultat SHADOW: {len(shadow)}. Aceste rezultate nu ocupă sloturi și nu intră în selecție.",
        f"Setup-uri acceptate la 2R și eliminate la 3R: {len(eliminated or [])}.",
        "",
        "## Baseline aleator",
        "",
        f"Seed-uri: {baseline.get('seeds', 0)}; R mediu median: {fmt(baseline.get('median_mean_r'))}; "
        f"percentila strategiei reale: {fmt(baseline.get('strategy_percentile'), 1)}%.",
        "",
        "## Sensibilitate",
        "",
        "| Parametru | Valoare | N | R mediu net | DD max R |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in sensitivity:
        lines.append(
            f"| {row['parameter']} | {row['value']} | {row['n']} | {fmt(row['mean_r'])} | {fmt(row['max_drawdown_r'])} |"
        )
    if plateau:
        lines += ["", "### Verificarea platoului", "", "| Parametru | Centru | Platou |", "|---|---:|---|"]
        for row in plateau:
            lines.append(f"| {row['parameter']} | {row['center']} | {'DA' if row['plateau'] else 'NU'} |")
    lines += [
        "",
        "## Limitări operaționale",
        "",
        "- TP și SL în aceeași lumânare 1H sunt tratate ca SL.",
        "- Funding-ul incomplet exclude trade-ul din metrica principală, dar trade-ul rămâne în fișierul detaliat.",
        "- Contextul Daily este descriptiv și apare `NA` până când EMA50 și EMA200 sunt disponibile.",
        "- Rezultatele trecute nu calibrează o probabilitate de succes.",
    ]
    return "\n".join(lines) + "\n"
