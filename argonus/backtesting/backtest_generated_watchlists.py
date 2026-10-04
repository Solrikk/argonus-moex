#!/usr/bin/env python3
from __future__ import annotations

from argonus.paths import WATCHLIST_DIR

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Iterable

from argonus.market_data.tbank_market_data import DailyCandle, TBankInvestClient
from argonus.watchlists import watchlist_best_target as wbt


DIR_RE = re.compile(r"generated_watchlists_(\d{4})_(\d{2})$")
FILE_RE = re.compile(r"watchlist_(\d{2})(\d{2})\.txt$")


@dataclass(slots=True)
class DayResult:
    trade_date: str
    month: str
    path: str
    symbol: str | None
    direction: str | None
    best_target_label: str | None
    best_target_price: float | None
    best_target_hit: bool
    any_target_hit: bool
    highest_hit_target_label: str | None
    day_open: float | None
    day_low: float | None
    day_high: float | None
    day_close: float | None
    overall_score: float | None
    skipped_reason: str | None = None


@dataclass(slots=True)
class WatchlistEntry:
    trade_date: date
    path: str
    raw_text: str
    symbols: tuple[str, ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backtest для generated_watchlists_*: берет top pick из watchlist_best_target.py "
            "и проверяет, достиг ли он целей в тот же торговый день."
        )
    )
    parser.add_argument(
        "--root",
        default=str(WATCHLIST_DIR),
        help="Папка с generated_watchlists_*. По умолчанию data/watchlists.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Печатать полный отчет в JSON.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Количество потоков для загрузки истории и расчета. По умолчанию 4.",
    )
    return parser.parse_args()


def discover_watchlists(root: str) -> list[WatchlistEntry]:
    discovered: list[WatchlistEntry] = []
    for entry in sorted(os.listdir(root)):
        directory_match = DIR_RE.fullmatch(entry)
        if not directory_match:
            continue

        year = int(directory_match.group(1))
        month_from_dir = int(directory_match.group(2))
        directory_path = os.path.join(root, entry)
        if not os.path.isdir(directory_path):
            continue

        for filename in sorted(os.listdir(directory_path)):
            file_match = FILE_RE.fullmatch(filename)
            if not file_match:
                continue

            month = int(file_match.group(1))
            day = int(file_match.group(2))
            if month != month_from_dir:
                raise RuntimeError(
                    f"Месяц в директории и файле не совпадает: {directory_path}/{filename}"
                )

            trade_date = date(year, month, day)
            if trade_date.weekday() >= 5:
                continue

            path = os.path.join(directory_path, filename)
            with open(path, "r", encoding="utf-8") as handle:
                raw_text = handle.read()
            symbols = tuple(idea.symbol for idea in wbt.parse_watchlist(raw_text))
            discovered.append(
                WatchlistEntry(
                    trade_date=trade_date,
                    path=path,
                    raw_text=raw_text,
                    symbols=symbols,
                )
            )
    return sorted(discovered, key=lambda item: (item.trade_date, item.path))


def candle_hits_target(direction: str, candle: DailyCandle, target_price: float) -> bool:
    if direction == "long":
        return candle.high >= target_price
    if direction == "short":
        return candle.low <= target_price
    return False


def highest_hit_target_label(direction: str, candle: DailyCandle, target_prices: Iterable[tuple[int, float]]) -> str | None:
    hit_indices = [
        index
        for index, price in target_prices
        if candle_hits_target(direction, candle, price)
    ]
    if not hit_indices:
        return None
    return f"T{max(hit_indices)}"


def build_candle_cache(workers: int, watchlists: list[WatchlistEntry]) -> tuple[dict[str, list[DailyCandle]], list[DailyCandle]]:
    if not watchlists:
        return {}, []

    all_symbols = sorted({symbol for item in watchlists for symbol in item.symbols})
    min_trade_date = min(item.trade_date for item in watchlists)
    max_trade_date = max(item.trade_date for item in watchlists)
    history_start = min_trade_date - timedelta(days=500)
    history_end = max_trade_date

    def fetch_symbol_history(symbol: str) -> tuple[str, list[DailyCandle]]:
        client = TBankInvestClient(user_agent="watchlist-backtest/1.0")
        instrument_id = client.resolve_share_instrument_id(symbol, wbt.DEFAULT_BOARD)
        candles = client.get_daily_candles(
            instrument_id,
            start_date=history_start,
            end_date=history_end,
        )
        return symbol, candles

    stock_cache: dict[str, list[DailyCandle]] = {}
    with ThreadPoolExecutor(max_workers=max(workers, 1)) as executor:
        for symbol, candles in executor.map(fetch_symbol_history, all_symbols):
            stock_cache[symbol] = candles

    client = TBankInvestClient(user_agent="watchlist-backtest/1.0")
    index_instrument_id = client.resolve_index_instrument_id(
        wbt.DEFAULT_INDEX_SYMBOL,
        class_code_hint=wbt.DEFAULT_INDEX_BOARD,
    )
    index_candles = client.get_daily_candles(
        index_instrument_id,
        start_date=history_start,
        end_date=history_end,
    )
    return stock_cache, index_candles


