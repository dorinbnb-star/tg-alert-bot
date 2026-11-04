"""One-shot universe OOS experiment; imports the frozen engine unchanged.

Download is resumable before execution. Execution has an exclusive persistent
lock and cannot be repeated. Baseline section 22 uses the existing randomized
market-entry mode, whereas the tested strategy enters at breakout-next-open.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import threading
import time

from backtest import v3_data as data, v3_engine as engine, v3_report as report
from backtest import v3_run as serialization, v3_universe_select as selection

ROOT = selection.RUN_ROOT
BARS = ('4H', '1H', '1D', 'funding')
VARIANT = engine.Variant('ENTRY_BREAKOUT_NEXT_OPEN', 'REFERENCE', 2.0)
BASELINE = engine.Variant('ENTRY_MARKET_NEXT_OPEN', 'REFERENCE', 2.0)
_network_lock = threading.Lock()
_baseline_inputs = None


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(serialization.json_safe(payload), indent=2, sort_keys=True)+'\n', encoding='utf-8')
    temporary.replace(path)


def read_selection(commit: str) -> dict:
    selection.verify_frozen()
    raw = selection.OUTPUT.read_bytes().replace(b'\r\n', b'\n')
    if selection.git('show', f'{commit}:backtest/v3_universe.json') != raw:
        raise ValueError('Selection does not match its published commit')
    selection.git('merge-base', '--is-ancestor', commit, 'origin/codex/github-actions-monitor')
    chosen = json.loads(raw)
    symbols = [r['symbol'] for r in chosen['selected']]
    if len(symbols) != 40 or len(set(symbols)) != 40 or set(symbols) & set(selection.EXCLUDED):
        raise ValueError('Invalid fixed universe')
    return chosen


class LimitedFetcher(data.Fetcher):
    def bytes(self, url: str, interval: float = .21) -> bytes:
        with _network_lock:
            time.sleep(.20)
        return super().bytes(url, max(interval, .35))


def fetch_series(root: Path, instrument: dict, bar: str) -> dict:
    symbol = instrument['symbol']
    start = max(data.parse_utc('2022-03-01T00:00:00Z'), instrument['list_time_ms'])
    end = data.parse_utc('2026-11-01T00:00:00Z')
    fetcher = LimitedFetcher()
    if bar == 'funding':
        rows, sources = data.fetch_funding(fetcher, symbol, start, end)
        stamps = [int(r['fundingTime']) for r in rows]
        meta = dict(count=len(rows), first=data.iso(stamps[0]) if stamps else None,
                    last=data.iso(stamps[-1]) if stamps else None, source_files=sources,
                    max_interval_hours=max((b-a)/3_600_000 for a,b in zip(stamps,stamps[1:])) if len(stamps)>1 else None,
                    requested_start=data.iso(start), requested_end=data.iso(end))
    else:
        rows = data.fetch_candles(fetcher, symbol, bar, start, end)
        meta = data.describe_candles(rows, bar, start, end)
        step = data.STEPS[bar]
        meta['expected_first_closed_bar_start'] = data.iso(((start+step-1)//step)*step)
        meta['expected_last_bar_start'] = data.iso(end-step)
    meta['sha256'] = data.write_rows(data.series_path(root, symbol, bar), rows)
    meta['status'] = 'OK' if rows else 'EMPTY'
    return meta


def download(chosen: dict, commit: str) -> None:
    if (ROOT/'RUN_LOCK.json').exists():
        raise FileExistsError('Experiment already started; data download is locked')
    root = ROOT/'data'
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root/'manifest-v3.json'
    manifest = data.load_manifest(root)
    if manifest and manifest.get('_provenance', {}).get('selection_commit') != commit:
        raise ValueError('Data belongs to another selection')
    manifest['_provenance'] = dict(selection_commit=commit, frozen_commit=selection.FROZEN_COMMIT,
                                   snapshot_utc=chosen['snapshot_utc'], started_at=now())
    tasks = []
    for item in chosen['selected']:
        symbol = item['symbol']
        manifest.setdefault(symbol, {})['instrument'] = dict(tickSz=item['tick_size'],
                                                             listTime=item['list_time_utc'], state='live')
        for bar in BARS:
            old = manifest[symbol].get(bar)
            path = data.series_path(root, symbol, bar)
            if old and old.get('sha256'):
                if not path.exists() or data.rows_sha(path) != old['sha256']:
                    raise ValueError(f'Changed downloaded data: {symbol} {bar}')
                continue
            tasks.append((item, bar))
    save(manifest_path, manifest)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(fetch_series,root,item,bar):(item['symbol'],bar) for item,bar in tasks}
        for index, future in enumerate(as_completed(futures), 1):
            symbol, bar = futures[future]
            try:
                meta = future.result()
            except Exception as exc:
                meta = dict(status='MISSING', error=f'{type(exc).__name__}: {exc}')
            manifest[symbol][bar] = meta
            save(manifest_path, manifest)
            print(f'{index}/{len(tasks)} {symbol} {bar} {meta["status"]} count={meta.get("count",0)}', flush=True)
    manifest['_provenance']['completed_at'] = now()
    save(manifest_path, manifest)


def verify_data(chosen: dict, manifest: dict, root: Path) -> list[dict]:
    issues = []
    for item in chosen['selected']:
        symbol = item['symbol']
        for bar in BARS:
            meta = manifest.get(symbol, {}).get(bar)
            if meta is None:
                raise ValueError(f'Download has not completed: {symbol} {bar}')
            if meta.get('sha256'):
                path = data.series_path(root, symbol, bar)
                if not path.exists() or data.rows_sha(path) != meta['sha256']:
                    raise ValueError(f'Hash mismatch: {symbol} {bar}')
            elif meta.get('status') != 'MISSING':
                raise ValueError(f'Unverifiable series: {symbol} {bar}')
            reasons = []
            if meta.get('status') != 'OK': reasons.append(meta.get('status','MISSING'))
            if meta.get('gap_count',0): reasons.append('CANDLE_GAPS')
            if bar != 'funding' and meta.get('status') == 'OK':
                if meta['first'] != meta['expected_first_closed_bar_start']: reasons.append('INCOMPLETE_START')
                if meta['last'] != meta['expected_last_bar_start']: reasons.append('INCOMPLETE_END')
            if bar == 'funding' and (meta.get('max_interval_hours') or 0)>9:
                reasons.append('FUNDING_INTERVAL_OVER_9H')
            if reasons: issues.append(dict(symbol=symbol, series=bar, reasons=reasons, details=meta))
    return issues


def acquire_run_lock(path: Path, metadata: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as handle:
        json.dump(metadata, handle, indent=2)


def criteria(metrics: dict, seeds: list[dict]) -> tuple[list[dict], dict]:
    if sorted(r['seed'] for r in seeds) != list(range(200)):
        raise ValueError('Expected exactly seeds 0..199 without duplicates')
    means = [r['mean_r'] for r in seeds if r['mean_r'] is not None]
    strategy_mean = metrics['mean_r']
    percentile = (100*sum(v<strategy_mean for v in means)/200
                  if len(means)==200 and strategy_mean is not None else None)
    p90 = sorted(means)[math.ceil(.9*len(means))-1] if means else None
    specifications = [('Trade-uri eligibile', metrics['n'], '>= 60', lambda x:x>=60),
                      ('R mediu net', strategy_mean, '>= 0.15', lambda x:x>=.15),
                      ('Profit factor net', metrics['profit_factor'], '>= 1.2', lambda x:x>=1.2),
                      ('R mediu net, costuri ×2', metrics['mean_r_cost_x2'], '> 0', lambda x:x>0),
                      ('Percentila strictă față de baseline', percentile, '> 90', lambda x:x>90)]
    rows = [dict(criterion=name, value=value, threshold=threshold,
                 verdict='PASS' if value is not None and check(value) else 'FAIL')
            for name,value,threshold,check in specifications]
    return rows, dict(seeds=200, valid_seeds=len(means), p90_mean_r=p90,
                      strict_percentile=percentile, baseline_variant=BASELINE.key,
                      percentile_formula='100 * count(seed_mean < strategy_mean) / 200')


def init_baseline(market, config, costs, prepared):
    global _baseline_inputs
    _baseline_inputs = market, config, costs, prepared


def baseline_seed(seed: int) -> dict:
    market, config, costs, prepared = _baseline_inputs
    result = engine.run_variant(market, config, costs, BASELINE, prepared, random_seed=seed)
    metrics = report.metrics(result.trades)
    return dict(seed=seed, **metrics)


def write_report(result, seeds, chosen, manifest, provenance, issues, root: Path) -> None:
    metrics = report.metrics(result.trades)
    checks, baseline = criteria(metrics, seeds)
    verdict = 'PASS' if all(r['verdict']=='PASS' for r in checks) else 'FAIL'
    by_symbol = {r['symbol']:report.metrics([t for t in result.trades if t['symbol']==r['symbol']])
                 for r in chosen['selected']}
    by_direction = {side:report.metrics([t for t in result.trades if t['direction']==side]) for side in ('LONG','SHORT')}
    funnel = dict(Counter(r['status'] for r in result.transitions))
    funnel['TRADES_ALL'] = len(result.trades)
    funnel['TRADES_FUNDING_COMPLETE'] = metrics['n']
    payload = dict(verdict=verdict, criteria=checks, metrics=metrics, baseline=baseline,
                   provenance=provenance, transition_funnel=funnel,
                   setup_statuses=dict(Counter(r['status'] for r in result.setups)),
                   by_symbol=by_symbol, by_direction=by_direction, data_issues=issues,
                   funding_incomplete=[t for t in result.trades if not t['funding_complete']],
                   baseline_seed_metrics=seeds)
    save(root/'report.json', payload)
    f = report.fmt
    lines = ['# V3 — test unic pe univers nou', '', f'Verdict automat: **{verdict}**', '',
             '| Criteriu | Valoare | Prag | Verdict |', '|---|---:|---|---|']
    for row in checks:
        lines.append(f"| {row['criterion']} | {f(row['value'],6)} | {row['threshold']} | {row['verdict']} |")
    lines += ['', f"Selecție: `{provenance['selection_commit']}`.",
              f"Scripturi: `{provenance['scripts_commit']}`. Reguli: `{selection.FROZEN_COMMIT}`.",
              f"Snapshot univers: {chosen['snapshot_utc']}. O singură serie, fără split.",
              'Selecția folosește lichiditatea și instrumentele live la snapshot, nu un univers istoric reconstruit.',
              'Prioritatea simultană păstrează regula ordinii simbolurilor configurate: aici rangul selecției fixate.',
              'Costul implicit pentru monedele noi este cel din costs.json; fișierul nu a fost modificat.', '',
              '## Metrici', '', '| N eligibil | N total | Win rate | R mediu net | PF | DD R | DD % | Durată medie h | R costuri ×2 |',
              '|---:|---:|---:|---:|---:|---:|---:|---:|---:|',
              f"| {metrics['n']} | {metrics['n_all']} | {f(metrics['win_rate'])} | {f(metrics['mean_r'])} | {f(metrics['profit_factor'])} | {f(metrics['max_drawdown_r'])} | {f(100*metrics['max_drawdown_pct'])} | {f(metrics['duration_mean_h'])} | {f(metrics['mean_r_cost_x2'])} |", '',
              f"FUNDING_INCOMPLETE: {metrics['funding_incomplete']}; excluse din metricile principale conform specificației.",
              f"DATA_END: {sum(t['outcome']=='DATA_END' for t in result.trades)}.", '', '## Baseline', '',
              '200 seed-uri fixe 0…199, secțiunea 22: confirmare aleatoare în fereastra existentă, market la următorul open.',
              f'Baseline: `{BASELINE.key}` cu random_seed. Strategia: `{VARIANT.key}` fără random_seed.',
              f"Seed-uri cu R mediu definit: {baseline['valid_seeds']}/200. P90 nearest-rank: {f(baseline['p90_mean_r'],6)}R.",
              'Criteriul 5 folosește procentul strict de medii aleatoare sub media strategiei; egalitățile nu sunt depășite.', '',
              '## Funnel și respingeri', '', '```json', json.dumps(dict(transitions=funnel,statuses=payload['setup_statuses']),indent=2),'```']
    for title, mapping in [('Pe monedă',by_symbol),('Pe direcție',by_direction)]:
        lines += ['', '## '+title, '', '| Grup | N | Win rate | R mediu net | PF | DD R |', '|---|---:|---:|---:|---:|---:|']
        for label,m in mapping.items():
            lines.append(f"| {label} | {m['n']} | {f(m['win_rate'])} | {f(m['mean_r'])} | {f(m['profit_factor'])} | {f(m['max_drawdown_r'])} |")
    lines += ['', '## Date lipsă sau incomplete', '',
              'Nicio monedă nu a fost înlocuită. Golurile sunt raportate; datele nu au fost interpolate.',
              '```json',json.dumps(serialization.json_safe(issues),indent=2),'```', '',
              '## Cele 40 de monede', '', ', '.join(r['symbol'] for r in chosen['selected']), '',
              'Manifest: data/manifest-v3.json. Tranzacții: trades.csv. Tranziții: transitions.csv.',
              'Documentația unității volumului: https://www.okx.com/docs-v5/en/#order-book-trading-market-data-get-tickers',
              'Rezultate exploratorii; fără ajustări sau rerulări după observarea rezultatelor.']
    (root/'report.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')


def execute(chosen: dict, commit: str) -> None:
    root = ROOT/'data'
    manifest = data.load_manifest(root)
    if manifest.get('_provenance',{}).get('selection_commit') != commit:
        raise ValueError('Manifest selection commit mismatch')
    issues = verify_data(chosen, manifest, root)
    provenance = dict(selection_commit=commit, scripts_commit=selection.git('rev-parse','HEAD').decode().strip(),
                      frozen_commit=selection.FROZEN_COMMIT, started_at=now(),
                      selection_sha256=hashlib.sha256(selection.OUTPUT.read_bytes()).hexdigest(),
                      manifest_sha256=hashlib.sha256((root/'manifest-v3.json').read_bytes()).hexdigest(),
                      frozen_files=selection.verify_frozen())
    config = dict(engine.load_config(), symbols=[r['symbol'] for r in chosen['selected']])
    costs = json.loads((selection.ROOT/'backtest/costs.json').read_text())
    market = data.load_market(root, config['symbols'])
    acquire_run_lock(ROOT/'RUN_LOCK.json', provenance)
    print('ONE-SHOT EXPERIMENT STARTED', flush=True)
    prepared = engine.prepare_market(market,config)
    result = engine.run_variant(market,config,costs,VARIANT,prepared)
    serialization.write_csv(ROOT/'trades.csv',result.trades)
    serialization.write_csv(ROOT/'setups.csv',serialization.collapse_setups(result.setups))
    serialization.write_csv(ROOT/'transitions.csv',result.transitions)
    save(ROOT/'strategy-completed.json',dict(completed_at=now(),metrics=report.metrics(result.trades)))
    print('Strategy finished; baseline seeds 0..199 starting',flush=True)
    seeds = []
    with ProcessPoolExecutor(max_workers=2, initializer=init_baseline,
                             initargs=(market,config,costs,prepared)) as pool:
        futures = {pool.submit(baseline_seed,seed):seed for seed in range(200)}
        for future in as_completed(futures):
            row = future.result()
            save(ROOT/'baseline'/f"seed-{row['seed']:03d}.json",row)
            seeds.append(row)
            print(f'Baseline {len(seeds)}/200 completed (seed {row["seed"]})',flush=True)
    seeds.sort(key=lambda r:r['seed'])
    provenance['completed_at'] = now()
    write_report(result,seeds,chosen,manifest,provenance,issues,ROOT)
    save(ROOT/'COMPLETE.json',provenance)
    print('COMPLETE '+str(ROOT/'report.md'),flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('download','run'))
    parser.add_argument('--selection-commit',required=True)
    args = parser.parse_args(argv)
    chosen = read_selection(args.selection_commit)
    if args.command=='download': download(chosen,args.selection_commit)
    else: execute(chosen,args.selection_commit)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
