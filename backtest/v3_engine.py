"""Event-driven portfolio simulator for the v3 channel strategy."""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Sequence

from backtest import v3_strategy as strategy

EntryMode = Literal["ENTRY_LIMIT", "ENTRY_MARKET_NEXT_OPEN"]
ExitFamily = Literal["REFERENCE", "SPLIT"]
BreakevenMode = Literal["BE_AFTER_TP1", "BE_AFTER_TP2", "NO_BE"]
FilterMode = Literal["BASE", "VOLUME", "CHOCH_PAUSE_12", "CHOCH_NEW_STRUCTURE"]


@dataclass(frozen=True)
class Variant:
    entry_mode: EntryMode
    exit_family: ExitFamily
    minimum_rr: float
    breakeven: BreakevenMode = "NO_BE"
    filter_mode: FilterMode = "BASE"

    @property
    def key(self) -> str:
        rr = int(self.minimum_rr)
        suffix = "" if self.filter_mode == "BASE" else f"__{self.filter_mode}"
        if self.exit_family == "REFERENCE":
            return f"{self.entry_mode}__REF_RR{rr}{suffix}"
        return f"{self.entry_mode}__SPLIT_RR{rr}__{self.breakeven}{suffix}"


def variants() -> tuple[Variant, ...]:
    out: list[Variant] = []
    for entry in ("ENTRY_LIMIT", "ENTRY_MARKET_NEXT_OPEN"):
        for rr in (2.0, 3.0):
            out.append(Variant(entry, "REFERENCE", rr))
            for be in ("BE_AFTER_TP1", "BE_AFTER_TP2", "NO_BE"):
                out.append(Variant(entry, "SPLIT", rr, be))
    return tuple(out)


@dataclass
class MarketData:
    h1: list[strategy.Candle]
    h4: list[strategy.Candle]
    daily: list[strategy.Candle] = field(default_factory=list)
    funding: list[dict] = field(default_factory=list)
    tick_size: float = 1e-8


@dataclass
class Prepared:
    data: MarketData
    atr1: list[float | None]
    atr4: list[float | None]
    pivots: list[strategy.Pivot]
    h1_by_start: dict[int, int]
    h4_by_close: dict[int, int]
    funding: list[tuple[int, float]]
    channels: dict[int, strategy.Channel]
    choch: list[strategy.ChochEvent]


@dataclass
class ActiveSetup:
    setup: strategy.Setup
    retested: bool = False
    touched_target: bool = False
    confirmation_ms: int | None = None
    confirmation_index: int | None = None
    atr1_confirmation: float | None = None
    entry_plan: float | None = None
    stop: float | None = None
    targets: tuple[float, float] | None = None
    random_confirmation_ms: int | None = None


@dataclass
class PendingEntry:
    active_ms: int
    confirmation_ms: int
    entry_plan: float
    stop: float
    atr1_confirmation: float
    targets: tuple[float, float] | None
    bars_seen: int = 0


@dataclass
class Position:
    symbol: str
    setup: strategy.Setup
    variant: Variant
    confirmation_ms: int
    entry_ms: int
    entry_raw: float
    entry_effective: float
    stop: float
    original_stop: float
    target: float
    tp1: float | None
    tp2: float | None
    qty: float
    remaining: float
    risk_unit: float
    r0: float
    timeout_ms: int
    daily_context: dict
    fee_total: float = 0.0
    slippage_total: float = 0.0
    funding_total: float = 0.0
    gross_raw_total: float = 0.0
    price_pnl_total: float = 0.0
    tp1_done: bool = False
    tp2_done: bool = False
    pending_stop: float | None = None
    highest: float = float("-inf")
    lowest: float = float("inf")
    exit_ms: int | None = None
    outcome: str = "OPEN"
    ambiguous_sl: bool = False
    gap_stop: bool = False
    funding_cursor: int = 0
    funding_complete: bool = True
    mfe_r: float = 0.0
    mae_r: float = 0.0
    tranche_pnl: dict[str, float] = field(default_factory=lambda: {"TP1": 0.0, "TP2": 0.0, "RUNNER": 0.0})


@dataclass
class RunResult:
    variant: str
    setups: list[dict]
    trades: list[dict]
    ending_equity: float
    transitions: list[dict] = field(default_factory=list)


def load_config(path: Path | None = None) -> dict:
    path = path or Path(__file__).with_name("v3_config.json")
    return json.loads(path.read_text(encoding="utf-8"))


def prepare(data: MarketData, params: strategy.Parameters) -> Prepared:
    funding: list[tuple[int, float]] = []
    for item in data.funding:
        raw = item.get("realizedRate") or item.get("fundingRate")
        if raw not in (None, ""):
            funding.append((int(item["fundingTime"]), float(raw)))
    funding.sort()
    atr4 = strategy.wilder_atr(data.h4, 14, strategy.FOUR_HOURS_MS)
    pivots = strategy.confirmed_pivots(data.h4, params.pivot_k)
    channels = {}
    for index in range(params.channel_window_4h, len(data.h4)):
        channel = strategy.channel_before("", data.h4, index, params, atr4, pivots)
        if channel is not None:
            channels[index] = channel
    return Prepared(
        data=data,
        atr1=strategy.wilder_atr(data.h1, 14, strategy.HOUR_MS),
        atr4=atr4,
        pivots=pivots,
        h1_by_start={candle.start_ms: index for index, candle in enumerate(data.h1)},
        h4_by_close={candle.start_ms + strategy.FOUR_HOURS_MS: index for index, candle in enumerate(data.h4)},
        funding=funding,
        channels=channels,
        choch=strategy.choch_events(data.h4, pivots),
    )


