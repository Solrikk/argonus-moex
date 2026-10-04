#!/usr/bin/env python3
from __future__ import annotations

from argonus.paths import WATCHLIST_DIR

import argparse
import json
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

from argonus.watchlists import watchlist_best_target as wbt
from argonus.backtesting.backtest_generated_watchlists import (
    WatchlistEntry,
    build_candle_cache,
    candle_hits_target,
    discover_watchlists,
    fetch_trade_day_candle,
    highest_hit_target_label,
    patch_watchlist_data_access,
)


@dataclass(slots=True)
class CandidateRealization:
    rank: int
    symbol: str
    direction: str
    regime: str
    anchor_kind: str | None
    post_impulse_state: str
    best_target_label: str
    best_target_price: float
    reached_target: str | None
    reached_target_index: int
    any_target_hit: bool
    chosen_target_hit: bool
    overall_score: float
    setup_score: float
    market_score: float
    instrument_score: float
    history_score: float
    ladder_score: float
    entry_quality_score: float
    relative_strength_score: float
    freshness_score: float
    volume_quality_score: float
    prob_hit_t1: float
    prob_hit_t3: float
    expected_max_target: float
    risk_complete_miss: float
    reranker_any_target_prob: float | None
    standalone_selection_score: float
    candle_open: float
    candle_low: float
    candle_high: float
    candle_close: float


@dataclass(slots=True)
class T1Record:
    trade_date: str
    month: str
    path: str
    t1_type: str
    top: CandidateRealization
    actual_best: CandidateRealization | None
    num_candidates: int
    num_t2plus_candidates: int
    top_vs_actual_score_gap: float | None
    top_vs_actual_setup_gap: float | None
    top_vs_actual_instrument_gap: float | None
    top_vs_actual_history_gap: float | None
    top_vs_actual_entry_gap: float | None
    top_vs_actual_freshness_gap: float | None
    top_vs_actual_rs_gap: float | None
    top_vs_actual_prob_t1_gap: float | None
    top_vs_actual_prob_t3_gap: float | None
    top_vs_actual_expmax_gap: float | None
    top_vs_actual_missrisk_gap: float | None
    tags: list[str]
    candidates: list[CandidateRealization]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Подробная диагностика T1-days для generated_watchlists_*: "
            "сравнивает текущий top pick с лучшим кандидатом дня, который реально дошел до T2+."
        )
    )
    parser.add_argument("--root", default=str(WATCHLIST_DIR), help="Папка с generated_watchlists_*. По умолчанию data/watchlists.")
    parser.add_argument("--workers", type=int, default=4, help="Количество потоков для загрузки истории.")
    parser.add_argument("--json", action="store_true", help="Печатать полный JSON.")
    parser.add_argument("--out", help="Сохранить полный JSON-отчет в файл.")
    return parser.parse_args()


def get_target_price(idea: wbt.WatchIdea, label: str) -> float | None:
    if not label.startswith("T"):
        return None
    try:
        target_index = int(label[1:])
    except ValueError:
        return None
    for target in idea.targets:
        if target.index == target_index:
            return target.price
    return None


def build_candidate_realization(
    idea: wbt.WatchIdea,
    item: wbt.IdeaAnalysis,
    rank: int,
    candle: Any,
) -> CandidateRealization:
    all_targets = sorted((target.index, target.price) for target in idea.targets)
    reached_label = highest_hit_target_label(item.direction, candle, all_targets)
    reached_index = int(reached_label[1:]) if reached_label else 0

    return CandidateRealization(
        rank=rank,
        symbol=item.symbol,
        direction=item.direction,
        regime=item.regime,
        anchor_kind=item.anchor_kind,
        post_impulse_state=item.post_impulse_state,
        best_target_label=item.best_target.label,
        best_target_price=item.best_target.price,
        reached_target=reached_label,
        reached_target_index=reached_index,
        any_target_hit=reached_index > 0,
        chosen_target_hit=candle_hits_target(item.direction, candle, item.best_target.price),
        overall_score=item.overall_score,
        setup_score=item.setup_score,
        market_score=item.market_score,
        instrument_score=item.instrument_score,
        history_score=item.history_score,
        ladder_score=item.ladder_score,
        entry_quality_score=item.entry_quality_score,
        relative_strength_score=item.relative_strength_score,
        freshness_score=item.freshness_score,
        volume_quality_score=item.volume_quality_score,
        prob_hit_t1=item.prob_hit_t1,
        prob_hit_t3=item.prob_hit_t3,
        expected_max_target=item.expected_max_target,
        risk_complete_miss=item.risk_complete_miss,
        reranker_any_target_prob=item.reranker_any_target_prob,
        standalone_selection_score=item.standalone_selection_score,
        candle_open=candle.open,
        candle_low=candle.low,
        candle_high=candle.high,
        candle_close=candle.close,
    )