def patch_watchlist_data_access(
    stock_cache: dict[str, list[DailyCandle]],
    index_candles: list[DailyCandle],
) -> None:
    def slice_sessions(candles: list[DailyCandle], as_of: date, limit: int) -> list[wbt.SessionData]:
        filtered = [item for item in candles if item.trade_date <= as_of - timedelta(days=1)]
        if not filtered:
            raise wbt.MarketDataError(f"Нет дневных свечей до {as_of.isoformat()}.")
        return wbt.candles_to_sessions(filtered[-limit:])

    def cached_fetch_stock_sessions(
        symbol: str,
        as_of: date,
        limit: int = 180,
        lookback_days: int = 420,
        board: str = wbt.DEFAULT_BOARD,
    ) -> list[wbt.SessionData]:
        del lookback_days, board
        candles = stock_cache.get(symbol)
        if not candles:
            raise wbt.MarketDataError(f"По {symbol} нет свечей в кэше.")
        return slice_sessions(candles, as_of, limit)

    def cached_fetch_index_sessions(
        symbol: str,
        as_of: date,
        limit: int = 40,
        lookback_days: int = 120,
        board: str = wbt.DEFAULT_INDEX_BOARD,
    ) -> list[wbt.SessionData]:
        del lookback_days, board
        if symbol != wbt.DEFAULT_INDEX_SYMBOL:
            raise wbt.MarketDataError(f"В кэше нет индекса {symbol}.")
        return slice_sessions(index_candles, as_of, limit)

    wbt.fetch_stock_sessions = cached_fetch_stock_sessions
    wbt.fetch_recent_stock_sessions = cached_fetch_stock_sessions
    wbt.fetch_index_sessions = cached_fetch_index_sessions
    wbt.fetch_previous_index_sessions = cached_fetch_index_sessions


def fetch_trade_day_candle(stock_cache: dict[str, list[DailyCandle]], symbol: str, trade_date: date) -> DailyCandle:
    candles = stock_cache.get(symbol)
    if not candles:
        raise RuntimeError(f"По {symbol} нет кэша свечей.")
    for candle in candles:
        if candle.trade_date == trade_date:
            return candle
    raise RuntimeError(f"По {symbol} нет дневной свечи на {trade_date.isoformat()}.")


def evaluate_watchlist(stock_cache: dict[str, list[DailyCandle]], entry: WatchlistEntry) -> DayResult:
    ideas_by_symbol = {idea.symbol: idea for idea in wbt.parse_watchlist(entry.raw_text)}
    analyses = wbt.analyze_watchlist(entry.raw_text, entry.trade_date)
    if not analyses:
        return DayResult(
            trade_date=entry.trade_date.isoformat(),
            month=entry.trade_date.strftime("%Y-%m"),
            path=entry.path,
            symbol=None,
            direction=None,
            best_target_label=None,
            best_target_price=None,
            best_target_hit=False,
            any_target_hit=False,
            highest_hit_target_label=None,
            day_open=None,
            day_low=None,
            day_high=None,
            day_close=None,
            overall_score=None,
            skipped_reason="Пустой результат анализа.",
        )

    top = analyses[0]
    if top.skipped_reason:
        return DayResult(
            trade_date=entry.trade_date.isoformat(),
            month=entry.trade_date.strftime("%Y-%m"),
            path=entry.path,
            symbol=top.symbol,
            direction=top.direction,
            best_target_label=top.best_target.label,
            best_target_price=top.best_target.price,
            best_target_hit=False,
            any_target_hit=False,
            highest_hit_target_label=None,
            day_open=None,
            day_low=None,
            day_high=None,
            day_close=None,
            overall_score=top.overall_score,
            skipped_reason=top.skipped_reason,
        )

    idea = ideas_by_symbol.get(top.symbol)
    if idea is None:
        raise RuntimeError(f"В исходном файле не найдена идея для {top.symbol}: {entry.path}")

    candle = fetch_trade_day_candle(stock_cache, top.symbol, entry.trade_date)
    all_targets = sorted((target.index, target.price) for target in idea.targets)
    best_target_hit = top.best_target.label.startswith("T") and candle_hits_target(
        top.direction,
        candle,
        top.best_target.price,
    )
    reached_label = highest_hit_target_label(top.direction, candle, all_targets)

    return DayResult(
        trade_date=entry.trade_date.isoformat(),
        month=entry.trade_date.strftime("%Y-%m"),
        path=entry.path,
        symbol=top.symbol,
        direction=top.direction,
        best_target_label=top.best_target.label,
        best_target_price=top.best_target.price,
        best_target_hit=bool(best_target_hit),
        any_target_hit=reached_label is not None,
        highest_hit_target_label=reached_label,
        day_open=candle.open,
        day_low=candle.low,
        day_high=candle.high,
        day_close=candle.close,
        overall_score=top.overall_score,
        skipped_reason=None,
    )


