"""Pure intraday signal generation shared by replay and future market scanners.

No broker, trained model, future returns, or filesystem access. Callers supply
daily rows (date, O,H,L,C,volume_lots) and 5m rows (HH:MM,O,H,L,C,volume_lots).
Only completed bars strictly before the decision time enter a signal.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Engine:
    name: str
    decision_time: str = "10:30"
    stop_pct: float = 1.0
    target_pct: float = 2.0
    exit_time: str = "18:35"
    max_positions: int = 3


# Fixed research family. Parameters are not fitted against a test month.
ENGINES = (
    Engine("opening_follow"),
    Engine("opening_fade"),
    Engine("relative_follow"),
    Engine("relative_fade"),
    Engine("rs5_follow", target_pct=3.0),
    Engine("rs5_reversal_confirmed", target_pct=3.0),
    Engine("opening_breakout"),
    Engine("afternoon_follow", decision_time="13:30"),
)


@dataclass(frozen=True)
class Snapshot:
    date: str
    symbol: str
    as_of_time: str
    last_completed_bar: str
    reference_price: float
    atr_pct: float
    prior_return5_pct: float
    prior_turnover_lower_bound_rub: float
    recent_turnover_lower_bound_rub: float
    opening_return_pct: float
    opening_range_high: float
    opening_range_low: float


@dataclass(frozen=True)
class Signal:
    date: str
    symbol: str
    direction: str
    engine: str
    decision_time: str
    reference_price: float
    score: float
    stop_pct: float
    target_pct: float
    exit_time: str
    max_positions: int
    turnover_lower_bound_rub: float
    last_completed_bar: str


def minutes(time: str) -> int:
    h, m = time.split(":")
    if len(h) != 2 or len(m) != 2 or not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
        raise ValueError("Invalid clock")
    return 60 * int(h) + int(m)


def _validate_rows(rows: Sequence[Sequence]) -> None:
    times = [row[0] for row in rows]
    if times != sorted(set(times)):
        raise ValueError("Input rows must be sorted and unique")
    for row in rows:
        if len(row) not in (6, 7) or any(not math.isfinite(float(x)) for x in row[1:]):
            raise ValueError("Invalid candle values")
        _, o, h, low, c, volume = row[:6]
        if min(o, h, low, c) <= 0 or volume < 0 or low > min(o, c) or h < max(o, c):
            raise ValueError("Invalid candle OHLC/volume")
        if len(row) == 7 and row[6] < 0:
            raise ValueError("Invalid turnover")


def turnover(row: Sequence) -> float:
    """Use actual RUB turnover when provided, otherwise a lots*price bound."""
    return float(row[6] if len(row) == 7 else row[4] * row[5])


def snapshot(symbol: str, trade_date: str, daily_rows: Sequence[Sequence],
             session_rows: Sequence[Sequence], as_of_time: str) -> Snapshot | None:
    """Use prior daily history and completed main-session bars only.

    volume_lots * price is a conservative turnover lower bound, NOT actual
    ruble turnover: a lot may contain several shares. No current lot size is
    retroactively inserted into history.
    """
    cutoff = minutes(as_of_time)
    if cutoff % 5:
        raise ValueError("Decision time must align to five minutes")
    past = [row for row in daily_rows if row[0] < trade_date]
    closed = [row for row in session_rows if "09:50" <= row[0] < as_of_time
              and minutes(row[0]) + 5 <= cutoff]
    _validate_rows(past)
    _validate_rows(closed)
    if len(past) < 21 or len(closed) < 6 or closed[0][0] != "09:50":
        return None
    if minutes(closed[-1][0]) + 5 != cutoff:
        return None  # do not issue a signal from a stale final quote
    atr = statistics.fmean(max(row[2] - row[3], abs(row[2] - prev[4]), abs(row[3] - prev[4]))
                           / prev[4] * 100 for prev, row in zip(past[-15:-1], past[-14:]))
    if atr <= 0:
        return None
    early = [r for r in closed if r[0] < "10:10"]
    return Snapshot(
        trade_date, symbol, as_of_time, closed[-1][0], closed[-1][4], atr,
        (past[-1][4] / past[-6][4] - 1) * 100,
        statistics.median(turnover(r) for r in past[-20:]),
        sum(turnover(r) for r in closed[-6:]),
        (closed[-1][4] / closed[0][1] - 1) * 100,
        max(r[2] for r in early), min(r[3] for r in early),
    )


def select_signals(snapshots: Sequence[Snapshot], engine: Engine) -> list[Signal]:
    """Pick up to three distinct symbols using information already available.

    A 50k leg must be <=1% of the prior 30-minute turnover lower bound.
    Daily liquidity and all ranking features are point-in-time; unknown
    historical borrow availability still needs an external execution preflight.
    """
    if engine not in ENGINES:
        raise ValueError("Unknown or modified engine")
    if len({(s.date, s.as_of_time) for s in snapshots}) > 1:
        raise ValueError("Snapshots must share a date and decision time")
    if len({s.symbol for s in snapshots}) != len(snapshots):
        raise ValueError("Duplicate symbol")
    if any(s.as_of_time != engine.decision_time or s.last_completed_bar >= s.as_of_time for s in snapshots):
        raise ValueError("Snapshot is not available at the engine decision time")
    if any(any(not math.isfinite(x) for x in (
        s.reference_price, s.atr_pct, s.prior_return5_pct, s.prior_turnover_lower_bound_rub,
        s.recent_turnover_lower_bound_rub, s.opening_return_pct, s.opening_range_high, s.opening_range_low,
    )) or s.atr_pct <= 0 or s.reference_price <= 0 for s in snapshots):
        raise ValueError("Invalid snapshot values")
    eligible = [s for s in snapshots if s.prior_turnover_lower_bound_rub >= 50_000_000
                and s.recent_turnover_lower_bound_rub >= 5_000_000]
    if not eligible:
        return []
    median_open = statistics.median(s.opening_return_pct for s in eligible)
    median_rs5 = statistics.median(s.prior_return5_pct for s in eligible)
    ranked = []
    for s in eligible:
        move = s.opening_return_pct
        threshold = .20
        if engine.name.startswith("relative"):
            move -= median_open
        elif engine.name.startswith("rs5"):
            move = s.prior_return5_pct - median_rs5
            threshold = 1.0
            if engine.name == "rs5_reversal_confirmed":
                if move * s.opening_return_pct >= 0 or abs(s.opening_return_pct) < .20:
                    continue
                move = -move
        elif engine.name == "opening_breakout":
            threshold = .10
            if s.reference_price > s.opening_range_high:
                move = (s.reference_price / s.opening_range_high - 1) * 100
            elif s.reference_price < s.opening_range_low:
                move = (s.reference_price / s.opening_range_low - 1) * 100
            else:
                continue
        if engine.name.endswith("fade"):
            move = -move
        if abs(move) < threshold:
            continue
        ranked.append(Signal(s.date, s.symbol, "long" if move > 0 else "short", engine.name,
                             engine.decision_time, s.reference_price, abs(move) / s.atr_pct,
                             engine.stop_pct, engine.target_pct, engine.exit_time,
                             engine.max_positions, s.recent_turnover_lower_bound_rub,
                             s.last_completed_bar))
    ranked.sort(key=lambda s: (-s.score, s.symbol))
    return ranked[:engine.max_positions]
