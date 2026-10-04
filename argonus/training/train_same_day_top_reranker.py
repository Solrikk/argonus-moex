#!/usr/bin/env python3
from __future__ import annotations

from argonus.paths import WATCHLIST_DIR, MODEL_DIR

import argparse
import json
import math
import os
from dataclasses import dataclass
from typing import Any

import numpy as np
from sklearn.linear_model import LogisticRegression

from argonus.watchlists import watchlist_best_target as wbt
from argonus.backtesting.backtest_generated_watchlists import (
    build_candle_cache,
    candle_hits_target,
    discover_watchlists,
    fetch_trade_day_candle,
    patch_watchlist_data_access,
)


DEFAULT_BASE_NUMERIC_FEATURES = (
    "overall_score",
    "setup_score",
    "market_score",
    "instrument_score",
    "history_score",
    "ladder_score",
    "entry_quality_score",
    "relative_strength_score",
    "volume_quality_score",
    "freshness_score",
    "regime_confidence",
    "continuation_prob",
    "exhaustion_prob",
    "retrace_ratio",
    "compression_ratio",
    "breakout_proximity",
    "prob_hit_t1",
    "prob_hit_t3",
    "expected_max_target",
    "risk_complete_miss",
)
DEFAULT_NUMERIC_FEATURES = DEFAULT_BASE_NUMERIC_FEATURES

DEFAULT_C_GRID = (0.35, 0.7, 1.4, 2.8, 5.6)
DEFAULT_TOPK_GRID = (3, 4, 5, 6)
DEFAULT_MAX_OVERALL_GAP_GRID = (8.0, 12.0, 16.0, 20.0, 28.0, 36.0)
DEFAULT_MIN_PROB_GAP_GRID = (0.005, 0.01, 0.015, 0.02, 0.03)
DEFAULT_TOP_PROB_CEILING_GRID = (0.52, 0.56, 0.60, 0.64, 0.68, 0.72)
DEFAULT_MIN_ALT_PROB_GRID = (0.48, 0.50, 0.52, 0.54, 0.56)
DEFAULT_OUTPUT = str(MODEL_DIR / "same_day_top_reranker_model.json")


@dataclass(slots=True)
class CandidateSample:
    day_index: int
    month: str
    rank: int
    any_target_hit: int
    winner_hit: int
    t2plus_hit: int
    reached_target_index: int
    progress_to_t1: float | None
    overall_score: float
    item: wbt.IdeaAnalysis


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Обучает same_day_top_reranker_model.json на generated_watchlists_* "
            "по факту same-day исходов кандидатов."
        )
    )
    parser.add_argument("--root", default=str(WATCHLIST_DIR), help="Папка с generated_watchlists_*. По умолчанию data/watchlists.")
    parser.add_argument("--workers", type=int, default=4, help="Потоки для загрузки истории.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Куда сохранить модель.")
    parser.add_argument(
        "--label-mode",
        choices=("winner", "any_hit", "t2plus"),
        default="winner",
        help="На чем учить reranker: на winner-of-day, any_target_hit или достижение T2+.",
    )
    parser.add_argument(
        "--selection-mode",
        choices=("cv", "in_sample"),
        default="cv",
        help="По какому результату выбирать финальную override-конфигурацию.",
    )
    parser.add_argument(
        "--feature-mode",
        choices=("base", "contextual"),
        default="base",
        help="Какой feature-space использовать: базовый или с признаками относительно top1 дня.",
    )
    parser.add_argument(
        "--summary-json",
        help="Куда сохранить summary по обучению и in-sample backtest.",
    )
    return parser.parse_args()