def choose_actual_best(candidates: list[CandidateRealization]) -> CandidateRealization | None:
    t2plus_candidates = [item for item in candidates if item.reached_target_index >= 2]
    if not t2plus_candidates:
        return None
    return max(
        t2plus_candidates,
        key=lambda item: (
            item.reached_target_index,
            item.overall_score,
        ),
    )


def build_tags(top: CandidateRealization, actual: CandidateRealization | None) -> list[str]:
    tags: list[str] = []
    if actual is None:
        tags.append("no_t2plus_anywhere")
        return tags

    if actual.rank <= 2:
        tags.append("actual_in_top2")
    elif actual.rank <= 3:
        tags.append("actual_in_top3")
    else:
        tags.append("actual_below_top3")

    score_gap = top.overall_score - actual.overall_score
    if score_gap <= 1.0:
        tags.append("near_tie")
    elif score_gap <= 5.0:
        tags.append("moderate_score_gap")
    else:
        tags.append("wide_score_gap")

    if top.regime != actual.regime:
        tags.append(f"regime:{top.regime}->{actual.regime}")
    if top.anchor_kind != actual.anchor_kind:
        tags.append(f"anchor:{top.anchor_kind}->{actual.anchor_kind}")
    if top.direction != actual.direction:
        tags.append(f"direction:{top.direction}->{actual.direction}")
    if actual.expected_max_target - top.expected_max_target >= 0.20:
        tags.append("actual_higher_expmax")
    if top.risk_complete_miss - actual.risk_complete_miss >= 0.05:
        tags.append("actual_lower_missrisk")
    if actual.prob_hit_t1 - top.prob_hit_t1 >= 0.05:
        tags.append("actual_higher_prob_t1")
    if actual.freshness_score - top.freshness_score >= 8.0:
        tags.append("actual_much_fresher")
    if actual.relative_strength_score - top.relative_strength_score >= 8.0:
        tags.append("actual_stronger_rs")
    return tags


def analyze_entry(entry: WatchlistEntry, stock_cache: dict[str, list[Any]]) -> T1Record | None:
    ideas_by_symbol = {idea.symbol: idea for idea in wbt.parse_watchlist(entry.raw_text)}
    analyses = wbt.analyze_watchlist(entry.raw_text, entry.trade_date)
    if not analyses:
        return None

    candidates: list[CandidateRealization] = []
    for rank, item in enumerate(analyses, start=1):
        if item.skipped_reason:
            continue
        try:
            candle = fetch_trade_day_candle(stock_cache, item.symbol, entry.trade_date)
        except RuntimeError:
            if rank == 1:
                return None
            continue
        idea = ideas_by_symbol[item.symbol]
        candidates.append(build_candidate_realization(idea, item, rank, candle))

    if not candidates:
        return None

    top = candidates[0]
    if top.reached_target_index != 1:
        return None

    actual_best = choose_actual_best(candidates[1:]) if len(candidates) > 1 else None
    t1_type = "avoidable" if actual_best is not None else "unavoidable"
    tags = build_tags(top, actual_best)

    return T1Record(
        trade_date=entry.trade_date.isoformat(),
        month=entry.trade_date.strftime("%Y-%m"),
        path=entry.path,
        t1_type=t1_type,
        top=top,
        actual_best=actual_best,
        num_candidates=len(candidates),
        num_t2plus_candidates=sum(1 for item in candidates if item.reached_target_index >= 2),
        top_vs_actual_score_gap=(top.overall_score - actual_best.overall_score) if actual_best else None,
        top_vs_actual_setup_gap=(top.setup_score - actual_best.setup_score) if actual_best else None,
        top_vs_actual_instrument_gap=(top.instrument_score - actual_best.instrument_score) if actual_best else None,
        top_vs_actual_history_gap=(top.history_score - actual_best.history_score) if actual_best else None,
        top_vs_actual_entry_gap=(top.entry_quality_score - actual_best.entry_quality_score) if actual_best else None,
        top_vs_actual_freshness_gap=(top.freshness_score - actual_best.freshness_score) if actual_best else None,
        top_vs_actual_rs_gap=(top.relative_strength_score - actual_best.relative_strength_score) if actual_best else None,
        top_vs_actual_prob_t1_gap=(top.prob_hit_t1 - actual_best.prob_hit_t1) if actual_best else None,
        top_vs_actual_prob_t3_gap=(top.prob_hit_t3 - actual_best.prob_hit_t3) if actual_best else None,
        top_vs_actual_expmax_gap=(top.expected_max_target - actual_best.expected_max_target) if actual_best else None,
        top_vs_actual_missrisk_gap=(top.risk_complete_miss - actual_best.risk_complete_miss) if actual_best else None,
        tags=tags,
        candidates=candidates,
    )


