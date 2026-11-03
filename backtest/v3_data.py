#!/usr/bin/env python3
"""Download and validate the public OKX inputs used only by backtest v3."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from backtest import v3_engine, v3_strategy

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "backtest_data" / "v3"
RESULTS_DIR = ROOT / "backtest_results" / "v3"
OKX = "https://www.okx.com"
START = "2022-03-01T00:00:00Z"
END = "2026-11-01T00:00:00Z"
HEADERS = {"Accept": "application/json", "User-Agent": "ai-crypto-trader-v3-backtest/1.0"}
BARS = {"1D": "1Dutc", "4H": "4H", "1H": "1H"}
STEPS = {"1D": v3_strategy.DAY_MS, "4H": v3_strategy.FOUR_HOURS_MS, "1H": v3_strategy.HOUR_MS}


def parse_utc(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp() * 1000)


def iso(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def series_path(root: Path, symbol: str, name: str) -> Path:
    return root / symbol / f"{name}.jsonl.gz"


def write_rows(path: Path, rows: list) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = "".join(json.dumps(row, separators=(",", ":"), sort_keys=isinstance(row, dict)) + "\n" for row in rows).encode()
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as handle:
        with gzip.GzipFile(fileobj=handle, mode="wb", mtime=0) as archive:
            archive.write(raw)
    temporary.replace(path)
    return hashlib.sha256(raw).hexdigest()


def read_rows(path: Path) -> list:
    if not path.exists():
        return []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def rows_sha(path: Path) -> str:
    with gzip.open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


class Fetcher:
    def __init__(self, base_url: str = OKX):
        self.base_url = base_url
        self.last_call = 0.0
        self.requests = 0

    def bytes(self, url: str, interval: float = 0.21) -> bytes:
        delay = 1.0
        for attempt in range(7):
            wait = self.last_call + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last_call = time.monotonic()
            self.requests += 1
            try:
                request = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                if exc.code not in (429,) and exc.code < 500:
                    raise
            except (urllib.error.URLError, TimeoutError):
                pass
            if attempt == 6:
                raise RuntimeError(f"OKX request failed after retries: {url}")
            time.sleep(delay)
            delay = min(delay * 2.0, 20.0)
        raise AssertionError("unreachable")

    def json(self, path: str, params: dict, interval: float = 0.21) -> dict:
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        payload = json.loads(self.bytes(url, interval).decode("utf-8"))
        if payload.get("code") != "0":
            raise RuntimeError(f"OKX {path}: {payload.get('code')} {payload.get('msg')}")
        return payload


def fetch_candles(fetcher: Fetcher, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[list[str]]:
    found: dict[int, list[str]] = {}
    cursor = end_ms
    while True:
        data = fetcher.json(
            "/api/v5/market/history-candles",
            {"instId": symbol, "bar": BARS[interval], "after": str(cursor), "limit": "300"},
            0.11,
        )["data"]
        if not data:
            break
        oldest = cursor
        for raw in data:
            timestamp = int(raw[0])
            oldest = min(oldest, timestamp)
            if start_ms <= timestamp < end_ms and (len(raw) < 9 or str(raw[8]) == "1"):
                found[timestamp] = [str(item) for item in raw]
        if oldest >= cursor or oldest <= start_ms:
            break
        cursor = oldest
    return [found[key] for key in sorted(found)]


def describe_candles(rows: list[list], interval: str, start_ms: int, end_ms: int) -> dict:
    timestamps = [int(row[0]) for row in rows]
    step = STEPS[interval]
    gaps = [[iso(left), iso(right)] for left, right in zip(timestamps, timestamps[1:]) if right - left != step]
    return {
        "requested_start": iso(start_ms), "requested_end": iso(end_ms),
        "first": iso(timestamps[0]) if timestamps else None,
        "last": iso(timestamps[-1]) if timestamps else None,
        "count": len(rows), "gap_count": len(gaps), "gaps": gaps[:20],
        "unconfirmed": sum(len(row) >= 9 and str(row[8]) != "1" for row in rows),
    }


def month_windows(start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    start = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
    cursor = datetime(start.year, start.month, 1, tzinfo=timezone.utc)
    out = []
    while int(cursor.timestamp() * 1000) < end_ms:
        # OKX documents a range below ten months; nine calendar months avoids
        # the endpoint's inclusive-boundary 400 response at exactly ten months.
        year = cursor.year + (cursor.month - 1 + 9) // 12
        month = (cursor.month - 1 + 9) % 12 + 1
        next_cursor = datetime(year, month, 1, tzinfo=timezone.utc)
        out.append((int(cursor.timestamp() * 1000), min(int(next_cursor.timestamp() * 1000) - 1, end_ms - 1)))
        cursor = next_cursor
    return out


def funding_files(fetcher: Fetcher, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    family = symbol.removesuffix("-SWAP")
    found: dict[str, dict] = {}
    for begin, end in month_windows(start_ms, end_ms):
        payload = fetcher.json(
            "/api/v5/public/market-data-history",
            {"module": "3", "instType": "SWAP", "instFamilyList": family,
             "dateAggrType": "monthly", "begin": str(begin), "end": str(end)},
            0.41,
        )
        for envelope in payload["data"]:
            for detail in envelope.get("details", []):
                for item in detail.get("groupDetails", []):
                    if item.get("url"):
                        found[item["url"]] = item
    return sorted(found.values(), key=lambda item: int(item.get("dateTs") or item.get("dataTs") or 0))


def fetch_funding(fetcher: Fetcher, symbol: str, start_ms: int, end_ms: int) -> tuple[list[dict], list[str]]:
    found: dict[int, dict] = {}
    sources: list[str] = []
    for item in funding_files(fetcher, symbol, start_ms, end_ms):
        source = item["url"]
        archive = zipfile.ZipFile(io.BytesIO(fetcher.bytes(source, 0.11)))
        sources.append(item.get("filename", source))
        for name in archive.namelist():
            text = archive.read(name).decode("utf-8-sig")
            for row in csv.DictReader(io.StringIO(text)):
                timestamp = int(row.get("funding_time") or row.get("fundingTime"))
                if start_ms <= timestamp < end_ms:
                    found[timestamp] = {
                        "fundingTime": str(timestamp),
                        "fundingRate": row.get("funding_rate") or row.get("fundingRate") or "",
                        "realizedRate": row.get("realized_rate") or row.get("realizedRate") or row.get("funding_rate") or "",
                        "source": item.get("filename", name),
                    }
    recent = fetcher.json(
        "/api/v5/public/funding-rate-history", {"instId": symbol, "limit": "400"}, 0.21,
    )["data"]
    for item in recent:
        timestamp = int(item["fundingTime"])
        if start_ms <= timestamp < end_ms:
            found[timestamp] = {
                "fundingTime": str(timestamp), "fundingRate": item.get("fundingRate", ""),
                "realizedRate": item.get("realizedRate", ""), "source": "funding-rate-history",
            }
    return [found[key] for key in sorted(found)], sources


def fetch_instruments(fetcher: Fetcher) -> dict[str, dict]:
    rows = fetcher.json("/api/v5/public/instruments", {"instType": "SWAP"})["data"]
    return {row["instId"]: row for row in rows}


def load_manifest(root: Path) -> dict:
    path = root / "manifest-v3.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_manifest(root: Path, manifest: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest-v3.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def command_download(args: argparse.Namespace) -> int:
    root = Path(args.data_dir)
    config = v3_engine.load_config()
    manifest = load_manifest(root)
    fetcher = Fetcher()
    start_ms, end_ms = parse_utc(args.start), parse_utc(args.end)
    instruments = fetch_instruments(fetcher)
    for symbol in config["symbols"]:
        instrument = instruments.get(symbol)
        if not instrument:
            raise RuntimeError(f"Instrument unavailable: {symbol}")
        symbol_start = max(start_ms, int(instrument.get("listTime") or start_ms))
        manifest.setdefault(symbol, {})["instrument"] = {
            "tickSz": instrument["tickSz"], "listTime": iso(int(instrument["listTime"])), "state": instrument["state"],
        }
        for interval in ("1D", "4H", "1H"):
            path = series_path(root, symbol, interval)
            existing = manifest[symbol].get(interval, {})
            if (path.exists() and existing.get("requested_start") == iso(symbol_start)
                    and existing.get("requested_end") == iso(end_ms)
                    and existing.get("sha256") == rows_sha(path)):
                print(f"skip {symbol} {interval}: {existing.get('count', 0)} cached")
                continue
            rows = fetch_candles(fetcher, symbol, interval, symbol_start, end_ms)
            info = describe_candles(rows, interval, symbol_start, end_ms)
            info["sha256"] = write_rows(path, rows)
            manifest[symbol][interval] = info
            save_manifest(root, manifest)
            print(f"{symbol} {interval}: {info['count']} {info['first']} -> {info['last']}, gaps={info['gap_count']}")
        path = series_path(root, symbol, "funding")
        existing = manifest[symbol].get("funding", {})
        if (path.exists() and existing.get("requested_start") == iso(symbol_start)
                and existing.get("requested_end") == iso(end_ms)
                and existing.get("sha256") == rows_sha(path)):
            print(f"skip {symbol} funding: {existing.get('count', 0)} cached")
            continue
        funding, sources = fetch_funding(fetcher, symbol, symbol_start, end_ms)
        digest = write_rows(path, funding)
        manifest[symbol]["funding"] = {
            "requested_start": iso(symbol_start), "requested_end": iso(end_ms),
            "count": len(funding), "first": iso(int(funding[0]["fundingTime"])) if funding else None,
            "last": iso(int(funding[-1]["fundingTime"])) if funding else None,
            "source_files": len(sources), "sha256": digest,
        }
        save_manifest(root, manifest)
        print(f"{symbol} funding: {len(funding)} {manifest[symbol]['funding']['first']} -> {manifest[symbol]['funding']['last']}")
    print(f"OKX requests: {fetcher.requests}")
    return 0


def load_market(root: Path, symbols: list[str]) -> dict[str, v3_engine.MarketData]:
    manifest = load_manifest(root)
    out = {}
    for symbol in symbols:
        item = manifest.get(symbol, {})
        out[symbol] = v3_engine.MarketData(
            h1=v3_strategy.candles_from_rows(read_rows(series_path(root, symbol, "1H"))),
            h4=v3_strategy.candles_from_rows(read_rows(series_path(root, symbol, "4H"))),
            daily=v3_strategy.candles_from_rows(read_rows(series_path(root, symbol, "1D"))),
            funding=read_rows(series_path(root, symbol, "funding")),
            tick_size=float(item.get("instrument", {}).get("tickSz", "0.00000001")),
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    parser.add_argument("--start", default=START)
    parser.add_argument("--end", default=END)
    parser.set_defaults(handler=command_download)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
