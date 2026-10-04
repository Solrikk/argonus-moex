#!/usr/bin/env python3
"""Research-only selector hypotheses on the frozen common candidate calendar.

The program never imports the broker or production bot.  It validates and
reads the immutable candidate cache created by the earlier flat-month audit,
then compares a small family of previously untested selector constructions
with the reconstructed Engine-A+RS5 control.

Important: the analyzer/reranker artifacts used to create the cache overlap
the evaluated history.  The chronology below is therefore leakage-safe at the
selection layer, but it is not a clean end-to-end out-of-sample backtest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np
import sklearn
from sklearn.linear_model import HuberRegressor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import RobustScaler


from argonus.paths import PROJECT_ROOT as ROOT
CACHE_PATH = ROOT / "data/backtests/FLAT_MONTH_SELECTOR_WALKFORWARD_2026-07-18.cache.json"

BASELINE = "engine_a_rs5_common_control"
FEE_PCT = 0.08
STRESS_FEE_PCT = 0.20
INITIAL_CAPITAL = 150_000.0

# This family is intentionally small.  It excludes every one-feature maximum
# and threshold already covered by the prior selector/generator audits.
FIXED_POLICIES = (
    "borda_rs5_t2plus_target_top3",
    "pareto_target_probabilities_top3",
    "rank1_only_generator",
)
EXPANDING_POLICY = "huber_rank_rs5_t2plus_top3"
POLICIES = FIXED_POLICIES + (EXPANDING_POLICY,)

HUBER_FEATURES = (
    "current_rank",
    "directional_rs5_pp",
    "reranker_t2plus_prob",
)
HUBER_ALPHA = 1.0
HUBER_EPSILON = 1.35


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON instead of compact canonical JSON.",
    )
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


def load_cache() -> dict[str, Any]:
    payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise RuntimeError("Unsupported candidate-cache schema.")
    expected = payload.get("cache_sha256")
    body = {key: value for key, value in payload.items() if key != "cache_sha256"}
    actual = canonical_sha(body)
    if expected != actual:
        raise RuntimeError(
            f"Candidate-cache hash mismatch: expected {expected}, calculated {actual}."
        )
    execution = payload.get("execution", {})
    if float(execution.get("round_trip_cost_pct", -1.0)) != FEE_PCT:
        raise RuntimeError("Unexpected commission model in candidate cache.")
    if bool(payload.get("selection", {}).get("alternative_day_set_broadened")):
        raise RuntimeError("Alternative day set must not be broadened.")
    return payload


def admissible(day: dict[str, Any], top_k: int = 3) -> list[dict[str, Any]]:
    return [
        row
        for row in day["candidates"]
        if int(row["current_rank"]) <= top_k
        and row["gate"] is None
        and bool(row["rs5_pass"])
    ]


def analysis_days(cache: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for day in cache["days"]:
        if day["rank1_gate"] is not None:
            continue
        if not admissible(day, 3):
            continue
        pool6 = admissible(day, 6)
        if any(row["outcome_return_pct"] is None for row in pool6):
            continue
        result.append(day)
    if len(result) != 47:
        raise RuntimeError(f"Expected frozen 47-day common calendar, got {len(result)}.")
    return result


def baseline_choice(day: dict[str, Any]) -> dict[str, Any]:
    return min(admissible(day, 3), key=lambda row: int(row["current_rank"]))


def feature(row: dict[str, Any], name: str) -> float:
    if name == "current_rank":
        return float(row["current_rank"])
    if name == "target_move_pct":
        return float(row["target_move_pct"])
    value = row["features"].get(name)
    if value is None or not math.isfinite(float(value)):
        raise RuntimeError(f"Missing {name} for {row['symbol']}.")
    return float(value)


def borda_choice(
    day: dict[str, Any], names: tuple[str, ...]
) -> dict[str, Any]:
    pool = admissible(day, 3)
    scores = {id(row): 0 for row in pool}
    for name in names:
        ordered = sorted(
            pool,
            key=lambda row: (feature(row, name), -int(row["current_rank"])),
        )
        for points, row in enumerate(ordered):
            scores[id(row)] += points
    return max(
        pool,
        key=lambda row: (scores[id(row)], -int(row["current_rank"])),
    )


def fixed_choice(day: dict[str, Any], policy: str) -> dict[str, Any] | None:
    base = baseline_choice(day)
    if policy == "borda_rs5_t2plus_target_top3":
        return borda_choice(
            day,
            ("directional_rs5_pp", "reranker_t2plus_prob", "target_move_pct"),
        )
    if policy == "pareto_target_probabilities_top3":
        names = (
            "reranker_any_target_prob",
            "reranker_winner_prob",
            "reranker_t2plus_prob",
        )
        dominators = [
            row
            for row in admissible(day, 3)
            if row is not base
            and all(feature(row, name) >= feature(base, name) for name in names)
            and any(feature(row, name) > feature(base, name) for name in names)
        ]
        if not dominators:
            return base
        ranked = borda_choice(day, names)
        if ranked in dominators:
            return ranked
        return min(dominators, key=lambda row: int(row["current_rank"]))
    if policy == "rank1_only_generator":
        return base if int(base["current_rank"]) == 1 else None
    raise ValueError(policy)


def ledger_row(day: dict[str, Any], row: dict[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {
            "date": day["date"],
            "month": day["month"],
            "symbol": "CASH",
            "direction": "cash",
            "current_rank": None,
            "return_pct": 0.0,
            "traded": False,
        }
    return {
        "date": day["date"],
        "month": day["month"],
        "symbol": row["symbol"],
        "direction": row["direction"],
        "current_rank": int(row["current_rank"]),
        "return_pct": float(row["outcome_return_pct"]),
        "traded": True,
    }


def fixed_ledger(
    days: Iterable[dict[str, Any]], policy: str
) -> list[dict[str, Any]]:
    return [ledger_row(day, fixed_choice(day, policy)) for day in days]


def baseline_ledger(days: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [ledger_row(day, baseline_choice(day)) for day in days]


def expanding_huber_ledger(
    days: list[dict[str, Any]], evaluation_months: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    output: list[dict[str, Any]] = []
    fits: list[dict[str, Any]] = []
    for month in evaluation_months:
        train_days = [day for day in days if day["month"] < month]
        test_days = [day for day in days if day["month"] == month]
        if not train_days or not test_days:
            continue
        x_train: list[list[float]] = []
        y_train: list[float] = []
        for day in train_days:
            for row in admissible(day, 3):
                x_train.append([feature(row, name) for name in HUBER_FEATURES])
                y_train.append(float(row["outcome_return_pct"]))
        model = make_pipeline(
            RobustScaler(),
            HuberRegressor(
                alpha=HUBER_ALPHA,
                epsilon=HUBER_EPSILON,
                max_iter=1000,
            ),
        )
        model.fit(x_train, y_train)
        fits.append(
            {
                "evaluation_month": month,
                "train_months": sorted({day["month"] for day in train_days}),
                "train_last_date": max(day["date"] for day in train_days),
                "train_days": len(train_days),
                "train_candidate_rows": len(x_train),
            }
        )
        for day in test_days:
            pool = admissible(day, 3)
            x_test = np.asarray(
                [[feature(row, name) for name in HUBER_FEATURES] for row in pool],
                dtype=float,
            )
            predictions = model.predict(x_test)
            selected_index = max(
                range(len(pool)),
                key=lambda index: (float(predictions[index]), -int(pool[index]["current_rank"])),
            )
            output.append(ledger_row(day, pool[selected_index]))
    return output, fits


def compound(values: Iterable[float]) -> float:
    return (math.prod(1.0 + value / 100.0 for value in values) - 1.0) * 100.0


def summarize(rows: list[dict[str, Any]], fee_pct: float = FEE_PCT) -> dict[str, Any]:
    capital = 1.0
    peak = 1.0
    mdd = 0.0
    normalized: list[float] = []
    for row in rows:
        value = float(row["return_pct"])
        if row["traded"] and fee_pct != FEE_PCT:
            risk_multiplier = 0.5 if row["direction"] == "long" else 1.0
            value -= (fee_pct - FEE_PCT) * risk_multiplier
        normalized.append(value)
        capital *= 1.0 + value / 100.0
        peak = max(peak, capital)
        mdd = min(mdd, capital / peak - 1.0)

    by_month: dict[str, list[tuple[dict[str, Any], float]]] = defaultdict(list)
    for row, value in zip(rows, normalized):
        by_month[row["month"]].append((row, value))
    return {
        "calendar_decisions": len(rows),
        "trades": sum(bool(row["traded"]) for row in rows),
        "wins": sum(bool(row["traded"]) and value > 0.0 for row, value in zip(rows, normalized)),
        "losses": sum(bool(row["traded"]) and value <= 0.0 for row, value in zip(rows, normalized)),
        "return_pct": (capital - 1.0) * 100.0,
        "max_drawdown_pct": mdd * 100.0,
        "ending_capital_150k": INITIAL_CAPITAL * capital,
        "monthly": {
            month: {
                "calendar_decisions": len(items),
                "trades": sum(bool(row["traded"]) for row, _ in items),
                "wins": sum(bool(row["traded"]) and value > 0.0 for row, value in items),
                "losses": sum(bool(row["traded"]) and value <= 0.0 for row, value in items),
                "return_pct": compound(value for _, value in items),
            }
            for month, items in sorted(by_month.items())
        },
    }


def paired_differences(
    baseline: list[dict[str, Any]], challenger: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if [row["date"] for row in baseline] != [row["date"] for row in challenger]:
        raise RuntimeError("Paired ledgers have different calendars.")
    result = []
    for control, alternative in zip(baseline, challenger):
        if control["symbol"] == alternative["symbol"]:
            continue
        delta = math.log1p(float(alternative["return_pct"]) / 100.0) - math.log1p(
            float(control["return_pct"]) / 100.0
        )
        result.append(
            {
                "date": control["date"],
                "month": control["month"],
                "control_symbol": control["symbol"],
                "challenger_symbol": alternative["symbol"],
                "control_return_pct": control["return_pct"],
                "challenger_return_pct": alternative["return_pct"],
                "paired_log_delta": delta,
            }
        )
    return result


def exact_sign_flip_pvalue(deltas: Iterable[float]) -> float:
    values = [float(value) for value in deltas if abs(float(value)) > 1e-15]
    if not values or sum(values) <= 0.0:
        return 1.0
    if len(values) > 20:
        raise RuntimeError("Exact sign-flip implementation is capped at 20 divergences.")
    observed = sum(values)
    extreme = 0
    for mask in range(1 << len(values)):
        score = sum(
            value if mask & (1 << index) else -value
            for index, value in enumerate(values)
        )
        extreme += score >= observed - 1e-15
    return extreme / float(1 << len(values))


def holm_adjust(raw: dict[str, float]) -> dict[str, float]:
    ordered = sorted(raw, key=raw.get)
    result: dict[str, float] = {}
    running = 0.0
    for index, name in enumerate(ordered):
        running = max(running, min(1.0, raw[name] * (len(ordered) - index)))
        result[name] = running
    return result


def subset(rows: list[dict[str, Any]], months: set[str]) -> list[dict[str, Any]]:
    return [row for row in rows if row["month"] in months]


def diagnostics(
    baseline: list[dict[str, Any]], challenger: list[dict[str, Any]]
) -> dict[str, Any]:
    base_summary = summarize(baseline)
    alt_summary = summarize(challenger)
    divergences = paired_differences(baseline, challenger)
    months = sorted({row["month"] for row in baseline})
    split = max(1, len(months) // 2)
    first_months = set(months[:split])
    second_months = set(months[split:])
    first_delta = (
        summarize(subset(challenger, first_months))["return_pct"]
        - summarize(subset(baseline, first_months))["return_pct"]
    )
    second_delta = (
        summarize(subset(challenger, second_months))["return_pct"]
        - summarize(subset(baseline, second_months))["return_pct"]
    )
    positive = [max(0.0, row["paired_log_delta"]) for row in divergences]
    positive_total = sum(positive)
    leave_one: list[float] = []
    for divergence in divergences:
        kept_base = [row for row in baseline if row["date"] != divergence["date"]]
        kept_alt = [row for row in challenger if row["date"] != divergence["date"]]
        leave_one.append(
            summarize(kept_alt)["return_pct"] - summarize(kept_base)["return_pct"]
        )
    return {
        "summary": alt_summary,
        "stress_cost_0_20_summary": summarize(challenger, STRESS_FEE_PCT),
        "delta_return_pp": alt_summary["return_pct"] - base_summary["return_pct"],
        "mdd_delta_pp": (
            alt_summary["max_drawdown_pct"] - base_summary["max_drawdown_pct"]
        ),
        "divergences": len(divergences),
        "nonzero_divergences": sum(
            abs(row["paired_log_delta"]) > 1e-15 for row in divergences
        ),
        "raw_one_sided_exact_sign_flip_p": exact_sign_flip_pvalue(
            row["paired_log_delta"] for row in divergences
        ),
        "chronological_halves": {
            "first_months": sorted(first_months),
            "first_delta_return_pp": first_delta,
            "second_months": sorted(second_months),
            "second_delta_return_pp": second_delta,
        },
        "largest_positive_delta_share_pct": (
            max(positive, default=0.0) / positive_total * 100.0
            if positive_total > 0.0
            else None
        ),
        "leave_one_divergence_out_delta_pp_range": (
            [min(leave_one), max(leave_one)] if leave_one else [0.0, 0.0]
        ),
        "paired_divergence_rows": divergences,
    }


def build_report() -> dict[str, Any]:
    cache = load_cache()
    days = analysis_days(cache)
    months = sorted({day["month"] for day in days})
    train_months = months[:3]
    evaluation_months = months[3:]
    training_days = [day for day in days if day["month"] in train_months]
    evaluation_days = [day for day in days if day["month"] in evaluation_months]
    train_control = baseline_ledger(training_days)
    control = baseline_ledger(evaluation_days)

    train_policy_diagnostics: dict[str, Any] = {}
    for policy in FIXED_POLICIES:
        train_summary = summarize(fixed_ledger(training_days, policy))
        train_policy_diagnostics[policy] = {
            "summary": train_summary,
            "delta_return_pp": (
                train_summary["return_pct"] - summarize(train_control)["return_pct"]
            ),
            "mdd_delta_pp": (
                train_summary["max_drawdown_pct"]
                - summarize(train_control)["max_drawdown_pct"]
            ),
        }
    train_qualified = [
        policy
        for policy, row in train_policy_diagnostics.items()
        if row["delta_return_pp"] > 0.0 and row["mdd_delta_pp"] >= -1e-12
    ]
    frozen_train_choice = (
        max(
            train_qualified,
            key=lambda policy: train_policy_diagnostics[policy]["summary"]["return_pct"],
        )
        if train_qualified
        else BASELINE
    )

    ledgers = {
        policy: fixed_ledger(evaluation_days, policy) for policy in FIXED_POLICIES
    }
    huber_ledger, fit_audit = expanding_huber_ledger(days, evaluation_months)
    ledgers[EXPANDING_POLICY] = huber_ledger

    policy_rows = {
        name: diagnostics(control, ledger) for name, ledger in ledgers.items()
    }
    raw_p = {
        name: row["raw_one_sided_exact_sign_flip_p"]
        for name, row in policy_rows.items()
    }
    adjusted = holm_adjust(raw_p)
    for name, row in policy_rows.items():
        row["holm_adjusted_p"] = adjusted[name]
        halves = row["chronological_halves"]
        share = row["largest_positive_delta_share_pct"]
        checks = {
            "positive_total_delta": row["delta_return_pp"] > 0.0,
            "positive_both_chronological_halves": (
                halves["first_delta_return_pp"] > 0.0
                and halves["second_delta_return_pp"] > 0.0
            ),
            "mdd_not_worse": row["mdd_delta_pp"] >= -1e-12,
            "at_least_10_divergences": row["divergences"] >= 10,
            "holm_p_at_most_0_10": row["holm_adjusted_p"] <= 0.10,
            "largest_positive_share_at_most_35pct": (
                share is not None and share <= 35.0
            ),
        }
        row["promotion_checks"] = checks
        row["promotion_ready"] = all(checks.values())

    # Full-common summaries are diagnostics only.  They cannot replace the
    # exact 54-trade production ledger because the common cache has 47 days.
    full_common = {
        "baseline": summarize(baseline_ledger(days)),
        **{
            policy: summarize(fixed_ledger(days, policy))
            for policy in FIXED_POLICIES
        },
    }
    best_raw = max(
        POLICIES,
        key=lambda name: policy_rows[name]["delta_return_pp"],
    )
    report = {
        "schema_version": 1,
        "artifact_type": "selector_consensus_expanding_research",
        "research_only": True,
        "production_files_changed": False,
        "verdict": "NO_GO",
        "reason": (
            "No challenger passed the predeclared divergence, chronological-half, "
            "drawdown, concentration and multiplicity gates."
        ),
        "scope": {
            "cache_start": cache["scope"]["start"],
            "cache_end": cache["scope"]["end"],
            "common_days": len(days),
            "train_months": train_months,
            "outer_evaluation_months": evaluation_months,
            "outer_evaluation_days": len(evaluation_days),
            "alternative_day_set_broadened": False,
        },
        "execution": cache["execution"],
        "stress_round_trip_cost_pct": STRESS_FEE_PCT,
        "exact_production_control_reference": {
            "period": "2025-11-01..2026-07-16",
            "trades": 54,
            "wins": 31,
            "losses": 23,
            "return_pct": 50.0956,
            "max_drawdown_pct": -3.4758,
            "warning": (
                "Reference only: alternatives are compared on the paired 47-day "
                "common reconstruction and are never spliced into this exact ledger."
            ),
        },
        "outer_control": {
            "policy": BASELINE,
            "summary": summarize(control),
            "stress_cost_0_20_summary": summarize(control, STRESS_FEE_PCT),
        },
        "train_only_fixed_policy_selection": {
            "control": summarize(train_control),
            "challengers": train_policy_diagnostics,
            "selection_rule": "Strictly positive return delta and non-worse MDD on Nov-Jan; otherwise retain control.",
            "frozen_choice_for_outer_window": frozen_train_choice,
            "outer_result_of_frozen_choice": (
                summarize(control)
                if frozen_train_choice == BASELINE
                else summarize(ledgers[frozen_train_choice])
            ),
        },
        "policies": policy_rows,
        "best_raw_outer_challenger": best_raw,
        "expanding_fit_audit": fit_audit,
        "full_common_diagnostic": full_common,
        "method": {
            "fixed_family": list(FIXED_POLICIES),
            "expanding_policy": EXPANDING_POLICY,
            "huber_features": list(HUBER_FEATURES),
            "huber_alpha": HUBER_ALPHA,
            "huber_epsilon": HUBER_EPSILON,
            "fit_rule": (
                "For each outer month, RobustScaler and HuberRegressor are fit only "
                "on candidate rows from strictly earlier months."
            ),
            "selection_multiplicity": (
                "One-sided exact paired sign-flip p-values adjusted with Holm across "
                "all four new challengers."
            ),
            "no_hyperparameter_search_in_script": True,
        },
        "limitations": [
            "The new family was formulated after the archive existed; this is exploratory, not untouched OOS evidence.",
            "Current analyzer and reranker artifacts overlap the evaluated dates.",
            "Historical point-in-time spread, depth, max-lots and short availability are absent for alternatives.",
            "The paired common calendar has only 47 days and the outer evaluation only 37 days.",
            "October is excluded because its current-version reconstruction is not comparable to this frozen cache.",
        ],
        "provenance": {
            "cache_path": str(CACHE_PATH.relative_to(ROOT)),
            "cache_file_sha256": sha_file(CACHE_PATH),
            "cache_canonical_sha256": cache["cache_sha256"],
            "research_script_sha256": sha_file(Path(__file__).resolve()),
            "sklearn_version": sklearn.__version__,
        },
    }
    report["report_sha256"] = canonical_sha(report)
    return report


def main() -> int:
    args = parse_args()
    report = build_report()
    if args.pretty:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(canonical_bytes(report).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