def average(values: list[float | None]) -> float | None:
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)


def counter_to_sorted_pairs(counter: Counter[Any]) -> list[list[Any]]:
    return [[list(key) if isinstance(key, tuple) else key, count] for key, count in counter.most_common()]


def build_summary(records: list[T1Record]) -> dict[str, Any]:
    avoidable = [record for record in records if record.t1_type == "avoidable"]
    unavoidable = [record for record in records if record.t1_type == "unavoidable"]

    months = Counter(record.month for record in records)
    t1_type_counts = Counter(record.t1_type for record in records)
    top_regimes = Counter(record.top.regime for record in records)
    top_anchor_kinds = Counter(record.top.anchor_kind or "none" for record in records)
    top_directions = Counter(record.top.direction for record in records)
    top_targets = Counter(record.top.best_target_label for record in records)
    top_tickers = Counter(record.top.symbol for record in records)
    tags = Counter(tag for record in records for tag in record.tags)
    actual_ranks = Counter(record.actual_best.rank for record in avoidable if record.actual_best is not None)
    actual_regimes = Counter(record.actual_best.regime for record in avoidable if record.actual_best is not None)
    actual_anchor_kinds = Counter(record.actual_best.anchor_kind or "none" for record in avoidable if record.actual_best is not None)
    actual_tickers = Counter(record.actual_best.symbol for record in avoidable if record.actual_best is not None)
    regime_pairs = Counter((record.top.regime, record.actual_best.regime) for record in avoidable if record.actual_best is not None)
    anchor_pairs = Counter(
        ((record.top.anchor_kind or "none"), (record.actual_best.anchor_kind or "none"))
        for record in avoidable
        if record.actual_best is not None
    )
    direction_pairs = Counter((record.top.direction, record.actual_best.direction) for record in avoidable if record.actual_best is not None)

    feature_gaps = {
        "overall_score_gap_top_minus_actual": average([record.top_vs_actual_score_gap for record in avoidable]),
        "setup_gap_top_minus_actual": average([record.top_vs_actual_setup_gap for record in avoidable]),
        "instrument_gap_top_minus_actual": average([record.top_vs_actual_instrument_gap for record in avoidable]),
        "history_gap_top_minus_actual": average([record.top_vs_actual_history_gap for record in avoidable]),
        "entry_gap_top_minus_actual": average([record.top_vs_actual_entry_gap for record in avoidable]),
        "freshness_gap_top_minus_actual": average([record.top_vs_actual_freshness_gap for record in avoidable]),
        "rs_gap_top_minus_actual": average([record.top_vs_actual_rs_gap for record in avoidable]),
        "prob_t1_gap_top_minus_actual": average([record.top_vs_actual_prob_t1_gap for record in avoidable]),
        "prob_t3_gap_top_minus_actual": average([record.top_vs_actual_prob_t3_gap for record in avoidable]),
        "expmax_gap_top_minus_actual": average([record.top_vs_actual_expmax_gap for record in avoidable]),
        "missrisk_gap_top_minus_actual": average([record.top_vs_actual_missrisk_gap for record in avoidable]),
    }

    return {
        "t1_days": len(records),
        "avoidable_t1_days": len(avoidable),
        "unavoidable_t1_days": len(unavoidable),
        "months": counter_to_sorted_pairs(months),
        "t1_types": counter_to_sorted_pairs(t1_type_counts),
        "top_regimes": counter_to_sorted_pairs(top_regimes),
        "top_anchor_kinds": counter_to_sorted_pairs(top_anchor_kinds),
        "top_directions": counter_to_sorted_pairs(top_directions),
        "top_targets": counter_to_sorted_pairs(top_targets),
        "top_tickers": counter_to_sorted_pairs(top_tickers),
        "actual_ranks_on_avoidable": counter_to_sorted_pairs(actual_ranks),
        "actual_regimes_on_avoidable": counter_to_sorted_pairs(actual_regimes),
        "actual_anchor_kinds_on_avoidable": counter_to_sorted_pairs(actual_anchor_kinds),
        "actual_tickers_on_avoidable": counter_to_sorted_pairs(actual_tickers),
        "regime_pairs_on_avoidable": counter_to_sorted_pairs(regime_pairs),
        "anchor_pairs_on_avoidable": counter_to_sorted_pairs(anchor_pairs),
        "direction_pairs_on_avoidable": counter_to_sorted_pairs(direction_pairs),
        "tags": counter_to_sorted_pairs(tags),
        "feature_gaps": feature_gaps,
    }


