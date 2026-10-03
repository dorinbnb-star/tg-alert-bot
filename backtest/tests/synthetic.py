"""Deterministic synthetic OKX-like history for the backtest tests (never real market data)."""

from __future__ import annotations

import random
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_crypto_monitor import entry_monitor as em  # noqa: E402

MINUTE = 60_000
END_MS = 1_790_985_600_000  # 2026-10-03T00:00:00Z
DAY = 86_400_000


def minutes(seed: int, days: int, end_ms: int = END_MS) -> list[em.Candle]:
    rnd = random.Random(seed)
    price = 100.0
    out = []
    for start in range(end_ms - days * DAY, end_ms, MINUTE):
        opened = price
        price *= 1 + rnd.gauss(0.00002, 0.0012)
        high = max(opened, price) * (1 + abs(rnd.gauss(0, 0.0004)))
        low = min(opened, price) * (1 - abs(rnd.gauss(0, 0.0004)))
        out.append(em.Candle(start, opened, high, low, price, 1.0))
    return out


def aggregate(candles: list[em.Candle], step_ms: int) -> list[em.Candle]:
    groups: dict[int, list[em.Candle]] = {}
    for candle in candles:
        groups.setdefault(candle.start_ms // step_ms * step_ms, []).append(candle)
    return [
        em.Candle(start, chunk[0].open, max(c.high for c in chunk), min(c.low for c in chunk), chunk[-1].close,
                  float(len(chunk)))
        for start, chunk in sorted(groups.items())
        if len(chunk) == step_ms // MINUTE
    ]


def market(seed: int = 7, days: int = 45) -> tuple[dict[str, list[em.Candle]], list[em.Candle]]:
    minute = minutes(seed, days)
    series = {interval: aggregate(minute, em.interval_ms(interval)) for interval in ("240", "60", "15")}
    return series, minute


def rows(candles: list[em.Candle]) -> list[list[str]]:
    return [[str(c.start_ms), repr(c.open), repr(c.high), repr(c.low), repr(c.close), repr(c.volume), "0", "0", "1"]
            for c in candles]
