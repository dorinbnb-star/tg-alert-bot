"""Offline swing v2 signal generation and portfolio replay."""
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from itertools import product
import math
import random

from .v3_strategy import Candle, HOUR_MS as H, confirmed_pivots, ema, wilder_atr

DAY = 24 * H


@dataclass(frozen=True)
class Variant:
    target_rank: int
    split_exit: bool
    minimum_rr: float

    @property
    def key(self):
        return f"P{self.target_rank}_{'SPLIT' if self.split_exit else 'FULL'}_RR{self.minimum_rr:g}"


def variants():
    return tuple(Variant(*item) for item in product((1, 2), (False, True), (0, 1.8, 2, 2.5, 3)))


def tick_price(price, tick, upward):
    units = price / tick
    return (math.ceil(units - 1e-9) if upward else math.floor(units + 1e-9)) * tick


@dataclass(frozen=True)
class Signal:
    symbol: str
    sign: int
    pivot_ms: int
    level: float
    extreme: float
    atr: float
    time: int
    targets: tuple


@dataclass
class Episode:
    pivot: object
    index: int
    sign: int
    max_depth: float
    extreme: float
    reclaim: Candle | None = None
    random_index: int | None = None


class Prepared:
    def __init__(self, data):
        self.data = data
        self.starts = [c.start_ms for c in data.h1]
        self.atr = wilder_atr(data.h1, 14, H)
        self.daily_times = [c.start_ms + DAY for c in data.daily]
        self.ema50 = ema(data.daily, 50, DAY)
        self.ema200 = ema(data.daily, 200, DAY)
        self.pivots = confirmed_pivots(data.h4, 2)
        self.pivot_times = [pivot.confirmed_ms for pivot in self.pivots]
        self.rates = sorted({int(row["fundingTime"]): float(row["realizedRate"])
                             for row in data.funding if row.get("realizedRate") not in (None, "")}.items())
        self.rate_times = [ts for ts, _ in self.rates]

    def trend(self, time):
        i = bisect_right(self.daily_times, time) - 1
        if i < 0 or time - self.daily_times[i] >= DAY or self.ema200[i] is None:
            return 0
        close, fast, slow = self.data.daily[i].close, self.ema50[i], self.ema200[i]
        return 1 if close > fast > slow else -1 if close < fast < slow else 0

    def daily_context(self, time):
        i = bisect_right(self.daily_times, time) - 1
        if i < 0 or self.ema200[i] is None or time - self.daily_times[i] >= DAY:
            return {}
        return dict(daily_close=self.data.daily[i].close, daily_ema50=self.ema50[i],
                    daily_ema200=self.ema200[i])


