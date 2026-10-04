#!/usr/bin/env python3
"""Research-only replay of the live Engine A + RS5 on sessions after the frozen books.

The frozen 54/65-trade books stop on 2026-07-16.  This script rebuilds one
watchlist per weekday session with generate_watchlist.py's own scan, runs the
current analyzer with the gates exported by run_tick.sh (regime gates, 8% rally
guard, T4 >= 2%, RS5 top-3 >= -2.39 pp) and replays every selected idea on
5-minute candles with research_0705_execution.simulate: 07:05 entry, stop gaps,
5 bps adverse slippage and 0.04% commission per side, open of the 18:35 bar.

Generator histories are downloaded once per TQBR share and served back through
a cached client, so every date still sees only sessions before it.  All market
data behind the report is stored in one gzip cache; ``--offline`` rebuilds the
report from it without constructing an API client.  Nothing here can place,
modify or cancel orders.

``--validate-july`` runs the same pipeline on 2026-07-01..16 and compares it
with the stored July watchlists and the frozen 54-trade ledger.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from argonus.backtesting import backtest_generated_watchlists as daily_bt
from argonus.backtesting import backtest_live_61_trades as exact_bt
from argonus.watchlists import generate_watchlist as gw
from argonus.research import research_0705_execution as r0705
from argonus.research import research_fixed_150k_sizing as fixed_research
from argonus.research import research_flat_month_exit_risk as book_data
from argonus.research import research_october_engine_a_rs5 as october
from argonus.market_data.tbank_market_data import (
    MOSCOW_TZ,
    DailyCandle,
    ShareInstrument,
    TBankApiError,
    TBankInvestClient,
    _get_field,
    parse_api_datetime,
    quotation_to_float,
    to_api_datetime_range,
)
from argonus.watchlists import watchlist_best_target as wbt


from argonus.paths import PROJECT_ROOT as ROOT
DEFAULT_OUTPUT_DIR = ROOT / "data/backtests/new_period_engine_a_2026-09-30"
JULY_DIR = ROOT / "data/watchlists/generated_watchlists_2026_07"
FIRST_NEW_SESSION = date(2026, 7, 17)
VALIDATION_START = date(2026, 7, 1)
VALIDATION_END = date(2026, 7, 16)

# generate_watchlist.fetch_stock_history(days=60) asks for as_of - 2 * 60 days.
GENERATOR_LOOKBACK_DAYS = 120
# backtest_generated_watchlists.build_candle_cache history window.
ANALYZER_LOOKBACK_DAYS = 500
# Bars after 18:35 are kept only so that a missing 18:35 bar still has an exit.
SESSION_START = "07:00"
SESSION_END = "18:55"
SLIPPAGE_STRESS_BPS = (0.0, 5.0, 10.0, 20.0)
RETRY_DELAYS = (2.0, 4.0, 8.0, 16.0, 32.0)
USER_AGENT = "new-period-engine-a-research/1.0"

T = TypeVar("T")


def progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def with_retry(call: Callable[[], T]) -> T:
    """The client already retries 429/5xx; this adds slower outer attempts."""
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            return call()
        except TBankApiError as exc:
            text = str(exc)
            transient = "HTTP 429" in text or "HTTP 5" in text or "сети" in text
            if not transient or attempt >= len(RETRY_DELAYS):
                raise
            time.sleep(RETRY_DELAYS[attempt])
    raise RuntimeError("unreachable")


# ─── Serialisation ──────────────────────────────────────────────────────────


def candle_to_row(candle: DailyCandle) -> list[Any]:
    return [candle.trade_date.isoformat(), candle.open, candle.low, candle.high, candle.close, candle.volume_lots]


def row_to_candle(row: list[Any]) -> DailyCandle:
    return DailyCandle(date.fromisoformat(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), int(row[5]))


def write_json_gz(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, allow_nan=False)
    temporary.replace(path)


def read_json_gz(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


# ─── Market data ────────────────────────────────────────────────────────────


def fetch_calendar(client: TBankInvestClient, start: date, end: date) -> list[date]:
    """Weekday sessions with a completed IMOEX daily candle; the bot skips weekends."""
    index_id = client.resolve_index_instrument_id(wbt.DEFAULT_INDEX_SYMBOL, class_code_hint=wbt.DEFAULT_INDEX_BOARD)
    candles = with_retry(lambda: client.get_daily_candles(index_id, start_date=start, end_date=end))
    return [candle.trade_date for candle in candles if candle.trade_date.weekday() < 5]


def fetch_daily_map(
    client: TBankInvestClient,
    ids_by_key: dict[str, str],
    start: date,
    end: date,
    workers: int,
) -> tuple[dict[str, list[DailyCandle]], dict[str, str]]:
    def one(item: tuple[str, str]) -> tuple[str, list[DailyCandle] | None, str | None]:
        key, instrument_id = item
        try:
            candles = with_retry(lambda: client.get_daily_candles(instrument_id, start_date=start, end_date=end))
            return key, candles, None
        except TBankApiError as exc:
            return key, None, str(exc)

    histories: dict[str, list[DailyCandle]] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(workers, 1)) as pool:
        for index, (key, candles, error) in enumerate(pool.map(one, sorted(ids_by_key.items())), start=1):
            if error is not None:
                errors[key] = error
            else:
                histories[key] = candles or []
            if index % 50 == 0 or index == len(ids_by_key):
                progress(f"  дневные свечи: {index}/{len(ids_by_key)}")
    return histories, errors


def fetch_five_minute_session(client: TBankInvestClient, symbol: str, trade_date: date) -> list[list[Any]]:
    instrument_id = client.resolve_share_instrument_id(symbol, wbt.DEFAULT_BOARD)
    start_dt, end_dt = to_api_datetime_range(trade_date, trade_date)
    payload = {
        "instrumentId": instrument_id,
        "from": start_dt,
        "to": end_dt,
        "interval": "CANDLE_INTERVAL_5_MIN",
        "candleSourceType": "CANDLE_SOURCE_EXCHANGE",
    }
    response = with_retry(
        lambda: client._post("tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles", payload)
    )
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
        if SESSION_START <= clock <= SESSION_END:
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
    return rows


# ─── Watchlist generation ───────────────────────────────────────────────────


class CachedGeneratorClient:
    """Serves generate_watchlist's two API calls from one prefetched snapshot."""

    def __init__(
        self,
        shares: list[ShareInstrument],
        histories: dict[str, list[DailyCandle]],
        errors: dict[str, str],
        start: date,
        end: date,
    ) -> None:
        self.shares = shares
        self.histories = histories
        self.errors = errors
        self.start = start
        self.end = end

    def list_moex_shares(self) -> list[ShareInstrument]:
        return [ShareInstrument(**asdict(share)) for share in self.shares]

    def get_daily_candles(self, instrument_id: str, *, start_date: date, end_date: date) -> list[DailyCandle]:
        if start_date < self.start or end_date > self.end:
            raise RuntimeError(
                f"Generator window {start_date}..{end_date} is outside the cache {self.start}..{self.end}"
            )
        if instrument_id in self.errors:
            raise TBankApiError(self.errors[instrument_id])
        return [candle for candle in self.histories[instrument_id] if start_date <= candle.trade_date <= end_date]


