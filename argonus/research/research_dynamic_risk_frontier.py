#!/usr/bin/env python3
"""Read-only dynamic sizing/risk frontier for the frozen Argonus books.

This study deliberately does not import ``trade_bot`` and cannot contact the
broker.  It reuses the hash-checked 54/65-trade Engine-A+RS5 reconstruction,
keeps the historical 1% stop/T4/18:35 exit and 0.08% round-trip cost, and
changes only the position notional.

The new family is a causal within-calendar-month drawdown brake.  Before an
entry it may reduce exposure using only equity from trades already closed in
that month.  A grid is reported in-sample, while any parameter selection is
evaluated with an expanding monthly walk-forward.
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
from pathlib import Path
from typing import Any, Iterable, Sequence

from argonus.research import research_flat_month_exit_risk as source


from argonus.paths import PROJECT_ROOT as ROOT
START_EQUITY = 50_000.0
POSITION_CAP = 150_000.0
BASE_LEVERAGE = 3.0
BOOTSTRAP_SAMPLES = 5_000
BOOTSTRAP_SEED = 20260718
EXPECTED = {
    "ledger54_return_pct": 124.25481149246642,
    "ledger54_mdd_pct": -10.34425508866087,
    "ledger65_return_pct": 131.2984111205006,
    "ledger65_mdd_pct": -12.162575150094113,
}


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    leverage_cap: float = BASE_LEVERAGE
    monthly_drawdown_trigger_pct: float | None = None
    triggered_multiplier: float = 1.0


BASELINE = Rule("baseline_3x_cap150k")
BRAKE_GRID = tuple(
    Rule(
        f"monthly_dd{abs(trigger):g}_x{multiplier:g}",
        monthly_drawdown_trigger_pct=trigger,
        triggered_multiplier=multiplier,
    )
    for trigger in (-3.0, -4.0, -5.0, -6.0)
    for multiplier in (0.25, 0.50, 0.75)
)
LEVERAGE_FRONTIER = tuple(
    Rule(f"leverage_{leverage:g}x", leverage_cap=leverage)
    for leverage in (1.0, 1.5, 2.0, 2.25, 2.5, 2.75, 3.0)
)
ALL_SELECTABLE = (BASELINE, *BRAKE_GRID)
_NET_RETURN_CACHE: dict[tuple[str, str, str], float] = {}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def trade_net_return_pct(trade: source.Trade) -> float:
    key = (trade.date, trade.symbol, trade.session_sha256)
    if key not in _NET_RETURN_CACHE:
        _NET_RETURN_CACHE[key] = source.simulate_exit(
            trade, source.baseline_rule()
        ).net_return_pct
    return _NET_RETURN_CACHE[key]


def simulate(
    trades: Sequence[source.Trade],
    rule: Rule | None = None,
    *,
    schedule: dict[str, Rule] | None = None,
    start_equity: float = START_EQUITY,
) -> dict[str, Any]:
    if (rule is None) == (schedule is None):
        raise ValueError("Supply exactly one of rule or schedule.")
    equity = start_equity
    peak = equity
    mdd = 0.0
    current_month = ""
    month_peak = equity
    month_start: dict[str, float] = {}
    month_end: dict[str, float] = {}
    rows: list[dict[str, Any]] = []
    for trade in trades:
        if trade.month != current_month:
            current_month = trade.month
            month_peak = equity
            month_start[current_month] = equity
        active = rule if rule is not None else schedule[trade.month]  # type: ignore[index]
        pre_month_dd = (equity / month_peak - 1.0) * 100.0
        triggered = (
            active.monthly_drawdown_trigger_pct is not None
            and pre_month_dd <= active.monthly_drawdown_trigger_pct
        )
        multiplier = active.triggered_multiplier if triggered else 1.0
        base_notional = min(POSITION_CAP, active.leverage_cap * equity)
        position = base_notional * multiplier
        net_return = trade_net_return_pct(trade)
        before = equity
        equity += position * net_return / 100.0
        if equity <= 0.0:
            raise RuntimeError(f"Historical account depleted on {trade.date}.")
        peak = max(peak, equity)
        month_peak = max(month_peak, equity)
        drawdown = (equity / peak - 1.0) * 100.0
        mdd = min(mdd, drawdown)
        month_end[current_month] = equity
        rows.append(
            {
                "date": trade.date,
                "month": trade.month,
                "symbol": trade.symbol,
                "direction": trade.direction,
                "rule": active.name,
                "pre_month_drawdown_pct": pre_month_dd,
                "triggered": triggered,
                "multiplier": multiplier,
                "position_rub": position,
                "net_return_on_notional_pct": net_return,
                "equity_before": before,
                "pnl_rub": equity - before,
                "equity_after": equity,
                "drawdown_pct": drawdown,
            }
        )
    monthly = {
        month: (month_end[month] / month_start[month] - 1.0) * 100.0
        for month in sorted(month_start)
    }
    total_return = (equity / start_equity - 1.0) * 100.0
    return {
        "rule": asdict(rule) if rule is not None else "scheduled",
        "trades": len(trades),
        "final_equity_rub": equity,
        "return_pct": total_return,
        "geometric_mean_per_trade_pct": (
            (equity / start_equity) ** (1.0 / len(trades)) - 1.0
        )
        * 100.0,
        "max_trade_close_drawdown_pct": mdd,
        "worst_month_pct": min(monthly.values(), default=0.0),
        "monthly": monthly,
        "triggered_trades": sum(row["triggered"] for row in rows),
        "minimum_position_rub": min((row["position_rub"] for row in rows), default=0.0),
        "rows": rows,
    }


def compact(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if key != "rows"}


def train_score(result: dict[str, Any]) -> float:
    return float(result["return_pct"]) + 0.5 * float(
        result["max_trade_close_drawdown_pct"]
    )


def select_rule(train: Sequence[source.Trade]) -> Rule:
    control = simulate(train, BASELINE)
    eligible: list[tuple[float, float, str, Rule]] = []
    for candidate in ALL_SELECTABLE:
        result = simulate(train, candidate)
        if result["max_trade_close_drawdown_pct"] + 1e-12 < control[
            "max_trade_close_drawdown_pct"
        ]:
            continue
        eligible.append(
            (train_score(result), result["return_pct"], candidate.name, candidate)
        )
    return max(eligible)[-1]


def expanding_walk_forward(trades: Sequence[source.Trade]) -> dict[str, Any]:
    months = sorted({trade.month for trade in trades})
    test_months = months[3:]
    schedule: dict[str, Rule] = {}
    selections = []
    for month in test_months:
        train = [trade for trade in trades if trade.month < month]
        selected = select_rule(train)
        schedule[month] = selected
        selections.append(
            {
                "test_month": month,
                "training_trades": len(train),
                "selected_rule": selected.name,
            }
        )
    test = [trade for trade in trades if trade.month in set(test_months)]
    selected_result = simulate(test, schedule=schedule)
    baseline_result = simulate(test, BASELINE)
    return {
        "initial_training_months": months[:3],
        "test_months": test_months,
        "test_trades": len(test),
        "selected_rule_frequency": dict(
            sorted(Counter(row["selected_rule"] for row in selections).items())
        ),
        "strategy": compact(selected_result),
        "baseline": compact(baseline_result),
        "delta_return_pp": selected_result["return_pct"]
        - baseline_result["return_pct"],
        "selections": selections,
    }


def frozen_early_selection(trades: Sequence[source.Trade]) -> dict[str, Any]:
    months = sorted({trade.month for trade in trades})
    train = [trade for trade in trades if trade.month in set(months[:3])]
    test = [trade for trade in trades if trade.month in set(months[3:])]
    selected = select_rule(train)
    variant = simulate(test, selected)
    baseline = simulate(test, BASELINE)
    return {
        "training_months": months[:3],
        "training_trades": len(train),
        "selected_rule": selected.name,
        "test_months": months[3:],
        "test_trades": len(test),
        "strategy": compact(variant),
        "baseline": compact(baseline),
        "delta_return_pp": variant["return_pct"] - baseline["return_pct"],
    }


def bootstrap_delta(
    trades: Sequence[source.Trade], challenger: Rule
) -> dict[str, Any]:
    by_month: dict[str, list[source.Trade]] = defaultdict(list)
    for trade in trades:
        by_month[trade.month].append(trade)
    months = sorted(by_month)
    rng = random.Random(BOOTSTRAP_SEED)
    deltas = []
    for _ in range(BOOTSTRAP_SAMPLES):
        # Each sampled block is simulated from identical fresh capital.  This
        # preserves the within-month causal brake and avoids pretending that a
        # duplicated calendar label is one continuous month.
        variant_factor = 1.0
        baseline_factor = 1.0
        for month in (rng.choice(months) for _ in months):
            variant_factor *= 1.0 + simulate(
                by_month[month], challenger
            )["return_pct"] / 100.0
            baseline_factor *= 1.0 + simulate(
                by_month[month], BASELINE
            )["return_pct"] / 100.0
        deltas.append((variant_factor - baseline_factor) * 100.0)
    ordered = sorted(deltas)

    def percentile(p: float) -> float:
        position = (len(ordered) - 1) * p
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return ordered[lower]
        weight = position - lower
        return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

    return {
        "samples": BOOTSTRAP_SAMPLES,
        "seed": BOOTSTRAP_SEED,
        "probability_delta_positive": sum(value > 0.0 for value in deltas)
        / len(deltas),
        "median_delta_pp": statistics.median(deltas),
        "delta_ci95_pp": [percentile(0.025), percentile(0.975)],
    }


def stress_table() -> list[dict[str, Any]]:
    rows = []
    for rule in (BASELINE, Rule("leverage_2.75x", leverage_cap=2.75)):
        opening_notional = min(POSITION_CAP, rule.leverage_cap * START_EQUITY)
        rows.append(
            {
                "rule": rule.name,
                "opening_notional_rub": opening_notional,
                "opening_leverage": opening_notional / START_EQUITY,
                "instant_price_loss_that_exhausts_equity_pct": START_EQUITY
                / opening_notional
                * 100.0,
                "single_gap_account_loss_pct": {
                    str(shock): opening_notional * shock / 100.0 / START_EQUITY * 100.0
                    for shock in (5, 10, 20)
                },
            }
        )
    return rows


def build_report() -> dict[str, Any]:
    trades54, trades65, provenance = source.load_books()
    baseline54 = simulate(trades54, BASELINE)
    baseline65 = simulate(trades65, BASELINE)
    actual = {
        "ledger54_return_pct": baseline54["return_pct"],
        "ledger54_mdd_pct": baseline54["max_trade_close_drawdown_pct"],
        "ledger65_return_pct": baseline65["return_pct"],
        "ledger65_mdd_pct": baseline65["max_trade_close_drawdown_pct"],
    }
    for key, expected in EXPECTED.items():
        if not math.isclose(actual[key], expected, rel_tol=0.0, abs_tol=1e-9):
            raise RuntimeError(f"Frozen control drift: {key}={actual[key]} != {expected}")

    grid = []
    for rule in BRAKE_GRID:
        result54 = simulate(trades54, rule)
        result65 = simulate(trades65, rule)
        grid.append(
            {
                "rule": asdict(rule),
                "ledger54": compact(result54),
                "ledger65": compact(result65),
                "delta54_pp": result54["return_pct"] - baseline54["return_pct"],
                "delta65_pp": result65["return_pct"] - baseline65["return_pct"],
                "dominates_historical_baseline_both_ledgers": (
                    result54["return_pct"] >= baseline54["return_pct"]
                    and result65["return_pct"] >= baseline65["return_pct"]
                    and result54["max_trade_close_drawdown_pct"]
                    >= baseline54["max_trade_close_drawdown_pct"]
                    and result65["max_trade_close_drawdown_pct"]
                    >= baseline65["max_trade_close_drawdown_pct"]
                ),
            }
        )
    dominant = [row for row in grid if row["dominates_historical_baseline_both_ledgers"]]
    best = max(dominant, key=lambda row: row["delta65_pp"]) if dominant else None
    selected_rule = (
        next(rule for rule in BRAKE_GRID if rule.name == best["rule"]["name"])
        if best
        else BASELINE
    )
    selected54 = simulate(trades54, selected_rule)
    selected65 = simulate(trades65, selected_rule)

    return {
        "schema_version": 1,
        "scope": "read_only_dynamic_risk_no_broker_imports_no_production_changes",
        "source_sha256": sha256_file(Path(source.__file__)),
        "provenance": provenance,
        "controls": actual,
        "baseline": {
            "ledger54": compact(baseline54),
            "ledger65": compact(baseline65),
        },
        "leverage_frontier": [
            {
                "rule": asdict(rule),
                "ledger54": compact(simulate(trades54, rule)),
                "ledger65": compact(simulate(trades65, rule)),
            }
            for rule in LEVERAGE_FRONTIER
        ],
        "monthly_drawdown_brake_grid": grid,
        "best_historical_dominant_row": best,
        "best_historical_diagnostics": {
            "rule": selected_rule.name,
            "ledger54_bootstrap": bootstrap_delta(trades54, selected_rule),
            "ledger65_bootstrap": bootstrap_delta(trades65, selected_rule),
            "ledger54_triggered_rows": [
                row for row in selected54["rows"] if row["triggered"]
            ],
            "ledger65_triggered_rows": [
                row for row in selected65["rows"] if row["triggered"]
            ],
        },
        "expanding_walk_forward": {
            "ledger54": expanding_walk_forward(trades54),
            "ledger65": expanding_walk_forward(trades65),
        },
        "frozen_early_selection": {
            "ledger54": frozen_early_selection(trades54),
            "ledger65": frozen_early_selection(trades65),
        },
        "gap_stress": {
            "interpretation": (
                "Deterministic opening-equity sensitivity, not a probability estimate; "
                "fees, liquidation slippage and broker margin changes are omitted."
            ),
            "rows": stress_table(),
        },
        "verdict": {
            "production_go": False,
            "reason": (
                "The only full-sample dominance is economically tiny on ledger65, "
                "comes from five Oct/Nov decisions, has no later affected forward "
                "observations, and does not improve the pre-trigger gap ruin boundary."
            ),
        },
        "limitations": [
            "All hypotheses were examined after the historical outcomes; walk-forward timing is causal but not untouched OOS evidence.",
            "October is a current-version in-sample reconstruction; November-July is the hash-checked frozen54 reconstruction.",
            "MDD is measured only at trade close; gaps, intraday liquidation, lot rounding, funding, tax and real slippage are absent.",
            "The account model assumes every instrument can obtain the requested broker leverage up to the common cap.",
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--verify-controls", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report()
    if args.verify_controls:
        print("Controls: PASS")
        print("Verdict: NO-GO")
    else:
        print(
            json.dumps(
                report,
                ensure_ascii=False,
                sort_keys=True,
                indent=2 if args.pretty else None,
                allow_nan=False,
            )
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        raise SystemExit(f"dynamic risk research error: {exc}")
