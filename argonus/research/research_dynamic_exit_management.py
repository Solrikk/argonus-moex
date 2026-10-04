#!/usr/bin/env python3
"""Research-only causal position-management study for Engine A + RS5.

This is deliberately separate from ``research_flat_month_exit_risk.py``.  The
earlier study covered fixed stop/target/time exits and pre-trade sizing.  This
study evaluates a small, frozen family of *dynamic* managers:

* close-confirmed breakeven / profit-lock ratchets;
* partial profit taking, optionally followed by breakeven;
* conditional checkpoint exits or de-risking.

There is no broker client and no import of ``trade_bot``.  Every management
decision uses the current candle close and becomes effective on the next
five-minute candle.  On a candle where the active stop and any favourable
limit are both touched, the stop is always applied first.  The same immutable
65-trade and 54-trade ledgers and 0.08% round-trip fee as the existing study
are used.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from argonus.research import research_flat_month_exit_risk as base


EXPECTED_BASELINE_54_TABLE = base.EXPECTED_54_TABLE_RETURN
EXPECTED_BASELINE_65_TABLE = base.EXPECTED_65_TABLE_RETURN
EXPECTED_BASELINE_65_ACCOUNT = base.EXPECTED_65_FIXED_RETURN
EPSILON = 1e-12


@dataclass(frozen=True, slots=True)
class ManagementRule:
    name: str
    # (close progress in fractions of frozen T4 distance, locked progress).
    ratchets: tuple[tuple[float, float], ...] = ()
    # A partial limit is a fraction of frozen T4 distance and position.
    partial_trigger_t: float | None = None
    partial_fraction: float = 0.0
    partial_then_breakeven: bool = False
    # A checkpoint observes that candle's close.  Its order is filled at the
    # next available five-minute open, after the signal is actually knowable.
    checkpoint_time: str | None = None
    checkpoint_max_progress_t: float = 0.0
    checkpoint_exit_fraction: float = 0.0


@dataclass(frozen=True, slots=True)
class ManagedExit:
    net_return_pct: float
    gross_return_pct: float
    last_exit_time: str
    final_reason: str
    events: tuple[str, ...]
    ambiguous_stop_first: bool


def frozen_rules() -> tuple[ManagementRule, ...]:
    """Return the complete predeclared family; do not tune it in this study."""
    return (
        ManagementRule("baseline"),
        ManagementRule("be_close_025t", ratchets=((0.25, 0.0),)),
        ManagementRule("be_close_050t", ratchets=((0.50, 0.0),)),
        ManagementRule(
            "ratchet_close_025t_be_050t_lock025t",
            ratchets=((0.25, 0.0), (0.50, 0.25)),
        ),
        ManagementRule(
            "partial50_at_050t", partial_trigger_t=0.50, partial_fraction=0.50
        ),
        ManagementRule(
            "partial50_at_075t", partial_trigger_t=0.75, partial_fraction=0.50
        ),
        ManagementRule(
            "partial50_at_050t_then_be",
            partial_trigger_t=0.50,
            partial_fraction=0.50,
            partial_then_breakeven=True,
        ),
        ManagementRule(
            "partial50_at_075t_then_be",
            partial_trigger_t=0.75,
            partial_fraction=0.50,
            partial_then_breakeven=True,
        ),
        ManagementRule(
            "failfast_1200_if_nonpositive",
            checkpoint_time="12:00",
            checkpoint_max_progress_t=0.0,
            checkpoint_exit_fraction=1.0,
        ),
        ManagementRule(
            "failfast_1400_if_nonpositive",
            checkpoint_time="14:00",
            checkpoint_max_progress_t=0.0,
            checkpoint_exit_fraction=1.0,
        ),
        ManagementRule(
            "derisk50_1200_if_nonpositive",
            checkpoint_time="12:00",
            checkpoint_max_progress_t=0.0,
            checkpoint_exit_fraction=0.50,
        ),
        ManagementRule(
            "derisk50_1400_if_nonpositive",
            checkpoint_time="14:00",
            checkpoint_max_progress_t=0.0,
            checkpoint_exit_fraction=0.50,
        ),
        ManagementRule(
            "late_guard_1700_if_below_025t",
            checkpoint_time="17:00",
            checkpoint_max_progress_t=0.25,
            checkpoint_exit_fraction=1.0,
        ),
    )


def _directional_return(entry: float, price: float, sign: float) -> float:
    return sign * (price / entry - 1.0)


def _price_at_progress(entry: float, distance: float, sign: float, progress: float) -> float:
    return entry * (1.0 + sign * distance * progress)


def _favourable_hit(trade: base.Trade, candle: Any, price: float) -> bool:
    return candle.high >= price if trade.direction == "long" else candle.low <= price


def _stop_hit(trade: base.Trade, candle: Any, price: float) -> bool:
    return candle.low <= price if trade.direction == "long" else candle.high >= price


def _ratchet_stop(current: float, proposed: float, direction: str) -> float:
    return max(current, proposed) if direction == "long" else min(current, proposed)


def simulate_managed_exit(trade: base.Trade, rule: ManagementRule) -> ManagedExit:
    """Replay one manager with conservative five-minute event ordering."""
    window = [candle for candle in trade.candles if "07:00" <= candle.time <= "18:35"]
    if not window:
        raise RuntimeError(f"No candles for {trade.date} {trade.symbol}")
    entry = float(window[0].open)
    sign = 1.0 if trade.direction == "long" else -1.0
    target_distance = sign * (float(trade.target_price) / entry - 1.0)
    if target_distance <= 0.0:
        raise RuntimeError(f"Frozen target behind entry: {trade.date} {trade.symbol}")
    target = _price_at_progress(entry, target_distance, sign, 1.0)
    active_stop = entry * (1.0 - sign * base.BASELINE_STOP / 100.0)
    partial_price = (
        _price_at_progress(entry, target_distance, sign, rule.partial_trigger_t)
        if rule.partial_trigger_t is not None
        else None
    )

    remaining = 1.0
    gross = 0.0
    partial_done = False
    pending_checkpoint: tuple[float, str] | None = None
    events: list[str] = []
    ambiguous = False
    last_time = window[-1].time
    final_reason = "time_exit"

    def realise(fraction: float, price: float, event: str, event_time: str) -> None:
        nonlocal remaining, gross, last_time, final_reason
        fraction = min(max(fraction, 0.0), remaining)
        if fraction <= EPSILON:
            return
        gross += fraction * _directional_return(entry, price, sign) * 100.0
        remaining -= fraction
        if remaining < EPSILON:
            remaining = 0.0
        events.append(f"{event}@{event_time}:{fraction:.6f}")
        last_time = event_time
        final_reason = event

    for candle in window:
        if remaining <= EPSILON:
            break

        # A close-confirmed checkpoint cannot be filled at that same close:
        # the close is only known once the candle has completed.  Model the
        # resulting market order at the next available five-minute open.
        if pending_checkpoint is not None:
            fraction, event = pending_checkpoint
            realise(fraction, float(candle.open), event, candle.time)
            pending_checkpoint = None
            if remaining <= EPSILON:
                break

        stop_touched = _stop_hit(trade, candle, active_stop)
        target_touched = _favourable_hit(trade, candle, target)
        partial_touched = bool(
            partial_price is not None
            and not partial_done
            and _favourable_hit(trade, candle, partial_price)
        )

        # Conservative ambiguity policy: an already-active stop wins over every
        # favourable fill on that candle.
        if stop_touched:
            ambiguous = ambiguous or target_touched or partial_touched
            realise(
                remaining,
                active_stop,
                "both_stop_first" if (target_touched or partial_touched) else "stop",
                candle.time,
            )
            break

        pending_breakeven = False
        if partial_touched and partial_price is not None:
            realise(rule.partial_fraction, partial_price, "partial_target", candle.time)
            partial_done = True
            pending_breakeven = rule.partial_then_breakeven

        if remaining > EPSILON and target_touched:
            realise(remaining, target, "target", candle.time)
            break

        if (
            remaining > EPSILON
            and rule.checkpoint_time == candle.time
            and rule.checkpoint_exit_fraction > 0.0
        ):
            close_progress = _directional_return(entry, float(candle.close), sign) / target_distance
            if close_progress <= rule.checkpoint_max_progress_t + EPSILON:
                pending_checkpoint = (
                    rule.checkpoint_exit_fraction,
                    "checkpoint_derisk_next_open"
                    if rule.checkpoint_exit_fraction < 1.0
                    else "checkpoint_exit_next_open",
                )

        if remaining > EPSILON and candle.time == "18:35":
            realise(remaining, float(candle.close), "time_exit", candle.time)
            break

        if remaining <= EPSILON:
            break

        # Close-derived stop changes are intentionally delayed until the next
        # candle.  The current candle's low/high can never trigger a stop that
        # was learned from its own close.
        close_progress = _directional_return(entry, float(candle.close), sign) / target_distance
        next_stop = active_stop
        for trigger, locked_progress in rule.ratchets:
            if close_progress + EPSILON >= trigger:
                proposed = _price_at_progress(
                    entry, target_distance, sign, locked_progress
                )
                next_stop = _ratchet_stop(next_stop, proposed, trade.direction)
        if pending_breakeven:
            next_stop = _ratchet_stop(next_stop, entry, trade.direction)
        active_stop = next_stop

    if remaining > EPSILON:
        raise RuntimeError(f"Position remained open: {trade.date} {trade.symbol} {rule.name}")
    return ManagedExit(
        net_return_pct=gross - base.FEE_PCT,
        gross_return_pct=gross,
        last_exit_time=last_time,
        final_reason=final_reason,
        events=tuple(events),
        ambiguous_stop_first=ambiguous,
    )


def net_vector(trades: Sequence[base.Trade], rule: ManagementRule) -> list[float]:
    return [simulate_managed_exit(trade, rule).net_return_pct for trade in trades]


def table_vector(trades: Sequence[base.Trade], net: Sequence[float]) -> list[float]:
    return [
        value * base.table_multiplier(trade)
        for trade, value in zip(trades, net, strict=True)
    ]


def account_vector(trades: Sequence[base.Trade], net: Sequence[float]) -> list[float]:
    equity = base.START_EQUITY
    values: list[float] = []
    for trade, value in zip(trades, net, strict=True):
        del trade  # The current live sizing is direction-agnostic.
        position = min(base.POSITION_CAP, base.MAX_LEVERAGE * equity)
        account_return = position / equity * value
        values.append(account_return)
        equity *= 1.0 + account_return / 100.0
        if equity <= 0.0:
            raise RuntimeError("Account depleted in research replay")
    return values


def metrics_for(
    trades: Sequence[base.Trade], net: Sequence[float]
) -> dict[str, dict[str, Any]]:
    table = table_vector(trades, net)
    account = account_vector(trades, net)
    account_metrics = base.return_metrics(account, trades)
    account_metrics["final_equity_rub"] = base.START_EQUITY * (
        1.0 + account_metrics["return_pct"] / 100.0
    )
    return {
        "table": base.return_metrics(table, trades),
        "account_50k_position_150k": account_metrics,
    }


def _subset(
    trades: Sequence[base.Trade], values: Sequence[float], *, start_month: str
) -> tuple[list[base.Trade], list[float]]:
    indices = [index for index, trade in enumerate(trades) if trade.month >= start_month]
    return [trades[index] for index in indices], [values[index] for index in indices]


def _rank_rule(
    rules: Sequence[ManagementRule],
    trades: Sequence[base.Trade],
    vectors: dict[str, list[float]],
) -> ManagementRule:
    rows = []
    for rule in rules:
        account = account_vector(trades, vectors[rule.name])
        metrics = base.return_metrics(account, trades)
        rows.append((base.objective(metrics), metrics["return_pct"], rule.name, rule))
    rows.sort(reverse=True, key=lambda row: (row[0], row[1], row[2]))
    return rows[0][3]


def expanding_walk_forward(
    rules: Sequence[ManagementRule], trades: Sequence[base.Trade]
) -> dict[str, Any]:
    months = sorted({trade.month for trade in trades})
    test_months = months[3:]
    full_vectors = {rule.name: net_vector(trades, rule) for rule in rules}
    baseline_full = full_vectors["baseline"]
    chosen_net: list[float] = []
    baseline_net: list[float] = []
    stitched_trades: list[base.Trade] = []
    selections: list[dict[str, Any]] = []
    for test_month in test_months:
        train_indices = [i for i, trade in enumerate(trades) if trade.month < test_month]
        test_indices = [i for i, trade in enumerate(trades) if trade.month == test_month]
        train_trades = [trades[i] for i in train_indices]
        train_vectors = {
            name: [values[i] for i in train_indices]
            for name, values in full_vectors.items()
        }
        selected = _rank_rule(rules, train_trades, train_vectors)
        selected_values = [full_vectors[selected.name][i] for i in test_indices]
        control_values = [baseline_full[i] for i in test_indices]
        test_trades = [trades[i] for i in test_indices]
        chosen_net.extend(selected_values)
        baseline_net.extend(control_values)
        stitched_trades.extend(test_trades)
        selected_account = account_vector(test_trades, selected_values)
        control_account = account_vector(test_trades, control_values)
        selections.append(
            {
                "test_month": test_month,
                "training_trades": len(train_trades),
                "selected_rule": selected.name,
                "account_test_return_pct": base.compound(selected_account),
                "account_baseline_return_pct": base.compound(control_account),
                "account_delta_pp": base.compound(selected_account)
                - base.compound(control_account),
            }
        )
    chosen_metrics = metrics_for(stitched_trades, chosen_net)
    baseline_metrics = metrics_for(stitched_trades, baseline_net)
    return {
        "test_months": test_months,
        "test_trades": len(stitched_trades),
        "selected_rule_frequency": dict(
            sorted(Counter(row["selected_rule"] for row in selections).items())
        ),
        "strategy": chosen_metrics,
        "baseline": baseline_metrics,
        "account_month_delta": base.month_delta_stats(
            account_vector(stitched_trades, chosen_net),
            account_vector(stitched_trades, baseline_net),
            stitched_trades,
        ),
        "selections": selections,
    }


def one_shot_holdout(
    rules: Sequence[ManagementRule],
    trades: Sequence[base.Trade],
    development_end_month: str,
) -> dict[str, Any]:
    development = [trade for trade in trades if trade.month <= development_end_month]
    holdout = [trade for trade in trades if trade.month > development_end_month]
    development_vectors = {
        rule.name: net_vector(development, rule) for rule in rules
    }
    selected = _rank_rule(rules, development, development_vectors)
    selected_net = net_vector(holdout, selected)
    baseline_net = net_vector(holdout, rules[0])
    selected_metrics = metrics_for(holdout, selected_net)
    baseline_metrics = metrics_for(holdout, baseline_net)
    return {
        "development_end_month": development_end_month,
        "development_trades": len(development),
        "holdout_months": sorted({trade.month for trade in holdout}),
        "holdout_trades": len(holdout),
        "selected_rule": selected.name,
        "strategy": selected_metrics,
        "baseline": baseline_metrics,
        "account_month_delta": base.month_delta_stats(
            account_vector(holdout, selected_net),
            account_vector(holdout, baseline_net),
            holdout,
        ),
    }


def evaluate_rule(
    rule: ManagementRule,
    trades54: Sequence[base.Trade],
    trades65: Sequence[base.Trade],
    baseline54: Sequence[float],
    baseline65: Sequence[float],
) -> dict[str, Any]:
    net54 = net_vector(trades54, rule)
    net65 = net_vector(trades65, rule)
    metrics54 = metrics_for(trades54, net54)
    metrics65 = metrics_for(trades65, net65)
    table54 = table_vector(trades54, net54)
    table65 = table_vector(trades65, net65)
    account54 = account_vector(trades54, net54)
    account65 = account_vector(trades65, net65)
    baseline_account54 = account_vector(trades54, baseline54)
    baseline_account65 = account_vector(trades65, baseline65)

    holdout54_trades, holdout54_net = _subset(trades54, net54, start_month="2026-03")
    _, holdout54_base = _subset(trades54, baseline54, start_month="2026-03")
    holdout65_trades, holdout65_net = _subset(trades65, net65, start_month="2026-02")
    _, holdout65_base = _subset(trades65, baseline65, start_month="2026-02")
    holdout54 = metrics_for(holdout54_trades, holdout54_net)
    holdout65 = metrics_for(holdout65_trades, holdout65_net)
    holdout54_control = metrics_for(holdout54_trades, holdout54_base)
    holdout65_control = metrics_for(holdout65_trades, holdout65_base)

    return {
        "rule": asdict(rule),
        "ledger54": metrics54,
        "ledger65": metrics65,
        "delta": {
            "ledger54_table_pp": metrics54["table"]["return_pct"]
            - base.compound(table_vector(trades54, baseline54)),
            "ledger65_table_pp": metrics65["table"]["return_pct"]
            - base.compound(table_vector(trades65, baseline65)),
            "ledger54_account_pp": metrics54["account_50k_position_150k"]["return_pct"]
            - base.compound(baseline_account54),
            "ledger65_account_pp": metrics65["account_50k_position_150k"]["return_pct"]
            - base.compound(baseline_account65),
        },
        "account_month_delta65": base.month_delta_stats(
            account65, baseline_account65, trades65
        ),
        "account_concentration54": base.delta_concentration(
            account54, baseline_account54, trades54
        ),
        "account_concentration65": base.delta_concentration(
            account65, baseline_account65, trades65
        ),
        "account_month_bootstrap54": base.month_block_bootstrap(
            account54, baseline_account54, trades54
        ),
        "account_month_bootstrap65": base.month_block_bootstrap(
            account65, baseline_account65, trades65
        ),
        "chronological_holdout": {
            "ledger54_mar_jul": {
                "strategy": holdout54,
                "baseline": holdout54_control,
                "account_month_delta": base.month_delta_stats(
                    account_vector(holdout54_trades, holdout54_net),
                    account_vector(holdout54_trades, holdout54_base),
                    holdout54_trades,
                ),
            },
            "ledger65_feb_jul": {
                "strategy": holdout65,
                "baseline": holdout65_control,
                "account_month_delta": base.month_delta_stats(
                    account_vector(holdout65_trades, holdout65_net),
                    account_vector(holdout65_trades, holdout65_base),
                    holdout65_trades,
                ),
            },
        },
    }


def robustness_checks(row: dict[str, Any], baseline: dict[str, Any]) -> dict[str, bool]:
    a54 = row["ledger54"]["account_50k_position_150k"]
    a65 = row["ledger65"]["account_50k_position_150k"]
    b54 = baseline["ledger54"]["account_50k_position_150k"]
    b65 = baseline["ledger65"]["account_50k_position_150k"]
    h54 = row["chronological_holdout"]["ledger54_mar_jul"]
    h65 = row["chronological_holdout"]["ledger65_feb_jul"]
    return {
        "not_baseline": row["rule"]["name"] != "baseline",
        "full_account_return_beats_54": a54["return_pct"] > b54["return_pct"],
        "full_account_return_beats_65": a65["return_pct"] > b65["return_pct"],
        "full_account_mdd_not_worse_54": a54["max_drawdown_pct"]
        >= b54["max_drawdown_pct"] - EPSILON,
        "full_account_mdd_not_worse_65": a65["max_drawdown_pct"]
        >= b65["max_drawdown_pct"] - EPSILON,
        "holdout_account_return_beats_54": h54["strategy"]["account_50k_position_150k"]["return_pct"]
        > h54["baseline"]["account_50k_position_150k"]["return_pct"],
        "holdout_account_return_beats_65": h65["strategy"]["account_50k_position_150k"]["return_pct"]
        > h65["baseline"]["account_50k_position_150k"]["return_pct"],
        "holdout_account_mdd_not_worse_54": h54["strategy"]["account_50k_position_150k"]["max_drawdown_pct"]
        >= h54["baseline"]["account_50k_position_150k"]["max_drawdown_pct"] - EPSILON,
        "holdout_account_mdd_not_worse_65": h65["strategy"]["account_50k_position_150k"]["max_drawdown_pct"]
        >= h65["baseline"]["account_50k_position_150k"]["max_drawdown_pct"] - EPSILON,
        "positive_delta_months_65_ge_6": row["account_month_delta65"]["positive_delta_months"] >= 6,
        "bootstrap_ci_low_positive_54": row["account_month_bootstrap54"]["delta_ci95_pp"][0] > 0.0,
        "bootstrap_ci_low_positive_65": row["account_month_bootstrap65"]["delta_ci95_pp"][0] > 0.0,
        "survives_best_contributor_54": row["account_concentration54"]["delta_without_best_contributor_pp"] > 0.0,
        "survives_best_contributor_65": row["account_concentration65"]["delta_without_best_contributor_pp"] > 0.0,
    }


def build_report() -> dict[str, Any]:
    trades54, trades65, provenance = base.load_books()
    rules = frozen_rules()
    baseline_rule = rules[0]
    baseline54 = net_vector(trades54, baseline_rule)
    baseline65 = net_vector(trades65, baseline_rule)

    # Independent replay control against the previous simulator.
    old54 = [base.simulate_exit(trade, base.baseline_rule()).net_return_pct for trade in trades54]
    old65 = [base.simulate_exit(trade, base.baseline_rule()).net_return_pct for trade in trades65]
    max_net_error = max(
        [abs(a - b) for a, b in zip(baseline54, old54, strict=True)]
        + [abs(a - b) for a, b in zip(baseline65, old65, strict=True)]
    )
    if max_net_error > 1e-12:
        raise RuntimeError(f"Baseline engine drift: {max_net_error}")

    evaluated = [
        evaluate_rule(rule, trades54, trades65, baseline54, baseline65)
        for rule in rules
    ]
    baseline_row = evaluated[0]
    controls = {
        "max_baseline_net_replay_error_pct": max_net_error,
        "ledger54_table_return_pct": baseline_row["ledger54"]["table"]["return_pct"],
        "ledger65_table_return_pct": baseline_row["ledger65"]["table"]["return_pct"],
        "ledger65_account_return_pct": baseline_row["ledger65"]["account_50k_position_150k"]["return_pct"],
    }
    expected = (
        ("ledger54_table_return_pct", EXPECTED_BASELINE_54_TABLE),
        ("ledger65_table_return_pct", EXPECTED_BASELINE_65_TABLE),
        ("ledger65_account_return_pct", EXPECTED_BASELINE_65_ACCOUNT),
    )
    for key, value in expected:
        if not math.isclose(controls[key], value, rel_tol=0.0, abs_tol=1e-9):
            raise RuntimeError(f"Control mismatch {key}: {controls[key]} != {value}")

    for row in evaluated:
        row["robustness_checks"] = robustness_checks(row, baseline_row)
        row["passes_all_robustness_checks"] = all(row["robustness_checks"].values())

    ranked = sorted(
        evaluated,
        key=lambda row: (
            base.objective(row["ledger65"]["account_50k_position_150k"]),
            row["ledger65"]["account_50k_position_150k"]["return_pct"],
            row["rule"]["name"],
        ),
        reverse=True,
    )
    wf65 = expanding_walk_forward(rules, trades65)
    wf54 = expanding_walk_forward(rules, trades54)
    holdout65 = one_shot_holdout(rules, trades65, "2026-01")
    holdout54 = one_shot_holdout(rules, trades54, "2026-02")
    robust = [row["rule"]["name"] for row in evaluated if row["passes_all_robustness_checks"]]

    return {
        "schema_version": 1,
        "artifact_type": "argonus_dynamic_exit_management_research",
        "classification": "RESEARCH_ONLY_ARCHIVED_CAUSAL_WITH_CHRONOLOGICAL_HOLDOUT",
        "generated_for_date": "2026-07-18",
        "production_changed": False,
        "method": {
            "fee_pct_round_trip": base.FEE_PCT,
            "entry": "07:00 candle open; same convention as frozen baseline",
            "active_stop": "1.00% baseline stop; stop-first on every ambiguous 5m candle",
            "causal_update": (
                "ratchet observes current close and changes the stop only for the next "
                "5m candle; checkpoint market exits fill at the next 5m open"
            ),
            "partial_fee": "0.08% weighted round trip; partial legs sum to the original position",
            "family_frozen_rules": [asdict(rule) for rule in rules],
            "multiplicity_policy": "all 12 challengers reported; no post-result parameter refinement",
            "walk_forward": "initial three calendar months, then expanding monthly selection; baseline is selectable",
            "one_shot_holdout": {
                "ledger65": "develop Oct-Jan, score Feb-Jul once",
                "ledger54": "develop Nov-Feb, score Mar-Jul once",
            },
            "bootstrap": f"{base.BOOTSTRAP_SAMPLES} month-block samples, seed {base.BOOTSTRAP_SEED}",
        },
        "provenance": provenance,
        "controls": controls,
        "baseline": baseline_row,
        "ranked_exploratory": ranked,
        "expanding_walk_forward": {"ledger65": wf65, "ledger54": wf54},
        "one_shot_holdout": {"ledger65": holdout65, "ledger54": holdout54},
        "robust_candidates": robust,
        "verdict": {
            "status": "RESEARCH_CANDIDATE" if robust else "NO_GO",
            "production_change_authorized": False,
            "reason": (
                "Archived evidence can nominate only a forward-shadow candidate. "
                "Production remains unchanged even if every retrospective gate passes."
            ),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--verify-controls", action="store_true")
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args()


def summary(report: dict[str, Any]) -> dict[str, Any]:
    ranked = report["ranked_exploratory"]
    rows = []
    for row in ranked:
        rows.append(
            {
                "name": row["rule"]["name"],
                "return65_account_pct": row["ledger65"]["account_50k_position_150k"]["return_pct"],
                "mdd65_account_pct": row["ledger65"]["account_50k_position_150k"]["max_drawdown_pct"],
                "delta65_account_pp": row["delta"]["ledger65_account_pp"],
                "return54_account_pct": row["ledger54"]["account_50k_position_150k"]["return_pct"],
                "mdd54_account_pct": row["ledger54"]["account_50k_position_150k"]["max_drawdown_pct"],
                "holdout65_return_pct": row["chronological_holdout"]["ledger65_feb_jul"]["strategy"]["account_50k_position_150k"]["return_pct"],
                "holdout65_baseline_pct": row["chronological_holdout"]["ledger65_feb_jul"]["baseline"]["account_50k_position_150k"]["return_pct"],
                "holdout54_return_pct": row["chronological_holdout"]["ledger54_mar_jul"]["strategy"]["account_50k_position_150k"]["return_pct"],
                "holdout54_baseline_pct": row["chronological_holdout"]["ledger54_mar_jul"]["baseline"]["account_50k_position_150k"]["return_pct"],
                "positive_delta_months65": row["account_month_delta65"]["positive_delta_months"],
                "passes_all": row["passes_all_robustness_checks"],
            }
        )
    return {
        "controls": report["controls"],
        "ranked": rows,
        "walk_forward": report["expanding_walk_forward"],
        "one_shot_holdout": report["one_shot_holdout"],
        "robust_candidates": report["robust_candidates"],
        "verdict": report["verdict"],
    }


def main() -> int:
    args = parse_args()
    report = build_report()
    if args.verify_controls:
        print(
            "controls ok: "
            f"54={report['controls']['ledger54_table_return_pct']:.12f}% "
            f"65={report['controls']['ledger65_table_return_pct']:.12f}% "
            f"account65={report['controls']['ledger65_account_return_pct']:.12f}%"
        )
        return 0
    value = summary(report) if args.summary else report
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if args.pretty else None,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
