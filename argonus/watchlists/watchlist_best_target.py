#!/usr/bin/env python3
from __future__ import annotations

from argonus.paths import MODEL_DIR, PROJECT_ROOT

import argparse
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable

from argonus.watchlists import shadow_rs5_selector as rs5_shadow
from argonus.market_data.tbank_market_data import DailyCandle, TBankApiError, TBankInvestClient


DEFAULT_BOARD = "TQBR"
DEFAULT_INDEX_BOARD = "SNDX"
DEFAULT_INDEX_SYMBOL = "IMOEX"
DEFAULT_INPUT_FILENAMES = ("data/watchlists/watchlist.txt", "watchlist.md")
TARGET_RATIO_SWEET_SPOT = 0.60
TARGET_RATIO_SIGMA = 0.18
# EV objective (behind WBT_EV_OBJECTIVE flag): rank by expected % return net of a fixed stop,
# instead of probability of touching a near target. STOP_LOSS_PCT is the user's per-trade stop.
STOP_LOSS_PCT = 1.0
EV_SCORE_SCALE = 12.5  # maps EV% onto a 0..100 score: EV=0 -> 50, EV=+4% -> 100, EV=-4% -> 0
RANK_CALIBRATION_REVERSAL_REGIMES = {
    "counter_impulse_reversal",
    "compressed_level_reversal",
    "micro_overshoot_reversal",
}
RANK_CALIBRATION_LONG_REVERSAL_MARKET_FLOOR = 54.0
RANK_CALIBRATION_LONG_REVERSAL_PENALTY = 4.0
RANK_CALIBRATION_MEAN_REVERSION_HISTORY_FLOOR = 40.0
RANK_CALIBRATION_MEAN_REVERSION_INSTRUMENT_FLOOR = 75.0
RANK_CALIBRATION_MEAN_REVERSION_BONUS = 6.0
RANK_CALIBRATION_SHORT_MEAN_REVERSION_MARKET_FLOOR = 58.0
RANK_CALIBRATION_SHORT_MEAN_REVERSION_SETUP_FLOOR = 50.0
RANK_CALIBRATION_SHORT_MEAN_REVERSION_INSTRUMENT_FLOOR = 72.0
RANK_CALIBRATION_SHORT_MEAN_REVERSION_HISTORY_FLOOR = 39.0
RANK_CALIBRATION_SHORT_MEAN_REVERSION_BONUS = 8.0
RANK_CALIBRATION_SHORT_MEAN_REVERSION_BULLISH_WEAK_MARKET_CEILING = 42.0
RANK_CALIBRATION_SHORT_MEAN_REVERSION_BULLISH_WEAK_SETUP_FLOOR = 70.0
RANK_CALIBRATION_SHORT_MEAN_REVERSION_BULLISH_WEAK_INSTRUMENT_CEILING = 50.0
RANK_CALIBRATION_SHORT_MEAN_REVERSION_BULLISH_WEAK_HISTORY_FLOOR = 60.0
RANK_CALIBRATION_SHORT_MEAN_REVERSION_BULLISH_WEAK_PENALTY = 10.0
RANK_CALIBRATION_SHORT_MEAN_REVERSION_NEUTRAL_STRONG_MARKET_FLOOR = 46.0
RANK_CALIBRATION_SHORT_MEAN_REVERSION_NEUTRAL_STRONG_MARKET_CEILING = 48.0
RANK_CALIBRATION_SHORT_MEAN_REVERSION_NEUTRAL_STRONG_SETUP_FLOOR = 50.0
RANK_CALIBRATION_SHORT_MEAN_REVERSION_NEUTRAL_STRONG_SETUP_CEILING = 60.0
RANK_CALIBRATION_SHORT_MEAN_REVERSION_NEUTRAL_STRONG_INSTRUMENT_FLOOR = 78.0
RANK_CALIBRATION_SHORT_MEAN_REVERSION_NEUTRAL_STRONG_HISTORY_FLOOR = 35.0
RANK_CALIBRATION_SHORT_MEAN_REVERSION_NEUTRAL_STRONG_HISTORY_CEILING = 40.0
RANK_CALIBRATION_SHORT_MEAN_REVERSION_NEUTRAL_STRONG_BONUS = 4.0
RANK_CALIBRATION_LONG_BULLISH_MEAN_REVERSION_MARKET_FLOOR = 56.0
RANK_CALIBRATION_LONG_BULLISH_MEAN_REVERSION_SETUP_FLOOR = 40.0
RANK_CALIBRATION_LONG_BULLISH_MEAN_REVERSION_INSTRUMENT_FLOOR = 60.0
RANK_CALIBRATION_LONG_BULLISH_MEAN_REVERSION_HISTORY_FLOOR = 20.0
RANK_CALIBRATION_LONG_BULLISH_MEAN_REVERSION_BONUS = 7.0
RANK_CALIBRATION_LONG_MEAN_REVERSION_STRONG_INSTRUMENT_MARKET_CEILING = 46.0
RANK_CALIBRATION_LONG_MEAN_REVERSION_STRONG_INSTRUMENT_SETUP_FLOOR = 35.0
RANK_CALIBRATION_LONG_MEAN_REVERSION_STRONG_INSTRUMENT_FLOOR = 80.0
RANK_CALIBRATION_LONG_MEAN_REVERSION_STRONG_INSTRUMENT_BONUS = 20.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_MID_BEARISH_MARKET_FLOOR = 56.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_MID_BEARISH_SETUP_FLOOR = 50.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_MID_BEARISH_SETUP_CEILING = 55.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_MID_BEARISH_INSTRUMENT_FLOOR = 75.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_MID_BEARISH_HISTORY_FLOOR = 38.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_MID_BEARISH_HISTORY_CEILING = 42.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_MID_BEARISH_PENALTY = 10.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_NEUTRAL_SETUP_FLOOR = 35.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_NEUTRAL_SETUP_CEILING = 45.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_NEUTRAL_MARKET_FLOOR = 47.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_NEUTRAL_MARKET_CEILING_2 = 50.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_NEUTRAL_INSTRUMENT_FLOOR = 70.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_NEUTRAL_INSTRUMENT_CEILING = 78.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_NEUTRAL_HISTORY_FLOOR = 50.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_NEUTRAL_PENALTY = 6.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_BULLISH_MARKET_CEILING = 42.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_BULLISH_SETUP_FLOOR = 50.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_BULLISH_SETUP_CEILING = 56.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_BULLISH_INSTRUMENT_CEILING = 70.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_BULLISH_HISTORY_FLOOR = 40.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_BULLISH_HISTORY_CEILING = 45.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_BULLISH_PENALTY = 8.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_STALE_MARKET_CEILING = 56.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_STALE_SETUP_FLOOR = 45.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_STALE_SETUP_CEILING = 52.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_STALE_INSTRUMENT_FLOOR = 78.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_STALE_HISTORY_FLOOR = 55.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_STALE_PENALTY = 14.0
RANK_CALIBRATION_SHORT_OVERSHOOT_WEAK_MARKET_CEILING = 48.0
RANK_CALIBRATION_SHORT_OVERSHOOT_WEAK_INSTRUMENT_CEILING = 60.0
RANK_CALIBRATION_SHORT_OVERSHOOT_WEAK_HISTORY_CEILING = 30.0
RANK_CALIBRATION_SHORT_OVERSHOOT_WEAK_PENALTY = 4.0
RANK_CALIBRATION_SHORT_CAPITULATION_HOT_MARKET_FLOOR = 54.0
RANK_CALIBRATION_SHORT_CAPITULATION_HOT_SETUP_FLOOR = 50.0
RANK_CALIBRATION_SHORT_CAPITULATION_HOT_INSTRUMENT_FLOOR = 75.0
RANK_CALIBRATION_SHORT_CAPITULATION_HOT_HISTORY_FLOOR = 50.0
RANK_CALIBRATION_SHORT_CAPITULATION_HOT_PENALTY = 20.0
RANK_CALIBRATION_LONG_EXTREME_BROKEN_SUPPORT_RATIO = 1.0
RANK_CALIBRATION_LONG_EXTREME_BROKEN_SUPPORT_PENALTY = 8.0
RANK_CALIBRATION_LONG_MEAN_REVERSION_BROKEN_SUPPORT_RATIO = 0.20
RANK_CALIBRATION_LONG_MEAN_REVERSION_BROKEN_SUPPORT_PENALTY = 8.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_NEUTRAL_MARKET_CEILING = 50.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_SETUP_GAP = 20.0
RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_PENALTY = 20.0
RANK_CALIBRATION_LONG_STRONG_REVERSAL_SETUP_CEILING = 35.0
RANK_CALIBRATION_LONG_STRONG_REVERSAL_HISTORY_CEILING = 35.0
RANK_CALIBRATION_LONG_STRONG_REVERSAL_PENALTY = 10.0
RANK_CALIBRATION_LONG_COUNTER_IMPULSE_WEAK_MARKET_FLOOR = 52.0
RANK_CALIBRATION_LONG_COUNTER_IMPULSE_WEAK_MARKET_CEILING = 54.0
RANK_CALIBRATION_LONG_COUNTER_IMPULSE_WEAK_SETUP_CEILING = 25.0
RANK_CALIBRATION_LONG_COUNTER_IMPULSE_WEAK_INSTRUMENT_FLOOR = 80.0
RANK_CALIBRATION_LONG_COUNTER_IMPULSE_WEAK_HISTORY_FLOOR = 40.0
RANK_CALIBRATION_LONG_COUNTER_IMPULSE_WEAK_PENALTY = 8.0
RANK_CALIBRATION_LONG_COMPRESSED_LEVEL_HISTORY_CEILING = 25.0
RANK_CALIBRATION_LONG_COMPRESSED_LEVEL_PENALTY = 5.0
RANK_CALIBRATION_LONG_NEUTRAL_REVERSAL_MARKET_CEILING = 51.0
RANK_CALIBRATION_LONG_NEUTRAL_REVERSAL_SETUP_CEILING = 40.0
RANK_CALIBRATION_LONG_NEUTRAL_REVERSAL_HISTORY_CEILING = 40.0
RANK_CALIBRATION_LONG_NEUTRAL_REVERSAL_PENALTY = 14.0
RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_EDGE_GAP = 5.0
RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_EDGE_SPAN = 20.0
RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_SETUP_FLOOR = 35.0
RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_SETUP_SPAN = 25.0
RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_MARKET_SPAN = 8.0
RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_EDGE_BONUS = 2.0
RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_MARKET_BONUS = 4.0
RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_EXPLICIT_LEVEL_BONUS = 1.5
RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_FRESH_ENTRY_QUALITY_FLOOR = 72.0
RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_FRESH_HISTORY_CEILING = 45.0
RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_FRESH_INSTRUMENT_FLOOR = 60.0
RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_FRESH_FRESHNESS_FLOOR = 70.0
RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_FRESH_VOLUME_FLOOR = 30.0
RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_FRESH_BONUS = 16.0
RANK_CALIBRATION_ONE_DAY_COUNTER_IMPULSE_WEAK_SETUP_CEILING = 28.0
RANK_CALIBRATION_ONE_DAY_COUNTER_IMPULSE_WEAK_MARKET_CEILING = 50.0
RANK_CALIBRATION_ONE_DAY_COUNTER_IMPULSE_WEAK_HISTORY_FLOOR = 45.0
RANK_CALIBRATION_ONE_DAY_COUNTER_IMPULSE_WEAK_PROB_FLOOR = 0.55
RANK_CALIBRATION_ONE_DAY_COUNTER_IMPULSE_WEAK_PENALTY = 6.0
RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_LIVE_SETUP_FLOOR = 78.0
RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_LIVE_ENTRY_QUALITY_FLOOR = 70.0
RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_LIVE_FRESHNESS_FLOOR = 58.0
RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_LIVE_HISTORY_CEILING = 58.0
RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_LIVE_DIRECT_BONUS = 6.0
RANK_CALIBRATION_ONE_DAY_LIVE_READINESS_BASELINE = 60.0
RANK_CALIBRATION_ONE_DAY_LIVE_READINESS_SPAN = 16.0
RANK_CALIBRATION_ONE_DAY_LIVE_READINESS_BONUS = 4.0
RANK_CALIBRATION_ONE_DAY_STRUCTURAL_IMBALANCE_GAP = 8.0
RANK_CALIBRATION_ONE_DAY_STRUCTURAL_IMBALANCE_SPAN = 18.0
RANK_CALIBRATION_ONE_DAY_STRUCTURAL_IMBALANCE_PENALTY = 6.0
RANK_CALIBRATION_ONE_DAY_STALE_LEVEL_FRESHNESS_CEILING = 60.0
RANK_CALIBRATION_ONE_DAY_STALE_LEVEL_ENTRY_CEILING = 68.0
RANK_CALIBRATION_ONE_DAY_STALE_LEVEL_PAPER_GAP = 10.0
RANK_CALIBRATION_ONE_DAY_STALE_LEVEL_PAPER_GAP_SPAN = 18.0
RANK_CALIBRATION_ONE_DAY_STALE_LEVEL_PENALTY = 10.0
RANK_CALIBRATION_ONE_DAY_EXPLICIT_RESCUE_PROB_FLOOR = 0.58
RANK_CALIBRATION_ONE_DAY_EXPLICIT_RESCUE_MISS_CEILING = 0.42
RANK_CALIBRATION_ONE_DAY_EXPLICIT_RESCUE_ENTRY_CEILING = 60.0
RANK_CALIBRATION_ONE_DAY_EXPLICIT_RESCUE_SETUP_FLOOR = 35.0
RANK_CALIBRATION_ONE_DAY_EXPLICIT_RESCUE_INSTRUMENT_FLOOR = 70.0
RANK_CALIBRATION_ONE_DAY_EXPLICIT_RESCUE_BONUS = 7.0
RANK_CALIBRATION_ONE_DAY_LEVEL_REJECTION_RESCUE_PROB_FLOOR = 0.58
RANK_CALIBRATION_ONE_DAY_LEVEL_REJECTION_RESCUE_FRESHNESS_FLOOR = 70.0
RANK_CALIBRATION_ONE_DAY_LEVEL_REJECTION_RESCUE_SETUP_FLOOR = 80.0
RANK_CALIBRATION_ONE_DAY_LEVEL_REJECTION_RESCUE_BONUS = 2.0
RANK_CALIBRATION_ONE_DAY_PIVOT_HIGH_FAST_ENTRY_FLOOR = 72.0
RANK_CALIBRATION_ONE_DAY_PIVOT_HIGH_FAST_FRESHNESS_FLOOR = 72.0
RANK_CALIBRATION_ONE_DAY_PIVOT_HIGH_FAST_SETUP_FLOOR = 75.0
RANK_CALIBRATION_ONE_DAY_PIVOT_HIGH_FAST_HISTORY_CEILING = 35.0
RANK_CALIBRATION_ONE_DAY_PIVOT_HIGH_FAST_INSTRUMENT_FLOOR = 68.0
RANK_CALIBRATION_ONE_DAY_PIVOT_HIGH_FAST_BONUS = 2.0
RANK_CALIBRATION_ONE_DAY_PIVOT_LOW_STALE_PROB_CEILING = 0.56
RANK_CALIBRATION_ONE_DAY_PIVOT_LOW_STALE_SETUP_FLOOR = 80.0
RANK_CALIBRATION_ONE_DAY_PIVOT_LOW_STALE_HISTORY_FLOOR = 50.0
RANK_CALIBRATION_ONE_DAY_PIVOT_LOW_STALE_PENALTY = 2.0
ENTRY_QUALITY_OVERALL_WEIGHT = 0.15
PRECONFIRMATION_REVERSAL_REGIMES = {
    "sticky_level_reversal",
    "micro_overshoot_reversal",
    "overshoot_reversal",
    "fragile_breakout_reversal",
}
RECLAIM_REVERSAL_REGIMES = {"deep_reclaim_reversal"}
ENTRY_QUALITY_REVERSAL_REGIMES = {
    "mean_reversion",
    "counter_impulse_reversal",
    "compressed_level_reversal",
    "micro_overshoot_reversal",
    "overshoot_reversal",
    "extended_overshoot_reversal",
    "sticky_level_reversal",
    "capitulation_reversal",
    "absorption_reversal",
    "pre_level_rejection",
    "fragile_breakout_reversal",
    "deep_reclaim_reversal",
}
ONE_DAY_PIVOT_ANCHOR_KINDS = {"rolling_pivot_high", "rolling_pivot_low"}
ONE_DAY_STALE_EXPLICIT_LEVEL_REGIMES = {
    "mean_reversion",
    "counter_impulse_reversal",
    "compressed_level_reversal",
    "micro_overshoot_reversal",
    "overshoot_reversal",
    "extended_overshoot_reversal",
    "capitulation_reversal",
    "absorption_reversal",
    "pre_level_rejection",
}
ONE_DAY_OVERRIDE_REVERSAL_REGIMES = {
    "counter_impulse_reversal",
    "compressed_level_reversal",
    "micro_overshoot_reversal",
    "overshoot_reversal",
    "extended_overshoot_reversal",
    "capitulation_reversal",
    "absorption_reversal",
}
ONE_DAY_OVERRIDE_TOP2_REGIMES = {"mean_reversion", "level_rejection", "neutral_context"}
ONE_DAY_OVERRIDE_LIVE_LEADER_REGIMES = {"mean_reversion", "level_rejection", "neutral_context"}
SAME_DAY_TOP_RERANKER_MODEL_FILENAME = "same_day_top_reranker_model.json"
SAME_DAY_TOP_WINNER_RERANKER_MODEL_FILENAME = "same_day_top_winner_reranker_model.json"
SAME_DAY_TOP_T2PLUS_RERANKER_MODEL_FILENAME = "same_day_top_t2plus_reranker_model.json"
RUNNER_DAY_CONFIDENCE_MODEL_FILENAME = "runner_day_confidence_model.json"
SAME_DAY_TOP_RERANKER_CONTEXT_NUMERIC_FEATURES = (
    "candidate_rank",
    "overall_gap_vs_top",
    "setup_gap_vs_top",
    "instrument_gap_vs_top",
    "history_gap_vs_top",
    "entry_gap_vs_top",
    "freshness_gap_vs_top",
    "rs_gap_vs_top",
    "prob_t1_gap_vs_top",
    "prob_t3_gap_vs_top",
    "expmax_gap_vs_top",
    "utility_gap_vs_top",
    "missrisk_improvement_vs_top",
    "same_direction_as_top",
    "same_anchor_as_top",
    "same_regime_as_top",
)
SAME_DAY_TOP_RERANKER_BINARY_FEATURES = (
    "is_explicit_level",
    "is_pivot_anchor",
    "is_reversal_regime",
    "is_mean_reversion",
    "is_neutral_post_impulse",
    "is_t2_target",
)
SAME_DAY_STANDALONE_RESCUE_SCAN = 7
SAME_DAY_STANDALONE_RESCUE_TOP_SCORE_CEILING = 54.0
SAME_DAY_STANDALONE_RESCUE_MIN_CANDIDATE_SCORE = 55.0
SAME_DAY_STANDALONE_RESCUE_MIN_SCORE_GAP = 8.0
SAME_DAY_STANDALONE_RESCUE_MIN_UTILITY_GAP = 6.0
SAME_DAY_STANDALONE_RESCUE_MAX_OVERALL_GAP = 42.0
SAME_DAY_STANDALONE_RESCUE_ENTRY_TOLERANCE = 10.0
SAME_DAY_STANDALONE_RESCUE_PROB_T1_TOLERANCE = 0.08
SAME_DAY_STANDALONE_RESCUE_RISK_TOLERANCE = 0.06
SAME_DAY_WINNER_RERANKER_SCAN = 5
SAME_DAY_WINNER_RERANKER_MAX_OVERALL_GAP = 16.0
SAME_DAY_WINNER_RERANKER_MIN_PROB_GAP = 0.10
SAME_DAY_WINNER_RERANKER_TOP_PROB_CEILING = 0.60
SAME_DAY_WINNER_RERANKER_MIN_ALT_PROB = 0.20
SAME_DAY_WINNER_RERANKER_MISS_RISK_TOLERANCE = 0.0
SAME_DAY_T2PLUS_RERANKER_SCAN = 5
SAME_DAY_T2PLUS_RERANKER_MAX_OVERALL_GAP = 12.0
SAME_DAY_T2PLUS_RERANKER_MIN_PROB_GAP = 0.10
SAME_DAY_T2PLUS_RERANKER_TOP_PROB_CEILING = 0.58
SAME_DAY_T2PLUS_RERANKER_MIN_ALT_PROB = 0.24
SAME_DAY_T2PLUS_RERANKER_MISS_RISK_TOLERANCE = 0.0

TICKER_RE = re.compile(r"^[A-Z0-9]{1,10}$")
TICKER_LINE_RE = re.compile(r"^\$?([A-Z0-9]{1,10})$", re.IGNORECASE)
TARGET_RE = re.compile(r"Цель\s*(\d+)\s*[—\-]\s*([\d.,]+)", re.IGNORECASE)
NUMBER_RE_PART = r"\d+(?:[.,]\d+)?"
AVG_MOVE_RE = re.compile(
    rf"В среднем за день проходит\s*({NUMBER_RE_PART})\s*(?:(?:руб|р)\s*)?\.?\s*\(\s*({NUMBER_RE_PART})\s*%\)",
    re.IGNORECASE,
)
CLOSE_RE = re.compile(r"^Close\s+([\d.,]+)$", re.IGNORECASE)
LEVEL_RE = re.compile(
    r"(Поддержк(?:а|и)|Сопротивлени(?:е|я))\s*(?:[—\-:.\s]*)?(\d[\d.,]*)\s*(?:руб|р)?\.?",
    re.IGNORECASE,
)


class ParseError(RuntimeError):
    pass


class MarketDataError(RuntimeError):
    pass


_market_client: TBankInvestClient | None = None
_same_day_top_reranker_model: SameDayTopRerankerModel | None = None
_same_day_top_reranker_checked = False
_same_day_top_winner_reranker_model: SameDayTopRerankerModel | None = None
_same_day_top_winner_reranker_checked = False
_same_day_top_t2plus_reranker_model: SameDayTopRerankerModel | None = None
_same_day_top_t2plus_reranker_checked = False
_runner_day_confidence_model: "RunnerDayConfidenceModel | None" = None
_runner_day_confidence_checked = False


@dataclass(slots=True)
class Target:
    index: int
    price: float


@dataclass(slots=True)
class WatchIdea:
    symbol: str
    targets: list[Target] = field(default_factory=list)
    average_day_rub: float | None = None
    average_day_pct: float | None = None
    support: float | None = None
    resistance: float | None = None
    declared_direction: str | None = None
    reference_close: float | None = None


@dataclass(slots=True)
class SessionData:
    trade_date: date
    open: float
    low: float
    high: float
    close: float
    volume: float = 0.0


@dataclass(slots=True)
class TargetChoice:
    label: str
    price: float
    move_pct_from_close: float
    ratio_to_average_day: float
    score: float


@dataclass(slots=True)
class PostImpulseFeatures:
    state: str = "neutral"
    continuation_prob: float = 0.45
    exhaustion_prob: float = 0.45
    retrace_ratio: float = 0.0
    compression_ratio: float = 1.0
    breakout_proximity: float = 0.0
    impulse_strength: float = 0.0
    impulse_age: int = 0
    anchor_price: float | None = None
    anchor_kind: str | None = None


@dataclass(slots=True)
class IndicatorSnapshot:
    rsi5: float = 50.0
    atr5_pct: float = 0.0
    ema5_gap_pct: float = 0.0
    ema10_gap_pct: float = 0.0
    ema_spread_pct: float = 0.0
    adx14: float = 20.0
    macd_hist_pct: float = 0.0
    stoch_k14: float = 50.0
    bollinger_z20: float = 0.0


@dataclass(slots=True)
class EntryQualitySnapshot:
    total: float = 50.0
    relative_strength: float = 50.0
    volume_quality: float = 50.0
    freshness: float = 50.0


@dataclass(slots=True)
class AnchorInfo:
    price: float
    kind: str
    confidence: float
    explicit: bool = False


@dataclass(slots=True)
class RegimeAssessment:
    regime: str
    confidence: float
    anchor: AnchorInfo | None


@dataclass(slots=True)
class ExpectedUtility:
    score: float
    prob_hit_t1: float
    prob_hit_t3: float
    expected_max_target: float
    risk_complete_miss: float
    target_probabilities: dict[str, float] = field(default_factory=dict)
    expected_value_pct: float = 0.0
    best_ev_label: str | None = None


@dataclass(slots=True)
class IdeaAnalysis:
    symbol: str
    direction: str
    previous_session: SessionData
    best_target: TargetChoice
    ladder_score: float
    setup_score: float
    market_score: float
    instrument_score: float
    history_score: float
    entry_quality_score: float
    relative_strength_score: float
    volume_quality_score: float
    freshness_score: float
    reversion_indicator_score: float
    overall_score: float
    support: float | None
    resistance: float | None
    watchlist_reference_close: float | None = None
    reference_close_delta_pct: float | None = None
    regime: str = "unknown"
    regime_confidence: float = 0.0
    post_impulse_state: str = "neutral"
    continuation_prob: float = 0.0
    exhaustion_prob: float = 0.0
    retrace_ratio: float = 0.0
    compression_ratio: float = 0.0
    breakout_proximity: float = 0.0
    anchor_price: float | None = None
    anchor_kind: str | None = None
    prob_hit_t1: float = 0.0
    prob_hit_t3: float = 0.0
    expected_max_target: float = 0.0
    risk_complete_miss: float = 1.0
    reranker_any_target_prob: float | None = None
    reranker_winner_prob: float | None = None
    reranker_t2plus_prob: float | None = None
    raw_expected_utility_score: float = 0.0
    standalone_selection_score: float = 0.0
    expected_value_pct: float = 0.0
    vol_expansion: float = 1.0
    market_bullish: bool = False
    exit_target: TargetChoice | None = None
    runner_day_prob: float | None = None
    directional_rs5_pp: float | None = None
    rs5_stock_return_5d_pct: float | None = None
    rs5_index_return_5d_pct: float | None = None
    rs5_stock_window: tuple[str, str] | None = None
    rs5_index_window: tuple[str, str] | None = None
    short_rally_10d_pct: float | None = None
    target_probabilities: dict[str, float] = field(default_factory=dict)
    skipped_reason: str | None = None


@dataclass(slots=True)
class SameDayTopRerankerModel:
    numeric_features: tuple[str, ...]
    binary_features: tuple[str, ...]
    regimes: tuple[str, ...]
    anchor_kinds: tuple[str, ...]
    directions: tuple[str, ...]
    post_impulse_states: tuple[str, ...]
    best_targets: tuple[str, ...]
    scaler_mean: tuple[float, ...]
    scaler_scale: tuple[float, ...]
    coefficients: tuple[float, ...]
    intercept: float
    topk: int
    max_overall_gap: float
    min_prob_gap: float
    top_prob_ceiling: float
    min_alt_prob: float
    regime_index: dict[str, int] = field(default_factory=dict)
    anchor_kind_index: dict[str, int] = field(default_factory=dict)
    direction_index: dict[str, int] = field(default_factory=dict)
    post_impulse_state_index: dict[str, int] = field(default_factory=dict)
    best_target_index: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class RunnerDayConfidenceModel:
    """Логистическая модель P(идея дойдёт до T3+) — используется как «уверенность дня»
    для финального выбора: в дни с низкой вероятностью раннера лучше не торговать."""

    features: tuple[str, ...]
    scaler_mean: tuple[float, ...]
    scaler_scale: tuple[float, ...]
    coefficients: tuple[float, ...]
    intercept: float
    threshold: float
    keep_fraction: float
    # Лонгам строже: mean-reversion генератор даёт слабые лонги (валидировано
    # на 6 мес: EV лонгов −0.23% на медвежьих днях), торгуем их только при
    # высокой уверенности.
    threshold_long: float = 0.0


def parse_number(raw: str) -> float:
    return float(raw.replace(" ", "").replace(",", "."))


def parse_analysis_date(raw: str, base_date: date | None = None) -> date:
    value = raw.strip().lower()
    if not value:
        raise ParseError("Пустая дата анализа.")

    if base_date is None:
        base_date = date.today()

    aliases = {
        "today": 0,
        "сегодня": 0,
        "yesterday": -1,
        "вчера": -1,
        "позавчера": -2,
        "tomorrow": 1,
        "завтра": 1,
        "послезавтра": 2,
    }
    if value in aliases:
        return base_date + timedelta(days=aliases[value])

    delta_match = re.fullmatch(r"([+-])\s*(\d+)", value)
    if delta_match:
        sign = 1 if delta_match.group(1) == "+" else -1
        days = int(delta_match.group(2))
        return base_date + timedelta(days=sign * days)

    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue

    raise ParseError(
        "Не понял дату. Используй YYYY-MM-DD, DD.MM.YYYY, сегодня, вчера, завтра, послезавтра, +2 или -1."
    )


def resolve_as_of_date(raw_value: str | None) -> date:
    if raw_value:
        return parse_analysis_date(raw_value)

    if sys.stdin.isatty():
        prompt = "Дата анализа [сегодня]: "
        typed = input(prompt).strip()
        if typed:
            return parse_analysis_date(typed)

    return date.today()


def finalize_idea(current: WatchIdea | None, ideas: list[WatchIdea]) -> None:
    if current is None:
        return
    if not current.targets:
        raise ParseError(f"У инструмента {current.symbol} не найдено ни одной цели.")
    if current.average_day_pct is None:
        raise ParseError(f"У инструмента {current.symbol} не найден средний дневной ход.")
    ideas.append(current)


