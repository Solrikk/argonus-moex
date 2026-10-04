#!/usr/bin/env python3
"""Reproduce fixed-150k sizing on the frozen 61- and reconstructed 54-trade books.

This is a research-only utility.  It never imports the broker client and cannot
place, replace, or cancel an order.  The 54-trade RS5 book is rebuilt from the
immutable 61-trade snapshot by applying the seven historical vetoes and the two
published Top-3 replacements.  Every 5-minute execution path is hash checked.

The live-sizing reconstruction starts with 50,000 RUB and uses before each
trade::

    position = min(150,000 RUB, 3 * current_equity)

PnL is the exact frozen gross trade return less the requested round-trip cost,
multiplied by that position.  This intentionally models neither lot rounding
nor a tighter per-instrument broker margin limit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from argonus.backtesting import backtest_live_61_trades as frozen


from argonus.paths import PROJECT_ROOT as ROOT
SNAPSHOT = ROOT / "data/backtests/live_rr2_t4_candidates_2025-11_2026-07.json"
FIVE_MIN_DIR = ROOT / "data/intraday_universe/five_min"
FALLBACK = ROOT / "data/backtests/five_min_fallback_5_sessions.json"

# Pin the reconstruction code and its two top-level immutable input manifests.
# Individual sessions are additionally pinned inside the snapshot/replacements.
PINNED_FILE_SHA256 = {
    "argonus/backtesting/backtest_live_61_trades.py": "a6fa4fed7e298ea1cd5842b748304472531ddfd4457d333a5a70b4bb48e2bd75",
    "data/backtests/live_rr2_t4_candidates_2025-11_2026-07.json": (
        "996ee71f4a081b631a59c3cdaf3e27db08f02fd12dda54a7bd3389620ed4b870"
    ),
    "data/backtests/five_min_fallback_5_sessions.json": (
        "4779fa4a8adb7c9a7ee899cb29fb9646c39c91e7ae2d303c4dfa1031d62cd74a"
    ),
}

# These rank-1 candidates failed RS5 and had no otherwise-eligible Top-3
# replacement.  The values are deliberately explicit so the 54-trade identity
# does not depend on the current analyzer or mutable model files.
RS5_VETOES = {
    "2025-11-10": "RAGR",
    "2025-12-11": "ASTR",
    "2026-01-06": "CBOM",
    "2026-02-11": "VTBR",
    "2026-03-02": "MAGN",
    "2026-03-03": "MSNG",
    "2026-03-04": "VTBR",
}

# Replacement targets and session hashes come from the archived Top-3 policy
# record.  SessionStore independently recalculates each normalized-session hash.
RS5_REPLACEMENTS: dict[str, dict[str, Any]] = {
    "2026-05-04": {
        "date": "2026-05-04",
        "month": "2026-05",
        "rank": 2,
        "symbol": "RUAL",
        "direction": "short",
        "gate": None,
        "target_label": "T4",
        "target_price": 38.052,
        "target_move_pct": 2.3807080554130278,
        "data_source": frozen.LOCAL_SOURCE,
        "session_sha256": "a650402ff77eee47157e01cad75006975b1f63e42709399538a17c436ecbef0d",
    },
    "2026-07-01": {
        "date": "2026-07-01",
        "month": "2026-07",
        "rank": 3,
        "symbol": "CNRU",
        "direction": "short",
        "gate": None,
        "target_label": "T4",
        "target_price": 483.99,
        "target_move_pct": 3.741050119331746,
        "data_source": frozen.LOCAL_SOURCE,
        "session_sha256": "7cf56af8791262662a9a09ccee2b07fd8e6e01280ce7f0815911568ecc2f514b",
    },
}

EXPECTED_CONTROLS = {
    "published_rs5_long_0_5_short_1_return_pct": 50.09557506271673,
    "fixed_150k_on_50k_61_return_pct": 94.10756142840174,
    "fixed_150k_on_50k_61_final_equity": 97_053.78071420087,
    "fixed_150k_on_50k_54_return_pct": 124.25481149246642,
    "fixed_150k_on_50k_54_final_equity": 112_127.40574623321,
}


@dataclass(frozen=True, slots=True)
class ResearchTrade:
    date: str
    month: str
    symbol: str
    direction: str
    gross_return_pct: float
    exit_reason: str
    data_source: str
    session_sha256: str


@dataclass(slots=True)
class SizedTrade:
    date: str
    month: str
    symbol: str
    direction: str
    gross_return_pct: float
    cost_pct: float
    net_return_on_notional_pct: float
    equity_before: float
    position_notional: float
    leverage_on_equity: float
    pnl_rub: float
    equity_after: float
    drawdown_pct: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Research-only fixed-150k reconstruction for 61/54 frozen trades."
    )
    parser.add_argument("--start-equity", type=float, default=50_000.0)
    parser.add_argument("--position-cap", type=float, default=150_000.0)
    parser.add_argument("--max-leverage", type=float, default=3.0)
    parser.add_argument("--cost-pct", type=float, default=0.08)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--verify-controls",
        action="store_true",
        help="Require exact frozen controls; valid only with default sizing/costs.",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_pinned_files() -> dict[str, str]:
    actual: dict[str, str] = {}
    for relative, expected in PINNED_FILE_SHA256.items():
        path = ROOT / relative
        value = sha256_file(path)
        if value != expected:
            raise RuntimeError(
                f"Pinned input changed: {relative}; expected {expected}, got {value}."
            )
        actual[relative] = value
    return actual


def to_research_trade(value: dict[str, Any]) -> ResearchTrade:
    return ResearchTrade(
        date=str(value["date"]),
        month=str(value["month"]),
        symbol=str(value["symbol"]),
        direction=str(value["direction"]),
        gross_return_pct=float(value["gross_return_pct"]),
        exit_reason=str(value["exit_reason"]),
        data_source=str(value["data_source"]),
        session_sha256=str(value["session_sha256"]),
    )


def load_ledgers() -> tuple[list[ResearchTrade], list[ResearchTrade], dict[str, str]]:
    pinned = verify_pinned_files()
    snapshot = frozen.load_snapshot(SNAPSHOT)
    model = snapshot.get("execution_model") or {}
    store = frozen.SessionStore(
        five_min_dir=FIVE_MIN_DIR,
        fallback_path=FALLBACK,
        pinned_start=str(model.get("entry_time_msk", "07:00")),
        pinned_end=str(model.get("time_exit_msk", "18:35")),
        check_hashes=True,
    )
    full_risk = frozen.build_report(
        snapshot,
        store,
        frozen.BacktestConfig(long_risk=1.0, short_risk=1.0),
    )
    ledger_61 = [to_research_trade(value) for value in full_risk["trades"]]

    by_date = {trade.date: trade for trade in ledger_61}
    for trade_date, expected_symbol in RS5_VETOES.items():
        actual = by_date.get(trade_date)
        if actual is None or actual.symbol != expected_symbol:
            raise RuntimeError(
                f"RS5 veto identity mismatch on {trade_date}: "
                f"expected {expected_symbol}, got {getattr(actual, 'symbol', None)}."
            )

    replacement_results: dict[str, ResearchTrade] = {}
    replacement_config = frozen.BacktestConfig(long_risk=1.0, short_risk=1.0)
    for trade_date, candidate in sorted(RS5_REPLACEMENTS.items()):
        candles, source, digest = store.load(candidate)
        result = frozen.simulate_trade(
            candidate,
            candles,
            source,
            digest,
            replacement_config,
        )
        replacement_results[trade_date] = to_research_trade(asdict(result))

    ledger_54: list[ResearchTrade] = []
    for trade in ledger_61:
        if trade.date in RS5_VETOES:
            continue
        ledger_54.append(replacement_results.get(trade.date, trade))
    if len(ledger_61) != 61 or len(ledger_54) != 54:
        raise RuntimeError(
            f"Ledger count mismatch: control={len(ledger_61)}, RS5={len(ledger_54)}."
        )
    if [trade.date for trade in ledger_54] != sorted(trade.date for trade in ledger_54):
        raise RuntimeError("RS5 ledger is not strictly chronological.")
    return ledger_61, ledger_54, pinned


def compound(values: Iterable[float]) -> float:
    return (math.prod(1.0 + value / 100.0 for value in values) - 1.0) * 100.0


def published_rs5_control(ledger: list[ResearchTrade], cost_pct: float) -> float:
    values = [
        (trade.gross_return_pct - cost_pct)
        * (0.5 if trade.direction == "long" else 1.0)
        for trade in ledger
    ]
    return compound(values)


def simulate_fixed_notional(
    ledger: list[ResearchTrade],
    *,
    start_equity: float,
    position_cap: float,
    max_leverage: float,
    cost_pct: float,
) -> dict[str, Any]:
    if min(start_equity, position_cap, max_leverage) <= 0.0:
        raise RuntimeError("start equity, position cap, and leverage must be positive.")
    if cost_pct < 0.0:
        raise RuntimeError("round-trip cost cannot be negative.")

    equity = start_equity
    peak = equity
    peak_date = "start"
    max_drawdown = 0.0
    max_drawdown_peak_date = "start"
    trough_date = "start"
    sized: list[SizedTrade] = []
    for trade in ledger:
        position = min(position_cap, max_leverage * equity)
        net_return = trade.gross_return_pct - cost_pct
        pnl = position * net_return / 100.0
        before = equity
        equity += pnl
        if equity <= 0.0:
            raise RuntimeError(f"Account ruined on {trade.date} {trade.symbol}.")
        if equity > peak:
            peak = equity
            peak_date = trade.date
        drawdown = (equity / peak - 1.0) * 100.0
        if drawdown < max_drawdown:
            max_drawdown = drawdown
            max_drawdown_peak_date = peak_date
            trough_date = trade.date
        sized.append(
            SizedTrade(
                date=trade.date,
                month=trade.month,
                symbol=trade.symbol,
                direction=trade.direction,
                gross_return_pct=trade.gross_return_pct,
                cost_pct=cost_pct,
                net_return_on_notional_pct=net_return,
                equity_before=before,
                position_notional=position,
                leverage_on_equity=position / before,
                pnl_rub=pnl,
                equity_after=equity,
                drawdown_pct=drawdown,
            )
        )

    by_month: dict[str, list[SizedTrade]] = defaultdict(list)
    for trade in sized:
        by_month[trade.month].append(trade)
    monthly: list[dict[str, Any]] = []
    for month, trades in sorted(by_month.items()):
        start = trades[0].equity_before
        end = trades[-1].equity_after
        monthly.append(
            {
                "month": month,
                "trades": len(trades),
                "wins": sum(trade.net_return_on_notional_pct > 0.0 for trade in trades),
                "losses": sum(trade.net_return_on_notional_pct <= 0.0 for trade in trades),
                "return_on_month_start_equity_pct": (end / start - 1.0) * 100.0,
                "equity_after": end,
            }
        )
    worst_month = min(monthly, key=lambda row: row["return_on_month_start_equity_pct"])
    return {
        "summary": {
            "trades": len(sized),
            "wins": sum(trade.net_return_on_notional_pct > 0.0 for trade in sized),
            "losses": sum(trade.net_return_on_notional_pct <= 0.0 for trade in sized),
            "start_equity": start_equity,
            "final_equity": equity,
            "total_return_on_start_equity_pct": (equity / start_equity - 1.0) * 100.0,
            "max_trade_close_drawdown_pct": max_drawdown,
            "max_drawdown_peak_date": max_drawdown_peak_date,
            "max_drawdown_trough_date": trough_date,
            "worst_month": worst_month["month"],
            "worst_month_return_pct": worst_month["return_on_month_start_equity_pct"],
            "minimum_position_notional": min(trade.position_notional for trade in sized),
            "maximum_position_notional": max(trade.position_notional for trade in sized),
        },
        "monthly": monthly,
        "trades": [asdict(trade) for trade in sized],
    }


def close(actual: float, expected: float) -> bool:
    return math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-9)


def verify_controls(report: dict[str, Any], defaults: bool) -> list[str]:
    if not defaults:
        raise RuntimeError("--verify-controls requires default sizing and cost arguments.")
    actual = {
        "published_rs5_long_0_5_short_1_return_pct": report["published_table_control"][
            "total_return_pct"
        ],
        "fixed_150k_on_50k_61_return_pct": report["ledger_61"]["summary"][
            "total_return_on_start_equity_pct"
        ],
        "fixed_150k_on_50k_61_final_equity": report["ledger_61"]["summary"][
            "final_equity"
        ],
        "fixed_150k_on_50k_54_return_pct": report["ledger_54_rs5"]["summary"][
            "total_return_on_start_equity_pct"
        ],
        "fixed_150k_on_50k_54_final_equity": report["ledger_54_rs5"]["summary"][
            "final_equity"
        ],
    }
    lines: list[str] = []
    for name, expected in EXPECTED_CONTROLS.items():
        value = float(actual[name])
        if not close(value, expected):
            raise RuntimeError(f"Control {name} mismatch: expected {expected}, got {value}.")
        lines.append(f"{name}={value:.12f}")
    return lines


def build_research_report(args: argparse.Namespace) -> dict[str, Any]:
    ledger_61, ledger_54, pinned = load_ledgers()
    config = {
        "start_equity": args.start_equity,
        "position_cap": args.position_cap,
        "max_leverage": args.max_leverage,
        "round_trip_cost_pct": args.cost_pct,
        "position_formula": "min(position_cap, max_leverage * current_equity)",
    }
    shared = {
        "start_equity": args.start_equity,
        "position_cap": args.position_cap,
        "max_leverage": args.max_leverage,
        "cost_pct": args.cost_pct,
    }
    return {
        "schema_version": 1,
        "scope": "research_only_no_broker_calls",
        "pinned_inputs": pinned,
        "config": config,
        "rs5_reconstruction": {
            "vetoes": RS5_VETOES,
            "replacements": RS5_REPLACEMENTS,
            "warning": (
                "The 54-trade identity is reconstructed from the frozen 61-trade book and "
                "the published RS5 divergences; it was not stored as a standalone original ledger."
            ),
        },
        "published_table_control": {
            "formula": "compound((gross-cost)*(0.5 long, 1.0 short)) on 150k",
            "total_return_pct": published_rs5_control(ledger_54, args.cost_pct),
        },
        "ledger_61": simulate_fixed_notional(ledger_61, **shared),
        "ledger_54_rs5": simulate_fixed_notional(ledger_54, **shared),
        "limitations": [
            "Trade-close drawdown only; intraday path drawdown is not measured.",
            "No lot rounding, broker-specific margin cap, slippage, gap loss, tax, or margin funding.",
            "The 0.08% default is the frozen round-trip commission assumption.",
            "The candidate/selector research overlaps the evaluated period, so this is not OOS.",
        ],
    }


def signed(value: float) -> str:
    return f"{value:+.3f}%"


def print_report(report: dict[str, Any], controls: list[str] | None) -> None:
    config = report["config"]
    print(
        "Research-only sizing: "
        f"equity={config['start_equity']:.0f}, cap={config['position_cap']:.0f}, "
        f"leverage<={config['max_leverage']:g}x, cost={config['round_trip_cost_pct']:g}%"
    )
    print(
        "Published 54-trade table control: "
        + signed(report["published_table_control"]["total_return_pct"])
    )
    for label, key in (("Frozen 61", "ledger_61"), ("RS5 54", "ledger_54_rs5")):
        block = report[key]
        summary = block["summary"]
        print()
        print(
            f"{label}: {summary['trades']} trades, "
            f"{summary['wins']}/{summary['losses']}, "
            f"return {signed(summary['total_return_on_start_equity_pct'])}, "
            f"equity {summary['final_equity']:.2f}, "
            f"MDD {signed(summary['max_trade_close_drawdown_pct'])}, "
            f"worst {summary['worst_month']} "
            f"{signed(summary['worst_month_return_pct'])}"
        )
        print("month     trades   W/L       return       equity")
        for row in block["monthly"]:
            print(
                f"{row['month']}  {row['trades']:>3}   "
                f"{row['wins']:>2}/{row['losses']:<2}  "
                f"{signed(row['return_on_month_start_equity_pct']):>10}  "
                f"{row['equity_after']:>11.2f}"
            )
    if controls:
        print()
        print("Controls: PASS")
        for line in controls:
            print("  " + line)


def main() -> int:
    args = parse_args()
    report = build_research_report(args)
    defaults = (
        close(args.start_equity, 50_000.0)
        and close(args.position_cap, 150_000.0)
        and close(args.max_leverage, 3.0)
        and close(args.cost_pct, 0.08)
    )
    controls = verify_controls(report, defaults) if args.verify_controls else None
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if controls:
            print("Controls: PASS", file=sys.stderr)
    else:
        print_report(report, controls)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2)