def generate_watchlists(cached_client: CachedGeneratorClient, dates: list[date]) -> dict[str, str]:
    gw._market_client = cached_client
    gw.progress = lambda message: None
    texts: dict[str, str] = {}
    for as_of in dates:
        result = gw._scan_and_select_result(as_of, top_n=gw.TOP_PER_DIRECTION, min_volume=gw.MIN_AVG_VOLUME_RUB)
        texts[as_of.isoformat()] = gw.format_watchlist(result.selected)
    return texts


def write_watchlists(texts: dict[str, str], directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for day, text in sorted(texts.items()):
        path = directory / f"watchlist_{day}.txt"
        path.write_text(text, encoding="utf-8")
        paths[day] = path
    return paths


# ─── Selection ──────────────────────────────────────────────────────────────


def select(
    paths: dict[str, Path],
    stock_cache: dict[str, list[DailyCandle]],
    index_candles: list[DailyCandle],
) -> list[dict[str, Any]]:
    """Current analyzer + pinned gates + RS5 exactly as research_october_engine_a_rs5."""
    entries = []
    for day, path in sorted(paths.items()):
        text = path.read_text(encoding="utf-8")
        symbols = tuple(idea.symbol for idea in wbt.parse_watchlist(text))
        entries.append(october.Entry(date.fromisoformat(day), path, text, symbols))
    daily_bt.build_candle_cache = lambda workers, watchlists: (stock_cache, index_candles)
    days, _ = october.select_days(entries, workers=1)
    return days


def analyzer_symbols(paths: dict[str, Path]) -> list[str]:
    return sorted({idea.symbol for path in paths.values() for idea in wbt.parse_watchlist(path.read_text(encoding="utf-8"))})


def fetch_analyzer_data(
    client: TBankInvestClient,
    symbols: list[str],
    first: date,
    last: date,
    workers: int,
) -> tuple[dict[str, list[DailyCandle]], list[DailyCandle], dict[str, str]]:
    start = first - timedelta(days=ANALYZER_LOOKBACK_DAYS)
    ids = {symbol: client.resolve_share_instrument_id(symbol, wbt.DEFAULT_BOARD) for symbol in symbols}
    stock_cache, errors = fetch_daily_map(client, ids, start, last, workers)
    index_id = client.resolve_index_instrument_id(wbt.DEFAULT_INDEX_SYMBOL, class_code_hint=wbt.DEFAULT_INDEX_BOARD)
    index_candles = with_retry(lambda: client.get_daily_candles(index_id, start_date=start, end_date=last))
    return stock_cache, index_candles, errors


def fetch_selected_sessions(client: TBankInvestClient, days: list[dict[str, Any]]) -> dict[str, Any]:
    sessions: dict[str, Any] = {}
    for day in days:
        selected = day.get("selected")
        if not selected:
            continue
        key = f"{day['date']}|{selected['symbol']}"
        rows = fetch_five_minute_session(client, selected["symbol"], date.fromisoformat(day["date"]))
        sessions[key] = {
            "status": "ok" if rows else "unavailable_no_5m_candles",
            "rows": rows,
            "session_sha256": october.canonical_sha256(rows),
        }
    return sessions


# ─── Replay ─────────────────────────────────────────────────────────────────


def to_trade(day: dict[str, Any], session: dict[str, Any]) -> book_data.Trade:
    selected = day["selected"]
    return book_data.Trade(
        date=day["date"],
        month=day["date"][:7],
        symbol=selected["symbol"],
        direction=selected["direction"],
        target_price=float(selected["target_price"]),
        candles=tuple(exact_bt.Candle(str(row[0]), *map(float, row[1:5])) for row in session["rows"]),
        source="t-invest 5m, new period",
        session_sha256=session["session_sha256"],
        expected_gross_return_pct=0.0,
        features=None,
    )


def replay_0705(trades: list[book_data.Trade], fills: r0705.FillModel) -> tuple[list[r0705.Execution], list[dict[str, str]]]:
    executions: list[r0705.Execution] = []
    failures: list[dict[str, str]] = []
    for trade in trades:
        try:
            executions.append(r0705.simulate(trade, r0705.POLICIES[0], fills))
        except ValueError as exc:
            # No 07:05 bar means no morning book for the 07:05 FOK entry.
            failures.append({"date": trade.date, "symbol": trade.symbol, "reason": str(exc)})
    return executions, failures


def table_convention(trades: list[book_data.Trade]) -> list[dict[str, Any]]:
    """Legacy table: 07:00 entry, 0.08% round trip, long at half risk."""
    config = exact_bt.BacktestConfig()
    rows = []
    for trade in trades:
        candidate = {
            "date": trade.date,
            "month": trade.month,
            "symbol": trade.symbol,
            "direction": trade.direction,
            "target_label": "T4",
            "target_price": trade.target_price,
        }
        result = exact_bt.simulate_trade(candidate, list(trade.candles), trade.source, trade.session_sha256, config)
        multiplier = 0.5 if trade.direction == "long" else 1.0
        rows.append(
            {
                "date": trade.date,
                "month": trade.month,
                "symbol": trade.symbol,
                "direction": trade.direction,
                "gross_return_pct": result.gross_return_pct,
                "exit_reason": result.exit_reason,
                "table_return_pct": (result.gross_return_pct - config.fee_pct) * multiplier,
            }
        )
    return rows


def monthly_fresh_50k(executions: list[r0705.Execution]) -> dict[str, dict[str, Any]]:
    """Each month from its own 50,000 RUB start, so months are comparable."""
    months: dict[str, list[r0705.Execution]] = {}
    for row in executions:
        months.setdefault(row.month, []).append(row)
    result = {}
    for month, rows in sorted(months.items()):
        account = r0705.account(rows)
        traded = [row for row in rows if row.traded]
        result[month] = {
            "trades": account["trades"],
            "wins": account["wins"],
            "losses": account["losses"],
            "return_pct": account["return_pct"],
            "pnl_rub": account["pnl_rub"],
            "avg_net_pct": sum(row.net_return_pct for row in traded) / len(traded) if traded else 0.0,
        }
    return result


def trade_stats(executions: list[r0705.Execution]) -> dict[str, Any]:
    traded = [row for row in executions if row.traded]
    nets = [row.net_return_pct for row in traded]
    wins = [value for value in nets if value > 0]
    losses = [value for value in nets if value <= 0]
    by_direction = {}
    for direction in ("long", "short"):
        values = [row.net_return_pct for row in traded if row.direction == direction]
        by_direction[direction] = {
            "trades": len(values),
            "wins": sum(value > 0 for value in values),
            "avg_net_pct": sum(values) / len(values) if values else 0.0,
        }
    return {
        "trades": len(traded),
        "wins": len(wins),
        "win_rate_pct": 100.0 * len(wins) / len(traded) if traded else 0.0,
        "avg_net_pct": sum(nets) / len(nets) if nets else 0.0,
        "avg_win_pct": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss_pct": sum(losses) / len(losses) if losses else 0.0,
        "by_direction": by_direction,
        "exit_reasons": dict(sorted(Counter(row.reason for row in traded).items())),
    }


def historical_baseline() -> dict[str, Any]:
    trades54, trades65, _ = book_data.load_books()
    executions = [r0705.simulate(trade, r0705.POLICIES[0], r0705.FillModel()) for trade in trades65]
    # The Extended65 calendar is every stored watchlist from October to 2026-07-16.
    sessions = [
        path for path in (ROOT / "data/watchlists").glob("generated_watchlists_*/watchlist_*.txt")
        if "generated_watchlists_2025_10" <= path.parent.name <= "generated_watchlists_2026_07"
    ]
    return {
        "book": "Extended65 (2025-10 .. 2026-07-16)",
        "sessions": len(sessions),
        "account": r0705.summary(r0705.account(executions)),
        "stats": trade_stats(executions),
        "monthly_fresh_50k": monthly_fresh_50k(executions),
    }


def index_context(cache: dict[str, Any]) -> dict[str, Any]:
    closes = {row[0]: float(row[4]) for row in cache["analyzer_index"]}
    start, end = cache["period"]["start"], cache["period"]["end"]
    before = max(day for day in closes if day < start)
    return {
        "symbol": wbt.DEFAULT_INDEX_SYMBOL,
        "close_before_start": [before, closes[before]],
        "close_at_end": [end, closes[end]],
        "change_pct": (closes[end] / closes[before] - 1.0) * 100.0,
    }


def entry_flips(executions: list[dict[str, Any]], table_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trades whose stop outcome differs between the 07:00 and the live 07:05 entry."""
    table_by_date = {row["date"]: row for row in table_rows}
    flips = []
    for row in executions:
        table = table_by_date[row["date"]]
        stopped_0705 = row["reason"] in {"stop", "stop_gap", "both_stop_first"}
        stopped_0700 = table["exit_reason"] in {"stop", "both_stop_first"}
        if stopped_0705 != stopped_0700:
            flips.append(
                {
                    "date": row["date"],
                    "symbol": row["symbol"],
                    "direction": row["direction"],
                    "net_0705_pct": row["net_return_pct"],
                    "gross_0700_pct": table["gross_return_pct"],
                }
            )
    return flips


def build_report(cache: dict[str, Any]) -> dict[str, Any]:
    days = cache["selection_days"]
    sessions = cache["sessions"]
    trades: list[book_data.Trade] = []
    skips: list[dict[str, Any]] = []
    for day in days:
        selected = day.get("selected")
        if not selected:
            skips.append({"date": day["date"], "stage": "selection", "reason": day["decision_reason"]})
            continue
        session = sessions[f"{day['date']}|{selected['symbol']}"]
        if not session["rows"]:
            skips.append({"date": day["date"], "stage": "market_data", "reason": session["status"]})
            continue
        trades.append(to_trade(day, session))

    executions, failures = replay_0705(trades, r0705.FillModel())
    skips.extend({"date": row["date"], "stage": "execution", "reason": row["reason"]} for row in failures)
    account = r0705.account(executions)
    stress = {}
    for bps in SLIPPAGE_STRESS_BPS:
        rows, _ = replay_0705(trades, r0705.FillModel(slippage_side_bps=bps))
        stress[f"{bps:g}"] = r0705.summary(r0705.account(rows))
    table_rows = table_convention(trades)
    table_months: dict[str, list[float]] = {}
    for row in table_rows:
        table_months.setdefault(row["month"], []).append(row["table_return_pct"])
    rank_by_date = {day["date"]: day["selected"]["rank"] for day in days if day.get("selected")}
    return {
        "schema_version": 1,
        "artifact_type": "argonus_new_period_engine_a_rs5_replay",
        "classification": "OUT-OF-SAMPLE FOR MODELS AND GATES (all fitted on data up to 2026-07-16); current universe snapshot",
        "period": cache["period"],
        "sessions": len(days),
        "config": {
            "entry": "07:05 open + adverse slippage",
            "exit": "stop / T4 / open of the 18:35 bar",
            "stop_loss_pct": 1.0,
            "fee_side_pct": r0705.FillModel().fee_side_pct,
            "slippage_side_bps": r0705.FillModel().slippage_side_bps,
            "account": "50,000 RUB start, position min(150,000, 3 x equity)",
            "gates": {
                "regime_gates": october.REGIME_GATES,
                "short_rally_guard_pct": october.SHORT_RALLY_GUARD_PCT,
                "minimum_target_pct": october.MIN_TARGET_PCT,
                "rs5_threshold_pp": october.RS5_THRESHOLD_PP,
                "rs5_top_k": october.RS5_TOP_K,
                "day_filter": False,
            },
        },
        "summary": {**r0705.summary(account), "stats": trade_stats(executions)},
        "monthly_fresh_50k": monthly_fresh_50k(executions),
        "slippage_stress": stress,
        "decisions": dict(sorted(Counter(day["decision"] for day in days).items())),
        "skip_reasons": dict(sorted(Counter(skip_category(skip["reason"]) for skip in skips).items())),
        "table_convention": {
            "description": "07:00 entry, 0.08% round trip, long at 0.5x, compounded monthly",
            "monthly": {month: october.compound(values) for month, values in sorted(table_months.items())},
            "total_pct": october.compound([row["table_return_pct"] for row in table_rows]),
            "trades": table_rows,
            "entry_flips_vs_0705": entry_flips(account["per_trade"], table_rows),
        },
        "index": index_context(cache),
        "trades": [{**row, "selected_rank": rank_by_date[row["date"]]} for row in account["per_trade"]],
        "skips": sorted(skips, key=lambda row: row["date"]),
        "historical_baseline": historical_baseline(),
        "selection_days": days,
    }


def skip_category(reason: str) -> str:
    for prefix, label in (
        ("regime: short", "режим: шорт при индексе выше EMA20"),
        ("regime: long", "режим: лонг при индексе ниже EMA20"),
        ("rally_guard", "rally-guard"),
        ("rr_floor", "RR-floor (T4 < 2%)"),
        ("no eligible top-", "RS5: нет кандидата выше порога"),
        ("no trade-day daily candle", "нет дневной свечи дня сделки"),
        ("Missing exact 07:05 candle", "нет 5-мин бара 07:05"),
        ("analysis:", "анализатор отбросил rank-1"),
    ):
        if reason.startswith(prefix):
            return label
    return reason


# ─── Markdown ───────────────────────────────────────────────────────────────


def rub(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ")


def pct(value: float, digits: int = 2) -> str:
    return f"{value:+.{digits}f}%".replace(".", ",")


def month_name(month: str) -> str:
    names = {"01": "январь", "02": "февраль", "03": "март", "04": "апрель", "05": "май", "06": "июнь",
             "07": "июль", "08": "август", "09": "сентябрь", "10": "октябрь", "11": "ноябрь", "12": "декабрь"}
    return f"{names[month[5:7]]} {month[:4]}"


def markdown(report: dict[str, Any], validation: dict[str, Any] | None) -> str:
    summary = report["summary"]
    stats = summary["stats"]
    history = report["historical_baseline"]
    period = report["period"]
    lines = [
        f"# Движок A + RS5 на новых днях — {period['start']} … {period['end']}",
        "",
        "Research-only replay текущей боевой конфигурации (`run_tick.sh`) на сессиях, которых не было в "
        "замороженных книгах 54/65. Модели, реранкеры, RS5-порог и RR-floor подобраны на данных до 16 июля, "
        "поэтому для них это вневыборочный период. Реальные заявки не отправлялись.",
        "",
        f"**{report['sessions']} сессий → {summary['trades']} сделок, W/L {summary['wins']}/{summary['losses']}. "
        f"50 000 ₽ → {rub(summary['ending_equity_rub'])} ₽ ({pct(summary['return_pct'])}), "
        f"просадка по закрытым сделкам {pct(summary['closed_trade_mdd_pct'])}.**",
        "",
        "Модель исполнения как у контроля `research_0705_execution.py`: вход по open бара 07:05 с проскальзыванием "
        "5 б.п., стоп 1% (гэп исполняется хуже стопа), цель — frozen T4, выход по open бара 18:35, комиссия 0,04% "
        "с оборота каждой стороны, позиция min(150 000 ₽, 3× капитала).",
        "",
        f"Рынок: {report['index']['symbol']} {rub(report['index']['close_before_start'][1])} "
        f"({report['index']['close_before_start'][0]}) → {rub(report['index']['close_at_end'][1])} "
        f"({report['index']['close_at_end'][0]}), {pct(report['index']['change_pct'], 1)}. "
        "Режимные ворота запрещают шорт выше EMA20 и лонг ниже неё.",
        "",
        "## По месяцам (каждый месяц — со своих 50 000 ₽)",
        "",
        "| Месяц | Сделки | W/L | Средняя сделка, net | Доходность месяца | PnL, ₽ |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for month, row in report["monthly_fresh_50k"].items():
        lines.append(
            f"| {month_name(month)} | {row['trades']} | {row['wins']}/{row['losses']} | {pct(row['avg_net_pct'])} | "
            f"{pct(row['return_pct'])} | {rub(row['pnl_rub'])} |"
        )
    hist_stats = history["stats"]
    hist_months = history["monthly_fresh_50k"]
    hist_avg_month = sum(row["return_pct"] for row in hist_months.values()) / len(hist_months)
    new_months = report["monthly_fresh_50k"]
    new_avg_month = sum(row["return_pct"] for row in new_months.values()) / len(new_months) if new_months else 0.0

    def direction_cell(values: dict[str, Any]) -> str:
        return f"{values['trades']} сделок, {values['wins']} в плюсе, в среднем {pct(values['avg_net_pct'])}"

    lines += [
        "",
        "## Сравнение с историей (та же модель исполнения)",
        "",
        "| Показатель | История окт–16 июля | Новый период |",
        "|---|---:|---:|",
        f"| Сессий → сделок | {history['sessions']} → {hist_stats['trades']} "
        f"({100 * hist_stats['trades'] / history['sessions']:.0f}%) | {report['sessions']} → {stats['trades']} "
        f"({100 * stats['trades'] / report['sessions']:.0f}%) |",
        f"| Доля прибыльных | {hist_stats['win_rate_pct']:.0f}% | {stats['win_rate_pct']:.0f}% |",
        f"| Средняя сделка, net | {pct(hist_stats['avg_net_pct'])} | {pct(stats['avg_net_pct'])} |",
        f"| Средний выигрыш / проигрыш | {pct(hist_stats['avg_win_pct'])} / {pct(hist_stats['avg_loss_pct'])} | "
        f"{pct(stats['avg_win_pct'])} / {pct(stats['avg_loss_pct'])} |",
        f"| Лонги | {direction_cell(hist_stats['by_direction']['long'])} | "
        f"{direction_cell(stats['by_direction']['long'])} |",
        f"| Шорты | {direction_cell(hist_stats['by_direction']['short'])} | "
        f"{direction_cell(stats['by_direction']['short'])} |",
        f"| Средний месяц со своих 50 000 ₽ | {pct(hist_avg_month)} | {pct(new_avg_month)} |",
        "",
        "Исторические месяцы (со своих 50 000 ₽): "
        + ", ".join(f"{month[2:]} {pct(row['return_pct'], 1)}" for month, row in hist_months.items())
        + ".",
        "",
        "## Все дни",
        "",
        "| Дата | Решение | Ранг | Выход | Net | PnL, ₽ | Счёт, ₽ |",
        "|---|---|---:|---|---:|---:|---:|",
    ]
    trades_by_date = {row["date"]: row for row in report["trades"]}
    skips_by_date = {row["date"]: row for row in report["skips"]}
    for day in report["selection_days"]:
        trade = trades_by_date.get(day["date"])
        if trade:
            lines.append(
                f"| {day['date']} | {trade['symbol']} {trade['direction']} | {trade['selected_rank']} | "
                f"{trade['reason']} | {pct(trade['net_return_pct'])} | {rub(trade['pnl_rub'])} | "
                f"{rub(trade['equity_after_rub'])} |"
            )
        else:
            reason = skips_by_date[day["date"]]["reason"]
            lines.append(f"| {day['date']} | пропуск | — | {skip_category(reason)} | — | — | — |")
    lines += [
        "",
        "Причины пропусков: "
        + "; ".join(f"{label} — {count}" for label, count in report["skip_reasons"].items())
        + ".",
        "",
        "## Чувствительность к проскальзыванию",
        "",
        "| Б.п. на сторону | Доходность | PnL, ₽ | MDD |",
        "|---:|---:|---:|---:|",
    ]
    for bps, row in report["slippage_stress"].items():
        lines.append(f"| {bps} | {pct(row['return_pct'])} | {rub(row['pnl_rub'])} | {pct(row['closed_trade_mdd_pct'])} |")
    table = report["table_convention"]
    lines += [
        "",
        "Старая табличная конвенция (вход 07:00, 0,08% за круг, лонг с половинным риском): "
        + ", ".join(f"{month_name(month)} {pct(value)}" for month, value in table["monthly"].items())
        + f"; всего {pct(table['total_pct'])}.",
    ]
    if table["entry_flips_vs_0705"]:
        lines += [
            "",
            "Сделки, где исход решили первые пять минут (стоп при одном входе и не стоп при другом): "
            + ", ".join(
                f"{row['date']} {row['symbol']} {row['direction']} — 07:05 {pct(row['net_0705_pct'])} net, "
                f"07:00 {pct(row['gross_0700_pct'])} gross"
                for row in table["entry_flips_vs_0705"]
            )
            + ".",
        ]
    if validation:
        lines += ["", "## Проверка конвейера на 1–16 июля", ""]
        lines += validation_lines(validation)
    lines += [
        "",
        "## Ограничения",
        "",
        "- Watchlist'ы пересобраны сейчас: вселенная TQBR и флаги (шорт доступен, покупка доступна) — снимок "
        "30 сентября, а не на дату сделки.",
        "- Вход 07:05 в бою — FOK по стакану глубины 50 с лимитом влияния 10 б.п.; исторического стакана нет, "
        "поэтому исполнение — open бара плюс 5 б.п. Лотность, маржинальные ставки, налоги не учтены.",
        "- Просадка считается по закрытым сделкам; внутри дня она может быть глубже.",
        f"- Период короткий ({summary['trades']} сделок): доверительный интервал широкий, один-два исхода "
        "заметно меняют итог.",
        "",
        "## Воспроизведение",
        "",
        "```bash",
        "python3 -m argonus.research.research_new_period_engine_a            # загрузка + отчёт",
        "python3 -m argonus.research.research_new_period_engine_a --offline  # только из кэша, без API",
        "python3 -m argonus.research.research_new_period_engine_a --validate-july",
        "```",
    ]
    return "\n".join(lines) + "\n"


def validation_lines(validation: dict[str, Any]) -> list[str]:
    lines = [
        f"Watchlist'ы, пересобранные сейчас, совпали с сохранёнными в июле: "
        f"{validation['watchlists_identical']}/{validation['sessions']}.",
        f"Выбор сделок по сохранённым июльским watchlist'ам совпал с замороженной книгой: "
        f"{validation['stored_match']}/{validation['sessions']} дней; по пересобранным — "
        f"{validation['regenerated_match']}/{validation['sessions']}.",
        "",
        "| Дата | Замороженная книга | По сохранённым | По пересобранным |",
        "|---|---|---|---|",
    ]
    for row in validation["days"]:
        lines.append(f"| {row['date']} | {row['frozen']} | {row['stored']} | {row['regenerated']} |")
    if validation.get("returns"):
        lines += [
            "",
            "Доходность gross по табличной конвенции на свежих 5-мин свечах против замороженной книги: "
            + ", ".join(
                f"{row['date'][5:]} {row['symbol']} {row['fresh']:+.2f}/{row['frozen']:+.2f}"
                for row in validation["returns"]
            )
            + ".",
        ]
    return lines


# ─── No-skip variants ───────────────────────────────────────────────────────

NO_SKIP_POLICIES = {
    "no_skip": "без пропусков: ворота выключены, RS5 выбирает, но вместо пропуска берёт rank-1",
    "rank1": "всегда rank-1 анализатора: без ворот и без RS5",
}


@contextmanager
def gates_disabled() -> Iterator[None]:
    """Regime gates, rally guard and the T4 floor never reject an idea inside this block."""
    saved = (october.REGIME_GATES, october.SHORT_RALLY_GUARD_PCT, october.MIN_TARGET_PCT)
    october.REGIME_GATES, october.SHORT_RALLY_GUARD_PCT, october.MIN_TARGET_PCT = False, math.inf, -math.inf
    try:
        yield
    finally:
        october.REGIME_GATES, october.SHORT_RALLY_GUARD_PCT, october.MIN_TARGET_PCT = saved


def select_without_gates(cache: dict[str, Any], output_dir: Path) -> tuple[list[dict[str, Any]], dict[str, list[DailyCandle]]]:
    """Re-run the analyzer offline from the cache with every day-skipping gate disabled."""
    paths = write_watchlists(cache["watchlists"], output_dir / "watchlists")
    stock_cache = {symbol: [row_to_candle(row) for row in rows] for symbol, rows in cache["analyzer_stock"].items()}
    index_candles = [row_to_candle(row) for row in cache["analyzer_index"]]
    with gates_disabled():
        return select(paths, stock_cache, index_candles), stock_cache


def selection_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in ("rank", "symbol", "direction", "target_label", "target_price", "target_move_pct", "overall_score", "aligned_rs5_pp")
    }


def apply_no_skip_policy(
    days: list[dict[str, Any]],
    stock_cache: dict[str, list[DailyCandle]],
    live_days: dict[str, dict[str, Any]],
    policy: str,
) -> list[dict[str, Any]]:
    """Pick a trade for every session; only structurally untradable ideas are passed over."""

    def tradable(row: dict[str, Any], trade_date: str) -> bool:
        # With the gates disabled only analyzer/target problems remain as reasons.
        return (
            row["eligibility_reason"] is None
            and row.get("target_price") is not None
            and any(candle.trade_date.isoformat() == trade_date for candle in stock_cache.get(row["symbol"], []))
        )

    result = []
    for day in days:
        rows = day["rs5_policy"]["candidates"]
        chosen = None
        if policy == "no_skip" and day.get("selected"):
            chosen = next(row for row in rows if row["rank"] == day["selected"]["rank"])
        if chosen is None:
            chosen = next((row for row in rows if tradable(row, day["date"])), None)
        live = live_days[day["date"]]
        result.append(
            {
                "date": day["date"],
                "decision": "trade" if chosen else "no_tradable_candidate",
                "decision_reason": day["decision_reason"],
                "selected": selection_from_row(chosen) if chosen else None,
                "live_selected": live.get("selected"),
                "live_skip_reason": None if live.get("selected") else live["decision_reason"],
            }
        )
    return result


def collect_no_skip(cache: dict[str, Any], output_dir: Path, offline: bool) -> dict[str, Any]:
    days, stock_cache = select_without_gates(cache, output_dir)
    live_days = {day["date"]: day for day in cache["selection_days"]}
    policies = {name: apply_no_skip_policy(days, stock_cache, live_days, name) for name in NO_SKIP_POLICIES}
    needed = {
        f"{day['date']}|{day['selected']['symbol']}"
        for policy_days in policies.values() for day in policy_days if day["selected"]
    }
    missing = sorted(needed - set(cache["sessions"]))
    if missing and offline:
        raise RuntimeError(f"--offline: no cached 5m sessions for {missing}")
    sessions: dict[str, Any] = {}
    if missing:
        client = TBankInvestClient(user_agent=USER_AGENT)
        progress(f"5-мин сессии для новых дней: {len(missing)}")
        for key in missing:
            trade_date, symbol = key.split("|")
            rows = fetch_five_minute_session(client, symbol, date.fromisoformat(trade_date))
            sessions[key] = {
                "status": "ok" if rows else "unavailable_no_5m_candles",
                "rows": rows,
                "session_sha256": october.canonical_sha256(rows),
            }
    return {
        "schema_version": 1,
        "created_at": datetime.now(MOSCOW_TZ).isoformat(timespec="seconds"),
        "period": cache["period"],
        "policies": policies,
        "sessions": sessions,
    }


def replay_policy(days: list[dict[str, Any]], sessions: dict[str, Any], fills: r0705.FillModel = r0705.FillModel()) -> dict[str, Any]:
    trades = []
    unavailable = []
    for day in days:
        if not day["selected"]:
            unavailable.append({"date": day["date"], "reason": day["decision"]})
            continue
        session = sessions[f"{day['date']}|{day['selected']['symbol']}"]
        if not session["rows"]:
            unavailable.append({"date": day["date"], "reason": session["status"]})
            continue
        trades.append(to_trade(day, session))
    executions, failures = replay_0705(trades, fills)
    unavailable.extend(failures)
    account = r0705.account(executions)
    return {"executions": executions, "account": account, "unavailable": unavailable}


def build_no_skip_report(cache: dict[str, Any], no_skip: dict[str, Any], live_report: dict[str, Any]) -> dict[str, Any]:
    sessions = {**cache["sessions"], **no_skip["sessions"]}
    live_summary = live_report["summary"]
    variants: dict[str, Any] = {
        "live": {
            "label": "с пропусками (боевой)",
            "summary": {key: value for key, value in live_summary.items() if key != "stats"},
            "stats": live_summary["stats"],
            "monthly_fresh_50k": live_report["monthly_fresh_50k"],
        }
    }
    replays = {}
    for name, label in NO_SKIP_POLICIES.items():
        replay = replay_policy(no_skip["policies"][name], sessions)
        replays[name] = replay
        stress = {
            f"{bps:g}": r0705.summary(replay_policy(no_skip["policies"][name], sessions, r0705.FillModel(slippage_side_bps=bps))["account"])
            for bps in SLIPPAGE_STRESS_BPS
        }
        variants[name] = {
            "label": label,
            "summary": r0705.summary(replay["account"]),
            "stats": trade_stats(replay["executions"]),
            "monthly_fresh_50k": monthly_fresh_50k(replay["executions"]),
            "slippage_stress": stress,
            "unavailable": replay["unavailable"],
        }

    # Days grouped by what the live engine did on them.
    groups: dict[str, dict[str, Any]] = {}
    changed_picks: dict[str, list[dict[str, str]]] = {}
    for name in NO_SKIP_POLICIES:
        executions = {row.date: row for row in replays[name]["executions"]}
        grouped: dict[str, list[r0705.Execution]] = {}
        changed_picks[name] = []
        for day in no_skip["policies"][name]:
            row = executions.get(day["date"])
            if row is None:
                continue
            if day["live_skip_reason"] is not None:
                grouped.setdefault(skip_category(day["live_skip_reason"]), []).append(row)
                grouped.setdefault("все бывшие пропуски", []).append(row)
            else:
                grouped.setdefault("дни, где боевой торговал", []).append(row)
                live = day["live_selected"]
                if (live["symbol"], live["direction"]) != (row.symbol, row.direction):
                    changed_picks[name].append(
                        {"date": day["date"], "live": f"{live['symbol']} {live['direction']}", "variant": f"{row.symbol} {row.direction}"}
                    )
        groups[name] = {group: trade_stats(rows) for group, rows in grouped.items()}
    return {
        "schema_version": 1,
        "artifact_type": "argonus_new_period_no_skip_comparison",
        "period": cache["period"],
        "sessions": len(cache["selection_days"]),
        "variants": variants,
        "groups": groups,
        "changed_picks_on_live_days": changed_picks,
        "trades": {
            "live": live_report["trades"],
            **{name: replays[name]["account"]["per_trade"] for name in NO_SKIP_POLICIES},
        },
        "policies": no_skip["policies"],
    }


def no_skip_markdown(report: dict[str, Any]) -> str:
    variants = report["variants"]
    period = report["period"]
    lines = [
        f"# Без пропусков — {period['start']} … {period['end']}",
        "",
        f"Те же {report['sessions']} сессий и та же модель исполнения, что в [REPORT.md](REPORT.md): вход по open бара "
        "07:05 + 5 б.п., стоп 1%, цель frozen T4, выход по open бара 18:35, комиссия 0,04% на сторону, позиция "
        "min(150 000 ₽, 3× капитала), старт 50 000 ₽. Убраны все причины пропуска дня: режимные ворота, rally-guard, "
        "минимальная цель 2% и пропуск RS5. Боевые `trade_bot.py` и `run_tick.sh` не менялись.",
        "",
        "| Вариант | Сделки | W/L | Средняя сделка, net | Итог на 50 000 ₽ | MDD по закрытым |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for variant in variants.values():
        summary, stats = variant["summary"], variant["stats"]
        lines.append(
            f"| {variant['label']} | {summary['trades']} | {summary['wins']}/{summary['losses']} | "
            f"{pct(stats['avg_net_pct'])} | {rub(summary['ending_equity_rub'])} ₽ ({pct(summary['return_pct'])}) | "
            f"{pct(summary['closed_trade_mdd_pct'])} |"
        )
    lines += [
        "",
        "## По месяцам (каждый месяц — со своих 50 000 ₽)",
        "",
        "| Месяц | " + " | ".join(variant["label"] for variant in variants.values()) + " |",
        "|---|" + "---:|" * len(variants),
    ]
    months = sorted({month for variant in variants.values() for month in variant["monthly_fresh_50k"]})
    for month in months:
        cells = []
        for variant in variants.values():
            row = variant["monthly_fresh_50k"].get(month)
            cells.append(f"{pct(row['return_pct'])} ({row['trades']} сд.)" if row else "—")
        lines.append(f"| {month_name(month)} | " + " | ".join(cells) + " |")
    def group_cell(stats: dict[str, Any] | None) -> str:
        if not stats:
            return "—"
        return f"{stats['trades']} сд., {stats['wins']} в плюсе, в среднем {pct(stats['avg_net_pct'])}"

    lines += [
        "",
        "## Что дали дни, которые боевой пропускал",
        "",
        "| Дни | " + " | ".join(NO_SKIP_POLICIES[name].split(":")[0] for name in NO_SKIP_POLICIES) + " |",
        "|---|" + "---|" * len(NO_SKIP_POLICIES),
    ]
    order = ["дни, где боевой торговал", "все бывшие пропуски"]
    names = order + sorted({group for groups in report["groups"].values() for group in groups} - set(order))
    for group in names:
        lines.append(
            f"| {group} | " + " | ".join(group_cell(report["groups"][name].get(group)) for name in NO_SKIP_POLICIES) + " |"
        )
    for name, changes in report["changed_picks_on_live_days"].items():
        if changes:
            lines += [
                "",
                f"В дни, где боевой торговал, вариант «{NO_SKIP_POLICIES[name].split(':')[0]}» выбрал другое: "
                + ", ".join(f"{row['date']} {row['live']} → {row['variant']}" for row in changes)
                + ".",
            ]
    lines += ["", "Проскальзывание:"]
    for name in NO_SKIP_POLICIES:
        stress = variants[name]["slippage_stress"]
        lines.append(
            f"- {NO_SKIP_POLICIES[name].split(':')[0]}: "
            + ", ".join(f"{bps} б.п. {pct(row['return_pct'])}" for bps, row in stress.items())
            + "."
        )
    lines += [
        "",
        "## Все дни",
        "",
        "| Дата | Боевой (с пропусками) | Без пропусков | Всегда rank-1 |",
        "|---|---|---|---|",
    ]
    trades = {name: {row["date"]: row for row in rows} for name, rows in report["trades"].items()}

    def trade_cell(name: str, trade_date: str, rank: int | None = None) -> str:
        trade = trades[name].get(trade_date)
        if trade is None:
            return "нет сделки"
        suffix = f" (rank {rank})" if rank and rank > 1 else ""
        return f"{trade['symbol']} {trade['direction']}{suffix}: {trade['reason']} {pct(trade['net_return_pct'])}"

    rank1_days = {day["date"]: day for day in report["policies"]["rank1"]}
    for day in report["policies"]["no_skip"]:
        live_cell = (
            trade_cell("live", day["date"], day["live_selected"]["rank"])
            if day["live_selected"]
            else f"пропуск ({skip_category(day['live_skip_reason'])})"
        )
        no_skip_rank = day["selected"]["rank"] if day["selected"] else None
        rank1_day = rank1_days[day["date"]]
        rank1_rank = rank1_day["selected"]["rank"] if rank1_day["selected"] else None
        lines.append(
            f"| {day['date']} | {live_cell} | {trade_cell('no_skip', day['date'], no_skip_rank)} | "
            f"{trade_cell('rank1', day['date'], rank1_rank)} |"
        )
    lines += [
        "",
        "Воспроизведение: `python3 -m argonus.research.research_new_period_engine_a --no-skip` "
        "(анализатор перезапускается из кэша без API; догружаются только недостающие 5-мин сессии).",
    ]
    return "\n".join(lines) + "\n"


# ─── Pipelines ──────────────────────────────────────────────────────────────


def collect_period(start: date, end: date, output_dir: Path, workers: int) -> dict[str, Any]:
    client = TBankInvestClient(user_agent=USER_AGENT)
    calendar = fetch_calendar(client, start, end)
    if not calendar:
        raise RuntimeError(f"No completed sessions between {start} and {end}")
    progress(f"Сессий: {len(calendar)} ({calendar[0]} … {calendar[-1]})")

    shares = with_retry(client.list_moex_shares)
    generator_start = calendar[0] - timedelta(days=GENERATOR_LOOKBACK_DAYS)
    generator_end = calendar[-1] - timedelta(days=1)
    progress(f"Генератор: история {len(shares)} акций {generator_start} … {generator_end}")
    histories, errors = fetch_daily_map(
        client, {share.instrument_id: share.instrument_id for share in shares}, generator_start, generator_end, workers
    )
    if errors:
        progress(f"  ошибок загрузки: {len(errors)}")
    cached = CachedGeneratorClient(shares, histories, errors, generator_start, generator_end)
    texts = generate_watchlists(cached, calendar)
    paths = write_watchlists(texts, output_dir / "watchlists")

    symbols = analyzer_symbols(paths)
    progress(f"Анализатор: {len(symbols)} бумаг из watchlist'ов")
    stock_cache, index_candles, analyzer_errors = fetch_analyzer_data(client, symbols, calendar[0], calendar[-1], workers)
    if analyzer_errors:
        raise RuntimeError(f"Analyzer history failed: {analyzer_errors}")
    days = select(paths, stock_cache, index_candles)

    progress("5-мин сессии выбранных сделок …")
    sessions = fetch_selected_sessions(client, days)
    return {
        "schema_version": 1,
        "created_at": datetime.now(MOSCOW_TZ).isoformat(timespec="seconds"),
        "period": {"start": calendar[0].isoformat(), "end": calendar[-1].isoformat()},
        "calendar": [value.isoformat() for value in calendar],
        "universe_snapshot": [asdict(share) for share in shares],
        "generator_window": [generator_start.isoformat(), generator_end.isoformat()],
        "generator_histories": {key: [candle_to_row(c) for c in value] for key, value in histories.items()},
        "generator_errors": errors,
        "watchlists": texts,
        "analyzer_stock": {key: [candle_to_row(c) for c in value] for key, value in stock_cache.items()},
        "analyzer_index": [candle_to_row(c) for c in index_candles],
        "selection_days": days,
        "sessions": sessions,
        "artifact_hashes": october.artifact_hashes(),
    }


def describe(day: dict[str, Any]) -> str:
    selected = day.get("selected")
    if not selected:
        return f"пропуск ({skip_category(day['decision_reason'])})"
    suffix = f", rank {selected['rank']}" if selected["rank"] > 1 else ""
    return f"{selected['symbol']} {selected['direction']}{suffix}"


def validate_july(output_dir: Path, workers: int) -> dict[str, Any]:
    """Rebuild 2026-07-01..16 both ways and compare with the frozen 54-trade ledger."""
    client = TBankInvestClient(user_agent=USER_AGENT)
    calendar = fetch_calendar(client, VALIDATION_START, VALIDATION_END)
    stored = {
        day.isoformat(): JULY_DIR / f"watchlist_{day.month:02d}{day.day:02d}.txt"
        for day in calendar
    }
    missing = [str(path) for path in stored.values() if not path.is_file()]
    if missing:
        raise RuntimeError(f"Stored July watchlists missing: {missing}")

    shares = with_retry(client.list_moex_shares)
    generator_start = calendar[0] - timedelta(days=GENERATOR_LOOKBACK_DAYS)
    generator_end = calendar[-1] - timedelta(days=1)
    histories, errors = fetch_daily_map(
        client, {share.instrument_id: share.instrument_id for share in shares}, generator_start, generator_end, workers
    )
    texts = generate_watchlists(CachedGeneratorClient(shares, histories, errors, generator_start, generator_end), calendar)
    regenerated = write_watchlists(texts, output_dir / "validation_july" / "watchlists")

    symbols = sorted(set(analyzer_symbols(stored)) | set(analyzer_symbols(regenerated)))
    stock_cache, index_candles, analyzer_errors = fetch_analyzer_data(client, symbols, calendar[0], calendar[-1], workers)
    if analyzer_errors:
        raise RuntimeError(f"Analyzer history failed: {analyzer_errors}")
    stored_days = {day["date"]: day for day in select(stored, stock_cache, index_candles)}
    regenerated_days = {day["date"]: day for day in select(regenerated, stock_cache, index_candles)}

    _, ledger_54, _ = fixed_research.load_ledgers()
    frozen = {trade.date: trade for trade in ledger_54 if VALIDATION_START.isoformat() <= trade.date <= VALIDATION_END.isoformat()}

    def key(day: dict[str, Any]) -> tuple[str, str] | None:
        selected = day.get("selected")
        return (selected["symbol"], selected["direction"]) if selected else None

    rows = []
    stored_match = regenerated_match = identical = 0
    for trade_date in stored:
        frozen_trade = frozen.get(trade_date)
        frozen_key = (frozen_trade.symbol, frozen_trade.direction) if frozen_trade else None
        stored_match += key(stored_days[trade_date]) == frozen_key
        regenerated_match += key(regenerated_days[trade_date]) == frozen_key
        identical += stored[trade_date].read_text(encoding="utf-8") == texts[trade_date]
        rows.append(
            {
                "date": trade_date,
                "frozen": f"{frozen_trade.symbol} {frozen_trade.direction}" if frozen_trade else "пропуск",
                "stored": describe(stored_days[trade_date]),
                "regenerated": describe(regenerated_days[trade_date]),
            }
        )

    returns = []
    for trade_date, day in sorted(stored_days.items()):
        frozen_trade = frozen.get(trade_date)
        if not day.get("selected") or frozen_trade is None or key(day) != (frozen_trade.symbol, frozen_trade.direction):
            continue
        rows_5m = fetch_five_minute_session(client, day["selected"]["symbol"], date.fromisoformat(trade_date))
        trade = to_trade(day, {"rows": rows_5m, "session_sha256": october.canonical_sha256(rows_5m)})
        fresh = table_convention([trade])[0]["gross_return_pct"]
        returns.append({"date": trade_date, "symbol": trade.symbol, "fresh": fresh, "frozen": frozen_trade.gross_return_pct})

    return {
        "sessions": len(calendar),
        "watchlists_identical": identical,
        "stored_match": stored_match,
        "regenerated_match": regenerated_match,
        "days": rows,
        "returns": returns,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", type=date.fromisoformat, default=FIRST_NEW_SESSION)
    parser.add_argument("--end", type=date.fromisoformat, default=None, help="Последняя сессия (по умолчанию вчера по МСК).")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--offline", action="store_true", help="Собрать отчёт только из кэша, без API.")
    parser.add_argument("--validate-july", action="store_true", help="Сверить конвейер с замороженной книгой за 1–16 июля.")
    parser.add_argument("--no-skip", action="store_true", help="Сравнить с вариантами, которые торгуют каждый день.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    cache_path = output_dir / "market_cache.json.gz"
    validation_path = output_dir / "validation_july.json"

    if args.validate_july:
        validation = validate_july(output_dir, args.workers)
        validation_path.parent.mkdir(parents=True, exist_ok=True)
        validation_path.write_text(json.dumps(validation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("\n".join(validation_lines(validation)))
        return 0

    if args.offline or cache_path.exists():
        cache = read_json_gz(cache_path)
    else:
        end = args.end or (datetime.now(MOSCOW_TZ).date() - timedelta(days=1))
        cache = collect_period(args.start, end, output_dir, args.workers)
        write_json_gz(cache_path, cache)

    report = build_report(cache)
    validation = json.loads(validation_path.read_text(encoding="utf-8")) if validation_path.exists() else None
    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    text = markdown(report, validation)
    (output_dir / "REPORT.md").write_text(text, encoding="utf-8")

    if args.no_skip:
        no_skip_path = output_dir / "no_skip_cache.json.gz"
        if no_skip_path.exists():
            no_skip = read_json_gz(no_skip_path)
        else:
            no_skip = collect_no_skip(cache, output_dir, args.offline)
            write_json_gz(no_skip_path, no_skip)
        comparison = build_no_skip_report(cache, no_skip, report)
        (output_dir / "no_skip_report.json").write_text(
            json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
        )
        text = no_skip_markdown(comparison)
        (output_dir / "NO_SKIP_REPORT.md").write_text(text, encoding="utf-8")

    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