def parse_watchlist(text: str) -> list[WatchIdea]:
    ideas: list[WatchIdea] = []
    current: WatchIdea | None = None
    current_declared_direction: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if line.startswith("Samnakopil_"):
            continue
        if "Сегодня" in line and not TICKER_RE.fullmatch(line):
            continue
        lowered = line.lower()
        if "кандидаты в лонг" in lowered:
            current_declared_direction = "long"
            continue
        if "кандидаты в шорт" in lowered:
            current_declared_direction = "short"
            continue

        ticker_match = TICKER_LINE_RE.fullmatch(line)
        if ticker_match:
            finalize_idea(current, ideas)
            current = WatchIdea(
                symbol=ticker_match.group(1).upper(),
                declared_direction=current_declared_direction,
            )
            continue

        if current is None:
            continue

        close_match = CLOSE_RE.match(line)
        if close_match:
            current.reference_close = parse_number(close_match.group(1))
            continue

        target_match = TARGET_RE.search(line)
        if target_match:
            current.targets.append(
                Target(
                    index=int(target_match.group(1)),
                    price=parse_number(target_match.group(2)),
                )
            )
            continue

        avg_match = AVG_MOVE_RE.search(line)
        if avg_match:
            current.average_day_rub = parse_number(avg_match.group(1))
            current.average_day_pct = parse_number(avg_match.group(2))
            continue

        level_match = LEVEL_RE.search(line)
        if level_match:
            label = level_match.group(1).lower()
            price = parse_number(level_match.group(2))
            if label.startswith("поддерж"):
                current.support = price
            else:
                current.resistance = price
            continue

    finalize_idea(current, ideas)
    if not ideas:
        raise ParseError("Не удалось распарсить ни одной идеи из вотчлиста.")
    return ideas


def get_market_client() -> TBankInvestClient:
    global _market_client
    if _market_client is None:
        try:
            _market_client = TBankInvestClient(user_agent="watchlist-best-target/1.0")
        except TBankApiError as exc:
            raise MarketDataError(str(exc)) from exc
    return _market_client


def candles_to_sessions(candles: list[DailyCandle]) -> list[SessionData]:
    return [
        SessionData(
            trade_date=item.trade_date,
            open=item.open,
            low=item.low,
            high=item.high,
            close=item.close,
            volume=float(item.volume_lots),
        )
        for item in candles
    ]


def fetch_previous_session(symbol: str, as_of: date, board: str = DEFAULT_BOARD) -> SessionData:
    sessions = fetch_recent_stock_sessions(symbol, as_of, limit=1, board=board)
    if not sessions:
        raise MarketDataError(f"По {symbol} нет дневных свечей T-Invest API до {as_of.isoformat()}.")
    return sessions[-1]


def fetch_recent_stock_sessions(
    symbol: str,
    as_of: date,
    limit: int = 6,
    board: str = DEFAULT_BOARD,
) -> list[SessionData]:
    return fetch_stock_sessions(symbol, as_of, limit=limit, lookback_days=40, board=board)


def fetch_stock_sessions(
    symbol: str,
    as_of: date,
    limit: int = 180,
    lookback_days: int = 420,
    board: str = DEFAULT_BOARD,
) -> list[SessionData]:
    window_start = as_of - timedelta(days=lookback_days)
    window_end = as_of - timedelta(days=1)

    try:
        candles = get_market_client().get_daily_candles(
            get_market_client().resolve_share_instrument_id(symbol, board),
            start_date=window_start,
            end_date=window_end,
        )
    except TBankApiError as exc:
        raise MarketDataError(
            f"По {symbol} не удалось получить дневные свечи T-Invest API до {as_of.isoformat()}: {exc}"
        ) from exc

    if not candles:
        raise MarketDataError(f"По {symbol} нет дневных свечей T-Invest API до {as_of.isoformat()}.")
    return candles_to_sessions(candles[-limit:])


def fetch_previous_index_sessions(
    symbol: str,
    as_of: date,
    limit: int = 5,
    board: str = DEFAULT_INDEX_BOARD,
) -> list[SessionData]:
    return fetch_index_sessions(symbol, as_of, limit=limit, lookback_days=40, board=board)


def fetch_index_sessions(
    symbol: str,
    as_of: date,
    limit: int = 40,
    lookback_days: int = 120,
    board: str = DEFAULT_INDEX_BOARD,
) -> list[SessionData]:
    window_start = as_of - timedelta(days=lookback_days)
    window_end = as_of - timedelta(days=1)

    try:
        instrument_id = get_market_client().resolve_index_instrument_id(symbol, class_code_hint=board)
        candles = get_market_client().get_daily_candles(
            instrument_id,
            start_date=window_start,
            end_date=window_end,
        )
    except TBankApiError as exc:
        raise MarketDataError(
            f"По индексу {symbol} не удалось получить дневные свечи T-Invest API: {exc}"
        ) from exc

    if not candles:
        raise MarketDataError(f"По индексу {symbol} нет дневных свечей T-Invest API до {as_of.isoformat()}.")
    return candles_to_sessions(candles[-limit:])


def detect_price_levels(sessions: list[SessionData], average_day_pct: float) -> tuple[float | None, float | None]:
    """Detect implicit resistance and support from clusters of highs/lows in recent sessions."""
    if len(sessions) < 4:
        return None, None

    window = sessions[-10:] if len(sessions) >= 10 else sessions
    latest_close = window[-1].close
    avg_move_abs = latest_close * average_day_pct / 100.0
    cluster_threshold = avg_move_abs * 0.35

    highs = sorted([s.high for s in window], reverse=True)
    lows = sorted([s.low for s in window])

    def find_cluster(prices: list[float], min_touches: int = 3) -> float | None:
        for _, anchor in enumerate(prices):
            touches = sum(1 for p in prices if abs(p - anchor) <= cluster_threshold)
            if touches >= min_touches:
                cluster_prices = [p for p in prices if abs(p - anchor) <= cluster_threshold]
                return sum(cluster_prices) / len(cluster_prices)
        return None

    resistance = find_cluster(highs, min_touches=3)
    support = find_cluster(lows, min_touches=3)

    if resistance is not None and resistance < latest_close * 1.001:
        pass
    elif resistance is not None and resistance > latest_close * (1.0 + average_day_pct * 2.0 / 100.0):
        resistance = None

    if support is not None and support > latest_close * 0.999:
        pass
    elif support is not None and support < latest_close * (1.0 - average_day_pct * 2.0 / 100.0):
        support = None

    return resistance, support


def analyze_post_impulse_features(
    direction: str,
    sessions: list[SessionData],
    average_day_pct: float,
) -> PostImpulseFeatures:
    avg_move = max(average_day_pct, 0.01)
    if len(sessions) < 4:
        return PostImpulseFeatures()

    window = sessions[-8:] if len(sessions) >= 8 else sessions
    latest = window[-1]
    direction_sign = 1.0 if direction == "long" else -1.0

    impulse_index = -1
    impulse_strength = 0.0
    for index, session in enumerate(window[:-1]):
        aligned_body_pct = direction_sign * ((session.close / max(session.open, 0.01)) - 1.0) * 100.0
        range_pct = (session.high - session.low) / max(session.close, 0.01) * 100.0
        aligned_body_pct = max(aligned_body_pct, 0.0)
        if aligned_body_pct <= avg_move * 0.55 and range_pct <= avg_move * 1.10:
            continue

        strength = 0.75 * aligned_body_pct / avg_move + 0.25 * range_pct / avg_move
        if strength > impulse_strength:
            impulse_index = index
            impulse_strength = strength

    if impulse_index < 0 or impulse_strength < 1.15:
        return PostImpulseFeatures(
            state="neutral",
            continuation_prob=0.45,
            exhaustion_prob=0.40,
            retrace_ratio=0.0,
            compression_ratio=1.0,
            breakout_proximity=0.0,
            impulse_strength=0.0,
            impulse_age=0,
        )

    impulse = window[impulse_index]
    post_sessions = window[impulse_index + 1:]
    impulse_body_pct = max(direction_sign * ((impulse.close / max(impulse.open, 0.01)) - 1.0) * 100.0, avg_move * 0.30)
    post_bodies = [
        abs(session.close - session.open) / max(session.open, 0.01) * 100.0
        for session in post_sessions
    ] or [0.0]
    compression_ratio = sum(post_bodies) / len(post_bodies) / max(impulse_body_pct, 0.01)

    if direction == "long":
        adverse_extreme = min(session.low for session in post_sessions) if post_sessions else impulse.low
        retrace_pct = max(0.0, (impulse.close - adverse_extreme) / max(impulse.close, 0.01) * 100.0)
        extension_pct = max(0.0, (latest.close - impulse.close) / max(impulse.close, 0.01) * 100.0)
        continuation_trigger = max(session.high for session in post_sessions) if post_sessions else impulse.high
        breakout_gap_pct = max(0.0, (continuation_trigger - latest.close) / max(latest.close, 0.01) * 100.0)
        breakout_proximity = clamp(1.0 - breakout_gap_pct / max(avg_move * 0.9, 0.01), 0.0, 1.0)
        failed_move = latest.close <= impulse.open
        anchor_price = min(session.low for session in post_sessions) if post_sessions else impulse.low
        anchor_kind = "impulse_rebound_low"
        failure_state = "failed_breakout"
    else:
        adverse_extreme = max(session.high for session in post_sessions) if post_sessions else impulse.high
        retrace_pct = max(0.0, (adverse_extreme - impulse.close) / max(impulse.close, 0.01) * 100.0)
        extension_pct = max(0.0, (impulse.close - latest.close) / max(impulse.close, 0.01) * 100.0)
        continuation_trigger = min(session.low for session in post_sessions) if post_sessions else impulse.low
        breakout_gap_pct = max(0.0, (latest.close - continuation_trigger) / max(latest.close, 0.01) * 100.0)
        breakout_proximity = clamp(1.0 - breakout_gap_pct / max(avg_move * 0.9, 0.01), 0.0, 1.0)
        failed_move = latest.close >= impulse.open
        anchor_price = max(session.high for session in post_sessions) if post_sessions else impulse.high
        anchor_kind = "impulse_rebound_high"
        failure_state = "failed_breakdown"

    retrace_ratio = retrace_pct / max(impulse_body_pct, avg_move * 0.45)
    extension_ratio = extension_pct / max(impulse_body_pct, avg_move * 0.45)
    impulse_age = len(window) - 1 - impulse_index

    state = "neutral"
    continuation_prob = 0.45
    exhaustion_prob = 0.45

    if failed_move or retrace_ratio >= 0.95:
        state = failure_state
        continuation_prob = 0.10
        exhaustion_prob = 0.92
    elif breakout_proximity >= 0.72 and extension_ratio >= 0.18:
        state = "trend_continuation"
        continuation_prob = 0.80
        exhaustion_prob = 0.18
    elif retrace_ratio <= 0.40 and compression_ratio <= 0.38:
        state = "pause"
        continuation_prob = 0.76
        exhaustion_prob = 0.20
    elif retrace_ratio <= 0.65 and compression_ratio <= 0.50:
        state = "pause"
        continuation_prob = 0.64
        exhaustion_prob = 0.28
    elif retrace_ratio >= 0.75 and compression_ratio <= 0.35:
        state = "exhaustion"
        continuation_prob = 0.26
        exhaustion_prob = 0.76

    if state in {"failed_breakout", "failed_breakdown"}:
        failure_reclaim_strength = calculate_failure_reclaim_strength(
            direction=direction,
            latest=latest,
            anchor_price=anchor_price,
            average_day_pct=avg_move,
        )
        if failure_reclaim_strength >= 0.60:
            state = "reclaim_after_failure"
            continuation_prob = max(continuation_prob, 0.32 + 0.36 * failure_reclaim_strength)
            exhaustion_prob = min(exhaustion_prob, 0.70 - 0.26 * failure_reclaim_strength)

    strength_bonus = clamp((impulse_strength - 1.10) / 2.40, 0.0, 0.16)
    freshness_penalty = clamp((impulse_age - 3) / 5.0, 0.0, 0.12)
    continuation_prob = clamp(
        continuation_prob + strength_bonus + 0.06 * (1.0 - min(retrace_ratio, 1.0)) - freshness_penalty,
        0.02,
        0.98,
    )
    exhaustion_prob = clamp(
        exhaustion_prob + 0.12 * max(retrace_ratio - 0.50, 0.0) + freshness_penalty - strength_bonus * 0.35,
        0.02,
        0.98,
    )

    return PostImpulseFeatures(
        state=state,
        continuation_prob=continuation_prob,
        exhaustion_prob=exhaustion_prob,
        retrace_ratio=retrace_ratio,
        compression_ratio=compression_ratio,
        breakout_proximity=breakout_proximity,
        impulse_strength=impulse_strength,
        impulse_age=impulse_age,
        anchor_price=anchor_price,
        anchor_kind=anchor_kind,
    )


def select_anchor(
    idea: WatchIdea,
    direction: str,
    sessions: list[SessionData],
    average_day_pct: float,
    post_impulse: PostImpulseFeatures,
) -> AnchorInfo | None:
    avg_move = max(average_day_pct, 0.01)
    latest = sessions[-1]
    day_range = max(latest.high - latest.low, 0.001)
    day_range_pct = day_range / max(latest.close, 0.01) * 100.0

    candidates: list[AnchorInfo] = []
    explicit_price = idea.support if direction == "long" else idea.resistance
    if explicit_price is not None:
        candidates.append(AnchorInfo(price=explicit_price, kind="explicit_level", confidence=0.95, explicit=True))

    auto_resistance, auto_support = detect_price_levels(sessions, avg_move)
    cluster_price = auto_support if direction == "long" else auto_resistance
    if cluster_price is not None:
        candidates.append(AnchorInfo(price=cluster_price, kind="cluster_level", confidence=0.62))

    if post_impulse.anchor_price is not None and post_impulse.anchor_kind is not None:
        impulse_confidence = 0.72 if post_impulse.state in {"pause", "trend_continuation"} else 0.52
        candidates.append(
            AnchorInfo(
                price=post_impulse.anchor_price,
                kind=post_impulse.anchor_kind,
                confidence=impulse_confidence,
            )
        )

    pivot_window = sessions[-5:] if len(sessions) >= 5 else sessions
    pivot_price = min(session.low for session in pivot_window) if direction == "long" else max(session.high for session in pivot_window)
    pivot_kind = "rolling_pivot_low" if direction == "long" else "rolling_pivot_high"
    candidates.append(AnchorInfo(price=pivot_price, kind=pivot_kind, confidence=0.45))

    if not candidates:
        return None

    best_candidate: AnchorInfo | None = None
    best_score = -1e9
    for candidate in candidates:
        close_distance_pct = abs(latest.close - candidate.price) / max(latest.close, 0.01) * 100.0
        close_distance_ratio = close_distance_pct / avg_move
        on_right_side = latest.close >= candidate.price if direction == "long" else latest.close <= candidate.price
        distance_fit = clamp(1.0 - abs(close_distance_ratio - 0.65) / 0.85, 0.0, 1.0)
        side_score = 0.35 if on_right_side else -0.55
        if candidate.explicit and not on_right_side:
            explicit_contact = evaluate_level_contact(direction, latest, candidate.price, avg_move)
            penetration_ratio = float(explicit_contact["penetration_pct"]) / avg_move
            # Keep explicit levels in play only for narrow false-break cases.
            if direction == "long":
                extreme_close = clamp((latest.high - latest.close) / day_range, 0.0, 1.0)
            else:
                extreme_close = clamp((latest.close - latest.low) / day_range, 0.0, 1.0)
            narrow_false_break = (
                close_distance_ratio <= 0.20
                and extreme_close >= 0.92
                and day_range_pct <= avg_move * 0.55
            )
            sticky_near_level = (
                close_distance_ratio <= 0.05
                and 0.20 <= penetration_ratio <= 0.45
                and day_range_pct <= avg_move * 1.55
            )
            if narrow_false_break or sticky_near_level:
                side_score = 0.05
            else:
                side_score -= 0.10
        score = candidate.confidence + side_score + 0.35 * distance_fit
        if score > best_score:
            best_score = score
            best_candidate = candidate

    if best_candidate is None or best_score < 0.20:
        return None
    return best_candidate


def calculate_level_touch_tolerance_abs(reference_price: float, average_day_pct: float) -> float:
    avg_move_abs = reference_price * max(average_day_pct, 0.01) / 100.0
    return max(avg_move_abs * 0.12, reference_price * 0.00025)


def evaluate_level_contact(
    direction: str,
    session: SessionData,
    anchor_price: float,
    average_day_pct: float,
) -> dict[str, float | bool]:
    close_base = max(session.close, 0.001)
    tolerance_abs = calculate_level_touch_tolerance_abs(close_base, average_day_pct)
    tolerance_pct = tolerance_abs / close_base * 100.0

    if direction == "long":
        raw_gap_pct = max(0.0, (session.low - anchor_price) / close_base * 100.0)
        touch_gap_pct = max(0.0, raw_gap_pct - tolerance_pct)
        penetration_pct = max(0.0, (anchor_price - session.low) / close_base * 100.0)
        escape_pct = max(0.0, (session.close - anchor_price) / close_base * 100.0)
        touched = session.low <= anchor_price + tolerance_abs
        close_on_right_side = session.close >= anchor_price
    else:
        raw_gap_pct = max(0.0, (anchor_price - session.high) / close_base * 100.0)
        touch_gap_pct = max(0.0, raw_gap_pct - tolerance_pct)
        penetration_pct = max(0.0, (session.high - anchor_price) / close_base * 100.0)
        escape_pct = max(0.0, (anchor_price - session.close) / close_base * 100.0)
        touched = session.high >= anchor_price - tolerance_abs
        close_on_right_side = session.close <= anchor_price

    return {
        "touched": touched,
        "touch_gap_pct": touch_gap_pct,
        "penetration_pct": penetration_pct,
        "escape_pct": escape_pct,
        "close_on_right_side": close_on_right_side,
    }


def calculate_failure_reclaim_strength(
    direction: str,
    latest: SessionData,
    anchor_price: float | None,
    average_day_pct: float,
) -> float:
    if anchor_price is None:
        return 0.0

    day_range = max(latest.high - latest.low, 0.001)
    close_base = max(latest.close, 0.01)
    avg_move = max(average_day_pct, 0.01)

    if direction == "long":
        close_location = clamp((latest.close - latest.low) / day_range, 0.0, 1.0)
        reclaim_pct = max(0.0, (latest.close - anchor_price) / close_base * 100.0)
        body_pct = max(0.0, (latest.close - latest.open) / max(latest.open, 0.01) * 100.0)
        on_right_side = latest.close >= anchor_price
    else:
        close_location = clamp((latest.high - latest.close) / day_range, 0.0, 1.0)
        reclaim_pct = max(0.0, (anchor_price - latest.close) / close_base * 100.0)
        body_pct = max(0.0, (latest.open - latest.close) / max(latest.open, 0.01) * 100.0)
        on_right_side = latest.close <= anchor_price

    if not on_right_side:
        return 0.0

    range_pct = day_range / close_base * 100.0
    reclaim_fit = clamp(reclaim_pct / max(avg_move * 0.65, 0.01), 0.0, 1.0)
    range_fit = clamp((range_pct / avg_move - 0.80) / 0.80, 0.0, 1.0)
    body_fit = clamp(body_pct / max(avg_move * 0.28, 0.01), 0.0, 1.0)
    return clamp(
        0.38 * close_location
        + 0.32 * reclaim_fit
        + 0.18 * range_fit
        + 0.12 * body_fit,
        0.0,
        1.0,
    )


def calculate_capitulation_reversal_strength(
    average_day_pct: float,
    context: dict[str, float],
    close_distance_ratio: float | None,
    anchor: AnchorInfo | None,
    level_contact: dict[str, float | bool] | None,
) -> float:
    if anchor is None or close_distance_ratio is None or level_contact is None:
        return 0.0

    touched = bool(level_contact["touched"])
    close_on_right_side = bool(level_contact["close_on_right_side"])
    penetration_pct = float(level_contact["penetration_pct"])
    escape_pct = float(level_contact["escape_pct"])
    avg_move = max(average_day_pct, 0.01)

    if not touched or not close_on_right_side:
        return 0.0
    if close_distance_ratio > 0.35:
        return 0.0
    if penetration_pct < avg_move * 0.10 or penetration_pct > avg_move * 0.60:
        return 0.0
    if escape_pct > max(penetration_pct * 0.45, avg_move * 0.08):
        return 0.0
    if context["aligned_ret3"] > -avg_move * 0.70:
        return 0.0
    if context["aligned_body"] > -avg_move * 0.35:
        return 0.0
    if context["day_range_pct"] < avg_move * 0.70:
        return 0.0

    penetration_fit = clamp((penetration_pct / avg_move - 0.10) / 0.30, 0.0, 1.0)
    reclaim_tightness = clamp(
        1.0 - escape_pct / max(penetration_pct, avg_move * 0.08),
        0.0,
        1.0,
    )
    proximity_fit = clamp(1.0 - close_distance_ratio / 0.35, 0.0, 1.0)
    ret3_fit = clamp((-context["aligned_ret3"] / avg_move - 0.70) / 1.00, 0.0, 1.0)
    body_fit = clamp((-context["aligned_body"] / avg_move - 0.35) / 0.90, 0.0, 1.0)
    range_fit = clamp((context["day_range_pct"] / avg_move - 0.70) / 0.70, 0.0, 1.0)
    explicit_fit = 1.0 if anchor.explicit else 0.45

    return clamp(
        0.20 * penetration_fit
        + 0.22 * reclaim_tightness
        + 0.18 * proximity_fit
        + 0.18 * ret3_fit
        + 0.12 * body_fit
        + 0.05 * range_fit
        + 0.05 * explicit_fit,
        0.0,
        1.0,
    )


def calculate_absorption_reversal_strength(
    average_day_pct: float,
    context: dict[str, float],
    close_distance_ratio: float | None,
    anchor: AnchorInfo | None,
    level_contact: dict[str, float | bool] | None,
) -> float:
    if anchor is None or close_distance_ratio is None or level_contact is None or not anchor.explicit:
        return 0.0

    touched = bool(level_contact["touched"])
    close_on_right_side = bool(level_contact["close_on_right_side"])
    penetration_pct = float(level_contact["penetration_pct"])
    escape_pct = float(level_contact["escape_pct"])
    avg_move = max(average_day_pct, 0.01)
    range_ratio = context["day_range_pct"] / avg_move

    if not touched or not close_on_right_side:
        return 0.0
    if close_distance_ratio > 0.05:
        return 0.0
    if penetration_pct > avg_move * 0.18:
        return 0.0
    if escape_pct > avg_move * 0.04:
        return 0.0
    if context["aligned_ret3"] > -avg_move * 0.55:
        return 0.0
    if context["aligned_body"] > -avg_move * 0.18:
        return 0.0
    if range_ratio < 0.70 or range_ratio > 1.45:
        return 0.0

    proximity_fit = clamp(1.0 - close_distance_ratio / 0.05, 0.0, 1.0)
    penetration_fit = clamp(1.0 - penetration_pct / max(avg_move * 0.18, 0.01), 0.0, 1.0)
    ret3_fit = clamp((-context["aligned_ret3"] / avg_move - 0.55) / 0.90, 0.0, 1.0)
    body_fit = clamp((-context["aligned_body"] / avg_move - 0.18) / 0.90, 0.0, 1.0)
    range_fit = clamp(1.0 - abs(range_ratio - 1.00) / 0.55, 0.0, 1.0)
    escape_fit = clamp(1.0 - escape_pct / max(avg_move * 0.04, 0.01), 0.0, 1.0)

    return clamp(
        0.22 * proximity_fit
        + 0.18 * penetration_fit
        + 0.20 * ret3_fit
        + 0.16 * body_fit
        + 0.12 * range_fit
        + 0.12 * escape_fit,
        0.0,
        1.0,
    )


def calculate_pre_level_rejection_strength(
    average_day_pct: float,
    context: dict[str, float],
    close_distance_ratio: float | None,
    anchor: AnchorInfo | None,
    level_contact: dict[str, float | bool] | None,
) -> float:
    if anchor is None or close_distance_ratio is None or level_contact is None or not anchor.explicit:
        return 0.0

    touched = bool(level_contact["touched"])
    close_on_right_side = bool(level_contact["close_on_right_side"])
    touch_gap_pct = float(level_contact["touch_gap_pct"])
    escape_pct = float(level_contact["escape_pct"])
    avg_move = max(average_day_pct, 0.01)
    range_ratio = context["day_range_pct"] / avg_move

    if touched or not close_on_right_side:
        return 0.0
    if close_distance_ratio > 0.45:
        return 0.0
    if touch_gap_pct > avg_move * 0.22:
        return 0.0
    if escape_pct <= 0.0 or escape_pct > avg_move * 0.32:
        return 0.0
    if context["aligned_ret3"] > -avg_move * 0.35:
        return 0.0
    if context["aligned_body"] > avg_move * 0.10:
        return 0.0
    if range_ratio < 0.45 or range_ratio > 1.45:
        return 0.0

    gap_fit = clamp(1.0 - touch_gap_pct / max(avg_move * 0.22, 0.01), 0.0, 1.0)
    proximity_fit = clamp(1.0 - close_distance_ratio / 0.45, 0.0, 1.0)
    ret3_fit = clamp((-context["aligned_ret3"] / avg_move - 0.35) / 0.90, 0.0, 1.0)
    body_fit = clamp((-context["aligned_body"] / avg_move + 0.05) / 0.65, 0.0, 1.0)
    range_fit = clamp(1.0 - abs(range_ratio - 0.90) / 0.55, 0.0, 1.0)
    escape_fit = clamp(1.0 - escape_pct / max(avg_move * 0.32, 0.01), 0.0, 1.0)

    return clamp(
        0.24 * gap_fit
        + 0.20 * proximity_fit
        + 0.18 * ret3_fit
        + 0.14 * body_fit
        + 0.12 * range_fit
        + 0.12 * escape_fit,
        0.0,
        1.0,
    )


def calculate_overshoot_reversal_strength(
    direction: str,
    latest: SessionData,
    explicit_price: float | None,
    average_day_pct: float,
    context: dict[str, float],
) -> tuple[float, float | None, dict[str, float | bool] | None]:
    if explicit_price is None:
        return 0.0, None, None

    avg_move = max(average_day_pct, 0.01)
    close_distance_pct = abs(latest.close - explicit_price) / max(latest.close, 0.01) * 100.0
    close_distance_ratio = close_distance_pct / avg_move
    level_contact = evaluate_level_contact(direction, latest, explicit_price, avg_move)
    touched = bool(level_contact["touched"])
    close_on_right_side = bool(level_contact["close_on_right_side"])
    penetration_pct = float(level_contact["penetration_pct"])
    penetration_ratio = penetration_pct / avg_move
    range_ratio = context["day_range_pct"] / avg_move

    if not touched or close_on_right_side:
        return 0.0, close_distance_ratio, level_contact
    if close_distance_ratio > 0.20:
        return 0.0, close_distance_ratio, level_contact
    if penetration_ratio < 0.80 or penetration_ratio > 1.15:
        return 0.0, close_distance_ratio, level_contact
    if range_ratio < 0.85 or range_ratio > 1.65:
        return 0.0, close_distance_ratio, level_contact
    if context["aligned_ret3"] > -avg_move * 0.35:
        return 0.0, close_distance_ratio, level_contact
    if context["aligned_body"] > avg_move * 0.12:
        return 0.0, close_distance_ratio, level_contact

    penetration_fit = clamp(1.0 - abs(penetration_ratio - 0.95) / 0.20, 0.0, 1.0)
    proximity_fit = clamp(1.0 - close_distance_ratio / 0.20, 0.0, 1.0)
    range_fit = clamp(1.0 - abs(range_ratio - 1.20) / 0.45, 0.0, 1.0)
    ret3_fit = clamp((-context["aligned_ret3"] / avg_move - 0.35) / 0.75, 0.0, 1.0)
    body_fit = clamp((-context["aligned_body"] / avg_move + 0.05) / 0.80, 0.0, 1.0)

    strength = clamp(
        0.28 * penetration_fit
        + 0.24 * proximity_fit
        + 0.18 * range_fit
        + 0.18 * ret3_fit
        + 0.12 * body_fit,
        0.0,
        1.0,
    )
    return strength, close_distance_ratio, level_contact


def calculate_extended_overshoot_reversal_strength(
    direction: str,
    latest: SessionData,
    explicit_price: float | None,
    average_day_pct: float,
    context: dict[str, float],
) -> tuple[float, float | None, dict[str, float | bool] | None]:
    if explicit_price is None:
        return 0.0, None, None

    avg_move = max(average_day_pct, 0.01)
    close_distance_pct = abs(latest.close - explicit_price) / max(latest.close, 0.01) * 100.0
    close_distance_ratio = close_distance_pct / avg_move
    level_contact = evaluate_level_contact(direction, latest, explicit_price, avg_move)
    touched = bool(level_contact["touched"])
    close_on_right_side = bool(level_contact["close_on_right_side"])
    penetration_pct = float(level_contact["penetration_pct"])
    penetration_ratio = penetration_pct / avg_move
    range_ratio = context["day_range_pct"] / avg_move

    if not touched or close_on_right_side:
        return 0.0, close_distance_ratio, level_contact
    if close_distance_ratio < 0.22 or close_distance_ratio > 0.60:
        return 0.0, close_distance_ratio, level_contact
    if penetration_ratio < 0.55 or penetration_ratio > 1.15:
        return 0.0, close_distance_ratio, level_contact
    if range_ratio < 1.15 or range_ratio > 1.85:
        return 0.0, close_distance_ratio, level_contact
    if context["aligned_ret5"] > -avg_move * 1.50:
        return 0.0, close_distance_ratio, level_contact
    if context["aligned_ret3"] < -avg_move * 0.90:
        return 0.0, close_distance_ratio, level_contact
    if context["aligned_body"] > -avg_move * 0.25:
        return 0.0, close_distance_ratio, level_contact

    proximity_fit = clamp(1.0 - abs(close_distance_ratio - 0.40) / 0.20, 0.0, 1.0)
    penetration_fit = clamp(1.0 - abs(penetration_ratio - 0.82) / 0.27, 0.0, 1.0)
    range_fit = clamp(1.0 - abs(range_ratio - 1.45) / 0.35, 0.0, 1.0)
    ret5_fit = clamp((-context["aligned_ret5"] / avg_move - 1.50) / 0.95, 0.0, 1.0)
    ret3_moderation_fit = clamp(1.0 - abs(-context["aligned_ret3"] / avg_move - 0.60) / 0.30, 0.0, 1.0)
    body_fit = clamp((-context["aligned_body"] / avg_move - 0.25) / 0.55, 0.0, 1.0)

    strength = clamp(
        0.22 * proximity_fit
        + 0.22 * penetration_fit
        + 0.18 * range_fit
        + 0.16 * ret5_fit
        + 0.12 * ret3_moderation_fit
        + 0.10 * body_fit,
        0.0,
        1.0,
    )
    return strength, close_distance_ratio, level_contact