def summarize(results: list[DayResult]) -> dict[str, object]:
    monthly: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "days": 0,
            "any_target_hit_days": 0,
            "best_target_hit_days": 0,
            "miss_days": 0,
            "skipped_days": 0,
        }
    )

    totals = {
        "days": 0,
        "any_target_hit_days": 0,
        "best_target_hit_days": 0,
        "miss_days": 0,
        "skipped_days": 0,
    }

    miss_days: list[dict[str, object]] = []

    for item in results:
        bucket = monthly[item.month]
        bucket["days"] += 1
        totals["days"] += 1

        if item.skipped_reason:
            bucket["skipped_days"] += 1
            totals["skipped_days"] += 1
            miss_days.append(
                {
                    "trade_date": item.trade_date,
                    "symbol": item.symbol,
                    "reason": item.skipped_reason,
                    "path": item.path,
                }
            )
            continue

        if item.any_target_hit:
            bucket["any_target_hit_days"] += 1
            totals["any_target_hit_days"] += 1
        else:
            bucket["miss_days"] += 1
            totals["miss_days"] += 1
            miss_days.append(
                {
                    "trade_date": item.trade_date,
                    "symbol": item.symbol,
                    "best_target": item.best_target_label,
                    "best_target_price": item.best_target_price,
                    "day_low": item.day_low,
                    "day_high": item.day_high,
                    "path": item.path,
                }
            )

        if item.best_target_hit:
            bucket["best_target_hit_days"] += 1
            totals["best_target_hit_days"] += 1

    return {
        "totals": totals,
        "monthly": dict(sorted(monthly.items())),
        "miss_days": miss_days,
        "results": [
            asdict(item)
            for item in sorted(results, key=lambda value: (value.trade_date, value.path))
        ],
    }


def format_percent(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0.0%"
    return f"{numerator / denominator * 100.0:.1f}%"


def format_text_report(report: dict[str, object]) -> str:
    totals = report["totals"]
    monthly = report["monthly"]
    miss_days = report["miss_days"]

    lines = [
        "Общий итог",
        (
            f"Торговых дней: {totals['days']} | "
            f"top pick hit any target: {totals['any_target_hit_days']} "
            f"({format_percent(totals['any_target_hit_days'], totals['days'])}) | "
            f"top pick hit best target: {totals['best_target_hit_days']} "
            f"({format_percent(totals['best_target_hit_days'], totals['days'])}) | "
            f"miss days: {totals['miss_days']} | "
            f"skipped: {totals['skipped_days']}"
        ),
        "",
        "По месяцам",
    ]

    for month, stats in monthly.items():
        lines.append(
            (
                f"{month}: days={stats['days']} | "
                f"any_target={stats['any_target_hit_days']} "
                f"({format_percent(stats['any_target_hit_days'], stats['days'])}) | "
                f"best_target={stats['best_target_hit_days']} "
                f"({format_percent(stats['best_target_hit_days'], stats['days'])}) | "
                f"miss={stats['miss_days']} | skipped={stats['skipped_days']}"
            )
        )

    lines.append("")
    lines.append("Miss days")
    if not miss_days:
        lines.append("Нет miss days.")
    else:
        for item in miss_days:
            reason = item.get("reason")
            if reason:
                lines.append(
                    f"{item['trade_date']}: {item.get('symbol') or 'N/A'} | skip | {reason}"
                )
            else:
                lines.append(
                    (
                        f"{item['trade_date']}: {item['symbol']} | best={item['best_target']} "
                        f"@{item['best_target_price']:.3f} | "
                        f"day_low={item['day_low']:.3f} | day_high={item['day_high']:.3f}"
                    )
                )

    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    watchlists = discover_watchlists(args.root)
    if not watchlists:
        print("Не найдено ни одного generated_watchlists_*/watchlist_*.txt.", file=sys.stderr)
        return 1

    stock_cache, index_candles = build_candle_cache(args.workers, watchlists)
    patch_watchlist_data_access(stock_cache, index_candles)

    # Прогреваем ВСЕ reranker-модели до запуска потоков: ленивые загрузчики
    # не потокобезопасны, и при workers>1 часть дней могла молча считаться
    # без reranker'а (нестабильные цифры между прогонами).
    wbt.load_same_day_top_reranker_model()
    wbt.load_same_day_top_winner_reranker_model()
    wbt.load_same_day_top_t2plus_reranker_model()

    def safe_evaluate(entry: WatchlistEntry) -> DayResult:
        try:
            return evaluate_watchlist(stock_cache, entry)
        except Exception as exc:  # noqa: BLE001
            return DayResult(
                trade_date=entry.trade_date.isoformat(),
                month=entry.trade_date.strftime("%Y-%m"),
                path=entry.path,
                symbol=None,
                direction=None,
                best_target_label=None,
                best_target_price=None,
                best_target_hit=False,
                any_target_hit=False,
                highest_hit_target_label=None,
                day_open=None,
                day_low=None,
                day_high=None,
                day_close=None,
                overall_score=None,
                skipped_reason=str(exc),
            )

    with ThreadPoolExecutor(max_workers=max(args.workers, 1)) as executor:
        results = list(executor.map(safe_evaluate, watchlists))

    report = summarize(results)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_text_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