def make_proto_model(
    numeric_features: tuple[str, ...],
    regimes: tuple[str, ...],
    anchor_kinds: tuple[str, ...],
    directions: tuple[str, ...],
    post_impulse_states: tuple[str, ...],
    best_targets: tuple[str, ...],
) -> wbt.SameDayTopRerankerModel:
    feature_count = (
        len(numeric_features)
        + len(wbt.SAME_DAY_TOP_RERANKER_BINARY_FEATURES)
        + len(regimes)
        + len(anchor_kinds)
        + len(directions)
        + len(post_impulse_states)
        + len(best_targets)
    )
    return wbt.SameDayTopRerankerModel(
        numeric_features=numeric_features,
        binary_features=wbt.SAME_DAY_TOP_RERANKER_BINARY_FEATURES,
        regimes=regimes,
        anchor_kinds=anchor_kinds,
        directions=directions,
        post_impulse_states=post_impulse_states,
        best_targets=best_targets,
        scaler_mean=tuple(0.0 for _ in range(feature_count)),
        scaler_scale=tuple(1.0 for _ in range(feature_count)),
        coefficients=tuple(0.0 for _ in range(feature_count)),
        intercept=0.0,
        topk=6,
        max_overall_gap=36.0,
        min_prob_gap=0.01,
        top_prob_ceiling=0.68,
        min_alt_prob=0.50,
        regime_index={name: index for index, name in enumerate(regimes)},
        anchor_kind_index={name: index for index, name in enumerate(anchor_kinds)},
        direction_index={name: index for index, name in enumerate(directions)},
        post_impulse_state_index={name: index for index, name in enumerate(post_impulse_states)},
        best_target_index={name: index for index, name in enumerate(best_targets)},
    )


def get_target_price(idea: wbt.WatchIdea, label: str) -> float | None:
    if not label.startswith("T"):
        return None
    try:
        index = int(label[1:])
    except ValueError:
        return None
    for target in idea.targets:
        if target.index == index:
            return target.price
    return None


def calculate_progress_to_target(
    direction: str,
    previous_close: float,
    candle: Any,
    target_price: float | None,
) -> float | None:
    if target_price is None:
        return None

    target_move = abs(target_price - previous_close)
    if target_move <= 1e-9:
        return None

    if direction == "long":
        realized = max(candle.high - previous_close, 0.0)
    elif direction == "short":
        realized = max(previous_close - candle.low, 0.0)
    else:
        return None
    return realized / target_move


def choose_actual_day_winner(day_samples: list[CandidateSample]) -> CandidateSample | None:
    hit_samples = [sample for sample in day_samples if sample.any_target_hit]
    if not hit_samples:
        return None
    return max(
        hit_samples,
        key=lambda sample: (
            sample.reached_target_index,
            sample.progress_to_t1 if sample.progress_to_t1 is not None else -math.inf,
            sample.overall_score,
        ),
    )


def build_dataset(root: str, workers: int) -> tuple[list[CandidateSample], int]:
    watchlists = discover_watchlists(root)
    stock_cache, index_candles = build_candle_cache(workers, watchlists)
    patch_watchlist_data_access(stock_cache, index_candles)

    # Disable learned reranking during dataset generation.
    wbt._same_day_top_reranker_checked = True
    wbt._same_day_top_reranker_model = None
    wbt._same_day_top_winner_reranker_checked = True
    wbt._same_day_top_winner_reranker_model = None
    wbt._same_day_top_t2plus_reranker_checked = True
    wbt._same_day_top_t2plus_reranker_model = None

    samples: list[CandidateSample] = []
    for day_index, entry in enumerate(watchlists):
        ideas_by_symbol = {idea.symbol: idea for idea in wbt.parse_watchlist(entry.raw_text)}
        analyses = wbt.analyze_watchlist(entry.raw_text, entry.trade_date)
        day_samples: list[CandidateSample] = []
        for rank, item in enumerate(analyses, start=1):
            if item.skipped_reason:
                continue
            try:
                candle = fetch_trade_day_candle(stock_cache, item.symbol, entry.trade_date)
            except RuntimeError:
                continue
            idea = ideas_by_symbol.get(item.symbol)
            if idea is None:
                continue
            reached_target_indices = [
                target.index
                for target in idea.targets
                if candle_hits_target(item.direction, candle, target.price)
            ]
            reached_target_index = max(reached_target_indices) if reached_target_indices else 0
            any_target_hit = int(
                reached_target_index > 0
            )
            t2plus_hit = int(
                reached_target_index >= 2
            )
            progress_to_t1 = calculate_progress_to_target(
                item.direction,
                item.previous_session.close,
                candle,
                get_target_price(idea, "T1"),
            )
            day_samples.append(
                CandidateSample(
                    day_index=day_index,
                    month=entry.trade_date.strftime("%Y-%m"),
                    rank=rank,
                    any_target_hit=any_target_hit,
                    winner_hit=0,
                    t2plus_hit=t2plus_hit,
                    reached_target_index=reached_target_index,
                    progress_to_t1=progress_to_t1,
                    overall_score=item.overall_score,
                    item=item,
                )
            )
        winner = choose_actual_day_winner(day_samples)
        if winner is not None:
            winner.winner_hit = 1
        samples.extend(day_samples)
    return samples, len(watchlists)


