#!/usr/bin/env python3
"""Ablate the Engine-A selector stack on archived generated watchlists.

This is a diagnostic-only, read-only utility.  It rebuilds the six analyses for
each historical watchlist using strictly prior-day daily candles, applies a
small set of selector-stack variants, and resolves the selected leader on the
same 5-minute execution path as the frozen Engine-A book.  Because the current
analyzer and model files can drift after a watchlist was archived, the report
also states how many frozen leaders the reconstruction actually matches.

It deliberately does not train or write a live model.  The archived model
files overlap the dates below and the local index fixture contains closes only,
so the later calendar slice is *not* an untouched OOS exam.  The output is
useful only as a same-cache sensitivity diagnostic.
"""
from __future__ import annotations

import argparse
import copy
import gzip
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

from argonus.watchlists import watchlist_best_target as wbt
from argonus.backtesting.backtest_generated_watchlists import discover_watchlists


from argonus.paths import PROJECT_ROOT as ROOT
FIVE_MIN_DIR = ROOT / "data/intraday_universe" / "five_min"
DAILY_DIR = ROOT / "data/intraday_universe" / "dailies"
INDEX_DAILY = ROOT / "data/intraday_universe" / "index_daily.json"
FALLBACK = ROOT / "data/backtests" / "five_min_fallback_5_sessions.json"
FROZEN = ROOT / "data/backtests" / "live_rr2_t4_candidates_2025-11_2026-07.json"


@dataclass(frozen=True, slots=True)
class Pick:
    trade_date: str
    symbol: str
    direction: str
    return_pct: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="A/B selector stack on exact 5-minute Engine-A execution."
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--fee-pct", type=float, default=0.08)
    parser.add_argument("--long-risk", type=float, default=0.5)
    parser.add_argument("--short-risk", type=float, default=1.0)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def _daily_sessions(path: Path) -> list[wbt.SessionData]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [
        wbt.SessionData(
            trade_date=date.fromisoformat(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]) if len(row) > 5 else 0.0,
        )
        for row in rows
    ]


def patch_prior_day_data(root: Path, symbols: set[str]) -> None:
    dailies = {
        symbol: _daily_sessions(root / "data/intraday_universe" / "dailies" / f"{symbol}.json")
        for symbol in sorted(symbols)
    }
    index_rows = json.loads(
        (root / "data/intraday_universe" / "index_daily.json").read_text(encoding="utf-8")
    )
    index = [
        wbt.SessionData(
            trade_date=date.fromisoformat(row[0]),
            open=float(row[1]),
            high=float(row[1]),
            low=float(row[1]),
            close=float(row[1]),
        )
        for row in index_rows
    ]

    def before(rows: list[wbt.SessionData], as_of: date, limit: int) -> list[wbt.SessionData]:
        result = [row for row in rows if row.trade_date < as_of]
        if not result:
            raise wbt.MarketDataError(f"No history before {as_of.isoformat()}")
        return result[-limit:]

    def stock(symbol: str, as_of: date, limit: int = 180, **_: object) -> list[wbt.SessionData]:
        return before(dailies[symbol], as_of, limit)

    def market(symbol: str, as_of: date, limit: int = 40, **_: object) -> list[wbt.SessionData]:
        del symbol
        return before(index, as_of, limit)

    wbt.fetch_stock_sessions = stock
    wbt.fetch_recent_stock_sessions = stock
    wbt.fetch_index_sessions = market
    wbt.fetch_previous_index_sessions = market


class IntradayStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.cache: dict[str, dict[str, list[list[object]]]] = {}
        payload = json.loads(
            (root / "data/backtests" / "five_min_fallback_5_sessions.json").read_text(
                encoding="utf-8"
            )
        )
        # Schema v1 is keyed as ``YYYY-MM-DD|SYMBOL``; tolerate a future list
        # of explicit records as well.
        self.fallback: dict[tuple[str, str], list[list[object]]] = {}
        rows = payload if isinstance(payload, list) else payload.get("sessions", {})
        if isinstance(rows, dict):
            for key, candles in rows.items():
                trade_date, symbol = str(key).split("|", 1)
                self.fallback[(trade_date, symbol)] = candles
        else:
            for row in rows:
                if isinstance(row, dict):
                    self.fallback[(str(row["date"]), str(row["symbol"]))] = row["candles"]

    def session(self, symbol: str, trade_date: str) -> list[list[object]] | None:
        if symbol not in self.cache:
            path = self.root / "data/intraday_universe" / "five_min" / f"{symbol}.json.gz"
            if path.exists():
                with gzip.open(path, "rt", encoding="utf-8") as handle:
                    self.cache[symbol] = json.load(handle)
            else:
                self.cache[symbol] = {}
        return self.cache[symbol].get(trade_date) or self.fallback.get((trade_date, symbol))


