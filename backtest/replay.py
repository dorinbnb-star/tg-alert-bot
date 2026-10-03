"""Replay client with the OkxClient interface: at scan time T it returns exactly what the live endpoint would.

Live `/market/candles` with `limit=N` returns the N newest rows, the newest being the candle still open at T
(confirm=0), which `parse_okx_candles` drops; `fetch_closed_market` then keeps 239/159/159 closed candles.
Here the stored history is parsed once with the same `parse_okx_candles`, and `klines` returns the N newest
candles that had started by T minus the one still open, i.e. the same list the live parser produces.
"""

from __future__ import annotations

import bisect
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_crypto_monitor import entry_monitor as em  # noqa: E402
from backtest import download  # noqa: E402

MINUTE = 60_000


class LookAheadError(AssertionError):
    pass


def parse_rows(rows: list[list], interval: str) -> list[em.Candle]:
    """Stored rows are ascending; OKX sends newest first, so reverse before the live parser."""
    return em.parse_okx_candles(list(reversed(rows)), interval)


def load_series(root: Path, symbol: str, interval: str) -> list[em.Candle]:
    return parse_rows(download.read_rows(download.series_path(root, symbol, download.bar(interval))), interval)


class ReplayOkxClient:
    def __init__(self, series: dict[str, list[em.Candle]], minute: list[em.Candle] | None = None):
        self.series = series
        self.starts = {interval: [candle.start_ms for candle in candles] for interval, candles in series.items()}
        self.minute = minute or []
        self.minute_starts = [candle.start_ms for candle in self.minute]
        self.now_ms = 0

    def at(self, now_ms: int) -> "ReplayOkxClient":
        self.now_ms = int(now_ms)
        return self

    def server_time_ms(self) -> int:
        return self.now_ms

    def klines(self, symbol: str, interval: str, limit: int) -> list[em.Candle]:
        if interval not in em.OKX_BARS:
            raise em.MonitorError(f"Interval neacceptat: {interval}")
        candles = self.series[interval]
        count = bisect.bisect_right(self.starts[interval], self.now_ms)  # candles that had started by T
        window = candles[max(0, count - int(limit)):count]
        step = em.interval_ms(interval)
        if window and window[-1].start_ms + step > self.now_ms:
            window = window[:-1]  # the candle still open at T: live it arrives with confirm=0 and is dropped
        for candle in window:
            if candle.start_ms + step > self.now_ms:
                raise LookAheadError(f"{symbol} {interval}: lumanare terminata dupa T")
        return window

    def last_price(self, symbol: str) -> float:
        """Price at T = open of the 1m candle containing T (known at its start, so nothing after T is used)."""
        index = bisect.bisect_right(self.minute_starts, self.now_ms) - 1
        if index < 0 or not self.minute[index].start_ms <= self.now_ms < self.minute[index].start_ms + MINUTE:
            raise em.MonitorError("Lipsa lumanare 1m la momentul scanarii")
        return self.minute[index].open