def format_percent(part: int, whole: int) -> str:
    if whole <= 0:
        return "0.0%"
    return f"{part / whole * 100.0:.1f}%"


def format_text_report(records: list[T1Record], summary: dict[str, Any]) -> str:
    lines: list[str] = []
    t1_days = summary["t1_days"]
    avoidable = summary["avoidable_t1_days"]
    unavoidable = summary["unavoidable_t1_days"]

    lines.append("T1 Day Analysis")
    lines.append(
        f"Всего T1 days: {t1_days} | avoidable: {avoidable} ({format_percent(avoidable, t1_days)}) | "
        f"unavoidable: {unavoidable} ({format_percent(unavoidable, t1_days)})"
    )
    lines.append("")
    lines.append("Ключевые паттерны")

    for label, values in [
        ("top_regimes", summary["top_regimes"][:6]),
        ("actual_regimes_on_avoidable", summary["actual_regimes_on_avoidable"][:6]),
        ("regime_pairs_on_avoidable", summary["regime_pairs_on_avoidable"][:8]),
        ("anchor_pairs_on_avoidable", summary["anchor_pairs_on_avoidable"][:8]),
        ("tags", summary["tags"][:10]),
    ]:
        lines.append(f"{label}: {values}")

    lines.append("")
    lines.append("Средние гэпы top минус actual на avoidable T1")
    for key, value in summary["feature_gaps"].items():
        if value is not None:
            lines.append(f"{key}: {value:.3f}")

    lines.append("")
    lines.append("Разбор по T1 days")
    for record in records:
        if record.actual_best is None:
            lines.append(
                (
                    f"{record.trade_date}: {record.top.symbol} {record.top.direction} "
                    f"{record.top.regime}/{record.top.anchor_kind} best={record.top.best_target_label} "
                    f"score={record.top.overall_score:.1f} | unavoidable | no T2+ candidate"
                )
            )
            continue

        lines.append(
            (
                f"{record.trade_date}: top={record.top.symbol} r1 {record.top.direction} "
                f"{record.top.regime}/{record.top.anchor_kind} hit={record.top.reached_target} "
                f"score={record.top.overall_score:.1f} -> actual={record.actual_best.symbol} r{record.actual_best.rank} "
                f"{record.actual_best.direction} {record.actual_best.regime}/{record.actual_best.anchor_kind} "
                f"hit={record.actual_best.reached_target} score={record.actual_best.overall_score:.1f} | "
                f"gap={record.top_vs_actual_score_gap:.2f} | tags={', '.join(record.tags)}"
            )
        )

    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    watchlists = discover_watchlists(args.root)
    stock_cache, index_candles = build_candle_cache(args.workers, watchlists)
    patch_watchlist_data_access(stock_cache, index_candles)

    records: list[T1Record] = []
    for entry in watchlists:
        record = analyze_entry(entry, stock_cache)
        if record is not None:
            records.append(record)

    records = sorted(records, key=lambda item: (item.trade_date, item.path))
    summary = build_summary(records)
    payload = {
        "summary": summary,
        "records": [asdict(item) for item in records],
    }

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_text_report(records, summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
