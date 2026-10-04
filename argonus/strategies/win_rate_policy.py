"""Pure, research-only execution rules for the 60% win-rate study.

The module cannot access accounts or submit orders. A partial exit is one
trade, and a winner means its TOTAL return after costs is strictly positive.
All wider-stop rules reduce notional to preserve the original planned risk.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from argonus.research import research_0705_execution as base
from argonus.research.research_flat_month_exit_risk import Trade


@dataclass(frozen=True)
class Rule:
    name: str
    target_multiple: float = 1.0
    target_cap_pct: float | None = None
    stop_pct: float = 1.0
    entry_time: str = "07:05"
    time_exit: str = "18:35"
    feature_filter: str | None = None
    confirmation: bool = False
    partial_at_multiple: float | None = None
    partial_fraction: float = 0.5
    breakeven_after_partial: bool = False


# Frozen before evaluating the study. No ticker/date-specific rules.
RULES = (
    Rule("baseline"),
    Rule("target075", target_multiple=.75),
    Rule("target050", target_multiple=.50),
    Rule("target_cap200", target_cap_pct=2.0),
    Rule("target_cap150", target_cap_pct=1.5),
    Rule("target075_exit1700", target_multiple=.75, time_exit="17:00"),
    Rule("target_cap150_exit1700", target_cap_pct=1.5, time_exit="17:00"),
    Rule("stop125", stop_pct=1.25),
    Rule("stop150", stop_pct=1.50),
    Rule("target075_stop125", target_multiple=.75, stop_pct=1.25),
    Rule("target050_stop125", target_multiple=.50, stop_pct=1.25),
    Rule("target_cap150_stop125", target_cap_pct=1.5, stop_pct=1.25),
    Rule("target_cap200_stop125", target_cap_pct=2.0, stop_pct=1.25),
    Rule("partial50_half_target", partial_at_multiple=.50),
    Rule("partial50_three_quarter_target", partial_at_multiple=.75),
    Rule("partial50_half_target_be", partial_at_multiple=.50, breakeven_after_partial=True),
    Rule("partial50_three_quarter_target_be", partial_at_multiple=.75, breakeven_after_partial=True),
    Rule("rs5_nonnegative", feature_filter="rs5_nonnegative"),
    Rule("stock_momentum", feature_filter="stock_momentum"),
    Rule("market_momentum", feature_filter="market_momentum"),
    Rule("stock_not_extended", feature_filter="stock_not_extended"),
    Rule("market_not_extended", feature_filter="market_not_extended"),
    Rule("confirm0705", confirmation=True),
    Rule("confirm0715", entry_time="07:15", confirmation=True),
    Rule("entry0715", entry_time="07:15"),
    Rule("entry0905", entry_time="09:05"),
    Rule("confirm0905", entry_time="09:05", confirmation=True),
    Rule("confirm_partial_be", confirmation=True, partial_at_multiple=.50, breakeven_after_partial=True),
    Rule("market_momentum_partial", feature_filter="market_momentum", partial_at_multiple=.50),
)


@dataclass(frozen=True)
class Leg:
    fraction: float
    time: str
    price: float
    reason: str


@dataclass(frozen=True)
class Execution(base.Execution):
    entry_time: str | None = None
    legs: tuple[Leg, ...] = ()


def passes_filter(rule: Rule, features: Mapping[str, float]) -> bool:
    """Features contain only returns of sessions completed before this date."""
    name = rule.feature_filter
    if name is None:
        return True
    key, low, high = {
        "rs5_nonnegative": ("aligned_rs5_pp", 0.0, math.inf),
        "stock_momentum": ("aligned_stock_return5_pct", 0.0, math.inf),
        "market_momentum": ("aligned_market_return5_pct", 0.0, math.inf),
        "stock_not_extended": ("aligned_stock_return5_pct", -math.inf, 8.0),
        "market_not_extended": ("aligned_market_return5_pct", -math.inf, 5.0),
    }[name]
    value = features.get(key)
    return value is not None and math.isfinite(value) and low <= value <= high


def simulate(trade: Trade, rule: Rule = RULES[0],
             fills: base.FillModel = base.FillModel(),
             features: Mapping[str, float] | None = None) -> Execution:
    """Causal entry, conservative OHLC barriers and weighted partial costs."""
    base._validate(trade, base.Policy(rule.name, stop_pct=rule.stop_pct,
                                     target_multiple=rule.target_multiple,
                                     time_exit=rule.time_exit))
    if not "07:05" <= rule.entry_time < rule.time_exit:
        raise ValueError("Entry must be before exit and at/after 07:05")
    if base._minutes(rule.entry_time) % 5:
        raise ValueError("Entry must align with five-minute candles")
    if rule.target_cap_pct is not None and (
        not math.isfinite(rule.target_cap_pct) or rule.target_cap_pct <= 0
    ):
        raise ValueError("Target cap must be positive and finite")
    if rule.partial_at_multiple is not None and not (
        0 < rule.partial_at_multiple < rule.target_multiple and 0 < rule.partial_fraction < 1
    ):
        raise ValueError("Partial must precede full target and leave a remainder")
    if rule.breakeven_after_partial and rule.partial_at_multiple is None:
        raise ValueError("Breakeven requires a partial exit")
    exposure = min(1.0, 1.0 / rule.stop_pct)
    sign = 1 if trade.direction == "long" else -1
    slip = fills.slippage_side_bps / 10_000
    window = [c for c in trade.candles if c.time >= rule.entry_time]
    if not window or window[0].time != rule.entry_time:
        raise ValueError(f"Missing exact {rule.entry_time} candle: {trade.date} {trade.symbol}")
    entry = window[0].open * (1 + sign * slip)
    legs: list[Leg] = []

    def result(reason: str, ambiguous: bool = False) -> Execution:
        traded = bool(legs)
        if traded:
            if not math.isclose(sum(leg.fraction for leg in legs), 1.0, abs_tol=1e-12):
                raise RuntimeError("Trade is not fully closed")
            average_exit = sum(leg.fraction * leg.price for leg in legs)
            gross = sign * (average_exit / entry - 1) * 100
            fee = fills.fee_side_pct * (1 + average_exit / entry)
        else:
            average_exit = None
            gross = fee = 0.0
        return Execution(trade.date, trade.month, trade.symbol, trade.direction,
                         rule.name, traded, entry, average_exit,
                         legs[-1].time if legs else None, reason,
                         gross, gross - fee, exposure, ambiguous,
                         rule.entry_time if traded else None, tuple(legs))

    if not passes_filter(rule, features or {}):
        return result("skip_feature_filter")
    if rule.confirmation:
        # Every OHLC used here belongs to a bar CLOSED before entry.
        prior = [c for c in trade.candles if "07:00" <= c.time < rule.entry_time]
        if not prior:
            raise ValueError("Missing pre-entry confirmation data")
        if sign * (prior[-1].close - prior[0].open) <= 0:
            return result("skip_morning_confirmation")
    distance = sign * (trade.target_price - entry)
    if distance <= 0:
        return result("skip_target_behind_entry")
    target_distance = distance * rule.target_multiple
    if rule.target_cap_pct is not None:
        target_distance = min(target_distance, entry * rule.target_cap_pct / 100)
    target = entry + sign * target_distance
    partial = (entry + sign * distance * rule.partial_at_multiple
               if rule.partial_at_multiple is not None else None)
    if target <= 0 or (partial is not None and sign * (target - partial) <= 0):
        raise ValueError("Invalid target/partial geometry")
    stop = entry * (1 - sign * rule.stop_pct / 100)
    remaining = 1.0
    pending_breakeven = False

    def close(fraction: float, raw_price: float, clock: str, reason: str) -> None:
        nonlocal remaining
        legs.append(Leg(fraction, clock, raw_price * (1 - sign * slip), reason))
        remaining -= fraction

    for c in window:
        if pending_breakeven:
            stop = entry
            pending_breakeven = False
        if sign * (c.open - stop) <= 0:
            close(remaining, c.open, c.time, "stop_gap")
            return result("stop_gap")
        if sign * (c.open - target) >= 0:
            # The partial threshold precedes full target even at an open gap.
            if partial is not None and not legs:
                close(rule.partial_fraction, partial, c.time, "partial_at_open")
            close(remaining, target, c.time, "target_at_open")
            return result("target_at_open")
        if c.time >= rule.time_exit:
            close(remaining, c.open, c.time, "time_exit_open")
            return result("time_exit_open")
        # A partial at the open occurs before the later H/L path. Its new
        # breakeven stop nevertheless activates only on the NEXT candle.
        if partial is not None and not legs and sign * (c.open - partial) >= 0:
            close(rule.partial_fraction, partial, c.time, "partial_at_open")
            pending_breakeven = rule.breakeven_after_partial
        stop_hit = c.low <= stop if sign > 0 else c.high >= stop
        target_hit = c.high >= target if sign > 0 else c.low <= target
        partial_hit = partial is not None and not legs and (
            c.high >= partial if sign > 0 else c.low <= partial
        )
        if stop_hit:
            close(remaining, stop, c.time, "stop")
            both = bool(target_hit or partial_hit)
            return result("both_stop_first" if both else "stop", both)
        if partial_hit:
            close(rule.partial_fraction, partial, c.time, "partial")
            pending_breakeven = rule.breakeven_after_partial
        if target_hit:
            close(remaining, target, c.time, "target")
            return result("target")
    raise ValueError(f"Missing exit candle >= {rule.time_exit}: {trade.date} {trade.symbol}")
