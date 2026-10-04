#!/usr/bin/env python3
"""Leakage-aware selector study on the archived six-candidate watchlists.

This is a research-only program.  It has no broker imports and cannot place,
replace, or cancel an order.  It reconstructs causal pre-entry features from
strictly prior daily sessions, freezes exact 5-minute outcomes in a cache, and
compares the current Engine-A+RS5 ordering with simple one-feature rankings.

The current analyzer and reranker artifacts overlap the evaluated history.
Consequently even the chronological tests below are *selection-layer* tests,
not a clean end-to-end out-of-sample estimate.  The report makes that caveat
explicit and never updates a production artifact.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from argonus.research import research_fixed_150k_sizing as fixed
from argonus.research import selector_stack_ablation as shared
from argonus.watchlists import watchlist_best_target as wbt
from argonus.backtesting.backtest_generated_watchlists import discover_watchlists


from argonus.paths import PROJECT_ROOT as ROOT
DATA_DIR = ROOT / "data/backtests"
CACHE = DATA_DIR / "argonus.research.flat_month_selector_walkforward.cache.json"
JSON_REPORT = DATA_DIR / "argonus.research.flat_month_selector_walkforward.json"
MD_REPORT = DATA_DIR / "argonus.research.flat_month_selector_walkforward.md"

START = "2025-11-01"
END = "2026-07-16"
RS5_FLOOR = -2.39
FEE_PCT = 0.08
LONG_RISK = 0.5
SHORT_RISK = 1.0
BASELINE = "baseline_current_order_top3"

FEATURES: dict[str, str] = {
    "overall_score": "overall score",
    "prob_hit_t1": "P(hit T1)",
    "prob_hit_t3": "P(hit T3)",
    "expected_max_target": "expected maximum target",
    "expected_value_pct": "analyzer expected value",
    "raw_expected_utility_score": "raw expected utility",
    "entry_quality_score": "entry quality",
    "relative_strength_score": "analyzer relative-strength score",
    "freshness_score": "freshness",
    "vol_expansion": "volatility expansion",
    "directional_rs5_pp": "direction-aligned RS5",
    "reranker_any_target_prob": "any-target reranker probability",
    "reranker_winner_prob": "winner reranker probability",
    "reranker_t2plus_prob": "T2+ reranker probability",
    "standalone_selection_score": "standalone selection score",
}

FEATURE_FIELDS = tuple(FEATURES)
SOURCE_FILES = (
    "argonus/watchlists/watchlist_best_target.py",
    "argonus/research/selector_stack_ablation.py",
    "argonus/watchlists/shadow_rs5_selector.py",
    "models/same_day_top_reranker_model.json",
    "models/same_day_top_winner_reranker_model.json",
    "models/same_day_top_t2plus_reranker_model.json",
    "models/runner_day_confidence_model.json",
    "data/backtests/live_rr2_t4_candidates_2025-11_2026-07.json",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rebuild-cache",
        action="store_true",
        help="Rebuild the immutable research cache from local archived inputs.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Require and replay the existing cache; do not reconstruct analyses.",
    )
    parser.add_argument("--print-json", action="store_true")
    return parser.parse_args()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cache_with_hash(payload: dict[str, Any]) -> dict[str, Any]:
    clean = {key: value for key, value in payload.items() if key != "cache_sha256"}
    return {**clean, "cache_sha256": canonical_sha(clean)}


def validate_cache(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != 1:
        raise RuntimeError("Unsupported selector cache schema.")
    expected = payload.get("cache_sha256")
    actual = canonical_sha({key: value for key, value in payload.items() if key != "cache_sha256"})
    if expected != actual:
        raise RuntimeError(f"Selector cache hash mismatch: expected {expected}, got {actual}.")


def normalized_session_sha(rows: list[list[object]]) -> str:
    selected = [row[:5] for row in rows if "07:00" <= str(row[0]) <= "18:35"]
    return canonical_sha(selected)


def candidate_row(
    item: wbt.IdeaAnalysis,
    *,
    raw_item: wbt.IdeaAnalysis,
    t2_probability: float | None,
    rank: int,
    gate: str | None,
    candles: list[list[object]] | None,
) -> dict[str, Any]:
    risk = LONG_RISK if item.direction == "long" else SHORT_RISK
    outcome = None
    session_hash = None
    if candles and item.exit_target is not None:
        outcome = shared.exact_return(
            candles,
            item.direction,
            float(item.exit_target.price),
            FEE_PCT,
            risk,
        )
        session_hash = normalized_session_sha(candles)
    features: dict[str, float | None] = {}
    for field in FEATURE_FIELDS:
        if field == "reranker_t2plus_prob":
            value = t2_probability
        elif field in {"reranker_any_target_prob", "reranker_winner_prob"}:
            value = getattr(item, field)
        else:
            # Preserve the analyzer value before learned promotion mutates score.
            value = getattr(raw_item, field)
        features[field] = float(value) if value is not None and math.isfinite(float(value)) else None
    return {
        "symbol": item.symbol,
        "direction": item.direction,
        "current_rank": rank,
        "gate": gate,
        "rs5_pass": bool(
            raw_item.directional_rs5_pp is not None
            and raw_item.directional_rs5_pp >= RS5_FLOOR
        ),
        "target_label": item.exit_target.label if item.exit_target else None,
        "target_price": float(item.exit_target.price) if item.exit_target else None,
        "target_move_pct": (
            float(item.exit_target.move_pct_from_close) if item.exit_target else None
        ),
        "outcome_return_pct": outcome,
        "session_sha256": session_hash,
        "features": features,
    }


def build_cache() -> dict[str, Any]:
    watchlists = [
        entry
        for entry in discover_watchlists(str(ROOT / "data/watchlists"))
        if START <= entry.trade_date.isoformat() <= END
    ]
    if len(watchlists) != 174:
        raise RuntimeError(f"Expected 174 archived watchlists, found {len(watchlists)}.")
    symbols = {symbol for entry in watchlists for symbol in entry.symbols}
    shared.patch_prior_day_data(ROOT, symbols)
    store = shared.IntradayStore(ROOT)

    original_override = wbt.apply_same_day_top_override
    wbt.apply_same_day_top_override = lambda items: items
    days: list[dict[str, Any]] = []
    try:
        for entry in watchlists:
            raw = wbt.analyze_watchlist(entry.raw_text, entry.trade_date)
            raw_by_symbol = {item.symbol: item for item in raw}
            ranked = shared.current(copy.deepcopy(raw))
            t2_ranked = wbt.apply_learned_same_day_t2plus_reranker(copy.deepcopy(raw))
            t2_by_symbol = {
                item.symbol: item.reranker_t2plus_prob for item in t2_ranked
            }
            rows: list[dict[str, Any]] = []
            ds = entry.trade_date.isoformat()
            for rank, item in enumerate(ranked, start=1):
                raw_item = raw_by_symbol[item.symbol]
                gate = shared.gate_reason(item, entry.trade_date)
                candles = store.session(item.symbol, ds)
                rows.append(
                    candidate_row(
                        item,
                        raw_item=raw_item,
                        t2_probability=t2_by_symbol.get(item.symbol),
                        rank=rank,
                        gate=gate,
                        candles=candles,
                    )
                )
            days.append(
                {
                    "date": ds,
                    "month": ds[:7],
                    "watchlist_sha256": hashlib.sha256(
                        entry.raw_text.encode("utf-8")
                    ).hexdigest(),
                    "rank1_gate": rows[0]["gate"] if rows else "empty",
                    "candidates": rows,
                }
            )
    finally:
        wbt.apply_same_day_top_override = original_override

    combined_watchlists = canonical_sha(
        [[day["date"], day["watchlist_sha256"]] for day in days]
    )
    payload = {
        "schema_version": 1,
        "artifact_type": "flat_month_selector_walkforward_candidate_cache",
        "scope": {"start": START, "end": END},
        "execution": {
            "entry": "first 5-minute open at/after 07:00 MSK",
            "exit": "T4, 1% stop, stop-first, time exit at 18:35 MSK",
            "round_trip_cost_pct": FEE_PCT,
            "long_risk": LONG_RISK,
            "short_risk": SHORT_RISK,
        },
        "selection": {
            "baseline": BASELINE,
            "rs5_floor_pp": RS5_FLOOR,
            "alternative_day_set_broadened": False,
        },
        "provenance": {
            "source_sha256": {
                name: sha_file(ROOT / name) for name in SOURCE_FILES
            },
            "combined_watchlists_sha256": combined_watchlists,
            "daily_causality": "every analyzer session has trade_date < decision date",
            "session_hash_contract": "sha256(canonical JSON of 07:00-18:35 OHLC rows)",
        },
        "limitations": [
            "Current analyzer and reranker artifacts were fitted/tuned on dates overlapping this archive.",
            "Historical point-in-time broker trade/short availability, max lots, spread and depth are absent for alternatives.",
            "Only locally archived 5-minute paths are scored; missing candidate paths are never imputed.",
        ],
        "days": days,
    }
    return cache_with_hash(payload)


def admissible(day: dict[str, Any], top_k: int) -> list[dict[str, Any]]:
    return [
        row
        for row in day["candidates"]
        if int(row["current_rank"]) <= top_k
        and row["gate"] is None
        and row["rs5_pass"]
    ]


def analysis_days(cache: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    exclusions = Counter()
    for day in cache["days"]:
        if day["rank1_gate"] is not None:
            exclusions["rank1_existing_gate"] += 1
            continue
        top3 = admissible(day, 3)
        if not top3:
            exclusions["baseline_rs5_skip"] += 1
            continue
        pool6 = admissible(day, 6)
        if any(row["outcome_return_pct"] is None for row in pool6):
            exclusions["incomplete_common_top6_outcome"] += 1
            continue
        selected.append(day)
    coverage = {
        "watchlist_days": len(cache["days"]),
        "candidate_rows": sum(len(day["candidates"]) for day in cache["days"]),
        "candidate_rows_with_5m": sum(
            row["outcome_return_pct"] is not None
            for day in cache["days"]
            for row in day["candidates"]
        ),
        "common_decision_days": len(selected),
        "exclusions": dict(exclusions),
        "common_month_counts": dict(Counter(day["month"] for day in selected)),
    }
    return selected, coverage


def policy_names() -> list[str]:
    return [BASELINE] + [
        f"max_{feature}_top{top_k}"
        for top_k in (3, 6)
        for feature in FEATURES
    ]


def parse_policy(name: str) -> tuple[str, int]:
    if name == BASELINE:
        return "current_rank", 3
    if not name.startswith("max_") or "_top" not in name:
        raise ValueError(name)
    feature, raw_k = name[4:].rsplit("_top", 1)
    if feature not in FEATURES:
        raise ValueError(name)
    return feature, int(raw_k)


def choose(day: dict[str, Any], policy: str) -> dict[str, Any]:
    feature, top_k = parse_policy(policy)
    pool = admissible(day, top_k)
    if not pool:
        raise RuntimeError(f"No admissible candidate on common day {day['date']}.")
    if policy == BASELINE:
        return min(pool, key=lambda row: int(row["current_rank"]))

    def key(row: dict[str, Any]) -> tuple[float, int]:
        value = row["features"].get(feature)
        numeric = float(value) if value is not None else -math.inf
        return numeric, -int(row["current_rank"])

    return max(pool, key=key)


def ledger(days: Iterable[dict[str, Any]], policy: str) -> list[dict[str, Any]]:
    result = []
    for day in days:
        row = choose(day, policy)
        result.append(
            {
                "date": day["date"],
                "month": day["month"],
                "symbol": row["symbol"],
                "direction": row["direction"],
                "rank": row["current_rank"],
                "return_pct": float(row["outcome_return_pct"]),
            }
        )
    return result


def compound(values: Iterable[float]) -> float:
    return (math.prod(1.0 + value / 100.0 for value in values) - 1.0) * 100.0


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    capital = 1.0
    peak = 1.0
    mdd = 0.0
    for row in rows:
        capital *= 1.0 + float(row["return_pct"]) / 100.0
        peak = max(peak, capital)
        mdd = min(mdd, capital / peak - 1.0)
    months: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        months[row["month"]].append(row)
    return {
        "trades": len(rows),
        "wins": sum(float(row["return_pct"]) > 0.0 for row in rows),
        "losses": sum(float(row["return_pct"]) <= 0.0 for row in rows),
        "return_pct": (capital - 1.0) * 100.0,
        "max_drawdown_pct": mdd * 100.0,
        "monthly": {
            month: {
                "trades": len(items),
                "wins": sum(float(row["return_pct"]) > 0.0 for row in items),
                "losses": sum(float(row["return_pct"]) <= 0.0 for row in items),
                "return_pct": compound(float(row["return_pct"]) for row in items),
            }
            for month, items in sorted(months.items())
        },
    }


def slice_rows(rows: list[dict[str, Any]], start: str, end: str) -> list[dict[str, Any]]:
    return [row for row in rows if start <= row["month"] <= end]


def paired_log_deltas(
    baseline: list[dict[str, Any]], alternative: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if [row["date"] for row in baseline] != [row["date"] for row in alternative]:
        raise RuntimeError("Paired ledgers do not share the same calendar.")
    result = []
    for base, alt in zip(baseline, alternative):
        delta = math.log1p(float(alt["return_pct"]) / 100.0) - math.log1p(
            float(base["return_pct"]) / 100.0
        )
        if base["symbol"] != alt["symbol"]:
            result.append(
                {
                    "date": base["date"],
                    "month": base["month"],
                    "baseline_symbol": base["symbol"],
                    "alternative_symbol": alt["symbol"],
                    "baseline_return_pct": base["return_pct"],
                    "alternative_return_pct": alt["return_pct"],
                    "log_delta": delta,
                }
            )
    return result


def sign_flip_pvalue(deltas: list[float], seed: int) -> float:
    values = [value for value in deltas if abs(value) > 1e-15]
    if not values or sum(values) <= 0.0:
        return 1.0
    observed = sum(values)
    n = len(values)
    if n <= 20:
        total = 1 << n
        extreme = 0
        for mask in range(total):
            score = sum(value if mask & (1 << i) else -value for i, value in enumerate(values))
            extreme += score >= observed - 1e-15
        return extreme / total
    rng = random.Random(seed)
    draws = 200_000
    extreme = 1
    for _ in range(draws):
        score = sum(value if rng.getrandbits(1) else -value for value in values)
        extreme += score >= observed - 1e-15
    return extreme / (draws + 1)


def holm_adjust(raw: dict[str, float]) -> dict[str, float]:
    ordered = sorted(raw, key=raw.get)
    count = len(ordered)
    adjusted: dict[str, float] = {}
    running = 0.0
    for index, name in enumerate(ordered):
        running = max(running, min(1.0, raw[name] * (count - index)))
        adjusted[name] = running
    return adjusted


def window_evaluation(
    ledgers: dict[str, list[dict[str, Any]]], start: str, end: str
) -> dict[str, Any]:
    baseline = slice_rows(ledgers[BASELINE], start, end)
    raw_p: dict[str, float] = {}
    rows: dict[str, Any] = {}
    for index, name in enumerate(policy_names()[1:], start=1):
        alternative = slice_rows(ledgers[name], start, end)
        divergences = paired_log_deltas(baseline, alternative)
        raw_p[name] = sign_flip_pvalue(
            [row["log_delta"] for row in divergences], seed=20260718 + index
        )
        alt_summary = summarize(alternative)
        base_summary = summarize(baseline)
        rows[name] = {
            "summary": alt_summary,
            "delta_return_pp": alt_summary["return_pct"] - base_summary["return_pct"],
            "divergences": len(divergences),
            "raw_one_sided_sign_flip_p": raw_p[name],
        }
    adjusted = holm_adjust(raw_p)
    for name, value in adjusted.items():
        rows[name]["holm_adjusted_p"] = value
    return {"baseline": summarize(baseline), "alternatives": rows}


def month_omit_deltas(
    baseline: list[dict[str, Any]], alternative: list[dict[str, Any]]
) -> dict[str, float]:
    months = sorted({row["month"] for row in baseline})
    return {
        month: summarize([row for row in alternative if row["month"] != month])["return_pct"]
        - summarize([row for row in baseline if row["month"] != month])["return_pct"]
        for month in months
    }


def dependence(
    baseline: list[dict[str, Any]], alternative: list[dict[str, Any]]
) -> dict[str, Any]:
    divergences = paired_log_deltas(baseline, alternative)
    alt_summary = summarize(alternative)
    base_summary = summarize(baseline)
    leave_one: list[float] = []
    for omitted in divergences:
        base_kept = [row for row in baseline if row["date"] != omitted["date"]]
        alt_kept = [row for row in alternative if row["date"] != omitted["date"]]
        leave_one.append(
            summarize(alt_kept)["return_pct"] - summarize(base_kept)["return_pct"]
        )
    positive = [max(0.0, row["log_delta"]) for row in divergences]
    positive_total = sum(positive)
    top = max(divergences, key=lambda row: row["log_delta"], default=None)
    return {
        "delta_return_pp": alt_summary["return_pct"] - base_summary["return_pct"],
        "divergences": len(divergences),
        "positive_divergences": sum(value > 0.0 for value in positive),
        "largest_positive_share_pct": (
            max(positive, default=0.0) / positive_total * 100.0 if positive_total > 0.0 else None
        ),
        "largest_positive_divergence": top,
        "leave_one_divergence_out_delta_pp_range": (
            [min(leave_one), max(leave_one)] if leave_one else [0.0, 0.0]
        ),
        "leave_one_month_out_delta_pp": month_omit_deltas(baseline, alternative),
    }


def robust_train_choice(
    ledgers: dict[str, list[dict[str, Any]]], months: list[str], *, guarded: bool
) -> tuple[str, dict[str, Any]]:
    base = [row for row in ledgers[BASELINE] if row["month"] in months]
    candidates: list[tuple[float, str, dict[str, Any]]] = []
    p_raw: dict[str, float] = {}
    diagnostics: dict[str, dict[str, Any]] = {}
    for index, name in enumerate(policy_names()[1:], start=1):
        alt = [row for row in ledgers[name] if row["month"] in months]
        dep = dependence(base, alt)
        bsum, asum = summarize(base), summarize(alt)
        month_deltas = dep["leave_one_month_out_delta_pp"]
        divergences = paired_log_deltas(base, alt)
        p_raw[name] = sign_flip_pvalue(
            [row["log_delta"] for row in divergences], 117_000 + len(months) * 100 + index
        )
        diagnostics[name] = {
            "return_pct": asum["return_pct"],
            "delta_return_pp": asum["return_pct"] - bsum["return_pct"],
            "mdd_delta_pp": asum["max_drawdown_pct"] - bsum["max_drawdown_pct"],
            "divergences": dep["divergences"],
            "all_leave_one_train_month_deltas_positive": bool(month_deltas)
            and min(month_deltas.values()) > 0.0,
            "largest_positive_share_pct": dep["largest_positive_share_pct"],
        }
    adjusted = holm_adjust(p_raw)
    for name, row in diagnostics.items():
        row["raw_p"] = p_raw[name]
        row["holm_p"] = adjusted[name]
        if guarded:
            qualified = (
                row["delta_return_pp"] > 0.0
                and row["mdd_delta_pp"] >= -1e-12
                and row["divergences"] >= 4
                and row["all_leave_one_train_month_deltas_positive"]
                and row["holm_p"] <= 0.10
                and (
                    row["largest_positive_share_pct"] is not None
                    and row["largest_positive_share_pct"] <= 35.0
                )
            )
        else:
            qualified = row["delta_return_pp"] > 0.0
        row["qualified"] = qualified
        if qualified:
            candidates.append((row["return_pct"], name, row))
    if not candidates:
        return BASELINE, {
            "reason": "no_alternative_passed_guard" if guarded else "no_positive_alternative",
            "evaluated_alternatives": len(diagnostics),
        }
    _, name, row = max(candidates)
    return name, row


def walkforward(
    ledgers: dict[str, list[dict[str, Any]]], *, guarded: bool
) -> dict[str, Any]:
    months = sorted({row["month"] for row in ledgers[BASELINE]})
    eval_months = months[3:]
    chosen_rows: list[dict[str, Any]] = []
    selections: list[dict[str, Any]] = []
    for month in eval_months:
        prior = [value for value in months if value < month]
        policy, diagnostics = robust_train_choice(ledgers, prior, guarded=guarded)
        month_rows = [row for row in ledgers[policy] if row["month"] == month]
        chosen_rows.extend(month_rows)
        selections.append(
            {
                "test_month": month,
                "train_months": prior,
                "selected_policy": policy,
                "training_diagnostics": diagnostics,
                "test_summary": summarize(month_rows),
            }
        )
    baseline_eval = [row for row in ledgers[BASELINE] if row["month"] in eval_months]
    return {
        "evaluation_months": eval_months,
        "baseline": summarize(baseline_eval),
        "walkforward": summarize(chosen_rows),
        "delta_return_pp": summarize(chosen_rows)["return_pct"]
        - summarize(baseline_eval)["return_pct"],
        "selections": selections,
    }


def frozen_control() -> dict[str, Any]:
    _, ledger54, pinned = fixed.load_ledgers()
    rows = [
        {
            "date": row.date,
            "month": row.month,
            "symbol": row.symbol,
            "direction": row.direction,
            "return_pct": (row.gross_return_pct - FEE_PCT)
            * (LONG_RISK if row.direction == "long" else SHORT_RISK),
        }
        for row in ledger54
    ]
    return {
        "summary": summarize(rows),
        "pinned_inputs": pinned,
        "warning": "Exact 54-trade control; alternative rankings use the separate 48-day common reconstruction and must not be spliced into this ledger.",
    }


def oracle_ledger(days: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
    rows = []
    for day in days:
        candidate = max(
            admissible(day, top_k), key=lambda row: float(row["outcome_return_pct"])
        )
        rows.append(
            {
                "date": day["date"],
                "month": day["month"],
                "symbol": candidate["symbol"],
                "direction": candidate["direction"],
                "rank": candidate["current_rank"],
                "return_pct": candidate["outcome_return_pct"],
            }
        )
    return rows


def build_report(cache: dict[str, Any]) -> dict[str, Any]:
    days, coverage = analysis_days(cache)
    names = policy_names()
    ledgers = {name: ledger(days, name) for name in names}
    summaries = {name: summarize(rows) for name, rows in ledgers.items()}

    train_eval = window_evaluation(ledgers, "2025-11", "2026-01")
    validation_eval = window_evaluation(ledgers, "2026-02", "2026-03")
    test_eval = window_evaluation(ledgers, "2026-04", "2026-07")
    full_eval = window_evaluation(ledgers, "2025-11", "2026-07")

    train_months = ["2025-11", "2025-12", "2026-01"]
    locked_aggressive, aggressive_diag = robust_train_choice(
        ledgers, train_months, guarded=False
    )
    locked_guarded, guarded_diag = robust_train_choice(
        ledgers, train_months, guarded=True
    )
    best_hindsight = max(
        names[1:], key=lambda name: summaries[name]["return_pct"]
    )

    # Keep the weak-month hypotheses visible even when they lose the full
    # archive.  This prevents a targeted May improvement from being presented
    # without its earlier-window failures.
    shadow_candidate = "max_vol_expansion_top6"
    focal = sorted(
        {
            locked_aggressive,
            locked_guarded,
            best_hindsight,
            shadow_candidate,
            "max_directional_rs5_pp_top6",
            "max_expected_value_pct_top3",
        }
        - {BASELINE}
    )
    focal_results = {
        name: {
            "description": (
                FEATURES[parse_policy(name)[0]] + f", top-{parse_policy(name)[1]}"
            ),
            "all": summaries[name],
            "train": train_eval["alternatives"][name],
            "validation": validation_eval["alternatives"][name],
            "test": test_eval["alternatives"][name],
            "dependence_full": dependence(ledgers[BASELINE], ledgers[name]),
            "weak_month_divergences": [
                row
                for row in paired_log_deltas(ledgers[BASELINE], ledgers[name])
                if row["month"] in {"2025-11", "2025-12", "2026-05"}
            ],
        }
        for name in focal
    }

    weak_month_scan: dict[str, Any] = {}
    for month in ("2025-11", "2025-12", "2026-05"):
        base_month = [row for row in ledgers[BASELINE] if row["month"] == month]
        base_month_summary = summarize(base_month)
        ranked_month = []
        for name in names[1:]:
            alt_month = [row for row in ledgers[name] if row["month"] == month]
            alt_month_summary = summarize(alt_month)
            ranked_month.append(
                (
                    alt_month_summary["return_pct"] - base_month_summary["return_pct"],
                    name,
                    alt_month_summary,
                )
            )
        best_delta, best_name, best_month_summary = max(
            ranked_month, key=lambda item: item[0]
        )
        if best_delta <= 1e-12:
            weak_month_scan[month] = {
                "baseline": base_month_summary,
                "best_policy": None,
                "best_month_delta_pp": 0.0,
                "interpretation": "No tested ranking improves this month on the common executable pool.",
            }
            continue
        weak_month_scan[month] = {
            "baseline": base_month_summary,
            "best_policy": best_name,
            "best_policy_month": best_month_summary,
            "best_month_delta_pp": best_delta,
            "best_policy_full_delta_pp": summaries[best_name]["return_pct"]
            - summaries[BASELINE]["return_pct"],
            "train_delta_pp": train_eval["alternatives"][best_name]["delta_return_pp"],
            "validation_delta_pp": validation_eval["alternatives"][best_name]["delta_return_pp"],
            "test_delta_pp": test_eval["alternatives"][best_name]["delta_return_pp"],
            "month_divergences": [
                row
                for row in paired_log_deltas(ledgers[BASELINE], ledgers[best_name])
                if row["month"] == month
            ],
            "interpretation": "Hindsight-selected for this month; invalid as deployable evidence.",
        }

    # Rank exploratory policies separately in each window; this exposes how
    # unstable the apparent winner is without silently treating it as OOS.
    ranking = {}
    for window_name, window in (
        ("train", train_eval),
        ("validation", validation_eval),
        ("test", test_eval),
        ("full_hindsight", full_eval),
    ):
        ranking[window_name] = sorted(
            (
                {
                    "policy": name,
                    **window["alternatives"][name],
                }
                for name in names[1:]
            ),
            key=lambda row: row["delta_return_pp"],
            reverse=True,
        )[:8]

    report = {
        "schema_version": 1,
        "artifact_type": "flat_month_selector_walkforward_research",
        "created_for_date": "2026-07-18",
        "verdict": {
            "production_change": "REJECT",
            "shadow_test": {
                "policy": shadow_candidate,
                "status": "DATA_COLLECTION_ONLY_NOT_AN_ORDER_DECISION",
                "reason": (
                    "It is the only simple rule that turned the reconstructed May sample positive, "
                    "but it lost over the full archive and therefore has no activation case."
                ),
            },
            "reason": "Filled after robustness results are computed below; no artifact in this program can change production.",
        },
        "data": {
            "cache": str(CACHE.relative_to(ROOT)),
            "cache_sha256": cache["cache_sha256"],
            "coverage": coverage,
            "limitations": cache["limitations"],
        },
        "execution_and_selection_contract": cache["execution"] | cache["selection"],
        "exact_frozen_rs5_control": frozen_control(),
        "common_reconstruction_baseline": summaries[BASELINE],
        "split": {
            "train": {"months": "2025-11..2026-01", **train_eval},
            "validation": {"months": "2026-02..2026-03", **validation_eval},
            "test": {"months": "2026-04..2026-07-16", **test_eval},
        },
        "policy_family": {
            "baseline": BASELINE,
            "alternative_count": len(names) - 1,
            "features": FEATURES,
            "top_k_values": [3, 6],
            "multiple_testing": "one-sided paired log-return sign-flip tests; Holm-Bonferroni separately per window",
        },
        "locked_train_selection": {
            "aggressive_best_train": locked_aggressive,
            "aggressive_training_diagnostics": aggressive_diag,
            "guarded_choice": locked_guarded,
            "guarded_training_diagnostics": guarded_diag,
        },
        "walkforward": {
            "aggressive": walkforward(ledgers, guarded=False),
            "guarded": walkforward(ledgers, guarded=True),
        },
        "focal_policies": focal_results,
        "weak_month_scan": weak_month_scan,
        "top_exploratory_rankings": ranking,
        "hindsight_oracle_not_a_strategy": {
            "top3": summarize(oracle_ledger(days, 3)),
            "top6": summarize(oracle_ledger(days, 6)),
            "warning": "Uses realized same-day returns to pick the candidate and is an unattainable opportunity bound.",
        },
    }

    guarded = report["walkforward"]["guarded"]
    aggressive = report["walkforward"]["aggressive"]
    if locked_guarded == BASELINE and all(
        row["selected_policy"] == BASELINE for row in guarded["selections"]
    ):
        report["verdict"]["reason"] = (
            "No one-feature challenger cleared the predeclared train/LOMO, concentration, drawdown, "
            "and Holm-adjusted evidence guard; guarded walk-forward therefore stayed with baseline."
        )
    else:
        report["verdict"]["reason"] = (
            "A challenger cleared historical guards, but artifact/model overlap and missing point-in-time "
            "broker evidence still forbid production activation."
        )
    report["verdict"]["aggressive_walkforward_delta_pp"] = aggressive["delta_return_pp"]
    report["verdict"]["guarded_walkforward_delta_pp"] = guarded["delta_return_pp"]
    report["verdict"]["best_hindsight_policy"] = best_hindsight
    report["verdict"]["best_hindsight_delta_pp"] = (
        summaries[best_hindsight]["return_pct"] - summaries[BASELINE]["return_pct"]
    )
    report["verdict"]["shadow_candidate_full_delta_pp"] = (
        summaries[shadow_candidate]["return_pct"] - summaries[BASELINE]["return_pct"]
    )
    report["verdict"]["shadow_candidate_validation_delta_pp"] = validation_eval[
        "alternatives"
    ][shadow_candidate]["delta_return_pp"]
    report["verdict"]["shadow_candidate_may_delta_pp"] = weak_month_scan[
        "2026-05"
    ]["best_month_delta_pp"]
    return report


def fmt(value: float) -> str:
    return f"{value:+.2f}%"


def summary_cell(row: dict[str, Any]) -> str:
    return (
        f"{row['trades']}, {row['wins']}/{row['losses']}, "
        f"{fmt(row['return_pct'])}, MDD {fmt(row['max_drawdown_pct'])}"
    )


def render_markdown(report: dict[str, Any]) -> str:
    control = report["exact_frozen_rs5_control"]["summary"]
    base = report["common_reconstruction_baseline"]
    locked = report["locked_train_selection"]
    guarded = report["walkforward"]["guarded"]
    aggressive = report["walkforward"]["aggressive"]
    coverage = report["data"]["coverage"]
    lines = [
        "# Flat-month selector walk-forward — 2026-07-18",
        "",
        "## Verdict",
        "",
        "**Production: reject.** " + report["verdict"]["reason"],
        "",
        "The only acceptable next action is a pre-registered forward shadow challenger. "
        "Do not retune on November, December or May again.",
        "",
        "## Two controls that must not be mixed",
        "",
        f"- Exact published frozen RS5 ledger: **{summary_cell(control)}**.",
        f"- Common full-pool reconstruction: **{summary_cell(base)}** on {coverage['common_decision_days']} fully observed decision days.",
        "",
        f"The frozen ledger is the authoritative 54-trade table.  The {coverage['common_decision_days']}-day reconstruction is "
        "used only for paired selector counterfactuals because the archive does not preserve "
        "point-in-time features and broker evidence for every rejected candidate.",
        "",
        "## Coverage and causality",
        "",
        f"- {coverage['watchlist_days']} watchlists, {coverage['candidate_rows']} candidate rows; "
        f"{coverage['candidate_rows_with_5m']} rows have an archived exact 5-minute outcome.",
        f"- Common selector calendar: {coverage['common_decision_days']} days; alternatives never add a day the baseline skipped.",
        "- All daily features use completed sessions strictly before the decision date.",
        "- The current analyzer/reranker files overlap this history, so the chronological split is not clean end-to-end OOS.",
        "",
        "## Primary split",
        "",
        "| Window | Baseline | Best raw alternative | Raw delta | Holm p |",
        "|---|---:|---|---:|---:|",
    ]
    for key in ("train", "validation", "test"):
        window = report["split"][key]
        best = max(
            window["alternatives"].items(),
            key=lambda item: item[1]["delta_return_pp"],
        )
        lines.append(
            f"| {key} ({window['months']}) | {summary_cell(window['baseline'])} | "
            f"`{best[0]}` | {best[1]['delta_return_pp']:+.2f} pp | {best[1]['holm_adjusted_p']:.3f} |"
        )
    lines += [
        "",
        f"Train-only aggressive choice: `{locked['aggressive_best_train']}`. "
        f"Guarded choice: `{locked['guarded_choice']}`.",
        "",
        "The family contains 30 simple tests (15 features x top-3/top-6).  P-values are "
        "paired sign-flip tests on log-return differences and are Holm-Bonferroni adjusted "
        "within each window.",
        "",
        "## Expanding chronological walk-forward",
        "",
        "| Rule | Eval baseline | Walk-forward | Delta |",
        "|---|---:|---:|---:|",
        f"| Aggressive: pick highest prior return | {fmt(aggressive['baseline']['return_pct'])} | {fmt(aggressive['walkforward']['return_pct'])} | {aggressive['delta_return_pp']:+.2f} pp |",
        f"| Guarded: LOMO + MDD + concentration + Holm<=0.10 | {fmt(guarded['baseline']['return_pct'])} | {fmt(guarded['walkforward']['return_pct'])} | {guarded['delta_return_pp']:+.2f} pp |",
        "",
        "### Month-by-month policy chosen using prior months only",
        "",
        "| Test month | Aggressive policy | Guarded policy |",
        "|---|---|---|",
    ]
    guarded_by_month = {row["test_month"]: row for row in guarded["selections"]}
    for row in aggressive["selections"]:
        lines.append(
            f"| {row['test_month']} | `{row['selected_policy']}` | "
            f"`{guarded_by_month[row['test_month']]['selected_policy']}` |"
        )

    lines += [
        "",
        "## Weak months: opportunity versus predictability",
        "",
        "| Month | Baseline common sample | Top-3 hindsight oracle | Top-6 hindsight oracle |",
        "|---|---:|---:|---:|",
    ]
    oracle3 = report["hindsight_oracle_not_a_strategy"]["top3"]["monthly"]
    oracle6 = report["hindsight_oracle_not_a_strategy"]["top6"]["monthly"]
    for month in ("2025-11", "2025-12", "2026-05"):
        b = base["monthly"].get(month, {"return_pct": 0.0})
        lines.append(
            f"| {month} | {fmt(b['return_pct'])} | {fmt(oracle3[month]['return_pct'])} | {fmt(oracle6[month]['return_pct'])} |"
        )
    lines += [
        "",
        "The oracle proves that better same-day candidates often existed in the six-name pool. "
        "It does **not** prove that the archived features could identify them before entry.",
        "",
        "### Best hindsight rule for each weak month",
        "",
        "| Month | Hindsight rule | Month delta | Full-archive delta | Verdict |",
        "|---|---|---:|---:|---|",
    ]
    for month, row in report["weak_month_scan"].items():
        if row["best_policy"] is None:
            lines.append(
                f"| {month} | none | {row['best_month_delta_pp']:+.2f} pp | n/a | candidate-pool/edge issue, not solved by tested selector |"
            )
        else:
            lines.append(
                f"| {month} | `{row['best_policy']}` | {row['best_month_delta_pp']:+.2f} pp | "
                f"{row['best_policy_full_delta_pp']:+.2f} pp | hindsight-only; reject activation |"
            )
    lines += [
        "",
        "## Focal policies and single-trade dependence",
        "",
        "| Policy | Full result | Full delta | Divergences | Largest positive share | LODO delta range |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, row in report["focal_policies"].items():
        dep = row["dependence_full"]
        share = dep["largest_positive_share_pct"]
        leave = dep["leave_one_divergence_out_delta_pp_range"]
        share_text = f"{share:.1f}%" if share is not None else "n/a"
        lines.append(
            f"| `{name}` | {summary_cell(row['all'])} | {dep['delta_return_pp']:+.2f} pp | "
            f"{dep['divergences']} | {share_text} | {leave[0]:+.2f}..{leave[1]:+.2f} pp |"
        )
    if not report["focal_policies"]:
        lines.append("| No challenger | — | — | — | — | — |")

    lines += [
        "",
        "## What may enter shadow",
        "",
        "Only `max_vol_expansion_top6` may be registered as a **data-collection-only** challenger "
        "before the next session.  It changed reconstructed May from -2.24% to +0.72% (+2.96 pp), "
        "but lost 6.84 pp over the full common archive and 4.89 pp on validation.  It must not "
        "select or replace a live order. "
        "Use the current Engine-A+RS5 choice as control, log all six candidates with broker "
        "availability/spread/max-lots at decision time, and never replace the live order.",
        "",
        "Minimum review gate: 40 new baseline-eligible sessions, at least 10 genuine divergences, "
        "positive paired delta in both chronological halves, no worse drawdown, Holm-adjusted "
        "p<=0.10 against the single pre-registered family, and no one divergence above 35% of "
        "positive delta.  Anything selected after seeing these archived weak months is rejected.",
        "",
        "## Reproduce offline",
        "",
        "```bash",
        "python3 -m argonus.research.flat_month_selector_walkforward --offline",
        "```",
        "",
        f"Cache SHA256: `{report['data']['cache_sha256']}`.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    if args.offline and args.rebuild_cache:
        raise RuntimeError("--offline and --rebuild-cache are mutually exclusive.")
    if args.offline:
        if not CACHE.exists():
            raise RuntimeError(f"Offline cache is missing: {CACHE}")
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
        validate_cache(cache)
    elif args.rebuild_cache or not CACHE.exists():
        cache = build_cache()
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
        validate_cache(cache)

    report = build_report(cache)
    JSON_REPORT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    MD_REPORT.write_text(render_markdown(report), encoding="utf-8")
    if args.print_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
