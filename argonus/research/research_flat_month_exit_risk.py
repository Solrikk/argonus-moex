#!/usr/bin/env python3
"""Research exits and causal sizing on the frozen Argonus 54/65 ledgers.

This module is intentionally research-only.  It reads immutable/local backtest
artifacts, never constructs a broker or market-data client, and never imports
``trade_bot``.  The 65-trade book is the eleven October 2025 Engine-A+RS5
trades followed by the frozen 54-trade RS5 ledger for November--July.

The study deliberately reports selection-aware checks rather than promoting
the best in-sample row:

* expanding monthly walk-forward (three initial months, then Jan--Jul);
* leave-one-month-out selection diagnostics;
* month-block bootstrap deltas;
* drawdown, weak-month and winner-concentration checks;
* an independent replay control for every baseline 5-minute exit.

All features used by volatility/regime sizing are built from daily rows whose
date is strictly earlier than the trade date.  Stateful streak/drawdown rules
observe only previously closed trades.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from argonus.backtesting import backtest_live_61_trades as frozen
from argonus.research import research_fixed_150k_sizing as fixed
from argonus.research import research_october_engine_a_rs5 as october


from argonus.paths import PROJECT_ROOT as ROOT
SNAPSHOT = ROOT / "data/backtests/live_rr2_t4_candidates_2025-11_2026-07.json"
FIVE_MIN_DIR = ROOT / "data/intraday_universe/five_min"
FALLBACK = ROOT / "data/backtests/five_min_fallback_5_sessions.json"
OCTOBER_CACHE = ROOT / "data/backtests/october_engine_a_rs5_selected_5m_cache.json"
OCTOBER_REPORT = ROOT / "data/backtests/OCTOBER_ENGINE_A_RS5_RESEARCH_2025_10.json"
DAILY_DIR = ROOT / "data/intraday_universe/dailies"
INDEX_DAILY = ROOT / "data/intraday_universe/index_daily.json"

FEE_PCT = 0.08
START_EQUITY = 50_000.0
POSITION_CAP = 150_000.0
MAX_LEVERAGE = 3.0
BASELINE_STOP = 1.0
BASELINE_TARGET_MULTIPLE = 1.0
BASELINE_TIME_EXIT = "18:35"
EXPECTED_54_TABLE_RETURN = 50.09557506271673
EXPECTED_65_TABLE_RETURN = 54.27745462958335
EXPECTED_65_FIXED_RETURN = 131.2984111205006
EXPECTED_TRADE_COUNT_54 = 54
EXPECTED_TRADE_COUNT_65 = 65
BOOTSTRAP_SAMPLES = 5000
BOOTSTRAP_SEED = 20260718
WF_FIRST_TEST_MONTH_INDEX = 3


@dataclass(frozen=True, slots=True)
class FeatureSet:
    prior_daily_rows: int
    range20_pct: float
    vol_ratio_5_20: float
    regime_strength_pct: float
    regime_strength_z: float


@dataclass(frozen=True, slots=True)
class Trade:
    date: str
    month: str
    symbol: str
    direction: str
    target_price: float
    candles: tuple[frozen.Candle, ...]
    source: str
    session_sha256: str
    expected_gross_return_pct: float
    features: FeatureSet


@dataclass(frozen=True, slots=True)
class ExitRule:
    name: str
    stop_pct: float = BASELINE_STOP
    target_multiple: float = BASELINE_TARGET_MULTIPLE
    time_exit: str = BASELINE_TIME_EXIT


@dataclass(frozen=True, slots=True)
class ExitResult:
    date: str
    month: str
    symbol: str
    direction: str
    gross_return_pct: float
    net_return_pct: float
    exit_reason: str
    exit_time: str
    target_price: float
    stop_price: float
    ambiguous: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exit/risk walk-forward research on the immutable 54/65 ledgers."
    )
    parser.add_argument("--verify-controls", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def compound(values: Iterable[float]) -> float:
    capital = 1.0
    for value in values:
        capital *= 1.0 + value / 100.0
    return (capital - 1.0) * 100.0


def percentile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = (len(sorted_values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(sorted_values[lower])
    fraction = position - lower
    return float(sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction)


def return_metrics(
    returns: Sequence[float],
    trades: Sequence[Trade],
) -> dict[str, Any]:
    if len(returns) != len(trades):
        raise RuntimeError("Return/trade length mismatch.")
    capital = 1.0
    peak = 1.0
    mdd = 0.0
    monthly_values: dict[str, list[float]] = defaultdict(list)
    positive = []
    for value, trade in zip(returns, trades, strict=True):
        capital *= 1.0 + value / 100.0
        peak = max(peak, capital)
        mdd = min(mdd, capital / peak - 1.0)
        monthly_values[trade.month].append(value)
        if value > 0.0:
            positive.append(value)
    monthly = {
        month: compound(values) for month, values in sorted(monthly_values.items())
    }
    month_returns = list(monthly.values())
    bottom_four = sorted(month_returns)[: min(4, len(month_returns))]
    total_positive = sum(positive)
    sorted_positive = sorted(positive, reverse=True)
    best_index = max(range(len(returns)), key=lambda index: returns[index]) if returns else None
    without_best = [
        value for index, value in enumerate(returns) if index != best_index
    ]
    return {
        "trades": len(returns),
        "wins": sum(value > 0.0 for value in returns),
        "losses": sum(value <= 0.0 for value in returns),
        "return_pct": (capital - 1.0) * 100.0,
        "max_drawdown_pct": mdd * 100.0,
        "worst_month_pct": min(month_returns) if month_returns else 0.0,
        "positive_months": sum(value > 0.0 for value in month_returns),
        "weak_months_le_1pct": sum(value <= 1.0 for value in month_returns),
        "bottom_four_mean_pct": statistics.fmean(bottom_four) if bottom_four else 0.0,
        "monthly": monthly,
        "largest_winner_share_pct": (
            sorted_positive[0] / total_positive * 100.0 if total_positive > 0.0 else 0.0
        ),
        "top3_winner_share_pct": (
            sum(sorted_positive[:3]) / total_positive * 100.0 if total_positive > 0.0 else 0.0
        ),
        "return_without_best_trade_pct": compound(without_best),
    }


def objective(metrics: dict[str, Any]) -> float:
    """Pre-registered training utility: reward return, penalize DD and weak tail."""
    return (
        float(metrics["return_pct"])
        + 0.50 * float(metrics["max_drawdown_pct"])
        + 0.25 * float(metrics["worst_month_pct"])
    )


def read_daily_rows(symbol: str) -> list[list[Any]]:
    path = DAILY_DIR / f"{symbol}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise RuntimeError(f"Daily cache is not a list: {path}")
    return value


def ema(values: Sequence[float], period: int) -> float:
    if not values:
        raise RuntimeError("Cannot calculate EMA without values.")
    alpha = 2.0 / (period + 1.0)
    result = float(values[0])
    for value in values[1:]:
        result = alpha * float(value) + (1.0 - alpha) * result
    return result


def features_for(
    symbol: str,
    direction: str,
    trade_date: str,
    daily_cache: dict[str, list[list[Any]]],
    index_rows: list[list[Any]],
) -> FeatureSet:
    stock = [row for row in daily_cache[symbol] if str(row[0]) < trade_date]
    index = [row for row in index_rows if str(row[0]) < trade_date]
    if len(stock) < 20 or len(index) < 21:
        raise RuntimeError(f"Insufficient causal daily history: {trade_date} {symbol}")
    ranges = [
        (float(row[2]) - float(row[3])) / max(float(row[4]), 1e-12) * 100.0
        for row in stock[-20:]
    ]
    range20 = statistics.fmean(ranges)
    range5 = statistics.fmean(ranges[-5:])
    index_closes = [float(row[1]) for row in index]
    index_returns = [
        (index_closes[i] / index_closes[i - 1] - 1.0) * 100.0
        for i in range(max(1, len(index_closes) - 20), len(index_closes))
    ]
    index_sigma = statistics.pstdev(index_returns) if len(index_returns) >= 2 else 0.0
    index_ema20 = ema(index_closes[-20:], 20)
    raw_distance = (index_closes[-1] / index_ema20 - 1.0) * 100.0
    aligned_distance = raw_distance if direction == "long" else -raw_distance
    return FeatureSet(
        prior_daily_rows=len(stock),
        range20_pct=range20,
        vol_ratio_5_20=range5 / max(range20, 1e-12),
        regime_strength_pct=aligned_distance,
        regime_strength_z=aligned_distance / max(index_sigma, 1e-12),
    )


def validate_october_cache(payload: dict[str, Any]) -> None:
    stored = payload.get("cache_sha256")
    body = {key: value for key, value in payload.items() if key != "cache_sha256"}
    actual = october.canonical_sha256(body)
    if stored != actual:
        raise RuntimeError(f"October cache hash mismatch: {stored} != {actual}")


def load_books() -> tuple[list[Trade], list[Trade], dict[str, Any]]:
    ledger_61, ledger_54, frozen_pins = fixed.load_ledgers()
    snapshot = frozen.load_snapshot(SNAPSHOT)
    snapshot_by_date = {str(row["date"]): row for row in snapshot["candidates"]}
    store = frozen.SessionStore(
        five_min_dir=FIVE_MIN_DIR,
        fallback_path=FALLBACK,
        pinned_start="07:00",
        pinned_end="18:35",
        check_hashes=True,
    )
    october_cache = json.loads(OCTOBER_CACHE.read_text(encoding="utf-8"))
    validate_october_cache(october_cache)
    october_report = json.loads(OCTOBER_REPORT.read_text(encoding="utf-8"))

    symbols = {trade.symbol for trade in ledger_54}
    symbols.update(str(row["symbol"]) for row in october_report["trades"])
    daily_cache = {symbol: read_daily_rows(symbol) for symbol in sorted(symbols)}
    index_rows = json.loads(INDEX_DAILY.read_text(encoding="utf-8"))

    trades54: list[Trade] = []
    for expected in ledger_54:
        candidate = dict(
            fixed.RS5_REPLACEMENTS.get(expected.date)
            or snapshot_by_date.get(expected.date)
            or {}
        )
        if not candidate:
            raise RuntimeError(f"Candidate missing for frozen trade {expected.date}")
        candles, source, digest = store.load(candidate)
        if expected.symbol != candidate["symbol"] or expected.session_sha256 != digest:
            raise RuntimeError(f"Frozen identity mismatch on {expected.date}")
        trades54.append(
            Trade(
                date=expected.date,
                month=expected.month,
                symbol=expected.symbol,
                direction=expected.direction,
                target_price=float(candidate["target_price"]),
                candles=tuple(candles),
                source=source,
                session_sha256=digest,
                expected_gross_return_pct=expected.gross_return_pct,
                features=features_for(
                    expected.symbol,
                    expected.direction,
                    expected.date,
                    daily_cache,
                    index_rows,
                ),
            )
        )

    october_trades: list[Trade] = []
    for row in october_report["trades"]:
        key = f"{row['date']}|{row['symbol']}"
        cached = october_cache["selected_sessions"].get(key)
        if not cached or cached.get("status") != "ok":
            raise RuntimeError(f"October session missing: {key}")
        raw = cached["rows"]
        digest = october.canonical_sha256(raw)
        if digest != cached["session_sha256"] or digest != row["session_sha256"]:
            raise RuntimeError(f"October session identity mismatch: {key}")
        candles = tuple(
            frozen.Candle(str(item[0]), *map(float, item[1:5])) for item in raw
        )
        october_trades.append(
            Trade(
                date=str(row["date"]),
                month="2025-10",
                symbol=str(row["symbol"]),
                direction=str(row["direction"]),
                target_price=float(row["target_price"]),
                candles=candles,
                source="immutable_selected_5m_cache",
                session_sha256=digest,
                expected_gross_return_pct=float(row["gross_return_pct"]),
                features=features_for(
                    str(row["symbol"]),
                    str(row["direction"]),
                    str(row["date"]),
                    daily_cache,
                    index_rows,
                ),
            )
        )

    trades65 = sorted([*october_trades, *trades54], key=lambda value: value.date)
    if len(trades54) != EXPECTED_TRADE_COUNT_54 or len(trades65) != EXPECTED_TRADE_COUNT_65:
        raise RuntimeError(f"Ledger size mismatch: {len(trades54)}/{len(trades65)}")
    provenance = {
        "ledger_61_count": len(ledger_61),
        "ledger_54_count": len(ledger_54),
        "october_count": len(october_trades),
        "frozen_pins": frozen_pins,
        "inputs": {
            str(path.relative_to(ROOT)): sha256_file(path)
            for path in (
                SNAPSHOT,
                FALLBACK,
                OCTOBER_CACHE,
                OCTOBER_REPORT,
                INDEX_DAILY,
            )
        },
    }
    return trades54, trades65, provenance


def simulate_exit(trade: Trade, rule: ExitRule) -> ExitResult:
    window = [candle for candle in trade.candles if "07:00" <= candle.time <= rule.time_exit]
    if not window:
        raise RuntimeError(f"No candles for {trade.date} {trade.symbol} under {rule.name}")
    entry = window[0].open
    sign = 1.0 if trade.direction == "long" else -1.0
    original_distance = sign * (trade.target_price / entry - 1.0)
    if original_distance <= 0.0:
        raise RuntimeError(f"Frozen target is behind entry: {trade.date} {trade.symbol}")
    target = entry * (1.0 + sign * original_distance * rule.target_multiple)
    stop = entry * (1.0 - sign * rule.stop_pct / 100.0)
    exit_price = window[-1].close
    exit_time = window[-1].time
    reason = "time_exit"
    ambiguous = False
    gross = sign * (exit_price / entry - 1.0) * 100.0
    for candle in window:
        if trade.direction == "long":
            stop_hit = candle.low <= stop
            target_hit = candle.high >= target
        else:
            stop_hit = candle.high >= stop
            target_hit = candle.low <= target
        if stop_hit:
            ambiguous = target_hit
            exit_price = stop
            exit_time = candle.time
            reason = "both_stop_first" if ambiguous else "stop"
            gross = -rule.stop_pct
            break
        if target_hit:
            exit_price = target
            exit_time = candle.time
            reason = "target"
            gross = sign * (target / entry - 1.0) * 100.0
            break
    return ExitResult(
        date=trade.date,
        month=trade.month,
        symbol=trade.symbol,
        direction=trade.direction,
        gross_return_pct=gross,
        net_return_pct=gross - FEE_PCT,
        exit_reason=reason,
        exit_time=exit_time,
        target_price=target,
        stop_price=stop,
        ambiguous=ambiguous,
    )


def baseline_rule() -> ExitRule:
    return ExitRule("baseline_sl1_t4_1835")


def table_multiplier(trade: Trade) -> float:
    return 0.5 if trade.direction == "long" else 1.0


def exit_returns(
    trades: Sequence[Trade],
    long_rule: ExitRule,
    short_rule: ExitRule | None = None,
) -> list[float]:
    short_rule = short_rule or long_rule
    values: list[float] = []
    for trade in trades:
        rule = long_rule if trade.direction == "long" else short_rule
        values.append(simulate_exit(trade, rule).net_return_pct * table_multiplier(trade))
    return values


def month_delta_stats(
    variant: Sequence[float], baseline: Sequence[float], trades: Sequence[Trade]
) -> dict[str, Any]:
    by_month_variant: dict[str, list[float]] = defaultdict(list)
    by_month_baseline: dict[str, list[float]] = defaultdict(list)
    for value, control, trade in zip(variant, baseline, trades, strict=True):
        by_month_variant[trade.month].append(value)
        by_month_baseline[trade.month].append(control)
    deltas = {
        month: compound(by_month_variant[month]) - compound(by_month_baseline[month])
        for month in sorted(by_month_baseline)
    }
    return {
        "positive_delta_months": sum(value > 0.0 for value in deltas.values()),
        "negative_delta_months": sum(value < 0.0 for value in deltas.values()),
        "zero_delta_months": sum(abs(value) <= 1e-12 for value in deltas.values()),
        "median_month_delta_pp": statistics.median(deltas.values()) if deltas else 0.0,
        "worst_month_delta_pp": min(deltas.values()) if deltas else 0.0,
        "monthly_delta_pp": deltas,
    }


def delta_concentration(
    variant: Sequence[float], baseline: Sequence[float], trades: Sequence[Trade]
) -> dict[str, Any]:
    deltas = [value - control for value, control in zip(variant, baseline, strict=True)]
    positive = sorted((value for value in deltas if value > 0.0), reverse=True)
    positive_sum = sum(positive)
    total_delta = compound(variant) - compound(baseline)
    best_index = max(range(len(deltas)), key=lambda index: deltas[index]) if deltas else None
    if best_index is None:
        without_best = 0.0
    else:
        v = [value for index, value in enumerate(variant) if index != best_index]
        b = [value for index, value in enumerate(baseline) if index != best_index]
        without_best = compound(v) - compound(b)
    return {
        "total_return_delta_pp": total_delta,
        "largest_positive_trade_delta_share_pct": (
            positive[0] / positive_sum * 100.0 if positive_sum > 0.0 else 0.0
        ),
        "top3_positive_trade_delta_share_pct": (
            sum(positive[:3]) / positive_sum * 100.0 if positive_sum > 0.0 else 0.0
        ),
        "delta_without_best_contributor_pp": without_best,
        "best_delta_trade": (
            {
                "date": trades[best_index].date,
                "symbol": trades[best_index].symbol,
                "delta_pp_on_trade": deltas[best_index],
            }
            if best_index is not None
            else None
        ),
    }


def month_block_bootstrap(
    variant: Sequence[float], baseline: Sequence[float], trades: Sequence[Trade]
) -> dict[str, Any]:
    months = sorted({trade.month for trade in trades})
    indices = {
        month: [index for index, trade in enumerate(trades) if trade.month == month]
        for month in months
    }
    rng = random.Random(BOOTSTRAP_SEED)
    deltas: list[float] = []
    for _ in range(BOOTSTRAP_SAMPLES):
        sampled = [rng.choice(months) for _ in months]
        v: list[float] = []
        b: list[float] = []
        for month in sampled:
            for index in indices[month]:
                v.append(variant[index])
                b.append(baseline[index])
        deltas.append(compound(v) - compound(b))
    ordered = sorted(deltas)
    return {
        "samples": BOOTSTRAP_SAMPLES,
        "seed": BOOTSTRAP_SEED,
        "probability_delta_positive": sum(value > 0.0 for value in deltas) / len(deltas),
        "delta_ci95_pp": [percentile(ordered, 0.025), percentile(ordered, 0.975)],
        "median_delta_pp": percentile(ordered, 0.5),
    }


def exit_grid() -> list[ExitRule]:
    values = []
    for stop in (0.75, 1.0, 1.25, 1.5):
        for target in (0.50, 0.75, 1.0, 1.25):
            for time_exit in ("12:00", "14:00", "16:00", "17:00", "18:35"):
                values.append(
                    ExitRule(
                        name=f"sl{stop:g}_t{target:g}_x{time_exit.replace(':', '')}",
                        stop_pct=stop,
                        target_multiple=target,
                        time_exit=time_exit,
                    )
                )
    return values


def evaluate_exit_rule(rule: ExitRule, trades54: Sequence[Trade], trades65: Sequence[Trade]) -> dict[str, Any]:
    baseline54 = exit_returns(trades54, baseline_rule())
    baseline65 = exit_returns(trades65, baseline_rule())
    values54 = exit_returns(trades54, rule)
    values65 = exit_returns(trades65, rule)
    metrics54 = return_metrics(values54, trades54)
    metrics65 = return_metrics(values65, trades65)
    return {
        "rule": asdict(rule),
        "ledger54": metrics54,
        "ledger65": metrics65,
        "month_delta": month_delta_stats(values65, baseline65, trades65),
        "concentration": delta_concentration(values65, baseline65, trades65),
        "training_objective65": objective(metrics65),
        "delta54_pp": metrics54["return_pct"] - compound(baseline54),
        "delta65_pp": metrics65["return_pct"] - compound(baseline65),
    }


def select_best_rule(rules: Sequence[ExitRule], trades: Sequence[Trade]) -> ExitRule:
    ranked = []
    for rule in rules:
        values = exit_returns(trades, rule)
        metrics = return_metrics(values, trades)
        ranked.append((objective(metrics), metrics["return_pct"], rule.name, rule))
    ranked.sort(reverse=True, key=lambda row: (row[0], row[1], row[2]))
    return ranked[0][3]


def walk_forward_exit(rules: Sequence[ExitRule], trades: Sequence[Trade]) -> dict[str, Any]:
    months = sorted({trade.month for trade in trades})
    test_months = months[WF_FIRST_TEST_MONTH_INDEX:]
    stitched: list[float] = []
    baseline: list[float] = []
    stitched_trades: list[Trade] = []
    selections: list[dict[str, Any]] = []
    control = baseline_rule()
    for test_month in test_months:
        train = [trade for trade in trades if trade.month < test_month]
        test = [trade for trade in trades if trade.month == test_month]
        selected = select_best_rule(rules, train)
        selected_values = exit_returns(test, selected)
        baseline_values = exit_returns(test, control)
        stitched.extend(selected_values)
        baseline.extend(baseline_values)
        stitched_trades.extend(test)
        selections.append(
            {
                "test_month": test_month,
                "training_months": sorted({trade.month for trade in train}),
                "training_trades": len(train),
                "selected_rule": selected.name,
                "test_return_pct": compound(selected_values),
                "baseline_test_return_pct": compound(baseline_values),
                "delta_pp": compound(selected_values) - compound(baseline_values),
            }
        )
    return {
        "first_test_month": test_months[0],
        "test_months": test_months,
        "test_trades": len(stitched_trades),
        "selected_rule_frequency": dict(sorted(Counter(row["selected_rule"] for row in selections).items())),
        "strategy": return_metrics(stitched, stitched_trades),
        "baseline": return_metrics(baseline, stitched_trades),
        "delta": month_delta_stats(stitched, baseline, stitched_trades),
        "selections": selections,
    }


def leave_one_month_out_exit(rules: Sequence[ExitRule], trades: Sequence[Trade]) -> dict[str, Any]:
    months = sorted({trade.month for trade in trades})
    stitched: list[float] = []
    baseline: list[float] = []
    stitched_trades: list[Trade] = []
    selections: list[dict[str, Any]] = []
    control = baseline_rule()
    for held_out in months:
        train = [trade for trade in trades if trade.month != held_out]
        test = [trade for trade in trades if trade.month == held_out]
        selected = select_best_rule(rules, train)
        selected_values = exit_returns(test, selected)
        baseline_values = exit_returns(test, control)
        stitched.extend(selected_values)
        baseline.extend(baseline_values)
        stitched_trades.extend(test)
        selections.append(
            {
                "held_out_month": held_out,
                "selected_rule": selected.name,
                "held_out_return_pct": compound(selected_values),
                "baseline_held_out_return_pct": compound(baseline_values),
                "delta_pp": compound(selected_values) - compound(baseline_values),
            }
        )
    order = sorted(range(len(stitched_trades)), key=lambda index: stitched_trades[index].date)
    stitched = [stitched[index] for index in order]
    baseline = [baseline[index] for index in order]
    stitched_trades = [stitched_trades[index] for index in order]
    return {
        "months": months,
        "selected_rule_frequency": dict(sorted(Counter(row["selected_rule"] for row in selections).items())),
        "strategy": return_metrics(stitched, stitched_trades),
        "baseline": return_metrics(baseline, stitched_trades),
        "delta": month_delta_stats(stitched, baseline, stitched_trades),
        "selections": selections,
    }


def directional_exit_variants() -> list[tuple[str, ExitRule, ExitRule]]:
    base = baseline_rule()
    values: list[tuple[str, ExitRule, ExitRule]] = [("directional_baseline", base, base)]
    alternatives = [
        ExitRule("sl0.75", stop_pct=0.75),
        ExitRule("sl1.25", stop_pct=1.25),
        ExitRule("sl1.5", stop_pct=1.5),
        ExitRule("target0.5", target_multiple=0.5),
        ExitRule("target0.75", target_multiple=0.75),
        ExitRule("target1.25", target_multiple=1.25),
        ExitRule("time1400", time_exit="14:00"),
        ExitRule("time1600", time_exit="16:00"),
        ExitRule("time1700", time_exit="17:00"),
    ]
    for alternative in alternatives:
        values.append((f"long_{alternative.name}", alternative, base))
        values.append((f"short_{alternative.name}", base, alternative))
    return values


def risk_multiplier(name: str, trade: Trade, state: dict[str, Any]) -> float:
    if name == "equal_notional":
        return 1.0
    if name == "long_half":
        return 0.5 if trade.direction == "long" else 1.0
    if name == "long_quarter":
        return 0.25 if trade.direction == "long" else 1.0
    if name == "long_three_quarter":
        return 0.75 if trade.direction == "long" else 1.0
    if name == "short_three_quarter":
        return 0.75 if trade.direction == "short" else 1.0
    if name == "high_vol_half":
        return 0.5 if trade.features.vol_ratio_5_20 > 1.20 else 1.0
    if name == "inverse_vol_clip":
        return min(1.0, max(0.5, 1.0 / max(trade.features.vol_ratio_5_20, 1e-12)))
    if name == "weak_regime_half":
        return 0.5 if trade.features.regime_strength_z < 0.50 else 1.0
    if name == "loss_streak2_half_until_win":
        return 0.5 if int(state["loss_streak"]) >= 2 else 1.0
    if name == "drawdown3_half":
        return 0.5 if float(state["pre_trade_drawdown_pct"]) <= -3.0 else 1.0
    if name == "drawdown_tiered":
        drawdown = float(state["pre_trade_drawdown_pct"])
        if drawdown <= -6.0:
            return 0.5
        if drawdown <= -3.0:
            return 0.75
        return 1.0
    raise RuntimeError(f"Unknown risk rule: {name}")


RISK_RULES = (
    "equal_notional",
    "long_half",
    "long_quarter",
    "long_three_quarter",
    "short_three_quarter",
    "high_vol_half",
    "inverse_vol_clip",
    "weak_regime_half",
    "loss_streak2_half_until_win",
    "drawdown3_half",
    "drawdown_tiered",
)


STATELESS_RISK_RULES = (
    "equal_notional",
    "long_half",
    "long_quarter",
    "long_three_quarter",
    "short_three_quarter",
    "high_vol_half",
    "inverse_vol_clip",
    "weak_regime_half",
)


def simulate_account(trades: Sequence[Trade], rule_name: str) -> dict[str, Any]:
    base_exit = baseline_rule()
    equity = START_EQUITY
    peak = equity
    loss_streak = 0
    per_trade: list[dict[str, Any]] = []
    monthly_start: dict[str, float] = {}
    monthly_end: dict[str, float] = {}
    for trade in trades:
        monthly_start.setdefault(trade.month, equity)
        pre_dd = (equity / peak - 1.0) * 100.0
        state = {"loss_streak": loss_streak, "pre_trade_drawdown_pct": pre_dd}
        multiplier = risk_multiplier(rule_name, trade, state)
        result = simulate_exit(trade, base_exit)
        position = min(POSITION_CAP, MAX_LEVERAGE * equity) * multiplier
        pnl = position * result.net_return_pct / 100.0
        before = equity
        equity += pnl
        if equity <= 0.0:
            raise RuntimeError(f"Account depleted: {rule_name} {trade.date}")
        peak = max(peak, equity)
        drawdown = (equity / peak - 1.0) * 100.0
        if result.net_return_pct > 0.0:
            loss_streak = 0
        else:
            loss_streak += 1
        monthly_end[trade.month] = equity
        per_trade.append(
            {
                "date": trade.date,
                "month": trade.month,
                "symbol": trade.symbol,
                "direction": trade.direction,
                "net_return_pct": result.net_return_pct,
                "multiplier": multiplier,
                "position_rub": position,
                "equity_before": before,
                "pnl_rub": pnl,
                "equity_after": equity,
                "drawdown_pct": drawdown,
            }
        )
    monthly = {
        month: (monthly_end[month] / monthly_start[month] - 1.0) * 100.0
        for month in sorted(monthly_start)
    }
    pnl_positive = sorted(
        (row["pnl_rub"] for row in per_trade if row["pnl_rub"] > 0.0), reverse=True
    )
    total_positive = sum(pnl_positive)
    return {
        "rule": rule_name,
        "trades": len(trades),
        "wins": sum(row["pnl_rub"] > 0.0 for row in per_trade),
        "losses": sum(row["pnl_rub"] <= 0.0 for row in per_trade),
        "final_equity_rub": equity,
        "return_pct": (equity / START_EQUITY - 1.0) * 100.0,
        "max_drawdown_pct": min((row["drawdown_pct"] for row in per_trade), default=0.0),
        "worst_month_pct": min(monthly.values()) if monthly else 0.0,
        "positive_months": sum(value > 0.0 for value in monthly.values()),
        "weak_months_le_1pct": sum(value <= 1.0 for value in monthly.values()),
        "bottom_four_mean_pct": statistics.fmean(sorted(monthly.values())[:4]) if monthly else 0.0,
        "monthly": monthly,
        "average_multiplier": statistics.fmean(row["multiplier"] for row in per_trade),
        "minimum_multiplier": min((row["multiplier"] for row in per_trade), default=1.0),
        "reduced_trades": sum(row["multiplier"] < 1.0 for row in per_trade),
        "largest_winner_share_pct": (
            pnl_positive[0] / total_positive * 100.0 if total_positive > 0.0 else 0.0
        ),
        "top3_winner_share_pct": (
            sum(pnl_positive[:3]) / total_positive * 100.0 if total_positive > 0.0 else 0.0
        ),
        "per_trade": per_trade,
    }


def account_return_vector(result: dict[str, Any]) -> list[float]:
    return [
        row["pnl_rub"] / row["equity_before"] * 100.0 for row in result["per_trade"]
    ]


def sizing_walk_forward(trades: Sequence[Trade]) -> dict[str, Any]:
    months = sorted({trade.month for trade in trades})
    test_months = months[WF_FIRST_TEST_MONTH_INDEX:]
    full_results = {name: simulate_account(trades, name) for name in STATELESS_RISK_RULES}
    vectors = {name: account_return_vector(result) for name, result in full_results.items()}
    baseline_vector = vectors["equal_notional"]
    stitched: list[float] = []
    baseline: list[float] = []
    stitched_trades: list[Trade] = []
    selections = []
    for test_month in test_months:
        train_indices = [index for index, trade in enumerate(trades) if trade.month < test_month]
        test_indices = [index for index, trade in enumerate(trades) if trade.month == test_month]
        ranked = []
        train_trades = [trades[index] for index in train_indices]
        for name in STATELESS_RISK_RULES:
            values = [vectors[name][index] for index in train_indices]
            metrics = return_metrics(values, train_trades)
            ranked.append((objective(metrics), metrics["return_pct"], name))
        ranked.sort(reverse=True)
        selected_name = ranked[0][2]
        selected_values = [vectors[selected_name][index] for index in test_indices]
        control_values = [baseline_vector[index] for index in test_indices]
        test_trades = [trades[index] for index in test_indices]
        stitched.extend(selected_values)
        baseline.extend(control_values)
        stitched_trades.extend(test_trades)
        selections.append(
            {
                "test_month": test_month,
                "selected_rule": selected_name,
                "delta_pp": compound(selected_values) - compound(control_values),
            }
        )
    return {
        "test_months": test_months,
        "test_trades": len(stitched_trades),
        "selected_rule_frequency": dict(sorted(Counter(row["selected_rule"] for row in selections).items())),
        "strategy": return_metrics(stitched, stitched_trades),
        "baseline": return_metrics(baseline, stitched_trades),
        "delta": month_delta_stats(stitched, baseline, stitched_trades),
        "selections": selections,
    }


def build_report() -> dict[str, Any]:
    trades54, trades65, provenance = load_books()
    base = baseline_rule()
    base_full54 = [simulate_exit(trade, base) for trade in trades54]
    base_full65 = [simulate_exit(trade, base) for trade in trades65]
    max_replay_error = max(
        abs(result.gross_return_pct - trade.expected_gross_return_pct)
        for result, trade in zip(base_full65, trades65, strict=True)
    )
    baseline_table54 = [result.net_return_pct * table_multiplier(trade) for result, trade in zip(base_full54, trades54, strict=True)]
    baseline_table65 = [result.net_return_pct * table_multiplier(trade) for result, trade in zip(base_full65, trades65, strict=True)]
    baseline_metrics54 = return_metrics(baseline_table54, trades54)
    baseline_metrics65 = return_metrics(baseline_table65, trades65)
    baseline_fixed65 = simulate_account(trades65, "equal_notional")

    controls = {
        "max_trade_gross_replay_error_pct": max_replay_error,
        "ledger54_table_return_pct": baseline_metrics54["return_pct"],
        "ledger65_table_return_pct": baseline_metrics65["return_pct"],
        "ledger65_fixed_return_pct": baseline_fixed65["return_pct"],
        "expected": {
            "ledger54_table_return_pct": EXPECTED_54_TABLE_RETURN,
            "ledger65_table_return_pct": EXPECTED_65_TABLE_RETURN,
            "ledger65_fixed_return_pct": EXPECTED_65_FIXED_RETURN,
        },
    }
    if max_replay_error > 1e-10:
        raise RuntimeError(f"Baseline replay drift: {max_replay_error}")
    for key, expected in controls["expected"].items():
        if not math.isclose(float(controls[key]), expected, rel_tol=0.0, abs_tol=1e-9):
            raise RuntimeError(f"Control drift {key}: {controls[key]} != {expected}")

    grid = exit_grid()
    evaluated = [evaluate_exit_rule(rule, trades54, trades65) for rule in grid]
    ranked = sorted(
        evaluated,
        key=lambda row: (
            row["training_objective65"],
            row["ledger65"]["return_pct"],
            row["rule"]["name"],
        ),
        reverse=True,
    )
    top_rows = ranked[:10]
    best = ranked[0]
    best_rule = ExitRule(**best["rule"])
    best_values54 = exit_returns(trades54, best_rule)
    best_values65 = exit_returns(trades65, best_rule)
    best["bootstrap"] = {
        "ledger54": month_block_bootstrap(best_values54, baseline_table54, trades54),
        "ledger65": month_block_bootstrap(best_values65, baseline_table65, trades65),
    }
    best["concentration54"] = delta_concentration(
        best_values54, baseline_table54, trades54
    )

    robust_candidates = []
    for row in evaluated:
        rule = ExitRule(**row["rule"])
        values54 = exit_returns(trades54, rule)
        values = exit_returns(trades65, rule)
        bootstrap54 = month_block_bootstrap(values54, baseline_table54, trades54)
        bootstrap = month_block_bootstrap(values, baseline_table65, trades65)
        concentration54 = delta_concentration(values54, baseline_table54, trades54)
        checks = {
            "return_improves_54": row["delta54_pp"] > 0.0,
            "return_improves_65": row["delta65_pp"] > 0.0,
            "mdd_not_worse_54": row["ledger54"]["max_drawdown_pct"] >= baseline_metrics54["max_drawdown_pct"] - 1e-12,
            "mdd_not_worse_65": row["ledger65"]["max_drawdown_pct"] >= baseline_metrics65["max_drawdown_pct"] - 1e-12,
            "positive_delta_months_ge_6": row["month_delta"]["positive_delta_months"] >= 6,
            "bootstrap_ci_low_positive_54": bootstrap54["delta_ci95_pp"][0] > 0.0,
            "bootstrap_ci_low_positive_65": bootstrap["delta_ci95_pp"][0] > 0.0,
            "delta_survives_best_trade_removal_54": concentration54["delta_without_best_contributor_pp"] > 0.0,
            "delta_survives_best_trade_removal_65": row["concentration"]["delta_without_best_contributor_pp"] > 0.0,
        }
        if all(checks.values()):
            robust_candidates.append(
                {
                    "rule": row["rule"],
                    "checks": checks,
                    "bootstrap": {"ledger54": bootstrap54, "ledger65": bootstrap},
                }
            )

    directional_rows = []
    for name, long_rule, short_rule in directional_exit_variants():
        values54 = exit_returns(trades54, long_rule, short_rule)
        values65 = exit_returns(trades65, long_rule, short_rule)
        metrics54 = return_metrics(values54, trades54)
        metrics65 = return_metrics(values65, trades65)
        directional_rows.append(
            {
                "name": name,
                "long_rule": asdict(long_rule),
                "short_rule": asdict(short_rule),
                "ledger54": metrics54,
                "ledger65": metrics65,
                "delta54_pp": metrics54["return_pct"] - baseline_metrics54["return_pct"],
                "delta65_pp": metrics65["return_pct"] - baseline_metrics65["return_pct"],
                "month_delta": month_delta_stats(values65, baseline_table65, trades65),
                "concentration": delta_concentration(values65, baseline_table65, trades65),
            }
        )
    directional_ranked = sorted(
        directional_rows,
        key=lambda row: (objective(row["ledger65"]), row["ledger65"]["return_pct"]),
        reverse=True,
    )

    risk65 = {name: simulate_account(trades65, name) for name in RISK_RULES}
    risk54 = {name: simulate_account(trades54, name) for name in RISK_RULES}
    baseline_account_vector65 = account_return_vector(risk65["equal_notional"])
    sizing_rows = []
    for name in RISK_RULES:
        row65 = risk65[name]
        row54 = risk54[name]
        vector = account_return_vector(row65)
        bootstrap = month_block_bootstrap(vector, baseline_account_vector65, trades65)
        concentration = delta_concentration(vector, baseline_account_vector65, trades65)
        sizing_rows.append(
            {
                "rule": name,
                "ledger54": {key: value for key, value in row54.items() if key != "per_trade"},
                "ledger65": {key: value for key, value in row65.items() if key != "per_trade"},
                "delta54_pp": row54["return_pct"] - risk54["equal_notional"]["return_pct"],
                "delta65_pp": row65["return_pct"] - risk65["equal_notional"]["return_pct"],
                "bootstrap": bootstrap,
                "concentration": concentration,
            }
        )
    sizing_ranked = sorted(
        sizing_rows,
        key=lambda row: (
            row["ledger65"]["return_pct"] + 0.5 * row["ledger65"]["max_drawdown_pct"] + 0.25 * row["ledger65"]["worst_month_pct"],
            row["ledger65"]["return_pct"],
        ),
        reverse=True,
    )

    weak_baseline_months = {
        month: value
        for month, value in baseline_metrics65["monthly"].items()
        if value <= 2.0
    }
    # Run selection-aware checks twice.  The 65-trade book includes the newly
    # generated October cohort; the original frozen 54-trade ledger is an
    # important sensitivity check because October can materially alter the
    # expanding selector's early training path.
    exit_wf65 = walk_forward_exit(grid, trades65)
    exit_wf54 = walk_forward_exit(grid, trades54)
    exit_lomo65 = leave_one_month_out_exit(grid, trades65)
    exit_lomo54 = leave_one_month_out_exit(grid, trades54)
    sizing_wf = sizing_walk_forward(trades65)

    promotion_checks = {
        "any_exit_rule_passes_all_robust_gates": bool(robust_candidates),
        "exit_walk_forward_return_beats_baseline_65": (
            exit_wf65["strategy"]["return_pct"] > exit_wf65["baseline"]["return_pct"]
        ),
        "exit_walk_forward_mdd_not_worse_65": (
            exit_wf65["strategy"]["max_drawdown_pct"] >= exit_wf65["baseline"]["max_drawdown_pct"]
        ),
        "exit_walk_forward_return_beats_baseline_54": (
            exit_wf54["strategy"]["return_pct"] > exit_wf54["baseline"]["return_pct"]
        ),
        "exit_walk_forward_mdd_not_worse_54": (
            exit_wf54["strategy"]["max_drawdown_pct"] >= exit_wf54["baseline"]["max_drawdown_pct"]
        ),
        "exit_lomo_return_beats_baseline_65": (
            exit_lomo65["strategy"]["return_pct"] > exit_lomo65["baseline"]["return_pct"]
        ),
        "exit_lomo_return_beats_baseline_54": (
            exit_lomo54["strategy"]["return_pct"] > exit_lomo54["baseline"]["return_pct"]
        ),
        "sizing_walk_forward_return_beats_baseline": (
            sizing_wf["strategy"]["return_pct"] > sizing_wf["baseline"]["return_pct"]
        ),
        "sizing_walk_forward_mdd_not_worse": (
            sizing_wf["strategy"]["max_drawdown_pct"] >= sizing_wf["baseline"]["max_drawdown_pct"]
        ),
    }
    verdict = "NO-GO" if not all(promotion_checks.values()) else "ROBUST_CANDIDATE"

    return {
        "schema_version": 1,
        "artifact_type": "argonus_flat_month_exit_risk_research",
        "classification": "RESEARCH_ONLY_CURRENT_VERSION_IN_SAMPLE_WITH_SELECTION_AWARE_VALIDATION",
        "generated_for_date": "2026-07-18",
        "provenance": provenance,
        "method": {
            "fee_pct_round_trip": FEE_PCT,
            "entry_time": "07:00",
            "baseline": asdict(base),
            "table_sizing": "0.5x long / 1.0x short compounded",
            "fixed_sizing": "min(150000 RUB, 3x pre-trade equity) times causal rule multiplier",
            "exit_grid": {
                "stop_pct": [0.75, 1.0, 1.25, 1.5],
                "target_multiple_of_entry_to_frozen_t4_distance": [0.5, 0.75, 1.0, 1.25],
                "time_exit": ["12:00", "14:00", "16:00", "17:00", "18:35"],
                "rules": len(grid),
            },
            "training_objective": "return_pct + 0.50*max_drawdown_pct + 0.25*worst_month_pct",
            "walk_forward": {
                "ledger65": "initial Oct-Dec train, then expanding monthly Jan-Jul test",
                "ledger54": "initial Nov-Jan train, then expanding monthly Feb-Jul test",
            },
            "lomo": "choose exit rule on every other month of the same ledger, score held-out month",
            "bootstrap": "5000 month-block samples with replacement, seed 20260718",
            "causality": [
                "Every exit uses only candles observed through its exit event/time.",
                "Daily volatility/regime features use rows with date < trade date.",
                "Loss-streak/drawdown sizing observes only prior closed trades.",
            ],
        },
        "controls": controls,
        "baseline": {
            "ledger54_table": baseline_metrics54,
            "ledger65_table": baseline_metrics65,
            "ledger65_fixed_equal_notional": {key: value for key, value in baseline_fixed65.items() if key != "per_trade"},
            "direction_counts65": dict(sorted(Counter(trade.direction for trade in trades65).items())),
            "weak_months_le_2pct": weak_baseline_months,
        },
        "exit_grid": {
            "tested_rules": len(grid),
            "top10_in_sample_training_objective": top_rows,
            "best_in_sample": best,
            "robust_candidates": robust_candidates,
            "expanding_walk_forward": {
                "ledger65": exit_wf65,
                "ledger54": exit_wf54,
            },
            "leave_one_month_out": {
                "ledger65": exit_lomo65,
                "ledger54": exit_lomo54,
            },
        },
        "directional_exit": {
            "tested_rules": len(directional_rows),
            "top10_in_sample_training_objective": directional_ranked[:10],
        },
        "sizing": {
            "tested_rules": len(sizing_rows),
            "ranked_by_return_dd_tail_utility": sizing_ranked,
            "stateless_expanding_walk_forward": sizing_wf,
        },
        "promotion_gate": promotion_checks,
        "verdict": {
            "status": verdict,
            "production_change_authorized": False,
            "reason": (
                "No exit/risk rule may be promoted unless it improves both ledgers, "
                "survives month holdouts/bootstrap and concentration checks, and its "
                "expanding walk-forward return and drawdown both beat baseline."
            ),
        },
    }


def main() -> int:
    args = parse_args()
    report = build_report()
    if args.verify_controls:
        print(
            "controls ok: "
            f"54={report['controls']['ledger54_table_return_pct']:.12f}% "
            f"65={report['controls']['ledger65_table_return_pct']:.12f}% "
            f"fixed65={report['controls']['ledger65_fixed_return_pct']:.12f}%"
        )
        return 0
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            sort_keys=True,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
