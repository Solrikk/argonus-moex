#!/usr/bin/env python3
"""Independent audit of the study1 RS5-fade skip-day engine.

Research only: this program reads immutable local artifacts, reproduces the
candidate ledger, combines it with the frozen 54-trade Engine-A+RS5 ledger,
and writes JSON/Markdown reports.  It has no broker imports and cannot change
production configuration or place orders.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from argonus.research import research_fixed_150k_sizing as fixed


from argonus.paths import PROJECT_ROOT as ROOT
FEATURES = ROOT / "data/intraday_universe/features.json"
MANIFEST = ROOT / "data/intraday_universe/manifest.json"
STUDY = ROOT / "argonus/research/universe/study1.py"
JSON_REPORT = ROOT / "data/backtests/FLAT_MONTH_SECOND_ENGINE_AUDIT_2026-07-18.json"
MD_REPORT = ROOT / "data/backtests/FLAT_MONTH_SECOND_ENGINE_AUDIT_2026-07-18.md"
FIT_END = "2026-03"
DEFAULT_COST = 0.08


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compound(values: Iterable[float]) -> float:
    return (math.prod(1.0 + value / 100.0 for value in values) - 1.0) * 100.0


def summarize(rows: list[dict[str, Any]], field: str = "return_pct") -> dict[str, Any]:
    equity = 1.0
    peak = 1.0
    mdd = 0.0
    monthly: dict[str, list[float]] = defaultdict(list)
    for row in sorted(rows, key=lambda value: (value["date"], value.get("engine", ""))):
        value = float(row[field])
        equity *= 1.0 + value / 100.0
        peak = max(peak, equity)
        mdd = min(mdd, equity / peak - 1.0)
        monthly[row["month"]].append(value)
    return {
        "trades": len(rows),
        "wins": sum(float(row[field]) > 0.0 for row in rows),
        "losses": sum(float(row[field]) <= 0.0 for row in rows),
        "return_pct": (equity - 1.0) * 100.0,
        "max_drawdown_trade_close_pct": mdd * 100.0,
        "monthly": {
            month: {
                "trades": len(values),
                "wins": sum(value > 0.0 for value in values),
                "losses": sum(value <= 0.0 for value in values),
                "return_pct": compound(values),
            }
            for month, values in sorted(monthly.items())
        },
    }


def universe_ledger(
    feature_rows: list[dict[str, Any]], *, target: int, cost: float
) -> list[dict[str, Any]]:
    per_day: dict[str, list[tuple[float, dict[str, Any], str, float]]] = defaultdict(list)
    for row in feature_rows:
        rs5 = row.get("rs5")
        if rs5 is None:
            continue
        direction = "short" if float(rs5) > 0.0 else "long"
        gross = row.get(f"ret_{direction}_t{target}")
        if gross is None:
            continue
        per_day[str(row["date"])].append((abs(float(rs5)), row, direction, float(gross)))
    result = []
    for trade_date, candidates in sorted(per_day.items()):
        score, row, direction, gross = max(candidates, key=lambda item: item[0])
        result.append(
            {
                "date": trade_date,
                "month": trade_date[:7],
                "engine": "B_rs5_fade",
                "symbol": row["sym"],
                "direction": direction,
                "score_abs_rs5_pp": score,
                "gross_return_pct": gross,
                "cost_pct": cost,
                "return_pct": gross - cost,
            }
        )
    return result


def engine_a_ledger(cost: float) -> list[dict[str, Any]]:
    _, ledger54, _ = fixed.load_ledgers()
    return [
        {
            "date": row.date,
            "month": row.month,
            "engine": "A_rs5",
            "symbol": row.symbol,
            "direction": row.direction,
            "gross_return_pct": row.gross_return_pct,
            "cost_pct": cost,
            "return_pct": (row.gross_return_pct - cost)
            * (0.5 if row.direction == "long" else 1.0),
            "full_notional_return_pct": row.gross_return_pct - cost,
        }
        for row in ledger54
    ]


def split(rows: list[dict[str, Any]], exam: bool) -> list[dict[str, Any]]:
    return [row for row in rows if (row["month"] > FIT_END) == exam]


def simulate_fixed(rows: list[dict[str, Any]]) -> dict[str, Any]:
    equity = 50_000.0
    peak = equity
    mdd = 0.0
    min_equity = equity
    for row in sorted(rows, key=lambda value: (value["date"], value["engine"])):
        position = min(150_000.0, 3.0 * equity)
        field = "full_notional_return_pct" if row["engine"] == "A_rs5" else "return_pct"
        equity += position * float(row[field]) / 100.0
        peak = max(peak, equity)
        min_equity = min(min_equity, equity)
        mdd = min(mdd, equity / peak - 1.0)
    return {
        "trades": len(rows),
        "start_equity_rub": 50_000.0,
        "final_equity_rub": equity,
        "return_pct": (equity / 50_000.0 - 1.0) * 100.0,
        "max_drawdown_trade_close_pct": mdd * 100.0,
        "minimum_equity_rub": min_equity,
        "contract": "position=min(150000,3*equity); no lot rounding, spread, slippage or margin-limit history",
    }


def ticker_concentration(rows: list[dict[str, Any]], turnover: list[str]) -> dict[str, Any]:
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_symbol[row["symbol"]].append(row)
    ranking = {symbol: index + 1 for index, symbol in enumerate(turnover)}
    details = []
    for symbol, items in by_symbol.items():
        logs = [math.log1p(float(row["return_pct"]) / 100.0) for row in items]
        details.append(
            {
                "symbol": symbol,
                "turnover_proxy_rank_current_snapshot": ranking.get(symbol),
                "trades": len(items),
                "wins": sum(float(row["return_pct"]) > 0.0 for row in items),
                "return_pct": compound(float(row["return_pct"]) for row in items),
                "log_growth_contribution": sum(logs),
            }
        )
    details.sort(key=lambda row: row["log_growth_contribution"], reverse=True)
    positive = sum(max(0.0, row["log_growth_contribution"]) for row in details)
    bottom20 = [
        row for row in rows if ranking.get(row["symbol"], 10_000) >= 81
    ]
    named = {symbol: by_symbol.get(symbol, []) for symbol in ("VJGZ", "LNZL", "SVETP", "IVAT")}
    return {
        "unique_symbols": len(by_symbol),
        "top_positive_symbols": details[:12],
        "top1_positive_log_share_pct": (
            max(0.0, details[0]["log_growth_contribution"]) / positive * 100.0
            if positive else None
        ),
        "top5_positive_log_share_pct": (
            sum(max(0.0, row["log_growth_contribution"]) for row in details[:5])
            / positive
            * 100.0
            if positive else None
        ),
        "current_turnover_proxy_bottom20": summarize(bottom20),
        "named_illiquidity_flags": {
            symbol: {
                "turnover_proxy_rank_current_snapshot": ranking.get(symbol),
                **summarize(items),
            }
            for symbol, items in named.items()
        },
    }


def cost_stress(
    feature_rows: list[dict[str, Any]], a_dates: set[str], target: int
) -> dict[str, Any]:
    result = {}
    for cost in (0.08, 0.12, 0.20, 0.30, 0.50):
        all_b = universe_ledger(feature_rows, target=target, cost=cost)
        skip = [row for row in all_b if row["date"] not in a_dates]
        a = engine_a_ledger(cost)
        result[f"{cost:.2f}"] = {
            "skip_engine": summarize(skip),
            "combined_table_convention": summarize(a + skip),
            "combined_fixed_150k": simulate_fixed(a + skip),
        }
    return result


def find_compound_breakeven(gross: list[float]) -> float:
    low, high = 0.0, 2.0
    for _ in range(80):
        middle = (low + high) / 2.0
        if compound(value - middle for value in gross) >= 0.0:
            low = middle
        else:
            high = middle
    return low


def build_report() -> dict[str, Any]:
    feature_rows = json.loads(FEATURES.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    a = engine_a_ledger(DEFAULT_COST)
    a_dates = {row["date"] for row in a}
    b30_all = universe_ledger(feature_rows, target=30, cost=DEFAULT_COST)
    b20_all = universe_ledger(feature_rows, target=20, cost=DEFAULT_COST)
    b30_skip = [row for row in b30_all if row["date"] not in a_dates]
    b20_skip = [row for row in b20_all if row["date"] not in a_dates]
    combined30 = sorted(a + b30_skip, key=lambda row: row["date"])
    combined20 = sorted(a + b20_skip, key=lambda row: row["date"])

    if len(b30_all) != 176 or len(b30_skip) != 122 or len(a) != 54:
        raise RuntimeError(
            f"Ledger identity mismatch: allB={len(b30_all)}, skipB={len(b30_skip)}, A={len(a)}"
        )

    concentration = ticker_concentration(b30_skip, manifest["top_by_turnover"])
    cost = cost_stress(feature_rows, a_dates, 30)
    target = {
        "t30_skip": summarize(b30_skip),
        "t20_skip": summarize(b20_skip),
        "delta_return_pp": summarize(b30_skip)["return_pct"]
        - summarize(b20_skip)["return_pct"],
    }
    gross = [float(row["gross_return_pct"]) for row in b30_skip]

    report = {
        "schema_version": 1,
        "artifact_type": "flat_month_second_engine_audit",
        "verdict": {
            "production": "NO_GO",
            "shadow": "ALLOW_SIGNAL_AND_EXECUTABILITY_LOGGING_ONLY",
            "production_files_changed": False,
            "reason": (
                "The selection-level exam is positive, but the skip-day edge is target- and cost-fragile, "
                "uses a July-survivor/current-liquidity universe, and lacks point-in-time executability/spread evidence."
            ),
        },
        "provenance": {
            "features_sha256": sha_file(FEATURES),
            "manifest_sha256": sha_file(MANIFEST),
            "study1_sha256": sha_file(STUDY),
            "engine_a_pinned_by": "argonus.research.research_fixed_150k_sizing.load_ledgers()",
        },
        "config_selection": {
            "tested_configs": 36,
            "selected_on_fit": "rs5_fade|gate0|t30",
            "fit_months": "2025-11..2026-03",
            "exam_months": "2026-04..2026-07-16",
            "entry": "07:30 candle open",
            "stop": "1% stop-first",
            "target": "3% from entry",
            "time_exit": "18:35",
            "default_cost_pct": DEFAULT_COST,
        },
        "study1_all_days_reproduction": {
            "fit": summarize(split(b30_all, False)),
            "exam": summarize(split(b30_all, True)),
        },
        "skip_day_engine": {
            "all": summarize(b30_skip),
            "fit": summarize(split(b30_skip, False)),
            "exam": summarize(split(b30_skip, True)),
            "direction_counts": dict(Counter(row["direction"] for row in b30_skip)),
            "gross_outcome_counts": {
                "target_3pct": sum(abs(row["gross_return_pct"] - 3.0) < 1e-12 for row in b30_skip),
                "stop_minus_1pct": sum(abs(row["gross_return_pct"] + 1.0) < 1e-12 for row in b30_skip),
                "time_exit": sum(row["gross_return_pct"] not in {3.0, -1.0} for row in b30_skip),
            },
            "compound_cost_breakeven_pct": find_compound_breakeven(gross),
        },
        "engine_a_control": {
            "all": summarize(a),
            "fit": summarize(split(a, False)),
            "exam": summarize(split(a, True)),
        },
        "combined": {
            "table_convention_t30": summarize(combined30),
            "table_convention_t20": summarize(combined20),
            "fit_t30": summarize(split(combined30, False)),
            "exam_t30": summarize(split(combined30, True)),
            "fixed_150k_t30": simulate_fixed(combined30),
            "fixed_150k_t20": simulate_fixed(combined20),
        },
        "target_sensitivity": target,
        "cost_stress": cost,
        "concentration": concentration,
        "leave_one_month_out_skip_return_pct": {
            month: summarize([row for row in b30_skip if row["month"] != month])["return_pct"]
            for month in sorted({row["month"] for row in b30_skip})
        },
        "limitations": [
            "The universe was selected using the current share list and a turnover proxy calculated from rows ending in July 2026; November membership is survivorship contaminated.",
            "The turnover proxy is lots*close and omits lot size; it is not historical RUB turnover.",
            "No point-in-time short availability, max lots, bid/ask spread, order-book depth, borrow cost, fills or delisting/status history is archived.",
            "Flat 0.08% costs are implausibly optimistic for the least liquid selected names.",
            "The 3% target was selected among 36 configurations on fit; t20 instability shows parameter dependence.",
            "Trade-close MDD excludes intratrade adverse excursion, gaps and simultaneous margin stress.",
        ],
    }
    return report


def fmt(value: float) -> str:
    return f"{value:+.2f}%"


def cell(row: dict[str, Any]) -> str:
    return (
        f"{row['trades']}, {row['wins']}/{row['losses']}, {fmt(row['return_pct'])}, "
        f"MDD {fmt(row['max_drawdown_trade_close_pct'])}"
    )


def render(report: dict[str, Any]) -> str:
    all_days = report["study1_all_days_reproduction"]
    skip = report["skip_day_engine"]
    combined = report["combined"]
    concentration = report["concentration"]
    lines = [
        "# Flat-month second-engine audit — 2026-07-18",
        "",
        "## Verdict",
        "",
        "**Production: NO-GO.** The arithmetic reproduces, including the positive held-out "
        "calendar slice, but the result is not executable evidence.  Permit signal and broker-"
        "preflight logging in shadow only; the signal must never place or replace an order.",
        "",
        "## Independent reproduction",
        "",
        "| Ledger | Fit Nov-Mar | Exam Apr-Jul 16 | Full |",
        "|---|---:|---:|---:|",
        f"| study1 B on all days | {cell(all_days['fit'])} | {cell(all_days['exam'])} | — |",
        f"| B only when frozen A skips | {cell(skip['fit'])} | {cell(skip['exam'])} | {cell(skip['all'])} |",
        f"| Frozen Engine A | {cell(report['engine_a_control']['fit'])} | {cell(report['engine_a_control']['exam'])} | {cell(report['engine_a_control']['all'])} |",
        f"| Combined A + skip-day B | {cell(combined['fit_t30'])} | {cell(combined['exam_t30'])} | {cell(combined['table_convention_t30'])} |",
        "",
        "The original study1 monthly MDD is measured only after each month.  Values above are "
        "recalculated after every trade and are therefore the relevant, though still incomplete, control.",
        "",
        "The configuration was the best of 36 variants on Nov-Mar.  Apr-Jul is a legitimate "
        "later calendar slice for the rule choice, but not clean end-to-end OOS because the "
        "current universe was selected with data through July.",
        "",
        "## Combined monthly table (historical table convention)",
        "",
        "| Month | Trades | W/L | Return |",
        "|---|---:|---:|---:|",
    ]
    for month, row in combined["table_convention_t30"]["monthly"].items():
        lines.append(
            f"| {month} | {row['trades']} | {row['wins']}/{row['losses']} | {fmt(row['return_pct'])} |"
        )
    lines += [
        "",
        "The extra engine lifts reconstructed November and May, but it does not solve every flat "
        "month: December changes from Engine A's -0.17% to **-0.99% combined**.  Its own skip-day "
        "branch also loses in March (-0.65%) and June (-2.20%).",
        "",
        "## Fixed 150k on 50k research sizing",
        "",
        "| Exit | Final equity | Return | Trade-close MDD |",
        "|---|---:|---:|---:|",
        f"| t30 | {combined['fixed_150k_t30']['final_equity_rub']:,.0f} RUB | {fmt(combined['fixed_150k_t30']['return_pct'])} | {fmt(combined['fixed_150k_t30']['max_drawdown_trade_close_pct'])} |",
        f"| t20 | {combined['fixed_150k_t20']['final_equity_rub']:,.0f} RUB | {fmt(combined['fixed_150k_t20']['return_pct'])} | {fmt(combined['fixed_150k_t20']['max_drawdown_trade_close_pct'])} |",
        "",
        "This sizing omits lot rounding, historical margin limits, spread, slippage and gaps; it is a stress arithmetic result, not a capital forecast.",
        "",
        f"At 0.50% round-trip cost the combined fixed-sizing result falls to "
        f"{report['cost_stress']['0.50']['combined_fixed_150k']['final_equity_rub']:,.0f} RUB "
        f"with {fmt(report['cost_stress']['0.50']['combined_fixed_150k']['max_drawdown_trade_close_pct'])} MDD.",
        "",
        "## Target and cost fragility",
        "",
        "| Variant | Skip-day B result | Combined result |",
        "|---|---:|---:|",
        f"| t30, cost 0.08% | {cell(report['target_sensitivity']['t30_skip'])} | {cell(combined['table_convention_t30'])} |",
        f"| t20, cost 0.08% | {cell(report['target_sensitivity']['t20_skip'])} | {cell(combined['table_convention_t20'])} |",
    ]
    for cost in ("0.12", "0.20", "0.30", "0.50"):
        row = report["cost_stress"][cost]
        lines.append(
            f"| t30, cost {cost}% | {cell(row['skip_engine'])} | {cell(row['combined_table_convention'])} |"
        )
    lines += [
        "",
        f"The skip ledger's compounded cost break-even is only "
        f"{report['skip_day_engine']['compound_cost_breakeven_pct']:.3f}% per round trip.  "
        "Moving t30 to t20 cuts its return by "
        f"{report['target_sensitivity']['delta_return_pp']:.2f} percentage points.",
        "",
        "## Concentration and executability",
        "",
        f"- {concentration['unique_symbols']} symbols appear in 122 skip-day trades; top five "
        f"positive symbol contributions supply {concentration['top5_positive_log_share_pct']:.1f}% of all positive log contribution.",
        f"- VJGZ alone is the largest positive symbol contributor: 14 trades, "
        f"{fmt(concentration['named_illiquidity_flags']['VJGZ']['return_pct'])}, current proxy rank 77/100.",
        f"- Current-turnover-proxy bottom-20 names: {cell(concentration['current_turnover_proxy_bottom20'])}.",
        "- Flagged names:",
        "",
        "| Symbol | Current proxy rank | Trades | Return |",
        "|---|---:|---:|---:|",
    ]
    for symbol, row in concentration["named_illiquidity_flags"].items():
        lines.append(
            f"| {symbol} | {row['turnover_proxy_rank_current_snapshot']} | {row['trades']} | {fmt(row['return_pct'])} |"
        )
    lines += [
        "",
        "The universe itself is not point-in-time: it was built from the current share list and "
        "ranked with data through July.  Its `lots*close` proxy omits lot size.  Thus historical "
        "membership, liquidity and short executability are all survivorship-contaminated.",
        "",
        "## Shadow gate",
        "",
        "If retained, pre-register exactly `rs5_fade|gate0|t30`; do not retune t20/t30 or costs "
        "on this archive.  For every Engine-A skip, record all candidates and the proposed winner, "
        "point-in-time trade/short availability, max lots, lot size, bid/ask, spread, depth and a "
        "conservative counterfactual fill.  Require at least 60 new skip days, 30 executable "
        "decisions, positive net result at both actual estimated costs and a 0.50% stress, positive "
        "results in both chronological halves, no worse MDD, and no symbol above 20% of positive "
        "contribution before any reconsideration.",
        "",
        "## Reproduce",
        "",
        "```bash",
        "python3 -m argonus.research.universe.study1 --exam 'rs5_fade|gate0|t30'",
        "python3 -m argonus.research.flat_month_second_engine_audit",
        "```",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    report = build_report()
    JSON_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    MD_REPORT.write_text(render(report), encoding="utf-8")
    print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