def calculate_micro_overshoot_reversal_strength(
    direction: str,
    latest: SessionData,
    explicit_price: float | None,
    average_day_pct: float,
    context: dict[str, float],
) -> tuple[float, float | None, dict[str, float | bool] | None]:
    if explicit_price is None:
        return 0.0, None, None

    avg_move = max(average_day_pct, 0.01)
    close_distance_pct = abs(latest.close - explicit_price) / max(latest.close, 0.01) * 100.0
    close_distance_ratio = close_distance_pct / avg_move
    level_contact = evaluate_level_contact(direction, latest, explicit_price, avg_move)
    touched = bool(level_contact["touched"])
    close_on_right_side = bool(level_contact["close_on_right_side"])
    penetration_pct = float(level_contact["penetration_pct"])
    penetration_ratio = penetration_pct / avg_move
    range_ratio = context["day_range_pct"] / avg_move
    day_range = max(latest.high - latest.low, 0.001)

    if direction == "long":
        extreme_close = clamp((latest.high - latest.close) / day_range, 0.0, 1.0)
    else:
        extreme_close = clamp((latest.close - latest.low) / day_range, 0.0, 1.0)

    if not touched or close_on_right_side:
        return 0.0, close_distance_ratio, level_contact
    if close_distance_ratio > 0.12:
        return 0.0, close_distance_ratio, level_contact
    if penetration_ratio < 0.04 or penetration_ratio > 0.16:
        return 0.0, close_distance_ratio, level_contact
    if extreme_close < 0.82:
        return 0.0, close_distance_ratio, level_contact
    if range_ratio < 0.55 or range_ratio > 1.75:
        return 0.0, close_distance_ratio, level_contact
    if context["aligned_ret3"] > -avg_move * 0.55:
        return 0.0, close_distance_ratio, level_contact
    if context["aligned_body"] > -avg_move * 0.12:
        return 0.0, close_distance_ratio, level_contact

    penetration_fit = clamp(1.0 - abs(penetration_ratio - 0.10) / 0.06, 0.0, 1.0)
    proximity_fit = clamp(1.0 - close_distance_ratio / 0.12, 0.0, 1.0)
    extreme_fit = clamp((extreme_close - 0.82) / 0.18, 0.0, 1.0)
    range_fit = clamp(1.0 - abs(range_ratio - 1.00) / 0.75, 0.0, 1.0)
    ret3_fit = clamp((-context["aligned_ret3"] / avg_move - 0.55) / 0.90, 0.0, 1.0)
    body_fit = clamp((-context["aligned_body"] / avg_move - 0.12) / 0.70, 0.0, 1.0)

    strength = clamp(
        0.20 * penetration_fit
        + 0.20 * proximity_fit
        + 0.18 * extreme_fit
        + 0.14 * range_fit
        + 0.16 * ret3_fit
        + 0.12 * body_fit,
        0.0,
        1.0,
    )
    return strength, close_distance_ratio, level_contact


def calculate_sticky_level_reversal_strength(
    direction: str,
    latest: SessionData,
    explicit_price: float | None,
    average_day_pct: float,
    context: dict[str, float],
) -> tuple[float, float | None, dict[str, float | bool] | None]:
    if explicit_price is None:
        return 0.0, None, None

    avg_move = max(average_day_pct, 0.01)
    close_distance_pct = abs(latest.close - explicit_price) / max(latest.close, 0.01) * 100.0
    close_distance_ratio = close_distance_pct / avg_move
    level_contact = evaluate_level_contact(direction, latest, explicit_price, avg_move)
    touched = bool(level_contact["touched"])
    close_on_right_side = bool(level_contact["close_on_right_side"])
    penetration_pct = float(level_contact["penetration_pct"])
    penetration_ratio = penetration_pct / avg_move
    range_ratio = context["day_range_pct"] / avg_move

    if not touched or close_on_right_side:
        return 0.0, close_distance_ratio, level_contact
    if close_distance_ratio > 0.05:
        return 0.0, close_distance_ratio, level_contact
    if penetration_ratio < 0.20 or penetration_ratio > 0.45:
        return 0.0, close_distance_ratio, level_contact
    if range_ratio < 0.75 or range_ratio > 1.60:
        return 0.0, close_distance_ratio, level_contact
    if context["aligned_ret3"] > avg_move * 0.05:
        return 0.0, close_distance_ratio, level_contact

    proximity_fit = clamp(1.0 - close_distance_ratio / 0.05, 0.0, 1.0)
    penetration_fit = clamp(1.0 - abs(penetration_ratio - 0.32) / 0.13, 0.0, 1.0)
    range_fit = clamp(1.0 - abs(range_ratio - 1.05) / 0.55, 0.0, 1.0)
    ret3_fit = clamp((-context["aligned_ret3"] / avg_move + 0.05) / 0.45, 0.0, 1.0)
    body_fit = clamp((-context["aligned_body"] / avg_move + 0.05) / 0.70, 0.0, 1.0)

    strength = clamp(
        0.26 * proximity_fit
        + 0.24 * penetration_fit
        + 0.18 * range_fit
        + 0.16 * ret3_fit
        + 0.16 * body_fit,
        0.0,
        1.0,
    )
    return strength, close_distance_ratio, level_contact


def calculate_deep_reclaim_reversal_strength(
    direction: str,
    latest: SessionData,
    explicit_price: float | None,
    average_day_pct: float,
    context: dict[str, float],
) -> tuple[float, float | None, dict[str, float | bool] | None]:
    if explicit_price is None:
        return 0.0, None, None

    avg_move = max(average_day_pct, 0.01)
    close_distance_pct = abs(latest.close - explicit_price) / max(latest.close, 0.01) * 100.0
    close_distance_ratio = close_distance_pct / avg_move
    level_contact = evaluate_level_contact(direction, latest, explicit_price, avg_move)
    touched = bool(level_contact["touched"])
    close_on_right_side = bool(level_contact["close_on_right_side"])
    penetration_pct = float(level_contact["penetration_pct"])
    escape_pct = float(level_contact["escape_pct"])
    penetration_ratio = penetration_pct / avg_move
    escape_ratio = escape_pct / avg_move
    range_ratio = context["day_range_pct"] / avg_move

    if not touched or not close_on_right_side:
        return 0.0, close_distance_ratio, level_contact
    if close_distance_ratio > 0.08:
        return 0.0, close_distance_ratio, level_contact
    if penetration_ratio < 1.10 or penetration_ratio > 3.20:
        return 0.0, close_distance_ratio, level_contact
    if escape_ratio > 0.10:
        return 0.0, close_distance_ratio, level_contact
    if range_ratio < 1.80 or range_ratio > 4.20:
        return 0.0, close_distance_ratio, level_contact
    if context["aligned_body"] < avg_move * 0.45:
        return 0.0, close_distance_ratio, level_contact

    proximity_fit = clamp(1.0 - close_distance_ratio / 0.08, 0.0, 1.0)
    penetration_fit = clamp(1.0 - abs(penetration_ratio - 1.80) / 1.00, 0.0, 1.0)
    reclaim_fit = clamp(1.0 - escape_ratio / 0.10, 0.0, 1.0)
    range_fit = clamp(1.0 - abs(range_ratio - 2.60) / 1.40, 0.0, 1.0)
    body_fit = clamp((context["aligned_body"] / avg_move - 0.45) / 1.20, 0.0, 1.0)

    strength = clamp(
        0.24 * proximity_fit
        + 0.22 * penetration_fit
        + 0.20 * reclaim_fit
        + 0.18 * range_fit
        + 0.16 * body_fit,
        0.0,
        1.0,
    )
    return strength, close_distance_ratio, level_contact


def calculate_compressed_level_reversal_strength(
    direction: str,
    latest: SessionData,
    explicit_price: float | None,
    average_day_pct: float,
    context: dict[str, float],
) -> tuple[float, float | None, dict[str, float | bool] | None]:
    if explicit_price is None:
        return 0.0, None, None

    avg_move = max(average_day_pct, 0.01)
    close_distance_pct = abs(latest.close - explicit_price) / max(latest.close, 0.01) * 100.0
    close_distance_ratio = close_distance_pct / avg_move
    level_contact = evaluate_level_contact(direction, latest, explicit_price, avg_move)
    touched = bool(level_contact["touched"])
    close_on_right_side = bool(level_contact["close_on_right_side"])
    penetration_pct = float(level_contact["penetration_pct"])
    escape_pct = float(level_contact["escape_pct"])
    penetration_ratio = penetration_pct / avg_move
    escape_ratio = escape_pct / avg_move
    range_ratio = context["day_range_pct"] / avg_move
    body_ratio = context["aligned_body"] / avg_move

    if not touched or not close_on_right_side:
        return 0.0, close_distance_ratio, level_contact
    if close_distance_ratio > 0.14:
        return 0.0, close_distance_ratio, level_contact
    if penetration_ratio < 0.10 or penetration_ratio > 0.35:
        return 0.0, close_distance_ratio, level_contact
    if escape_ratio < 0.05 or escape_ratio > 0.20:
        return 0.0, close_distance_ratio, level_contact
    if range_ratio < 0.38 or range_ratio > 0.72:
        return 0.0, close_distance_ratio, level_contact
    if context["aligned_ret3"] > -avg_move * 0.55:
        return 0.0, close_distance_ratio, level_contact
    if abs(body_ratio) > 0.22:
        return 0.0, close_distance_ratio, level_contact

    proximity_fit = clamp(1.0 - close_distance_ratio / 0.14, 0.0, 1.0)
    penetration_fit = clamp(1.0 - abs(penetration_ratio - 0.22) / 0.13, 0.0, 1.0)
    escape_fit = clamp(1.0 - abs(escape_ratio - 0.12) / 0.08, 0.0, 1.0)
    range_fit = clamp(1.0 - abs(range_ratio - 0.55) / 0.17, 0.0, 1.0)
    ret3_fit = clamp((-context["aligned_ret3"] / avg_move - 0.55) / 0.65, 0.0, 1.0)
    body_balance_fit = clamp(1.0 - abs(body_ratio) / 0.22, 0.0, 1.0)

    strength = clamp(
        0.22 * proximity_fit
        + 0.20 * penetration_fit
        + 0.18 * escape_fit
        + 0.16 * range_fit
        + 0.12 * ret3_fit
        + 0.12 * body_balance_fit,
        0.0,
        1.0,
    )
    return strength, close_distance_ratio, level_contact


def classify_regime(
    idea: WatchIdea,
    direction: str,
    sessions: list[SessionData],
    average_day_pct: float,
    post_impulse: PostImpulseFeatures,
    anchor: AnchorInfo | None,
    indicator_snapshot: IndicatorSnapshot | None = None,
) -> RegimeAssessment:
    avg_move = max(average_day_pct, 0.01)
    latest = sessions[-1]
    context = build_directional_context(direction, sessions[-5:], avg_move)
    indicators = indicator_snapshot or calculate_indicator_snapshot(sessions)
    counter_impulse_strength = (
        max(0.0, -context["aligned_ret3"] / max(avg_move * 1.05, 0.01))
        + max(0.0, -context["aligned_body"] / max(avg_move * 0.70, 0.01))
        + max(0.0, context["day_range_pct"] / max(avg_move, 0.01) - 1.0) * 0.35
    )

    close_distance_ratio: float | None = None
    touched = False
    on_right_side = False
    level_contact: dict[str, float | bool] | None = None
    penetration_pct = 0.0
    if anchor is not None:
        close_distance_pct = abs(latest.close - anchor.price) / max(latest.close, 0.01) * 100.0
        close_distance_ratio = close_distance_pct / avg_move
        level_contact = evaluate_level_contact(direction, latest, anchor.price, avg_move)
        touched = bool(level_contact["touched"])
        on_right_side = bool(level_contact["close_on_right_side"])
        penetration_pct = float(level_contact["penetration_pct"])

    day_range = max(latest.high - latest.low, 0.001)
    if direction == "long":
        extreme_close = clamp((latest.high - latest.close) / day_range, 0.0, 1.0)
        rsi_reversion_fit = clamp((46.0 - indicators.rsi5) / 18.0, 0.0, 1.0)
        ema_reversion_fit = clamp((-indicators.ema10_gap_pct) / max(avg_move * 0.45, 0.01), 0.0, 1.0)
    else:
        extreme_close = clamp((latest.close - latest.low) / day_range, 0.0, 1.0)
        rsi_reversion_fit = clamp((indicators.rsi5 - 54.0) / 18.0, 0.0, 1.0)
        ema_reversion_fit = clamp(indicators.ema10_gap_pct / max(avg_move * 0.45, 0.01), 0.0, 1.0)
    atr_reversion_fit = clamp((indicators.atr5_pct / avg_move - 0.55) / 0.95, 0.0, 1.0)
    broad_mean_reversion_signal = clamp(
        0.45 * clamp(-context["aligned_ret3"] / max(avg_move * 1.15, 0.01), 0.0, 1.0)
        + 0.25 * rsi_reversion_fit
        + 0.20 * ema_reversion_fit
        + 0.10 * atr_reversion_fit,
        0.0,
        1.0,
    )

    absorption_strength = calculate_absorption_reversal_strength(
        average_day_pct=avg_move,
        context=context,
        close_distance_ratio=close_distance_ratio,
        anchor=anchor,
        level_contact=level_contact,
    )
    if absorption_strength > 0.0:
        confidence = clamp(
            0.48
            + 0.26 * absorption_strength
            + 0.10 * (anchor.confidence if anchor is not None else 0.40)
            + 0.08 * (1.0 - close_distance_ratio / 0.05 if close_distance_ratio is not None else 0.0),
            0.40,
            0.92,
        )
        return RegimeAssessment(regime="absorption_reversal", confidence=confidence, anchor=anchor)

    explicit_price = idea.support if direction == "long" else idea.resistance
    explicit_distance_ratio: float | None = None
    explicit_contact: dict[str, float | bool] | None = None
    explicit_penetration_ratio = 0.0
    if explicit_price is not None:
        explicit_close_distance_pct = abs(latest.close - explicit_price) / max(latest.close, 0.01) * 100.0
        explicit_distance_ratio = explicit_close_distance_pct / avg_move
        explicit_contact = evaluate_level_contact(direction, latest, explicit_price, avg_move)
        explicit_penetration_ratio = float(explicit_contact["penetration_pct"]) / avg_move

    deep_reclaim_strength, deep_reclaim_distance_ratio, deep_reclaim_contact = calculate_deep_reclaim_reversal_strength(
        direction=direction,
        latest=latest,
        explicit_price=explicit_price,
        average_day_pct=avg_move,
        context=context,
    )
    if (
        post_impulse.state == "reclaim_after_failure"
        and deep_reclaim_strength > 0.0
        and explicit_price is not None
    ):
        confidence = clamp(
            0.52
            + 0.22 * deep_reclaim_strength
            + 0.10 * (1.0 - min((deep_reclaim_distance_ratio or 0.08) / 0.08, 1.0))
            + 0.08 * clamp(float(deep_reclaim_contact["penetration_pct"]) / max(avg_move * 2.20, 0.01), 0.0, 1.0),
            0.45,
            0.94,
        )
        explicit_anchor = AnchorInfo(price=explicit_price, kind="explicit_level", confidence=0.95, explicit=True)
        return RegimeAssessment(regime="deep_reclaim_reversal", confidence=confidence, anchor=explicit_anchor)

    compressed_level_strength, compressed_distance_ratio, compressed_contact = calculate_compressed_level_reversal_strength(
        direction=direction,
        latest=latest,
        explicit_price=explicit_price,
        average_day_pct=avg_move,
        context=context,
    )
    if compressed_level_strength > 0.0 and explicit_price is not None:
        confidence = clamp(
            0.44
            + 0.24 * compressed_level_strength
            + 0.14 * (1.0 - min((compressed_distance_ratio or 0.14) / 0.14, 1.0))
            + 0.08 * clamp(float(compressed_contact["escape_pct"]) / max(avg_move * 0.18, 0.01), 0.0, 1.0),
            0.38,
            0.92,
        )
        explicit_anchor = AnchorInfo(price=explicit_price, kind="explicit_level", confidence=0.95, explicit=True)
        return RegimeAssessment(regime="compressed_level_reversal", confidence=confidence, anchor=explicit_anchor)

    sticky_level_strength, sticky_distance_ratio, sticky_contact = calculate_sticky_level_reversal_strength(
        direction=direction,
        latest=latest,
        explicit_price=explicit_price,
        average_day_pct=avg_move,
        context=context,
    )
    if sticky_level_strength > 0.0 and explicit_price is not None:
        confidence = clamp(
            0.40
            + 0.22 * sticky_level_strength
            + 0.14 * (1.0 - min((sticky_distance_ratio or 0.05) / 0.05, 1.0))
            + 0.08 * clamp(float(sticky_contact["penetration_pct"]) / max(avg_move * 0.45, 0.01), 0.0, 1.0),
            0.34,
            0.88,
        )
        explicit_anchor = AnchorInfo(price=explicit_price, kind="explicit_level", confidence=0.95, explicit=True)
        return RegimeAssessment(regime="sticky_level_reversal", confidence=confidence, anchor=explicit_anchor)

    micro_overshoot_strength, micro_distance_ratio, micro_contact = calculate_micro_overshoot_reversal_strength(
        direction=direction,
        latest=latest,
        explicit_price=explicit_price,
        average_day_pct=avg_move,
        context=context,
    )
    if micro_overshoot_strength > 0.0 and explicit_price is not None:
        confidence = clamp(
            0.42
            + 0.24 * micro_overshoot_strength
            + 0.12 * (1.0 - min((micro_distance_ratio or 0.12) / 0.12, 1.0))
            + 0.08 * clamp(float(micro_contact["penetration_pct"]) / max(avg_move * 0.16, 0.01), 0.0, 1.0),
            0.36,
            0.90,
        )
        explicit_anchor = AnchorInfo(price=explicit_price, kind="explicit_level", confidence=0.95, explicit=True)
        return RegimeAssessment(regime="micro_overshoot_reversal", confidence=confidence, anchor=explicit_anchor)

    overshoot_strength, explicit_distance_ratio, explicit_contact = calculate_overshoot_reversal_strength(
        direction=direction,
        latest=latest,
        explicit_price=explicit_price,
        average_day_pct=avg_move,
        context=context,
    )
    if overshoot_strength > 0.0 and explicit_price is not None:
        penetration_fit = clamp(
            float(explicit_contact["penetration_pct"]) / max(avg_move * 1.10, 0.01),
            0.0,
            1.0,
        )
        confidence = clamp(
            0.44
            + 0.26 * overshoot_strength
            + 0.12 * (1.0 - min((explicit_distance_ratio or 0.20) / 0.20, 1.0))
            + 0.08 * penetration_fit,
            0.38,
            0.92,
        )
        explicit_anchor = AnchorInfo(price=explicit_price, kind="explicit_level", confidence=0.95, explicit=True)
        return RegimeAssessment(regime="overshoot_reversal", confidence=confidence, anchor=explicit_anchor)

    extended_overshoot_strength, extended_distance_ratio, extended_contact = calculate_extended_overshoot_reversal_strength(
        direction=direction,
        latest=latest,
        explicit_price=explicit_price,
        average_day_pct=avg_move,
        context=context,
    )
    if extended_overshoot_strength > 0.0 and explicit_price is not None:
        penetration_fit = clamp(
            float(extended_contact["penetration_pct"]) / max(avg_move * 0.85, 0.01),
            0.0,
            1.0,
        )
        confidence = clamp(
            0.44
            + 0.24 * extended_overshoot_strength
            + 0.12 * (1.0 - min(abs((extended_distance_ratio or 0.40) - 0.40) / 0.20, 1.0))
            + 0.08 * penetration_fit,
            0.38,
            0.92,
        )
        explicit_anchor = AnchorInfo(price=explicit_price, kind="explicit_level", confidence=0.95, explicit=True)
        return RegimeAssessment(regime="extended_overshoot_reversal", confidence=confidence, anchor=explicit_anchor)

    counter_impulse_ready = (
        anchor is not None
        and close_distance_ratio is not None
        and close_distance_ratio <= 1.85
        and touched
        and on_right_side
        and not (
            anchor is not None
            and anchor.explicit
            and post_impulse.state == "neutral"
            and penetration_pct < avg_move * 0.08
            and close_distance_ratio > 0.25
            and context["day_range_pct"] < avg_move * 1.70
        )
        and not (
            anchor is not None
            and not anchor.explicit
            and explicit_contact is not None
            and bool(explicit_contact["touched"])
            and not bool(explicit_contact["close_on_right_side"])
            and post_impulse.state == "neutral"
            and explicit_distance_ratio is not None
            and explicit_distance_ratio <= 0.90
            and explicit_penetration_ratio >= 0.30
        )
        and context["aligned_ret3"] < -avg_move * 0.75
        and context["aligned_body"] < -avg_move * 0.22
        and context["day_range_pct"] > avg_move * 0.90
    )
    broad_mean_reversion_override = (
        broad_mean_reversion_signal >= 0.80
        and close_distance_ratio is not None
        and close_distance_ratio >= 0.52
        and penetration_pct < avg_move * 0.18
        and post_impulse.state == "neutral"
    )
    if counter_impulse_ready:
        if broad_mean_reversion_override:
            confidence = clamp(
                0.44
                + 0.20 * broad_mean_reversion_signal
                + 0.12 * atr_reversion_fit
                + 0.10 * (1.0 - min(close_distance_ratio / 1.20, 1.0)),
                0.34,
                0.88,
            )
            return RegimeAssessment(regime="mean_reversion", confidence=confidence, anchor=anchor)
        confidence = clamp(
            0.42
            + 0.16 * clamp(counter_impulse_strength / 2.40, 0.0, 1.0)
            + 0.12 * anchor.confidence
            + 0.12 * (1.0 - close_distance_ratio / 1.85)
            + 0.06 * (1.0 if anchor.explicit else 0.0),
            0.35,
            0.95,
        )
        return RegimeAssessment(regime="counter_impulse_reversal", confidence=confidence, anchor=anchor)

    capitulation_strength = calculate_capitulation_reversal_strength(
        average_day_pct=avg_move,
        context=context,
        close_distance_ratio=close_distance_ratio,
        anchor=anchor,
        level_contact=level_contact,
    )
    if capitulation_strength > 0.0:
        confidence = clamp(
            0.48
            + 0.28 * capitulation_strength
            + 0.10 * (anchor.confidence if anchor is not None else 0.40)
            + 0.08 * (1.0 if anchor is not None and anchor.explicit else 0.0),
            0.35,
            0.95,
        )
        return RegimeAssessment(regime="capitulation_reversal", confidence=confidence, anchor=anchor)

    pre_level_strength = calculate_pre_level_rejection_strength(
        average_day_pct=avg_move,
        context=context,
        close_distance_ratio=close_distance_ratio,
        anchor=anchor,
        level_contact=level_contact,
    )
    if pre_level_strength > 0.0:
        confidence = clamp(
            0.40
            + 0.24 * pre_level_strength
            + 0.14 * (anchor.confidence if anchor is not None else 0.40)
            + 0.10 * (1.0 - close_distance_ratio / 0.45 if close_distance_ratio is not None else 0.0),
            0.34,
            0.90,
        )
        return RegimeAssessment(regime="pre_level_rejection", confidence=confidence, anchor=anchor)

    fragile_breakout_ready = (
        anchor is not None
        and anchor.explicit
        and close_distance_ratio is not None
        and touched
        and not on_right_side
        and 0.08 <= penetration_pct / avg_move <= 0.32
        and extreme_close >= 0.92
        and context["aligned_ret3"] < -avg_move * 0.35
        and context["day_range_pct"] <= avg_move * 0.55
    )
    if fragile_breakout_ready:
        penetration_fit = clamp((penetration_pct / avg_move - 0.08) / 0.24, 0.0, 1.0)
        range_fit = clamp(1.0 - context["day_range_pct"] / max(avg_move * 0.55, 0.01), 0.0, 1.0)
        confidence = clamp(
            0.46
            + 0.14 * anchor.confidence
            + 0.14 * penetration_fit
            + 0.12 * extreme_close
            + 0.10 * range_fit
            + 0.08 * clamp(-context["aligned_ret3"] / avg_move - 0.35, 0.0, 1.0),
            0.40,
            0.92,
        )
        return RegimeAssessment(regime="fragile_breakout_reversal", confidence=confidence, anchor=anchor)

    if (
        post_impulse.state == "reclaim_after_failure"
        and anchor is not None
        and close_distance_ratio is not None
        and close_distance_ratio <= 1.55
        and on_right_side
        and context["day_range_pct"] > avg_move * 0.85
    ):
        confidence = clamp(
            0.42
            + 0.20 * anchor.confidence
            + 0.18 * (1.0 - close_distance_ratio / 1.55)
            + 0.12 * post_impulse.continuation_prob
            + 0.08 * (1.0 - post_impulse.exhaustion_prob),
            0.35,
            0.92,
        )
        return RegimeAssessment(regime="counter_impulse_reversal", confidence=confidence, anchor=anchor)

    directional_reaction = (
        context["aligned_body"] > avg_move * 0.08
        or context["breakout_margin_pct"] > 0.0
        or post_impulse.state in {"pause", "trend_continuation"}
    )
    if (
        anchor is not None
        and close_distance_ratio is not None
        and close_distance_ratio <= 1.40
        and on_right_side
        and (touched or post_impulse.state in {"pause", "trend_continuation"})
        and directional_reaction
    ):
        confidence = clamp(
            0.38
            + 0.22 * anchor.confidence
            + 0.20 * (1.0 - close_distance_ratio / 1.40)
            + 0.16 * (1.0 if touched else 0.0)
            + 0.08 * (1.0 if post_impulse.state in {"pause", "trend_continuation"} else 0.0),
            0.25,
            0.95,
        )
        return RegimeAssessment(regime="level_rejection", confidence=confidence, anchor=anchor)

    if direction == "short" and post_impulse.state in {"pause", "trend_continuation"} and post_impulse.impulse_strength >= 1.20:
        confidence = clamp(
            0.40
            + 0.30 * post_impulse.continuation_prob
            + 0.20 * (1.0 - min(post_impulse.retrace_ratio, 1.0))
            + 0.10 * (anchor.confidence if anchor is not None else 0.40),
            0.30,
            0.95,
        )
        return RegimeAssessment(regime="post_breakdown_continuation", confidence=confidence, anchor=anchor)

    if direction == "long" and post_impulse.state in {"pause", "trend_continuation"} and post_impulse.impulse_strength >= 1.20:
        confidence = clamp(
            0.40
            + 0.30 * post_impulse.continuation_prob
            + 0.20 * (1.0 - min(post_impulse.retrace_ratio, 1.0))
            + 0.10 * (anchor.confidence if anchor is not None else 0.40),
            0.30,
            0.95,
        )
        return RegimeAssessment(regime="post_breakout_continuation", confidence=confidence, anchor=anchor)

    if context["aligned_ret3"] < -avg_move * 0.25:
        confidence = clamp(0.46 + min(-context["aligned_ret3"] / (avg_move * 2.2), 0.35), 0.25, 0.86)
        return RegimeAssessment(regime="mean_reversion", confidence=confidence, anchor=anchor)

    confidence = clamp(
        0.32
        + 0.08 * (1.0 if context["breakout_margin_pct"] > 0.0 else 0.0)
        + 0.06 * (1.0 if post_impulse.state in {"pause", "trend_continuation"} else 0.0),
        0.22,
        0.60,
    )
    return RegimeAssessment(regime="neutral_context", confidence=confidence, anchor=anchor)


def get_regime_profile(regime_name: str) -> dict[str, float]:
    return {
        "level_rejection": {
            "entry_bonus": 0.02,
            "followthrough_bonus": 0.00,
            "quality": 0.48,
        },
        "mean_reversion": {
            "entry_bonus": 0.04,
            "followthrough_bonus": 0.05,
            "quality": 0.58,
        },
        "trend_continuation": {
            "entry_bonus": 0.04,
            "followthrough_bonus": 0.10,
            "quality": 0.64,
        },
        "post_breakdown_continuation": {
            "entry_bonus": 0.06,
            "followthrough_bonus": 0.16,
            "quality": 0.76,
        },
        "post_breakout_continuation": {
            "entry_bonus": 0.06,
            "followthrough_bonus": 0.16,
            "quality": 0.76,
        },
        "counter_impulse_reversal": {
            "entry_bonus": 0.08,
            "followthrough_bonus": 0.14,
            "quality": 0.72,
        },
        "compressed_level_reversal": {
            "entry_bonus": 0.08,
            "followthrough_bonus": 0.18,
            "quality": 0.80,
        },
        "deep_reclaim_reversal": {
            "entry_bonus": 0.09,
            "followthrough_bonus": 0.22,
            "quality": 0.86,
        },
        "absorption_reversal": {
            "entry_bonus": 0.08,
            "followthrough_bonus": 0.15,
            "quality": 0.80,
        },
        "pre_level_rejection": {
            "entry_bonus": 0.05,
            "followthrough_bonus": 0.08,
            "quality": 0.62,
        },
        "sticky_level_reversal": {
            "entry_bonus": 0.10,
            "followthrough_bonus": 0.24,
            "quality": 0.88,
        },
        "micro_overshoot_reversal": {
            "entry_bonus": 0.08,
            "followthrough_bonus": 0.18,
            "quality": 0.80,
        },
        "extended_overshoot_reversal": {
            "entry_bonus": 0.08,
            "followthrough_bonus": 0.18,
            "quality": 0.80,
        },
        "overshoot_reversal": {
            "entry_bonus": 0.07,
            "followthrough_bonus": 0.14,
            "quality": 0.74,
        },
        "fragile_breakout_reversal": {
            "entry_bonus": 0.10,
            "followthrough_bonus": 0.22,
            "quality": 0.88,
        },
        "capitulation_reversal": {
            "entry_bonus": 0.08,
            "followthrough_bonus": 0.18,
            "quality": 0.84,
        },
        "neutral_context": {
            "entry_bonus": 0.00,
            "followthrough_bonus": 0.02,
            "quality": 0.40,
        },
    }.get(
        regime_name,
        {
            "entry_bonus": 0.03,
            "followthrough_bonus": 0.04,
            "quality": 0.52,
        },
    )


