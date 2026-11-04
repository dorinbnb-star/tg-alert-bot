"""Freeze an OKX universe snapshot before downloading any backtest inputs."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import subprocess

from backtest import v3_data

ROOT = Path(__file__).resolve().parents[1]
FROZEN_COMMIT = '10ff9ebefb828c14828e6db5c5448a96e5c60191'
OUTPUT = ROOT / 'backtest/v3_universe.json'
RUN_ROOT = ROOT.parent / 'v3-oos-universe'
EXCLUDED = tuple(x+'-USDT-SWAP' for x in
                 'BTC ETH SOL XRP BNB AVAX LINK LTC SUI UNI AAVE TIA WLD WIF STRK VIRTUAL ENA YGG'.split())
STABLE_FIAT = frozenset('USDT USDC FDUSD USD EUR EURC EURCV EURT DAI USDS USDE USD1 USDG '
                       'USDD USDP TUSD PYUSD RLUSD FRAX USDF GUSD BUSD USDD USDQ '
                       'GBP GBPT JPY JPYT CHF AUD CAD BRL TRY EURI AEUR EURR XAUT PAXG'.split())
CUTOFF = v3_data.parse_utc('2025-11-01T00:00:00Z')
FROZEN_FILES = ('v3_strategy.py', 'v3_engine.py', 'v3_config.json', 'costs.json',
                'SPEC-v3-canal.md', 'v3_data.py', 'v3_report.py', 'v3_run.py')


def git(*args: str) -> bytes:
    return subprocess.check_output(['git', *args], cwd=ROOT)


def verify_frozen() -> dict:
    hashes = {}
    for name in FROZEN_FILES:
        path = 'backtest/'+name
        original = git('show', f'{FROZEN_COMMIT}:{path}')
        actual = (ROOT/path).read_bytes().replace(b'\r\n', b'\n')
        if actual != original:
            raise ValueError(f'Frozen file differs from {FROZEN_COMMIT}: {path}')
        hashes[path] = hashlib.sha256(original).hexdigest()
    return hashes


def select(snapshot: dict, crypto_only: bool, count: int = 40) -> dict:
    instruments = snapshot['instruments']
    tickers = {row['instId']: row for row in snapshot['tickers']}
    if len(tickers) != len(snapshot['tickers']):
        raise ValueError('Duplicate ticker identifiers')
    if len({r['instId'] for r in instruments}) != len(instruments):
        raise ValueError('Duplicate instrument identifiers')
    candidates, excluded = [], []
    for instrument in instruments:
        symbol = instrument['instId']
        base = symbol.removesuffix('-USDT-SWAP')
        reasons = []
        if symbol in EXCLUDED:
            reasons.append('PREVIOUS_UNIVERSE')
        if base in STABLE_FIAT:
            reasons.append('STABLECOIN_OR_FIAT_LINKED')
        if instrument.get('instType') != 'SWAP' or not symbol.endswith('-USDT-SWAP'):
            reasons.append('NOT_USDT_PERPETUAL')
        if instrument.get('settleCcy') != 'USDT' or instrument.get('ctType') != 'linear':
            reasons.append('NOT_LINEAR_USDT_SETTLED')
        if instrument.get('state') != 'live':
            reasons.append('NOT_LIVE')
        if crypto_only and instrument.get('instCategory') != '1':
            reasons.append('NON_CRYPTO_OR_UNKNOWN_CATEGORY')
        try:
            listing = int(instrument.get('listTime') or 0)
        except ValueError:
            listing = 0
        if listing <= 0 or listing > CUTOFF:
            reasons.append('INSUFFICIENT_LISTING_HISTORY')
        ticker = tickers.get(symbol, {})
        try:
            volume, price = Decimal(ticker['volCcy24h']), Decimal(ticker['last'])
            if not volume.is_finite() or not price.is_finite() or volume < 0 or price <= 0:
                raise ValueError('Invalid ticker')
            notional = volume * price
        except (KeyError, InvalidOperation, ValueError):
            reasons.append('MISSING_OR_INVALID_TICKER')
            notional = None
        row = dict(symbol=symbol, list_time_ms=listing, list_time_utc=v3_data.iso(listing),
                   inst_category=instrument.get('instCategory'), tick_size=instrument.get('tickSz'),
                   volume_usdt=str(notional) if notional is not None else None,
                   vol_ccy_24h=ticker.get('volCcy24h'), price=ticker.get('last'),
                   ticker_timestamp_ms=ticker.get('ts'))
        if reasons:
            excluded.append(dict(row, reasons=reasons))
        else:
            candidates.append(row)
    candidates.sort(key=lambda row: (-Decimal(row['volume_usdt']), row['symbol']))
    if len(candidates) < count:
        raise ValueError(f'Only {len(candidates)} eligible instruments; need {count}')
    selected = [dict(row, rank=index) for index, row in enumerate(candidates[:count], 1)]
    excluded.extend(dict(row, reasons=['OUTSIDE_TOP_40']) for row in candidates[count:])
    return dict(schema_version=1, frozen_commit=FROZEN_COMMIT,
                snapshot_utc=snapshot['snapshot_utc'], selection_count=count,
                source_endpoints=['/api/v5/public/instruments?instType=SWAP',
                                  '/api/v5/market/tickers?instType=SWAP'],
                documentation='https://www.okx.com/docs-v5/en/#order-book-trading-market-data-get-tickers',
                rule=dict(volume='volCcy24h (base currency) * last (USDT); not contract count',
                          ordering='volume_usdt descending, instId alphabetical for ties',
                          list_time_cutoff_utc='2025-11-01T00:00:00Z',
                          crypto_only=crypto_only, category='instCategory=1' if crypto_only else None,
                          priority='selection rank, preserving the frozen engine configured-symbol-order rule'),
                previously_used=list(EXCLUDED), stablecoin_fiat_exclusions=sorted(STABLE_FIAT),
                selected=selected, excluded=sorted(excluded, key=lambda row: row['symbol']),
                preregistration=dict(variant='ENTRY_BREAKOUT_NEXT_OPEN__REF_RR2',
                                     start_utc='2022-03-01T00:00:00Z', end_exclusive_utc='2026-11-01T00:00:00Z',
                                     seeds=list(range(200)),
                                     baseline_variant='ENTRY_MARKET_NEXT_OPEN__REF_RR2',
                                     baseline_rule='Frozen section 22 random confirmation, market at next open; no retest',
                                     criteria={'eligible_trades_min':60, 'mean_net_r_min':0.15,
                                               'profit_factor_min':1.2, 'mean_cost_x2_strict_min':0,
                                               'baseline_percentile_strict_min':90},
                                     percentile='100 * count(seed_mean < strategy_mean) / 200 > 90; require 200 valid seed means; report nearest-rank P90 also',
                                     funding_incomplete='Exclude from primary metrics, report separately'))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, default=RUN_ROOT/'selection-snapshot.json')
    parser.add_argument('--crypto-only', action='store_true')
    args = parser.parse_args(argv)
    if OUTPUT.exists():
        raise FileExistsError('Universe already fixed; refusing to replace it')
    hashes = verify_frozen()
    if not args.snapshot.exists():
        fetcher = v3_data.Fetcher()
        instruments = fetcher.json('/api/v5/public/instruments', {'instType':'SWAP'})['data']
        tickers = fetcher.json('/api/v5/market/tickers', {'instType':'SWAP'})['data']
        snapshot = dict(snapshot_utc=datetime.now(timezone.utc).isoformat(),
                        instruments=instruments, tickers=tickers)
        args.snapshot.parent.mkdir(parents=True, exist_ok=True)
        with args.snapshot.open('x', encoding='utf-8') as handle:
            json.dump(snapshot, handle, indent=2)
    snapshot = json.loads(args.snapshot.read_text(encoding='utf-8'))
    result = select(snapshot, args.crypto_only)
    result['snapshot_sha256'] = hashlib.sha256(args.snapshot.read_bytes()).hexdigest()
    result['frozen_file_sha256'] = hashes
    with OUTPUT.open('x', encoding='utf-8') as handle:
        json.dump(result, handle, indent=2, ensure_ascii=False)
        handle.write('\n')
    for row in result['selected']:
        print(row['rank'], row['symbol'], row['volume_usdt'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
