#!/usr/bin/env python3
"""Read-only entry-timing sensitivity study on the frozen 54/65 ledgers.

The candidate waits for the first complete five-minute candle and enters at
the 07:05 candle open.  It keeps the pre-trade absolute T4 target, resets the
1% stop from the delayed entry, uses stop-first on ambiguous candles, exits at
18:35, and charges the same 0.08% round-trip cost as the published control.

This is a post-hoc historical discovery, not an activation artifact.  The
module never imports ``trade_bot`` and has no broker/network/write path.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from argonus.research import research_flat_month_exit_risk as base


ENTRY_TIMES = ("07:00", "07:05", "07:10", "07:15", "07:30", "08:00")
CONTROL_ENTRY = "07:00"
CANDIDATE_ENTRY = "07:05"
TIME_EXIT = "18:35"
STOP_PCT = 1.0


@dataclass(frozen=True, slots=True)
class EntryResult:
    date: str
    month: str
    symbol: str
    direction: str
    entry_time: str
    entry_price: float
    target_price: float
    stop_price: float
    gross_return_pct: float
    net_return_pct: float
    exit_reason: str
    exit_time: str
    ambiguous: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--verify-controls", action="store_true")
    return parser.parse_args()


def simulate_entry(trade: base.Trade, entry_time: str) -> EntryResult:
    window = [
        candle for candle in trade.candles
        if entry_time <= candle.time <= TIME_EXIT
    ]
    if not window:
        raise RuntimeError(
            f"missing candle at/after {entry_time}: {trade.date} {trade.symbol}"
        )
    entry = window[0].open
    sign = 1.0 if trade.direction == "long" else -1.0
    target = trade.target_price
    if sign * (target / entry - 1.0) <= 0.0:
        raise RuntimeError(
            f"absolute target is behind {entry_time} entry: "
            f"{trade.date} {trade.symbol}"
        )
    stop = entry * (1.0 - sign * STOP_PCT / 100.0)
    exit_price = window[-1].close
    exit_time = window[-1].time
    reason = "time_exit"
    ambiguous = False
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
            break
        if target_hit:
            exit_price = target
            exit_time = candle.time
            reason = "target"
            break
    gross = sign * (exit_price / entry - 1.0) * 100.0
    return EntryResult(
        date=trade.date,
        month=trade.month,
        symbol=trade.symbol,
        direction=trade.direction,
        entry_time=window[0].time,
        entry_price=entry,
        target_price=target,
        stop_price=stop,
        gross_return_pct=gross,
        net_return_pct=gross - base.FEE_PCT,
        exit_reason=reason,
        exit_time=exit_time,
        ambiguous=ambiguous,
    )


def table_returns(trades: Sequence[base.Trade], entry_time: str) -> list[float]:
    return [
        simulate_entry(trade, entry_time).net_return_pct * base.table_multiplier(trade)
        for trade in trades
    ]


def fixed_account(
    trades: Sequence[base.Trade],
    entry_time: str,
    *,
    extra_cost_pct: float = 0.0,
) -> dict[str, Any]:
    """Replay min(150k, 3*equity); extra_cost is a transparent cost stress."""
    equity = base.START_EQUITY
    peak = equity
    max_drawdown = 0.0
    monthly_start: dict[str, float] = {}
    monthly_end: dict[str, float] = {}
    rows: list[dict[str, Any]] = []
    for trade in trades:
        monthly_start.setdefault(trade.month, equity)
        result = simulate_entry(trade, entry_time)
        net = result.net_return_pct - extra_cost_pct
        position = min(base.POSITION_CAP, base.MAX_LEVERAGE * equity)
        before = equity
        pnl = position * net / 100.0
        equity += pnl
        if equity <= 0.0:
            raise RuntimeError(f"account depleted on {trade.date} {trade.symbol}")
        peak = max(peak, equity)
        drawdown = (equity / peak - 1.0) * 100.0
        max_drawdown = min(max_drawdown, drawdown)
        monthly_end[trade.month] = equity
        rows.append(
            {
                **asdict(result),
                "net_after_extra_cost_pct": net,
                "position_rub": position,
                "equity_before_rub": before,
                "pnl_rub": pnl,
                "equity_after_rub": equity,
                "drawdown_pct": drawdown,
            }
        )
    monthly = {
        month: (monthly_end[month] / monthly_start[month] - 1.0) * 100.0
        for month in sorted(monthly_start)
    }
    return {
        "trades": len(rows),
        "wins": sum(row["net_after_extra_cost_pct"] > 0.0 for row in rows),
        "losses": sum(row["net_after_extra_cost_pct"] <= 0.0 for row in rows),
        "ending_equity_rub": equity,
        "return_pct": (equity / base.START_EQUITY - 1.0) * 100.0,
        "max_drawdown_pct": max_drawdown,
        "worst_month_pct": min(monthly.values()),
        "positive_months": sum(value > 0.0 for value in monthly.values()),
        "monthly": monthly,
        "per_trade": rows,
    }


def expanding_walk_forward(trades: Sequence[base.Trade]) -> dict[str, Any]:
    """Select a natural entry time on prior months, then freeze for next month."""
    months = sorted({trade.month for trade in trades})
    stitched: list[float] = []
    control: list[float] = []
    selections: list[dict[str, Any]] = []
    for test_month in months[3:]:
        train = [trade for trade in trades if trade.month < test_month]
        test = [trade for trade in trades if trade.month == test_month]
        ranked = []
        for entry_time in ENTRY_TIMES:
            metrics = base.return_metrics(table_returns(train, entry_time), train)
            ranked.append(
                (base.objective(metrics), metrics["return_pct"], entry_time)
            )
        selected = max(ranked)[2]
        selected_values = table_returns(test, selected)
        control_values = table_returns(test, CONTROL_ENTRY)
        stitched.extend(selected_values)
        control.extend(control_values)
        selections.append(
            {
                "test_month": test_month,
                "selected_entry_time": selected,
                "test_return_pct": base.compound(selected_values),
                "control_return_pct": base.compound(control_values),
                "delta_pp": (
                    base.compound(selected_values) - base.compound(control_values)
                ),
            }
        )
    return {
        "evaluation_months": months[3:],
        "selections": selections,
        "selection_counts": dict(
            sorted(Counter(row["selected_entry_time"] for row in selections).items())
        ),
        "selector_return_pct": base.compound(stitched),
        "control_return_pct": base.compound(control),
        "delta_pp": base.compound(stitched) - base.compound(control),
    }


def split_diagnostics(trades: Sequence[base.Trade]) -> list[dict[str, Any]]:
    months = sorted({trade.month for trade in trades})
    windows = (
        ("early", months[:3]),
        ("middle", months[3:5]),
        ("later", months[5:]),
    )
    result = []
    for name, selected_months in windows:
        subset = [trade for trade in trades if trade.month in selected_months]
        control = table_returns(subset, CONTROL_ENTRY)
        candidate = table_returns(subset, CANDIDATE_ENTRY)
        control_fixed = fixed_account(subset, CONTROL_ENTRY)
        candidate_fixed = fixed_account(subset, CANDIDATE_ENTRY)
        result.append(
            {
                "window": name,
                "months": selected_months,
                "trades": len(subset),
                "table_control_return_pct": base.compound(control),
                "table_candidate_return_pct": base.compound(candidate),
                "table_delta_pp": base.compound(candidate) - base.compound(control),
                "fixed_control_return_pct": control_fixed["return_pct"],
                "fixed_candidate_return_pct": candidate_fixed["return_pct"],
                "fixed_delta_pp": (
                    candidate_fixed["return_pct"] - control_fixed["return_pct"]
                ),
            }
        )
    return result


def evaluate_book(trades: Sequence[base.Trade]) -> dict[str, Any]:
    control = table_returns(trades, CONTROL_ENTRY)
    candidate = table_returns(trades, CANDIDATE_ENTRY)
    control_fixed = fixed_account(trades, CONTROL_ENTRY)
    candidate_fixed = fixed_account(trades, CANDIDATE_ENTRY)
    grid = {}
    for entry_time in ENTRY_TIMES:
        values = table_returns(trades, entry_time)
        account = fixed_account(trades, entry_time)
        grid[entry_time] = {
            "table": base.return_metrics(values, trades),
            "fixed": {
                key: account[key]
                for key in (
                    "trades", "wins", "losses", "ending_equity_rub",
                    "return_pct", "max_drawdown_pct", "worst_month_pct",
                    "positive_months", "monthly",
                )
            },
        }
    return {
        "control": grid[CONTROL_ENTRY],
        "candidate_0705": grid[CANDIDATE_ENTRY],
        "entry_time_grid": grid,
        "monthly_delta": base.month_delta_stats(candidate, control, trades),
        "delta_concentration": base.delta_concentration(candidate, control, trades),
        "month_block_bootstrap": base.month_block_bootstrap(candidate, control, trades),
        "chronological_splits": split_diagnostics(trades),
        "expanding_walk_forward": expanding_walk_forward(trades),
        "extra_cost_stress_fixed": {
            f"{stress:.2f}": {
                key: value
                for key, value in fixed_account(
                    trades, CANDIDATE_ENTRY, extra_cost_pct=stress
                ).items()
                if key in ("return_pct", "ending_equity_rub", "max_drawdown_pct")
            }
            for stress in (0.02, 0.05, 0.08, 0.10, 0.15, 0.20, 0.30)
        },
    }


def build_report() -> dict[str, Any]:
    trades54, trades65, provenance = base.load_books()
    ledger54 = evaluate_book(trades54)
    ledger65 = evaluate_book(trades65)
    return {
        "study": "post_hoc_entry_timing_sensitivity",
        "production_verdict": "NO_GO",
        "candidate_status": "forward_shadow_only",
        "reason": (
            "The attractive full-history delta is concentrated in a few early "
            "rescued trades; the later slice and expanding walk-forward delta "
            "are approximately flat, bootstrap intervals include zero, and an "
            "exact 07:05 candle open is not a guaranteed executable live fill."
        ),
        "contract": {
            "control_entry_time": CONTROL_ENTRY,
            "candidate_entry_time": CANDIDATE_ENTRY,
            "absolute_target": "frozen pre-trade T4",
            "stop_pct_from_entry": STOP_PCT,
            "time_exit": TIME_EXIT,
            "round_trip_cost_pct": base.FEE_PCT,
            "ambiguous_candle_policy": "stop_first",
            "fixed_position_formula": "min(150000 RUB, 3 * current_equity)",
        },
        "ledger54": ledger54,
        "ledger65": ledger65,
        "provenance": provenance,
    }


def verify_controls(report: dict[str, Any]) -> None:
    value54 = report["ledger54"]["control"]["table"]["return_pct"]
    value65 = report["ledger65"]["control"]["table"]["return_pct"]
    fixed65 = report["ledger65"]["control"]["fixed"]["return_pct"]
    if abs(value54 - base.EXPECTED_54_TABLE_RETURN) > 1e-9:
        raise RuntimeError(f"54 control mismatch: {value54}")
    if abs(value65 - base.EXPECTED_65_TABLE_RETURN) > 1e-9:
        raise RuntimeError(f"65 control mismatch: {value65}")
    if abs(fixed65 - base.EXPECTED_65_FIXED_RETURN) > 1e-9:
        raise RuntimeError(f"65 fixed control mismatch: {fixed65}")


def pretty_summary(report: dict[str, Any]) -> str:
    lines = [
        "Entry timing sensitivity (research only)",
        f"verdict={report['production_verdict']}",
    ]
    for name in ("ledger54", "ledger65"):
        row = report[name]
        control = row["control"]
        candidate = row["candidate_0705"]
        lines.append(
            f"{name}: table {control['table']['return_pct']:+.4f}% -> "
            f"{candidate['table']['return_pct']:+.4f}%; fixed "
            f"{control['fixed']['return_pct']:+.4f}% -> "
            f"{candidate['fixed']['return_pct']:+.4f}%; fixed MDD "
            f"{control['fixed']['max_drawdown_pct']:.4f}% -> "
            f"{candidate['fixed']['max_drawdown_pct']:.4f}%"
        )
        wf = row["expanding_walk_forward"]
        lines.append(
            f"  expanding WF delta={wf['delta_pp']:+.4f} pp, "
            f"bootstrap P(delta>0)={row['month_block_bootstrap']['probability_delta_positive']:.3f}, "
            f"CI={row['month_block_bootstrap']['delta_ci95_pp']}"
        )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    report = build_report()
    if args.verify_controls:
        verify_controls(report)
    print(pretty_summary(report) if args.pretty else json.dumps(
        report, ensure_ascii=False, sort_keys=True, indent=2
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