def estimate_target_probabilities(
    idea: WatchIdea,
    previous_close: float,
    market_score: float,
    setup_score: float,
    instrument_score: float,
    history_score: float,
    ladder_score: float,
    regime: RegimeAssessment,
    post_impulse: PostImpulseFeatures,
    selected_anchor: AnchorInfo | None = None,
) -> dict[str, float]:
    direction = infer_direction(previous_close, idea.targets, idea.declared_direction)
    selected_anchor_confirms = (
        selected_anchor is not None
        and is_price_on_expected_side(direction, previous_close, selected_anchor.price)
    )
    execution_score = combine_execution_score(setup_score, instrument_score) / 100.0
    history_fit = clamp(history_score / 100.0, 0.0, 1.0)
    market_fit = clamp(market_score / 100.0, 0.0, 1.0)
    ladder_fit = clamp(ladder_score / 100.0, 0.0, 1.0)
    regime_fit = clamp(regime.confidence, 0.0, 1.0)
    anchor_fit = clamp(regime.anchor.confidence if regime.anchor is not None else 0.35, 0.0, 1.0)
    profile = get_regime_profile(regime.regime)
    preconfirmation_discount = calculate_preconfirmation_reversal_discount(
        direction=direction,
        previous_close=previous_close,
        average_day_pct=idea.average_day_pct,
        regime=regime,
        post_impulse=post_impulse,
        selected_anchor=selected_anchor,
    )
    if preconfirmation_discount >= 0.18:
        profile = {
            "entry_bonus": min(profile["entry_bonus"], 0.03),
            "followthrough_bonus": min(profile["followthrough_bonus"], 0.06),
            "quality": min(profile["quality"], 0.54),
        }
    deep_reclaim_state = regime.regime in RECLAIM_REVERSAL_REGIMES and post_impulse.state == "reclaim_after_failure"
    effective_continuation_prob = post_impulse.continuation_prob
    effective_exhaustion_prob = post_impulse.exhaustion_prob
    effective_breakout_proximity = post_impulse.breakout_proximity
    effective_state = post_impulse.state
    if (
        regime.regime in PRECONFIRMATION_REVERSAL_REGIMES
        and post_impulse.state in {"failed_breakdown", "failed_breakout"}
        and selected_anchor_confirms
    ):
        effective_state = "neutral"
        effective_continuation_prob = max(
            effective_continuation_prob,
            0.46 + 0.20 * anchor_fit + 0.10 * history_fit,
        )
        effective_exhaustion_prob = clamp(
            min(effective_exhaustion_prob, 0.30 - 0.06 * history_fit),
            0.12,
            0.40,
        )
        effective_breakout_proximity = max(effective_breakout_proximity, 0.34 * anchor_fit)
    if deep_reclaim_state:
        effective_state = "neutral"
        effective_continuation_prob = max(effective_continuation_prob, 0.62 + 0.18 * anchor_fit + 0.10 * execution_score)
        effective_exhaustion_prob = clamp(min(effective_exhaustion_prob, 0.26 - 0.06 * history_fit), 0.10, 0.34)
        effective_breakout_proximity = max(effective_breakout_proximity, 0.42 * anchor_fit)

    ordered_targets = sorted(idea.targets, key=lambda item: item.index)
    if not ordered_targets:
        return {}

    max_index = max(target.index for target in ordered_targets)
    probabilities: dict[str, float] = {}
    previous_probability = 0.0
    previous_target_fit = 0.0

    for target in ordered_targets:
        move_pct = abs((target.price / previous_close - 1.0) * 100.0)
        _, fit_score = target_score(move_pct, idea.average_day_pct or 0.01)
        target_fit = clamp(fit_score / 100.0, 0.0, 1.0)
        depth_ratio = (target.index - 1) / max(max_index - 1, 1) if max_index > 1 else 0.0

        if target.index == 1:
            probability = clamp(
                0.06
                + 0.20 * execution_score
                + 0.36 * history_fit
                + 0.05 * market_fit
                + 0.07 * target_fit
                + 0.05 * ladder_fit
                + 0.05 * anchor_fit
                + 0.04 * regime_fit * profile["quality"]
                + profile["entry_bonus"]
                + 0.03 * effective_continuation_prob
                - 0.10 * effective_exhaustion_prob,
                0.03,
                0.96,
            )
            if regime.regime == "capitulation_reversal":
                probability += 0.05 * history_fit + 0.04 * anchor_fit
            elif regime.regime == "absorption_reversal":
                probability += 0.05 * anchor_fit + 0.04 * history_fit
            elif regime.regime == "compressed_level_reversal":
                probability += 0.04 * anchor_fit + 0.04 * execution_score
            elif regime.regime == "extended_overshoot_reversal":
                probability += 0.04 * anchor_fit + 0.04 * history_fit
            elif regime.regime == "pre_level_rejection":
                probability += 0.03 * anchor_fit + 0.02 * execution_score
            elif regime.regime in PRECONFIRMATION_REVERSAL_REGIMES:
                probability += 0.04 * anchor_fit + 0.03 * history_fit
            elif regime.regime in RECLAIM_REVERSAL_REGIMES:
                probability += 0.05 * anchor_fit + 0.03 * execution_score
            if deep_reclaim_state:
                probability += 0.04 * anchor_fit + 0.02 * history_fit
            if effective_state == "reclaim_after_failure":
                probability += 0.04 * execution_score + 0.03 * anchor_fit
        else:
            continuation_step = (
                0.16
                + 0.28 * history_fit
                + 0.14 * execution_score
                + 0.12 * effective_continuation_prob
                + 0.10 * target_fit
                + 0.08 * ladder_fit
                + 0.05 * regime_fit * profile["quality"]
                + profile["followthrough_bonus"]
                + 0.04 * effective_breakout_proximity
                - 0.14 * effective_exhaustion_prob
                - 0.08 * clamp(post_impulse.retrace_ratio - 0.35, 0.0, 1.2)
                - 0.07 * depth_ratio
            )
            if regime.regime == "counter_impulse_reversal":
                continuation_step += 0.06 * history_fit
            elif regime.regime == "capitulation_reversal":
                continuation_step += 0.08 * history_fit + 0.05 * anchor_fit
            elif regime.regime == "absorption_reversal":
                continuation_step += 0.06 * anchor_fit + 0.06 * history_fit
            elif regime.regime == "compressed_level_reversal":
                continuation_step += 0.07 * anchor_fit + 0.05 * execution_score
            elif regime.regime == "extended_overshoot_reversal":
                continuation_step += 0.07 * anchor_fit + 0.05 * history_fit
            elif regime.regime == "pre_level_rejection":
                continuation_step += 0.05 * anchor_fit + 0.04 * execution_score
            elif regime.regime in PRECONFIRMATION_REVERSAL_REGIMES:
                continuation_step += 0.06 * anchor_fit + 0.06 * history_fit
            elif regime.regime in RECLAIM_REVERSAL_REGIMES:
                continuation_step += 0.08 * anchor_fit + 0.06 * execution_score
            if deep_reclaim_state:
                continuation_step += 0.06 * anchor_fit + 0.04 * history_fit
            if effective_state == "pause":
                continuation_step += 0.05
            elif effective_state == "trend_continuation":
                continuation_step += 0.04
            elif effective_state == "reclaim_after_failure":
                continuation_step += 0.10 * execution_score + 0.06 * anchor_fit
            elif effective_state == "exhaustion":
                continuation_step -= 0.08
            elif effective_state in {"failed_breakdown", "failed_breakout"}:
                continuation_step -= 0.18

            target_fit_drop = max(previous_target_fit - target_fit, 0.0)
            continuation_step -= 0.06 * target_fit_drop
            probability = previous_probability * clamp(continuation_step, 0.05, 0.92)

        if preconfirmation_discount > 0.0:
            depth_penalty = preconfirmation_discount * (0.58 + 0.32 * depth_ratio)
            probability *= clamp(1.0 - depth_penalty, 0.08, 0.92)
        probability = clamp(probability, 0.02, 0.98)
        label = f"T{target.index}"
        probabilities[label] = probability
        previous_probability = probability
        previous_target_fit = target_fit

    return probabilities


def calculate_expected_utility(
    idea: WatchIdea,
    previous_close: float,
    market_score: float,
    setup_score: float,
    instrument_score: float,
    history_score: float,
    ladder_score: float,
    regime: RegimeAssessment,
    post_impulse: PostImpulseFeatures,
    selected_anchor: AnchorInfo | None = None,
) -> ExpectedUtility:
    direction = infer_direction(previous_close, idea.targets, idea.declared_direction)
    profile = get_regime_profile(regime.regime)
    preconfirmation_discount = calculate_preconfirmation_reversal_discount(
        direction=direction,
        previous_close=previous_close,
        average_day_pct=idea.average_day_pct,
        regime=regime,
        post_impulse=post_impulse,
        selected_anchor=selected_anchor,
    )
    if preconfirmation_discount >= 0.18:
        profile = {
            "entry_bonus": min(profile["entry_bonus"], 0.03),
            "followthrough_bonus": min(profile["followthrough_bonus"], 0.06),
            "quality": min(profile["quality"], 0.54),
        }
    probabilities = estimate_target_probabilities(
        idea=idea,
        previous_close=previous_close,
        market_score=market_score,
        setup_score=setup_score,
        instrument_score=instrument_score,
        history_score=history_score,
        ladder_score=ladder_score,
        regime=regime,
        post_impulse=post_impulse,
        selected_anchor=selected_anchor,
    )

    if not probabilities:
        return ExpectedUtility(
            score=0.0,
            prob_hit_t1=0.0,
            prob_hit_t3=0.0,
            expected_max_target=0.0,
            risk_complete_miss=1.0,
            target_probabilities={},
        )

    ordered_labels = sorted(probabilities.keys(), key=lambda label: int(label[1:]))
    max_index = max(int(label[1:]) for label in ordered_labels)
    prob_hit_t1 = probabilities.get("T1", probabilities[ordered_labels[0]])
    prob_hit_t2 = probabilities.get("T2", prob_hit_t1)
    prob_hit_t3 = probabilities.get("T3", probabilities[ordered_labels[-1]])
    prob_hit_t4 = probabilities.get("T4", probabilities[ordered_labels[-1]])
    expected_max_target = sum(probabilities[label] for label in ordered_labels)
    risk_complete_miss = clamp(1.0 - prob_hit_t1, 0.0, 1.0)
    market_fit = clamp(market_score / 100.0, 0.0, 1.0)
    history_fit = clamp(history_score / 100.0, 0.0, 1.0)

    # Expected value (% return) net of the stop, maximized over which target we aim at.
    best_ev = -math.inf
    best_ev_label: str | None = None
    for target in idea.targets:
        label = f"T{target.index}"
        reward_pct = abs((target.price / previous_close - 1.0) * 100.0)
        ev = target_expected_value(probabilities.get(label, 0.0), reward_pct)
        if ev > best_ev:
            best_ev = ev
            best_ev_label = label
    expected_value_pct = best_ev if best_ev > -math.inf else 0.0

    score = 100.0 * clamp(
        0.34 * prob_hit_t1
        + 0.27 * prob_hit_t2
        + 0.13 * prob_hit_t3
        + 0.04 * prob_hit_t4
        + 0.08 * (expected_max_target / max(max_index, 1))
        + 0.06 * profile["quality"] * clamp(regime.confidence, 0.0, 1.0)
        + 0.04 * market_fit
        + 0.04 * history_fit
        - 0.10 * post_impulse.exhaustion_prob
        - 0.05 * risk_complete_miss,
        0.0,
        1.0,
    )
    if preconfirmation_discount > 0.0:
        score *= clamp(1.0 - 0.72 * preconfirmation_discount, 0.25, 0.90)

    if _ev_objective_enabled():
        # Rank by expected value: map EV% onto the 0..100 score scale.
        score = clamp(50.0 + EV_SCORE_SCALE * expected_value_pct, 0.0, 100.0)

    return ExpectedUtility(
        score=score,
        prob_hit_t1=prob_hit_t1,
        prob_hit_t3=prob_hit_t3,
        expected_max_target=expected_max_target,
        risk_complete_miss=risk_complete_miss,
        target_probabilities=probabilities,
        expected_value_pct=expected_value_pct,
        best_ev_label=best_ev_label,
    )


def infer_direction(close_price: float, targets: Iterable[Target], declared_direction: str | None = None) -> str:
    if declared_direction in {"long", "short"}:
        return declared_direction

    above = sum(1 for target in targets if target.price > close_price)
    below = sum(1 for target in targets if target.price < close_price)
    if above > below:
        return "long"
    if below > above:
        return "short"
    return "neutral"


def target_score(move_pct: float, average_day_pct: float) -> tuple[float, float]:
    ratio = move_pct / max(average_day_pct, 0.01)
    score = math.exp(-((ratio - TARGET_RATIO_SWEET_SPOT) ** 2) / (2 * TARGET_RATIO_SIGMA**2)) * 100.0
    if ratio > 1.05:
        score *= max(0.1, 1.0 - (ratio - 1.05) * 2.2)
    if ratio < 0.25:
        score *= 0.7
    return ratio, score


def build_target_choice(target: Target, previous_close: float, average_day_pct: float) -> TargetChoice:
    move_pct = abs((target.price / previous_close - 1.0) * 100.0)
    ratio, score = target_score(move_pct, average_day_pct)
    return TargetChoice(
        label=f"T{target.index}",
        price=target.price,
        move_pct_from_close=move_pct,
        ratio_to_average_day=ratio,
        score=score,
    )


def choose_best_target(idea: WatchIdea, previous_close: float) -> TargetChoice:
    assert idea.average_day_pct is not None

    best_choice: TargetChoice | None = None
    for target in idea.targets:
        choice = build_target_choice(target, previous_close, idea.average_day_pct)
        if best_choice is None or choice.score > best_choice.score:
            best_choice = choice

    assert best_choice is not None
    return best_choice


def choose_best_target_from_probabilities(
    idea: WatchIdea,
    previous_close: float,
    probabilities: dict[str, float],
) -> TargetChoice:
    assert idea.average_day_pct is not None

    best_choice: TargetChoice | None = None
    best_composite = -math.inf
    for target in idea.targets:
        choice = build_target_choice(target, previous_close, idea.average_day_pct)
        probability = clamp(probabilities.get(choice.label, 0.0), 0.0, 1.0)
        one_day_fit = clamp(1.0 - abs(choice.ratio_to_average_day - 0.52) / 0.36, 0.0, 1.0)

        depth_penalty = 0.0
        if choice.ratio_to_average_day > 0.82:
            depth_penalty += clamp((choice.ratio_to_average_day - 0.82) / 0.30, 0.0, 1.0) * 18.0
        elif choice.ratio_to_average_day > 0.62:
            depth_penalty += clamp((choice.ratio_to_average_day - 0.62) / 0.20, 0.0, 1.0) * 6.0

        composite = 100.0 * (0.62 * probability + 0.18 * one_day_fit) + 0.20 * choice.score - depth_penalty
        if composite > best_composite:
            best_choice = choice
            best_composite = composite

    assert best_choice is not None
    return best_choice


def choose_best_target_by_ev(
    idea: WatchIdea,
    previous_close: float,
    probabilities: dict[str, float],
) -> TargetChoice:
    """Pick the target that maximizes expected % return net of the stop.

    Unlike choose_best_target_from_probabilities (which penalizes depth and lands on T2),
    this lets a deeper target win when reward x P(reach) beats the stop cost.
    """
    assert idea.average_day_pct is not None
    best_choice: TargetChoice | None = None
    best_ev = -math.inf
    for target in idea.targets:
        choice = build_target_choice(target, previous_close, idea.average_day_pct)
        probability = probabilities.get(choice.label, 0.0)
        ev = target_expected_value(probability, choice.move_pct_from_close)
        if ev > best_ev:
            best_ev = ev
            best_choice = choice
    if best_choice is None:
        return choose_best_target(idea, previous_close)
    return best_choice


def calculate_ladder_score(idea: WatchIdea, previous_close: float) -> float:
    assert idea.average_day_pct is not None
    ordered_targets = sorted(idea.targets, key=lambda item: item.index)
    if not ordered_targets:
        return 0.0

    total_weight = 0.0
    weighted_target_score = 0.0
    reachable_weight = 0.0
    max_index = max(target.index for target in ordered_targets)
    depth_score = 0.0

    for target in ordered_targets:
        move_pct = abs((target.price / previous_close - 1.0) * 100.0)
        ratio, score = target_score(move_pct, idea.average_day_pct)
        # One-day selection should not over-reward distant ladder depth.
        weight = 1.0 + 0.35 * float(max(target.index - 1, 0))
        total_weight += weight
        weighted_target_score += score * weight
        if ratio <= 1.05:
            reachable_weight += weight
            depth_score = max(depth_score, target.index / max_index * 100.0)

    if total_weight <= 0.0:
        return 0.0

    weighted_average = weighted_target_score / total_weight
    coverage_score = reachable_weight / total_weight * 100.0
    return clamp(0.60 * weighted_average + 0.25 * coverage_score + 0.15 * depth_score, 0.0, 100.0)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _ev_objective_enabled() -> bool:
    """Flag for the expected-value objective.

    Set WBT_EV_OBJECTIVE=1 to rank ideas by expected % return net of the 1% stop
    (reward x P(reach) - (1-P) x stop) and pick the EV-maximizing target, instead of
    the legacy 'probability of touching a near target' objective.
    """
    return os.environ.get("WBT_EV_OBJECTIVE", "").strip().lower() in {"1", "true", "yes", "on"}


def target_expected_value(probability: float, reward_pct: float) -> float:
    """EV in percent for aiming at a single target with a fixed stop.

    Reach the target -> +reward_pct; otherwise assume the stop is taken -> -STOP_LOSS_PCT.
    This deliberately encodes the asymmetry the user cares about: a small target barely
    clears the stop, so depth is only rewarded when reward x P beats the stop cost.
    """
    p = clamp(probability, 0.0, 1.0)
    return p * reward_pct - (1.0 - p) * STOP_LOSS_PCT


def calculate_ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    if period <= 1:
        return values[-1]
    multiplier = 2.0 / (period + 1.0)
    ema = values[0]
    for value in values[1:]:
        ema = ema + multiplier * (value - ema)
    return ema