def exact_return(
    candles: list[list[object]],
    direction: str,
    target: float,
    fee_pct: float,
    risk: float,
) -> float | None:
    session = [row for row in candles if "07:00" <= str(row[0]) <= "18:35"]
    if not session:
        return None
    entry = float(session[0][1])
    if entry <= 0.0:
        return None
    sign = 1.0 if direction == "long" else -1.0
    if sign * (target / entry - 1.0) <= 0.0:
        return None
    stop = entry * (1.0 - sign * 0.01)
    last_close = entry
    gross = 0.0
    for row in session:
        high, low, close = float(row[2]), float(row[3]), float(row[4])
        target_hit = high >= target if direction == "long" else low <= target
        stop_hit = low <= stop if direction == "long" else high >= stop
        if stop_hit:  # conservative stop-first if both occur in one candle
            gross = -1.0
            break
        if target_hit:
            gross = sign * (target / entry - 1.0) * 100.0
            break
        last_close = close
    else:
        gross = sign * (last_close / entry - 1.0) * 100.0
    return (gross - fee_pct) * risk


def gate_reason(item: wbt.IdeaAnalysis, as_of: date) -> str | None:
    if item.skipped_reason:
        return "analysis"
    if item.direction == "short" and item.market_bullish:
        return "regime"
    if item.direction == "long" and not item.market_bullish:
        return "regime"
    if item.direction == "short":
        sessions = wbt.fetch_stock_sessions(item.symbol, as_of)
        closes = [row.close for row in sessions if row.trade_date < as_of]
        if len(closes) >= 11 and (closes[-1] / closes[-11] - 1.0) * 100.0 > 8.0:
            return "rally"
    if item.exit_target is None:
        return "target"
    if item.exit_target.move_pct_from_close < 2.0:
        return "rr"
    return None


def current(items: list[wbt.IdeaAnalysis]) -> list[wbt.IdeaAnalysis]:
    items = wbt.apply_learned_same_day_top_reranker(items)
    items = wbt.apply_learned_same_day_winner_reranker(items)
    return wbt.apply_volatility_expansion_tilt(items)


def without_any_hit(items: list[wbt.IdeaAnalysis]) -> list[wbt.IdeaAnalysis]:
    items = wbt.apply_learned_same_day_winner_reranker(items)
    return wbt.apply_volatility_expansion_tilt(items)


def without_vol_tilt(items: list[wbt.IdeaAnalysis]) -> list[wbt.IdeaAnalysis]:
    items = wbt.apply_learned_same_day_top_reranker(items)
    return wbt.apply_learned_same_day_winner_reranker(items)


def winner_only(items: list[wbt.IdeaAnalysis]) -> list[wbt.IdeaAnalysis]:
    return wbt.apply_learned_same_day_winner_reranker(items)


def base_with_tilt(items: list[wbt.IdeaAnalysis]) -> list[wbt.IdeaAnalysis]:
    return wbt.apply_volatility_expansion_tilt(items)


def base_score(items: list[wbt.IdeaAnalysis]) -> list[wbt.IdeaAnalysis]:
    return sorted(items, key=lambda item: item.overall_score, reverse=True)


POLICIES: dict[str, Callable[[list[wbt.IdeaAnalysis]], list[wbt.IdeaAnalysis]]] = {
    "current": current,
    "without_in_sample_any_hit": without_any_hit,
    "without_volatility_tilt": without_vol_tilt,
    "winner_only": winner_only,
    "base_score_plus_tilt": base_with_tilt,
    "base_score": base_score,
}