def signals(symbol, p, start, end, seed=None):
    rng = random.Random(f"{seed}:{symbol}") if seed is not None else None
    available = {"LOW": [], "HIGH": []}
    cursor, active = 0, None
    used, found, events = set(), [], []
    last_h4 = h4_segment = None
    h4_index = 0
    for i in range(bisect_left(p.starts, start), bisect_left(p.starts, end)):
        bar = p.data.h1[i]
        if bar.end_1h_ms >= end:
            break
        if i == 0 or p.starts[i - 1] != bar.start_ms - H:
            active = None
        while h4_index < len(p.data.h4) and p.data.h4[h4_index].start_ms + 4 * H <= bar.start_ms:
            h4 = p.data.h4[h4_index]
            if last_h4 is None or h4.start_ms != last_h4 + 4 * H:
                h4_segment = h4.start_ms
                available = {"LOW": [], "HIGH": []}
                active = None
            last_h4 = h4.start_ms
            h4_index += 1
        while cursor < len(p.pivots) and p.pivots[cursor].confirmed_ms <= bar.start_ms:
            pivot = p.pivots[cursor]
            if h4_segment is not None and pivot.start_ms >= h4_segment:
                available[pivot.kind].append(pivot)
            cursor += 1
        sign = p.trend(bar.end_1h_ms)
        if (active is not None and (i > active.index + 3 or sign != active.sign)
                or last_h4 is not None and bar.start_ms >= last_h4 + 8 * H):
            active = None
        before = p.atr[i - 1] if i and p.starts[i - 1] == bar.start_ms - H else None
        extreme = bar.low if sign == 1 else bar.high
        if active is None and sign and before and last_h4 is not None and bar.start_ms < last_h4 + 8 * H:
            kind = "LOW" if sign == 1 else "HIGH"
            pivot = next((x for x in reversed(available[kind])
                          if x.start_ms not in used and sign * (x.level - extreme) > 0), None)
            if pivot is not None:
                active = Episode(pivot, i, sign, before, extreme)
                events.append((bar.end_1h_ms, "SWEEP"))
        if active is None:
            continue
        level = active.pivot.level
        if active.reclaim is None:
            if sign * extreme < sign * active.extreme:
                active.extreme = extreme
            if sign * (level - active.extreme) > active.max_depth:
                events.append((bar.end_1h_ms, "SWEEP_TOO_DEEP"))
                active = None
            elif sign * (bar.close - level) > 0:
                active.reclaim = bar
                events.append((bar.end_1h_ms, "RECLAIM"))
                if rng is not None and i < active.index + 3:
                    active.random_index = rng.randint(i + 1, active.index + 3)
            continue
        if sign * (bar.close - level) <= 0 or sign * (extreme - active.extreme) < 0:
            active = None
            continue
        threshold = active.reclaim.high if sign == 1 else active.reclaim.low
        if (i == active.random_index if rng is not None else sign * (bar.close - threshold) > 0):
            used.add(active.pivot.start_ms)
            events.append((bar.end_1h_ms, "CONFIRMED"))
            if p.atr[i] is not None:
                kind = "HIGH" if sign == 1 else "LOW"
                levels = {x.level for x in available[kind] if sign * (x.level - level) > 0}
                for pivot in p.pivots[cursor:bisect_right(p.pivot_times, bar.end_1h_ms)]:
                    if pivot.kind == kind and pivot.start_ms >= h4_segment and sign * (pivot.level - level) > 0:
                        levels.add(pivot.level)
                found.append(Signal(symbol, sign, active.pivot.start_ms, level, active.extreme,
                                    p.atr[i], bar.end_1h_ms, tuple(sorted(levels, reverse=sign < 0)[:2])))
            active = None
    return found, events


@dataclass(frozen=True)
class Order:
    signal: Signal
    entry: float
    stop: float
    target: float


def order_for(signal, tick, variant):
    s = signal.sign
    entry = tick_price(signal.level, tick, s < 0)
    structural = signal.extreme - s * max(.10 * signal.atr, 2 * tick)
    distance = max(s * (entry - structural), 1.5 * signal.atr)
    stop = tick_price(entry - s * distance, tick, s < 0)
    if len(signal.targets) < variant.target_rank:
        return None, "MISSING_STRUCTURAL_TARGET"
    target = tick_price(signal.targets[variant.target_rank - 1], tick, s < 0)
    risk, reward = s * (entry - stop), s * (target - entry)
    if min(entry, stop, target, risk, reward) <= 0 or reward < risk * variant.minimum_rr:
        return None, "RR_REJECTED"
    return Order(signal, entry, stop, target), "ORDER_CREATED"


class Position:
    def __init__(self, order, time, raw, effective, qty, risk, tp1, fee):
        self.order, self.time, self.raw, self.entry = order, time, raw, effective
        self.qty, self.remaining, self.r0, self.tp1 = qty, qty, risk, tp1
        self.stop, self.next_stop = order.stop, None
        self.fees, self.slippage = fee, abs(effective - raw) * qty
        self.funding = self.gross = self.price_pnl = self.mfe = self.mae = 0.0
        self.tp1_net = 0.0
        self.tp1_funding = 0.0
        self.last_rate_ms = time