def calculate_rsi(sessions: list[SessionData], period: int = 5) -> float:
    if len(sessions) < 2:
        return 50.0
    effective_period = min(period, len(sessions) - 1)
    changes = [sessions[index].close - sessions[index - 1].close for index in range(len(sessions) - effective_period, len(sessions))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = sum(gains) / max(effective_period, 1)
    average_loss = sum(losses) / max(effective_period, 1)
    if average_loss <= 1e-9:
        return 100.0 if average_gain > 1e-9 else 50.0
    relative_strength = average_gain / average_loss
    return clamp(100.0 - 100.0 / (1.0 + relative_strength), 0.0, 100.0)


def calculate_atr_pct(sessions: list[SessionData], period: int = 5) -> float:
    if not sessions:
        return 0.0
    if len(sessions) == 1:
        session = sessions[-1]
        return (session.high - session.low) / max(session.close, 0.01) * 100.0
    effective_period = min(period, len(sessions) - 1)
    true_ranges: list[float] = []
    for index in range(len(sessions) - effective_period, len(sessions)):
        session = sessions[index]
        previous_close = sessions[index - 1].close
        true_range = max(
            session.high - session.low,
            abs(session.high - previous_close),
            abs(session.low - previous_close),
        )
        true_ranges.append(true_range)
    latest_close = sessions[-1].close
    if not true_ranges or latest_close <= 0.0:
        return 0.0
    return sum(true_ranges) / len(true_ranges) / latest_close * 100.0


def calculate_standard_deviation(values: list[float]) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return math.sqrt(max(variance, 0.0))


def calculate_macd_hist_pct(sessions: list[SessionData]) -> float:
    closes = [session.close for session in sessions if session.close > 0.0]
    if len(closes) < 26:
        return 0.0

    ema12_values: list[float] = []
    ema26_values: list[float] = []
    multiplier_12 = 2.0 / 13.0
    multiplier_26 = 2.0 / 27.0
    ema12 = closes[0]
    ema26 = closes[0]
    for close in closes:
        ema12 = ema12 + multiplier_12 * (close - ema12)
        ema26 = ema26 + multiplier_26 * (close - ema26)
        ema12_values.append(ema12)
        ema26_values.append(ema26)

    macd_line = [fast - slow for fast, slow in zip(ema12_values, ema26_values)]
    signal_line = calculate_ema(macd_line[-min(len(macd_line), 50):], 9)
    latest_close = closes[-1]
    if latest_close <= 0.0:
        return 0.0
    return (macd_line[-1] - signal_line) / latest_close * 100.0


def calculate_stochastic_k(sessions: list[SessionData], period: int = 14) -> float:
    if not sessions:
        return 50.0
    window = sessions[-min(len(sessions), period):]
    lowest_low = min(session.low for session in window)
    highest_high = max(session.high for session in window)
    span = max(highest_high - lowest_low, 1e-9)
    return clamp((window[-1].close - lowest_low) / span * 100.0, 0.0, 100.0)


def calculate_bollinger_zscore(sessions: list[SessionData], period: int = 20) -> float:
    if not sessions:
        return 0.0
    closes = [session.close for session in sessions[-min(len(sessions), period):] if session.close > 0.0]
    if len(closes) < 5:
        return 0.0
    mean_close = sum(closes) / len(closes)
    stddev = calculate_standard_deviation(closes)
    if stddev <= 1e-9:
        return 0.0
    return (closes[-1] - mean_close) / (2.0 * stddev)


def calculate_adx(sessions: list[SessionData], period: int = 14) -> float:
    if len(sessions) < 3:
        return 20.0

    true_ranges: list[float] = []
    plus_dm_values: list[float] = []
    minus_dm_values: list[float] = []
    for index in range(1, len(sessions)):
        current = sessions[index]
        previous = sessions[index - 1]
        up_move = current.high - previous.high
        down_move = previous.low - current.low
        plus_dm = up_move if up_move > down_move and up_move > 0.0 else 0.0
        minus_dm = down_move if down_move > up_move and down_move > 0.0 else 0.0
        true_range = max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        true_ranges.append(true_range)
        plus_dm_values.append(plus_dm)
        minus_dm_values.append(minus_dm)

    effective_period = min(period, len(true_ranges))
    if effective_period <= 0:
        return 20.0

    dx_values: list[float] = []
    start_index = effective_period
    for end_index in range(start_index, len(true_ranges) + 1):
        tr_sum = sum(true_ranges[end_index - effective_period:end_index])
        if tr_sum <= 1e-9:
            continue
        plus_dm_sum = sum(plus_dm_values[end_index - effective_period:end_index])
        minus_dm_sum = sum(minus_dm_values[end_index - effective_period:end_index])
        plus_di = 100.0 * plus_dm_sum / tr_sum
        minus_di = 100.0 * minus_dm_sum / tr_sum
        di_sum = plus_di + minus_di
        if di_sum <= 1e-9:
            continue
        dx_values.append(100.0 * abs(plus_di - minus_di) / di_sum)

    if not dx_values:
        return 20.0
    recent_window = dx_values[-min(len(dx_values), 5):]
    return clamp(sum(recent_window) / len(recent_window), 0.0, 100.0)


def calculate_indicator_snapshot(sessions: list[SessionData]) -> IndicatorSnapshot:
    if not sessions:
        return IndicatorSnapshot()
    closes = [session.close for session in sessions]
    latest_close = closes[-1]
    if latest_close <= 0.0:
        return IndicatorSnapshot()
    ema5 = calculate_ema(closes[-min(len(closes), 20):], 5)
    ema10 = calculate_ema(closes[-min(len(closes), 40):], 10)
    return IndicatorSnapshot(
        rsi5=calculate_rsi(sessions, period=5),
        atr5_pct=calculate_atr_pct(sessions, period=5),
        ema5_gap_pct=(latest_close - ema5) / latest_close * 100.0,
        ema10_gap_pct=(latest_close - ema10) / latest_close * 100.0,
        ema_spread_pct=(ema5 - ema10) / latest_close * 100.0,
        adx14=calculate_adx(sessions, period=14),
        macd_hist_pct=calculate_macd_hist_pct(sessions),
        stoch_k14=calculate_stochastic_k(sessions, period=14),
        bollinger_z20=calculate_bollinger_zscore(sessions, period=20),
    )


def calculate_reversion_indicator_score(
    direction: str,
    indicators: IndicatorSnapshot,
    average_day_pct: float | None,
) -> float:
    avg_move = max(average_day_pct or 0.01, 0.01)
    macd_scale = max(avg_move * 0.14, 0.10)

    if direction == "long":
        stochastic_fit = clamp((38.0 - indicators.stoch_k14) / 26.0, 0.0, 1.0)
        band_fit = clamp((-indicators.bollinger_z20 - 0.08) / 0.82, 0.0, 1.0)
        macd_fit = clamp((-indicators.macd_hist_pct - avg_move * 0.01) / macd_scale, 0.0, 1.0)
    elif direction == "short":
        stochastic_fit = clamp((indicators.stoch_k14 - 62.0) / 26.0, 0.0, 1.0)
        band_fit = clamp((indicators.bollinger_z20 - 0.08) / 0.82, 0.0, 1.0)
        macd_fit = clamp((indicators.macd_hist_pct - avg_move * 0.01) / macd_scale, 0.0, 1.0)
    else:
        return 50.0

    adx_fit = clamp((indicators.adx14 - 18.0) / 18.0, 0.0, 1.0)
    return clamp(
        100.0 * (
            0.44 * stochastic_fit
            + 0.32 * band_fit
            + 0.16 * macd_fit
            + 0.08 * adx_fit
        ),
        0.0,
        100.0,
    )


def is_price_on_expected_side(direction: str, price: float, anchor_price: float) -> bool:
    if direction == "long":
        return price >= anchor_price
    if direction == "short":
        return price <= anchor_price
    return False


def calculate_preconfirmation_reversal_discount(
    direction: str,
    previous_close: float,
    average_day_pct: float | None,
    regime: RegimeAssessment,
    post_impulse: PostImpulseFeatures,
    selected_anchor: AnchorInfo | None = None,
) -> float:
    # A reversal that still closes beyond the key level is exploratory until price
    # reclaims the expected side of that level.
    if regime.regime not in PRECONFIRMATION_REVERSAL_REGIMES:
        return 0.0
    if regime.anchor is None or not regime.anchor.explicit:
        return 0.0
    if is_price_on_expected_side(direction, previous_close, regime.anchor.price):
        return 0.0
    if selected_anchor is not None and is_price_on_expected_side(direction, previous_close, selected_anchor.price):
        return 0.0

    avg_move = max(average_day_pct or 0.01, 0.01)
    gap_pct = abs(previous_close - regime.anchor.price) / max(previous_close, 0.01) * 100.0
    gap_ratio = gap_pct / avg_move

    gap_penalty = clamp(gap_ratio / 0.35, 0.0, 1.0)
    exhaustion_penalty = clamp((post_impulse.exhaustion_prob - 0.55) / 0.30, 0.0, 1.0)
    weak_continuation_penalty = clamp((0.45 - post_impulse.continuation_prob) / 0.25, 0.0, 1.0)
    failed_state = post_impulse.state in {"failed_breakdown", "failed_breakout"}

    if not failed_state and post_impulse.exhaustion_prob < 0.55 and post_impulse.continuation_prob >= 0.35:
        return clamp(0.04 * gap_penalty, 0.0, 0.10)

    if failed_state:
        return clamp(
            0.20
            + 0.18 * gap_penalty
            + 0.28 * exhaustion_penalty
            + 0.16 * weak_continuation_penalty,
            0.0,
            0.82,
        )

    return clamp(
        0.03
        + 0.08 * gap_penalty
        + 0.10 * exhaustion_penalty
        + 0.06 * weak_continuation_penalty,
        0.0,
        0.24,
    )


def calculate_level_exhaustion_penalty(
    direction: str,
    session: SessionData,
    anchor: float,
    best_target: TargetChoice,
    average_day_pct: float,
) -> float:
    day_range = max(session.high - session.low, 0.001)
    close_base = max(session.close, 0.001)

    if direction == "long":
        close_on_right_side = session.close >= anchor
        directional_close_location = clamp((session.close - session.low) / day_range, 0.0, 1.0)
        spent_from_anchor_pct = max(0.0, (session.close - anchor) / close_base * 100.0)
        anchor_to_target_pct = max(0.0, (best_target.price - anchor) / close_base * 100.0)
    else:
        close_on_right_side = session.close <= anchor
        directional_close_location = clamp((session.high - session.close) / day_range, 0.0, 1.0)
        spent_from_anchor_pct = max(0.0, (anchor - session.close) / close_base * 100.0)
        anchor_to_target_pct = max(0.0, (anchor - best_target.price) / close_base * 100.0)

    if not close_on_right_side or anchor_to_target_pct <= 0.0:
        return 0.0

    day_range_pct = day_range / close_base * 100.0
    body_pct = abs(session.close - session.open) / close_base * 100.0
    avg_move = max(average_day_pct, 0.01)

    spent_ratio = spent_from_anchor_pct / anchor_to_target_pct
    continuation_progress = spent_from_anchor_pct / max(best_target.move_pct_from_close, 0.10)
    continuation_risk = clamp((spent_ratio - 0.30) / 0.50, 0.0, 1.0)
    extreme_risk = clamp((directional_close_location - 0.58) / 0.35, 0.0, 1.0)
    range_expansion = clamp((day_range_pct / avg_move - 0.95) / 0.85, 0.0, 1.0)
    body_pressure = clamp((body_pct / avg_move - 0.45) / 0.85, 0.0, 1.0)
    followthrough_burn = clamp((continuation_progress - 0.80) / 0.50, 0.0, 1.0)

    penalty = 28.0 * (
        0.50 * continuation_risk
        + 0.30 * continuation_risk * extreme_risk
        + 0.20 * continuation_risk * max(range_expansion, body_pressure)
    )
    if continuation_risk > 0.0 and extreme_risk > 0.0:
        penalty += 8.0 * min(continuation_risk, extreme_risk)
    if followthrough_burn > 0.0 and extreme_risk > 0.0:
        penalty += 12.0 * min(followthrough_burn, extreme_risk)
    return clamp(penalty, 0.0, 30.0)


def calculate_level_compression_bonus(
    close_distance_ratio: float,
    day_range_pct: float,
    average_day_pct: float,
    target_ratio_to_average_day: float,
    on_right_side: bool,
) -> float:
    if not on_right_side:
        return 0.0
    if close_distance_ratio < 0.35 or close_distance_ratio > 0.95:
        return 0.0

    avg_move = max(average_day_pct, 0.01)
    compression_ratio = day_range_pct / avg_move
    if compression_ratio > 0.90:
        return 0.0
    if target_ratio_to_average_day < 0.75 or target_ratio_to_average_day > 1.05:
        return 0.0

    proximity_bonus = 1.0 - abs(close_distance_ratio - 0.60) / 0.35
    compression_bonus = (0.90 - compression_ratio) / 0.45
    strength = clamp(0.55 * proximity_bonus + 0.45 * compression_bonus, 0.0, 1.0)
    return strength * 14.0


def calculate_narrow_target_penalty(
    best_target: TargetChoice,
    ladder_score: float,
    history_score: float,
) -> float:
    if best_target.label == "T1" and ladder_score < 35.0 and history_score < 32.0:
        ladder_gap_penalty = (35.0 - ladder_score) * 0.18
        shallow_ratio_penalty = max(0.0, 0.45 - best_target.ratio_to_average_day) * 8.0
        return clamp(ladder_gap_penalty + shallow_ratio_penalty, 0.0, 5.0)

    if best_target.label == "T2" and ladder_score < 30.0 and history_score < 30.0:
        ladder_gap_penalty = (30.0 - ladder_score) * 0.12
        shallow_ratio_penalty = max(0.0, 0.50 - best_target.ratio_to_average_day) * 4.0
        return clamp(ladder_gap_penalty + shallow_ratio_penalty, 0.0, 3.0)

    return 0.0


def calculate_market_bias(index_sessions: list[SessionData]) -> float:
    latest = index_sessions[-1]
    day_range = max(latest.high - latest.low, 0.01)
    day_body_component = clamp((latest.close - latest.open) / day_range, -1.0, 1.0)

    closes = [session.close for session in index_sessions]
    if len(closes) >= 3:
        short_mean = sum(closes[-3:]) / 3.0
        trend_component = clamp(((latest.close / short_mean) - 1.0) * 100.0 / 1.5, -1.0, 1.0)
    else:
        trend_component = 0.0

    recent_window = index_sessions[-2:] if len(index_sessions) >= 2 else index_sessions
    swing_low = min(session.low for session in recent_window)
    swing_high = max(session.high for session in recent_window)
    swing_range = max(swing_high - swing_low, 0.01)
    swing_position = (latest.close - swing_low) / swing_range
    mean_reversion_component = clamp(-((swing_position - 0.5) * 2.0), -1.0, 1.0)

    return clamp(
        0.45 * day_body_component + 0.30 * trend_component + 0.25 * mean_reversion_component,
        -1.0,
        1.0,
    )


def calculate_market_score(direction: str, market_bias: float) -> float:
    base = 50.0
    if direction == "long":
        return clamp(base + market_bias * 24.0, 0.0, 100.0)
    if direction == "short":
        return clamp(base - market_bias * 24.0, 0.0, 100.0)
    return base


def calculate_relative_strength_score(
    direction: str,
    stock_sessions: list[SessionData],
    index_sessions: list[SessionData],
    average_day_pct: float | None,
    regime_name: str | None = None,
) -> float:
    avg_move = max(average_day_pct or 0.01, 0.01)
    reversal_mode = regime_name in ENTRY_QUALITY_REVERSAL_REGIMES
    weighted_edge = 0.0
    total_weight = 0.0
    for period, weight, scale in (
        (3, 0.48, 1.15),
        (5, 0.34, 1.75),
        (10, 0.18, 2.60),
    ):
        if len(stock_sessions) < period or len(index_sessions) < period:
            continue
        stock_window = stock_sessions[-period:]
        index_window = index_sessions[-period:]
        stock_ret = ((stock_window[-1].close / max(stock_window[0].close, 0.01)) - 1.0) * 100.0
        index_ret = ((index_window[-1].close / max(index_window[0].close, 0.01)) - 1.0) * 100.0
        if reversal_mode:
            edge = index_ret - stock_ret if direction == "long" else stock_ret - index_ret
        else:
            edge = stock_ret - index_ret if direction == "long" else index_ret - stock_ret
        weighted_edge += weight * clamp(edge / max(avg_move * scale, 0.10), -1.0, 1.0)
        total_weight += weight

    if total_weight <= 0.0:
        return 50.0

    score = 50.0 + 50.0 * (weighted_edge / total_weight)
    if len(stock_sessions) >= 2 and len(index_sessions) >= 2:
        stock_day_ret = ((stock_sessions[-1].close / max(stock_sessions[-2].close, 0.01)) - 1.0) * 100.0
        index_day_ret = ((index_sessions[-1].close / max(index_sessions[-2].close, 0.01)) - 1.0) * 100.0
        if reversal_mode:
            day_edge = index_day_ret - stock_day_ret if direction == "long" else stock_day_ret - index_day_ret
        else:
            day_edge = stock_day_ret - index_day_ret if direction == "long" else index_day_ret - stock_day_ret
        score += 8.0 * clamp(day_edge / max(avg_move * 0.90, 0.10), -1.0, 1.0)
    return clamp(score, 0.0, 100.0)


def calculate_volume_quality_score(
    direction: str,
    sessions: list[SessionData],
    average_day_pct: float | None,
    post_impulse: PostImpulseFeatures,
) -> float:
    if len(sessions) < 4:
        return 50.0

    recent_volumes = [session.volume for session in sessions[-6:-1] if session.volume > 0.0]
    latest_volume = max(sessions[-1].volume, 0.0)
    if len(recent_volumes) < 3 or latest_volume <= 0.0:
        return 50.0

    avg_recent_volume = sum(recent_volumes) / len(recent_volumes)
    volume_ratio = latest_volume / max(avg_recent_volume, 1.0)
    avg_move = max(average_day_pct or 0.01, 0.01)
    context = build_directional_context(direction, sessions[-5:], avg_move)

    directional_push_fit = clamp((volume_ratio - 0.95) / 0.80, 0.0, 1.0)
    dryup_fit = clamp((1.05 - volume_ratio) / 0.55, 0.0, 1.0)
    heavy_pressure = clamp((volume_ratio - 1.20) / 0.90, 0.0, 1.0)

    score = 50.0
    if context["aligned_body"] > avg_move * 0.12 or context["breakout_margin_pct"] > 0.0:
        score += 18.0 * directional_push_fit
        if volume_ratio < 0.75:
            score -= 10.0 * clamp((0.75 - volume_ratio) / 0.35, 0.0, 1.0)

    if context["aligned_ret3"] < -avg_move * 0.25:
        score += 14.0 * dryup_fit
        score -= 16.0 * heavy_pressure

    if post_impulse.state in {"pause", "trend_continuation", "reclaim_after_failure"}:
        score += 6.0 * directional_push_fit
    elif post_impulse.state in {"failed_breakdown", "failed_breakout", "exhaustion"}:
        score -= 8.0 * heavy_pressure

    return clamp(score, 0.0, 100.0)


def calculate_freshness_score(
    direction: str,
    sessions: list[SessionData],
    best_target: TargetChoice,
    average_day_pct: float | None,
    post_impulse: PostImpulseFeatures,
    anchor: AnchorInfo | None = None,
) -> float:
    if not sessions:
        return 50.0

    latest = sessions[-1]
    avg_move = max(average_day_pct or 0.01, 0.01)
    direction_sign = 1.0 if direction == "long" else -1.0
    aligned_body = direction_sign * ((latest.close / max(latest.open, 0.01)) - 1.0) * 100.0
    target_move_pct = max(best_target.move_pct_from_close, 0.10)
    spent_ratio = abs(aligned_body) / target_move_pct

    target_window_fit = clamp(1.0 - abs(best_target.ratio_to_average_day - 0.78) / 0.38, 0.0, 1.0)
    freshness_fit = 0.55
    if post_impulse.impulse_strength > 0.0:
        freshness_fit = clamp(1.0 - max(post_impulse.impulse_age - 2, 0) / 4.0, 0.0, 1.0)

    retrace_fit = 0.52
    breakout_ready_fit = 0.45
    if post_impulse.state in {"pause", "trend_continuation", "reclaim_after_failure"}:
        retrace_fit = clamp(1.0 - abs(post_impulse.retrace_ratio - 0.35) / 0.45, 0.0, 1.0)
        breakout_ready_fit = clamp(post_impulse.breakout_proximity, 0.0, 1.0)
    elif post_impulse.state == "neutral":
        retrace_fit = clamp(1.0 - abs(post_impulse.retrace_ratio - 0.20) / 0.55, 0.0, 1.0)
        breakout_ready_fit = clamp(1.0 - post_impulse.exhaustion_prob * 0.8, 0.0, 1.0)
    elif post_impulse.state == "exhaustion":
        retrace_fit = clamp(1.0 - post_impulse.retrace_ratio / 1.10, 0.0, 1.0)
        breakout_ready_fit = clamp(0.55 - post_impulse.exhaustion_prob * 0.6, 0.0, 1.0)

    score = 32.0 + 24.0 * target_window_fit + 16.0 * freshness_fit + 14.0 * retrace_fit + 14.0 * breakout_ready_fit
    if anchor is not None:
        close_distance_pct = abs(latest.close - anchor.price) / max(latest.close, 0.01) * 100.0
        close_distance_ratio = close_distance_pct / avg_move
        score += 10.0 * clamp(1.0 - abs(close_distance_ratio - 0.55) / 0.65, 0.0, 1.0)

    score -= 18.0 * clamp((spent_ratio - 0.85) / 0.55, 0.0, 1.0)
    score -= 16.0 * clamp((post_impulse.exhaustion_prob - 0.55) / 0.25, 0.0, 1.0)
    return clamp(score, 0.0, 100.0)


def calculate_entry_quality_snapshot(
    direction: str,
    stock_sessions: list[SessionData],
    index_sessions: list[SessionData],
    best_target: TargetChoice,
    average_day_pct: float | None,
    post_impulse: PostImpulseFeatures,
    regime_name: str,
    anchor: AnchorInfo | None = None,
) -> EntryQualitySnapshot:
    relative_strength = calculate_relative_strength_score(
        direction,
        stock_sessions,
        index_sessions,
        average_day_pct,
        regime_name,
    )
    volume_quality = calculate_volume_quality_score(
        direction,
        stock_sessions,
        average_day_pct,
        post_impulse,
    )
    freshness = calculate_freshness_score(
        direction,
        stock_sessions,
        best_target,
        average_day_pct,
        post_impulse,
        anchor,
    )
    total = clamp(
        0.45 * relative_strength + 0.20 * volume_quality + 0.35 * freshness,
        0.0,
        100.0,
    )
    return EntryQualitySnapshot(
        total=total,
        relative_strength=relative_strength,
        volume_quality=volume_quality,
        freshness=freshness,
    )


def get_entry_quality_weight(regime_name: str, entry_quality_score: float) -> float:
    if regime_name == "counter_impulse_reversal":
        return 0.05
    if regime_name == "mean_reversion":
        if entry_quality_score >= 80.0:
            return 0.15
        if entry_quality_score >= 70.0:
            return 0.06
        return 0.0
    return ENTRY_QUALITY_OVERALL_WEIGHT


def calculate_instrument_score(
    direction: str,
    sessions: list[SessionData],
    average_day_pct: float | None,
    idea: WatchIdea | None = None,
    best_target: TargetChoice | None = None,
    anchor: AnchorInfo | None = None,
    indicator_snapshot: IndicatorSnapshot | None = None,
) -> float:
    if not sessions:
        return 50.0

    window = sessions[-5:] if len(sessions) >= 5 else sessions
    latest = window[-1]
    avg_move = max(average_day_pct or 0.01, 0.01)
    indicators = indicator_snapshot or calculate_indicator_snapshot(sessions)
    direction_sign = 1.0 if direction == "long" else -1.0

    ret3 = 0.0
    if len(window) >= 3:
        ret3 = ((latest.close / window[-3].close) - 1.0) * 100.0

    ret5 = 0.0
    if len(window) >= 5:
        ret5 = ((latest.close / window[0].close) - 1.0) * 100.0
    elif len(window) >= 2:
        ret5 = ((latest.close / window[0].close) - 1.0) * 100.0

    range_low = min(session.low for session in window)
    range_high = max(session.high for session in window)
    range_size = max(range_high - range_low, 0.01)
    range_position = ((latest.close - range_low) / range_size - 0.5) * 2.0
    prior_window = window[:-1] if len(window) > 1 else window
    prior_low = min(session.low for session in prior_window)
    prior_high = max(session.high for session in prior_window)

    day_body = ((latest.close / max(latest.open, 0.01)) - 1.0) * 100.0

    aligned_ret3 = direction_sign * ret3
    aligned_ret5 = direction_sign * ret5
    aligned_body = direction_sign * day_body
    aligned_range = direction_sign * range_position

    ret3_component = clamp(aligned_ret3 / (avg_move * 1.0), -1.0, 1.0)
    ret5_component = clamp(aligned_ret5 / (avg_move * 1.6), -1.0, 1.0)
    body_component = clamp(aligned_body / (avg_move * 0.8), -1.0, 1.0)
    range_component = clamp(aligned_range, -1.0, 1.0)

    extension_pressure = max(0.0, aligned_ret5 / avg_move) + max(0.0, aligned_body / avg_move)
    extension_penalty = clamp((extension_pressure - 2.3) / 1.0, 0.0, 1.0)

    score = 50.0 + 50.0 * (
        0.45 * ret3_component +
        0.25 * ret5_component +
        0.20 * body_component +
        0.10 * range_component
    )
    score -= extension_penalty * 30.0
    continuation_score = clamp(score, 0.0, 100.0)

    # Mean-reversion: moderate counter-trend pullback is the setup signal
    if aligned_ret3 < -avg_move * 0.25:
        pullback_depth = -aligned_ret3 / avg_move
        pullback_quality = clamp(1.0 - abs(pullback_depth - 1.0) / 1.2, -0.3, 1.0)
        reversal_range = clamp(-aligned_range, -1.0, 1.0)
        reversal_body = clamp(aligned_body / (avg_move * 0.6), -1.0, 1.0)
        if direction == "long":
            rsi_reversion_fit = clamp((46.0 - indicators.rsi5) / 18.0, 0.0, 1.0)
            ema_reversion_fit = clamp((-indicators.ema10_gap_pct) / max(avg_move * 0.45, 0.01), 0.0, 1.0)
        else:
            rsi_reversion_fit = clamp((indicators.rsi5 - 54.0) / 18.0, 0.0, 1.0)
            ema_reversion_fit = clamp(indicators.ema10_gap_pct / max(avg_move * 0.45, 0.01), 0.0, 1.0)
        atr_reversion_fit = clamp((indicators.atr5_pct / avg_move - 0.55) / 0.95, 0.0, 1.0)
        reversion_score = 50.0 + 50.0 * (
            0.40 * pullback_quality
            + 0.30 * reversal_range
            + 0.30 * reversal_body
        )
        reversion_score += 14.0 * (
            0.45 * rsi_reversion_fit
            + 0.35 * ema_reversion_fit
            + 0.20 * atr_reversion_fit
        )
        if rsi_reversion_fit < 0.12 and ema_reversion_fit < 0.12:
            reversion_score -= 8.0
        if pullback_depth > 2.2:
            reversion_score -= clamp((pullback_depth - 2.2) / 1.5, 0.0, 1.0) * 35.0
        reversion_score = clamp(reversion_score, 0.0, 100.0)
        continuation_score = max(continuation_score, reversion_score)

    breakout_bonus = 0.0
    if direction == "long":
        breakout_margin_pct = max(0.0, (latest.close - prior_high) / max(latest.close, 0.01) * 100.0)
    else:
        breakout_margin_pct = max(0.0, (prior_low - latest.close) / max(latest.close, 0.01) * 100.0)
    breakout_bonus = clamp(breakout_margin_pct / (avg_move * 0.45), 0.0, 1.0) * 18.0
    continuation_score = clamp(continuation_score + breakout_bonus, 0.0, 100.0)
    trend_support = clamp(direction_sign * indicators.ema_spread_pct / max(avg_move * 0.18, 0.01), -1.0, 1.0)
    volatility_support = clamp(1.0 - abs(indicators.atr5_pct / avg_move - 1.0) / 0.80, -0.4, 1.0)
    continuation_score = clamp(continuation_score + 5.0 * trend_support + 3.0 * volatility_support, 0.0, 100.0)

    explicit_level = anchor is not None
    if not explicit_level:
        return continuation_score

    anchor_price = anchor.price
    exhaustion_penalty = calculate_level_exhaustion_penalty(direction, latest, anchor_price, best_target, avg_move)
    continuation_cap = max(55.0, 100.0 - exhaustion_penalty * 1.8)
    continuation_score = min(continuation_score, continuation_cap)

    close_distance_pct = abs(latest.close - anchor_price) / max(latest.close, 0.01) * 100.0
    close_distance_ratio = close_distance_pct / avg_move
    proximity_weight = clamp(1.0 - close_distance_pct / (avg_move * 1.2), 0.0, 1.0)
    level_penalty = 0.0
    if close_distance_ratio > 2.5:
        level_penalty = clamp((close_distance_ratio - 2.5) / 2.0, 0.0, 1.0) * 25.0
        return clamp(continuation_score - level_penalty, 0.0, 100.0)

    if direction == "long":
        approach_ret3_component = clamp((-ret3) / (avg_move * 1.1), -1.0, 1.0)
        approach_ret5_component = clamp((-ret5) / (avg_move * 1.8), -1.0, 1.0)
        approach_body_component = clamp((-day_body) / (avg_move * 0.9), -1.0, 1.0)
        approach_range_component = clamp(-range_position, -1.0, 1.0)
        on_right_side = latest.close >= anchor_price
    else:
        approach_ret3_component = clamp(ret3 / (avg_move * 1.1), -1.0, 1.0)
        approach_ret5_component = clamp(ret5 / (avg_move * 1.8), -1.0, 1.0)
        approach_body_component = clamp(day_body / (avg_move * 0.9), -1.0, 1.0)
        approach_range_component = clamp(range_position, -1.0, 1.0)
        on_right_side = latest.close <= anchor_price

    approach_extension = max(0.0, abs(ret3) / avg_move - 1.8)
    approach_penalty = clamp(approach_extension / 1.0, 0.0, 1.0) * 18.0

    approach_score = 50.0 + 50.0 * (
        0.35 * approach_ret3_component +
        0.15 * approach_ret5_component +
        0.25 * approach_body_component +
        0.25 * approach_range_component
    )
    approach_score = clamp(approach_score - approach_penalty, 0.0, 100.0)

    level_context_score = 0.65 * approach_score + 0.35 * continuation_score
    if continuation_score < 20.0:
        level_context_score -= (20.0 - continuation_score) * 0.45
    level_context_score += calculate_level_compression_bonus(
        close_distance_ratio=close_distance_ratio,
        day_range_pct=(latest.high - latest.low) / max(latest.close, 0.01) * 100.0,
        average_day_pct=avg_move,
        target_ratio_to_average_day=best_target.ratio_to_average_day if best_target is not None else 0.0,
        on_right_side=on_right_side,
    )

    blended_score = (
        proximity_weight * level_context_score
        + (1.0 - proximity_weight) * continuation_score
    )
    if on_right_side and aligned_ret3 > 0.0 and aligned_body > 0.0:
        target_move_pct = max(best_target.move_pct_from_close if best_target is not None else avg_move, 0.10)
        spent_move_ratio = abs(day_body) / target_move_pct
        spent_move_penalty = clamp((spent_move_ratio - 1.15) / 0.85, 0.0, 1.0) * 18.0
        continuation_floor = max(continuation_score - spent_move_penalty, 0.0)
        blended_score = max(blended_score, continuation_floor)
        if breakout_margin_pct <= 0.0:
            stale_pressure = max(0.0, spent_move_ratio - 0.75) + max(0.0, aligned_ret3 / avg_move - 1.0)
            stale_penalty = clamp(stale_pressure / 1.5, 0.0, 1.0) * 22.0
            blended_score -= stale_penalty
    blended_score -= exhaustion_penalty
    return clamp(blended_score, 0.0, 100.0)


def build_directional_context(
    direction: str,
    sessions: list[SessionData],
    average_day_pct: float,
) -> dict[str, float]:
    window = sessions[-5:] if len(sessions) >= 5 else sessions
    latest = window[-1]
    direction_sign = 1.0 if direction == "long" else -1.0

    ret3 = 0.0
    if len(window) >= 3:
        ret3 = ((latest.close / window[-3].close) - 1.0) * 100.0

    ret5 = 0.0
    if len(window) >= 5:
        ret5 = ((latest.close / window[0].close) - 1.0) * 100.0
    elif len(window) >= 2:
        ret5 = ((latest.close / window[0].close) - 1.0) * 100.0

    range_low = min(session.low for session in window)
    range_high = max(session.high for session in window)
    range_size = max(range_high - range_low, 0.01)
    range_position = ((latest.close - range_low) / range_size - 0.5) * 2.0
    day_body = ((latest.close / max(latest.open, 0.01)) - 1.0) * 100.0
    prior_window = window[:-1] if len(window) > 1 else window
    prior_low = min(session.low for session in prior_window)
    prior_high = max(session.high for session in prior_window)

    if direction == "long":
        breakout_margin_pct = max(0.0, (latest.close - prior_high) / max(latest.close, 0.01) * 100.0)
    else:
        breakout_margin_pct = max(0.0, (prior_low - latest.close) / max(latest.close, 0.01) * 100.0)

    day_range_pct = (latest.high - latest.low) / max(latest.close, 0.01) * 100.0
    return {
        "aligned_ret3": direction_sign * ret3,
        "aligned_ret5": direction_sign * ret5,
        "aligned_body": direction_sign * day_body,
        "aligned_range": direction_sign * range_position,
        "breakout_margin_pct": breakout_margin_pct,
        "day_range_pct": day_range_pct,
        "average_day_pct": average_day_pct,
    }


def calculate_history_score(
    direction: str,
    sessions: list[SessionData],
    best_target: TargetChoice,
    average_day_pct: float | None,
) -> float:
    avg_move = max(average_day_pct or 0.01, 0.01)
    if len(sessions) < 25:
        return 50.0

    current_context = build_directional_context(direction, sessions[-5:], avg_move)
    target_move_pct = max(best_target.move_pct_from_close, 0.10)
    analogs: list[tuple[float, float, float]] = []

    for end_index in range(4, len(sessions) - 1):
        sample_window = sessions[max(0, end_index - 4): end_index + 1]
        if len(sample_window) < 3:
            continue
        sample_context = build_directional_context(direction, sample_window, avg_move)
        distance = (
            abs(sample_context["aligned_ret3"] - current_context["aligned_ret3"]) / (avg_move * 1.2)
            + abs(sample_context["aligned_ret5"] - current_context["aligned_ret5"]) / (avg_move * 1.8)
            + abs(sample_context["aligned_body"] - current_context["aligned_body"]) / (avg_move * 1.0)
            + abs(sample_context["aligned_range"] - current_context["aligned_range"]) * 1.1
            + abs(sample_context["breakout_margin_pct"] - current_context["breakout_margin_pct"]) / (avg_move * 0.8)
        )

        setup_session = sample_window[-1]
        next_session = sessions[end_index + 1]
        if direction == "long":
            realized_move_pct = max(0.0, (next_session.high - setup_session.close) / max(setup_session.close, 0.01) * 100.0)
        else:
            realized_move_pct = max(0.0, (setup_session.close - next_session.low) / max(setup_session.close, 0.01) * 100.0)

        hit = 1.0 if realized_move_pct >= target_move_pct else 0.0
        move_ratio = clamp(realized_move_pct / target_move_pct, 0.0, 1.5)
        analogs.append((distance, hit, move_ratio))

    if len(analogs) < 8:
        return 50.0

    analogs.sort(key=lambda item: item[0])
    nearest = analogs[: min(18, len(analogs))]
    weighted_hit = 0.0
    weighted_move = 0.0
    total_weight = 0.0
    for distance, hit, move_ratio in nearest:
        weight = 1.0 / (0.35 + distance)
        weighted_hit += weight * hit
        weighted_move += weight * move_ratio
        total_weight += weight

    if total_weight <= 0.0:
        return 50.0

    hit_rate = clamp(weighted_hit / total_weight, 0.0, 1.0)
    normalized_move = clamp(weighted_move / total_weight / 1.15, 0.0, 1.0)
    sample_confidence = clamp(len(nearest) / 14.0, 0.0, 1.0)
    raw_score = 100.0 * (0.65 * hit_rate + 0.35 * normalized_move)
    compressed_score = 50.0 + (raw_score - 50.0) * sample_confidence

    # For one-day selection, historical analogs are a useful prior but a weak veto.
    # Fresh intraday leaders were being suppressed too hard on low history alone,
    # especially around T2 morning setups.
    if compressed_score < 50.0:
        downside_scale = 0.64 if best_target.label == "T1" else 0.58
        compressed_score = 50.0 + (compressed_score - 50.0) * downside_scale
    else:
        upside_scale = 0.84 if best_target.label in {"T1", "T2"} else 0.80
        compressed_score = 50.0 + (compressed_score - 50.0) * upside_scale

    return clamp(compressed_score, 0.0, 100.0)


def calculate_range_consolidation_penalty(
    sessions: list[SessionData],
    average_day_pct: float,
) -> float:
    """Penalize when price is stuck in a tight multi-day range (same highs/lows repeating)."""
    if len(sessions) < 4:
        return 0.0

    window = sessions[-5:] if len(sessions) >= 5 else sessions[-4:]
    highs = [s.high for s in window]
    lows = [s.low for s in window]
    latest_close = window[-1].close
    avg_move_abs = latest_close * average_day_pct / 100.0

    high_spread = max(highs) - min(highs)
    low_spread = max(lows) - min(lows)
    overall_range = max(highs) - min(lows)

    high_tight = high_spread < avg_move_abs * 0.7
    low_tight = low_spread < avg_move_abs * 0.7
    range_narrow = overall_range < avg_move_abs * 1.8

    if high_tight and low_tight and range_narrow:
        tightness = 1.0 - (high_spread + low_spread) / (avg_move_abs * 1.4)
        return clamp(tightness * 20.0, 0.0, 20.0)

    if (high_tight or low_tight) and range_narrow:
        return clamp(8.0, 0.0, 12.0)

    return 0.0


def calculate_target_beyond_resistance_penalty(
    direction: str,
    best_target: TargetChoice,
    resistance: float | None,
    support: float | None,
    previous_close: float,
    average_day_pct: float,
) -> float:
    """Penalize when the target price is beyond a detected resistance/support level."""
    if direction == "long" and resistance is not None:
        if best_target.price > resistance:
            overshoot_pct = (best_target.price - resistance) / max(previous_close, 0.01) * 100.0
            overshoot_ratio = overshoot_pct / max(average_day_pct, 0.01)
            return clamp(overshoot_ratio / 0.6, 0.0, 1.0) * 22.0
    elif direction == "short" and support is not None:
        if best_target.price < support:
            overshoot_pct = (support - best_target.price) / max(previous_close, 0.01) * 100.0
            overshoot_ratio = overshoot_pct / max(average_day_pct, 0.01)
            return clamp(overshoot_ratio / 0.6, 0.0, 1.0) * 22.0
    return 0.0


def calculate_recent_barrier_proximity_penalty(
    direction: str,
    best_target: TargetChoice,
    sessions: list[SessionData],
    previous_close: float,
    average_day_pct: float,
    resistance: float | None,
    support: float | None,
    regime_name: str,
) -> float:
    """Penalize reversal targets that press almost directly into a recent swing barrier without an opposing level."""
    if best_target.label not in {"T3", "T4"}:
        return 0.0
    if len(sessions) < 3:
        return 0.0
    if regime_name not in {
        "counter_impulse_reversal",
        "mean_reversion",
        "level_rejection",
        "capitulation_reversal",
        "absorption_reversal",
    }:
        return 0.0

    avg_move = max(average_day_pct, 0.01)
    barrier_price: float | None = None
    barrier_gap_pct = 0.0
    barrier_span_pct = 0.0

    if direction == "long":
        if resistance is not None:
            return 0.0
        overhead_highs = [session.high for session in sessions if session.high >= best_target.price]
        if not overhead_highs:
            return 0.0
        barrier_price = min(overhead_highs)
        barrier_gap_pct = max(0.0, (barrier_price - best_target.price) / max(previous_close, 0.01) * 100.0)
        barrier_span_pct = max(0.0, (barrier_price - previous_close) / max(previous_close, 0.01) * 100.0)
    elif direction == "short":
        if support is not None:
            return 0.0
        overhead_lows = [session.low for session in sessions if session.low <= best_target.price]
        if not overhead_lows:
            return 0.0
        barrier_price = max(overhead_lows)
        barrier_gap_pct = max(0.0, (best_target.price - barrier_price) / max(previous_close, 0.01) * 100.0)
        barrier_span_pct = max(0.0, (previous_close - barrier_price) / max(previous_close, 0.01) * 100.0)
    else:
        return 0.0

    if barrier_price is None:
        return 0.0

    barrier_gap_ratio = barrier_gap_pct / avg_move
    barrier_span_ratio = barrier_span_pct / avg_move
    if barrier_span_ratio < 0.55 or barrier_span_ratio > 1.05:
        return 0.0

    gap_fit = clamp((0.18 - barrier_gap_ratio) / 0.18, 0.0, 1.0)
    span_fit = clamp(1.0 - abs(barrier_span_ratio - 0.80) / 0.28, 0.0, 1.0)
    target_fit = clamp((best_target.ratio_to_average_day - 0.72) / 0.22, 0.0, 1.0)
    strength = clamp(0.55 * gap_fit + 0.25 * span_fit + 0.20 * target_fit, 0.0, 1.0)
    return strength * 7.0


def calculate_setup_score(
    idea: WatchIdea,
    direction: str,
    session: SessionData,
    best_target: TargetChoice,
    sessions: list[SessionData] | None = None,
    anchor: AnchorInfo | None = None,
) -> float:
    day_range = max(session.high - session.low, 0.001)
    close_location_long = clamp((session.close - session.low) / day_range, 0.0, 1.0)
    close_location_short = clamp((session.high - session.close) / day_range, 0.0, 1.0)
    body_direction = clamp((session.close - session.open) / day_range, -1.0, 1.0)

    if direction == "long":
        anchor_price = anchor.price if anchor is not None else session.low
        body_fit = 50.0 + 50.0 * body_direction
        location_fit = 100.0 * close_location_long
    elif direction == "short":
        anchor_price = anchor.price if anchor is not None else session.high
        body_fit = 50.0 + 50.0 * (-body_direction)
        location_fit = 100.0 * close_location_short
    else:
        return 0.0

    average_day_pct = max(idea.average_day_pct or 0.01, 0.01)
    close_distance_pct = abs(session.close - anchor_price) / max(session.close, 0.001) * 100.0
    close_distance_ratio = close_distance_pct / average_day_pct
    candle_fit = 0.55 * body_fit + 0.45 * location_fit

    rr_ratio = best_target.move_pct_from_close / max(close_distance_pct, 0.10)
    rr_fit = clamp(rr_ratio / 4.0 * 100.0, 0.0, 100.0)
    target_fit = clamp(best_target.score, 0.0, 100.0)

    explicit_level = anchor is not None
    level_penalty = 0.0
    if explicit_level and close_distance_ratio > 2.5:
        level_penalty = clamp((close_distance_ratio - 2.5) / 2.0, 0.0, 1.0) * 25.0
        explicit_level = False
    continuation_weight = clamp((close_distance_ratio - 0.55) / 0.75, 0.0, 1.0)

    if explicit_level:
        level_contact = evaluate_level_contact(direction, session, anchor_price, average_day_pct)
        touch_gap_pct = float(level_contact["touch_gap_pct"])
        penetration_pct = float(level_contact["penetration_pct"])
        escape_pct = float(level_contact["escape_pct"])
        touched = bool(level_contact["touched"])
        close_on_right_side = bool(level_contact["close_on_right_side"])
        if sessions is not None and len(sessions) >= 3:
            capitulation_context = build_directional_context(direction, sessions[-5:], average_day_pct)
        else:
            aligned_body_pct = ((session.close / max(session.open, 0.01)) - 1.0) * 100.0
            if direction == "short":
                aligned_body_pct *= -1.0
            capitulation_context = {
                "aligned_ret3": aligned_body_pct,
                "aligned_body": aligned_body_pct,
                "day_range_pct": day_range / max(session.close, 0.001) * 100.0,
            }
        capitulation_strength = calculate_capitulation_reversal_strength(
            average_day_pct=average_day_pct,
            context=capitulation_context,
            close_distance_ratio=close_distance_ratio,
            anchor=anchor,
            level_contact=level_contact,
        )
        absorption_strength = calculate_absorption_reversal_strength(
            average_day_pct=average_day_pct,
            context=capitulation_context,
            close_distance_ratio=close_distance_ratio,
            anchor=anchor,
            level_contact=level_contact,
        )
        pre_level_strength = calculate_pre_level_rejection_strength(
            average_day_pct=average_day_pct,
            context=capitulation_context,
            close_distance_ratio=close_distance_ratio,
            anchor=anchor,
            level_contact=level_contact,
        )

        touch_score = 100.0 if touched else max(
            0.0,
            100.0 - (touch_gap_pct / average_day_pct) * 140.0,
        )
        escape_score = clamp(escape_pct / (average_day_pct * 0.45), 0.0, 1.0) * 100.0
        side_score = escape_score if close_on_right_side else max(0.0, 25.0 - escape_score)
        rejection_score = 0.0
        if touched:
            rejection_score = clamp(
                (penetration_pct * 0.7 + escape_pct * 1.2) / (average_day_pct * 0.75),
                0.0,
                1.0,
            ) * 100.0

        reaction_fit = 0.55 * candle_fit + 0.45 * escape_score
        wick_rejection_bonus = 0.0
        if touched and close_on_right_side:
            if direction == "long":
                wick_pct = (session.close - session.low) / max(session.close, 0.001) * 100.0
            else:
                wick_pct = (session.high - session.close) / max(session.close, 0.001) * 100.0
            wick_ratio = wick_pct / average_day_pct
            if wick_ratio > 0.15:
                wick_rejection_bonus = clamp((wick_ratio - 0.15) / 0.5, 0.0, 1.0) * 12.0
        reversal_setup = (
            0.18 * touch_score +
            0.22 * side_score +
            0.30 * rejection_score +
            0.20 * reaction_fit +
            0.10 * rr_fit
        ) + wick_rejection_bonus
        reversal_setup += 22.0 * capitulation_strength
        reversal_setup += 28.0 * absorption_strength
        if not touched:
            reversal_setup += 14.0 * pre_level_strength

        # A tight close slightly beyond an explicit level can still be a fragile breakout.
        if anchor.explicit and touched and not close_on_right_side:
            day_range_pct = day_range / max(session.close, 0.001) * 100.0
            penetration_ratio = penetration_pct / average_day_pct
            range_ratio = day_range_pct / average_day_pct
            if direction == "long":
                extreme_close = close_location_short
            else:
                extreme_close = close_location_long

            if (
                0.08 <= penetration_ratio <= 0.32
                and extreme_close >= 0.92
                and range_ratio <= 0.55
                ):
                    breakout_fragility = clamp(
                        0.45 * clamp((penetration_ratio - 0.08) / 0.24, 0.0, 1.0)
                        + 0.35 * clamp((extreme_close - 0.92) / 0.08, 0.0, 1.0)
                        + 0.20 * clamp(1.0 - range_ratio / 0.55, 0.0, 1.0),
                    0.0,
                    1.0,
                    )
                    reversal_setup += 18.0 * breakout_fragility

            if (
                close_distance_ratio <= 0.05
                and 0.22 <= penetration_ratio <= 0.75
                and range_ratio >= 1.10
                and capitulation_context["aligned_ret3"] < -average_day_pct * 0.25
            ):
                sticky_bonus = clamp(
                    0.35 * clamp(1.0 - close_distance_ratio / 0.05, 0.0, 1.0)
                    + 0.35 * clamp(1.0 - abs(penetration_ratio - 0.42) / 0.33, 0.0, 1.0)
                    + 0.30 * clamp((range_ratio - 1.10) / 1.20, 0.0, 1.0),
                    0.0,
                    1.0,
                )
                reversal_setup += 24.0 * sticky_bonus

        weak_rejection_penalty = clamp((55.0 - reaction_fit) / 55.0, 0.0, 1.0) * 18.0
        if capitulation_strength > 0.0:
            weak_rejection_penalty *= 0.45
        if absorption_strength > 0.0:
            weak_rejection_penalty *= 1.0 - 0.85 * absorption_strength
        if pre_level_strength > 0.0:
            weak_rejection_penalty *= 1.0 - 0.55 * pre_level_strength
        if not close_on_right_side:
            weak_rejection_penalty += 10.0
            if anchor.explicit:
                day_range_pct = day_range / max(session.close, 0.001) * 100.0
                penetration_ratio = penetration_pct / average_day_pct
                range_ratio = day_range_pct / average_day_pct
                if direction == "long":
                    extreme_close = close_location_short
                else:
                    extreme_close = close_location_long
                if (
                    0.08 <= penetration_ratio <= 0.32
                    and extreme_close >= 0.92
                    and range_ratio <= 0.55
                ):
                    weak_rejection_penalty *= 0.45
                elif (
                    close_distance_ratio <= 0.05
                    and 0.22 <= penetration_ratio <= 0.75
                    and range_ratio >= 1.10
                ):
                    weak_rejection_penalty *= 0.20
        if touched and penetration_pct > average_day_pct * 0.9 and escape_pct < average_day_pct * 0.2:
            weak_rejection_penalty += 8.0
        if touched and close_on_right_side and penetration_pct > average_day_pct * 0.25 and escape_pct < average_day_pct * 0.20:
            penetration_severity = clamp(penetration_pct / (average_day_pct * 0.5), 0.3, 1.0)
            failed_reclaim_penalty = clamp(
                (penetration_pct / max(escape_pct, average_day_pct * 0.05) - 1.0) / 2.0,
                0.0,
                1.0,
            ) * 14.0 * penetration_severity
            weak_rejection_penalty += failed_reclaim_penalty

        reversal_setup -= weak_rejection_penalty
        reversal_setup -= calculate_level_exhaustion_penalty(direction, session, anchor_price, best_target, average_day_pct)
    else:
        touch_score = 0.0
        escape_score = 0.0
        reversal_setup = 0.0
        continuation_weight = 1.0

    continuation_fit = 0.70 * candle_fit + 0.30 * target_fit
    extension_penalty = clamp((close_distance_ratio - 1.15) / 0.55, 0.0, 1.0) * 18.0
    continuation_setup = continuation_fit - extension_penalty - level_penalty

    blended_setup = (1.0 - continuation_weight) * reversal_setup + continuation_weight * continuation_setup

    average_day_pct_val = max(idea.average_day_pct or 0.01, 0.01)
    if sessions is not None:
        blended_setup -= calculate_range_consolidation_penalty(sessions, average_day_pct_val)
    blended_setup -= calculate_target_beyond_resistance_penalty(
        direction, best_target, idea.resistance, idea.support, session.close, average_day_pct_val,
    )

    return clamp(blended_setup, 0.0, 100.0)


def combine_execution_score(setup_score: float, instrument_score: float) -> float:
    strong_side = max(setup_score, instrument_score)
    weak_side = min(setup_score, instrument_score)
    average_score = (setup_score + instrument_score) / 2.0
    return 0.25 * weak_side + 0.30 * average_score + 0.45 * strong_side


def calculate_rank_calibration(
    direction: str,
    regime_name: str,
    market_score: float,
    setup_score: float,
    instrument_score: float,
    history_score: float,
    average_day_pct: float | None,
    reference_close: float | None,
    support: float | None,
) -> float:
    adjustment = 0.0

    if (
        direction == "long"
        and regime_name in RANK_CALIBRATION_REVERSAL_REGIMES
        and market_score >= RANK_CALIBRATION_LONG_REVERSAL_MARKET_FLOOR
    ):
        adjustment -= RANK_CALIBRATION_LONG_REVERSAL_PENALTY

    if (
        regime_name == "mean_reversion"
        and history_score >= RANK_CALIBRATION_MEAN_REVERSION_HISTORY_FLOOR
        and instrument_score >= RANK_CALIBRATION_MEAN_REVERSION_INSTRUMENT_FLOOR
    ):
        adjustment += RANK_CALIBRATION_MEAN_REVERSION_BONUS

    if (
        direction == "short"
        and regime_name == "mean_reversion"
        and market_score >= RANK_CALIBRATION_SHORT_MEAN_REVERSION_MARKET_FLOOR
        and setup_score >= RANK_CALIBRATION_SHORT_MEAN_REVERSION_SETUP_FLOOR
        and instrument_score >= RANK_CALIBRATION_SHORT_MEAN_REVERSION_INSTRUMENT_FLOOR
        and history_score >= RANK_CALIBRATION_SHORT_MEAN_REVERSION_HISTORY_FLOOR
    ):
        adjustment += RANK_CALIBRATION_SHORT_MEAN_REVERSION_BONUS

    if (
        direction == "short"
        and regime_name == "mean_reversion"
        and market_score <= RANK_CALIBRATION_SHORT_MEAN_REVERSION_BULLISH_WEAK_MARKET_CEILING
        and setup_score >= RANK_CALIBRATION_SHORT_MEAN_REVERSION_BULLISH_WEAK_SETUP_FLOOR
        and instrument_score <= RANK_CALIBRATION_SHORT_MEAN_REVERSION_BULLISH_WEAK_INSTRUMENT_CEILING
        and history_score >= RANK_CALIBRATION_SHORT_MEAN_REVERSION_BULLISH_WEAK_HISTORY_FLOOR
    ):
        adjustment -= RANK_CALIBRATION_SHORT_MEAN_REVERSION_BULLISH_WEAK_PENALTY

    if (
        direction == "short"
        and regime_name == "mean_reversion"
        and RANK_CALIBRATION_SHORT_MEAN_REVERSION_NEUTRAL_STRONG_MARKET_FLOOR
        <= market_score
        <= RANK_CALIBRATION_SHORT_MEAN_REVERSION_NEUTRAL_STRONG_MARKET_CEILING
        and RANK_CALIBRATION_SHORT_MEAN_REVERSION_NEUTRAL_STRONG_SETUP_FLOOR
        <= setup_score
        <= RANK_CALIBRATION_SHORT_MEAN_REVERSION_NEUTRAL_STRONG_SETUP_CEILING
        and instrument_score >= RANK_CALIBRATION_SHORT_MEAN_REVERSION_NEUTRAL_STRONG_INSTRUMENT_FLOOR
        and RANK_CALIBRATION_SHORT_MEAN_REVERSION_NEUTRAL_STRONG_HISTORY_FLOOR
        <= history_score
        <= RANK_CALIBRATION_SHORT_MEAN_REVERSION_NEUTRAL_STRONG_HISTORY_CEILING
    ):
        adjustment += RANK_CALIBRATION_SHORT_MEAN_REVERSION_NEUTRAL_STRONG_BONUS

    if (
        direction == "long"
        and regime_name == "mean_reversion"
        and market_score >= RANK_CALIBRATION_LONG_BULLISH_MEAN_REVERSION_MARKET_FLOOR
        and setup_score >= RANK_CALIBRATION_LONG_BULLISH_MEAN_REVERSION_SETUP_FLOOR
        and instrument_score >= RANK_CALIBRATION_LONG_BULLISH_MEAN_REVERSION_INSTRUMENT_FLOOR
        and history_score >= RANK_CALIBRATION_LONG_BULLISH_MEAN_REVERSION_HISTORY_FLOOR
    ):
        adjustment += RANK_CALIBRATION_LONG_BULLISH_MEAN_REVERSION_BONUS

    if (
        direction == "long"
        and regime_name == "mean_reversion"
        and market_score <= RANK_CALIBRATION_LONG_MEAN_REVERSION_STRONG_INSTRUMENT_MARKET_CEILING
        and setup_score >= RANK_CALIBRATION_LONG_MEAN_REVERSION_STRONG_INSTRUMENT_SETUP_FLOOR
        and instrument_score >= RANK_CALIBRATION_LONG_MEAN_REVERSION_STRONG_INSTRUMENT_FLOOR
    ):
        adjustment += RANK_CALIBRATION_LONG_MEAN_REVERSION_STRONG_INSTRUMENT_BONUS

    if (
        direction == "short"
        and regime_name == "counter_impulse_reversal"
        and market_score >= RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_MID_BEARISH_MARKET_FLOOR
        and RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_MID_BEARISH_SETUP_FLOOR
        <= setup_score
        <= RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_MID_BEARISH_SETUP_CEILING
        and instrument_score >= RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_MID_BEARISH_INSTRUMENT_FLOOR
        and RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_MID_BEARISH_HISTORY_FLOOR
        <= history_score
        <= RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_MID_BEARISH_HISTORY_CEILING
    ):
        adjustment -= RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_MID_BEARISH_PENALTY

    if (
        direction == "short"
        and regime_name == "counter_impulse_reversal"
        and RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_NEUTRAL_MARKET_FLOOR
        <= market_score
        <= RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_NEUTRAL_MARKET_CEILING_2
        and RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_NEUTRAL_SETUP_FLOOR
        <= setup_score
        <= RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_NEUTRAL_SETUP_CEILING
        and RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_NEUTRAL_INSTRUMENT_FLOOR
        <= instrument_score
        <= RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_NEUTRAL_INSTRUMENT_CEILING
        and history_score >= RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_NEUTRAL_HISTORY_FLOOR
    ):
        adjustment -= RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_NEUTRAL_PENALTY

    if (
        direction == "short"
        and regime_name == "counter_impulse_reversal"
        and market_score <= RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_BULLISH_MARKET_CEILING
        and RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_BULLISH_SETUP_FLOOR
        <= setup_score
        <= RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_BULLISH_SETUP_CEILING
        and instrument_score <= RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_BULLISH_INSTRUMENT_CEILING
        and RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_BULLISH_HISTORY_FLOOR
        <= history_score
        <= RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_BULLISH_HISTORY_CEILING
    ):
        adjustment -= RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_BULLISH_PENALTY

    if (
        direction == "short"
        and regime_name == "counter_impulse_reversal"
        and market_score <= RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_STALE_MARKET_CEILING
        and RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_STALE_SETUP_FLOOR
        <= setup_score
        <= RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_STALE_SETUP_CEILING
        and instrument_score >= RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_STALE_INSTRUMENT_FLOOR
        and history_score >= RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_STALE_HISTORY_FLOOR
    ):
        adjustment -= RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_STALE_PENALTY

    if (
        direction == "short"
        and regime_name == "overshoot_reversal"
        and market_score <= RANK_CALIBRATION_SHORT_OVERSHOOT_WEAK_MARKET_CEILING
        and instrument_score < RANK_CALIBRATION_SHORT_OVERSHOOT_WEAK_INSTRUMENT_CEILING
        and history_score < RANK_CALIBRATION_SHORT_OVERSHOOT_WEAK_HISTORY_CEILING
    ):
        adjustment -= RANK_CALIBRATION_SHORT_OVERSHOOT_WEAK_PENALTY

    if (
        direction == "short"
        and regime_name == "capitulation_reversal"
        and market_score >= RANK_CALIBRATION_SHORT_CAPITULATION_HOT_MARKET_FLOOR
        and setup_score >= RANK_CALIBRATION_SHORT_CAPITULATION_HOT_SETUP_FLOOR
        and instrument_score >= RANK_CALIBRATION_SHORT_CAPITULATION_HOT_INSTRUMENT_FLOOR
        and history_score >= RANK_CALIBRATION_SHORT_CAPITULATION_HOT_HISTORY_FLOOR
    ):
        adjustment -= RANK_CALIBRATION_SHORT_CAPITULATION_HOT_PENALTY

    if (
        direction == "long"
        and support is not None
        and reference_close is not None
        and average_day_pct is not None
        and average_day_pct > 0.0
    ):
        average_day_move = reference_close * average_day_pct / 100.0
        if average_day_move > 0.0:
            support_gap_ratio = (support - reference_close) / average_day_move
            if support_gap_ratio > RANK_CALIBRATION_LONG_EXTREME_BROKEN_SUPPORT_RATIO:
                adjustment -= RANK_CALIBRATION_LONG_EXTREME_BROKEN_SUPPORT_PENALTY
            elif (
                regime_name == "mean_reversion"
                and support_gap_ratio > RANK_CALIBRATION_LONG_MEAN_REVERSION_BROKEN_SUPPORT_RATIO
            ):
                adjustment -= RANK_CALIBRATION_LONG_MEAN_REVERSION_BROKEN_SUPPORT_PENALTY

    if (
        direction == "short"
        and regime_name == "counter_impulse_reversal"
        and market_score < RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_NEUTRAL_MARKET_CEILING
        and (setup_score - instrument_score) > RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_SETUP_GAP
    ):
        adjustment -= RANK_CALIBRATION_SHORT_COUNTER_IMPULSE_PENALTY

    if (
        direction == "long"
        and regime_name in RANK_CALIBRATION_REVERSAL_REGIMES
        and market_score >= RANK_CALIBRATION_LONG_REVERSAL_MARKET_FLOOR
        and setup_score < RANK_CALIBRATION_LONG_STRONG_REVERSAL_SETUP_CEILING
        and history_score < RANK_CALIBRATION_LONG_STRONG_REVERSAL_HISTORY_CEILING
    ):
        adjustment -= RANK_CALIBRATION_LONG_STRONG_REVERSAL_PENALTY

    if (
        direction == "long"
        and regime_name == "counter_impulse_reversal"
        and RANK_CALIBRATION_LONG_COUNTER_IMPULSE_WEAK_MARKET_FLOOR
        <= market_score
        <= RANK_CALIBRATION_LONG_COUNTER_IMPULSE_WEAK_MARKET_CEILING
        and setup_score <= RANK_CALIBRATION_LONG_COUNTER_IMPULSE_WEAK_SETUP_CEILING
        and instrument_score >= RANK_CALIBRATION_LONG_COUNTER_IMPULSE_WEAK_INSTRUMENT_FLOOR
        and history_score >= RANK_CALIBRATION_LONG_COUNTER_IMPULSE_WEAK_HISTORY_FLOOR
    ):
        adjustment -= RANK_CALIBRATION_LONG_COUNTER_IMPULSE_WEAK_PENALTY

    if (
        direction == "long"
        and regime_name == "compressed_level_reversal"
        and market_score >= RANK_CALIBRATION_LONG_REVERSAL_MARKET_FLOOR
        and history_score < RANK_CALIBRATION_LONG_COMPRESSED_LEVEL_HISTORY_CEILING
    ):
        adjustment -= RANK_CALIBRATION_LONG_COMPRESSED_LEVEL_PENALTY

    if (
        direction == "long"
        and regime_name in RANK_CALIBRATION_REVERSAL_REGIMES
        and market_score <= RANK_CALIBRATION_LONG_NEUTRAL_REVERSAL_MARKET_CEILING
        and setup_score < RANK_CALIBRATION_LONG_NEUTRAL_REVERSAL_SETUP_CEILING
        and history_score < RANK_CALIBRATION_LONG_NEUTRAL_REVERSAL_HISTORY_CEILING
    ):
        adjustment -= RANK_CALIBRATION_LONG_NEUTRAL_REVERSAL_PENALTY

    return adjustment


def calculate_one_day_rank_adjustment(
    *,
    best_target: TargetChoice,
    regime_name: str,
    setup_score: float,
    instrument_score: float,
    history_score: float,
    market_score: float,
    entry_quality_score: float,
    relative_strength_score: float,
    volume_quality_score: float,
    freshness_score: float,
    prob_hit_t1: float,
    risk_complete_miss: float,
    reversion_indicator_score: float,
    anchor_kind: str | None = None,
    post_impulse_state: str = "neutral",
) -> float:
    adjustment = 0.0
    execution_quality = 0.45 * setup_score + 0.35 * instrument_score + 0.20 * history_score
    adjustment += clamp((execution_quality - 60.0) / 16.0, -1.0, 1.0) * 5.0
    adjustment += clamp((prob_hit_t1 - 0.56) / 0.18, -1.0, 1.0) * 7.0
    adjustment -= clamp((risk_complete_miss - 0.40) / 0.25, 0.0, 1.0) * 8.0

    live_readiness = clamp(
        0.38 * entry_quality_score
        + 0.26 * freshness_score
        + 0.24 * relative_strength_score
        + 0.12 * market_score,
        0.0,
        100.0,
    )
    adjustment += (
        clamp(
            (live_readiness - RANK_CALIBRATION_ONE_DAY_LIVE_READINESS_BASELINE)
            / RANK_CALIBRATION_ONE_DAY_LIVE_READINESS_SPAN,
            -1.0,
            1.0,
        )
        * RANK_CALIBRATION_ONE_DAY_LIVE_READINESS_BONUS
    )

    structural_strength = 0.42 * setup_score + 0.34 * instrument_score + 0.24 * history_score
    structural_imbalance = structural_strength - live_readiness
    if best_target.label in {"T1", "T2"}:
        imbalance_penalty = clamp(
            (structural_imbalance - RANK_CALIBRATION_ONE_DAY_STRUCTURAL_IMBALANCE_GAP)
            / RANK_CALIBRATION_ONE_DAY_STRUCTURAL_IMBALANCE_SPAN,
            0.0,
            1.0,
        )
        adjustment -= imbalance_penalty * RANK_CALIBRATION_ONE_DAY_STRUCTURAL_IMBALANCE_PENALTY
        if regime_name in ONE_DAY_STALE_EXPLICIT_LEVEL_REGIMES and anchor_kind == "explicit_level":
            adjustment -= imbalance_penalty * 2.5

    if best_target.label == "T3":
        adjustment -= 2.5
    elif best_target.label == "T4":
        adjustment -= 6.5

    if regime_name in {
        "counter_impulse_reversal",
        "compressed_level_reversal",
        "micro_overshoot_reversal",
        "mean_reversion",
        "capitulation_reversal",
        "absorption_reversal",
        "level_rejection",
        "pre_level_rejection",
        "sticky_level_reversal",
        "fragile_breakout_reversal",
        "overshoot_reversal",
        "extended_overshoot_reversal",
    }:
        if best_target.label in {"T3", "T4"}:
            adjustment -= 3.5
        if prob_hit_t1 < 0.55:
            adjustment -= 3.0

    if (
        best_target.label in {"T1", "T2"}
        and prob_hit_t1 >= 0.60
        and setup_score >= 55.0
        and instrument_score >= 55.0
    ):
        adjustment += 3.5

    if history_score < 45.0:
        adjustment -= min((45.0 - history_score) * 0.10, 4.0)

    if (
        regime_name == "mean_reversion"
        and best_target.label in {"T1", "T2"}
        and entry_quality_score >= RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_FRESH_ENTRY_QUALITY_FLOOR
        and history_score <= RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_FRESH_HISTORY_CEILING
        and instrument_score >= RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_FRESH_INSTRUMENT_FLOOR
        and freshness_score >= RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_FRESH_FRESHNESS_FLOOR
        and volume_quality_score >= RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_FRESH_VOLUME_FLOOR
    ):
        adjustment += RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_FRESH_BONUS

    if (
        regime_name == "counter_impulse_reversal"
        and best_target.label in {"T1", "T2"}
        and setup_score <= RANK_CALIBRATION_ONE_DAY_COUNTER_IMPULSE_WEAK_SETUP_CEILING
        and market_score <= RANK_CALIBRATION_ONE_DAY_COUNTER_IMPULSE_WEAK_MARKET_CEILING
        and history_score >= RANK_CALIBRATION_ONE_DAY_COUNTER_IMPULSE_WEAK_HISTORY_FLOOR
        and prob_hit_t1 >= RANK_CALIBRATION_ONE_DAY_COUNTER_IMPULSE_WEAK_PROB_FLOOR
    ):
        adjustment -= RANK_CALIBRATION_ONE_DAY_COUNTER_IMPULSE_WEAK_PENALTY

    if regime_name == "mean_reversion" and best_target.label == "T2":
        fresh_edge = clamp(
            (instrument_score - history_score - RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_EDGE_GAP)
            / RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_EDGE_SPAN,
            0.0,
            1.0,
        )
        setup_confirmation = clamp(
            (setup_score - RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_SETUP_FLOOR)
            / RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_SETUP_SPAN,
            0.0,
            1.0,
        )
        market_tailwind = clamp(
            (market_score - 50.0) / RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_MARKET_SPAN,
            0.0,
            1.0,
        )
        explicit_level = 1.0 if anchor_kind == "explicit_level" else 0.0
        adjustment += fresh_edge * (
            RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_EDGE_BONUS * (0.55 + 0.45 * setup_confirmation)
            + RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_MARKET_BONUS * market_tailwind
            + RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_EXPLICIT_LEVEL_BONUS * explicit_level
        )

    if (
        best_target.label in {"T1", "T2"}
        and anchor_kind == "explicit_level"
        and post_impulse_state == "neutral"
        and regime_name in ONE_DAY_STALE_EXPLICIT_LEVEL_REGIMES
    ):
        freshness_deficit = clamp(
            (RANK_CALIBRATION_ONE_DAY_STALE_LEVEL_FRESHNESS_CEILING - freshness_score) / 20.0,
            0.0,
            1.0,
        )
        entry_deficit = clamp(
            (RANK_CALIBRATION_ONE_DAY_STALE_LEVEL_ENTRY_CEILING - entry_quality_score) / 18.0,
            0.0,
            1.0,
        )
        paper_gap = max(setup_score, instrument_score, history_score) - entry_quality_score
        paper_gap_pressure = clamp(
            (paper_gap - RANK_CALIBRATION_ONE_DAY_STALE_LEVEL_PAPER_GAP)
            / RANK_CALIBRATION_ONE_DAY_STALE_LEVEL_PAPER_GAP_SPAN,
            0.0,
            1.0,
        )
        stale_level_penalty = RANK_CALIBRATION_ONE_DAY_STALE_LEVEL_PENALTY * (
            0.35 * freshness_deficit
            + 0.25 * entry_deficit
            + 0.40 * paper_gap_pressure
        )
        if regime_name == "counter_impulse_reversal":
            stale_level_penalty += 2.0 * clamp((history_score - 55.0) / 20.0, 0.0, 1.0)
        adjustment -= stale_level_penalty

    if (
        best_target.label == "T2"
        and anchor_kind == "explicit_level"
        and entry_quality_score <= RANK_CALIBRATION_ONE_DAY_EXPLICIT_RESCUE_ENTRY_CEILING
        and setup_score >= RANK_CALIBRATION_ONE_DAY_EXPLICIT_RESCUE_SETUP_FLOOR
        and instrument_score >= RANK_CALIBRATION_ONE_DAY_EXPLICIT_RESCUE_INSTRUMENT_FLOOR
        and prob_hit_t1 >= RANK_CALIBRATION_ONE_DAY_EXPLICIT_RESCUE_PROB_FLOOR
        and risk_complete_miss <= RANK_CALIBRATION_ONE_DAY_EXPLICIT_RESCUE_MISS_CEILING
    ):
        adjustment += RANK_CALIBRATION_ONE_DAY_EXPLICIT_RESCUE_BONUS

    if (
        regime_name == "level_rejection"
        and best_target.label == "T2"
        and anchor_kind == "explicit_level"
        and freshness_score >= RANK_CALIBRATION_ONE_DAY_LEVEL_REJECTION_RESCUE_FRESHNESS_FLOOR
        and setup_score >= RANK_CALIBRATION_ONE_DAY_LEVEL_REJECTION_RESCUE_SETUP_FLOOR
        and prob_hit_t1 >= RANK_CALIBRATION_ONE_DAY_LEVEL_REJECTION_RESCUE_PROB_FLOOR
    ):
        adjustment += RANK_CALIBRATION_ONE_DAY_LEVEL_REJECTION_RESCUE_BONUS

    if (
        regime_name == "mean_reversion"
        and best_target.label == "T2"
        and anchor_kind == "rolling_pivot_high"
        and entry_quality_score >= RANK_CALIBRATION_ONE_DAY_PIVOT_HIGH_FAST_ENTRY_FLOOR
        and freshness_score >= RANK_CALIBRATION_ONE_DAY_PIVOT_HIGH_FAST_FRESHNESS_FLOOR
        and setup_score >= RANK_CALIBRATION_ONE_DAY_PIVOT_HIGH_FAST_SETUP_FLOOR
        and history_score <= RANK_CALIBRATION_ONE_DAY_PIVOT_HIGH_FAST_HISTORY_CEILING
        and instrument_score >= RANK_CALIBRATION_ONE_DAY_PIVOT_HIGH_FAST_INSTRUMENT_FLOOR
    ):
        adjustment += RANK_CALIBRATION_ONE_DAY_PIVOT_HIGH_FAST_BONUS

    if (
        regime_name == "mean_reversion"
        and best_target.label == "T2"
        and anchor_kind == "rolling_pivot_low"
        and setup_score >= RANK_CALIBRATION_ONE_DAY_PIVOT_LOW_STALE_SETUP_FLOOR
        and history_score >= RANK_CALIBRATION_ONE_DAY_PIVOT_LOW_STALE_HISTORY_FLOOR
        and prob_hit_t1 < RANK_CALIBRATION_ONE_DAY_PIVOT_LOW_STALE_PROB_CEILING
    ):
        adjustment -= RANK_CALIBRATION_ONE_DAY_PIVOT_LOW_STALE_PENALTY

    fresh_setup_bonus = (
        clamp((setup_score - 62.0) / 18.0, 0.0, 1.0)
        * (0.45 + 0.55 * clamp((instrument_score - 50.0) / 18.0, 0.0, 1.0))
        * clamp((52.0 - history_score) / 22.0, 0.0, 1.0)
    )
    if best_target.label == "T2":
        adjustment += 6.0 * fresh_setup_bonus
    elif best_target.label == "T1":
        adjustment += 3.5 * fresh_setup_bonus

    return clamp(adjustment, -20.0, 12.0)


def calculate_one_day_live_leader_bonus(
    *,
    best_target: TargetChoice,
    regime_name: str,
    setup_score: float,
    history_score: float,
    entry_quality_score: float,
    freshness_score: float,
    anchor_kind: str | None,
) -> float:
    if (
        regime_name == "mean_reversion"
        and best_target.label == "T2"
        and anchor_kind == "rolling_pivot_high"
        and setup_score >= RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_LIVE_SETUP_FLOOR
        and entry_quality_score >= RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_LIVE_ENTRY_QUALITY_FLOOR
        and freshness_score >= RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_LIVE_FRESHNESS_FLOOR
        and history_score <= RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_LIVE_HISTORY_CEILING
    ):
        return RANK_CALIBRATION_ONE_DAY_MEAN_REVERSION_LIVE_DIRECT_BONUS
    return 0.0


def calculate_reference_close_delta_pct(reference_close: float | None, market_close: float) -> float | None:
    if reference_close is None or market_close <= 0.0:
        return None
    return abs(reference_close / market_close - 1.0) * 100.0


def load_same_day_top_reranker_model() -> SameDayTopRerankerModel | None:
    global _same_day_top_reranker_checked, _same_day_top_reranker_model

    if _same_day_top_reranker_checked:
        return _same_day_top_reranker_model

    model_path = os.path.join(
        str(MODEL_DIR),
        SAME_DAY_TOP_RERANKER_MODEL_FILENAME,
    )
    if not os.path.isfile(model_path):
        _same_day_top_reranker_checked = True
        return None

    with open(model_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    numeric_features = tuple(str(item) for item in payload["numeric_features"])
    binary_features = tuple(str(item) for item in payload["binary_features"])
    regimes = tuple(str(item) for item in payload["regimes"])
    anchor_kinds = tuple(str(item) for item in payload["anchor_kinds"])
    directions = tuple(str(item) for item in payload["directions"])
    post_impulse_states = tuple(str(item) for item in payload["post_impulse_states"])
    best_targets = tuple(str(item) for item in payload["best_targets"])
    scaler_mean = tuple(float(item) for item in payload["scaler_mean"])
    scaler_scale = tuple(float(item) for item in payload["scaler_scale"])
    coefficients = tuple(float(item) for item in payload["coefficients"])
    feature_count = (
        len(numeric_features)
        + len(binary_features)
        + len(regimes)
        + len(anchor_kinds)
        + len(directions)
        + len(post_impulse_states)
        + len(best_targets)
    )
    if not (
        len(scaler_mean) == len(scaler_scale) == len(coefficients) == feature_count
    ):
        raise RuntimeError("same_day_top_reranker_model.json has inconsistent feature dimensions.")

    override_payload = payload["override"]
    _same_day_top_reranker_model = SameDayTopRerankerModel(
        numeric_features=numeric_features,
        binary_features=binary_features,
        regimes=regimes,
        anchor_kinds=anchor_kinds,
        directions=directions,
        post_impulse_states=post_impulse_states,
        best_targets=best_targets,
        scaler_mean=scaler_mean,
        scaler_scale=scaler_scale,
        coefficients=coefficients,
        intercept=float(payload["intercept"]),
        topk=int(override_payload["topk"]),
        max_overall_gap=float(override_payload["max_overall_gap"]),
        min_prob_gap=float(override_payload["min_prob_gap"]),
        top_prob_ceiling=float(override_payload["top_prob_ceiling"]),
        min_alt_prob=float(override_payload["min_alt_prob"]),
        regime_index={name: index for index, name in enumerate(regimes)},
        anchor_kind_index={name: index for index, name in enumerate(anchor_kinds)},
        direction_index={name: index for index, name in enumerate(directions)},
        post_impulse_state_index={name: index for index, name in enumerate(post_impulse_states)},
        best_target_index={name: index for index, name in enumerate(best_targets)},
    )
    # Флаг выставляется только ПОСЛЕ присвоения модели: иначе параллельный
    # поток между двумя записями видит (checked=True, model=None) и молча
    # работает без reranker'а.
    _same_day_top_reranker_checked = True
    return _same_day_top_reranker_model


def load_same_day_top_winner_reranker_model() -> SameDayTopRerankerModel | None:
    global _same_day_top_winner_reranker_checked, _same_day_top_winner_reranker_model

    if _same_day_top_winner_reranker_checked:
        return _same_day_top_winner_reranker_model

    model_path = os.path.join(
        str(MODEL_DIR),
        SAME_DAY_TOP_WINNER_RERANKER_MODEL_FILENAME,
    )
    if not os.path.isfile(model_path):
        _same_day_top_winner_reranker_checked = True
        return None

    with open(model_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    numeric_features = tuple(str(item) for item in payload["numeric_features"])
    binary_features = tuple(str(item) for item in payload["binary_features"])
    regimes = tuple(str(item) for item in payload["regimes"])
    anchor_kinds = tuple(str(item) for item in payload["anchor_kinds"])
    directions = tuple(str(item) for item in payload["directions"])
    post_impulse_states = tuple(str(item) for item in payload["post_impulse_states"])
    best_targets = tuple(str(item) for item in payload["best_targets"])

    override_payload = payload["override"]
    _same_day_top_winner_reranker_model = SameDayTopRerankerModel(
        numeric_features=numeric_features,
        binary_features=binary_features,
        regimes=regimes,
        anchor_kinds=anchor_kinds,
        directions=directions,
        post_impulse_states=post_impulse_states,
        best_targets=best_targets,
        scaler_mean=tuple(float(item) for item in payload["scaler_mean"]),
        scaler_scale=tuple(float(item) for item in payload["scaler_scale"]),
        coefficients=tuple(float(item) for item in payload["coefficients"]),
        intercept=float(payload["intercept"]),
        topk=int(override_payload["topk"]),
        max_overall_gap=float(override_payload["max_overall_gap"]),
        min_prob_gap=float(override_payload["min_prob_gap"]),
        top_prob_ceiling=float(override_payload["top_prob_ceiling"]),
        min_alt_prob=float(override_payload["min_alt_prob"]),
        regime_index={name: index for index, name in enumerate(regimes)},
        anchor_kind_index={name: index for index, name in enumerate(anchor_kinds)},
        direction_index={name: index for index, name in enumerate(directions)},
        post_impulse_state_index={name: index for index, name in enumerate(post_impulse_states)},
        best_target_index={name: index for index, name in enumerate(best_targets)},
    )
    _same_day_top_winner_reranker_checked = True
    return _same_day_top_winner_reranker_model


def load_same_day_top_t2plus_reranker_model() -> SameDayTopRerankerModel | None:
    global _same_day_top_t2plus_reranker_checked, _same_day_top_t2plus_reranker_model

    if _same_day_top_t2plus_reranker_checked:
        return _same_day_top_t2plus_reranker_model

    model_path = os.path.join(
        str(MODEL_DIR),
        SAME_DAY_TOP_T2PLUS_RERANKER_MODEL_FILENAME,
    )
    if not os.path.isfile(model_path):
        _same_day_top_t2plus_reranker_checked = True
        return None

    with open(model_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    numeric_features = tuple(str(item) for item in payload["numeric_features"])
    binary_features = tuple(str(item) for item in payload["binary_features"])
    regimes = tuple(str(item) for item in payload["regimes"])
    anchor_kinds = tuple(str(item) for item in payload["anchor_kinds"])
    directions = tuple(str(item) for item in payload["directions"])
    post_impulse_states = tuple(str(item) for item in payload["post_impulse_states"])
    best_targets = tuple(str(item) for item in payload["best_targets"])

    override_payload = payload["override"]
    _same_day_top_t2plus_reranker_model = SameDayTopRerankerModel(
        numeric_features=numeric_features,
        binary_features=binary_features,
        regimes=regimes,
        anchor_kinds=anchor_kinds,
        directions=directions,
        post_impulse_states=post_impulse_states,
        best_targets=best_targets,
        scaler_mean=tuple(float(item) for item in payload["scaler_mean"]),
        scaler_scale=tuple(float(item) for item in payload["scaler_scale"]),
        coefficients=tuple(float(item) for item in payload["coefficients"]),
        intercept=float(payload["intercept"]),
        topk=int(override_payload["topk"]),
        max_overall_gap=float(override_payload["max_overall_gap"]),
        min_prob_gap=float(override_payload["min_prob_gap"]),
        top_prob_ceiling=float(override_payload["top_prob_ceiling"]),
        min_alt_prob=float(override_payload["min_alt_prob"]),
        regime_index={name: index for index, name in enumerate(regimes)},
        anchor_kind_index={name: index for index, name in enumerate(anchor_kinds)},
        direction_index={name: index for index, name in enumerate(directions)},
        post_impulse_state_index={name: index for index, name in enumerate(post_impulse_states)},
        best_target_index={name: index for index, name in enumerate(best_targets)},
    )
    _same_day_top_t2plus_reranker_checked = True
    return _same_day_top_t2plus_reranker_model


RUNNER_DAY_FEATURES = (
    "atr_expansion",
    "atr20_pct",
    "range_pos_dir",
    "dist_ema20_dir",
    "dmom5",
    "dmom10",
    "recent_expansion",
    "body_ratio",
    "consec_dir",
    "vol_trend",
    "avg_day_pct",
    "rr_t4",
    "rr_t2",
)


def _runner_atr_pct(sessions: list[SessionData], period: int) -> float:
    if len(sessions) < period + 1:
        period = len(sessions) - 1
    if period <= 0:
        return 0.0
    true_ranges = []
    for i in range(len(sessions) - period, len(sessions)):
        prev_close = sessions[i - 1].close
        true_ranges.append(
            max(
                sessions[i].high - sessions[i].low,
                abs(sessions[i].high - prev_close),
                abs(sessions[i].low - prev_close),
            )
        )
    last_close = sessions[-1].close
    return (sum(true_ranges) / len(true_ranges)) / last_close * 100.0 if last_close else 0.0


def compute_runner_day_features(
    sessions: list[SessionData],
    idea: WatchIdea,
    direction: str,
    reference_close: float,
) -> dict[str, float] | None:
    """Фичи runner-модели (та же математика, что в обучающем скрипте — менять синхронно)."""
    if len(sessions) < 21 or not idea.targets or reference_close <= 0.0:
        return None

    closes = [s.close for s in sessions]
    highs = [s.high for s in sessions]
    lows = [s.low for s in sessions]
    volumes = [s.volume for s in sessions]
    sign = 1.0 if direction == "long" else -1.0

    atr5 = _runner_atr_pct(sessions, 5)
    atr20 = _runner_atr_pct(sessions, 20)
    hi20 = max(highs[-20:])
    lo20 = min(lows[-20:])
    range_span = max(hi20 - lo20, 1e-9)
    range_pos = (closes[-1] - lo20) / range_span
    ema20 = calculate_ema(closes[-20:], 20)
    day_ranges = [
        (highs[i] - lows[i]) / closes[i] * 100.0
        for i in range(len(sessions) - 20, len(sessions))
        if closes[i]
    ]
    avg_day = sum(day_ranges) / len(day_ranges) if day_ranges else 0.01
    last3 = [
        (highs[i] - lows[i]) / closes[i] * 100.0
        for i in range(len(sessions) - 3, len(sessions))
        if closes[i]
    ]
    bodies = [
        abs(sessions[i].close - sessions[i].open) / max(sessions[i].high - sessions[i].low, 1e-9)
        for i in range(len(sessions) - 5, len(sessions))
    ]
    consecutive = 0
    for i in range(len(sessions) - 1, 0, -1):
        if (sessions[i].close - sessions[i].open) * sign > 0:
            consecutive += 1
        else:
            break

    max_target = max(idea.targets, key=lambda target: target.index).price
    t2_price = next((target.price for target in idea.targets if target.index == 2), max_target)

    return {
        "atr_expansion": atr5 / max(atr20, 1e-9),
        "atr20_pct": atr20,
        "range_pos_dir": range_pos if direction == "long" else 1.0 - range_pos,
        "dist_ema20_dir": (closes[-1] / max(ema20, 1e-9) - 1.0) * 100.0 * sign,
        "dmom5": (closes[-1] / closes[-6] - 1.0) * 100.0 * sign if len(closes) > 6 else 0.0,
        "dmom10": (closes[-1] / closes[-11] - 1.0) * 100.0 * sign if len(closes) > 11 else 0.0,
        "recent_expansion": (sum(last3) / len(last3) if last3 else 0.0) / max(avg_day, 1e-9),
        "body_ratio": sum(bodies) / len(bodies),
        "consec_dir": float(consecutive),
        "vol_trend": (sum(volumes[-5:]) / 5.0) / max(sum(volumes[-20:]) / 20.0, 1e-9),
        "avg_day_pct": avg_day,
        "rr_t4": abs(max_target / reference_close - 1.0) * 100.0 / STOP_LOSS_PCT,
        "rr_t2": abs(t2_price / reference_close - 1.0) * 100.0 / STOP_LOSS_PCT,
    }


def load_runner_day_confidence_model() -> RunnerDayConfidenceModel | None:
    global _runner_day_confidence_checked, _runner_day_confidence_model

    if _runner_day_confidence_checked:
        return _runner_day_confidence_model

    model_path = os.path.join(
        str(MODEL_DIR),
        RUNNER_DAY_CONFIDENCE_MODEL_FILENAME,
    )
    if not os.path.isfile(model_path):
        _runner_day_confidence_checked = True
        return None

    with open(model_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    features = tuple(str(item) for item in payload["features"])
    scaler_mean = tuple(float(item) for item in payload["scaler_mean"])
    scaler_scale = tuple(float(item) for item in payload["scaler_scale"])
    coefficients = tuple(float(item) for item in payload["coefficients"])
    if not (len(features) == len(scaler_mean) == len(scaler_scale) == len(coefficients)):
        raise RuntimeError(f"{RUNNER_DAY_CONFIDENCE_MODEL_FILENAME} has inconsistent feature dimensions.")

    _runner_day_confidence_model = RunnerDayConfidenceModel(
        features=features,
        scaler_mean=scaler_mean,
        scaler_scale=scaler_scale,
        coefficients=coefficients,
        intercept=float(payload["intercept"]),
        threshold=float(payload["threshold"]),
        keep_fraction=float(payload["keep_fraction"]),
        threshold_long=float(payload.get("threshold_long", payload["threshold"])),
    )
    _runner_day_confidence_checked = True
    return _runner_day_confidence_model


def score_runner_day_confidence(features: dict[str, float]) -> float | None:
    model = load_runner_day_confidence_model()
    if model is None:
        return None
    logit = model.intercept
    for name, mean, scale, coefficient in zip(
        model.features, model.scaler_mean, model.scaler_scale, model.coefficients
    ):
        if name not in features:
            return None
        logit += coefficient * (features[name] - mean) / (scale if scale else 1.0)
    return 1.0 / (1.0 + math.exp(-clamp(logit, -30.0, 30.0)))


def runner_day_confidence_threshold(direction: str = "short") -> float | None:
    env_name = (
        "WBT_DAY_CONFIDENCE_THRESHOLD_LONG" if direction == "long" else "WBT_DAY_CONFIDENCE_THRESHOLD"
    )
    override = os.environ.get(env_name)
    if override:
        try:
            return float(override)
        except ValueError:
            pass
    model = load_runner_day_confidence_model()
    if model is None:
        return None
    if direction == "long" and model.threshold_long > 0.0:
        return model.threshold_long
    return model.threshold


def encode_same_day_top_reranker_features(
    item: IdeaAnalysis,
    model: SameDayTopRerankerModel,
    *,
    top_item: IdeaAnalysis | None = None,
    rank: int = 1,
) -> list[float] | None:
    if item.skipped_reason:
        return None

    top_reference = top_item or item
    numeric_values: list[float] = []
    for name in model.numeric_features:
        if name == "candidate_rank":
            numeric_values.append(float(rank))
            continue
        if name == "overall_gap_vs_top":
            numeric_values.append(item.overall_score - top_reference.overall_score)
            continue
        if name == "setup_gap_vs_top":
            numeric_values.append(item.setup_score - top_reference.setup_score)
            continue
        if name == "instrument_gap_vs_top":
            numeric_values.append(item.instrument_score - top_reference.instrument_score)
            continue
        if name == "history_gap_vs_top":
            numeric_values.append(item.history_score - top_reference.history_score)
            continue
        if name == "entry_gap_vs_top":
            numeric_values.append(item.entry_quality_score - top_reference.entry_quality_score)
            continue
        if name == "freshness_gap_vs_top":
            numeric_values.append(item.freshness_score - top_reference.freshness_score)
            continue
        if name == "rs_gap_vs_top":
            numeric_values.append(item.relative_strength_score - top_reference.relative_strength_score)
            continue
        if name == "prob_t1_gap_vs_top":
            numeric_values.append(item.prob_hit_t1 - top_reference.prob_hit_t1)
            continue
        if name == "prob_t3_gap_vs_top":
            numeric_values.append(item.prob_hit_t3 - top_reference.prob_hit_t3)
            continue
        if name == "expmax_gap_vs_top":
            numeric_values.append(item.expected_max_target - top_reference.expected_max_target)
            continue
        if name == "utility_gap_vs_top":
            numeric_values.append(item.raw_expected_utility_score - top_reference.raw_expected_utility_score)
            continue
        if name == "missrisk_improvement_vs_top":
            numeric_values.append(top_reference.risk_complete_miss - item.risk_complete_miss)
            continue
        if name == "same_direction_as_top":
            numeric_values.append(1.0 if item.direction == top_reference.direction else 0.0)
            continue
        if name == "same_anchor_as_top":
            numeric_values.append(1.0 if item.anchor_kind == top_reference.anchor_kind else 0.0)
            continue
        if name == "same_regime_as_top":
            numeric_values.append(1.0 if item.regime == top_reference.regime else 0.0)
            continue
        numeric_values.append(float(getattr(item, name)))

    values = numeric_values
    values.extend(
        [
            1.0 if item.anchor_kind == "explicit_level" else 0.0,
            1.0 if item.anchor_kind in ONE_DAY_PIVOT_ANCHOR_KINDS else 0.0,
            1.0 if item.regime in ONE_DAY_OVERRIDE_REVERSAL_REGIMES else 0.0,
            1.0 if item.regime == "mean_reversion" else 0.0,
            1.0 if item.post_impulse_state == "neutral" else 0.0,
            1.0 if item.best_target.label == "T2" else 0.0,
        ]
    )

    categorical_slots: tuple[tuple[str, tuple[str, ...], dict[str, int]], ...] = (
        (item.regime, model.regimes, model.regime_index),
        (item.anchor_kind or "none", model.anchor_kinds, model.anchor_kind_index),
        (item.direction, model.directions, model.direction_index),
        (item.post_impulse_state, model.post_impulse_states, model.post_impulse_state_index),
        (item.best_target.label, model.best_targets, model.best_target_index),
    )
    for raw_value, categories, index_map in categorical_slots:
        one_hot = [0.0] * len(categories)
        category_index = index_map.get(raw_value)
        if category_index is not None:
            one_hot[category_index] = 1.0
        values.extend(one_hot)

    return values


def predict_same_day_reranker_probability(
    item: IdeaAnalysis,
    model: SameDayTopRerankerModel,
    *,
    top_item: IdeaAnalysis | None = None,
    rank: int = 1,
) -> float | None:
    features = encode_same_day_top_reranker_features(item, model, top_item=top_item, rank=rank)
    if features is None:
        return None

    logit = model.intercept
    for value, mean, scale, coefficient in zip(
        features,
        model.scaler_mean,
        model.scaler_scale,
        model.coefficients,
    ):
        standardized = (value - mean) / scale if scale > 0.0 else value - mean
        logit += coefficient * standardized

    if logit >= 0.0:
        exp_term = math.exp(-logit)
        return 1.0 / (1.0 + exp_term)
    exp_term = math.exp(logit)
    return exp_term / (1.0 + exp_term)


def predict_same_day_any_target_probability(
    item: IdeaAnalysis,
    model: SameDayTopRerankerModel,
    *,
    top_item: IdeaAnalysis | None = None,
    rank: int = 1,
) -> float | None:
    return predict_same_day_reranker_probability(
        item,
        model,
        top_item=top_item,
        rank=rank,
    )


def apply_learned_same_day_top_reranker(analyses: list[IdeaAnalysis]) -> list[IdeaAnalysis]:
    model = load_same_day_top_reranker_model()
    if model is None or len(analyses) < 2:
        return analyses

    top = analyses[0]
    for rank, item in enumerate(analyses, start=1):
        item.reranker_any_target_prob = predict_same_day_any_target_probability(
            item,
            model,
            top_item=top,
            rank=rank,
        )

    top_probability = top.reranker_any_target_prob
    if top_probability is None or top_probability > model.top_prob_ceiling:
        return analyses

    candidates = [
        candidate
        for candidate in analyses[1 : min(len(analyses), model.topk)]
        if candidate.reranker_any_target_prob is not None
        and top.overall_score - candidate.overall_score <= model.max_overall_gap
        and candidate.reranker_any_target_prob >= model.min_alt_prob
        and candidate.reranker_any_target_prob - top_probability >= model.min_prob_gap
    ]
    if not candidates:
        return analyses

    best_candidate = max(
        candidates,
        key=lambda item: (item.reranker_any_target_prob or 0.0, item.overall_score),
    )
    promote_same_day_override_candidate(analyses, best_candidate, top.overall_score)
    return analyses


def apply_learned_same_day_winner_reranker(analyses: list[IdeaAnalysis]) -> list[IdeaAnalysis]:
    model = load_same_day_top_winner_reranker_model()
    if model is None or len(analyses) < 2:
        return analyses

    analyses = sorted(analyses, key=lambda item: item.overall_score, reverse=True)
    top = analyses[0]
    if top.skipped_reason:
        return analyses

    for rank, item in enumerate(analyses, start=1):
        item.reranker_winner_prob = predict_same_day_reranker_probability(
            item,
            model,
            top_item=top,
            rank=rank,
        )

    top_probability = top.reranker_winner_prob
    if top_probability is None or top_probability > SAME_DAY_WINNER_RERANKER_TOP_PROB_CEILING:
        return analyses

    scan_limit = min(len(analyses), SAME_DAY_WINNER_RERANKER_SCAN)
    candidates = [
        candidate
        for candidate in analyses[1:scan_limit]
        if not candidate.skipped_reason
        and candidate.reranker_winner_prob is not None
        and candidate.reranker_winner_prob >= SAME_DAY_WINNER_RERANKER_MIN_ALT_PROB
        and candidate.reranker_winner_prob - top_probability >= SAME_DAY_WINNER_RERANKER_MIN_PROB_GAP
        and top.overall_score - candidate.overall_score <= SAME_DAY_WINNER_RERANKER_MAX_OVERALL_GAP
        and candidate.risk_complete_miss <= top.risk_complete_miss + SAME_DAY_WINNER_RERANKER_MISS_RISK_TOLERANCE
    ]
    if not candidates:
        return analyses

    best_candidate = max(
        candidates,
        key=lambda item: (
            item.reranker_winner_prob or 0.0,
            item.expected_max_target,
            item.prob_hit_t3,
            item.overall_score,
        ),
    )
    promote_same_day_override_candidate(analyses, best_candidate, top.overall_score)
    return analyses


def apply_learned_same_day_t2plus_reranker(analyses: list[IdeaAnalysis]) -> list[IdeaAnalysis]:
    model = load_same_day_top_t2plus_reranker_model()
    if model is None or len(analyses) < 2:
        return analyses

    analyses = sorted(analyses, key=lambda item: item.overall_score, reverse=True)
    top = analyses[0]
    if top.skipped_reason:
        return analyses

    for rank, item in enumerate(analyses, start=1):
        item.reranker_t2plus_prob = predict_same_day_reranker_probability(
            item,
            model,
            top_item=top,
            rank=rank,
        )

    top_probability = top.reranker_t2plus_prob
    if top_probability is None or top_probability > SAME_DAY_T2PLUS_RERANKER_TOP_PROB_CEILING:
        return analyses

    scan_limit = min(len(analyses), SAME_DAY_T2PLUS_RERANKER_SCAN)
    candidates = [
        candidate
        for candidate in analyses[1:scan_limit]
        if not candidate.skipped_reason
        and candidate.reranker_t2plus_prob is not None
        and candidate.reranker_t2plus_prob >= SAME_DAY_T2PLUS_RERANKER_MIN_ALT_PROB
        and candidate.reranker_t2plus_prob - top_probability >= SAME_DAY_T2PLUS_RERANKER_MIN_PROB_GAP
        and top.overall_score - candidate.overall_score <= SAME_DAY_T2PLUS_RERANKER_MAX_OVERALL_GAP
        and candidate.risk_complete_miss <= top.risk_complete_miss + SAME_DAY_T2PLUS_RERANKER_MISS_RISK_TOLERANCE
        and candidate.expected_max_target >= top.expected_max_target
        and candidate.prob_hit_t3 >= top.prob_hit_t3
    ]
    if not candidates:
        return analyses

    best_candidate = max(
        candidates,
        key=lambda item: (
            item.reranker_t2plus_prob or 0.0,
            item.expected_max_target,
            item.prob_hit_t3,
            item.raw_expected_utility_score,
            item.overall_score,
        ),
    )
    promote_same_day_override_candidate(analyses, best_candidate, top.overall_score)
    return analyses


def promote_same_day_override_candidate(
    analyses: list[IdeaAnalysis],
    candidate: IdeaAnalysis,
    current_top_score: float,
) -> None:
    candidate.overall_score = current_top_score + 0.001
    analyses.sort(key=lambda item: item.overall_score, reverse=True)


def calculate_standalone_same_day_selection_score(item: IdeaAnalysis) -> float:
    target_fit = clamp(
        1.0 - abs(item.best_target.ratio_to_average_day - TARGET_RATIO_SWEET_SPOT) / (TARGET_RATIO_SIGMA * 2.2),
        0.0,
        1.0,
    )
    score = (
        0.34 * item.raw_expected_utility_score
        + 24.0 * (1.0 - item.risk_complete_miss)
        + 10.0 * item.prob_hit_t1
        + 4.0 * item.prob_hit_t3
        + 0.10 * item.setup_score
        + 0.12 * item.entry_quality_score
        + 0.08 * item.freshness_score
        + 0.05 * item.relative_strength_score
        + 0.04 * item.instrument_score
        + 0.03 * item.history_score
        + 4.0 * target_fit
    )
    if item.best_target.label == "T3":
        score -= 2.0
    elif item.best_target.label == "T4":
        score -= 5.0
    elif item.best_target.label == "T1":
        score += 1.5

    if item.best_target.label in {"T1", "T2"} and item.prob_hit_t1 >= 0.55:
        score += 2.0

    return clamp(score, 0.0, 100.0)


VOL_TILT_TOP_K = 4


def _vol_tilt_enabled() -> bool:
    """Volatility-expansion tilt is ON by default; set WBT_DISABLE_VOL_TILT=1 to turn off."""
    return os.environ.get("WBT_DISABLE_VOL_TILT", "").strip().lower() not in {"1", "true", "yes", "on"}


def apply_volatility_expansion_tilt(
    analyses: list[IdeaAnalysis], top_k: int = VOL_TILT_TOP_K
) -> list[IdeaAnalysis]:
    """Among the top-K ideas by score, promote the one with the strongest volatility expansion.

    The runner signal that survived leave-one-month-out validation: across May and June it
    raised captured depth (1.90 -> 2.12 avg target) and turned per-trade EV positive in BOTH
    months, where ranking by score alone was negative. Quality is still enforced by only
    considering the top-K scored candidates.
    """
    ranked = sorted(analyses, key=lambda item: item.overall_score, reverse=True)
    pool = [item for item in ranked[:top_k] if not item.skipped_reason]
    if len(pool) < 2:
        return ranked
    if pool[0].market_bullish:
        # Rising market: skip the tilt (it leans short and loses on up days). Use base ranking.
        return ranked
    best = max(pool, key=lambda item: item.vol_expansion)
    ranked.remove(best)
    return [best] + ranked


def apply_same_day_top_override(analyses: list[IdeaAnalysis]) -> list[IdeaAnalysis]:
    if _ev_objective_enabled():
        # EV objective: rank purely by expected value. The learned rerankers optimize
        # "probability of any target", which pulls back toward shallow picks, so skip them.
        return sorted(analyses, key=lambda item: item.overall_score, reverse=True)
    # Honest selection only: base score + leave-one-month-out cross-validated rerankers.
    # The hand-tuned per-day override stack (apply_rule_based_same_day_top_override,
    # apply_standalone_same_day_top_rescue, apply_post_learned_same_day_overrides,
    # apply_final_precision_same_day_overrides) was removed after an A/B backtest showed
    # it was net-zero on "any target" and net-negative on the exact target even in-sample.
    # The pre-removal version is preserved in watchlist_best_target.py.bak.
    analyses = apply_learned_same_day_top_reranker(analyses)
    analyses = apply_learned_same_day_winner_reranker(analyses)
    if _vol_tilt_enabled():
        analyses = apply_volatility_expansion_tilt(analyses)
    return analyses


def analyze_watchlist(text: str, as_of: date) -> list[IdeaAnalysis]:
    analyses: list[IdeaAnalysis] = []
    full_index_sessions = fetch_index_sessions(DEFAULT_INDEX_SYMBOL, as_of)
    market_bias = calculate_market_bias(full_index_sessions[-5:])
    # Market regime (prior-day index vs its EMA20). The volatility tilt over-concentrates into
    # shorts, which hurts in a rising market (validated: Feb 2026 +1.1% -> tilt cut EV), so the
    # tilt is auto-disabled on bullish days.
    _index_closes = [session.close for session in full_index_sessions]
    market_bullish = len(_index_closes) >= 20 and _index_closes[-1] > calculate_ema(_index_closes[-20:], 20)
    for idea in parse_watchlist(text):
        try:
            full_stock_sessions = fetch_stock_sessions(idea.symbol, as_of)
            stock_sessions = full_stock_sessions[-6:]
            session = stock_sessions[-1]
        except MarketDataError as exc:
            analyses.append(
                IdeaAnalysis(
                    symbol=idea.symbol,
                    direction="unknown",
                    previous_session=SessionData(as_of, 0.0, 0.0, 0.0, 0.0),
                    best_target=TargetChoice("N/A", 0.0, 0.0, 0.0, 0.0),
                    ladder_score=0.0,
                    setup_score=0.0,
                    market_score=0.0,
                    instrument_score=0.0,
                    history_score=0.0,
                    entry_quality_score=0.0,
                    relative_strength_score=0.0,
                    volume_quality_score=0.0,
                    freshness_score=0.0,
                    reversion_indicator_score=50.0,
                    overall_score=0.0,
                    support=idea.support,
                    resistance=idea.resistance,
                    watchlist_reference_close=idea.reference_close,
                    skipped_reason=str(exc),
                )
            )
            continue

        avg_day_pct = max(idea.average_day_pct or 0.01, 0.01)
        auto_resistance, auto_support = detect_price_levels(stock_sessions, avg_day_pct)
        if idea.resistance is None and auto_resistance is not None:
            idea.resistance = auto_resistance
        if idea.support is None and auto_support is not None:
            idea.support = auto_support

        direction = infer_direction(session.close, idea.targets, idea.declared_direction)
        if direction == "neutral":
            analyses.append(
                IdeaAnalysis(
                    symbol=idea.symbol,
                    direction=direction,
                    previous_session=session,
                    best_target=TargetChoice("N/A", 0.0, 0.0, 0.0, 0.0),
                    ladder_score=0.0,
                    setup_score=0.0,
                    market_score=0.0,
                    instrument_score=0.0,
                    history_score=0.0,
                    entry_quality_score=0.0,
                    relative_strength_score=0.0,
                    volume_quality_score=0.0,
                    freshness_score=0.0,
                    reversion_indicator_score=50.0,
                    overall_score=0.0,
                    support=idea.support,
                    resistance=idea.resistance,
                    watchlist_reference_close=idea.reference_close,
                    reference_close_delta_pct=calculate_reference_close_delta_pct(idea.reference_close, session.close),
                    skipped_reason="Направление не удалось определить по целям.",
                )
            )
            continue

        # Precompute the RS5 shadow feature from the histories already fetched
        # for analysis.  trade_bot must not add synchronous market-data calls
        # in the 07:00 order path merely to collect shadow evidence.
        rs5_metrics = rs5_shadow.calculate_directional_rs5(
            direction,
            full_stock_sessions,
            full_index_sessions,
            as_of,
        )
        completed_stock_sessions = rs5_shadow.completed_sessions(
            full_stock_sessions, as_of
        )
        short_rally_10d_pct = None
        if len(completed_stock_sessions) >= 11:
            short_rally_10d_pct = (
                completed_stock_sessions[-1].close
                / completed_stock_sessions[-11].close
                - 1.0
            ) * 100.0

        target_ref_close = session.close
        reference_close_delta_pct = calculate_reference_close_delta_pct(idea.reference_close, session.close)
        best_target = choose_best_target(idea, target_ref_close)
        ladder_score = calculate_ladder_score(idea, target_ref_close)
        indicator_snapshot = calculate_indicator_snapshot(full_stock_sessions)
        reversion_indicator_score = calculate_reversion_indicator_score(direction, indicator_snapshot, avg_day_pct)
        post_impulse = analyze_post_impulse_features(direction, stock_sessions, avg_day_pct)
        anchor = select_anchor(idea, direction, stock_sessions, avg_day_pct, post_impulse)
        regime = classify_regime(
            idea,
            direction,
            stock_sessions,
            avg_day_pct,
            post_impulse,
            anchor,
            indicator_snapshot,
        )
        market_score = calculate_market_score(direction, market_bias)

        def evaluate_target(
            selected_target: TargetChoice,
        ) -> tuple[float, float, float, EntryQualitySnapshot, ExpectedUtility]:
            setup_score_local = calculate_setup_score(
                idea,
                direction,
                session,
                selected_target,
                sessions=stock_sessions,
                anchor=anchor,
            )
            instrument_score_local = calculate_instrument_score(
                direction,
                stock_sessions,
                idea.average_day_pct,
                idea,
                selected_target,
                anchor=anchor,
                indicator_snapshot=indicator_snapshot,
            )
            history_score_local = calculate_history_score(
                direction,
                full_stock_sessions,
                selected_target,
                idea.average_day_pct,
            )
            entry_quality_local = calculate_entry_quality_snapshot(
                direction,
                full_stock_sessions,
                full_index_sessions,
                selected_target,
                idea.average_day_pct,
                post_impulse,
                regime.regime,
                anchor,
            )
            utility_local = calculate_expected_utility(
                idea=idea,
                previous_close=target_ref_close,
                market_score=market_score,
                setup_score=setup_score_local,
                instrument_score=instrument_score_local,
                history_score=history_score_local,
                ladder_score=ladder_score,
                regime=regime,
                post_impulse=post_impulse,
                selected_anchor=anchor,
            )
            return (
                setup_score_local,
                instrument_score_local,
                history_score_local,
                entry_quality_local,
                utility_local,
            )

        setup_score, instrument_score, history_score, entry_quality, utility = evaluate_target(best_target)
        if _ev_objective_enabled():
            refined_best_target = choose_best_target_by_ev(
                idea,
                target_ref_close,
                utility.target_probabilities,
            )
        else:
            refined_best_target = choose_best_target_from_probabilities(
                idea,
                target_ref_close,
                utility.target_probabilities,
            )
        if refined_best_target.label != best_target.label:
            best_target = refined_best_target
            setup_score, instrument_score, history_score, entry_quality, utility = evaluate_target(best_target)
        narrow_target_penalty = calculate_narrow_target_penalty(best_target, ladder_score, history_score)
        barrier_penalty = calculate_recent_barrier_proximity_penalty(
            direction=direction,
            best_target=best_target,
            sessions=stock_sessions,
            previous_close=target_ref_close,
            average_day_pct=avg_day_pct,
            resistance=idea.resistance,
            support=idea.support,
            regime_name=regime.regime,
        )
        # Volatility expansion (recent ATR vs longer ATR): the one runner signal that
        # generalized across months. Used by the final volatility-expansion tilt.
        vol_expansion = calculate_atr_pct(full_stock_sessions, 5) / max(
            calculate_atr_pct(full_stock_sessions, 20), 1e-9
        )
        # Уверенность дня: P(идея дойдёт до T3+) по runner-модели. Не влияет на выбор
        # и ранжирование — используется только как совет «торговать/пропустить день».
        runner_features = compute_runner_day_features(
            full_stock_sessions, idea, direction, target_ref_close
        )
        runner_day_prob = (
            score_runner_day_confidence(runner_features) if runner_features is not None else None
        )
        # Recommended EXIT = the furthest target (hold for the runner). Validated over 5 months:
        # holding to T4 with the same 1% stop beats exiting at T2 (mean R +0.37 vs +0.28, positive
        # every month). Selection is unchanged; this only sets what we tell the user to aim for.
        exit_target = build_target_choice(
            max(idea.targets, key=lambda target: target.index),
            target_ref_close,
            idea.average_day_pct,
        )
        if _ev_objective_enabled():
            # Pure EV ranking: keep only the barrier penalty (hitting a wall is real),
            # drop the legacy near-target calibration that fights depth.
            overall_score = clamp(utility.score - barrier_penalty, 0.0, 100.0)
            analysis = IdeaAnalysis(
                symbol=idea.symbol,
                direction=direction,
                previous_session=session,
                best_target=best_target,
                ladder_score=ladder_score,
                setup_score=setup_score,
                market_score=market_score,
                instrument_score=instrument_score,
                history_score=history_score,
                entry_quality_score=entry_quality.total,
                relative_strength_score=entry_quality.relative_strength,
                volume_quality_score=entry_quality.volume_quality,
                freshness_score=entry_quality.freshness,
                reversion_indicator_score=reversion_indicator_score,
                overall_score=overall_score,
                support=idea.support,
                resistance=idea.resistance,
                watchlist_reference_close=idea.reference_close,
                reference_close_delta_pct=reference_close_delta_pct,
                regime=regime.regime,
                regime_confidence=regime.confidence,
                post_impulse_state=post_impulse.state,
                continuation_prob=post_impulse.continuation_prob,
                exhaustion_prob=post_impulse.exhaustion_prob,
                retrace_ratio=post_impulse.retrace_ratio,
                compression_ratio=post_impulse.compression_ratio,
                breakout_proximity=post_impulse.breakout_proximity,
                anchor_price=(regime.anchor if regime.anchor is not None else anchor).price
                if (regime.anchor if regime.anchor is not None else anchor) is not None
                else None,
                anchor_kind=(regime.anchor if regime.anchor is not None else anchor).kind
                if (regime.anchor if regime.anchor is not None else anchor) is not None
                else None,
                prob_hit_t1=utility.prob_hit_t1,
                prob_hit_t3=utility.prob_hit_t3,
                expected_max_target=utility.expected_max_target,
                risk_complete_miss=utility.risk_complete_miss,
                raw_expected_utility_score=utility.score,
                expected_value_pct=utility.expected_value_pct,
                vol_expansion=vol_expansion,
                market_bullish=market_bullish,
                exit_target=exit_target,
                runner_day_prob=runner_day_prob,
                directional_rs5_pp=(rs5_metrics or {}).get("aligned_rs5_pp"),
                rs5_stock_return_5d_pct=(rs5_metrics or {}).get("stock_return_5d_pct"),
                rs5_index_return_5d_pct=(rs5_metrics or {}).get("index_return_5d_pct"),
                rs5_stock_window=tuple((rs5_metrics or {}).get("stock_window", ())) or None,
                rs5_index_window=tuple((rs5_metrics or {}).get("index_window", ())) or None,
                short_rally_10d_pct=short_rally_10d_pct,
                target_probabilities=utility.target_probabilities,
            )
            analysis.standalone_selection_score = calculate_standalone_same_day_selection_score(analysis)
            analyses.append(analysis)
            continue
        overall_score = clamp(utility.score - narrow_target_penalty - barrier_penalty, 0.0, 100.0)
        overall_score = clamp(
            overall_score
            + 0.55 * calculate_rank_calibration(
                direction=direction,
                regime_name=regime.regime,
                market_score=market_score,
                setup_score=setup_score,
                instrument_score=instrument_score,
                history_score=history_score,
                average_day_pct=idea.average_day_pct,
                reference_close=target_ref_close,
                support=idea.support,
            )
            + calculate_one_day_rank_adjustment(
                best_target=best_target,
                regime_name=regime.regime,
                setup_score=setup_score,
                instrument_score=instrument_score,
                history_score=history_score,
                market_score=market_score,
                entry_quality_score=entry_quality.total,
                relative_strength_score=entry_quality.relative_strength,
                volume_quality_score=entry_quality.volume_quality,
                freshness_score=entry_quality.freshness,
                prob_hit_t1=utility.prob_hit_t1,
                risk_complete_miss=utility.risk_complete_miss,
                reversion_indicator_score=reversion_indicator_score,
                anchor_kind=anchor.kind if anchor is not None else None,
                post_impulse_state=post_impulse.state,
            ),
            0.0,
            100.0,
        )
        overall_score = clamp(
            overall_score + get_entry_quality_weight(regime.regime, entry_quality.total) * (entry_quality.total - 50.0),
            0.0,
            100.0,
        )
        overall_score = clamp(
            overall_score
            + calculate_one_day_live_leader_bonus(
                best_target=best_target,
                regime_name=regime.regime,
                setup_score=setup_score,
                history_score=history_score,
                entry_quality_score=entry_quality.total,
                freshness_score=entry_quality.freshness,
                anchor_kind=anchor.kind if anchor is not None else None,
            ),
            0.0,
            100.0,
        )
        resolved_anchor = regime.anchor if regime.anchor is not None else anchor

        analysis = IdeaAnalysis(
            symbol=idea.symbol,
            direction=direction,
            previous_session=session,
            best_target=best_target,
            ladder_score=ladder_score,
            setup_score=setup_score,
            market_score=market_score,
            instrument_score=instrument_score,
            history_score=history_score,
            entry_quality_score=entry_quality.total,
            relative_strength_score=entry_quality.relative_strength,
            volume_quality_score=entry_quality.volume_quality,
            freshness_score=entry_quality.freshness,
            reversion_indicator_score=reversion_indicator_score,
            overall_score=overall_score,
            support=idea.support,
            resistance=idea.resistance,
            watchlist_reference_close=idea.reference_close,
            reference_close_delta_pct=reference_close_delta_pct,
            regime=regime.regime,
            regime_confidence=regime.confidence,
            post_impulse_state=post_impulse.state,
            continuation_prob=post_impulse.continuation_prob,
            exhaustion_prob=post_impulse.exhaustion_prob,
            retrace_ratio=post_impulse.retrace_ratio,
            compression_ratio=post_impulse.compression_ratio,
            breakout_proximity=post_impulse.breakout_proximity,
            anchor_price=resolved_anchor.price if resolved_anchor is not None else None,
            anchor_kind=resolved_anchor.kind if resolved_anchor is not None else None,
            prob_hit_t1=utility.prob_hit_t1,
            prob_hit_t3=utility.prob_hit_t3,
            expected_max_target=utility.expected_max_target,
            risk_complete_miss=utility.risk_complete_miss,
            raw_expected_utility_score=utility.score,
            expected_value_pct=utility.expected_value_pct,
            vol_expansion=vol_expansion,
            market_bullish=market_bullish,
            exit_target=exit_target,
            runner_day_prob=runner_day_prob,
            directional_rs5_pp=(rs5_metrics or {}).get("aligned_rs5_pp"),
            rs5_stock_return_5d_pct=(rs5_metrics or {}).get("stock_return_5d_pct"),
            rs5_index_return_5d_pct=(rs5_metrics or {}).get("index_return_5d_pct"),
            rs5_stock_window=tuple((rs5_metrics or {}).get("stock_window", ())) or None,
            rs5_index_window=tuple((rs5_metrics or {}).get("index_window", ())) or None,
            short_rally_10d_pct=short_rally_10d_pct,
            target_probabilities=utility.target_probabilities,
        )
        analysis.standalone_selection_score = calculate_standalone_same_day_selection_score(analysis)
        analyses.append(analysis)
    return apply_same_day_top_override(analyses)


def detect_reference_close_mismatch(analyses: list[IdeaAnalysis]) -> str | None:
    mismatched_items = [
        item
        for item in analyses
        if not item.skipped_reason
        and item.watchlist_reference_close is not None
        and item.reference_close_delta_pct is not None
        and item.reference_close_delta_pct >= 0.35
    ]
    if not mismatched_items:
        return None

    sample = mismatched_items[0]
    return (
        "Внимание: Close из файла не совпадает с previous close T-Invest. "
        f"Совпадение нарушено у {len(mismatched_items)} идей; "
        f"пример: {sample.symbol} watchlist_close={sample.watchlist_reference_close:.3f}, "
        f"t_invest_prev_close={sample.previous_session.close:.3f} "
        f"на {sample.previous_session.trade_date.isoformat()}."
    )


def detect_watchlist_mismatch(analyses: list[IdeaAnalysis]) -> str | None:
    valid_items = [item for item in analyses if not item.skipped_reason]
    if len(valid_items) < 3:
        return None

    suspicious_items = [
        item
        for item in valid_items
        if item.best_target.move_pct_from_close >= 12.0
        or item.best_target.ratio_to_average_day >= 3.2
        or item.best_target.score <= 1.0
    ]
    if len(suspicious_items) < math.ceil(len(valid_items) * 0.6):
        return None

    return (
        "Внимание: дата анализа, вероятно, не совпадает с вотчлистом. "
        f"У {len(suspicious_items)} из {len(valid_items)} идей ближайшая цель слишком "
        "далеко от previous close. Проверь год и дату."
    )


def format_analysis(analyses: list[IdeaAnalysis]) -> str:
    lines: list[str] = []
    reference_close_warning = detect_reference_close_mismatch(analyses)
    mismatch_warning = detect_watchlist_mismatch(analyses)
    if reference_close_warning:
        lines.append(reference_close_warning)
        lines.append("")
    if mismatch_warning:
        lines.append(mismatch_warning)
        lines.append("")

    for rank, item in enumerate(analyses, start=1):
        if item.skipped_reason:
            lines.append(f"{rank}. {item.symbol}: skip, {item.skipped_reason}")
            continue

        lines.append(
            (
                f"{rank}. {item.symbol} | {item.direction} | "
                f"prev_close={item.previous_session.close:.3f}"
                + (
                    f" | watchlist_close={item.watchlist_reference_close:.3f}"
                    if item.watchlist_reference_close is not None and item.reference_close_delta_pct is not None
                    and item.reference_close_delta_pct >= 0.35
                    else ""
                )
                + " | "
                + (
                    f"ВЫХОД {item.exit_target.label} @{item.exit_target.price:.3f} "
                    f"(+{item.exit_target.move_pct_from_close:.2f}%), стоп {STOP_LOSS_PCT:.0f}% | "
                    if item.exit_target is not None
                    else ""
                )
                + f"best={item.best_target.label} @{item.best_target.price:.3f} | "
                f"move={item.best_target.move_pct_from_close:.2f}% | "
                f"avg_ratio={item.best_target.ratio_to_average_day:.2f} | "
                f"target_score={item.best_target.score:.1f} | "
                f"ladder_score={item.ladder_score:.1f} | "
                f"setup_score={item.setup_score:.1f} | "
                f"market_score={item.market_score:.1f} | "
                f"instrument_score={item.instrument_score:.1f} | "
                f"history_score={item.history_score:.1f} | "
                f"entry_score={item.entry_quality_score:.1f} | "
                f"rs={item.relative_strength_score:.1f} | "
                f"vol={item.volume_quality_score:.1f} | "
                f"fresh={item.freshness_score:.1f} | "
                f"rev_ind={item.reversion_indicator_score:.1f} | "
                f"regime={item.regime}({item.regime_confidence:.2f}) | "
                f"state={item.post_impulse_state} | "
                + (
                    f"anchor={item.anchor_kind}@{item.anchor_price:.3f} | "
                    if item.anchor_price is not None and item.anchor_kind is not None
                    else ""
                )
                + f"pT1={item.prob_hit_t1 * 100.0:.0f}% | "
                f"pT3={item.prob_hit_t3 * 100.0:.0f}% | "
                f"exp_targets={item.expected_max_target:.2f} | "
                f"miss_risk={item.risk_complete_miss * 100.0:.0f}% | "
                f"overall={item.overall_score:.1f}"
            )
        )
    if analyses:
        winner = analyses[0]
        lines.append("")
        if reference_close_warning or mismatch_warning:
            lines.append("Лучшая идея не определена: сначала проверь дату анализа.")
        elif winner.skipped_reason:
            lines.append("Лучшая идея не определена.")
        else:
            exit_hint = (
                f", держать до {winner.exit_target.label} @{winner.exit_target.price:.3f} "
                f"(+{winner.exit_target.move_pct_from_close:.2f}%), стоп {STOP_LOSS_PCT:.0f}%"
                if winner.exit_target is not None
                else ""
            )
            lines.append(
                "Лучшая идея: "
                f"{winner.symbol} {winner.direction}{exit_hint}"
            )
            day_verdict = format_day_confidence_verdict(winner)
            if day_verdict:
                lines.append(day_verdict)
    return "\n".join(lines)


def format_day_confidence_verdict(winner: IdeaAnalysis) -> str | None:
    """Совет «торговать/пропустить день» по runner-prob финального выбора.

    Валидация (5 мес, LOMO): торговля только в дни с prob выше порога монотонно
    поднимает EV/сделку; в отфильтрованные дни EV ~0. Выбор и ранжирование не меняет.
    """
    if os.environ.get("WBT_DISABLE_DAY_FILTER") == "1":
        return None
    if winner.runner_day_prob is None:
        return None
    threshold = runner_day_confidence_threshold(winner.direction)
    if threshold is None:
        return None
    kind = " (лонгам порог строже)" if winner.direction == "long" else ""
    prob_pct = winner.runner_day_prob * 100.0
    if winner.runner_day_prob >= threshold:
        return f"Уверенность дня: {prob_pct:.0f}% (порог {threshold * 100.0:.0f}%{kind}) — день торговый."
    return (
        f"Уверенность дня: {prob_pct:.0f}% < порога {threshold * 100.0:.0f}%{kind} — "
        "слабый день, лучше ПРОПУСТИТЬ (на таких днях EV исторически ~0)."
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Парсит текст вотчлиста, берет previous close через T-Invest API "
            "и выбирает лучшую цель по достижимости относительно среднего дневного хода."
        )
    )
    parser.add_argument(
        "--as-of",
        help=(
            "Дата анализа. Поддерживаются YYYY-MM-DD, DD.MM.YYYY, сегодня, вчера, завтра, "
            "послезавтра, +2, -1. Скрипт возьмет предыдущую торговую сессию."
        ),
    )
    parser.add_argument(
        "--input",
        help="Путь к текстовому файлу с вотчлистом. Если не задан, скрипт читает stdin.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Печатает результат в JSON вместо текстовой таблицы.",
    )
    return parser.parse_args()


def read_input(path: str | None) -> str:
    if path:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    if not sys.stdin.isatty():
        stdin_text = sys.stdin.read()
        if stdin_text.strip():
            return stdin_text

    script_dir = str(PROJECT_ROOT)
    candidates = [
        os.path.join(os.getcwd(), filename) for filename in DEFAULT_INPUT_FILENAMES
    ] + [
        os.path.join(script_dir, filename) for filename in DEFAULT_INPUT_FILENAMES
    ]

    for candidate in candidates:
        if os.path.isfile(candidate):
            with open(candidate, "r", encoding="utf-8") as handle:
                return handle.read()

    raise ParseError(
        "Нет входного текста. Для запуска из VS Code создай рядом со скриптом файл "
        "'data/watchlists/watchlist.txt' или запусти скрипт с --input / передай текст через stdin."
    )


def to_json_ready(analyses: list[IdeaAnalysis]) -> list[dict]:
    return [
        {
            "symbol": item.symbol,
            "direction": item.direction,
            "previous_session": {
                "trade_date": item.previous_session.trade_date.isoformat(),
                "open": item.previous_session.open,
                "low": item.previous_session.low,
                "high": item.previous_session.high,
                "close": item.previous_session.close,
                "volume": item.previous_session.volume,
            },
            "best_target": {
                "label": item.best_target.label,
                "price": item.best_target.price,
                "move_pct_from_close": item.best_target.move_pct_from_close,
                "ratio_to_average_day": item.best_target.ratio_to_average_day,
                "score": item.best_target.score,
            },
            "exit_target": (
                {
                    "label": item.exit_target.label,
                    "price": item.exit_target.price,
                    "move_pct_from_close": item.exit_target.move_pct_from_close,
                    "stop_loss_pct": STOP_LOSS_PCT,
                }
                if item.exit_target is not None
                else None
            ),
            "ladder_score": item.ladder_score,
            "support": item.support,
            "resistance": item.resistance,
            "watchlist_reference_close": item.watchlist_reference_close,
            "reference_close_delta_pct": item.reference_close_delta_pct,
            "setup_score": item.setup_score,
            "market_score": item.market_score,
            "instrument_score": item.instrument_score,
            "history_score": item.history_score,
            "entry_quality_score": item.entry_quality_score,
            "relative_strength_score": item.relative_strength_score,
            "volume_quality_score": item.volume_quality_score,
            "freshness_score": item.freshness_score,
            "reversion_indicator_score": item.reversion_indicator_score,
            "regime": item.regime,
            "regime_confidence": item.regime_confidence,
            "post_impulse_state": item.post_impulse_state,
            "continuation_prob": item.continuation_prob,
            "exhaustion_prob": item.exhaustion_prob,
            "retrace_ratio": item.retrace_ratio,
            "compression_ratio": item.compression_ratio,
            "breakout_proximity": item.breakout_proximity,
            "anchor_price": item.anchor_price,
            "anchor_kind": item.anchor_kind,
            "prob_hit_t1": item.prob_hit_t1,
            "prob_hit_t3": item.prob_hit_t3,
            "expected_max_target": item.expected_max_target,
            "risk_complete_miss": item.risk_complete_miss,
            "reranker_any_target_prob": item.reranker_any_target_prob,
            "runner_day_prob": item.runner_day_prob,
            "directional_rs5_pp": item.directional_rs5_pp,
            "rs5_stock_return_5d_pct": item.rs5_stock_return_5d_pct,
            "rs5_index_return_5d_pct": item.rs5_index_return_5d_pct,
            "rs5_stock_window": item.rs5_stock_window,
            "rs5_index_window": item.rs5_index_window,
            "short_rally_10d_pct": item.short_rally_10d_pct,
            "raw_expected_utility_score": item.raw_expected_utility_score,
            "standalone_selection_score": item.standalone_selection_score,
            "target_probabilities": item.target_probabilities,
            "overall_score": item.overall_score,
            "skipped_reason": item.skipped_reason,
        }
        for item in analyses
    ]


def main() -> int:
    args = parse_args()
    try:
        as_of = resolve_as_of_date(args.as_of)
        raw_text = read_input(args.input)
        if not raw_text.strip():
            raise ParseError("Пустой ввод: передай текст вотчлиста через stdin, --input или watchlist.txt.")
        analyses = analyze_watchlist(raw_text, as_of)
    except (ParseError, MarketDataError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(to_json_ready(analyses), ensure_ascii=False, indent=2))
    else:
        print(format_analysis(analyses))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