def build_feature_space(
    samples: list[CandidateSample],
    label_mode: str,
    feature_mode: str,
) -> tuple[wbt.SameDayTopRerankerModel, list[CandidateSample], np.ndarray, np.ndarray]:
    numeric_features = tuple(
        DEFAULT_BASE_NUMERIC_FEATURES
        + (wbt.SAME_DAY_TOP_RERANKER_CONTEXT_NUMERIC_FEATURES if feature_mode == "contextual" else ())
    )
    regimes = tuple(sorted({sample.item.regime for sample in samples}))
    anchor_kinds = tuple(sorted({sample.item.anchor_kind or "none" for sample in samples}))
    directions = tuple(sorted({sample.item.direction for sample in samples}))
    post_impulse_states = tuple(sorted({sample.item.post_impulse_state for sample in samples}))
    best_targets = tuple(sorted({sample.item.best_target.label for sample in samples}))
    proto_model = make_proto_model(
        numeric_features,
        regimes,
        anchor_kinds,
        directions,
        post_impulse_states,
        best_targets,
    )

    day_top_samples: dict[int, CandidateSample] = {}
    if feature_mode == "contextual":
        for sample in samples:
            current = day_top_samples.get(sample.day_index)
            if current is None or sample.rank < current.rank:
                day_top_samples[sample.day_index] = sample

    aligned_samples: list[CandidateSample] = []
    feature_rows: list[list[float]] = []
    labels: list[int] = []
    for sample in samples:
        encoded = wbt.encode_same_day_top_reranker_features(
            sample.item,
            proto_model,
            top_item=(
                day_top_samples[sample.day_index].item
                if feature_mode == "contextual" and sample.day_index in day_top_samples
                else sample.item
            ),
            rank=sample.rank,
        )
        if encoded is None:
            continue
        aligned_samples.append(sample)
        feature_rows.append(encoded)
        if label_mode == "winner":
            labels.append(sample.winner_hit)
        elif label_mode == "t2plus":
            labels.append(sample.t2plus_hit)
        else:
            labels.append(sample.any_target_hit)

    return proto_model, aligned_samples, np.asarray(feature_rows, dtype=float), np.asarray(labels, dtype=int)


def sigmoid(values: np.ndarray) -> np.ndarray:
    positive = values >= 0.0
    result = np.empty_like(values, dtype=float)
    result[positive] = 1.0 / (1.0 + np.exp(-values[positive]))
    exp_values = np.exp(values[~positive])
    result[~positive] = exp_values / (1.0 + exp_values)
    return result