def replay(prepared, signal_map, start, end, variant, costs, initial_equity=100_000):
    equity = initial_equity
    ranks = {symbol: i for i, symbol in enumerate(prepared)}
    orders, positions, trades, events = {}, {}, [], []
    by_time = {}
    for group in signal_map.values():
        for signal in group:
            by_time.setdefault(signal.time, []).append(signal)
    fee_rate = float(costs["taker_fee_rate"])
    slip_table = costs["slippage_rate"]

    def slip(symbol):
        return float(slip_table.get(symbol, slip_table["default"]))

    def event(symbol, time, status, pivot_ms=None):
        events.append(dict(symbol=symbol, time=time, status=status, pivot_ms=pivot_ms))

    def close(symbol, p, raw, time, status, partial=False):
        nonlocal equity
        s = p.order.signal.sign
        qty = p.qty / 2 if partial else p.remaining
        multiplier = float(costs["stop_slippage_multiplier"]) if status == "SL" else 1.0
        effective = raw * (1 - s * slip(symbol) * multiplier)
        pnl = s * (effective - p.entry) * qty
        fee = effective * qty * fee_rate
        p.fees += fee
        p.slippage += abs(effective - raw) * qty
        p.gross += s * (raw - p.raw) * qty
        p.price_pnl += pnl
        p.remaining -= qty
        equity += pnl - fee
        if partial:
            p.tp1_net = pnl - fee - p.entry * qty * fee_rate + p.tp1_funding
            p.tp1 = None
            p.next_stop = tick_price(p.entry, prepared[symbol].data.tick_size, s > 0)
            return
        del positions[symbol]
        market = prepared[symbol]
        lo = bisect_left(market.rate_times, p.time - 9 * H)
        hi = bisect_right(market.rate_times, time + 9 * H)
        coverage = market.rate_times[lo:hi]
        complete = bool(coverage) and all(b - a <= 9 * H for a, b in zip(coverage, coverage[1:]))
        complete = (complete and market.rate_times[0] <= p.time + 9 * H
                    and market.rate_times[-1] >= time - 9 * H)
        net = p.price_pnl - p.fees + p.funding
        daily = market.daily_context(p.time)
        trades.append(dict(symbol=symbol, direction="LONG" if s == 1 else "SHORT",
                           entry_ms=p.time, exit_ms=time, outcome=status, qty=p.qty, r0=p.r0,
                           entry_raw=p.raw, entry_effective=p.entry, initial_stop=p.order.stop,
                           target=p.order.target, pivot_ms=p.order.signal.pivot_ms,
                           confirmation_ms=p.order.signal.time, gross_pnl=p.gross, net_pnl=net,
                           fees=p.fees, slippage=p.slippage, funding=p.funding,
                           funding_complete=complete, net_r=net / p.r0,
                           net_r_cost_x2=(p.gross - 2*p.fees - 2*p.slippage + 2*p.funding) / p.r0,
                           duration_hours=(time - p.time) / H, mfe_r=p.mfe, mae_r=p.mae,
                           tp1_net_r=p.tp1_net / p.r0, runner_net_r=(net - p.tp1_net) / p.r0,
                           **daily))

    def fund(symbol, p, through, price):
        nonlocal equity
        market = prepared[symbol]
        lo = bisect_right(market.rate_times, p.last_rate_ms)
        hi = bisect_right(market.rate_times, through)
        amount = sum(-p.order.signal.sign * rate * price * p.remaining for _, rate in market.rates[lo:hi])
        p.funding += amount
        if p.tp1 is not None:
            p.tp1_funding += amount / 2
        equity += amount
        p.last_rate_ms = through

    indexes = {symbol: {t: i for i, t in enumerate(p.starts) if start <= t < end}
               for symbol, p in prepared.items()}
    for time in sorted(set().union(*(x.keys() for x in indexes.values()))):
        if time + H > end:
            break
        bars = {s: p.data.h1[indexes[s][time]] for s, p in prepared.items() if time in indexes[s]}
        for signal in by_time.get(time, ()):
            symbol = signal.symbol
            if symbol in positions or symbol in orders:
                event(symbol, time, "SYMBOL_BUSY", signal.pivot_ms)
                continue
            order, reason = order_for(signal, prepared[symbol].data.tick_size, variant)
            event(symbol, time, reason, signal.pivot_ms)
            if order:
                orders[symbol] = order
        for symbol, p in list(positions.items()):
            if symbol not in bars:
                continue
            bar = bars[symbol]
            if p.next_stop is not None:
                p.stop, p.next_stop = p.next_stop, None
            fund(symbol, p, time, bar.open)
            s = p.order.signal.sign
            if s * (bar.open - p.stop) <= 0:
                close(symbol, p, bar.open, time, "SL")
            elif time >= p.time + 168 * H:
                close(symbol, p, bar.open, time, "TIMEOUT")
        for symbol, order in sorted(list(orders.items()), key=lambda item: (item[1].signal.time, ranks[item[0]])):
            if time >= order.signal.time + 12 * H or symbol not in bars:
                event(symbol, time, "ORDER_EXPIRED" if time >= order.signal.time + 12 * H else "DATA_GAP",
                      order.signal.pivot_ms)
                del orders[symbol]
                continue
            bar = bars[symbol]
            s = order.signal.sign
            low, high = (bar.low, bar.high) if s == 1 else (bar.high, bar.low)
            if (s * (bar.open - order.stop) <= 0 or s * (high - order.target) >= 0):
                event(symbol, time, "INVALID_BEFORE_FILL", order.signal.pivot_ms)
                del orders[symbol]
                continue
            if s * (low - order.entry) > 0:
                continue
            del orders[symbol]
            if len(positions) >= 3 or equity <= 0:
                event(symbol, time, "REJECTED_POSITION_LIMIT", order.signal.pivot_ms)
                continue
            raw = min(bar.open, order.entry) if s == 1 else max(bar.open, order.entry)
            effective = raw * (1 + s * slip(symbol))
            tick = prepared[symbol].data.tick_size
            tp1 = tick_price(effective + 2 * (effective - order.stop), tick, s > 0) if variant.split_exit else None
            if tp1 is not None and s * (order.target - tp1) <= 0:
                event(symbol, time, "SPLIT_TARGET_TOO_CLOSE", order.signal.pivot_ms)
                continue
            if tp1 is not None and raw != bar.open and s * (high - tp1) >= 0:
                event(symbol, time, "TARGET_BEFORE_ENTRY", order.signal.pivot_ms)
                continue
            stop_effective = order.stop * (1 - s * slip(symbol) * float(costs["stop_slippage_multiplier"]))
            planned = order.entry * (1 + s * slip(symbol))
            unit = abs(planned - stop_effective) + fee_rate * (planned + stop_effective)
            risk = .02 * equity
            qty = risk / unit
            fee = effective * qty * fee_rate
            equity -= fee
            positions[symbol] = Position(order, time, raw, effective, qty, risk, tp1, fee)
            event(symbol, time, "FILLED", order.signal.pivot_ms)
        for symbol, p in list(positions.items()):
            if symbol not in bars:
                continue
            bar = bars[symbol]
            s = p.order.signal.sign
            adverse, favorable = (bar.low, bar.high) if s == 1 else (bar.high, bar.low)
            stop_hit = s * (adverse - p.stop) <= 0
            target_hit = s * (favorable - p.order.target) >= 0
            if stop_hit:
                adverse = p.stop
                favorable = p.entry
            elif target_hit:
                favorable = p.order.target
                adverse = p.entry
            elif p.time == time and p.raw != bar.open:
                favorable = p.entry
            p.mae = min(p.mae, s * (adverse - p.entry) * p.qty / p.r0)
            p.mfe = max(p.mfe, s * (favorable - p.entry) * p.qty / p.r0)
            # Settlement inside an hourly bar uses the bar open, before close-time exits.
            fund(symbol, p, bar.end_1h_ms, bar.open)
            if stop_hit:
                close(symbol, p, p.stop, bar.end_1h_ms, "SL")
            else:
                if p.tp1 is not None and s * (favorable - p.tp1) >= 0:
                    close(symbol, p, p.tp1, bar.end_1h_ms, "TP1", partial=True)
                if target_hit:
                    close(symbol, p, p.order.target, bar.end_1h_ms, "TP")
    for symbol, p in positions.items():
        event(symbol, end, "CENSORED", p.order.signal.pivot_ms)
    for symbol, order in orders.items():
        event(symbol, end, "ORDER_CANCELLED_AT_BOUNDARY", order.signal.pivot_ms)
    return trades, events
