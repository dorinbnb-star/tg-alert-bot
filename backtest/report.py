#!/usr/bin/env python3
"""Markdown + CSV report from simulate.py output. Deterministic (fixed bootstrap seed)."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest import download, variants  # noqa: E402

BOOTSTRAP_SEED = 20261003
BOOTSTRAP_ROUNDS = 2000
PERIODS = (("8m", "8 luni"), ("4m", "4 luni"), ("total", "12 luni"))
MODE_LABELS = {
    "main": "principal: 08:00-22:00 Bruxelles, toate alertele independente, dedup live",
    "24h": "secundar: 24h",
    "onepos": "secundar: o singura pozitie deschisa pe moneda (08:00-22:00)",
}
DOC_LINKS = [
    ("Comisioane OKX", "https://www.okx.com/fees"),
    ("Calculul comisionului la futures/swap", "https://www.okx.com/help/how-to-calculate-the-contract-transaction-fee"),
    ("Get candlesticks history (limita de rata, limit, paginare)",
     "https://www.okx.com/docs-v5/en/#public-data-rest-api-get-candlesticks-history"),
    ("Get funding rate history (adancimea istoricului, limita de rata)",
     "https://www.okx.com/docs-v5/en/#public-data-rest-api-get-funding-rate-history"),
    ("Get candlesticks (campul confirm)", "https://www.okx.com/docs-v5/en/#public-data-rest-api-get-candlesticks"),
]


def load_trades(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        for key in ("r", "r_cost_x2", "funding_est_r", "funding_real_r", "fee_r", "slippage_r"):
            row[key] = float(row[key])
        for key in ("scan_ms", "exit_ms", "ambiguous_sl", "gap", "minute_fallback", "funding_est_n"):
            row[key] = int(row[key])
    return rows


def max_drawdown(trades: list[dict], key: str) -> float:
    equity = peak = worst = 0.0
    for trade in sorted(trades, key=lambda t: (t["exit_ms"], t["scan_ms"], t["symbol"])):
        equity += trade[key]
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def bootstrap_ci(values: list[float]) -> tuple[float, float] | None:
    if len(values) < 2:
        return None
    rng = random.Random(BOOTSTRAP_SEED)
    size = len(values)
    means = sorted(sum(rng.choice(values) for _ in range(size)) / size for _ in range(BOOTSTRAP_ROUNDS))
    return means[int(0.025 * BOOTSTRAP_ROUNDS)], means[int(0.975 * BOOTSTRAP_ROUNDS) - 1]


def metrics(trades: list[dict], key: str = "r", with_ci: bool = False) -> dict:
    resolved = [t for t in trades if t["outcome"] != "OPEN"]
    values = [t[key] for t in resolved]
    wins = sum(1 for t in resolved if t["outcome"] == "TP")
    result = {
        "n": len(resolved),
        "wins": wins,
        "win_rate": wins / len(resolved) if resolved else None,
        "mean_r": sum(values) / len(values) if values else None,
        "total_r": sum(values),
        "max_dd_r": max_drawdown(resolved, key),
        "open": len(trades) - len(resolved),
        "ambiguous_sl": sum(t["ambiguous_sl"] for t in resolved),
        "estimated_funding_trades": sum(1 for t in resolved if t["funding_est_n"] > 0),
    }
    if with_ci:
        result["ci"] = bootstrap_ci(values)
    return result


def select(trades: list[dict], variant: str, mode: str, period: str = "total", symbol: str | None = None) -> list[dict]:
    return [t for t in trades if t["variant"] == variant and t["mode"] == mode
            and (period == "total" or t["period"] == period) and (symbol is None or t["symbol"] == symbol)]


def pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def num(value: float | None, digits: int = 3) -> str:
    return "-" if value is None else f"{value:+.{digits}f}"


def ci_text(ci: tuple[float, float] | None) -> str:
    return "-" if ci is None else f"[{ci[0]:+.3f}; {ci[1]:+.3f}]"


def summary_table(trades: list[dict], mode: str, key: str, variant_list: tuple[str, ...]) -> list[str]:
    lines = [
        "| Varianta | Perioada | Semnale (rezolvate) | Rata de castig | R mediu dupa costuri | IC 95% R mediu | R total | Drawdown max (R) | Deschise la final |",
        "|---|---|---:|---:|---:|---|---:|---:|---:|",
    ]
    for variant in variant_list:
        for period, label in PERIODS:
            m = metrics(select(trades, variant, mode, period), key, with_ci=True)
            lines.append(
                f"| {variants.LABELS[variant]} | {label} | {m['n']} | {pct(m['win_rate'])} | {num(m['mean_r'])} | "
                f"{ci_text(m['ci'])} | {num(m['total_r'], 2)} | {m['max_dd_r']:.2f} | {m['open']} |"
            )
    return lines


def coin_table(trades: list[dict], variant: str, mode: str, symbols: list[str]) -> list[str]:
    header = "| Moneda |" + "".join(f" {label}: N | castig | R mediu | DD |" for _, label in PERIODS)
    lines = [header, "|---|" + "---:|---:|---:|---:|" * len(PERIODS)]
    for symbol in symbols + ["TOTAL"]:
        cells = []
        for period, _ in PERIODS:
            m = metrics(select(trades, variant, mode, period, None if symbol == "TOTAL" else symbol))
            cells.append(f" {m['n']} | {pct(m['win_rate'])} | {num(m['mean_r'])} | {m['max_dd_r']:.2f} |")
        name = symbol.replace("-USDT-SWAP", "")
        lines.append(f"| {'**TOTAL**' if symbol == 'TOTAL' else name} |" + "".join(cells))
    return lines


def data_hash(manifest: dict) -> str:
    digest = hashlib.sha256()
    for symbol in sorted(manifest):
        for name in sorted(manifest[symbol]):
            digest.update(f"{symbol}/{name}:{manifest[symbol][name].get('sha256', '')}\n".encode("utf-8"))
    return digest.hexdigest()


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "necunoscut"


def build_report(results: Path, data_dir: Path) -> tuple[str, list[dict]]:
    trades = load_trades(results / "trades.csv")
    detect_summary = json.loads((results / "detect_summary.json").read_text(encoding="utf-8"))
    trade_summary = json.loads((results / "trade_summary.json").read_text(encoding="utf-8"))
    manifest = download.load_manifest(data_dir)
    docs_path = results / "okx_docs.json"
    docs = json.loads(docs_path.read_text(encoding="utf-8")) if docs_path.exists() else {"checks": []}
    costs = json.loads((Path(__file__).resolve().parent / "costs.json").read_text(encoding="utf-8"))
    symbols = download.symbols()
    out: list[str] = []
    add = out.append

    add("# Backtest strategia v1 (OKX, 18 monede)")
    add("")
    add(f"Fereastra: {download.START} - {download.END}; 8 luni: pana la {download.SPLIT}, 4 luni: dupa. "
        f"Commit `{git_commit()}`, hash date `{data_hash(manifest)[:16]}`.")
    add("1R = distanta planificata intrare-SL. R dupa comisioane taker, slippage si funding. "
        "Rata de castig = TP atins inaintea SL; TP si SL in aceeasi lumanare = SL. "
        "Drawdown = curba cumulata in R ordonata dupa momentul iesirii, 1R pe trade, fara compunere. "
        "Trade-urile inca deschise la final nu intra in metrici; sunt numarate separat.")
    add("")
    main_total = metrics(select(trades, "a", "main"), with_ci=True)
    add("## Raspuns scurt")
    add("")
    verdict = "PROFITABILA" if (main_total["mean_r"] or 0) > 0 else "NEPROFITABILA"
    add(f"v1 exact, rezultat principal, 12 luni: {main_total['n']} trade-uri, R mediu {num(main_total['mean_r'])} "
        f"(IC 95% {ci_text(main_total['ci'])}), R total {num(main_total['total_r'], 2)}, "
        f"drawdown {main_total['max_dd_r']:.2f}R. Verdict pe datele acestea: **{verdict}** dupa costuri.")
    add("")

    add("## 1. Rezultat principal: " + MODE_LABELS["main"])
    add("")
    out.extend(summary_table(trades, "main", "r", variants.TRADED_VARIANTS))
    add("")
    add("c2 este rulare secundara (R:R filtrat direct fata de pivotul 2).")
    add("")

    add("## 2. Alegerea pe 8 luni, verificata pe 4 luni")
    add("")
    first = {v: metrics(select(trades, v, "main", "8m")) for v in variants.MAIN_VARIANTS}
    eligible = {v: m for v, m in first.items() if m["mean_r"] is not None}
    if eligible:
        chosen = max(eligible, key=lambda v: (eligible[v]["mean_r"], v))
        later = metrics(select(trades, chosen, "main", "4m"), with_ci=True)
        add("Regula: varianta (a, b, c1) cu cel mai mare R mediu dupa costuri pe primele 8 luni.")
        add("")
        for v in variants.MAIN_VARIANTS:
            add(f"- {variants.LABELS[v]}: 8 luni R mediu {num(first[v]['mean_r'])} pe {first[v]['n']} trade-uri")
        add("")
        add(f"Aleasa: **{variants.LABELS[chosen]}**. Pe ultimele 4 luni: {later['n']} trade-uri, rata de castig "
            f"{pct(later['win_rate'])}, R mediu {num(later['mean_r'])} (IC 95% {ci_text(later['ci'])}), "
            f"R total {num(later['total_r'], 2)}, drawdown {later['max_dd_r']:.2f}R.")
    else:
        add("Nicio varianta nu are trade-uri pe primele 8 luni.")
    add("")

    add("## 3. Pe moneda (rezultat principal)")
    for variant in variants.TRADED_VARIANTS:
        add("")
        add(f"### {variants.LABELS[variant]}")
        add("")
        out.extend(coin_table(trades, variant, "main", symbols))
    add("")

    add("## 4. Rulari secundare")
    for mode in ("24h", "onepos"):
        add("")
        add(f"### {MODE_LABELS[mode]}")
        add("")
        out.extend(summary_table(trades, mode, "r", variants.TRADED_VARIANTS))
    add("")
    add("### Sensibilitate: costuri x2 (comision, slippage si funding platit, rezultat principal)")
    add("")
    out.extend(summary_table(trades, "main", "r_cost_x2", variants.TRADED_VARIANTS))
    add("")

    add("## 5. Unde se opresc scanarile (v1, fereastra 08-22, toate monedele)")
    add("")
    stages: Counter = Counter()
    for item in detect_summary.values():
        stages.update(item["stages"]["a"]["main"])
    total_stages = sum(stages.values()) or 1
    add("| Etapa | Scanari | Pondere |")
    add("|---|---:|---:|")
    for stage in ("BIAS", "PIVOT", "SWEEP", "REENTRY", "CONFIRMATION", "TARGET", "RR", "ENTRY"):
        add(f"| {stage} | {stages.get(stage, 0)} | {stages.get(stage, 0) / total_stages * 100:.2f}% |")
    add("")

    add("## 6. Contoare (rezultat principal)")
    add("")
    add("| Varianta | Detectate | Sarite (expirat/drift) | Duplicate dedup | Fara pivot 2 | Eroare date 1m | Executate | SL ambigue (TP+SL aceeasi lumanare) | Fallback 15m (fara 1m) | Gap peste SL | Deschise la final | Cu funding ESTIMARE |")
    add("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for variant in variants.TRADED_VARIANTS:
        counter: Counter = Counter()
        for item in trade_summary.values():
            counter.update(item["counters"][variant]["main"])
        skipped = sum(v for k, v in counter.items() if k.startswith("skipped:"))
        errors = sum(v for k, v in counter.items() if k.startswith("error:"))
        selected = select(trades, variant, "main")
        add(f"| {variant} | {counter['detected']} | {skipped} | {counter['duplicate']} | {counter['no_second_pivot']} | "
            f"{errors} | {counter['traded']} | {sum(t['ambiguous_sl'] for t in selected)} | "
            f"{sum(t['minute_fallback'] for t in selected)} | {sum(t['gap'] for t in selected)} | "
            f"{sum(1 for t in selected if t['outcome'] == 'OPEN')} | {sum(1 for t in selected if t['funding_est_n'] > 0)} |")
    add("")
    reasons: Counter = Counter()
    for item in trade_summary.values():
        reasons.update({k: v for k, v in item["counters"]["a"]["main"].items() if ":" in k})
    if reasons:
        add("Motive v1 (principal): " + "; ".join(f"`{k}` x{v}" for k, v in sorted(reasons.items())))
        add("")
    a_main = select(trades, "a", "main")
    real_funding = sum(t["funding_real_r"] for t in a_main if t["outcome"] != "OPEN")
    est_funding = sum(t["funding_est_r"] for t in a_main if t["outcome"] != "OPEN")
    add(f"Funding v1 (principal): real {real_funding:+.3f}R in total; **ESTIMARE** {est_funding:+.3f}R in total "
        f"(platit in defavoarea pozitiei acolo unde OKX nu da istoric).")
    add("")

    add("## 7. Costuri si surse")
    add("")
    add(f"Taker {costs['taker_fee_rate'] * 100:.3f}% pe fiecare parte (si la TP). Slippage: " + ", ".join(
        f"{k.replace('-USDT-SWAP', '')} {v * 100:.2f}%" for k, v in costs["slippage_rate"].items())
        + f"; la SL x{costs['stop_slippage_multiplier']}. Funding ESTIMARE: max({costs['funding_estimate']['floor_rate'] * 100:.3f}%, "
        f"p90 |rata reala|) la fiecare moment de funding, mereu in defavoarea pozitiei.")
    add("")
    add("Reconfirmare din paginile oficiale OKX, facuta de runner inainte de rulare:")
    add("")
    for check in docs.get("checks", []):
        found = "; ".join(f"`{text[:160]}`" for text in check["found"]) or f"NECONFIRMAT ({check['error'] or 'text negasit'})"
        add(f"- {check['id']}: {found} ({check['url']})")
    add("")
    for label, url in DOC_LINKS:
        add(f"- {label}: {url}")
    add("")

    add("## 8. Acoperirea reala a datelor pe moneda")
    add("")
    add("| Moneda | 4H prima / nr / lipsa | 1H prima / nr / lipsa | 15m prima / ultima / nr / lipsa | Funding real (de la / nr) | Funding ESTIMARE (pas, rata) | Blocuri 1m necesare / incomplete | Scanari fara date valide (08-22) |")
    add("|---|---|---|---|---|---|---|---|")
    for symbol in symbols:
        info = manifest.get(symbol, {})

        def cell(name: str, last: bool = False) -> str:
            item = info.get(name)
            if not item:
                return "lipsa"
            parts = [str(item.get("first", "-"))[:16]]
            if last:
                parts.append(str(item.get("last", "-"))[:16])
            parts += [str(item.get("count", 0)), str(item.get("missing_inside", 0))]
            return " / ".join(parts)

        funding = trade_summary.get(symbol, {}).get("funding", {})
        minute = info.get("1m", {})
        errors = sum(detect_summary.get(symbol, {}).get("errors", {}).get("main", {}).values())
        add(f"| {symbol.replace('-USDT-SWAP', '')} | {cell('4H')} | {cell('1H')} | {cell('15m', True)} | "
            f"{str(funding.get('real_first', '-'))[:16]} / {funding.get('real_count', 0)} | "
            f"{funding.get('estimate_step_hours', '-')}h, {funding.get('estimate_rate', 0) * 100:.4f}% | "
            f"{minute.get('blocks_needed', 0)} / {minute.get('blocks_incomplete', 0)} | {errors} |")
    add("")
    add("Lipsa = lumanari care lipsesc in interiorul seriei. O moneda listata dupa inceputul ferestrei are scanari "
        "fara date valide pana cand are 210 lumanari 4H (exact ca botul live, care ar raporta `Date incomplete`).")
    add("")

    add("## 9. Limitari")
    add("")
    add("- Pretul la momentul scanarii = deschiderea lumanarii de 1m care contine T (fara date de dupa T).")
    add("- Minutul (sau lumanarea 15m, fara 1m) care contine T poate doar sa opreasca pe SL, nu sa dea TP.")
    add("- Dupa prima lumanare 15m se folosesc lumanari 15m: TP si SL in aceeasi lumanare se numara SL.")
    add("- Dedup-ul live nu se declanseaza in acest grid (fiecare lumanare de confirmare e scanata o data valid); "
        "contorul de duplicate arata asta explicit.")
    add("- Probabilitatea ramane necalibrata; rezultatele trecute nu garanteaza nimic.")
    return "\n".join(out) + "\n", trades


def coin_rows(trades: list[dict]) -> list[dict]:
    rows = []
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for trade in trades:
        for period in (trade["period"], "total"):
            groups[(trade["variant"], trade["mode"], period, trade["symbol"])].append(trade)
            groups[(trade["variant"], trade["mode"], period, "TOTAL")].append(trade)
    for key in sorted(groups):
        m = metrics(groups[key])
        m2 = metrics(groups[key], "r_cost_x2")
        rows.append({
            "variant": key[0], "mode": key[1], "period": key[2], "symbol": key[3], "n": m["n"],
            "win_rate": "" if m["win_rate"] is None else round(m["win_rate"], 6),
            "mean_r": "" if m["mean_r"] is None else round(m["mean_r"], 6), "total_r": round(m["total_r"], 6),
            "max_dd_r": round(m["max_dd_r"], 6), "open": m["open"],
            "mean_r_cost_x2": "" if m2["mean_r"] is None else round(m2["mean_r"], 6),
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default=str(download.RESULTS_DIR))
    parser.add_argument("--data-dir", default=str(download.DATA_DIR))
    args = parser.parse_args(argv)
    results = Path(args.results)
    text, trades = build_report(results, Path(args.data_dir))
    (results / "report.md").write_text(text, encoding="utf-8")
    rows = coin_rows(trades)
    with (results / "metrics.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["variant"], lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(text)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