def fit_logistic_regression(
    x: np.ndarray,
    y: np.ndarray,
    c_value: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    mean = x.mean(axis=0)
    scale = x.std(axis=0)
    scale[scale <= 1e-9] = 1.0
    x_scaled = (x - mean) / scale

    model = LogisticRegression(
        C=c_value,
        solver="lbfgs",
        max_iter=5000,
        class_weight="balanced",
        random_state=0,
    )
    model.fit(x_scaled, y)
    coefficients = model.coef_[0].astype(float)
    intercept = float(model.intercept_[0])
    return mean, scale, coefficients, intercept


def predict_probabilities(
    x: np.ndarray,
    mean: np.ndarray,
    scale: np.ndarray,
    coefficients: np.ndarray,
    intercept: float,
) -> np.ndarray:
    x_scaled = (x - mean) / scale
    logits = x_scaled @ coefficients + intercept
    return sigmoid(logits)


def group_probabilities_by_day(
    samples: list[CandidateSample],
    probabilities: np.ndarray,
) -> list[list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for sample, probability in zip(samples, probabilities):
        grouped.setdefault(sample.day_index, []).append(
            {
                "rank": sample.rank,
                "month": sample.month,
                "overall_score": sample.overall_score,
                "probability": float(probability),
                "any_hit": bool(sample.any_target_hit),
                "winner_hit": bool(sample.winner_hit),
                "t2plus_hit": bool(sample.t2plus_hit),
            }
        )

    ordered_days: list[list[dict[str, Any]]] = []
    for day_index in sorted(grouped):
        day_rows = sorted(grouped[day_index], key=lambda item: item["rank"])
        ordered_days.append(day_rows)
    return ordered_days


def evaluate_override_config(
    grouped_days: list[list[dict[str, Any]]],
    *,
    label_mode: str,
    topk: int,
    max_overall_gap: float,
    min_prob_gap: float,
    top_prob_ceiling: float,
    min_alt_prob: float,
) -> dict[str, Any]:
    label_hit_days = 0
    any_hit_days = 0
    promotions = 0
    promoted_label_hit_days = 0
    label_key = "winner_hit" if label_mode == "winner" else ("t2plus_hit" if label_mode == "t2plus" else "any_hit")
    for day_rows in grouped_days:
        if not day_rows:
            continue
        top = day_rows[0]
        winner = top
        if top["probability"] <= top_prob_ceiling:
            candidates = [
                row
                for row in day_rows[1:min(len(day_rows), topk)]
                if top["overall_score"] - row["overall_score"] <= max_overall_gap
                and row["probability"] >= min_alt_prob
                and row["probability"] - top["probability"] >= min_prob_gap
            ]
            if candidates:
                winner = max(candidates, key=lambda item: (item["probability"], item["overall_score"]))
                promotions += 1
                if winner[label_key]:
                    promoted_label_hit_days += 1
        if winner["any_hit"]:
            any_hit_days += 1
        if winner[label_key]:
            label_hit_days += 1

    total_days = len(grouped_days)
    return {
        "hit_days": label_hit_days,
        "miss_days": total_days - any_hit_days,
        "any_hit_days": any_hit_days,
        "promotions": promotions,
        "promoted_hit_days": promoted_label_hit_days,
        "hit_rate": label_hit_days / total_days if total_days else 0.0,
        "topk": topk,
        "max_overall_gap": max_overall_gap,
        "min_prob_gap": min_prob_gap,
        "top_prob_ceiling": top_prob_ceiling,
        "min_alt_prob": min_alt_prob,
    }


def find_best_configuration(
    samples: list[CandidateSample],
    probabilities: np.ndarray,
    label_mode: str,
) -> dict[str, Any]:
    grouped_days = group_probabilities_by_day(samples, probabilities)

    best: dict[str, Any] | None = None
    for topk in DEFAULT_TOPK_GRID:
        for max_overall_gap in DEFAULT_MAX_OVERALL_GAP_GRID:
            for min_prob_gap in DEFAULT_MIN_PROB_GAP_GRID:
                for top_prob_ceiling in DEFAULT_TOP_PROB_CEILING_GRID:
                    for min_alt_prob in DEFAULT_MIN_ALT_PROB_GRID:
                        result = evaluate_override_config(
                            grouped_days,
                            label_mode=label_mode,
                            topk=topk,
                            max_overall_gap=max_overall_gap,
                            min_prob_gap=min_prob_gap,
                            top_prob_ceiling=top_prob_ceiling,
                            min_alt_prob=min_alt_prob,
                        )
                        if best is None:
                            best = result
                            continue
                        current_key = (
                            result["hit_days"],
                            -result["miss_days"],
                            result["any_hit_days"],
                            result["promoted_hit_days"],
                            -result["promotions"],
                            -result["max_overall_gap"],
                            -result["topk"],
                            -result["top_prob_ceiling"],
                            -result["min_alt_prob"],
                            result["min_prob_gap"],
                        )
                        best_key = (
                            best["hit_days"],
                            -best["miss_days"],
                            best["any_hit_days"],
                            best["promoted_hit_days"],
                            -best["promotions"],
                            -best["max_overall_gap"],
                            -best["topk"],
                            -best["top_prob_ceiling"],
                            -best["min_alt_prob"],
                            best["min_prob_gap"],
                        )
                        if current_key > best_key:
                            best = result

    if best is None:
        raise RuntimeError("Не удалось подобрать override config.")
    return best


def predict_out_of_fold_probabilities(
    samples: list[CandidateSample],
    x: np.ndarray,
    y: np.ndarray,
    c_value: float,
) -> tuple[np.ndarray, list[str]]:
    months = sorted({sample.month for sample in samples})
    probabilities = np.zeros(len(samples), dtype=float)

    for holdout_month in months:
        train_indices = [index for index, sample in enumerate(samples) if sample.month != holdout_month]
        test_indices = [index for index, sample in enumerate(samples) if sample.month == holdout_month]
        if not train_indices or not test_indices:
            continue

        x_train = x[train_indices]
        y_train = y[train_indices]
        mean, scale, coefficients, intercept = fit_logistic_regression(x_train, y_train, c_value)
        probabilities[test_indices] = predict_probabilities(
            x[test_indices],
            mean,
            scale,
            coefficients,
            intercept,
        )

    return probabilities, months


def build_payload(
    proto_model: wbt.SameDayTopRerankerModel,
    mean: np.ndarray,
    scale: np.ndarray,
    coefficients: np.ndarray,
    intercept: float,
    override: dict[str, Any],
    training_summary: dict[str, Any],
) -> dict[str, Any]:
    return {
        "numeric_features": list(proto_model.numeric_features),
        "binary_features": list(proto_model.binary_features),
        "regimes": list(proto_model.regimes),
        "anchor_kinds": list(proto_model.anchor_kinds),
        "directions": list(proto_model.directions),
        "post_impulse_states": list(proto_model.post_impulse_states),
        "best_targets": list(proto_model.best_targets),
        "scaler_mean": mean.tolist(),
        "scaler_scale": scale.tolist(),
        "coefficients": coefficients.tolist(),
        "intercept": intercept,
        "override": {
            "topk": int(override["topk"]),
            "max_overall_gap": float(override["max_overall_gap"]),
            "min_prob_gap": float(override["min_prob_gap"]),
            "top_prob_ceiling": float(override["top_prob_ceiling"]),
            "min_alt_prob": float(override["min_alt_prob"]),
        },
        "training_summary": training_summary,
    }


def main() -> int:
    args = parse_args()
    samples, total_days = build_dataset(args.root, args.workers)
    if not samples:
        raise RuntimeError("Не удалось собрать ни одного sample для обучения reranker.")

    proto_model, aligned_samples, x, y = build_feature_space(samples, args.label_mode, args.feature_mode)
    positive_rate = float(y.mean()) if len(y) else 0.0

    best_payload: dict[str, Any] | None = None
    best_summary: dict[str, Any] | None = None
    best_score: tuple[float, int] | None = None

    for c_value in DEFAULT_C_GRID:
        cv_probabilities, cv_months = predict_out_of_fold_probabilities(aligned_samples, x, y, c_value)
        override = find_best_configuration(aligned_samples, cv_probabilities, args.label_mode)
        mean, scale, coefficients, intercept = fit_logistic_regression(x, y, c_value)
        in_sample_probabilities = predict_probabilities(x, mean, scale, coefficients, intercept)
        in_sample_override = find_best_configuration(aligned_samples, in_sample_probabilities, args.label_mode)
        summary = {
            "label_mode": args.label_mode,
            "selection_mode": args.selection_mode,
            "feature_mode": args.feature_mode,
            "training_days": total_days,
            "training_samples": int(len(y)),
            "positive_labels": int(y.sum()),
            "positive_rate": positive_rate,
            "cv_months": cv_months,
            "c_value": c_value,
            "cv_override_result": override,
            "in_sample_override_result": in_sample_override,
        }
        selected_result = override if args.selection_mode == "cv" else in_sample_override
        score = (
            selected_result["hit_days"],
            -selected_result["miss_days"],
            selected_result["any_hit_days"],
            -selected_result["promotions"],
        )
        if best_score is None or score > best_score:
            best_score = score
            best_summary = summary
            best_payload = build_payload(
                proto_model=proto_model,
                mean=mean,
                scale=scale,
                coefficients=coefficients,
                intercept=intercept,
                override=selected_result,
                training_summary=summary,
            )

    if best_payload is None or best_summary is None:
        raise RuntimeError("Не удалось обучить reranker.")

    output_path = os.path.abspath(args.output)
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(best_payload, handle, ensure_ascii=False, indent=2)

    if args.summary_json:
        with open(args.summary_json, "w", encoding="utf-8") as handle:
            json.dump(best_summary, handle, ensure_ascii=False, indent=2)

    print(
        json.dumps(
            {
                "output": output_path,
                "training_days": total_days,
                "training_samples": len(y),
                "positive_labels": int(y.sum()),
                "positive_rate": positive_rate,
                "best": best_summary,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
