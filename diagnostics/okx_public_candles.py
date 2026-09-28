#!/usr/bin/env python3
"""Check OKX public candle access from a GitHub-hosted runner."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
INSTRUMENT = "BTC-USDT-SWAP"
INTERVALS = ("1H", "15m")
MINIMUM_CANDLES = 3


class DiagnosticError(RuntimeError):
    pass


def fetch_candles(interval: str) -> tuple[int, dict]:
    query = urllib.parse.urlencode(
        {"instId": INSTRUMENT, "bar": interval, "limit": str(MINIMUM_CANDLES)}
    )
    request = urllib.request.Request(
        f"{OKX_CANDLES_URL}?{query}",
        headers={"Accept": "application/json", "User-Agent": "ai-crypto-trader-diagnostic/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status = response.getcode()
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise DiagnosticError(f"{interval}: HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise DiagnosticError(f"{interval}: network {type(exc).__name__}") from None

    if status != 200:
        raise DiagnosticError(f"{interval}: HTTP {status}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise DiagnosticError(f"{interval}: invalid JSON") from None
    if not isinstance(payload, dict):
        raise DiagnosticError(f"{interval}: JSON root is not an object")
    return status, payload


def validate_candles(interval: str, status: int, payload: dict) -> dict:
    if payload.get("code") != "0":
        raise DiagnosticError(f"{interval}: OKX code={payload.get('code', 'missing')}")
    rows = payload.get("data")
    if not isinstance(rows, list) or len(rows) < MINIMUM_CANDLES:
        count = len(rows) if isinstance(rows, list) else 0
        raise DiagnosticError(f"{interval}: only {count} candles")

    timestamps: list[int] = []
    for index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) < 5:
            raise DiagnosticError(f"{interval}: candle {index} is incomplete")
        try:
            timestamp = int(row[0])
            ohlc = [Decimal(str(value)) for value in row[1:5]]
        except (TypeError, ValueError, InvalidOperation):
            raise DiagnosticError(f"{interval}: candle {index} has non-numeric data") from None
        if timestamp <= 0 or any(not value.is_finite() for value in ohlc):
            raise DiagnosticError(f"{interval}: candle {index} has invalid numeric data")
        open_price, high, low, close = ohlc
        if high < max(open_price, low, close) or low > min(open_price, high, close):
            raise DiagnosticError(f"{interval}: candle {index} has invalid OHLC ordering")
        timestamps.append(timestamp)

    if timestamps != sorted(timestamps, reverse=True) or len(set(timestamps)) != len(timestamps):
        raise DiagnosticError(f"{interval}: timestamps are not unique and descending")

    newest = datetime.fromtimestamp(timestamps[0] / 1000, tz=timezone.utc)
    return {
        "interval": interval,
        "http_status": status,
        "candle_count": len(rows),
        "newest_candle_utc": newest.replace(microsecond=0).isoformat(),
        "timestamps_valid": True,
        "ohlc_numeric": True,
    }


def write_summary(results: list[dict], error: str | None = None) -> None:
    summary_file = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_file:
        return
    lines = [
        "## OKX public candles diagnostic",
        "",
        f"Endpoint: `{OKX_CANDLES_URL}`",
        "",
    ]
    if error:
        lines.extend([f"Result: **FAILED** - `{error}`", ""])
    else:
        lines.extend(
            [
                "| Instrument | Interval | HTTP | Candles | Timestamps | OHLC | Newest candle (UTC) |",
                "|---|---:|---:|---:|---|---|---|",
            ]
        )
        for result in results:
            lines.append(
                f"| {INSTRUMENT} | {result['interval']} | {result['http_status']} | "
                f"{result['candle_count']} | valid | numeric | {result['newest_candle_utc']} |"
            )
        lines.extend(["", "Result: **PASSED**", ""])
    Path(summary_file).write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    results: list[dict] = []
    try:
        for interval in INTERVALS:
            status, payload = fetch_candles(interval)
            results.append(validate_candles(interval, status, payload))
    except DiagnosticError as exc:
        write_summary(results, str(exc))
        print(f"ERROR: {exc}")
        return 2

    write_summary(results)
    print(json.dumps({"status": "OK", "instrument": INSTRUMENT, "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