def summarize(picks: list[Pick]) -> dict[str, object]:
    capital = 1.0
    peak = 1.0
    max_drawdown = 0.0
    monthly: dict[str, list[float]] = defaultdict(list)
    for pick in sorted(picks, key=lambda item: item.trade_date):
        capital *= 1.0 + pick.return_pct / 100.0
        peak = max(peak, capital)
        max_drawdown = min(max_drawdown, capital / peak - 1.0)
        monthly[pick.trade_date[:7]].append(pick.return_pct)
    month_result = {
        month: (math.prod(1.0 + value / 100.0 for value in values) - 1.0) * 100.0
        for month, values in sorted(monthly.items())
    }
    return {
        "trades": len(picks),
        "wins": sum(pick.return_pct > 0.0 for pick in picks),
        "losses": sum(pick.return_pct <= 0.0 for pick in picks),
        "return_pct": (capital - 1.0) * 100.0,
        "max_drawdown_pct": max_drawdown * 100.0,
        "monthly": month_result,
    }


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    watchlists = discover_watchlists(str(root / "data/watchlists"))
    symbols = {symbol for entry in watchlists for symbol in entry.symbols}
    patch_prior_day_data(root, symbols)
    store = IntradayStore(root)

    # Capture the causal analyses before any outcome-trained top override.
    original_override = wbt.apply_same_day_top_override
    wbt.apply_same_day_top_override = lambda items: items
    try:
        raw_by_date = {
            entry.trade_date: wbt.analyze_watchlist(entry.raw_text, entry.trade_date)
            for entry in watchlists
        }
    finally:
        wbt.apply_same_day_top_override = original_override

    picks: dict[str, list[Pick]] = {name: [] for name in POLICIES}
    leaders: dict[str, dict[str, str]] = {name: {} for name in POLICIES}
    for trade_date, raw in raw_by_date.items():
        ds = trade_date.isoformat()
        for name, policy in POLICIES.items():
            ranked = policy(copy.deepcopy(raw))
            if not ranked:
                continue
            leader = ranked[0]
            leaders[name][ds] = leader.symbol
            if gate_reason(leader, trade_date) is not None or leader.exit_target is None:
                continue
            candles = store.session(leader.symbol, ds)
            if not candles:
                continue
            risk = args.long_risk if leader.direction == "long" else args.short_risk
            result = exact_return(
                candles,
                leader.direction,
                float(leader.exit_target.price),
                args.fee_pct,
                risk,
            )
            if result is not None:
                picks[name].append(Pick(ds, leader.symbol, leader.direction, result))

    report: dict[str, object] = {
        "config": {
            "fee_pct": args.fee_pct,
            "long_risk": args.long_risk,
            "short_risk": args.short_risk,
            "stop_loss_pct": 1.0,
            "minimum_target_pct_from_previous_close": 2.0,
        },
        "data_limitations": [
            "Archived reranker training/tuning overlaps the evaluated calendar window.",
            "index_daily.json contains close-only rows; index OHLC is reconstructed as close.",
            "The later calendar slice is not an untouched out-of-sample exam.",
        ],
        "policies": {},
    }
    current_leaders = leaders["current"]
    frozen_payload = json.loads(
        (root / "data/backtests" / "live_rr2_t4_candidates_2025-11_2026-07.json").read_text(
            encoding="utf-8"
        )
    )
    frozen_symbols = {
        str(row["date"]): str(row["symbol"]) for row in frozen_payload["candidates"]
    }
    matching_frozen_leaders = sum(
        current_leaders.get(trade_date) == symbol
        for trade_date, symbol in frozen_symbols.items()
    )
    report["reconstruction"] = {
        "frozen_trade_dates": len(frozen_symbols),
        "matching_current_leaders": matching_frozen_leaders,
        "warning": (
            "Current-code reconstruction is not the frozen 61-trade control; "
            "use only same-cache relative ablations unless every leader matches."
        ),
    }
    for name in POLICIES:
        fit = [pick for pick in picks[name] if pick.trade_date < "2026-04-01"]
        exam = [pick for pick in picks[name] if pick.trade_date >= "2026-04-01"]
        report["policies"][name] = {
            "all": summarize(picks[name]),
            "earlier_2025_11_to_2026_03": summarize(fit),
            "later_not_oos_2026_04_to_2026_07_16": summarize(exam),
            "leader_changes_vs_current": sum(
                symbol != current_leaders.get(ds) for ds, symbol in leaders[name].items()
            ),
        }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        reconstruction = report["reconstruction"]
        print(
            "WARNING: current-code reconstruction matches "
            f"{reconstruction['matching_current_leaders']}/"
            f"{reconstruction['frozen_trade_dates']} frozen leaders; "
            "compare variants only on this shared reconstruction."
        )
        for name, row in report["policies"].items():
            all_result = row["all"]
            fit = row["earlier_2025_11_to_2026_03"]
            exam = row["later_not_oos_2026_04_to_2026_07_16"]
            print(
                f"{name:29} n={all_result['trades']:3d} "
                f"all={all_result['return_pct']:+7.2f}% DD={all_result['max_drawdown_pct']:6.2f}% "
                f"earlier={fit['return_pct']:+7.2f}% later(not-OOS)={exam['return_pct']:+7.2f}% "
                f"changes={row['leader_changes_vs_current']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
