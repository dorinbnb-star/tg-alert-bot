"""Pure strategy rules for the v3 horizontal-channel experiment.

The module has no dependency on the live bot or on the backtest engine.  Inputs
are closed candles; outputs are immutable channels, setups and price levels.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from statistics import median
from typing import Iterable, Literal, Sequence

HOUR_MS = 3_600_000
FOUR_HOURS_MS = 4 * HOUR_MS
DAY_MS = 24 * HOUR_MS
Direction = Literal["LONG", "SHORT"]
PivotKind = Literal["HIGH", "LOW"]


@dataclass(frozen=True)
class Candle:
    start_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def end_1h_ms(self) -> int:
        return self.start_ms + HOUR_MS


@dataclass(frozen=True)
class Pivot:
    kind: PivotKind
    index: int
    start_ms: int
    confirmed_ms: int
    level: float


@dataclass(frozen=True)
class ChochEvent:
    direction: Literal["BULLISH", "BEARISH"]
    index: int
    close_ms: int
    broken_level: float


@dataclass(frozen=True)
class Channel:
    symbol: str
    window_start_ms: int
    window_end_ms: int
    upper: float
    lower: float
    width: float
    atr_ref: float
    slope: float
    inside_closes: int
    high_pivots: tuple[Pivot, ...]
    low_pivots: tuple[Pivot, ...]

    @property
    def identity(self) -> tuple[str, int, int, float, float]:
        return (self.symbol, self.window_start_ms, self.window_end_ms, self.upper, self.lower)


@dataclass(frozen=True)
class Setup:
    symbol: str
    direction: Direction
    channel: Channel
    breakout_start_ms: int
    breakout_close_ms: int
    reference_level: float
    reference_tp: float
    retest_deadline_ms: int


@dataclass(frozen=True)
class Parameters:
    pivot_k: int = 2
    channel_window_4h: int = 60
    pivot_group_atr: float = 0.5
    channel_min_atr: float = 4.0
    channel_max_atr: float = 12.0
    channel_max_slope_width: float = 0.3
    channel_min_inside_fraction: float = 0.8
    breakout_atr: float = 0.5
    retest_tolerance_atr: float = 0.25
    retest_expiry_4h: int = 12
    entry_buffer_atr: float = 0.1
    limit_expiry_1h: int = 12
    structural_sl_atr: float = 1.0
    minimum_sl_atr_1h: float = 1.5
    maximum_sl_atr_4h: float = 3.0
    position_timeout_hours: int = 240
    structural_target_lookback_4h: int = 1080
    minimum_tp1_r: float = 1.0
    chandelier_atr: float = 3.0

    @classmethod
    def from_dict(cls, values: dict) -> "Parameters":
        names = cls.__dataclass_fields__
        return cls(**{key: values[key] for key in names if key in values})


def candles_from_rows(rows: Iterable[Sequence]) -> list[Candle]:
    """Convert OKX rows into ascending, confirmed candles."""
    out: dict[int, Candle] = {}
    for row in rows:
        if len(row) >= 9 and str(row[8]) != "1":
            continue
        candle = Candle(
            start_ms=int(row[0]), open=float(row[1]), high=float(row[2]),
            low=float(row[3]), close=float(row[4]), volume=float(row[5]),
        )
        out[candle.start_ms] = candle
    return [out[key] for key in sorted(out)]


def is_contiguous(candles: Sequence[Candle], step_ms: int) -> bool:
    return all(later.start_ms - earlier.start_ms == step_ms for earlier, later in zip(candles, candles[1:]))


def true_range(candle: Candle, previous_close: float) -> float:
    return max(candle.high - candle.low, abs(candle.high - previous_close), abs(candle.low - previous_close))


def wilder_atr(candles: Sequence[Candle], period: int = 14, step_ms: int | None = None) -> list[float | None]:
    """Wilder ATR; a gap resets the warm-up instead of bridging missing data."""
    values: list[float | None] = [None] * len(candles)
    trs: list[float] = []
    previous_atr: float | None = None
    for index, candle in enumerate(candles):
        if index == 0 or (step_ms is not None and candle.start_ms - candles[index - 1].start_ms != step_ms):
            trs = []
            previous_atr = None
            tr = candle.high - candle.low
        else:
            tr = true_range(candle, candles[index - 1].close)
        trs.append(tr)
        if len(trs) < period:
            continue
        if previous_atr is None:
            previous_atr = sum(trs[-period:]) / period
        else:
            previous_atr = ((period - 1) * previous_atr + tr) / period
        values[index] = previous_atr
    return values


def ema(candles: Sequence[Candle], period: int, step_ms: int | None = None) -> list[float | None]:
    """Close EMA seeded by the SMA; a gap starts a new segment."""
    out: list[float | None] = [None] * len(candles)
    segment: list[float] = []
    current: float | None = None
    alpha = 2.0 / (period + 1.0)
    for index, candle in enumerate(candles):
        if index and step_ms is not None and candle.start_ms - candles[index - 1].start_ms != step_ms:
            segment = []
            current = None
        segment.append(candle.close)
        if current is None:
            if len(segment) < period:
                continue
            current = sum(segment[-period:]) / period
        else:
            current = alpha * candle.close + (1.0 - alpha) * current
        out[index] = current
    return out


def confirmed_pivots(candles: Sequence[Candle], k: int = 2, step_ms: int = FOUR_HOURS_MS) -> list[Pivot]:
    pivots: list[Pivot] = []
    for index in range(k, len(candles) - k):
        block = candles[index - k:index + k + 1]
        if not is_contiguous(block, step_ms):
            continue
        center = candles[index]
        others = block[:k] + block[k + 1:]
        confirmed_ms = candles[index + k].start_ms + step_ms
        if all(center.high > item.high for item in others):
            pivots.append(Pivot("HIGH", index, center.start_ms, confirmed_ms, center.high))
        if all(center.low < item.low for item in others):
            pivots.append(Pivot("LOW", index, center.start_ms, confirmed_ms, center.low))
    return pivots


def _group_pivots(pivots: Sequence[Pivot], tolerance: float, kind: PivotKind) -> tuple[Pivot, ...] | None:
    ordered = sorted(pivots, key=lambda item: (item.level, item.start_ms))
    candidates: list[tuple[Pivot, ...]] = []
    for left in range(len(ordered)):
        for right in range(left + 2, len(ordered) + 1):
            group = tuple(ordered[left:right])
            if group[-1].level - group[0].level <= tolerance:
                candidates.append(group)
            else:
                break
    if not candidates:
        return None

    def rank(group: tuple[Pivot, ...]) -> tuple:
        spread = group[-1].level - group[0].level
        recent = max(item.confirmed_ms for item in group)
        center = float(median(item.level for item in group))
        direction_tie = center if kind == "HIGH" else -center
        return (len(group), -spread, recent, direction_tie)

    return max(candidates, key=rank)


def _ols_slope(values: Sequence[float]) -> float:
    size = len(values)
    x_mean = (size - 1) / 2.0
    y_mean = sum(values) / size
    denominator = sum((index - x_mean) ** 2 for index in range(size))
    return sum((index - x_mean) * (value - y_mean) for index, value in enumerate(values)) / denominator


def channel_before(
    symbol: str,
    candles_4h: Sequence[Candle],
    breakout_index: int,
    params: Parameters,
    atr_values: Sequence[float | None] | None = None,
    pivots: Sequence[Pivot] | None = None,
) -> Channel | None:
    """Build a channel using only the N candles before ``breakout_index``."""
    n = params.channel_window_4h
    if breakout_index < n or breakout_index >= len(candles_4h):
        return None
    window = candles_4h[breakout_index - n:breakout_index]
    if len(window) != n or not is_contiguous(window, FOUR_HOURS_MS):
        return None
    atr_values = list(atr_values) if atr_values is not None else wilder_atr(candles_4h, 14, FOUR_HOURS_MS)
    atr_ref = atr_values[breakout_index - 1]
    if atr_ref is None or atr_ref <= 0:
        return None
    available_ms = candles_4h[breakout_index].start_ms
    all_pivots = list(pivots) if pivots is not None else confirmed_pivots(candles_4h[:breakout_index], params.pivot_k)
    eligible = [
        pivot for pivot in all_pivots
        if window[0].start_ms <= pivot.start_ms <= window[-1].start_ms and pivot.confirmed_ms <= available_ms
    ]
    tolerance = params.pivot_group_atr * atr_ref
    highs = _group_pivots([p for p in eligible if p.kind == "HIGH"], tolerance, "HIGH")
    lows = _group_pivots([p for p in eligible if p.kind == "LOW"], tolerance, "LOW")
    if highs is None or lows is None:
        return None
    upper = float(median(item.level for item in highs))
    lower = float(median(item.level for item in lows))
    width = upper - lower
    if width <= 0 or not (params.channel_min_atr * atr_ref <= width <= params.channel_max_atr * atr_ref):
        return None
    slope = _ols_slope([item.close for item in window])
    if abs(slope * n) >= params.channel_max_slope_width * width:
        return None
    inside = sum(lower <= item.close <= upper for item in window)
    if inside < ceil(params.channel_min_inside_fraction * n):
        return None
    return Channel(
        symbol=symbol,
        window_start_ms=window[0].start_ms,
        window_end_ms=window[-1].start_ms + FOUR_HOURS_MS,
        upper=upper,
        lower=lower,
        width=width,
        atr_ref=atr_ref,
        slope=slope,
        inside_closes=inside,
        high_pivots=highs,
        low_pivots=lows,
    )


def breakout_setup(channel: Channel, candle: Candle, params: Parameters) -> Setup | None:
    threshold = params.breakout_atr * channel.atr_ref
    if candle.close >= channel.upper + threshold:
        direction: Direction = "LONG"
        level = channel.upper
        target = channel.upper + channel.width
    elif candle.close <= channel.lower - threshold:
        direction = "SHORT"
        level = channel.lower
        target = channel.lower - channel.width
    else:
        return None
    close_ms = candle.start_ms + FOUR_HOURS_MS
    return Setup(
        symbol=channel.symbol,
        direction=direction,
        channel=channel,
        breakout_start_ms=candle.start_ms,
        breakout_close_ms=close_ms,
        reference_level=level,
        reference_tp=target,
        retest_deadline_ms=close_ms + params.retest_expiry_4h * FOUR_HOURS_MS,
    )


def retest_zone(setup: Setup, params: Parameters) -> tuple[float, float]:
    offset = params.retest_tolerance_atr * setup.channel.atr_ref
    return setup.reference_level - offset, setup.reference_level + offset


@dataclass(frozen=True)
class BreakoutState:
    phase: str = "IDLE"
    channel: Channel | None = None
    direction: Direction | None = None
    armed_ms: int | None = None
    exit_ms: int | None = None
    last_ms: int | None = None
    bars: int = 0


def advance_breakout(state: BreakoutState, candidate: Channel | None,
                     previous_close: float, candle: Candle, params: Parameters
                     ) -> tuple[BreakoutState, Setup | None, list[dict]]:
    """Pure closed-4H state transition; no portfolio or future data access.

    Three bars including the exit is the approved starting hypothesis.
    Opposite-side exits restart the count, preserving frozen geometry.
    """
    from dataclasses import replace
    now = candle.start_ms + FOUR_HOURS_MS
    events: list[dict] = []
    if state.last_ms is not None and now - state.last_ms != FOUR_HOURS_MS:
        state = BreakoutState()

    def record(status: str, current: BreakoutState) -> None:
        ch = current.channel
        events.append(dict(status=status, status_ms=now, armed_ms=current.armed_ms,
                           exit_ms=current.exit_ms, direction=current.direction,
                           channel_id=repr(ch.identity) if ch else None,
                           upper=ch.upper if ch else None, lower=ch.lower if ch else None,
                           atr_ref=ch.atr_ref if ch else None))

    if state.phase == "BLOCKED":
        if candidate is not None and candidate.lower <= candle.close <= candidate.upper:
            state = BreakoutState("IDLE", last_ms=now)
            record("EXIT_REARM_READY", replace(state, channel=candidate))
        return replace(state, last_ms=now), None, events
    if state.phase != "EXITED":
        if candidate is None or not candidate.lower <= previous_close <= candidate.upper:
            return BreakoutState(last_ms=now), None, events
        newly_armed = state.phase != "ARMED"
        state = BreakoutState("ARMED", candidate, armed_ms=now if newly_armed else state.armed_ms,
                              last_ms=now)
        if newly_armed:
            record("ARMED", state)
        if candidate.lower <= candle.close <= candidate.upper:
            return state, None, events
        direction = "LONG" if candle.close > candidate.upper else "SHORT"
        state = replace(state, phase="EXITED", direction=direction, exit_ms=now, bars=1)
        record("EXITED", state)
    else:
        channel = state.channel
        if channel.lower <= candle.close <= channel.upper:
            record("EXIT_RETURNED", state)
            return BreakoutState("ARMED", armed_ms=state.armed_ms, last_ms=now), None, events
        direction = "LONG" if candle.close > channel.upper else "SHORT"
        if direction != state.direction:
            state = replace(state, direction=direction, exit_ms=now, bars=1)
            record("EXIT_SIDE_CHANGED", state)
            record("EXITED", state)
        else:
            state = replace(state, bars=state.bars + 1)
        state = replace(state, last_ms=now)
    setup = breakout_setup(state.channel, candle, params)
    if setup is not None and setup.direction == state.direction:
        record("BREAKOUT", state)
        return BreakoutState(last_ms=now), setup, events
    if state.bars >= 3:
        record("EXIT_NO_BREAKOUT", state)
        return BreakoutState("BLOCKED", last_ms=now), None, events
    return state, None, events


def touches_retest(setup: Setup, candle: Candle, params: Parameters) -> bool:
    if candle.start_ms < setup.breakout_close_ms:
        return False
    low, high = retest_zone(setup, params)
    return candle.low <= high and candle.high >= low


def confirms(setup: Setup, previous: Candle, candle: Candle) -> bool:
    if candle.start_ms - previous.start_ms != HOUR_MS:
        return False
    if setup.direction == "LONG":
        return candle.close > setup.channel.upper and candle.close > previous.high
    return candle.close < setup.channel.lower and candle.close < previous.low


def invalidated_at_close(setup: Setup, close: float, params: Parameters) -> bool:
    offset = params.breakout_atr * setup.channel.atr_ref
    if setup.direction == "LONG":
        return close <= setup.channel.upper - offset
    return close >= setup.channel.lower + offset


def target_touched(setup: Setup, candle: Candle, target: float | None = None) -> bool:
    level = setup.reference_tp if target is None else target
    return candle.high >= level if setup.direction == "LONG" else candle.low <= level


def planned_entry(setup: Setup, params: Parameters) -> float:
    offset = params.entry_buffer_atr * setup.channel.atr_ref
    return setup.reference_level + offset if setup.direction == "LONG" else setup.reference_level - offset


def initial_stop(
    setup: Setup,
    entry_basis: float,
    atr_1h_confirmation: float,
    params: Parameters,
) -> tuple[float, float, bool]:
    if setup.direction == "LONG":
        structural = setup.reference_level - params.structural_sl_atr * setup.channel.atr_ref
        minimum = entry_basis - params.minimum_sl_atr_1h * atr_1h_confirmation
        stop = min(structural, minimum)
        distance = entry_basis - stop
    else:
        structural = setup.reference_level + params.structural_sl_atr * setup.channel.atr_ref
        minimum = entry_basis + params.minimum_sl_atr_1h * atr_1h_confirmation
        stop = max(structural, minimum)
        distance = stop - entry_basis
    return stop, distance, distance > params.maximum_sl_atr_4h * setup.channel.atr_ref


def gross_rr(entry_basis: float, stop: float, target: float) -> float:
    risk = abs(entry_basis - stop)
    return abs(target - entry_basis) / risk if risk > 0 else 0.0


def structural_targets(
    setup: Setup,
    entry_plan: float,
    pivots: Sequence[Pivot],
    confirmation_ms: int,
    lookback_4h: int,
    tick_size: float,
) -> tuple[float, float] | None:
    earliest = confirmation_ms - lookback_4h * FOUR_HOURS_MS
    kind: PivotKind = "HIGH" if setup.direction == "LONG" else "LOW"
    candidates = [
        pivot for pivot in pivots
        if pivot.kind == kind and earliest <= pivot.start_ms < confirmation_ms
        and pivot.confirmed_ms <= confirmation_ms
        and ((pivot.level > entry_plan) if setup.direction == "LONG" else (pivot.level < entry_plan))
    ]
    candidates.sort(key=lambda item: (item.level, -item.confirmed_ms), reverse=setup.direction == "SHORT")
    distinct: list[Pivot] = []
    for pivot in candidates:
        if distinct and abs(pivot.level - distinct[-1].level) <= tick_size:
            if pivot.confirmed_ms > distinct[-1].confirmed_ms:
                distinct[-1] = pivot
            continue
        distinct.append(pivot)
    if len(distinct) < 2:
        return None
    return distinct[0].level, distinct[1].level


def structure_state(pivots: Sequence[Pivot], available_ms: int) -> Literal["BULLISH", "BEARISH", "UNCLEAR"]:
    highs = [pivot for pivot in pivots if pivot.kind == "HIGH" and pivot.confirmed_ms <= available_ms]
    lows = [pivot for pivot in pivots if pivot.kind == "LOW" and pivot.confirmed_ms <= available_ms]
    highs.sort(key=lambda item: item.start_ms)
    lows.sort(key=lambda item: item.start_ms)
    if len(highs) < 2 or len(lows) < 2:
        return "UNCLEAR"
    high_up = highs[-1].level > highs[-2].level
    low_up = lows[-1].level > lows[-2].level
    high_down = highs[-1].level < highs[-2].level
    low_down = lows[-1].level < lows[-2].level
    if high_up and low_up:
        return "BULLISH"
    if high_down and low_down:
        return "BEARISH"
    return "UNCLEAR"


def choch_events(candles: Sequence[Candle], pivots: Sequence[Pivot]) -> list[ChochEvent]:
    events: list[ChochEvent] = []
    for index, candle in enumerate(candles):
        close_ms = candle.start_ms + FOUR_HOURS_MS
        available = [pivot for pivot in pivots if pivot.confirmed_ms <= close_ms]
        state = structure_state(available, close_ms)
        highs = sorted((pivot for pivot in available if pivot.kind == "HIGH"), key=lambda item: item.start_ms)
        lows = sorted((pivot for pivot in available if pivot.kind == "LOW"), key=lambda item: item.start_ms)
        if state == "BULLISH" and lows and candle.close < lows[-1].level:
            event = ChochEvent("BEARISH", index, close_ms, lows[-1].level)
            if not events or (events[-1].direction, events[-1].broken_level) != (event.direction, event.broken_level):
                events.append(event)
        elif state == "BEARISH" and highs and candle.close > highs[-1].level:
            event = ChochEvent("BULLISH", index, close_ms, highs[-1].level)
            if not events or (events[-1].direction, events[-1].broken_level) != (event.direction, event.broken_level):
                events.append(event)
    return events


def new_structure_confirmed(
    event: ChochEvent,
    breakout_index: int,
    candles: Sequence[Candle],
    pivots: Sequence[Pivot],
) -> bool:
    after = [pivot for pivot in pivots if pivot.start_ms >= event.close_ms and pivot.confirmed_ms <= candles[breakout_index].start_ms]
    if event.direction == "BULLISH":
        anchors = sorted((pivot for pivot in after if pivot.kind == "LOW"), key=lambda item: item.confirmed_ms)
        if not anchors:
            return False
        levels = sorted(
            (pivot for pivot in after if pivot.kind == "HIGH" and pivot.confirmed_ms > anchors[0].confirmed_ms),
            key=lambda item: item.confirmed_ms,
        )
        if not levels:
            return False
        return any(
            candle.start_ms + FOUR_HOURS_MS >= levels[0].confirmed_ms and candle.close > levels[0].level
            for candle in candles[:breakout_index + 1]
        )
    anchors = sorted((pivot for pivot in after if pivot.kind == "HIGH"), key=lambda item: item.confirmed_ms)
    if not anchors:
        return False
    levels = sorted(
        (pivot for pivot in after if pivot.kind == "LOW" and pivot.confirmed_ms > anchors[0].confirmed_ms),
        key=lambda item: item.confirmed_ms,
    )
    if not levels:
        return False
    return any(
        candle.start_ms + FOUR_HOURS_MS >= levels[0].confirmed_ms and candle.close < levels[0].level
        for candle in candles[:breakout_index + 1]
    )


def daily_context(daily: Sequence[Candle], at_ms: int) -> dict[str, float | str]:
    missing = {
        "daily_close": "NA", "daily_ema50": "NA", "daily_ema200": "NA",
        "close_vs_ema50": "NA", "close_vs_ema200": "NA", "ema50_vs_ema200": "NA",
        "close_ema50_pct": "NA", "close_ema200_pct": "NA",
    }
    closed = [candle for candle in daily if candle.start_ms + DAY_MS <= at_ms]
    if not closed:
        return missing
    ema50 = ema(closed, 50, DAY_MS)[-1]
    ema200 = ema(closed, 200, DAY_MS)[-1]
    if ema50 is None or ema200 is None:
        return missing
    close = closed[-1].close
    relation = lambda left, right: "ABOVE" if left > right else "BELOW" if left < right else "EQUAL"
    return {
        "daily_close": close,
        "daily_ema50": ema50,
        "daily_ema200": ema200,
        "close_vs_ema50": relation(close, ema50),
        "close_vs_ema200": relation(close, ema200),
        "ema50_vs_ema200": relation(ema50, ema200),
        "close_ema50_pct": (close / ema50 - 1.0) * 100.0,
        "close_ema200_pct": (close / ema200 - 1.0) * 100.0,
    }