class PortfolioEngine:
    def __init__(self, market: dict[str, MarketData], config: dict, costs: dict, variant: Variant,
                 prepared: dict[str, Prepared] | None = None, random_seed: int | None = None):
        self.config = config
        self.params = strategy.Parameters.from_dict(config)
        self.costs = costs
        self.variant = variant
        self.symbols = [symbol for symbol in config["symbols"] if symbol in market]
        self.rank = {symbol: index for index, symbol in enumerate(config["symbols"])}
        self.prepared = prepared or {symbol: prepare(market[symbol], self.params) for symbol in self.symbols}
        for symbol in self.symbols:
            for index, channel in list(self.prepared[symbol].channels.items()):
                if not channel.symbol:
                    self.prepared[symbol].channels[index] = strategy.Channel(
                        symbol=symbol, window_start_ms=channel.window_start_ms, window_end_ms=channel.window_end_ms,
                        upper=channel.upper, lower=channel.lower, width=channel.width, atr_ref=channel.atr_ref,
                        slope=channel.slope, inside_closes=channel.inside_closes,
                        high_pivots=channel.high_pivots, low_pivots=channel.low_pivots,
                    )
        self.active: dict[str, ActiveSetup] = {}
        self.pending: dict[str, PendingEntry] = {}
        self.positions: dict[str, Position] = {}
        self.last_breakout: dict[str, int] = {}
        self.used_channels: set[tuple] = set()
        self.setups: list[dict] = []
        self.trades: list[dict] = []
        self.equity = float(config.get("starting_equity", 100_000.0))
        self.random_seed = random_seed
        self.breakout_states: dict[str, strategy.BreakoutState] = {}
        self.transitions: list[dict] = []

    def _slippage(self, symbol: str) -> float:
        table = self.costs["slippage_rate"]
        return float(table.get(symbol, table["default"]))

    def _effective(self, direction: str, raw: float, multiplier: float = 1.0) -> float:
        rate = self._current_slippage * multiplier
        return raw * (1.0 + rate if direction == "LONG" else 1.0 - rate)

    def _exit_effective(self, direction: str, raw: float, multiplier: float = 1.0) -> float:
        rate = self._current_slippage * multiplier
        return raw * (1.0 - rate if direction == "LONG" else 1.0 + rate)

    def _record_setup(self, symbol: str, active: ActiveSetup, status: str, at_ms: int, **extra) -> None:
        row = {
            "variant": self.variant.key,
            "symbol": symbol,
            "direction": active.setup.direction,
            "channel_id": repr(active.setup.channel.identity),
            "breakout_ms": active.setup.breakout_close_ms,
            "status": status,
            "status_ms": at_ms,
            "upper": active.setup.channel.upper,
            "lower": active.setup.channel.lower,
            "width": active.setup.channel.width,
            "atr_ref": active.setup.channel.atr_ref,
        }
        row.update(extra)
        self.setups.append(row)

    def _cancel(self, symbol: str, reason: str, at_ms: int) -> None:
        active = self.active.pop(symbol, None)
        self.pending.pop(symbol, None)
        if active is not None:
            self._record_setup(symbol, active, reason, at_ms)

    def _estimated_risk_cost(self, symbol: str, entry_basis: float, stop: float, market_entry: bool) -> float:
        fee = float(self.costs["taker_fee_rate"])
        slip = self._slippage(symbol)
        entry_slip = 0.0 if market_entry else entry_basis * slip
        stop_slip = stop * slip * float(self.costs["stop_slippage_multiplier"])
        return entry_basis * fee + stop * fee + entry_slip + stop_slip

    def _fill_position(
        self,
        symbol: str,
        active: ActiveSetup,
        pending: PendingEntry,
        entry_ms: int,
        raw: float,
        effective: float,
        stop: float,
    ) -> None:
        self._current_slippage = self._slippage(symbol)
        estimated_cost = self._estimated_risk_cost(
            symbol, effective if self.variant.entry_mode == "ENTRY_MARKET_NEXT_OPEN" else pending.entry_plan,
            stop, self.variant.entry_mode == "ENTRY_MARKET_NEXT_OPEN",
        )
        entry_basis = effective if self.variant.entry_mode == "ENTRY_MARKET_NEXT_OPEN" else pending.entry_plan
        risk_unit = abs(entry_basis - stop) + estimated_cost
        budget = float(self.config["risk_fraction"]) * self.equity
        qty = budget / risk_unit
        target = active.setup.reference_tp
        tp1, tp2 = pending.targets or (None, None)
        fee = effective * qty * float(self.costs["taker_fee_rate"])
        slippage = abs(effective - raw) * qty
        position = Position(
            symbol=symbol, setup=active.setup, variant=self.variant,
            confirmation_ms=pending.confirmation_ms, entry_ms=entry_ms,
            entry_raw=raw, entry_effective=effective, stop=stop, original_stop=stop,
            target=target, tp1=tp1, tp2=tp2, qty=qty, remaining=qty,
            risk_unit=risk_unit, r0=qty * risk_unit,
            timeout_ms=entry_ms + self.params.position_timeout_hours * strategy.HOUR_MS,
            daily_context=strategy.daily_context(self.prepared[symbol].data.daily, entry_ms),
            fee_total=fee, slippage_total=slippage,
        )
        rates = self.prepared[symbol].funding
        position.funding_cursor = next((i for i, (ts, _) in enumerate(rates) if ts > entry_ms), len(rates))
        if rates and entry_ms < rates[0][0]:
            position.funding_complete = False
        self.equity -= fee
        self.positions[symbol] = position
        self.pending.pop(symbol, None)
        self.active.pop(symbol, None)
        self._record_setup(symbol, active, "FILLED", entry_ms, entry_raw=raw, entry_effective=effective, stop=stop)

    def _apply_funding(self, symbol: str, position: Position, through_ms: int, price: float) -> None:
        rates = self.prepared[symbol].funding
        sign = 1.0 if position.setup.direction == "LONG" else -1.0
        while position.funding_cursor < len(rates) and rates[position.funding_cursor][0] <= through_ms:
            ts, rate = rates[position.funding_cursor]
            if ts > position.entry_ms:
                pnl = -sign * rate * price * position.remaining
                position.funding_total += pnl
                open_quantities = {
                    "TP1": 0.0 if position.tp1_done else min(0.40 * position.qty, position.remaining),
                    "TP2": 0.0 if position.tp2_done else min(0.30 * position.qty, position.remaining),
                }
                open_quantities["RUNNER"] = max(
                    0.0, position.remaining - open_quantities["TP1"] - open_quantities["TP2"],
                )
                if position.variant.exit_family == "REFERENCE":
                    open_quantities = {"TP1": 0.0, "TP2": 0.0, "RUNNER": position.remaining}
                for label, quantity in open_quantities.items():
                    position.tranche_pnl[label] += pnl * quantity / position.remaining
                self.equity += pnl
            position.funding_cursor += 1

    def _close_fraction(self, position: Position, fraction_qty: float, raw: float, at_ms: int,
                        label: str, stop: bool = False) -> None:
        if fraction_qty <= 0:
            return
        fraction_qty = min(fraction_qty, position.remaining)
        self._current_slippage = self._slippage(position.symbol)
        multiplier = float(self.costs["stop_slippage_multiplier"]) if stop else 1.0
        effective = self._exit_effective(position.setup.direction, raw, multiplier)
        sign = 1.0 if position.setup.direction == "LONG" else -1.0
        price_pnl = sign * (effective - position.entry_effective) * fraction_qty
        raw_pnl = sign * (raw - position.entry_raw) * fraction_qty
        fee = effective * fraction_qty * float(self.costs["taker_fee_rate"])
        entry_fee = position.entry_effective * fraction_qty * float(self.costs["taker_fee_rate"])
        slip = abs(effective - raw) * fraction_qty
        net = price_pnl - fee
        position.price_pnl_total += price_pnl
        position.gross_raw_total += raw_pnl
        position.fee_total += fee
        position.slippage_total += slip
        position.tranche_pnl[label] += net - entry_fee
        position.remaining -= fraction_qty
        self.equity += net
        position.exit_ms = at_ms

    def _finish(self, symbol: str, outcome: str, at_ms: int) -> None:
        position = self.positions.pop(symbol)
        position.outcome = outcome
        position.exit_ms = at_ms
        rates = self.prepared[symbol].funding
        if rates:
            coverage = [timestamp for timestamp, _ in rates
                        if position.entry_ms - 9 * strategy.HOUR_MS <= timestamp <= at_ms + 9 * strategy.HOUR_MS]
            if (not coverage or rates[0][0] > position.entry_ms + 9 * strategy.HOUR_MS
                    or rates[-1][0] < at_ms - 9 * strategy.HOUR_MS):
                position.funding_complete = False
            if any(right - left > 9 * strategy.HOUR_MS for left, right in zip(coverage, coverage[1:])):
                position.funding_complete = False
        else:
            position.funding_complete = False
        net = position.price_pnl_total - position.fee_total + position.funding_total
        net_x2 = position.gross_raw_total - 2.0 * position.fee_total - 2.0 * position.slippage_total + 2.0 * position.funding_total
        row = {
            "variant": self.variant.key,
            "entry_mode": self.variant.entry_mode,
            "exit_family": self.variant.exit_family,
            "minimum_rr": self.variant.minimum_rr,
            "breakeven": self.variant.breakeven,
            "symbol": symbol,
            "direction": position.setup.direction,
            "channel_id": repr(position.setup.channel.identity),
            "confirmation_ms": position.confirmation_ms,
            "entry_ms": position.entry_ms,
            "exit_ms": at_ms,
            "entry_raw": position.entry_raw,
            "entry_effective": position.entry_effective,
            "initial_stop": position.original_stop,
            "target": position.target,
            "tp1": position.tp1,
            "tp2": position.tp2,
            "outcome": outcome,
            "qty": position.qty,
            "r0": position.r0,
            "gross_pnl": position.gross_raw_total,
            "net_pnl": net,
            "net_r": net / position.r0,
            "net_r_cost_x2": net_x2 / position.r0,
            "fees": position.fee_total,
            "slippage": position.slippage_total,
            "funding": position.funding_total,
            "funding_complete": position.funding_complete,
            "duration_hours": (at_ms - position.entry_ms) / strategy.HOUR_MS,
            "mfe_r": position.mfe_r,
            "mae_r": position.mae_r,
            "ambiguous_sl": position.ambiguous_sl,
            "gap_stop": position.gap_stop,
            "tp1_pnl": position.tranche_pnl["TP1"],
            "tp2_pnl": position.tranche_pnl["TP2"],
            "runner_pnl": position.tranche_pnl["RUNNER"],
            "tp1_net_r": position.tranche_pnl["TP1"] / position.r0,
            "tp2_net_r": position.tranche_pnl["TP2"] / position.r0,
            "runner_net_r": position.tranche_pnl["RUNNER"] / position.r0,
            **position.daily_context,
        }
        self.trades.append(row)

    def _process_position(self, symbol: str, candle: strategy.Candle) -> None:
        position = self.positions.get(symbol)
        if position is None:
            return
        if position.pending_stop is not None:
            if position.setup.direction == "LONG":
                position.stop = max(position.stop, position.pending_stop)
            else:
                position.stop = min(position.stop, position.pending_stop)
            position.pending_stop = None
        self._apply_funding(symbol, position, candle.start_ms, candle.open)
        if candle.start_ms >= position.timeout_ms:
            self._close_fraction(position, position.remaining, candle.open, candle.start_ms, "RUNNER")
            self._finish(symbol, "TIMEOUT", candle.start_ms)
            return
        position.highest = max(position.highest, candle.high)
        position.lowest = min(position.lowest, candle.low)
        direction = position.setup.direction
        if direction == "LONG":
            favorable = (candle.high - position.entry_effective) * position.qty / position.r0
            adverse = (candle.low - position.entry_effective) * position.qty / position.r0
        else:
            favorable = (position.entry_effective - candle.low) * position.qty / position.r0
            adverse = (position.entry_effective - candle.high) * position.qty / position.r0
        position.mfe_r = max(position.mfe_r, favorable)
        position.mae_r = min(position.mae_r, adverse)
        hit_sl = candle.low <= position.stop if direction == "LONG" else candle.high >= position.stop
        if self.variant.exit_family == "REFERENCE":
            hit_tp = candle.high >= position.target if direction == "LONG" else candle.low <= position.target
            if hit_sl:
                position.ambiguous_sl = hit_tp
                raw = min(position.stop, candle.open) if direction == "LONG" else max(position.stop, candle.open)
                position.gap_stop = raw != position.stop
                self._close_fraction(position, position.remaining, raw, candle.start_ms + strategy.HOUR_MS, "RUNNER", True)
                self._finish(symbol, "SL", candle.start_ms + strategy.HOUR_MS)
            elif hit_tp:
                self._close_fraction(position, position.remaining, position.target,
                                     candle.start_ms + strategy.HOUR_MS, "RUNNER")
                self._finish(symbol, "TP", candle.start_ms + strategy.HOUR_MS)
            return

        assert position.tp1 is not None and position.tp2 is not None
        hit_tp1 = not position.tp1_done and (candle.high >= position.tp1 if direction == "LONG" else candle.low <= position.tp1)
        hit_tp2 = not position.tp2_done and (candle.high >= position.tp2 if direction == "LONG" else candle.low <= position.tp2)
        if hit_sl:
            position.ambiguous_sl = hit_tp1 or hit_tp2
            raw = min(position.stop, candle.open) if direction == "LONG" else max(position.stop, candle.open)
            position.gap_stop = raw != position.stop
            self._close_fraction(position, position.remaining, raw, candle.start_ms + strategy.HOUR_MS, "RUNNER", True)
            self._finish(symbol, "SL", candle.start_ms + strategy.HOUR_MS)
            return
        if hit_tp1:
            self._close_fraction(position, 0.40 * position.qty, position.tp1,
                                 candle.start_ms + strategy.HOUR_MS, "TP1")
            position.tp1_done = True
            if self.variant.breakeven == "BE_AFTER_TP1":
                position.pending_stop = self._breakeven(position)
        if hit_tp2:
            self._close_fraction(position, 0.30 * position.qty, position.tp2,
                                 candle.start_ms + strategy.HOUR_MS, "TP2")
            position.tp2_done = True
            if self.variant.breakeven == "BE_AFTER_TP2":
                position.pending_stop = self._breakeven(position)

    def _breakeven(self, position: Position) -> float:
        fee = float(self.costs["taker_fee_rate"])
        slip = self._slippage(position.symbol) * float(self.costs["stop_slippage_multiplier"])
        tick = self.prepared[position.symbol].data.tick_size
        if position.setup.direction == "LONG":
            raw = position.entry_effective * (1.0 + fee) / max(1.0 - fee - slip, 1e-12)
            return math.ceil(raw / tick - 1e-12) * tick
        raw = position.entry_effective * (1.0 - fee) / (1.0 + fee + slip)
        return math.floor(raw / tick + 1e-12) * tick

    def _update_chandelier(self, symbol: str, h4_index: int) -> None:
        position = self.positions.get(symbol)
        if position is None or self.variant.exit_family != "SPLIT" or not position.tp2_done:
            return
        atr = self.prepared[symbol].atr4[h4_index]
        if atr is None:
            return
        if position.setup.direction == "LONG":
            candidate = position.highest - self.params.chandelier_atr * atr
            position.pending_stop = max(position.stop, position.pending_stop or float("-inf"), candidate)
        else:
            candidate = position.lowest + self.params.chandelier_atr * atr
            position.pending_stop = min(position.stop, position.pending_stop or float("inf"), candidate)

    def _targets(self, symbol: str, active: ActiveSetup, confirmation_ms: int) -> tuple[float, float] | None:
        return strategy.structural_targets(
            active.setup, float(active.entry_plan), self.prepared[symbol].pivots, confirmation_ms,
            self.params.structural_target_lookback_4h, self.prepared[symbol].data.tick_size,
        )

    def _shadow_reference(self, symbol: str, active: ActiveSetup) -> dict:
        """Counterfactual reference exit; it never touches portfolio state."""
        assert active.confirmation_index is not None and active.atr1_confirmation is not None
        candles = self.prepared[symbol].data.h1
        start = active.confirmation_index + 1
        if start >= len(candles):
            return {"shadow_outcome": "NO_DATA", "shadow_net_r": None}
        direction = active.setup.direction
        plan = float(active.entry_plan)
        fill_index: int | None = None
        raw_entry: float | None = None
        if self.variant.entry_mode == "ENTRY_MARKET_NEXT_OPEN":
            candle = candles[start]
            self._current_slippage = self._slippage(symbol)
            effective = self._effective(direction, candle.open)
            stop, _, too_wide = strategy.initial_stop(
                active.setup, effective, active.atr1_confirmation, self.params,
            )
            target_hit = candle.open >= active.setup.reference_tp if direction == "LONG" else candle.open <= active.setup.reference_tp
            invalid = (candle.open <= active.setup.channel.upper - self.params.breakout_atr * active.setup.channel.atr_ref
                       if direction == "LONG" else
                       candle.open >= active.setup.channel.lower + self.params.breakout_atr * active.setup.channel.atr_ref)
            if target_hit or invalid or too_wide or strategy.gross_rr(effective, stop, active.setup.reference_tp) < self.variant.minimum_rr:
                return {"shadow_outcome": "REJECTED", "shadow_net_r": None}
            fill_index, raw_entry = start, candle.open
            entry_effective = effective
        else:
            stop = float(active.stop)
            for index in range(start, min(start + self.params.limit_expiry_1h, len(candles))):
                candle = candles[index]
                target_hit = strategy.target_touched(active.setup, candle)
                fill_hit = candle.low <= plan if direction == "LONG" else candle.high >= plan
                if target_hit:
                    return {"shadow_outcome": "TARGET_BEFORE_ENTRY", "shadow_net_r": None}
                if fill_hit:
                    fill_index = index
                    raw_entry = candle.open if ((direction == "LONG" and candle.open <= plan)
                                                or (direction == "SHORT" and candle.open >= plan)) else plan
                    break
            if fill_index is None:
                return {"shadow_outcome": "NO_FILL", "shadow_net_r": None}
            self._current_slippage = self._slippage(symbol)
            entry_effective = self._effective(direction, float(raw_entry))
        estimated = self._estimated_risk_cost(
            symbol, entry_effective if self.variant.entry_mode == "ENTRY_MARKET_NEXT_OPEN" else plan,
            stop, self.variant.entry_mode == "ENTRY_MARKET_NEXT_OPEN",
        )
        risk_unit = abs((entry_effective if self.variant.entry_mode == "ENTRY_MARKET_NEXT_OPEN" else plan) - stop) + estimated
        qty = 1.0 / risk_unit
        exit_raw = candles[-1].close
        exit_ms = candles[-1].start_ms + strategy.HOUR_MS
        outcome = "DATA_END"
        timeout = candles[fill_index].start_ms + self.params.position_timeout_hours * strategy.HOUR_MS
        for candle in candles[fill_index:]:
            if candle.start_ms >= timeout:
                exit_raw, exit_ms, outcome = candle.open, candle.start_ms, "TIMEOUT"
                break
            hit_sl = candle.low <= stop if direction == "LONG" else candle.high >= stop
            hit_tp = strategy.target_touched(active.setup, candle)
            if hit_sl:
                exit_raw = min(stop, candle.open) if direction == "LONG" else max(stop, candle.open)
                exit_ms, outcome = candle.start_ms + strategy.HOUR_MS, "SL"
                break
            if hit_tp:
                exit_raw, exit_ms, outcome = active.setup.reference_tp, candle.start_ms + strategy.HOUR_MS, "TP"
                break
        self._current_slippage = self._slippage(symbol)
        exit_effective = self._exit_effective(
            direction, exit_raw,
            float(self.costs["stop_slippage_multiplier"]) if outcome == "SL" else 1.0,
        )
        sign = 1.0 if direction == "LONG" else -1.0
        price_pnl = sign * (exit_effective - entry_effective) * qty
        fees = float(self.costs["taker_fee_rate"]) * qty * (entry_effective + exit_effective)
        funding = 0.0
        for timestamp, rate in self.prepared[symbol].funding:
            if candles[fill_index].start_ms < timestamp <= exit_ms:
                funding += -sign * rate * entry_effective * qty
        return {"shadow_outcome": outcome, "shadow_net_r": price_pnl - fees + funding}

    def _confirm(self, symbol: str, index: int, candle: strategy.Candle) -> None:
        active = self.active[symbol]
        atr1 = self.prepared[symbol].atr1[index]
        if atr1 is None:
            return
        active.confirmation_ms = candle.start_ms + strategy.HOUR_MS
        active.confirmation_index = index
        active.atr1_confirmation = atr1
        active.entry_plan = strategy.planned_entry(active.setup, self.params)
        stop, _, too_wide = strategy.initial_stop(active.setup, active.entry_plan, atr1, self.params)
        active.stop = stop
        is_limit = self.variant.entry_mode == "ENTRY_LIMIT"
        if is_limit and too_wide:
            self._cancel(symbol, "SL_TOO_WIDE", active.confirmation_ms)
            return
        if self.variant.exit_family == "SPLIT":
            active.targets = self._targets(symbol, active, active.confirmation_ms)
            if active.targets is None:
                shadow = self._shadow_reference(symbol, active)
                self._record_setup(symbol, active, "MISSING_STRUCTURAL_TARGET", active.confirmation_ms,
                                   **shadow)
                self.active.pop(symbol, None)
                return
            tp1, tp2 = active.targets
            history = self.prepared[symbol].data.h1
            first = next((i for i, item in enumerate(history) if item.start_ms >= active.setup.breakout_close_ms), index)
            if any(strategy.target_touched(active.setup, item, tp1) for item in history[first:index + 1]):
                self._cancel(symbol, "TARGET_BEFORE_ENTRY", active.confirmation_ms)
                return
            risk = abs(active.entry_plan - stop)
            if is_limit and abs(tp1 - active.entry_plan) < self.params.minimum_tp1_r * risk:
                shadow = self._shadow_reference(symbol, active)
                self._record_setup(symbol, active, "TP1_TOO_CLOSE", active.confirmation_ms,
                                   **shadow)
                self.active.pop(symbol, None)
                return
            rr_target = tp2
        else:
            rr_target = active.setup.reference_tp
        if is_limit and strategy.gross_rr(active.entry_plan, stop, rr_target) < self.variant.minimum_rr:
            self._cancel(symbol, "RR_REJECTED", active.confirmation_ms)
            return
        self.pending[symbol] = PendingEntry(
            active_ms=active.confirmation_ms,
            confirmation_ms=active.confirmation_ms,
            entry_plan=active.entry_plan,
            stop=stop,
            atr1_confirmation=atr1,
            targets=active.targets,
        )
        self._record_setup(symbol, active, "CONFIRMED", active.confirmation_ms,
                           entry_plan=active.entry_plan, stop=stop, targets=active.targets)
        self._record_setup(
            symbol, active,
            "ORDER_CREATED" if self.variant.entry_mode == "ENTRY_LIMIT" else "MARKET_PENDING",
            active.confirmation_ms, entry_plan=active.entry_plan, stop=stop, targets=active.targets,
        )

    def _market_candidate(self, symbol: str, candle: strategy.Candle) -> tuple | None:
        pending = self.pending.get(symbol)
        active = self.active.get(symbol)
        if pending is None or active is None or pending.active_ms != candle.start_ms:
            return None
        self._current_slippage = self._slippage(symbol)
        effective = self._effective(active.setup.direction, candle.open)
        stop, _, too_wide = strategy.initial_stop(
            active.setup, effective, pending.atr1_confirmation, self.params,
        )
        target = active.setup.reference_tp if self.variant.exit_family == "REFERENCE" else pending.targets[0]
        if (candle.open >= target) if active.setup.direction == "LONG" else (candle.open <= target):
            self._cancel(symbol, "TARGET_AT_OPEN", candle.start_ms)
            return None
        if (candle.open <= stop) if active.setup.direction == "LONG" else (candle.open >= stop):
            self._cancel(symbol, "SL_AT_OPEN", candle.start_ms)
            return None
        invalid = (
            candle.open <= active.setup.channel.upper - self.params.breakout_atr * active.setup.channel.atr_ref
            if active.setup.direction == "LONG"
            else candle.open >= active.setup.channel.lower + self.params.breakout_atr * active.setup.channel.atr_ref
        )
        if invalid:
            self._cancel(symbol, "INVALID_AT_OPEN", candle.start_ms)
            return None
        if self.variant.exit_family == "SPLIT":
            tp1, tp2 = pending.targets
            if abs(tp1 - effective) < self.params.minimum_tp1_r * abs(effective - stop):
                shadow = self._shadow_reference(symbol, active)
                self._record_setup(symbol, active, "TP1_TOO_CLOSE", candle.start_ms,
                                   **shadow)
                self.active.pop(symbol, None)
                self.pending.pop(symbol, None)
                return None
            rr_target = tp2
        else:
            rr_target = active.setup.reference_tp
        if too_wide:
            self._cancel(symbol, "SL_TOO_WIDE", candle.start_ms)
            return None
        if strategy.gross_rr(effective, stop, rr_target) < self.variant.minimum_rr:
            self._cancel(symbol, "RR_REJECTED", candle.start_ms)
            return None
        return pending.confirmation_ms, self.rank[symbol], symbol, candle.open, effective, stop

    def _limit_candidate(self, symbol: str, candle: strategy.Candle) -> tuple | None:
        pending = self.pending.get(symbol)
        active = self.active.get(symbol)
        if pending is None or active is None or candle.start_ms < pending.active_ms:
            return None
        direction = active.setup.direction
        target = active.setup.reference_tp if self.variant.exit_family == "REFERENCE" else pending.targets[0]
        target_hit = candle.high >= target if direction == "LONG" else candle.low <= target
        fill_hit = candle.low <= pending.entry_plan if direction == "LONG" else candle.high >= pending.entry_plan
        if target_hit:
            self._cancel(symbol, "TARGET_BEFORE_ENTRY", candle.start_ms + strategy.HOUR_MS)
            return None
        if not fill_hit:
            pending.bars_seen += 1
            if pending.bars_seen >= self.params.limit_expiry_1h:
                self._cancel(symbol, "ORDER_EXPIRED", candle.start_ms + strategy.HOUR_MS)
            return None
        raw = candle.open if ((direction == "LONG" and candle.open <= pending.entry_plan)
                              or (direction == "SHORT" and candle.open >= pending.entry_plan)) else pending.entry_plan
        self._current_slippage = self._slippage(symbol)
        effective = self._effective(direction, raw)
        return pending.confirmation_ms, self.rank[symbol], symbol, raw, effective, pending.stop

    def _process_close(self, symbol: str, index: int, candle: strategy.Candle) -> None:
        close_ms = candle.start_ms + strategy.HOUR_MS
        prepared = self.prepared[symbol]
        h4_index = prepared.h4_by_close.get(close_ms)
        if h4_index is not None:
            self._update_chandelier(symbol, h4_index)
            active = self.active.get(symbol)
            if active is not None and symbol not in self.positions:
                if strategy.invalidated_at_close(active.setup, prepared.data.h4[h4_index].close, self.params):
                    self._cancel(symbol, "CLOSE_BACK_IN_CHANNEL", close_ms)
                    active = None
            self._maybe_breakout(symbol, h4_index)
        active = self.active.get(symbol)
        if active is None or symbol in self.pending:
            return
        if close_ms >= active.setup.retest_deadline_ms:
            reason = "CONFIRMATION_TIMEOUT" if active.retested else "RETEST_TIMEOUT"
            self._cancel(symbol, reason, close_ms)
            return
        if strategy.target_touched(active.setup, candle):
            self._cancel(symbol, "TARGET_BEFORE_ENTRY", close_ms)
            return
        if self.random_seed is not None:
            if close_ms == active.random_confirmation_ms:
                self._confirm(symbol, index, candle)
            return
        if strategy.touches_retest(active.setup, candle, self.params) and not active.retested:
            active.retested = True
            self._record_setup(symbol, active, "RETESTED", close_ms)
        if active.retested and index > 0 and strategy.confirms(active.setup, prepared.data.h1[index - 1], candle):
            self._confirm(symbol, index, candle)

    def _maybe_breakout(self, symbol: str, h4_index: int) -> None:
        if symbol in self.active or symbol in self.pending or symbol in self.positions:
            self.breakout_states.pop(symbol, None)
            return
        prepared = self.prepared[symbol]
        channel = prepared.channels.get(h4_index)
        if channel is not None and channel.identity in self.used_channels:
            channel = None
        previous = self.last_breakout.get(symbol)
        if previous is not None and channel is not None:
            if sum(pivot.start_ms > previous for pivot in channel.high_pivots) < 2:
                channel = None
            elif sum(pivot.start_ms > previous for pivot in channel.low_pivots) < 2:
                channel = None
        state, setup, events = strategy.advance_breakout(
            self.breakout_states.get(symbol, strategy.BreakoutState()), channel,
            prepared.data.h4[h4_index - 1].close if h4_index else float('nan'),
            prepared.data.h4[h4_index], self.params)
        self.breakout_states[symbol] = state
        self.transitions.extend(dict(event, symbol=symbol, variant=self.variant.key,
                                     direction=event['direction'] or 'UNASSIGNED') for event in events)
        if setup is None:
            return
        channel = setup.channel
        if not self._filter_allows(prepared, h4_index):
            temp = ActiveSetup(setup)
            self._record_setup(symbol, temp, f"FILTER_REJECTED_{self.variant.filter_mode}", setup.breakout_close_ms)
            return
        self.used_channels.add(channel.identity)
        self.last_breakout[symbol] = setup.breakout_start_ms
        active = ActiveSetup(setup)
        if self.random_seed is not None:
            active.random_confirmation_ms = self._random_confirmation(symbol, setup)
            if active.random_confirmation_ms is None:
                self._record_setup(symbol, active, "RANDOM_NO_CANDIDATE", setup.breakout_close_ms)
                return
        self.active[symbol] = active
        self._record_setup(symbol, active, "BREAKOUT", setup.breakout_close_ms)

    def _random_confirmation(self, symbol: str, setup: strategy.Setup) -> int | None:
        prepared = self.prepared[symbol]
        candidates: list[int] = []
        for candle in prepared.data.h1:
            close_ms = candle.start_ms + strategy.HOUR_MS
            if candle.start_ms < setup.breakout_close_ms:
                continue
            if close_ms >= setup.retest_deadline_ms:
                break
            h4_index = prepared.h4_by_close.get(close_ms)
            if h4_index is not None and strategy.invalidated_at_close(
                setup, prepared.data.h4[h4_index].close, self.params,
            ):
                break
            if strategy.target_touched(setup, candle):
                break
            candidates.append(close_ms)
        if not candidates:
            return None
        seed = f"{self.random_seed}:{symbol}:{setup.breakout_start_ms}"
        return random.Random(seed).choice(candidates)

    def _filter_allows(self, prepared: Prepared, h4_index: int) -> bool:
        mode = self.variant.filter_mode
        if mode == "BASE":
            return True
        if mode == "VOLUME":
            if h4_index < 20:
                return False
            average = sum(item.volume for item in prepared.data.h4[h4_index - 20:h4_index]) / 20.0
            return prepared.data.h4[h4_index].volume >= 1.5 * average
        events = [event for event in prepared.choch if event.index < h4_index]
        if not events:
            return True
        latest = events[-1]
        if mode == "CHOCH_PAUSE_12":
            return h4_index - latest.index >= 12
        return strategy.new_structure_confirmed(latest, h4_index, prepared.data.h4, prepared.pivots)

    def run(self) -> RunResult:
        timeline = sorted({candle.start_ms for prepared in self.prepared.values() for candle in prepared.data.h1})
        for timestamp in timeline:
            current = {
                symbol: prepared.data.h1[index]
                for symbol, prepared in self.prepared.items()
                if (index := prepared.h1_by_start.get(timestamp)) is not None
            }
            for symbol in self.symbols:
                if symbol in current and symbol in self.positions:
                    self._process_position(symbol, current[symbol])

            if self.variant.entry_mode == "ENTRY_MARKET_NEXT_OPEN":
                candidates = [item for symbol in self.symbols if symbol in current
                              if (item := self._market_candidate(symbol, current[symbol])) is not None]
            else:
                candidates = [item for symbol in self.symbols if symbol in current
                              if (item := self._limit_candidate(symbol, current[symbol])) is not None]
            for _, _, symbol, raw, effective, stop in sorted(candidates):
                if len(self.positions) >= int(self.config["maximum_open_positions"]):
                    self._cancel(symbol, "REJECTED_POSITION_LIMIT", timestamp)
                    continue
                self._fill_position(symbol, self.active[symbol], self.pending[symbol], timestamp, raw, effective, stop)
                self._process_position(symbol, current[symbol])

            for symbol in self.symbols:
                candle = current.get(symbol)
                if candle is not None:
                    self._process_close(symbol, self.prepared[symbol].h1_by_start[timestamp], candle)

        for symbol in list(self.positions):
            position = self.positions[symbol]
            last = self.prepared[symbol].data.h1[-1]
            self._close_fraction(position, position.remaining, last.close, last.start_ms + strategy.HOUR_MS, "RUNNER")
            self._finish(symbol, "DATA_END", last.start_ms + strategy.HOUR_MS)
        return RunResult(self.variant.key, self.setups, self.trades, self.equity, self.transitions)


def prepare_market(market: dict[str, MarketData], config: dict) -> dict[str, Prepared]:
    params = strategy.Parameters.from_dict(config)
    return {symbol: prepare(data, params) for symbol, data in market.items()}


def run_variant(market: dict[str, MarketData], config: dict, costs: dict, variant: Variant,
                prepared: dict[str, Prepared] | None = None, random_seed: int | None = None) -> RunResult:
    return PortfolioEngine(market, config, costs, variant, prepared, random_seed).run()
