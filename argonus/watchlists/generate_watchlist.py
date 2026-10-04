#!/usr/bin/env python3
"""
Сканер T-Invest API: анализирует все акции TQBR и генерирует вотчлист
с лучшими кандидатами в лонг и шорт.

Результат совместим с watchlist_best_target.py.

Использование:
    python generate_watchlist.py                         # на сегодня
    python generate_watchlist.py --as-of вчера           # на вчера
    python generate_watchlist.py --as-of 2026-03-27 --top 4
    python generate_watchlist.py -o watchlist.txt        # сохранить в файл
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from argonus.market_data.tbank_market_data import ShareInstrument, TBankApiError, TBankInvestClient

# ─── Configuration ───────────────────────────────────────────────────────────

MIN_SESSIONS = 15
MIN_AVG_VOLUME_RUB = 50_000_000
MIN_PRICE = 1.0
MIN_AVG_RANGE_PCT = 0.8
TOP_PER_DIRECTION = 3
TARGET_RATIOS = [0.33, 0.55, 0.80, 1.10]
MAX_FETCH_WORKERS = 4
RCI_FAST_PERIOD = 9
RCI_SLOW_PERIOD = 26
PPO_FAST_PERIOD = 12
PPO_SLOW_PERIOD = 26
PPO_SIGNAL_PERIOD = 9
DEMARK_SETUP_PERIOD = 9
DEMARK_COUNTDOWN_PERIOD = 13
DEMARK_LOOKBACK_OFFSET = 4
DEMARK_COUNTDOWN_LOOKBACK = 60


_market_client: TBankInvestClient | None = None


# ─── Data structures ────────────────────────────────────────────────────────

@dataclass(slots=True)
class SessionData:
    trade_date: date
    open: float
    low: float
    high: float
    close: float
    volume: float = 0.0


@dataclass(slots=True)
class Candidate:
    symbol: str
    direction: str
    score: float
    reason: str
    sessions: list[SessionData]
    avg_day_rub: float
    avg_day_pct: float
    support: float | None
    resistance: float | None
    targets: list[float]
    instrument_uid: str | None = None
    lot_size: int = 1
    api_trade_available: bool | None = None
    buy_available: bool | None = None
    sell_available: bool | None = None
    short_enabled: bool | None = None
    exchange: str | None = None
    real_exchange: str | None = None


@dataclass(slots=True)
class ScanResult:
    """Selection plus the complete eligible pools used to make it.

    ``selected`` deliberately keeps the legacy order: selected longs first,
    then selected shorts.  The full pools are audit-only and must never feed
    back into the live selection implicitly.
    """

    selected: list[Candidate]
    eligible_longs: list[Candidate]
    eligible_shorts: list[Candidate]


@dataclass(slots=True)
class RciSnapshot:
    fast: float | None
    slow: float | None
    prev_fast: float | None
    prev_slow: float | None

    @property
    def fast_delta(self) -> float:
        if self.fast is None or self.prev_fast is None:
            return 0.0
        return self.fast - self.prev_fast

    @property
    def slow_delta(self) -> float:
        if self.slow is None or self.prev_slow is None:
            return 0.0
        return self.slow - self.prev_slow


@dataclass(slots=True)
class PpoSnapshot:
    ppo: float | None
    signal: float | None
    histogram: float | None
    prev_ppo: float | None
    prev_signal: float | None
    prev_histogram: float | None

    @property
    def ppo_delta(self) -> float:
        if self.ppo is None or self.prev_ppo is None:
            return 0.0
        return self.ppo - self.prev_ppo

    @property
    def histogram_delta(self) -> float:
        if self.histogram is None or self.prev_histogram is None:
            return 0.0
        return self.histogram - self.prev_histogram


@dataclass(slots=True)
class DemarkSnapshot:
    buy_setup: int
    sell_setup: int
    buy_countdown: int
    sell_countdown: int


# ─── Helpers ─────────────────────────────────────────────────────────────────

def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def progress(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def get_market_client() -> TBankInvestClient:
    global _market_client
    if _market_client is None:
        _market_client = TBankInvestClient(user_agent="generate-watchlist/1.0")
    return _market_client


def parse_date(raw: str) -> date:
    value = raw.strip().lower()
    today = date.today()
    aliases = {
        "today": 0, "сегодня": 0,
        "yesterday": -1, "вчера": -1,
        "завтра": 1, "послезавтра": 2,
    }
    if value in aliases:
        return today + timedelta(days=aliases[value])
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Не удалось разобрать дату: {raw}")


def fetch_stock_history(
    share: ShareInstrument,
    as_of: date,
    days: int = 60,
) -> list[SessionData]:
    """Fetch extended history for a single stock from T-Invest API."""
    window_start = as_of - timedelta(days=days * 2)
    window_end = as_of - timedelta(days=1)

    candles = get_market_client().get_daily_candles(
        share.instrument_id,
        start_date=window_start,
        end_date=window_end,
    )
    if not candles:
        return []

    sessions: list[SessionData] = []
    for candle in candles:
        if candle.open <= 0.0 or candle.close <= 0.0:
            continue
        # В T-Invest объём свечи приходит в лотах, поэтому переводим в рубли через lot * close.
        turnover_rub = float(candle.volume_lots) * float(share.lot) * float(candle.close)
        sessions.append(
            SessionData(
                trade_date=candle.trade_date,
                open=candle.open,
                low=candle.low,
                high=candle.high,
                close=candle.close,
                volume=turnover_rub,
            )
        )

    sessions.sort(key=lambda s: s.trade_date)
    return sessions[-days:]


# ─── Technical analysis ──────────────────────────────────────────────────────

def calculate_avg_day_move(sessions: list[SessionData]) -> tuple[float, float]:
    """Return (avg_day_rub, avg_day_pct) based on recent high-low ranges."""
    if len(sessions) < 5:
        return 0.0, 0.0
    window = sessions[-20:]
    ranges_rub = []
    ranges_pct = []
    for s in window:
        r = s.high - s.low
        ranges_rub.append(r)
        ranges_pct.append(r / max(s.close, 0.01) * 100.0)
    return (
        sum(ranges_rub) / len(ranges_rub),
        sum(ranges_pct) / len(ranges_pct),
    )


def detect_levels(
    sessions: list[SessionData],
    avg_day_pct: float,
) -> tuple[float | None, float | None]:
    """Detect resistance and support from price clusters."""
    if len(sessions) < 5:
        return None, None

    window = sessions[-15:] if len(sessions) >= 15 else sessions
    latest_close = window[-1].close
    avg_move_abs = latest_close * avg_day_pct / 100.0
    threshold = avg_move_abs * 0.40

    highs = sorted([s.high for s in window], reverse=True)
    lows = sorted([s.low for s in window])

    def find_cluster(prices: list[float], min_touches: int = 3) -> float | None:
        for anchor in prices:
            cluster = [p for p in prices if abs(p - anchor) <= threshold]
            if len(cluster) >= min_touches:
                return sum(cluster) / len(cluster)
        return None

    resistance = find_cluster(highs)
    support = find_cluster(lows)

    max_dist = avg_day_pct * 2.5 / 100.0
    if resistance is not None and resistance > latest_close * (1.0 + max_dist):
        resistance = None
    if support is not None and support < latest_close * (1.0 - max_dist):
        support = None

    return resistance, support


def calculate_rsi(sessions: list[SessionData], period: int = 14) -> float:
    if len(sessions) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        change = sessions[i].close - sessions[i - 1].close
        if change > 0:
            gains += change
        else:
            losses -= change
    gains /= period
    losses /= period
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - 100.0 / (1.0 + rs)


def calculate_average_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        end_index = index
        while end_index + 1 < len(order) and values[order[end_index + 1]] == values[order[index]]:
            end_index += 1

        rank = (index + 1 + end_index + 1) / 2.0
        for rank_index in range(index, end_index + 1):
            ranks[order[rank_index]] = rank
        index = end_index + 1
    return ranks


def calculate_rci(sessions: list[SessionData], period: int, offset: int = 0) -> float | None:
    end_index = len(sessions) - offset
    start_index = end_index - period
    if start_index < 0 or end_index > len(sessions):
        return None

    prices = [session.close for session in sessions[start_index:end_index]]
    price_ranks = calculate_average_ranks(prices)
    time_ranks = list(range(1, period + 1))
    squared_rank_distance = sum(
        (time_ranks[index] - price_ranks[index]) ** 2
        for index in range(period)
    )
    return (1.0 - 6.0 * squared_rank_distance / (period * (period * period - 1))) * 100.0


def calculate_rci_snapshot(sessions: list[SessionData]) -> RciSnapshot:
    return RciSnapshot(
        fast=calculate_rci(sessions, RCI_FAST_PERIOD),
        slow=calculate_rci(sessions, RCI_SLOW_PERIOD),
        prev_fast=calculate_rci(sessions, RCI_FAST_PERIOD, offset=1),
        prev_slow=calculate_rci(sessions, RCI_SLOW_PERIOD, offset=1),
    )


def calculate_rci_score(direction: str, rci: RciSnapshot) -> tuple[float, str]:
    if (
        rci.fast is None
        or rci.slow is None
        or rci.prev_fast is None
        or rci.prev_slow is None
    ):
        return 0.0, ""

    crossed_up = rci.prev_fast <= rci.prev_slow and rci.fast > rci.slow
    crossed_down = rci.prev_fast >= rci.prev_slow and rci.fast < rci.slow
    bullish_continuation = (
        rci.fast > rci.slow
        and rci.fast >= 45.0
        and rci.slow >= 20.0
        and rci.fast_delta >= -4.0
    )
    bearish_continuation = (
        rci.fast < rci.slow
        and rci.fast <= -45.0
        and rci.slow <= -20.0
        and rci.fast_delta <= 4.0
    )
    bullish_reversal = rci.fast <= -55.0 and rci.fast_delta >= 18.0
    bearish_reversal = rci.fast >= 55.0 and rci.fast_delta <= -18.0

    if direction == "long":
        score = 0.0
        reasons = []
        if crossed_up:
            score += 12.0
            reasons.append("RCI cross up")
        if bullish_continuation:
            score += 8.0
            reasons.append("RCI trend up")
        if bullish_reversal:
            score += 8.0
            reasons.append("RCI reversal up")
        if crossed_down:
            score -= 10.0
            reasons.append("RCI cross down")
        if bearish_continuation:
            score -= 14.0
            reasons.append("RCI trend down")
        return score, ", ".join(reasons)

    if direction == "short":
        score = 0.0
        reasons = []
        if crossed_down:
            score += 12.0
            reasons.append("RCI cross down")
        if bearish_continuation:
            score += 18.0
            reasons.append("RCI trend down")
        if bearish_reversal:
            score += 8.0
            reasons.append("RCI reversal down")
        if crossed_up:
            score -= 10.0
            reasons.append("RCI cross up")
        if bullish_continuation:
            score -= 14.0
            reasons.append("RCI trend up")
        return score, ", ".join(reasons)

    return 0.0, ""


def calculate_ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []

    alpha = 2.0 / (period + 1.0)
    ema = values[0]
    series = [ema]
    for value in values[1:]:
        ema = alpha * value + (1.0 - alpha) * ema
        series.append(ema)
    return series


def calculate_ppo_snapshot(sessions: list[SessionData]) -> PpoSnapshot:
    if len(sessions) < PPO_SLOW_PERIOD + PPO_SIGNAL_PERIOD:
        return PpoSnapshot(None, None, None, None, None, None)

    closes = [session.close for session in sessions]
    fast_ema = calculate_ema_series(closes, PPO_FAST_PERIOD)
    slow_ema = calculate_ema_series(closes, PPO_SLOW_PERIOD)
    ppo_values = [
        ((fast - slow) / slow * 100.0) if slow else 0.0
        for fast, slow in zip(fast_ema, slow_ema)
    ]
    signal_values = calculate_ema_series(ppo_values, PPO_SIGNAL_PERIOD)
    if len(ppo_values) < 2 or len(signal_values) < 2:
        return PpoSnapshot(None, None, None, None, None, None)

    ppo = ppo_values[-1]
    signal = signal_values[-1]
    prev_ppo = ppo_values[-2]
    prev_signal = signal_values[-2]
    return PpoSnapshot(
        ppo=ppo,
        signal=signal,
        histogram=ppo - signal,
        prev_ppo=prev_ppo,
        prev_signal=prev_signal,
        prev_histogram=prev_ppo - prev_signal,
    )


def calculate_ppo_score(direction: str, ppo: PpoSnapshot) -> tuple[float, str]:
    if (
        ppo.ppo is None
        or ppo.signal is None
        or ppo.histogram is None
        or ppo.prev_ppo is None
        or ppo.prev_signal is None
        or ppo.prev_histogram is None
    ):
        return 0.0, ""

    crossed_up = ppo.prev_ppo <= ppo.prev_signal and ppo.ppo > ppo.signal
    crossed_down = ppo.prev_ppo >= ppo.prev_signal and ppo.ppo < ppo.signal
    bullish_momentum = ppo.ppo > ppo.signal and ppo.histogram > 0.0 and ppo.ppo_delta >= 0.0
    bearish_momentum = ppo.ppo < ppo.signal and ppo.histogram < 0.0 and ppo.ppo_delta <= 0.0
    bullish_recovery = ppo.histogram_delta > 0.0 and ppo.ppo < 0.0
    bearish_weakening = ppo.histogram_delta < 0.0 and ppo.ppo > 0.0

    if direction == "long":
        score = 0.0
        reasons = []
        if crossed_up:
            score += 10.0
            reasons.append("PPO cross up")
        if bullish_momentum:
            score += 6.0
            reasons.append("PPO momentum up")
        elif bullish_recovery:
            score += 4.0
            reasons.append("PPO recovery")
        if crossed_down:
            score -= 8.0
            reasons.append("PPO cross down")
        if bearish_momentum:
            score -= 8.0
            reasons.append("PPO momentum down")
        return score, ", ".join(reasons)

    if direction == "short":
        score = 0.0
        reasons = []
        if crossed_down:
            score += 10.0
            reasons.append("PPO cross down")
        if bearish_momentum:
            score += 6.0
            reasons.append("PPO momentum down")
        elif bearish_weakening:
            score += 4.0
            reasons.append("PPO weakening")
        if crossed_up:
            score -= 8.0
            reasons.append("PPO cross up")
        if bullish_momentum:
            score -= 8.0
            reasons.append("PPO momentum up")
        return score, ", ".join(reasons)

    return 0.0, ""


def demark_setup_condition(sessions: list[SessionData], index: int, side: str) -> bool:
    if index < DEMARK_LOOKBACK_OFFSET:
        return False
    if side == "buy":
        return sessions[index].close < sessions[index - DEMARK_LOOKBACK_OFFSET].close
    if side == "sell":
        return sessions[index].close > sessions[index - DEMARK_LOOKBACK_OFFSET].close
    return False


def calculate_demark_setup_count(sessions: list[SessionData], side: str) -> int:
    count = 0
    for index in range(len(sessions) - 1, DEMARK_LOOKBACK_OFFSET - 1, -1):
        if not demark_setup_condition(sessions, index, side):
            break
        count += 1
    return count


def find_recent_demark_setup_completion(sessions: list[SessionData], side: str) -> int | None:
    first_possible = DEMARK_LOOKBACK_OFFSET + DEMARK_SETUP_PERIOD - 1
    min_index = max(first_possible, len(sessions) - DEMARK_COUNTDOWN_LOOKBACK)
    for index in range(len(sessions) - 1, min_index - 1, -1):
        start = index - DEMARK_SETUP_PERIOD + 1
        if all(demark_setup_condition(sessions, setup_index, side) for setup_index in range(start, index + 1)):
            return index
    return None


def calculate_demark_countdown(sessions: list[SessionData], side: str) -> int:
    setup_index = find_recent_demark_setup_completion(sessions, side)
    if setup_index is None:
        return 0

    count = 0
    for index in range(max(setup_index, 2), len(sessions)):
        if side == "buy":
            qualifies = sessions[index].close <= sessions[index - 2].low
        elif side == "sell":
            qualifies = sessions[index].close >= sessions[index - 2].high
        else:
            qualifies = False

        if qualifies:
            count += 1
            if count >= DEMARK_COUNTDOWN_PERIOD:
                return DEMARK_COUNTDOWN_PERIOD
    return count


def calculate_demark_snapshot(sessions: list[SessionData]) -> DemarkSnapshot:
    return DemarkSnapshot(
        buy_setup=calculate_demark_setup_count(sessions, "buy"),
        sell_setup=calculate_demark_setup_count(sessions, "sell"),
        buy_countdown=calculate_demark_countdown(sessions, "buy"),
        sell_countdown=calculate_demark_countdown(sessions, "sell"),
    )


def calculate_demark_score(direction: str, demark: DemarkSnapshot) -> tuple[float, str]:
    buy_setup_ready = demark.buy_setup >= DEMARK_SETUP_PERIOD
    sell_setup_ready = demark.sell_setup >= DEMARK_SETUP_PERIOD
    buy_setup_near = 7 <= demark.buy_setup < DEMARK_SETUP_PERIOD
    sell_setup_near = 7 <= demark.sell_setup < DEMARK_SETUP_PERIOD
    buy_countdown_ready = demark.buy_countdown >= DEMARK_COUNTDOWN_PERIOD
    sell_countdown_ready = demark.sell_countdown >= DEMARK_COUNTDOWN_PERIOD
    buy_countdown_near = 10 <= demark.buy_countdown < DEMARK_COUNTDOWN_PERIOD
    sell_countdown_near = 10 <= demark.sell_countdown < DEMARK_COUNTDOWN_PERIOD

    if direction == "long":
        score = 0.0
        reasons = []
        if buy_setup_ready:
            score += 10.0
            reasons.append("TD buy setup 9")
        elif buy_setup_near:
            score += 5.0
            reasons.append(f"TD buy setup {demark.buy_setup}")
        if buy_countdown_ready:
            score += 12.0
            reasons.append("TD buy countdown 13")
        elif buy_countdown_near:
            score += 6.0
            reasons.append(f"TD buy countdown {demark.buy_countdown}")
        if sell_setup_ready:
            score -= 8.0
            reasons.append("TD sell setup 9 против лонга")
        if sell_countdown_ready:
            score -= 10.0
            reasons.append("TD sell countdown 13 против лонга")
        return score, ", ".join(reasons)

    if direction == "short":
        score = 0.0
        reasons = []
        if sell_setup_ready:
            score += 10.0
            reasons.append("TD sell setup 9")
        elif sell_setup_near:
            score += 5.0
            reasons.append(f"TD sell setup {demark.sell_setup}")
        if sell_countdown_ready:
            score += 12.0
            reasons.append("TD sell countdown 13")
        elif sell_countdown_near:
            score += 6.0
            reasons.append(f"TD sell countdown {demark.sell_countdown}")
        if buy_setup_ready:
            score -= 8.0
            reasons.append("TD buy setup 9 против шорта")
        if buy_countdown_ready:
            score -= 10.0
            reasons.append("TD buy countdown 13 против шорта")
        return score, ", ".join(reasons)

    return 0.0, ""


def score_long(
    sessions: list[SessionData],
    avg_day_pct: float,
    support: float | None,
) -> tuple[float, str]:
    """Score how good a LONG setup is."""
    if len(sessions) < 10:
        return 0.0, ""

    latest = sessions[-1]
    avg = max(avg_day_pct, 0.01)

    # 1. Support proximity
    support_score = 0.0
    if support is not None:
        dist_pct = (latest.close - support) / max(latest.close, 0.01) * 100.0
        dist_ratio = dist_pct / avg
        if -0.3 < dist_ratio < 1.0:
            support_score = clamp(1.0 - abs(dist_ratio), 0.0, 1.0) * 20.0

    # 2. Candle quality (lower wick rejection, bullish body)
    day_range = max(latest.high - latest.low, 0.001)
    lower_wick = (min(latest.open, latest.close) - latest.low) / day_range
    body_ratio = (latest.close - latest.open) / day_range
    candle_score = clamp(lower_wick * 2.0 + max(body_ratio, 0) * 0.5, 0.0, 1.0) * 15.0

    # 3. RSI (oversold = good for long)
    rsi = calculate_rsi(sessions)
    rsi_score = 0.0
    if rsi < 40:
        rsi_score = clamp((40.0 - rsi) / 20.0, 0.0, 1.0) * 15.0
    elif rsi < 50:
        rsi_score = clamp((50.0 - rsi) / 15.0, 0.0, 1.0) * 6.0

    rci_score, rci_reason = calculate_rci_score("long", calculate_rci_snapshot(sessions))
    ppo_score, ppo_reason = calculate_ppo_score("long", calculate_ppo_snapshot(sessions))
    demark_score, demark_reason = calculate_demark_score("long", calculate_demark_snapshot(sessions))

    total = clamp(
        support_score
        + candle_score
        + rsi_score
        + rci_score
        + ppo_score
        + demark_score,
        0.0,
        100.0,
    )

    reasons = []
    if support_score > 10:
        reasons.append("у поддержки")
    if candle_score > 8:
        reasons.append("свеча")
    if rsi_score > 5:
        reasons.append(f"RSI={rsi:.0f}")
    if rci_reason:
        reasons.append(rci_reason)
    if ppo_reason:
        reasons.append(ppo_reason)
    if demark_reason:
        reasons.append(demark_reason)

    return total, ", ".join(reasons) if reasons else "базовый"


def score_short(
    sessions: list[SessionData],
    avg_day_pct: float,
    resistance: float | None,
) -> tuple[float, str]:
    """Score how good a SHORT setup is."""
    if len(sessions) < 10:
        return 0.0, ""

    latest = sessions[-1]
    avg = max(avg_day_pct, 0.01)

    # 1. Resistance proximity
    resistance_score = 0.0
    if resistance is not None:
        dist_pct = (resistance - latest.close) / max(latest.close, 0.01) * 100.0
        dist_ratio = dist_pct / avg
        if -0.3 < dist_ratio < 1.0:
            resistance_score = clamp(1.0 - abs(dist_ratio), 0.0, 1.0) * 20.0

    # 2. Candle quality (upper wick rejection, bearish body)
    day_range = max(latest.high - latest.low, 0.001)
    upper_wick = (latest.high - max(latest.open, latest.close)) / day_range
    body_ratio = (latest.close - latest.open) / day_range
    candle_score = clamp(upper_wick * 2.0 + max(-body_ratio, 0) * 0.5, 0.0, 1.0) * 15.0

    # 3. RSI (overbought = good for short)
    rsi = calculate_rsi(sessions)
    rsi_score = 0.0
    if rsi > 60:
        rsi_score = clamp((rsi - 60.0) / 20.0, 0.0, 1.0) * 15.0
    elif rsi > 50:
        rsi_score = clamp((rsi - 50.0) / 15.0, 0.0, 1.0) * 6.0

    rci_score, rci_reason = calculate_rci_score("short", calculate_rci_snapshot(sessions))
    ppo_score, ppo_reason = calculate_ppo_score("short", calculate_ppo_snapshot(sessions))
    demark_score, demark_reason = calculate_demark_score("short", calculate_demark_snapshot(sessions))

    total = clamp(
        resistance_score
        + candle_score
        + rsi_score
        + rci_score
        + ppo_score
        + demark_score,
        0.0,
        100.0,
    )

    reasons = []
    if resistance_score > 10:
        reasons.append("у сопротивления")
    if candle_score > 8:
        reasons.append("свеча")
    if rsi_score > 5:
        reasons.append(f"RSI={rsi:.0f}")
    if rci_reason:
        reasons.append(rci_reason)
    if ppo_reason:
        reasons.append(ppo_reason)
    if demark_reason:
        reasons.append(demark_reason)

    return total, ", ".join(reasons) if reasons else "базовый"


# ─── Target generation ───────────────────────────────────────────────────────

def generate_targets(
    close: float,
    direction: str,
    avg_day_pct: float,
) -> list[float]:
    """Generate 4 price targets at standard ratios of avg daily move."""
    avg_move = close * avg_day_pct / 100.0
    targets = []
    for ratio in TARGET_RATIOS:
        move = avg_move * ratio
        if direction == "long":
            price = close + move
        else:
            price = close - move
        targets.append(round_price(price, close))
    return targets


def round_price(price: float, reference: float) -> float:
    if reference >= 1000:
        return round(price, 1)
    if reference >= 100:
        return round(price, 2)
    if reference >= 10:
        return round(price, 3)
    if reference >= 1:
        return round(price, 3)
    return round(price, 4)


# ─── Main scan logic ────────────────────────────────────────────────────────

def load_stock_histories(
    shares: list[ShareInstrument],
    as_of: date,
) -> dict[str, tuple[ShareInstrument, list[SessionData]]]:
    histories: dict[str, tuple[ShareInstrument, list[SessionData]]] = {}
    failed_requests = 0
    workers = min(MAX_FETCH_WORKERS, max(len(shares), 1))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(fetch_stock_history, share, as_of): share
            for share in shares
        }
        total = len(future_map)

        for index, future in enumerate(as_completed(future_map), start=1):
            share = future_map[future]
            if index % 20 == 0 or index == total:
                progress(f"  {index}/{total} ...")

            try:
                sessions = future.result()
            except TBankApiError:
                failed_requests += 1
                continue

            if len(sessions) >= MIN_SESSIONS:
                histories[share.ticker] = (share, sessions)

    if failed_requests:
        progress(f"  Ошибок API при загрузке истории: {failed_requests}")

    return histories

def _scan_and_select_result(
    as_of: date,
    top_n: int = TOP_PER_DIRECTION,
    min_volume: float = MIN_AVG_VOLUME_RUB,
) -> ScanResult:
    """Scan TQBR and retain both the legacy selection and full eligible pools."""

    progress("Шаг 1: Список инструментов TQBR через T-Invest API ...")
    shares = get_market_client().list_moex_shares()
    if not shares:
        progress("Ошибка: T-Invest API не вернул список акций TQBR.")
        return ScanResult(selected=[], eligible_longs=[], eligible_shorts=[])

    progress(f"Шаг 2: {len(shares)} инструментов после фильтра площадки/валюты")

    progress(
        f"Шаг 3: Загрузка истории ({len(shares)} инструментов, до {MAX_FETCH_WORKERS} потоков) ..."
    )
    stock_histories = load_stock_histories(shares, as_of)

    progress(f"  С достаточной историей: {len(stock_histories)}")

    progress(f"\nШаг 4: Скоринг ...")
    long_pool: list[Candidate] = []
    short_pool: list[Candidate] = []
    skipped_price = 0
    skipped_volume = 0
    skipped_range = 0
    skipped_buy_blocked = 0
    skipped_short_blocked = 0

    for symbol, (share, sessions) in stock_histories.items():
        latest = sessions[-1]

        if latest.close < MIN_PRICE:
            skipped_price += 1
            continue

        recent_vols = [s.volume for s in sessions[-5:]]
        avg_vol = sum(recent_vols) / len(recent_vols)
        if avg_vol < min_volume:
            skipped_volume += 1
            continue

        avg_day_rub, avg_day_pct = calculate_avg_day_move(sessions)
        if avg_day_pct < MIN_AVG_RANGE_PCT:
            skipped_range += 1
            continue

        resistance, support = detect_levels(sessions, avg_day_pct)

        long_score, long_reason = score_long(sessions, avg_day_pct, support)
        short_score, short_reason = score_short(sessions, avg_day_pct, resistance)

        long_allowed = long_score > 15 and share.buy_available is not False
        short_allowed = short_score > 15 and share.short_enabled is not False

        if long_score > 15 and share.buy_available is False:
            skipped_buy_blocked += 1
        if short_score > 15 and share.short_enabled is False:
            skipped_short_blocked += 1

        if long_allowed and (not short_allowed or long_score >= short_score):
            targets = generate_targets(latest.close, "long", avg_day_pct)
            long_pool.append(
                Candidate(
                    symbol=symbol,
                    direction="long",
                    score=long_score,
                    reason=long_reason,
                    sessions=sessions,
                    avg_day_rub=avg_day_rub,
                    avg_day_pct=avg_day_pct,
                    support=support,
                    resistance=resistance,
                    targets=targets,
                    instrument_uid=share.uid,
                    lot_size=share.lot,
                    api_trade_available=share.api_trade_available,
                    buy_available=share.buy_available,
                    sell_available=share.sell_available,
                    short_enabled=share.short_enabled,
                    exchange=share.exchange,
                    real_exchange=share.real_exchange,
                )
            )
        elif short_allowed:
            targets = generate_targets(latest.close, "short", avg_day_pct)
            short_pool.append(
                Candidate(
                    symbol=symbol,
                    direction="short",
                    score=short_score,
                    reason=short_reason,
                    sessions=sessions,
                    avg_day_rub=avg_day_rub,
                    avg_day_pct=avg_day_pct,
                    support=support,
                    resistance=resistance,
                    targets=targets,
                    instrument_uid=share.uid,
                    lot_size=share.lot,
                    api_trade_available=share.api_trade_available,
                    buy_available=share.buy_available,
                    sell_available=share.sell_available,
                    short_enabled=share.short_enabled,
                    exchange=share.exchange,
                    real_exchange=share.real_exchange,
                )
            )

    progress(
        "  Отфильтровано: "
        f"{skipped_price} по цене, "
        f"{skipped_volume} по объёму, "
        f"{skipped_range} по волатильности, "
        f"{skipped_buy_blocked} недоступно для покупки, "
        f"{skipped_short_blocked} недоступно для шорта"
    )
    progress(f"  Кандидатов: {len(long_pool)} лонг, {len(short_pool)} шорт")

    long_pool.sort(key=lambda c: c.score, reverse=True)
    short_pool.sort(key=lambda c: c.score, reverse=True)

    selected_longs = long_pool[:top_n]
    selected_shorts = short_pool[:top_n]

    # Diagnostic table
    progress(f"\n{'=' * 65}")
    progress(f" ЛОНГ кандидаты (топ-{top_n} из {len(long_pool)}):")
    progress(f"{'=' * 65}")
    for i, c in enumerate(selected_longs, 1):
        progress(
            f"  {i}. {c.symbol:<8} score={c.score:5.1f}  "
            f"close={c.sessions[-1].close:>10.2f}  avg={c.avg_day_pct:.1f}%  "
            f"[{c.reason}]"
        )
    if not selected_longs:
        progress("  (нет кандидатов)")

    progress(f"\n{'=' * 65}")
    progress(f" ШОРТ кандидаты (топ-{top_n} из {len(short_pool)}):")
    progress(f"{'=' * 65}")
    for i, c in enumerate(selected_shorts, 1):
        progress(
            f"  {i}. {c.symbol:<8} score={c.score:5.1f}  "
            f"close={c.sessions[-1].close:>10.2f}  avg={c.avg_day_pct:.1f}%  "
            f"[{c.reason}]"
        )
    if not selected_shorts:
        progress("  (нет кандидатов)")
    progress("")

    return ScanResult(
        selected=selected_longs + selected_shorts,
        eligible_longs=long_pool,
        eligible_shorts=short_pool,
    )


def scan_and_select(
    as_of: date,
    top_n: int = TOP_PER_DIRECTION,
    min_volume: float = MIN_AVG_VOLUME_RUB,
) -> list[Candidate]:
    """Backward-compatible public API returning only the legacy selection."""

    return _scan_and_select_result(as_of, top_n=top_n, min_volume=min_volume).selected


# ─── Output formatting ──────────────────────────────────────────────────────

def format_price(price: float) -> str:
    if price >= 1000:
        return f"{price:.1f}"
    if price >= 100:
        return f"{price:.2f}"
    if price >= 10:
        return f"{price:.3f}"
    if price >= 1:
        return f"{price:.3f}"
    return f"{price:.4f}"


def format_avg_rub(value: float) -> str:
    if value >= 100:
        return f"{value:.0f}"
    if value >= 10:
        return f"{value:.1f}"
    if value >= 1:
        return f"{value:.2f}"
    return f"{value:.3f}"


def format_watchlist(candidates: list[Candidate]) -> str:
    """Format candidates as watchlist.txt compatible with watchlist_best_target.py."""
    longs = [c for c in candidates if c.direction == "long"]
    shorts = [c for c in candidates if c.direction == "short"]

    lines: list[str] = []

    if longs:
        lines.append("Кандидаты в лонг")
        lines.append("")
        for c in longs:
            close_str = format_price(c.sessions[-1].close)
            lines.append(f"${c.symbol}")
            lines.append(f"Close {close_str}")
            lines.append("")
            for i, price in enumerate(c.targets, 1):
                lines.append(f"Цель {i} - {format_price(price)}")
            avg_rub_str = format_avg_rub(c.avg_day_rub)
            lines.append(
                f"В среднем за день проходит {avg_rub_str}р. ({c.avg_day_pct:.1f}%)"
            )
            if c.support is not None:
                lines.append(f"Поддержки {format_price(c.support)}р.")
            lines.append("")

    if shorts:
        lines.append("Кандидаты в шорт")
        lines.append("")
        for c in shorts:
            close_str = format_price(c.sessions[-1].close)
            lines.append(f"${c.symbol}")
            lines.append(f"Close {close_str}")
            lines.append("")
            for i, price in enumerate(c.targets, 1):
                lines.append(f"Цель {i} - {format_price(price)}")
            avg_rub_str = format_avg_rub(c.avg_day_rub)
            lines.append(
                f"В среднем за день проходит {avg_rub_str}р. ({c.avg_day_pct:.1f}%)"
            )
            if c.resistance is not None:
                lines.append(f"Сопротивление {format_price(c.resistance)}р.")
            lines.append("")

    return "\n".join(lines)


# ─── Causal generator audit ───────────────────────────────────────────────────────────

AUDIT_SCHEMA_VERSION = 1
DEFAULT_AUDIT_FILENAME = "watchlist_candidates.json"
AUDIT_ARCHIVE_DIRNAME = "generator_audit"
RESEARCH_AUDIT_DIRNAME = "generator_research_audit"
CAPTURE_MODE_FORWARD = "forward_live"
CAPTURE_MODE_HISTORICAL = "historical_reconstruction"
CAPTURE_MODE_FUTURE = "future_preview"


def _rounded(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(float(value), digits)


def _close_return_pct(sessions: list[SessionData], lookback: int) -> float | None:
    if len(sessions) <= lookback:
        return None
    previous = sessions[-1 - lookback].close
    if previous <= 0:
        return None
    return (sessions[-1].close / previous - 1.0) * 100.0


def _average_range_pct(sessions: list[SessionData], window: int) -> float | None:
    sample = sessions[-window:]
    if not sample:
        return None
    values = [
        (item.high - item.low) / item.close * 100.0
        for item in sample
        if item.close > 0
    ]
    return sum(values) / len(values) if values else None


def _average_turnover_rub(sessions: list[SessionData], window: int) -> float | None:
    sample = sessions[-window:]
    if not sample:
        return None
    return sum(item.volume for item in sample) / len(sample)


def build_causal_features(candidate: Candidate, as_of: date) -> dict[str, object]:
    """Return a compact feature snapshot and reject any same/future-day candle.

    This invariant is intentionally strict.  Silently trimming a leaked candle
    would make a historical audit look causal while hiding an upstream bug.
    """

    sessions = candidate.sessions
    if not sessions:
        raise RuntimeError(f"Audit {candidate.symbol}: пустая история.")
    session_dates = [item.trade_date for item in sessions]
    if session_dates != sorted(session_dates) or len(session_dates) != len(set(session_dates)):
        raise RuntimeError(f"Audit {candidate.symbol}: сессии не отсортированы или дублируются.")
    leaked = [item.trade_date for item in sessions if item.trade_date >= as_of]
    if leaked:
        raise RuntimeError(
            f"Audit {candidate.symbol}: future leak — сессия {max(leaked).isoformat()} "
            f"не раньше as_of {as_of.isoformat()}."
        )

    latest = sessions[-1]
    previous = sessions[-2] if len(sessions) >= 2 else None
    rci = calculate_rci_snapshot(sessions)
    ppo = calculate_ppo_snapshot(sessions)
    demark = calculate_demark_snapshot(sessions)

    gap_pct = None
    if previous is not None and previous.close > 0:
        gap_pct = (latest.open / previous.close - 1.0) * 100.0
    body_pct = (latest.close / latest.open - 1.0) * 100.0 if latest.open > 0 else None
    support_distance = (
        (latest.close / candidate.support - 1.0) * 100.0
        if candidate.support is not None and candidate.support > 0
        else None
    )
    resistance_distance = (
        (candidate.resistance / latest.close - 1.0) * 100.0
        if candidate.resistance is not None and latest.close > 0
        else None
    )

    return {
        "session_count": len(sessions),
        "last_session_date": latest.trade_date.isoformat(),
        "return_1d_pct": _rounded(_close_return_pct(sessions, 1)),
        "return_5d_pct": _rounded(_close_return_pct(sessions, 5)),
        "return_10d_pct": _rounded(_close_return_pct(sessions, 10)),
        "return_20d_pct": _rounded(_close_return_pct(sessions, 20)),
        "latest_gap_pct": _rounded(gap_pct),
        "latest_body_pct": _rounded(body_pct),
        "latest_range_pct": _rounded(
            (latest.high - latest.low) / latest.close * 100.0 if latest.close > 0 else None
        ),
        "avg_range_5d_pct": _rounded(_average_range_pct(sessions, 5)),
        "avg_range_20d_pct": _rounded(_average_range_pct(sessions, 20)),
        "avg_turnover_5d_rub": _rounded(_average_turnover_rub(sessions, 5), 2),
        "avg_turnover_20d_rub": _rounded(_average_turnover_rub(sessions, 20), 2),
        "rsi14": _rounded(calculate_rsi(sessions)),
        "rci9": _rounded(rci.fast),
        "rci26": _rounded(rci.slow),
        "rci9_delta": _rounded(rci.fast_delta),
        "rci26_delta": _rounded(rci.slow_delta),
        "ppo": _rounded(ppo.ppo),
        "ppo_signal": _rounded(ppo.signal),
        "ppo_histogram": _rounded(ppo.histogram),
        "ppo_histogram_delta": _rounded(ppo.histogram_delta),
        "demark_buy_setup": demark.buy_setup,
        "demark_sell_setup": demark.sell_setup,
        "demark_buy_countdown": demark.buy_countdown,
        "demark_sell_countdown": demark.sell_countdown,
        "support_distance_pct": _rounded(support_distance),
        "resistance_distance_pct": _rounded(resistance_distance),
    }


def _candidate_audit_record(
    candidate: Candidate,
    *,
    as_of: date,
    rank: int,
    selected_positions: dict[tuple[str, str], int],
) -> dict[str, object]:
    key = (candidate.direction, candidate.symbol)
    return {
        "rank": rank,
        "selected": key in selected_positions,
        "live_output_position": selected_positions.get(key),
        "symbol": candidate.symbol,
        "direction": candidate.direction,
        "raw_score": _rounded(candidate.score),
        "reason": candidate.reason,
        "close": _rounded(candidate.sessions[-1].close),
        "avg_day_rub": _rounded(candidate.avg_day_rub),
        "avg_day_pct": _rounded(candidate.avg_day_pct),
        "targets": [
            {"label": f"T{index}", "price": _rounded(price)}
            for index, price in enumerate(candidate.targets, start=1)
        ],
        "support": _rounded(candidate.support),
        "resistance": _rounded(candidate.resistance),
        "instrument_snapshot": {
            "instrument_uid": candidate.instrument_uid,
            "lot_size": candidate.lot_size,
            "api_trade_available": candidate.api_trade_available,
            "buy_available": candidate.buy_available,
            "sell_available": candidate.sell_available,
            "short_enabled": candidate.short_enabled,
            "exchange": candidate.exchange,
            "real_exchange": candidate.real_exchange,
            "captured_by": "InstrumentsService/Shares before candidate selection",
        },
        "features": build_causal_features(candidate, as_of),
    }


def build_audit_payload(
    as_of: date,
    scan_result: ScanResult,
    *,
    top_n: int,
    min_volume_rub: float,
    generated_at: str | None = None,
    capture_date: date | None = None,
) -> dict[str, object]:
    """Build a secret-free, point-in-time snapshot of every eligible candidate."""

    actual_capture_date = capture_date or date.today()
    if as_of == actual_capture_date:
        capture_mode = CAPTURE_MODE_FORWARD
        provenance_note = (
            "Captured on as_of using only sessions before as_of; instrument availability "
            "and trading flags are the live API snapshot from the same capture day."
        )
    elif as_of < actual_capture_date:
        capture_mode = CAPTURE_MODE_HISTORICAL
        provenance_note = (
            "Historical reconstruction generated after as_of. Instrument availability and "
            "trading flags come from the current API snapshot, so survivorship/status drift "
            "is possible. This is not a forward observation."
        )
    else:
        capture_mode = CAPTURE_MODE_FUTURE
        provenance_note = (
            "Future preview generated before as_of from data known at capture time. Instrument "
            "availability and trading flags come from the current API snapshot at capture time. "
            "It is research-only and is not a verified forward observation."
        )

    for direction, pool in (
        ("long", scan_result.eligible_longs),
        ("short", scan_result.eligible_shorts),
    ):
        if any(item.direction != direction for item in pool):
            raise RuntimeError(f"Audit: в {direction}-pool найдено другое направление.")
        scores = [item.score for item in pool]
        if scores != sorted(scores, reverse=True):
            raise RuntimeError(f"Audit: {direction}-pool не отсортирован по raw score.")

    selected_positions = {
        (item.direction, item.symbol): position
        for position, item in enumerate(scan_result.selected, start=1)
    }
    long_records = [
        _candidate_audit_record(
            item,
            as_of=as_of,
            rank=rank,
            selected_positions=selected_positions,
        )
        for rank, item in enumerate(scan_result.eligible_longs, start=1)
    ]
    short_records = [
        _candidate_audit_record(
            item,
            as_of=as_of,
            rank=rank,
            selected_positions=selected_positions,
        )
        for rank, item in enumerate(scan_result.eligible_shorts, start=1)
    ]
    observed_dates = [
        item.sessions[-1].trade_date
        for item in scan_result.eligible_longs + scan_result.eligible_shorts
        if item.sessions
    ]

    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "artifact_type": "argonus_generator_candidate_audit",
        "as_of": as_of.isoformat(),
        "generated_at": generated_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "capture_mode": capture_mode,
        "provenance": {
            "capture_date": actual_capture_date.isoformat(),
            "forward_archive_eligible": capture_mode == CAPTURE_MODE_FORWARD,
            "instrument_availability_source": "current_api_snapshot_at_generation_time",
            "note": provenance_note,
        },
        "history_cutoff": {
            "rule": "session.trade_date < as_of",
            "latest_allowed_date": (as_of - timedelta(days=1)).isoformat(),
            "max_observed_session_date": max(observed_dates).isoformat() if observed_dates else None,
            "causal_cutoff_verified": True,
        },
        "config": {
            "board": "TQBR",
            "min_sessions": MIN_SESSIONS,
            "history_sessions_requested": 60,
            "min_avg_volume_rub": float(min_volume_rub),
            "volume_window_sessions": 5,
            "min_price": MIN_PRICE,
            "min_avg_range_pct": MIN_AVG_RANGE_PCT,
            "avg_range_window_sessions": 20,
            "candidate_score_floor_exclusive": 15.0,
            "top_per_direction": int(top_n),
            "target_ratios": list(TARGET_RATIOS),
            "direction_policy": "one_direction_per_symbol_highest_raw_score",
        },
        "pool_counts": {
            "long": len(long_records),
            "short": len(short_records),
            "selected": len(scan_result.selected),
        },
        "selected": [
            {
                "live_output_position": position,
                "direction_rank": (
                    next(
                        index
                        for index, pool_item in enumerate(
                            scan_result.eligible_longs
                            if item.direction == "long"
                            else scan_result.eligible_shorts,
                            start=1,
                        )
                        if pool_item.symbol == item.symbol
                    )
                ),
                "symbol": item.symbol,
                "direction": item.direction,
                "raw_score": _rounded(item.score),
            }
            for position, item in enumerate(scan_result.selected, start=1)
        ],
        "eligible_pools": {
            "long": long_records,
            "short": short_records,
        },
    }


def _atomic_write_text(path: str, text: str, *, overwrite: bool) -> bool:
    """Atomically publish text; return False when immutable target exists."""

    absolute_path = os.path.abspath(path)
    directory = os.path.dirname(absolute_path)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(
        dir=directory,
        prefix=f".{os.path.basename(absolute_path)}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o644)
        if overwrite:
            os.replace(temporary_path, absolute_path)
            temporary_path = ""
            return True
        try:
            # A hard-link publishes the already-fsynced inode atomically and,
            # unlike replace(), can never overwrite the immutable day archive.
            os.link(temporary_path, absolute_path)
            return True
        except FileExistsError:
            return False
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def write_audit_artifacts(payload: dict[str, object], current_path: str) -> tuple[str, bool]:
    """Write current sidecar and an immutable, same-date-idempotent archive."""

    as_of_value = str(payload["as_of"])
    capture_mode = str(payload.get("capture_mode") or "")
    current_absolute = os.path.abspath(current_path)
    if capture_mode == CAPTURE_MODE_FORWARD:
        archive_parts = (AUDIT_ARCHIVE_DIRNAME, f"{as_of_value}.json")
    elif capture_mode in {CAPTURE_MODE_HISTORICAL, CAPTURE_MODE_FUTURE}:
        archive_parts = (RESEARCH_AUDIT_DIRNAME, capture_mode, f"{as_of_value}.json")
    else:
        raise RuntimeError(f"Unsupported audit capture_mode: {capture_mode!r}.")
    archive_path = os.path.join(os.path.dirname(current_absolute), *archive_parts)
    if current_absolute == os.path.abspath(archive_path):
        raise RuntimeError("Current audit path не должен совпадать с immutable archive.")
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    archive_created = _atomic_write_text(archive_path, serialized, overwrite=False)
    _atomic_write_text(current_absolute, serialized, overwrite=True)
    return archive_path, archive_created


def resolve_audit_output(output_path: str | None, explicit_path: str | None, disabled: bool) -> str | None:
    if disabled:
        return None
    if explicit_path:
        return explicit_path
    if output_path:
        return os.path.join(os.path.dirname(os.path.abspath(output_path)), DEFAULT_AUDIT_FILENAME)
    return None


# ─── CLI ─────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Сканер T-Invest API: генерирует вотчлист лучших кандидатов в лонг и шорт.",
    )
    parser.add_argument(
        "--as-of",
        help="Дата анализа (YYYY-MM-DD, сегодня, вчера). По умолчанию: сегодня.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=TOP_PER_DIRECTION,
        help=f"Кандидатов в каждом направлении (по умолчанию {TOP_PER_DIRECTION}).",
    )
    parser.add_argument(
        "--output", "-o",
        help="Файл для сохранения. По умолчанию: stdout.",
    )
    parser.add_argument(
        "--min-volume",
        type=float,
        default=MIN_AVG_VOLUME_RUB / 1_000_000,
        help=f"Мин. средний дневной объём, млн руб. (по умолчанию {MIN_AVG_VOLUME_RUB // 1_000_000}).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Вывести результат в формате JSON.",
    )
    audit_group = parser.add_mutually_exclusive_group()
    audit_group.add_argument(
        "--audit-output",
        help=(
            "Путь current JSON-аудита полного candidate pool. "
            "По умолчанию при -o: watchlist_candidates.json рядом с output."
        ),
    )
    audit_group.add_argument(
        "--no-audit-output",
        action="store_true",
        help="Не писать current sidecar и immutable forward/research archive.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.as_of:
        try:
            as_of = parse_date(args.as_of)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    else:
        as_of = date.today()

    min_volume_rub = args.min_volume * 1_000_000

    progress(f"Дата анализа: {as_of.isoformat()}")
    progress(f"Мин. объём: {args.min_volume:.0f} млн руб.")
    progress(f"Топ: {args.top} лонг + {args.top} шорт\n")

    try:
        scan_result = _scan_and_select_result(
            as_of,
            top_n=args.top,
            min_volume=min_volume_rub,
        )
    except TBankApiError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    candidates = scan_result.selected

    if not candidates:
        progress("Не найдено подходящих кандидатов.")
        return 1

    if args.json:
        output = json.dumps(
            [
                {
                    "symbol": c.symbol,
                    "direction": c.direction,
                    "score": round(c.score, 1),
                    "reason": c.reason,
                    "close": c.sessions[-1].close,
                    "avg_day_rub": round(c.avg_day_rub, 2),
                    "avg_day_pct": round(c.avg_day_pct, 2),
                    "targets": c.targets,
                    "support": round(c.support, 2) if c.support else None,
                    "resistance": round(c.resistance, 2) if c.resistance else None,
                }
                for c in candidates
            ],
            ensure_ascii=False,
            indent=2,
        )
    else:
        output = format_watchlist(candidates)

    audit_output = resolve_audit_output(
        args.output,
        args.audit_output,
        args.no_audit_output,
    )
    audit_payload: dict[str, object] | None = None
    if audit_output:
        audit_payload = build_audit_payload(
            as_of,
            scan_result,
            top_n=args.top,
            min_volume_rub=min_volume_rub,
        )

    # Publish the selected watchlist before claiming in an audit that its
    # live_output_position was made available.  Causal audit validation above
    # still fails closed; an output I/O error propagates and leaves no audit.
    if args.output:
        _atomic_write_text(args.output, output, overwrite=True)
        progress(f"Вотчлист сохранён в {args.output}")
    else:
        print(output)

    if audit_output and audit_payload is not None:
        try:
            archive_path, archive_created = write_audit_artifacts(audit_payload, audit_output)
        except OSError as exc:
            # Shadow telemetry must not suppress an otherwise valid live
            # watchlist because a disk/mount temporarily refused the sidecar.
            # Causal validation above intentionally remains fail-closed.
            progress(f"ВНИМАНИЕ: audit sidecar не записан (вотчлист уже сохранён): {exc}")
        else:
            progress(f"Current аудит кандидатов: {audit_output}")
            archive_label = (
                "Forward-архив"
                if audit_payload["capture_mode"] == CAPTURE_MODE_FORWARD
                else "Research-архив"
            )
            progress(
                f"{archive_label}: {archive_path} "
                + ("(создан)" if archive_created else "(уже существует, не изменён)")
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
