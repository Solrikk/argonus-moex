#!/usr/bin/env python3
"""Research-only reconstruction of current Engine A + RS5 for October 2025.

The first run uses full prior-day daily OHLC from T-Invest to run the current
analyzer, applies the explicitly pinned production gates, and downloads only
the 5-minute sessions actually selected by the policy.  It publishes one
immutable cache.  Later runs read that cache and never construct an API client.

This is deliberately not production code and cannot place or cancel orders.
The reconstruction is in-sample/current-version: current reranker artifacts
were trained with October 2025, and the watchlists were rebuilt from the
current instrument universe/status snapshot.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from argonus.backtesting import backtest_generated_watchlists as daily_bt
from argonus.backtesting import backtest_live_61_trades as exact_bt
from argonus.research import research_fixed_150k_sizing as fixed_research
from argonus.watchlists import shadow_rs5_selector as rs5
from argonus.market_data.tbank_market_data import (
    MOSCOW_TZ,
    TBankApiError,
    TBankInvestClient,
    _get_field,
    parse_api_datetime,
    quotation_to_float,
    to_api_datetime_range,
)
from argonus.watchlists import watchlist_best_target as wbt


from argonus.paths import PROJECT_ROOT as ROOT
MONTH_DIR = ROOT / "data/watchlists/generated_watchlists_2025_10"
CACHE_PATH = ROOT / "data/backtests/october_engine_a_rs5_selected_5m_cache.json"
REPORT_JSON = ROOT / "data/backtests/OCTOBER_ENGINE_A_RS5_RESEARCH_2025_10.json"
REPORT_TEXT = ROOT / "data/backtests/OCTOBER_ENGINE_A_RS5_RESEARCH_2025_10.md"

EXPECTED_DATES = tuple(
    f"2025-10-{day:02d}"
    for day in (1, 2, 3, 6, 7, 8, 9, 10, 13, 14, 15, 16, 17, 20, 21, 22, 23, 24, 27, 28, 29, 30, 31)
)
PINNED_COMBINED_WATCHLIST_HASH = "cd0e08f970ba13c9e008e14b19014d2d9587b0226453fbc531383097ba7f321f"

ENTRY_TIME = "07:00"
EXIT_TIME = "18:35"
STOP_LOSS_PCT = 1.0
ROUND_TRIP_COST_PCT = 0.08
REGIME_GATES = True
SHORT_RALLY_GUARD_PCT = 8.0
MIN_TARGET_PCT = 2.0
RS5_THRESHOLD_PP = -2.39
RS5_TOP_K = 3
START_EQUITY_RUB = 50_000.0
TARGET_POSITION_RUB = 150_000.0
MAX_LEVERAGE = 3.0
RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0, 16.0)

PINNED_ARTIFACT_NAMES = (
    "argonus/watchlists/watchlist_best_target.py",
    "argonus/watchlists/shadow_rs5_selector.py",
    "argonus/backtesting/backtest_live_61_trades.py",
    "models/same_day_top_reranker_model.json",
    "models/same_day_top_winner_reranker_model.json",
    "models/same_day_top_t2plus_reranker_model.json",
    "models/runner_day_confidence_model.json",
)


@dataclass(frozen=True, slots=True)
class Entry:
    trade_date: date
    path: Path
    text: str
    symbols: tuple[str, ...]


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_hashes() -> dict[str, str | None]:
    return {
        name: file_sha256(ROOT / name) if (ROOT / name).is_file() else None
        for name in PINNED_ARTIFACT_NAMES
    }


def discover_entries(month_dir: Path) -> tuple[list[Entry], list[dict[str, Any]]]:
    if month_dir.resolve().name != "generated_watchlists_2025_10":
        raise RuntimeError("Этот research pipeline принимает только generated_watchlists_2025_10.")
    entries: list[Entry] = []
    manifest: list[dict[str, Any]] = []
    found_dates: list[str] = []
    for trade_date_text in EXPECTED_DATES:
        trade_date = date.fromisoformat(trade_date_text)
        path = month_dir / f"watchlist_10{trade_date.day:02d}.txt"
        if not path.is_file():
            raise RuntimeError(f"Нет ожидаемого watchlist: {path}")
        text = path.read_text(encoding="utf-8")
        symbols = tuple(idea.symbol for idea in wbt.parse_watchlist(text))
        if not symbols:
            raise RuntimeError(f"Пустой watchlist: {path}")
        digest = file_sha256(path)
        entries.append(Entry(trade_date, path, text, symbols))
        manifest.append({"date": trade_date_text, "name": path.name, "sha256": digest, "bytes": len(text.encode('utf-8'))})
        found_dates.append(trade_date_text)
    extras = sorted(
        path.name for path in month_dir.glob("watchlist_*.txt")
        if path.name not in {entry.path.name for entry in entries}
    )
    if extras:
        raise RuntimeError(f"Неожиданные watchlist-файлы в октябрьской папке: {', '.join(extras)}")
    if tuple(found_dates) != EXPECTED_DATES:
        raise RuntimeError("Набор октябрьских торговых дат не совпадает с pinned calendar.")
    # Compatibility control produced by the generation task: files are sorted
    # by path first, then the complete sha256sum-style lines are hashed.
    combined = hashlib.sha256(
        "".join(
            f"{row['sha256']}  generated_watchlists_2025_10/{row['name']}\n"
            for row in manifest
        ).encode("utf-8")
    ).hexdigest()
    if combined != PINNED_COMBINED_WATCHLIST_HASH:
        raise RuntimeError(
            "October watchlists changed: "
            f"expected {PINNED_COMBINED_WATCHLIST_HASH}, got {combined}."
        )
    return entries, manifest


def gate_reason(item: Any) -> str | None:
    """Exact named legacy gates; runner day filter is intentionally disabled."""
    if getattr(item, "skipped_reason", None):
        return f"analysis: {item.skipped_reason}"
    direction = getattr(item, "direction", None)
    if direction not in {"long", "short"}:
        return f"invalid direction: {direction}"
    if REGIME_GATES:
        bullish = bool(getattr(item, "market_bullish", False))
        if direction == "short" and bullish:
            return "regime: short while IMOEX is above EMA20"
        if direction == "long" and not bullish:
            return "regime: long while IMOEX is below EMA20"
    rally = getattr(item, "short_rally_10d_pct", None)
    if direction == "short" and rally is not None and float(rally) > SHORT_RALLY_GUARD_PCT:
        return f"rally_guard: {float(rally):.6f}% > {SHORT_RALLY_GUARD_PCT:.2f}%"
    target = getattr(item, "exit_target", None)
    if target is None:
        return "missing T4 exit target"
    move = float(getattr(target, "move_pct_from_close", 0.0))
    if move < MIN_TARGET_PCT:
        return f"rr_floor: {move:.6f}% < {MIN_TARGET_PCT:.2f}%"
    return None


def _rs5_precomputed(item: Any) -> dict[str, Any] | None:
    values = {
        "stock_return_5d_pct": getattr(item, "rs5_stock_return_5d_pct", None),
        "index_return_5d_pct": getattr(item, "rs5_index_return_5d_pct", None),
        "aligned_rs5_pp": getattr(item, "directional_rs5_pp", None),
        "stock_window": getattr(item, "rs5_stock_window", None),
        "index_window": getattr(item, "rs5_index_window", None),
    }
    if any(value is None for value in values.values()):
        return None
    values["stock_window"] = list(values["stock_window"])
    values["index_window"] = list(values["index_window"])
    return values


def select_days(entries: list[Entry], workers: int) -> tuple[list[dict[str, Any]], dict[str, list[Any]]]:
    bridge = [
        daily_bt.WatchlistEntry(entry.trade_date, str(entry.path), entry.text, entry.symbols)
        for entry in entries
    ]
    stock_cache, index_candles = daily_bt.build_candle_cache(workers, bridge)
    daily_bt.patch_watchlist_data_access(stock_cache, index_candles)
    wbt.load_same_day_top_reranker_model()
    wbt.load_same_day_top_winner_reranker_model()
    wbt.load_same_day_top_t2plus_reranker_model()

    days: list[dict[str, Any]] = []
    for entry in entries:
        analyses = wbt.analyze_watchlist(entry.text, entry.trade_date)
        top3 = analyses[:RS5_TOP_K]
        if not top3:
            days.append({"date": entry.trade_date.isoformat(), "decision": "skip", "decision_reason": "empty analysis", "selected": None, "candidates": []})
            continue
        eligibility = {rank: gate_reason(item) for rank, item in enumerate(top3, start=1)}
        precomputed = {
            rank: metrics
            for rank, item in enumerate(top3, start=1)
            if (metrics := _rs5_precomputed(item)) is not None
        }
        policy = rs5.evaluate_rs5_policy(
            top3,
            entry.trade_date,
            {},
            (),
            eligibility,
            threshold_pp=RS5_THRESHOLD_PP,
            top_k=RS5_TOP_K,
            precomputed_metrics_by_rank=precomputed,
        )
        decision = str(policy["decision"])
        selected_rank: int | None = None
        skip_reason: str | None = None
        if eligibility.get(1) is not None:
            skip_reason = eligibility[1]
        elif decision == "skip":
            skip_reason = str(policy["decision_reason"])
        elif decision in {"keep", "replacement"}:
            selected_rank = int(policy["recommended_rank"])
        elif decision == "insufficient_data_fail_open":
            selected_rank = 1
        else:
            skip_reason = f"unhandled policy decision: {decision}"

        selected: dict[str, Any] | None = None
        if selected_rank is not None:
            item = top3[selected_rank - 1]
            target = item.exit_target
            has_trade_day_daily = any(
                candle.trade_date == entry.trade_date
                for candle in stock_cache.get(item.symbol, [])
            )
            if not has_trade_day_daily:
                skip_reason = "no trade-day daily candle"
                selected_rank = None
            else:
                selected = {
                    "rank": selected_rank,
                    "symbol": item.symbol,
                    "direction": item.direction,
                    "target_label": target.label,
                    "target_price": float(target.price),
                    "target_move_pct": float(target.move_pct_from_close),
                    "overall_score": float(item.overall_score),
                    "aligned_rs5_pp": getattr(item, "directional_rs5_pp", None),
                }
        days.append(
            {
                "date": entry.trade_date.isoformat(),
                "watchlist_sha256": file_sha256(entry.path),
                "decision": "skip" if selected is None else ("replacement" if selected["rank"] > 1 else "keep"),
                "decision_reason": skip_reason or str(policy["decision_reason"]),
                "selected": selected,
                "rs5_policy": policy,
            }
        )
    return days, stock_cache


def fetch_selected_session(client: TBankInvestClient, symbol: str, trade_date: date, pause: float) -> list[list[Any]]:
    instrument_id = client.resolve_share_instrument_id(symbol, wbt.DEFAULT_BOARD)
    start_dt, end_dt = to_api_datetime_range(trade_date, trade_date)
    payload = {
        "instrumentId": instrument_id,
        "from": start_dt,
        "to": end_dt,
        "interval": "CANDLE_INTERVAL_5_MIN",
        "candleSourceType": "CANDLE_SOURCE_EXCHANGE",
    }
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            response = client._post("tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles", payload)
            rows: list[list[Any]] = []
            for item in response.get("candles") or response.get("historicalCandles") or []:
                if item.get("isComplete") is False:
                    continue
                raw_time = _get_field(item, "time")
                if not raw_time:
                    continue
                timestamp = parse_api_datetime(str(raw_time)).astimezone(MOSCOW_TZ)
                if timestamp.date() != trade_date:
                    continue
                clock = timestamp.strftime("%H:%M")
                if ENTRY_TIME <= clock <= EXIT_TIME:
                    rows.append([
                        clock,
                        quotation_to_float(_get_field(item, "open")),
                        quotation_to_float(_get_field(item, "high")),
                        quotation_to_float(_get_field(item, "low")),
                        quotation_to_float(_get_field(item, "close")),
                    ])
            rows.sort(key=lambda row: row[0])
            if len({row[0] for row in rows}) != len(rows):
                raise RuntimeError(f"Duplicate 5m clocks: {trade_date} {symbol}")
            time.sleep(max(pause, 0.0))
            return rows
        except TBankApiError as exc:
            if "HTTP 429" not in str(exc) or attempt >= len(RETRY_DELAYS):
                raise
            time.sleep(RETRY_DELAYS[attempt])
    raise RuntimeError("unreachable")


def _cache_body(entries_manifest: list[dict[str, Any]], days: list[dict[str, Any]], sessions: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_type": "argonus_october_engine_a_rs5_selected_5m_cache",
        "scope": "generated_watchlists_2025_10 only",
        "watchlists": entries_manifest,
        "artifact_hashes_at_selection": artifact_hashes(),
        "config": {
            "day_filter": False,
            "regime_gates": REGIME_GATES,
            "short_rally_guard_pct": SHORT_RALLY_GUARD_PCT,
            "minimum_target_pct": MIN_TARGET_PCT,
            "rs5_threshold_pp": RS5_THRESHOLD_PP,
            "rs5_top_k": RS5_TOP_K,
            "entry_time_msk": ENTRY_TIME,
            "exit_time_msk": EXIT_TIME,
            "stop_loss_pct": STOP_LOSS_PCT,
            "round_trip_cost_pct": ROUND_TRIP_COST_PCT,
            "same_bar_policy": "stop_first",
        },
        "selection_days": days,
        "selected_sessions": sessions,
        "leakage": {
            "classification": "CURRENT-VERSION IN-SAMPLE HISTORICAL RECONSTRUCTION — NOT OOS",
            "reranker_training_includes_october_2025": True,
            "current_universe_and_status_snapshot": True,
            "current_uid_resolution_for_historical_5m": True,
            "prior_day_daily_ohlc_guard": "analyzer receives only session.trade_date < trade_date",
            "warning": "Do not append this result to a forward/OOS equity curve as independent evidence.",
        },
    }


def write_immutable_cache(path: Path, body: dict[str, Any]) -> dict[str, Any]:
    payload = dict(body)
    payload["cache_sha256"] = canonical_sha256(body)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise RuntimeError(f"Immutable cache already exists: {path}") from exc
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return payload


def load_cache(path: Path, current_manifest: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Offline cache not found: {path}") from exc
    digest = payload.pop("cache_sha256", None)
    actual = canonical_sha256(payload)
    payload["cache_sha256"] = digest
    if digest != actual:
        raise RuntimeError(f"Cache hash mismatch: expected {digest}, got {actual}")
    if payload.get("watchlists") != current_manifest:
        raise RuntimeError("Current October watchlists differ from the immutable cache manifest.")
    return payload


def compound(returns_pct: list[float]) -> float:
    return (math.prod(1.0 + value / 100.0 for value in returns_pct) - 1.0) * 100.0


def trade_close_mdd(returns_pct: list[float]) -> float:
    equity = peak = 1.0
    result = 0.0
    for value in returns_pct:
        equity *= 1.0 + value / 100.0
        peak = max(peak, equity)
        result = min(result, equity / peak - 1.0)
    return result * 100.0


def table_return(trade: fixed_research.ResearchTrade) -> float:
    direction_multiplier = 0.5 if trade.direction == "long" else 1.0
    return (trade.gross_return_pct - ROUND_TRIP_COST_PCT) * direction_multiplier


def table_monthly(ledger: list[fixed_research.ResearchTrade]) -> list[dict[str, Any]]:
    grouped: dict[str, list[fixed_research.ResearchTrade]] = {}
    for trade in sorted(ledger, key=lambda value: value.date):
        grouped.setdefault(trade.month, []).append(trade)
    through_capital = 150_000.0
    rows: list[dict[str, Any]] = []
    for month, trades in grouped.items():
        month_return = compound([table_return(trade) for trade in trades])
        through_capital *= 1.0 + month_return / 100.0
        rows.append(
            {
                "month": month,
                "trades": len(trades),
                "wins": sum(
                    trade.gross_return_pct - ROUND_TRIP_COST_PCT > 0 for trade in trades
                ),
                "losses": sum(
                    trade.gross_return_pct - ROUND_TRIP_COST_PCT <= 0 for trade in trades
                ),
                "return_pct": month_return,
                "through_capital": through_capital,
            }
        )
    return rows


def build_cache(entries: list[Entry], manifest: list[dict[str, Any]], path: Path, workers: int, pause: float) -> dict[str, Any]:
    if path.exists():
        return load_cache(path, manifest)
    days, _ = select_days(entries, workers)
    client: TBankInvestClient | None = None
    sessions: dict[str, Any] = {}
    for day in days:
        selected = day.get("selected")
        if not selected:
            continue
        if client is None:
            client = TBankInvestClient(user_agent="october-engine-a-rs5-research/1.0")
        key = f"{day['date']}|{selected['symbol']}"
        rows = fetch_selected_session(client, selected["symbol"], date.fromisoformat(day["date"]), pause)
        sessions[key] = {
            "status": "ok" if rows else "unavailable_no_5m_candles",
            "rows": rows,
            "session_sha256": canonical_sha256(rows),
        }
    selected_keys = {
        f"{day['date']}|{day['selected']['symbol']}"
        for day in days if day.get("selected")
    }
    if set(sessions) != selected_keys:
        raise RuntimeError("5m cache contains a non-selected or misses a selected session.")
    return write_immutable_cache(path, _cache_body(manifest, days, sessions))


def build_report(cache: dict[str, Any]) -> dict[str, Any]:
    trades: list[dict[str, Any]] = []
    skips: list[dict[str, Any]] = []
    config = exact_bt.BacktestConfig(
        entry_time=ENTRY_TIME,
        exit_time=EXIT_TIME,
        stop_loss_pct=STOP_LOSS_PCT,
        fee_pct=ROUND_TRIP_COST_PCT,
        start_capital=START_EQUITY_RUB,
    )
    equity = START_EQUITY_RUB
    peak = equity
    max_drawdown = 0.0
    for day in cache["selection_days"]:
        selected = day.get("selected")
        if not selected:
            skips.append({"date": day["date"], "reason": day["decision_reason"], "stage": "selection"})
            continue
        key = f"{day['date']}|{selected['symbol']}"
        session = cache["selected_sessions"][key]
        rows = session["rows"]
        if not rows:
            skips.append({"date": day["date"], "symbol": selected["symbol"], "reason": session["status"], "stage": "market_data"})
            continue
        candles = [exact_bt.Candle(str(row[0]), *map(float, row[1:5])) for row in rows]
        candidate = {
            "date": day["date"],
            "month": "2025-10",
            "symbol": selected["symbol"],
            "direction": selected["direction"],
            "target_label": selected["target_label"],
            "target_price": selected["target_price"],
        }
        result = exact_bt.simulate_trade(candidate, candles, "immutable_selected_5m_cache", session["session_sha256"], config)
        position = min(TARGET_POSITION_RUB, MAX_LEVERAGE * equity)
        before = equity
        pnl = position * result.net_return_pct_at_full_risk / 100.0
        equity += pnl
        if equity <= 0:
            raise RuntimeError(f"Equity depleted on {day['date']}")
        peak = max(peak, equity)
        drawdown = (equity / peak - 1.0) * 100.0
        max_drawdown = min(max_drawdown, drawdown)
        row = asdict(result)
        row.update({"selected_rank": selected["rank"], "position_rub": position, "equity_before": before, "pnl_rub": pnl, "equity_after": equity, "drawdown_pct": drawdown})
        trades.append(row)
    wins = sum(row["net_return_pct_at_full_risk"] > 0 for row in trades)
    october_ledger = [
        fixed_research.ResearchTrade(
            date=str(row["date"]),
            month="2025-10",
            symbol=str(row["symbol"]),
            direction=str(row["direction"]),
            gross_return_pct=float(row["gross_return_pct"]),
            exit_reason=str(row["exit_reason"]),
            data_source=str(row["data_source"]),
            session_sha256=str(row["session_sha256"]),
        )
        for row in trades
    ]
    ledger_61, ledger_54, frozen_pins = fixed_research.load_ledgers()
    combined_ledger = sorted([*october_ledger, *ledger_54], key=lambda value: value.date)
    october_table_returns = [table_return(trade) for trade in october_ledger]
    combined_table_returns = [table_return(trade) for trade in combined_ledger]
    combined_fixed = fixed_research.simulate_fixed_notional(
        combined_ledger,
        start_equity=START_EQUITY_RUB,
        position_cap=TARGET_POSITION_RUB,
        max_leverage=MAX_LEVERAGE,
        cost_pct=ROUND_TRIP_COST_PCT,
    )
    return {
        "schema_version": 1,
        "artifact_type": "argonus_october_engine_a_rs5_current_version_research",
        "classification": cache["leakage"]["classification"],
        "cache_path": str(CACHE_PATH),
        "cache_sha256": cache["cache_sha256"],
        "config": {**cache["config"], "start_equity_rub": START_EQUITY_RUB, "target_position_rub": TARGET_POSITION_RUB, "max_leverage": MAX_LEVERAGE},
        "provenance": {
            "watchlists": cache["watchlists"],
            "artifact_hashes_at_selection": cache["artifact_hashes_at_selection"],
            "artifact_hashes_at_replay": artifact_hashes(),
            "offline_replay": True,
        },
        "leakage": cache["leakage"],
        "summary": {
            "calendar_days": len(cache["selection_days"]),
            "policy_selected_days": sum(day.get("selected") is not None for day in cache["selection_days"]),
            "executed_trades": len(trades),
            "wins": wins,
            "losses": len(trades) - wins,
            "skips": len(skips),
            "final_equity_rub": equity,
            "total_pnl_rub": equity - START_EQUITY_RUB,
            "total_return_on_equity_pct": (equity / START_EQUITY_RUB - 1.0) * 100.0,
            "trade_level_max_drawdown_pct": max_drawdown,
            "decisions": dict(sorted(Counter(day["decision"] for day in cache["selection_days"]).items())),
            "exit_reasons": dict(sorted(Counter(row["exit_reason"] for row in trades).items())),
        },
        "table_convention": {
            "description": "0.5x long / 1.0x short, compounded from 150,000 RUB",
            "october": {
                "return_pct": compound(october_table_returns),
                "capital_from_150k": 150_000.0 * (1.0 + compound(october_table_returns) / 100.0),
                "trade_close_mdd_pct": trade_close_mdd(october_table_returns),
            },
            "hybrid_october_current_plus_frozen_rs5_nov_jul": {
                "warning": (
                    "October is a current-version in-sample reconstruction; "
                    "Nov-Jul is the reconstructed frozen 54-trade RS5 ledger."
                ),
                "trades": len(combined_ledger),
                "wins": sum(
                    trade.gross_return_pct - ROUND_TRIP_COST_PCT > 0
                    for trade in combined_ledger
                ),
                "losses": sum(
                    trade.gross_return_pct - ROUND_TRIP_COST_PCT <= 0
                    for trade in combined_ledger
                ),
                "return_pct": compound(combined_table_returns),
                "capital_from_150k": 150_000.0 * (1.0 + compound(combined_table_returns) / 100.0),
                "trade_close_mdd_pct": trade_close_mdd(combined_table_returns),
                "monthly": table_monthly(combined_ledger),
            },
        },
        "fixed_150k_on_50k_hybrid_oct_to_jul": combined_fixed,
        "frozen_controls": {
            "ledger_61_trades": len(ledger_61),
            "ledger_54_trades": len(ledger_54),
            "pinned_inputs": frozen_pins,
        },
        "skips": skips,
        "trades": trades,
        "selection_days": cache["selection_days"],
    }


def format_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    table_october = report["table_convention"]["october"]
    hybrid = report["table_convention"]["hybrid_october_current_plus_frozen_rs5_nov_jul"]
    fixed = report["fixed_150k_on_50k_hybrid_oct_to_jul"]["summary"]
    lines = [
        "# October 2025 — current Engine A + RS5 research reconstruction",
        "",
        f"**{report['classification']}**",
        "",
        (f"Calendar={summary['calendar_days']} | selected={summary['policy_selected_days']} | "
         f"trades={summary['executed_trades']} | W/L={summary['wins']}/{summary['losses']} | skips={summary['skips']}"),
        (f"50,000 RUB -> {summary['final_equity_rub']:,.2f} RUB | "
         f"return={summary['total_return_on_equity_pct']:+.4f}% | MDD={summary['trade_level_max_drawdown_pct']:.4f}%"),
        (f"Table convention October: return={table_october['return_pct']:+.4f}% | "
         f"150,000 -> {table_october['capital_from_150k']:,.2f} RUB | "
         f"MDD={table_october['trade_close_mdd_pct']:.4f}%"),
        (f"Hybrid table Oct-Jul: {hybrid['trades']} trades | W/L={hybrid['wins']}/{hybrid['losses']} | "
         f"return={hybrid['return_pct']:+.4f}% | 150,000 -> {hybrid['capital_from_150k']:,.2f} RUB"),
        (f"Fixed 150k on 50k hybrid Oct-Jul: return={fixed['total_return_on_start_equity_pct']:+.4f}% | "
         f"equity={fixed['final_equity']:,.2f} RUB | MDD={fixed['max_trade_close_drawdown_pct']:.4f}%"),
        "",
        "The result is in-sample and uses the current instrument universe/status and current reranker artifacts. It is not OOS evidence.",
        "",
        "| Date | Pick | Rank | Exit | Net % | PnL RUB | Equity RUB |",
        "|---|---|---:|---|---:|---:|---:|",
    ]
    by_date = {row["date"]: row for row in report["trades"]}
    skip_by_date = {row["date"]: row for row in report["skips"]}
    for day in report["selection_days"]:
        trade = by_date.get(day["date"])
        if trade:
            lines.append(f"| {day['date']} | {trade['symbol']} {trade['direction']} | {trade['selected_rank']} | {trade['exit_reason']} | {trade['net_return_pct_at_full_risk']:+.4f} | {trade['pnl_rub']:+.2f} | {trade['equity_after']:.2f} |")
        else:
            skipped = skip_by_date[day["date"]]
            lines.append(f"| {day['date']} | SKIP | — | {skipped['reason']} | — | — | — |")
    lines.extend(
        [
            "",
            "## Hybrid table by month",
            "",
            "| Month | Trades | W/L | Return | Through capital |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for row in hybrid["monthly"]:
        lines.append(
            f"| {row['month']} | {row['trades']} | {row['wins']}/{row['losses']} | "
            f"{row['return_pct']:+.4f}% | {row['through_capital']:,.2f} RUB |"
        )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month-dir", type=Path, default=MONTH_DIR)
    parser.add_argument("--cache", type=Path, default=CACHE_PATH)
    parser.add_argument("--report-json", type=Path, default=REPORT_JSON)
    parser.add_argument("--report-text", type=Path, default=REPORT_TEXT)
    parser.add_argument("--offline", action="store_true", help="Fail if immutable cache is absent; never call API.")
    parser.add_argument("--workers", type=int, default=1, help="Daily-history fetch workers; 1 is rate-limit safe.")
    parser.add_argument("--request-pause", type=float, default=0.35)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    entries, manifest = discover_entries(args.month_dir.resolve())
    if args.cache.exists():
        cache = load_cache(args.cache.resolve(), manifest)
    elif args.offline:
        raise RuntimeError(f"--offline requires existing cache: {args.cache}")
    else:
        cache = build_cache(entries, manifest, args.cache.resolve(), max(args.workers, 1), args.request_pause)
    report = build_report(cache)
    args.report_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    text = format_report(report)
    args.report_text.write_text(text, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
