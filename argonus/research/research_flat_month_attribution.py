#!/usr/bin/env python3
"""Read-only attribution of flat months in the frozen Engine-A + RS5 book.

The script deliberately does not import ``trade_bot`` or a broker client.  It
hash-checks the frozen candidate/session inputs, rebuilds the exact 54-trade
RS5 ledger, joins the October current-version reconstruction for context, and
prints a machine-readable JSON report.  It never writes files or changes the
production configuration.

Two sizing conventions are kept separate:

* published table: ``(gross - 0.08%) * (0.5 long, 1.0 short)`` compounded;
* current target-notional research: up to 150,000 RUB at no more than 3x
  current equity, with no long discount once fixed target sizing is active.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable

from argonus.backtesting import backtest_live_61_trades as exact
from argonus.research import research_fixed_150k_sizing as fixed


from argonus.paths import PROJECT_ROOT as ROOT
SNAPSHOT = ROOT / "data/backtests/live_rr2_t4_candidates_2025-11_2026-07.json"
FALLBACK = ROOT / "data/backtests/five_min_fallback_5_sessions.json"
FIVE_MIN_DIR = ROOT / "data/intraday_universe/five_min"
INDEX_DAILY = ROOT / "data/intraday_universe/index_daily.json"
DAILIES_DIR = ROOT / "data/intraday_universe/dailies"
OCTOBER_REPORT = ROOT / "data/backtests/OCTOBER_ENGINE_A_RS5_RESEARCH_2025_10.json"
OCTOBER_CACHE = ROOT / "data/backtests/october_engine_a_rs5_selected_5m_cache.json"

PINNED_SHA256 = {
    "argonus/backtesting/backtest_live_61_trades.py": "a6fa4fed7e298ea1cd5842b748304472531ddfd4457d333a5a70b4bb48e2bd75",
    "argonus/research/research_fixed_150k_sizing.py": "355877a602fbc01ae70758e11709c18e76d85c73a3f179d2b5e305efadb479c3",
    "data/backtests/live_rr2_t4_candidates_2025-11_2026-07.json": "996ee71f4a081b631a59c3cdaf3e27db08f02fd12dda54a7bd3389620ed4b870",
    "data/backtests/five_min_fallback_5_sessions.json": "4779fa4a8adb7c9a7ee899cb29fb9646c39c91e7ae2d303c4dfa1031d62cd74a",
    "data/backtests/OCTOBER_ENGINE_A_RS5_RESEARCH_2025_10.json": "a43f41149227b1bea2ffc3561df6cd04c8c919c9851902283834994c2d9f271b",
    "data/backtests/october_engine_a_rs5_selected_5m_cache.json": "027565e52e21e67a35c1689899e6b79ef3bc7cab55b0a2d156294cb82bbe6318",
    "data/intraday_universe/index_daily.json": "e230c2f987cd84bd7070592679dd30e3bc06a399058ead3e8b73692b1bfc46de",
}

FOCUS_MONTHS = ("2025-10", "2025-11", "2025-12", "2026-01", "2026-03", "2026-05")
FROZEN_WEAK_MONTHS = set(FOCUS_MONTHS[1:])
STRONG_MONTHS = {"2026-02", "2026-04", "2026-06", "2026-07"}
ROUND_TRIP_COST_PCT = 0.08


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


def verify_inputs() -> dict[str, str]:
    actual: dict[str, str] = {}
    for relative, expected_hash in PINNED_SHA256.items():
        value = sha256_file(ROOT / relative)
        if value != expected_hash:
            raise RuntimeError(
                f"Pinned attribution input changed: {relative}; "
                f"expected {expected_hash}, got {value}."
            )
        actual[relative] = value
    return actual


def compound(values: Iterable[float]) -> float:
    return (math.prod(1.0 + value / 100.0 for value in values) - 1.0) * 100.0


def table_return(direction: str, gross_return_pct: float) -> float:
    multiplier = 0.5 if direction == "long" else 1.0
    return (gross_return_pct - ROUND_TRIP_COST_PCT) * multiplier


def minutes_from_entry(clock: str) -> int:
    hour, minute = map(int, clock.split(":"))
    return hour * 60 + minute - 7 * 60


def wilson_interval(wins: int, total: int, z: float = 1.959963984540054) -> list[float]:
    if total <= 0:
        return [0.0, 0.0]
    proportion = wins / total
    denominator = 1.0 + z * z / total
    center = (proportion + z * z / (2.0 * total)) / denominator
    half_width = z * math.sqrt(
        proportion * (1.0 - proportion) / total + z * z / (4.0 * total * total)
    ) / denominator
    return [(center - half_width) * 100.0, (center + half_width) * 100.0]


def fisher_two_sided(a: int, b: int, c: int, d: int) -> float:
    """Exact two-sided Fisher probability for [[a,b],[c,d]]."""
    total = a + b + c + d
    successes = a + c
    first_size = a + b
    denominator = math.comb(total, first_size)

    def probability(value: int) -> float:
        return (
            math.comb(successes, value)
            * math.comb(total - successes, first_size - value)
            / denominator
        )

    lower = max(0, first_size - (total - successes))
    upper = min(first_size, successes)
    observed = probability(a)
    return sum(
        probability(value)
        for value in range(lower, upper + 1)
        if probability(value) <= observed + 1e-15
    )


def load_index_rows() -> list[tuple[date, float]]:
    payload = json.loads(INDEX_DAILY.read_text(encoding="utf-8"))
    return [(date.fromisoformat(str(row[0])), float(row[1])) for row in payload]


def load_stock_rows(symbol: str) -> list[tuple[date, float]]:
    payload = json.loads((DAILIES_DIR / f"{symbol}.json").read_text(encoding="utf-8"))
    return [(date.fromisoformat(str(row[0])), float(row[4])) for row in payload]


def prior_values(rows: list[tuple[date, float]], trade_date: date) -> list[float]:
    return [value for session_date, value in rows if session_date < trade_date]


def ema(values: list[float], period: int) -> float:
    alpha = 2.0 / (period + 1.0)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def market_context(index_rows: list[tuple[date, float]], trade_date: date) -> dict[str, Any]:
    values = prior_values(index_rows, trade_date)
    if len(values) < 20:
        raise RuntimeError(f"Insufficient IMOEX context before {trade_date}.")
    return {
        "prior_close": values[-1],
        "ema20": ema(values[-20:], 20),
        "regime": "bullish" if values[-1] > ema(values[-20:], 20) else "bearish",
        "return_5d_pct": (values[-1] / values[-6] - 1.0) * 100.0,
    }


def aligned_rs5(
    index_rows: list[tuple[date, float]], symbol: str, direction: str, trade_date: date
) -> float:
    stock = prior_values(load_stock_rows(symbol), trade_date)
    index = prior_values(index_rows, trade_date)
    if len(stock) < 6 or len(index) < 6:
        raise RuntimeError(f"Insufficient RS5 history for {trade_date} {symbol}.")
    stock_return = (stock[-1] / stock[-6] - 1.0) * 100.0
    index_return = (index[-1] / index[-6] - 1.0) * 100.0
    return stock_return - index_return if direction == "long" else index_return - stock_return


def verify_october_cache() -> dict[str, Any]:
    payload = json.loads(OCTOBER_CACHE.read_text(encoding="utf-8"))
    expected = payload.pop("cache_sha256", None)
    actual = canonical_sha256(payload)
    payload["cache_sha256"] = expected
    if expected != actual:
        raise RuntimeError(f"October cache body changed: expected {expected}, got {actual}.")
    return payload


def load_exact_frozen_results() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], exact.SessionStore]:
    snapshot = exact.load_snapshot(SNAPSHOT)
    model = snapshot["execution_model"]
    store = exact.SessionStore(
        FIVE_MIN_DIR,
        FALLBACK,
        str(model["entry_time_msk"]),
        str(model["time_exit_msk"]),
        True,
    )
    config = exact.BacktestConfig(long_risk=1.0, short_risk=1.0)
    baseline = exact.build_report(snapshot, store, config)["trades"]
    results = {str(row["date"]): dict(row) for row in baseline}
    candidates = {str(row["date"]): dict(row) for row in snapshot["candidates"]}

    for trade_date, candidate in fixed.RS5_REPLACEMENTS.items():
        candles, source, digest = store.load(candidate)
        results[trade_date] = asdict(
            exact.simulate_trade(candidate, candles, source, digest, config)
        )
        candidates[trade_date] = dict(candidate)
    for trade_date in fixed.RS5_VETOES:
        results.pop(trade_date, None)
        candidates.pop(trade_date, None)
    ordered = [results[key] for key in sorted(results)]
    if len(ordered) != 54:
        raise RuntimeError(f"Expected 54 RS5 trades, reconstructed {len(ordered)}.")
    return ordered, candidates, store


def path_metrics(
    row: dict[str, Any], candles: list[exact.Candle]
) -> dict[str, float | int]:
    entry = float(row["entry_price"])
    exit_time = str(row["exit_time"])
    through_exit = [
        candle for candle in candles if "07:00" <= candle.time <= exit_time
    ]
    before_exit = [
        candle for candle in candles if "07:00" <= candle.time < exit_time
    ]
    if row["direction"] == "long":
        mfe = max((candle.high / entry - 1.0) * 100.0 for candle in through_exit)
        mae = max((1.0 - candle.low / entry) * 100.0 for candle in through_exit)
        pre_exit_mfe = max(
            [(candle.high / entry - 1.0) * 100.0 for candle in before_exit] or [0.0]
        )
        session_extreme = max(candle.high for candle in through_exit)
        target_gap = (float(row["target_price"]) - session_extreme) / entry * 100.0
    else:
        mfe = max((1.0 - candle.low / entry) * 100.0 for candle in through_exit)
        mae = max((candle.high / entry - 1.0) * 100.0 for candle in through_exit)
        pre_exit_mfe = max(
            [(1.0 - candle.low / entry) * 100.0 for candle in before_exit] or [0.0]
        )
        session_extreme = min(candle.low for candle in through_exit)
        target_gap = (session_extreme - float(row["target_price"])) / entry * 100.0
    return {
        "mfe_through_exit_pct": mfe,
        "mae_through_exit_pct": mae,
        "mfe_before_exit_bar_pct": pre_exit_mfe,
        "target_gap_at_best_extreme_pct": max(target_gap, 0.0),
        "minutes_to_exit": minutes_from_entry(exit_time),
    }


def enrich_frozen_rows(
    rows: list[dict[str, Any]],
    candidates: dict[str, dict[str, Any]],
    store: exact.SessionStore,
    index_rows: list[tuple[date, float]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        candidate = candidates[str(row["date"])]
        candles, _, _ = store.load(candidate)
        trade_date = date.fromisoformat(str(row["date"]))
        context = market_context(index_rows, trade_date)
        result.append(
            {
                **row,
                "table_return_pct": table_return(
                    str(row["direction"]), float(row["gross_return_pct"])
                ),
                "candidate_score": candidate.get("overall_score"),
                "selected_rank": candidate.get("rank"),
                "aligned_rs5_pp_local": aligned_rs5(
                    index_rows, str(row["symbol"]), str(row["direction"]), trade_date
                ),
                "market_regime": context["regime"],
                **path_metrics(row, candles),
            }
        )
    return result


def load_october_rows(index_rows: list[tuple[date, float]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    report = json.loads(OCTOBER_REPORT.read_text(encoding="utf-8"))
    cache = verify_october_cache()
    rows: list[dict[str, Any]] = []
    for raw in report["trades"]:
        row = dict(raw)
        key = f"{row['date']}|{row['symbol']}"
        candles = [
            exact.Candle(str(value[0]), *map(float, value[1:5]))
            for value in cache["selected_sessions"][key]["rows"]
        ]
        trade_date = date.fromisoformat(str(row["date"]))
        selected_day = next(
            day for day in report["selection_days"] if day["date"] == row["date"]
        )
        selected = selected_day["selected"]
        rows.append(
            {
                **row,
                "month": "2025-10",
                "table_return_pct": table_return(
                    str(row["direction"]), float(row["gross_return_pct"])
                ),
                "candidate_score": selected.get("overall_score"),
                "aligned_rs5_pp_local": aligned_rs5(
                    index_rows, str(row["symbol"]), str(row["direction"]), trade_date
                ),
                "market_regime": market_context(index_rows, trade_date)["regime"],
                **path_metrics(row, candles),
            }
        )
    return rows, report


def longest_losing_streak(returns: list[float]) -> int:
    longest = current = 0
    for value in returns:
        current = current + 1 if value <= 0.0 else 0
        longest = max(longest, current)
    return longest


def month_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [float(row["table_return_pct"]) for row in rows]
    positive = [value for value in returns if value > 0.0]
    negative = [value for value in returns if value <= 0.0]
    top = max(positive) if positive else 0.0
    without_top = list(returns)
    if positive:
        without_top.remove(top)
    gross_returns = [
        float(row["gross_return_pct"])
        * (0.5 if row["direction"] == "long" else 1.0)
        for row in rows
    ]
    return {
        "trades": len(rows),
        "wins": len(positive),
        "losses": len(negative),
        "win_rate_pct": len(positive) / len(rows) * 100.0,
        "win_rate_wilson_95_pct": wilson_interval(len(positive), len(rows)),
        "table_return_pct": compound(returns),
        "gross_before_cost_return_pct": compound(gross_returns),
        "cost_drag_pct_points": compound(gross_returns) - compound(returns),
        "arithmetic_return_sum_pct": sum(returns),
        "profit_factor_arithmetic": sum(positive) / abs(sum(negative)) if negative else None,
        "directions": dict(sorted(Counter(str(row["direction"]) for row in rows).items())),
        "exit_reasons": dict(sorted(Counter(str(row["exit_reason"]) for row in rows).items())),
        "top_winner_pct": top,
        "return_without_top_winner_pct": compound(without_top),
        "longest_losing_streak": longest_losing_streak(returns),
    }


def group_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [float(row["table_return_pct"]) for row in rows]
    winners = [value for value in returns if value > 0.0]
    losers = [value for value in returns if value <= 0.0]
    return {
        "trades": len(rows),
        "wins": len(winners),
        "losses": len(losers),
        "win_rate_pct": len(winners) / len(rows) * 100.0,
        "win_rate_wilson_95_pct": wilson_interval(len(winners), len(rows)),
        "compound_return_pct": compound(returns),
        "positive_return_sum_pct": sum(winners),
        "negative_return_sum_pct": sum(losers),
        "profit_factor_arithmetic": sum(winners) / abs(sum(losers)),
        "directions": dict(sorted(Counter(str(row["direction"]) for row in rows).items())),
        "exit_reasons": dict(sorted(Counter(str(row["exit_reason"]) for row in rows).items())),
    }


def month_market_return(index_rows: list[tuple[date, float]], month: str) -> float:
    values = [value for session, value in index_rows if session.strftime("%Y-%m") == month]
    prior = [value for session, value in index_rows if session.strftime("%Y-%m") < month]
    if not values or not prior:
        raise RuntimeError(f"No complete index context for {month}.")
    return (values[-1] / prior[-1] - 1.0) * 100.0


def watchlist_count(month: str) -> int:
    directory = ROOT / f"data/watchlists/generated_watchlists_{month.replace('-', '_')}"
    return len(list(directory.glob("watchlist_*.txt")))


def build_report() -> dict[str, Any]:
    pinned = verify_inputs()
    index_rows = load_index_rows()
    frozen_raw, frozen_candidates, store = load_exact_frozen_results()
    frozen_rows = enrich_frozen_rows(frozen_raw, frozen_candidates, store, index_rows)
    october_rows, october_report = load_october_rows(index_rows)
    all_rows = sorted([*october_rows, *frozen_rows], key=lambda row: str(row["date"]))

    month_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in all_rows:
        month_rows[str(row["month"])].append(row)

    baseline_snapshot = exact.load_snapshot(SNAPSHOT)
    baseline_counts = Counter(str(row["month"]) for row in baseline_snapshot["candidates"])
    selected_counts = Counter(str(row["month"]) for row in frozen_rows)
    fixed_monthly = {
        str(row["month"]): row
        for row in october_report["fixed_150k_on_50k_hybrid_oct_to_jul"]["monthly"]
    }

    monthly: dict[str, Any] = {}
    for month in sorted(month_rows):
        summary = month_summary(month_rows[month])
        summary.update(
            {
                "imoex_month_return_pct": month_market_return(index_rows, month),
                "watchlist_sessions": watchlist_count(month),
                "baseline_gate_passing_trades": (
                    len(month_rows[month]) if month == "2025-10" else baseline_counts[month]
                ),
                "rs5_trades": len(month_rows[month]),
                "fixed_150k_on_50k_month_return_pct": fixed_monthly[month][
                    "return_on_month_start_equity_pct"
                ],
            }
        )
        monthly[month] = summary

    weak_rows = [row for row in all_rows if str(row["month"]) in set(FOCUS_MONTHS)]
    strong_rows = [row for row in frozen_rows if str(row["month"]) in STRONG_MONTHS]
    weak_group = group_summary(weak_rows)
    strong_group = group_summary(strong_rows)
    weak_group["fisher_two_sided_win_rate_vs_strong_p"] = fisher_two_sided(
        weak_group["wins"], weak_group["losses"], strong_group["wins"], strong_group["losses"]
    )

    # Score attribution is valid only for unchanged rank-1 candidates.  The two
    # RS5 replacement scores are not in the frozen replacement records.
    unchanged = [
        row
        for row in frozen_rows
        if row["date"] not in fixed.RS5_REPLACEMENTS and row.get("candidate_score") is not None
    ]
    focus_unchanged = [row for row in unchanged if row["month"] in FROZEN_WEAK_MONTHS]

    def score_split(rows: list[dict[str, Any]]) -> dict[str, Any]:
        winner_scores = [
            float(row["candidate_score"])
            for row in rows
            if float(row["gross_return_pct"]) - ROUND_TRIP_COST_PCT > 0.0
        ]
        loser_scores = [
            float(row["candidate_score"])
            for row in rows
            if float(row["gross_return_pct"]) - ROUND_TRIP_COST_PCT <= 0.0
        ]
        return {
            "winner_n": len(winner_scores),
            "winner_mean": mean(winner_scores),
            "winner_median": median(winner_scores),
            "loser_n": len(loser_scores),
            "loser_mean": mean(loser_scores),
            "loser_median": median(loser_scores),
        }

    may_rows = month_rows["2026-05"]
    may_stops = [row for row in may_rows if row["exit_reason"] == "stop"]
    magn = next(row for row in may_rows if row["date"] == "2026-05-20")
    magn_target_net = abs(
        float(magn["target_price"]) / float(magn["entry_price"]) - 1.0
    ) * 100.0 - ROUND_TRIP_COST_PCT
    may_alt = [
        magn_target_net if row["date"] == "2026-05-20" else float(row["table_return_pct"])
        for row in may_rows
    ]

    return {
        "schema_version": 1,
        "scope": "read_only_flat_month_attribution_no_broker_imports",
        "pinned_inputs": pinned,
        "provenance": {
            "october": "current-version in-sample reconstruction; not OOS",
            "november_to_july": "hash-pinned 61-trade snapshot with explicit RS5 veto/replacement reconstruction",
            "standalone_frozen_54_ledger_exists": False,
            "original_top3_source_artifact_present": False,
            "candidate_counterfactual_limit": (
                "scratchpad/top3.json is referenced by the snapshot but absent; exact unused-candidate "
                "counterfactuals and non-passing-day gate reasons cannot be recovered"
            ),
        },
        "monthly": monthly,
        "focus_months": list(FOCUS_MONTHS),
        "focus_trades": [row for row in all_rows if row["month"] in set(FOCUS_MONTHS)],
        "weak_vs_strong": {"focus": weak_group, "strong": strong_group},
        "candidate_score_diagnostic": {
            "frozen_focus_months_unchanged_rank1": score_split(focus_unchanged),
            "full_frozen_book_unchanged_rank1": score_split(unchanged),
            "warning": "scores are not calibrated PnL forecasts; do not derive a threshold from this sample",
        },
        "may_path_diagnostic": {
            "stop_count": len(may_stops),
            "stops_with_pre_exit_mfe_at_least_0_5pct": sum(
                float(row["mfe_before_exit_bar_pct"]) >= 0.5 for row in may_stops
            ),
            "stop_pre_exit_mfe_pct": {
                str(row["symbol"]): float(row["mfe_before_exit_bar_pct"]) for row in may_stops
            },
            "magn_2026_05_20_target_gap_pct": magn["target_gap_at_best_extreme_pct"],
            "magn_2026_05_20_target_gap_rub": abs(
                float(magn["target_price"])
                - (
                    float(magn["target_price"])
                    + float(magn["target_gap_at_best_extreme_pct"])
                    * float(magn["entry_price"])
                    / 100.0
                )
            ),
            "may_return_if_magn_target_assumed_hit_pct": compound(may_alt),
            "warning": (
                "This is execution-model sensitivity, not permission to relabel the trade; "
                "the frozen result remains a time exit"
            ),
        },
        "rs5_diagnostic": {
            "focus_winner_mean_pp": mean(
                float(row["aligned_rs5_pp_local"])
                for row in frozen_rows
                if row["month"] in FROZEN_WEAK_MONTHS
                and float(row["gross_return_pct"]) - ROUND_TRIP_COST_PCT > 0.0
            ),
            "focus_loser_mean_pp": mean(
                float(row["aligned_rs5_pp_local"])
                for row in frozen_rows
                if row["month"] in FROZEN_WEAK_MONTHS
                and float(row["gross_return_pct"]) - ROUND_TRIP_COST_PCT <= 0.0
            ),
            "may": [
                {
                    "date": row["date"],
                    "symbol": row["symbol"],
                    "aligned_rs5_pp": row["aligned_rs5_pp_local"],
                    "win": float(row["gross_return_pct"]) - ROUND_TRIP_COST_PCT > 0.0,
                }
                for row in may_rows
            ],
            "warning": "RS5 is visibly non-monotonic here; tightening its floor is not supported",
        },
        "selection_opportunity": {
            month: {
                "watchlist_sessions": watchlist_count(month),
                "baseline_gate_passing": baseline_counts[month],
                "rs5_trades": selected_counts[month],
                "rs5_vetoes": baseline_counts[month] - selected_counts[month],
            }
            for month in sorted(selected_counts)
        },
        "limitations": [
            "All candidate/RS5 tuning overlaps the evaluated interval; this is not OOS evidence.",
            "Monthly samples contain only 3-11 trades and the ten-month hybrid contains only ten calendar blocks.",
            "Trade-close results omit slippage, lot rounding, margin funding, tax, gaps, and intrabar ordering beyond stop-first.",
            "October has different provenance from the frozen November-July ledger.",
            "Local daily files support contextual EMA20/RS5 attribution but are not standalone archived decision audits.",
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report()
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
    try:
        raise SystemExit(main())
    except (KeyError, TypeError, ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"attribution error: {exc}")
