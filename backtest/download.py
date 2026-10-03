#!/usr/bin/env python3
"""Resumable download of OKX public history for the backtest (runner only; data never goes into the repo).

Commands:
  candles  4H/1H/15m history for every config symbol, plus funding-rate-history
  golden   the live windows around the run 369 scan (golden test data)
  minute   1m candles only around the detected signals (needs simulate.py detect output)
  docs     re-confirm fees / rate limits / funding depth from the official OKX pages
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_crypto_monitor import entry_monitor as em  # noqa: E402

OKX = "https://www.okx.com"
DATA_DIR = PROJECT_ROOT / "backtest_data"
RESULTS_DIR = PROJECT_ROOT / "backtest_results"
CONFIG_PATH = PROJECT_ROOT / "ai_crypto_monitor" / "config-v0.1.json"
RULES_PATH = PROJECT_ROOT / "ai_crypto_monitor" / "rules-v0.1.json"
GOLDEN_PATH = Path(__file__).resolve().parent / "tests" / "golden_run369.json"

# Fixed window, so the same command on the same data gives the same result.
START = "2025-10-03T00:00:00Z"
SPLIT = "2026-06-03T00:00:00Z"
END = "2026-10-03T00:00:00Z"
# Warm-up before START so the first scan already has the live 240/160/160 windows.
WARMUP_DAYS = {"240": 45, "60": 10, "15": 3}
PAGE_LIMIT = 300  # history-candles maximum per OKX docs; pagination works with any smaller page the API returns
MIN_REQUEST_INTERVAL = 0.125  # <= 8 req/s, under the documented 20 req/2s (candles) budget
FUNDING_MIN_INTERVAL = 0.25  # <= 4 req/s, under the documented 10 req/2s (funding) budget
RETRY_CODES = {"50011", "50001", "50013", "50026"}

MINUTE = 60_000
QUARTER = 15 * MINUTE


def parse_utc(text: str) -> int:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_rules() -> dict:
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


def symbols() -> list[str]:
    return [str(item).upper().strip() for item in load_config()["symbols"]]


def bar(interval: str) -> str:
    return "1m" if interval == "1" else em.OKX_BARS[interval]


def duration_ms(interval: str) -> int:
    return MINUTE if interval == "1" else em.interval_ms(interval)


# ---------------------------------------------------------------- storage

def series_path(root: Path, symbol: str, name: str) -> Path:
    return root / symbol / f"{name}.jsonl.gz"


def write_rows(path: Path, rows: list) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows).encode("utf-8")
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as handle:
        with gzip.GzipFile(fileobj=handle, mode="wb", mtime=0) as archive:  # mtime=0: byte-identical output
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


def load_manifest(root: Path) -> dict:
    path = root / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_manifest(root: Path, manifest: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- HTTP

class Fetcher:
    def __init__(self, base_url: str = OKX):
        self.base_url = base_url
        self.last_call = 0.0
        self.requests = 0

    def get(self, path: str, params: dict, min_interval: float = MIN_REQUEST_INTERVAL) -> list:
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        delay = 1.0
        for attempt in range(7):
            wait = self.last_call + min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last_call = time.monotonic()
            self.requests += 1
            retry_reason = ""
            try:
                request = urllib.request.Request(url, headers=em.OKX_HEADERS)
                with urllib.request.urlopen(request, timeout=20) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if isinstance(payload, dict) and payload.get("code") in RETRY_CODES:
                    retry_reason = f"OKX code {payload.get('code')}"
                else:
                    return em.okx_data(payload, path)
            except urllib.error.HTTPError as exc:
                if exc.code != 429 and exc.code < 500:
                    raise em.MonitorError(f"HTTP {exc.code} la {path}") from None
                retry_reason = f"HTTP {exc.code}"
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                retry_reason = type(exc).__name__
            print(f"retry {attempt + 1} {path} {params.get('instId', '')}: {retry_reason}", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 30)
        raise em.MonitorError(f"{path} esuat dupa reincercari")


def fetch_candles(fetcher: Fetcher, symbol: str, interval: str, start_ms: int, end_ms: int,
                  page_limit: int = PAGE_LIMIT) -> list[list]:
    """All rows with start in [start_ms, end_ms), ascending, from /market/history-candles."""
    found: dict[int, list] = {}
    after = end_ms
    while True:
        params = {"instId": symbol, "bar": bar(interval), "after": str(after), "limit": str(page_limit)}
        try:
            data = fetcher.get("/api/v5/market/history-candles", params)
        except em.MonitorError:
            if page_limit <= 100:
                raise
            page_limit = 100  # the API's long-standing default page if the larger documented page is refused
            print(f"{symbol} {bar(interval)}: limit {params['limit']} refuzat, continui cu 100", flush=True)
            continue
        if not data:
            break
        oldest = after
        for row in data:
            ts = int(row[0])
            oldest = min(oldest, ts)
            if start_ms <= ts < end_ms:
                found[ts] = [str(value) for value in row]
        if oldest >= after or oldest <= start_ms:
            break
        after = oldest
    return [found[ts] for ts in sorted(found)]


def fetch_funding(fetcher: Fetcher, symbol: str, end_ms: int) -> list[dict]:
    found: dict[int, dict] = {}
    after = end_ms
    while True:
        data = fetcher.get(
            "/api/v5/public/funding-rate-history",
            {"instId": symbol, "after": str(after), "limit": "400"},
            min_interval=FUNDING_MIN_INTERVAL,
        )
        if not data:
            break
        oldest = after
        for item in data:
            ts = int(item["fundingTime"])
            oldest = min(oldest, ts)
            if ts < end_ms:
                found[ts] = {key: item.get(key) for key in ("fundingTime", "fundingRate", "realizedRate", "method")}
        if oldest >= after:
            break
        after = oldest
    return [found[ts] for ts in sorted(found)]


# ---------------------------------------------------------------- validation

def describe_series(rows: list[list], interval: str, start_ms: int, end_ms: int) -> dict:
    step = duration_ms(interval)
    stamps = [int(row[0]) for row in rows]
    gaps = []
    missing = 0
    for earlier, later in zip(stamps, stamps[1:]):
        if later - earlier != step:
            missing += max((later - earlier) // step - 1, 0)
            gaps.append([iso(earlier), iso(later)])
    return {
        "bar": bar(interval),
        "requested_start": iso(start_ms),
        "requested_end": iso(end_ms),
        "first": iso(stamps[0]) if stamps else None,
        "last": iso(stamps[-1]) if stamps else None,
        "count": len(stamps),
        "duplicates": len(stamps) - len(set(stamps)),
        "unconfirmed": sum(1 for row in rows if len(row) < 9 or row[8] != "1"),
        "missing_inside": missing,
        "gaps": gaps[:10],
        "gap_count": len(gaps),
        "missing_before_first": max((stamps[0] - start_ms) // step, 0) if stamps else None,
    }


def ensure_series(fetcher: Fetcher, root: Path, manifest: dict, symbol: str, interval: str,
                  start_ms: int, end_ms: int) -> dict:
    path = series_path(root, symbol, bar(interval))
    entry = manifest.get(symbol, {}).get(bar(interval))
    if (
        entry and path.exists() and entry.get("requested_start") == iso(start_ms)
        and entry.get("requested_end") == iso(end_ms) and entry.get("sha256") == rows_sha(path)
    ):
        print(f"skip {symbol} {bar(interval)}: deja descarcat ({entry['count']} lumanari)", flush=True)
        return entry
    rows = fetch_candles(fetcher, symbol, interval, start_ms, end_ms)
    info = describe_series(rows, interval, start_ms, end_ms)
    info["sha256"] = write_rows(path, rows)
    manifest.setdefault(symbol, {})[bar(interval)] = info
    save_manifest(root, manifest)
    print(f"{symbol} {bar(interval)}: {info['count']} lumanari {info['first']} -> {info['last']}, "
          f"lipsa interioare {info['missing_inside']}, neconfirmate {info['unconfirmed']}", flush=True)
    return info


def command_candles(args: argparse.Namespace) -> int:
    root = Path(args.data_dir)
    config = load_config()
    manifest = load_manifest(root)
    fetcher = Fetcher()
    end_ms = parse_utc(args.end)
    start_ms = parse_utc(args.start)
    for symbol in symbols():
        for key in ("context", "structure", "trigger"):
            interval = config["intervals"][key]
            warm = start_ms - WARMUP_DAYS[interval] * 86_400_000
            ensure_series(fetcher, root, manifest, symbol, interval, warm, end_ms)
        funding_path = series_path(root, symbol, "funding")
        entry = manifest.get(symbol, {}).get("funding")
        if not (entry and funding_path.exists() and entry.get("requested_end") == iso(end_ms)
                and entry.get("sha256") == rows_sha(funding_path)):
            items = fetch_funding(fetcher, symbol, end_ms)
            stamps = [int(item["fundingTime"]) for item in items]
            info = {
                "requested_end": iso(end_ms),
                "count": len(items),
                "first": iso(stamps[0]) if stamps else None,
                "last": iso(stamps[-1]) if stamps else None,
                "sha256": write_rows(funding_path, items),
            }
            manifest.setdefault(symbol, {})["funding"] = info
            save_manifest(root, manifest)
            print(f"{symbol} funding: {info['count']} intrari reale {info['first']} -> {info['last']}", flush=True)
    print(f"cereri OKX: {fetcher.requests}", flush=True)
    return 0


def command_golden(args: argparse.Namespace) -> int:
    root = Path(args.data_dir) / "golden"
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    config = load_config()
    manifest = load_manifest(root)
    fetcher = Fetcher()
    for symbol, item in golden["symbols"].items():
        scan_ms = parse_utc(item["detected_at"])
        for key in ("context", "structure", "trigger"):
            interval = config["intervals"][key]
            step = em.interval_ms(interval)
            limit = int(config["history_limits"][key])
            end = (scan_ms // step) * step + step  # include the candle that was still open at the scan
            start = end - (limit + 10) * step
            ensure_series(fetcher, root, manifest, symbol, interval, start, end)
    print(f"cereri OKX: {fetcher.requests}", flush=True)
    return 0


def command_minute(args: argparse.Namespace) -> int:
    """1m candles for the 15m candle that contains each detection's scan time (entry price + first partial candle)."""
    root = Path(args.data_dir)
    detections = Path(args.detections)
    needed: dict[str, set[int]] = {}
    with detections.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            needed.setdefault(row["symbol"], set()).add((int(row["scan_ms"]) // QUARTER) * QUARTER)
    manifest = load_manifest(root)
    fetcher = Fetcher()
    for symbol in sorted(needed):
        path = series_path(root, symbol, "1m")
        existing = {int(row[0]): row for row in read_rows(path)}
        fetched = 0
        incomplete = []
        for block in sorted(needed[symbol]):
            minutes = range(block, block + QUARTER, MINUTE)
            if all(ts in existing for ts in minutes):
                continue
            for row in fetch_candles(fetcher, symbol, "1", block, block + QUARTER, page_limit=100):
                existing[int(row[0])] = row
            fetched += 1
            if not all(ts in existing for ts in minutes):
                incomplete.append(iso(block))
        rows = [existing[ts] for ts in sorted(existing)]
        manifest.setdefault(symbol, {})["1m"] = {
            "blocks_needed": len(needed[symbol]),
            "blocks_incomplete": len(incomplete),
            "incomplete_examples": incomplete[:10],
            "count": len(rows),
            "sha256": write_rows(path, rows),
        }
        save_manifest(root, manifest)
        print(f"{symbol} 1m: {len(needed[symbol])} blocuri 15m, descarcate acum {fetched}, incomplete {len(incomplete)}",
              flush=True)
    print(f"cereri OKX: {fetcher.requests}", flush=True)
    return 0


# ---------------------------------------------------------------- official docs re-confirmation

DOC_CHECKS = [
    {
        "id": "history_candles",
        "url": "https://www.okx.com/docs-v5/en/#public-data-rest-api-get-candlesticks-history",
        "fetch": "https://www.okx.com/docs-v5/en/",
        "anchor": "GET /api/v5/market/history-candles",
        "patterns": [r"Rate Limit:[^.]{0,60}", r"The maximum is \d+[^.]{0,30}", r"records earlier than[^.]{0,40}"],
    },
    {
        "id": "funding_rate_history",
        "url": "https://www.okx.com/docs-v5/en/#public-data-rest-api-get-funding-rate-history",
        "fetch": "https://www.okx.com/docs-v5/en/",
        "anchor": "GET /api/v5/public/funding-rate-history",
        "patterns": [r"[^.]{0,80}(?:3|three) months[^.]{0,40}", r"Rate Limit:[^.]{0,60}", r"The maximum is \d+[^.]{0,30}"],
    },
    {
        "id": "candles_confirm",
        "url": "https://www.okx.com/docs-v5/en/#public-data-rest-api-get-candlesticks",
        "fetch": "https://www.okx.com/docs-v5/en/",
        "anchor": "GET /api/v5/market/candles",
        "patterns": [r"confirm[^.]{0,120}"],
    },
    {
        "id": "fees",
        "url": "https://www.okx.com/fees",
        "fetch": "https://www.okx.com/fees",
        "anchor": "",
        "patterns": [r"[^.<]{0,60}[Tt]aker[^.<]{0,60}0\.0[0-9]+%[^.<]{0,40}", r"0\.050%[^.<]{0,40}"],
    },
    {
        "id": "fees_help",
        "url": "https://www.okx.com/help/how-to-calculate-the-contract-transaction-fee",
        "fetch": "https://www.okx.com/help/how-to-calculate-the-contract-transaction-fee",
        "anchor": "",
        "patterns": [r"[^.<]{0,80}[Tt]aker[^.<]{0,80}"],
    },
]


def page_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": em.OKX_HEADERS["User-Agent"], "Accept": "text/html"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read().decode("utf-8", errors="replace")
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text)


def command_docs(args: argparse.Namespace) -> int:
    pages: dict[str, str] = {}
    results = []
    for check in DOC_CHECKS:
        item = {"id": check["id"], "url": check["url"], "found": [], "error": ""}
        try:
            if check["fetch"] not in pages:
                pages[check["fetch"]] = page_text(check["fetch"])
            text = pages[check["fetch"]]
            if check["anchor"]:
                index = text.find(check["anchor"])
                text = text[index:index + 6000] if index >= 0 else ""
                if index < 0:
                    item["error"] = "ancora nu a fost gasita in pagina"
            for pattern in check["patterns"]:
                match = re.search(pattern, text)
                if match:
                    item["found"].append(match.group(0).strip())
        except Exception as exc:  # documentation is evidence only; the report states what could not be confirmed
            item["error"] = f"{type(exc).__name__}: {exc}"[:200]
        results.append(item)
        print(f"[docs] {item['id']}: {item['found'] or item['error'] or 'nimic gasit'}", flush=True)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"checked_at": iso(int(time.time() * 1000)), "checks": results}, indent=2,
                              ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    sub = parser.add_subparsers(dest="command", required=True)
    candles = sub.add_parser("candles")
    candles.add_argument("--start", default=START)
    candles.add_argument("--end", default=END)
    sub.add_parser("golden")
    minute = sub.add_parser("minute")
    minute.add_argument("--detections", default=str(RESULTS_DIR / "detections.jsonl"))
    docs = sub.add_parser("docs")
    docs.add_argument("--out", default=str(RESULTS_DIR / "okx_docs.json"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"candles": command_candles, "golden": command_golden, "minute": command_minute, "docs": command_docs}
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
