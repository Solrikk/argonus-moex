#!/usr/bin/env python3
from __future__ import annotations

from argonus.paths import WATCHLIST_DIR

import argparse
import json
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dt_time
from pathlib import Path

from argonus.backtesting import backtest_generated_watchlists as bgw
from argonus.market_data.tbank_market_data import (
    MOSCOW_TZ,
    UTC,
    TBankApiError,
    TBankInvestClient,
    parse_api_datetime,
    quotation_to_float,
)
from argonus.watchlists import watchlist_best_target as wbt


FILE_RE = re.compile(r"watchlist_(\d{2})(\d{2})\.txt$")
ENTRY_TIME_DEFAULT = "07:05"
STOP_LOSS_PCT_DEFAULT = 1.0
START_CAPITAL_DEFAULT = 900_000.0
REQUEST_PAUSE_SECONDS = 0.20
RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)
WIN_TARGET_LABELS = {"T2", "T3", "T4"}


@dataclass(slots=True)
class MinuteCandle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(slots=True)
class TopPick:
    trade_date: date
    path: str
    symbol: str
    direction: str
    best_target_label: str
    best_target_price: float
    overall_score: float
    target_prices: dict[str, float]


@dataclass(slots=True)
class SimulatedTrade:
    trade_date: str
    symbol: str | None
    direction: str | None
    watchlist_path: str
    best_target_label: str | None
    best_target_price: float | None
    allowed_target: bool
    entry_time: str | None
    entry_price: float | None
    stop_price: float | None
    highest_reached_label: str | None
    exit_time: str | None
    exit_price: float | None
    exit_reason: str
    return_pct: float
    capital_before: float
    capital_after: float
    pnl_rub: float
    ambiguous_bar: bool
    notes: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Симуляция сделки по top pick из watchlist_best_target.py: "
            "вход в 07:05, стоп 1%, тейк по выбранной алгоритмом цели."
        )
    )
    parser.add_argument(
        "--month-dir",
        default=str(WATCHLIST_DIR / "generated_watchlists_2026_04"),
        help="Папка месяца с watchlist_*.txt. По умолчанию generated_watchlists_2026_04.",
    )
    parser.add_argument(
        "--entry-time",
        default=ENTRY_TIME_DEFAULT,
        help="Время входа по Москве в формате HH:MM. По умолчанию 07:05.",
    )
    parser.add_argument(
        "--stop-loss-pct",
        type=float,
        default=STOP_LOSS_PCT_DEFAULT,
        help="Размер стоп-лосса в процентах. По умолчанию 1.0.",
    )
    parser.add_argument(
        "--start-capital",
        type=float,
        default=START_CAPITAL_DEFAULT,
        help="Стартовый капитал. По умолчанию 900000.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Потоки для кэша дневных свечей. По умолчанию 4.",
    )
    parser.add_argument(
        "--json-output",
        help=(
            "Куда сохранить JSON. По умолчанию файл создается внутри month-dir "
            "с именем intraday_strategy_0705_sl1pct.json."
        ),
    )
    parser.add_argument(
        "--text-output",
        help=(
            "Куда сохранить текст. По умолчанию файл создается внутри month-dir "
            "с именем intraday_strategy_0705_sl1pct.txt."
        ),
    )
    return parser.parse_args()


def progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def parse_entry_time(raw: str) -> dt_time:
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except ValueError as exc:
        raise ValueError(f"Не удалось разобрать время входа: {raw}") from exc


def discover_watchlists(month_dir: Path) -> list[bgw.WatchlistEntry]:
    if not month_dir.is_dir():
        raise RuntimeError(f"Папка месяца не найдена: {month_dir}")

    dir_match = bgw.DIR_RE.fullmatch(month_dir.name)
    if not dir_match:
        raise RuntimeError(
            f"Папка должна называться как generated_watchlists_YYYY_MM: {month_dir}"
        )

    year = int(dir_match.group(1))
    month_from_dir = int(dir_match.group(2))
    watchlists: list[bgw.WatchlistEntry] = []

    for path in sorted(month_dir.glob("watchlist_*.txt")):
        match = FILE_RE.fullmatch(path.name)
        if not match:
            continue

        month = int(match.group(1))
        day = int(match.group(2))
        if month != month_from_dir:
            raise RuntimeError(f"Месяц в имени файла не совпадает с директорией: {path}")

        trade_date = date(year, month, day)
        if trade_date.weekday() >= 5:
            continue

        raw_text = path.read_text(encoding="utf-8")
        symbols = tuple(idea.symbol for idea in wbt.parse_watchlist(raw_text))
        watchlists.append(
            bgw.WatchlistEntry(
                trade_date=trade_date,
                path=str(path),
                raw_text=raw_text,
                symbols=symbols,
            )
        )

    return watchlists


def resolve_top_pick(entry: bgw.WatchlistEntry) -> TopPick | None:
    analyses = wbt.analyze_watchlist(entry.raw_text, entry.trade_date)
    if not analyses:
        return None

    top = analyses[0]
    if top.skipped_reason:
        return TopPick(
            trade_date=entry.trade_date,
            path=entry.path,
            symbol=top.symbol,
            direction=top.direction,
            best_target_label=top.best_target.label,
            best_target_price=top.best_target.price,
            overall_score=top.overall_score,
            target_prices={},
        )

    ideas_by_symbol = {idea.symbol: idea for idea in wbt.parse_watchlist(entry.raw_text)}
    idea = ideas_by_symbol.get(top.symbol)
    if idea is None:
        raise RuntimeError(f"В исходном файле не найдена идея для {top.symbol}: {entry.path}")

    target_prices = {
        f"T{target.index}": target.price
        for target in sorted(idea.targets, key=lambda item: item.index)
    }
    return TopPick(
        trade_date=entry.trade_date,
        path=entry.path,
        symbol=top.symbol,
        direction=top.direction,
        best_target_label=top.best_target.label,
        best_target_price=top.best_target.price,
        overall_score=top.overall_score,
        target_prices=target_prices,
    )


def fetch_intraday_candles(
    client: TBankInvestClient,
    instrument_id: str,
    trade_date: date,
) -> list[MinuteCandle]:
    start_dt = datetime.combine(trade_date, dt_time(7, 0), MOSCOW_TZ).astimezone(UTC)
    end_dt = datetime.combine(trade_date, dt_time(23, 59), MOSCOW_TZ).astimezone(UTC)
    payload = {
        "instrumentId": instrument_id,
        "from": start_dt.isoformat().replace("+00:00", "Z"),
        "to": end_dt.isoformat().replace("+00:00", "Z"),
        "interval": "CANDLE_INTERVAL_1_MIN",
        "candleSourceType": "CANDLE_SOURCE_EXCHANGE",
    }

    for attempt, delay in enumerate((0.0, *RETRY_DELAYS), start=1):
        if delay > 0:
            time.sleep(delay)
        try:
            response = client._post(
                "tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles",
                payload,
            )
            raw_candles = response.get("candles") or response.get("historicalCandles") or []
            candles: list[MinuteCandle] = []
            for item in raw_candles:
                if item.get("isComplete") is False:
                    continue
                raw_time = item.get("time")
                if not raw_time:
                    continue
                timestamp = parse_api_datetime(str(raw_time)).astimezone(MOSCOW_TZ)
                if timestamp.date() != trade_date:
                    continue
                candles.append(
                    MinuteCandle(
                        timestamp=timestamp,
                        open=quotation_to_float(item.get("open")),
                        high=quotation_to_float(item.get("high")),
                        low=quotation_to_float(item.get("low")),
                        close=quotation_to_float(item.get("close")),
                    )
                )
            candles.sort(key=lambda item: item.timestamp)
            time.sleep(REQUEST_PAUSE_SECONDS)
            return candles
        except TBankApiError as exc:
            if "HTTP 429" not in str(exc) or attempt > len(RETRY_DELAYS):
                raise

    raise RuntimeError("Не удалось загрузить intraday-свечи после повторов.")


def select_entry_candle(candles: list[MinuteCandle], entry_time: dt_time) -> MinuteCandle | None:
    for candle in candles:
        if candle.timestamp.time() >= entry_time:
            return candle
    return None


def candle_hits_price(direction: str, candle: MinuteCandle, price: float) -> bool:
    if direction == "long":
        return candle.high >= price
    if direction == "short":
        return candle.low <= price
    raise RuntimeError(f"Неизвестное направление: {direction}")


def highest_reached_label(direction: str, candles: list[MinuteCandle], target_prices: dict[str, float]) -> str | None:
    reached: list[int] = []
    for label, price in target_prices.items():
        if any(candle_hits_price(direction, candle, price) for candle in candles):
            reached.append(int(label[1:]))
    if not reached:
        return None
    return f"T{max(reached)}"


def compute_stop_price(direction: str, entry_price: float, stop_loss_pct: float) -> float:
    multiplier = stop_loss_pct / 100.0
    if direction == "long":
        return entry_price * (1.0 - multiplier)
    if direction == "short":
        return entry_price * (1.0 + multiplier)
    raise RuntimeError(f"Неизвестное направление: {direction}")


def compute_return_pct(direction: str, entry_price: float, exit_price: float) -> float:
    if direction == "long":
        return (exit_price / entry_price - 1.0) * 100.0
    if direction == "short":
        return (entry_price / exit_price - 1.0) * 100.0
    raise RuntimeError(f"Неизвестное направление: {direction}")


def simulate_trade(
    top_pick: TopPick | None,
    intraday_candles: list[MinuteCandle],
    entry_time: dt_time,
    capital_before: float,
    stop_loss_pct: float,
    market_data_ready: bool = True,
) -> SimulatedTrade:
    if top_pick is None:
        return SimulatedTrade(
            trade_date="N/A",
            symbol=None,
            direction=None,
            watchlist_path="N/A",
            best_target_label=None,
            best_target_price=None,
            allowed_target=False,
            entry_time=None,
            entry_price=None,
            stop_price=None,
            highest_reached_label=None,
            exit_time=None,
            exit_price=None,
            exit_reason="skip_no_analysis",
            return_pct=0.0,
            capital_before=capital_before,
            capital_after=capital_before,
            pnl_rub=0.0,
            ambiguous_bar=False,
            notes="Пустой результат анализа.",
        )

    if not intraday_candles:
        if not market_data_ready:
            return SimulatedTrade(
                trade_date=top_pick.trade_date.isoformat(),
                symbol=top_pick.symbol,
                direction=top_pick.direction,
                watchlist_path=top_pick.path,
                best_target_label=top_pick.best_target_label,
                best_target_price=top_pick.best_target_price,
                allowed_target=top_pick.best_target_label in WIN_TARGET_LABELS,
                entry_time=None,
                entry_price=None,
                stop_price=None,
                highest_reached_label=None,
                exit_time=None,
                exit_price=None,
                exit_reason="skip_no_market_data_yet",
                return_pct=0.0,
                capital_before=capital_before,
                capital_after=capital_before,
                pnl_rub=0.0,
                ambiguous_bar=False,
                notes="На эту дату в API еще нет готовых рыночных данных.",
            )
        return SimulatedTrade(
            trade_date=top_pick.trade_date.isoformat(),
            symbol=top_pick.symbol,
            direction=top_pick.direction,
            watchlist_path=top_pick.path,
            best_target_label=top_pick.best_target_label,
            best_target_price=top_pick.best_target_price,
            allowed_target=top_pick.best_target_label in WIN_TARGET_LABELS,
            entry_time=None,
            entry_price=None,
            stop_price=None,
            highest_reached_label=None,
            exit_time=None,
            exit_price=None,
            exit_reason="loss_no_intraday_data",
            return_pct=-stop_loss_pct,
            capital_before=capital_before,
            capital_after=capital_before * (1.0 - stop_loss_pct / 100.0),
            pnl_rub=capital_before * (-stop_loss_pct / 100.0),
            ambiguous_bar=False,
            notes="Нет intraday-свечей на дату.",
        )

    entry_candle = select_entry_candle(intraday_candles, entry_time)
    if entry_candle is None:
        return SimulatedTrade(
            trade_date=top_pick.trade_date.isoformat(),
            symbol=top_pick.symbol,
            direction=top_pick.direction,
            watchlist_path=top_pick.path,
            best_target_label=top_pick.best_target_label,
            best_target_price=top_pick.best_target_price,
            allowed_target=top_pick.best_target_label in WIN_TARGET_LABELS,
            entry_time=None,
            entry_price=None,
            stop_price=None,
            highest_reached_label=None,
            exit_time=None,
            exit_price=None,
            exit_reason="loss_no_entry_bar",
            return_pct=-stop_loss_pct,
            capital_before=capital_before,
            capital_after=capital_before * (1.0 - stop_loss_pct / 100.0),
            pnl_rub=capital_before * (-stop_loss_pct / 100.0),
            ambiguous_bar=False,
            notes="После времени входа нет ни одной минутной свечи.",
        )

    post_entry_candles = [
        candle
        for candle in intraday_candles
        if candle.timestamp >= entry_candle.timestamp
    ]
    highest_label = highest_reached_label(
        top_pick.direction,
        post_entry_candles,
        top_pick.target_prices,
    )

    entry_price = entry_candle.open
    stop_price = compute_stop_price(top_pick.direction, entry_price, stop_loss_pct)
    capital_after = capital_before
    exit_price: float | None = None
    exit_time: str | None = None
    exit_reason = "loss_end_of_day"
    trade_return_pct = -stop_loss_pct
    ambiguous_bar = False
    notes: str | None = None

    allowed_target = top_pick.best_target_label in WIN_TARGET_LABELS
    target_price = top_pick.best_target_price if allowed_target else None

    if allowed_target and target_price is not None:
        for candle in post_entry_candles:
            target_hit = candle_hits_price(top_pick.direction, candle, target_price)
            stop_hit = candle_hits_price(
                "short" if top_pick.direction == "long" else "long",
                candle,
                stop_price,
            )

            if target_hit and stop_hit:
                ambiguous_bar = True
                exit_price = stop_price
                exit_time = candle.timestamp.isoformat(timespec="minutes")
                exit_reason = "loss_ambiguous_bar_stop_first"
                trade_return_pct = -stop_loss_pct
                break

            if stop_hit:
                exit_price = stop_price
                exit_time = candle.timestamp.isoformat(timespec="minutes")
                exit_reason = "loss_stop_loss"
                trade_return_pct = -stop_loss_pct
                break

            if target_hit:
                exit_price = target_price
                exit_time = candle.timestamp.isoformat(timespec="minutes")
                exit_reason = f"win_{top_pick.best_target_label.lower()}"
                trade_return_pct = compute_return_pct(
                    top_pick.direction,
                    entry_price,
                    target_price,
                )
                break
    else:
        exit_reason = "loss_disallowed_best_target"
        notes = "Алгоритм выбрал T1, а по правилу стратегии это считается проигрышем."

    if exit_price is None:
        exit_price = stop_price
        exit_time = post_entry_candles[-1].timestamp.isoformat(timespec="minutes")
        if notes is None:
            notes = "Цель не достигнута до конца дня; по правилу стратегии день засчитан как stop-loss."

    capital_after = capital_before * (1.0 + trade_return_pct / 100.0)
    pnl_rub = capital_after - capital_before

    return SimulatedTrade(
        trade_date=top_pick.trade_date.isoformat(),
        symbol=top_pick.symbol,
        direction=top_pick.direction,
        watchlist_path=top_pick.path,
        best_target_label=top_pick.best_target_label,
        best_target_price=top_pick.best_target_price,
        allowed_target=allowed_target,
        entry_time=entry_candle.timestamp.isoformat(timespec="minutes"),
        entry_price=entry_price,
        stop_price=stop_price,
        highest_reached_label=highest_label,
        exit_time=exit_time,
        exit_price=exit_price,
        exit_reason=exit_reason,
        return_pct=trade_return_pct,
        capital_before=capital_before,
        capital_after=capital_after,
        pnl_rub=pnl_rub,
        ambiguous_bar=ambiguous_bar,
        notes=notes,
    )


def build_report(
    month_dir: Path,
    trades: list[SimulatedTrade],
    entry_time: dt_time,
    stop_loss_pct: float,
    start_capital: float,
) -> dict[str, object]:
    wins = [trade for trade in trades if trade.exit_reason.startswith("win_")]
    losses = [trade for trade in trades if trade.exit_reason.startswith("loss_")]
    skips = [trade for trade in trades if trade.exit_reason.startswith("skip_")]
    reached_counter = Counter(trade.highest_reached_label or "MISS" for trade in trades)
    chosen_counter = Counter(trade.best_target_label or "N/A" for trade in trades)
    exit_counter = Counter(trade.exit_reason for trade in trades)
    ambiguous_count = sum(1 for trade in trades if trade.ambiguous_bar)
    final_capital = trades[-1].capital_after if trades else start_capital

    return {
        "month_dir": str(month_dir),
        "config": {
            "entry_time_msk": entry_time.strftime("%H:%M"),
            "stop_loss_pct": stop_loss_pct,
            "start_capital": start_capital,
            "full_capital_each_trade": True,
            "win_target_labels": sorted(WIN_TARGET_LABELS),
            "loss_if_best_target_is_t1_or_if_day_ends_t1_miss": True,
            "same_bar_target_and_stop_policy": "stop_first",
        },
        "summary": {
            "days": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "skips": len(skips),
            "win_rate_pct": (len(wins) / len(trades) * 100.0) if trades else 0.0,
            "final_capital": final_capital,
            "total_return_pct": (
                (final_capital / start_capital - 1.0) * 100.0 if start_capital else 0.0
            ),
            "total_pnl_rub": final_capital - start_capital,
            "ambiguous_bars": ambiguous_count,
        },
        "chosen_best_targets": dict(sorted(chosen_counter.items())),
        "highest_reached_after_entry": dict(sorted(reached_counter.items())),
        "exit_reasons": dict(sorted(exit_counter.items())),
        "days_list": [asdict(trade) for trade in trades],
    }


def format_money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ")


def format_report_text(report: dict[str, object]) -> str:
    summary = report["summary"]
    config = report["config"]
    chosen_best_targets = report["chosen_best_targets"]
    highest_reached = report["highest_reached_after_entry"]
    exit_reasons = report["exit_reasons"]
    rows = report["days_list"]

    lines = [
        "Интрадей-симуляция стратегии",
        (
            f"Папка: {report['month_dir']} | "
            f"вход={config['entry_time_msk']} | "
            f"SL={config['stop_loss_pct']:.2f}% | "
            f"старт={format_money(config['start_capital'])}"
        ),
        (
            f"Дней={summary['days']} | wins={summary['wins']} | losses={summary['losses']} | "
            f"skips={summary['skips']} | win_rate={summary['win_rate_pct']:.1f}% | "
            f"итог={format_money(summary['final_capital'])} | "
            f"PnL={format_money(summary['total_pnl_rub'])} | "
            f"return={summary['total_return_pct']:.2f}%"
        ),
        "",
        "Что выбирал алгоритм",
    ]

    for label, count in chosen_best_targets.items():
        lines.append(f"{label}: {count}")

    lines.extend(["", "До какого уровня доходил день после входа"])
    for label, count in highest_reached.items():
        lines.append(f"{label}: {count}")

    lines.extend(["", "Причины выхода"])
    for label, count in exit_reasons.items():
        lines.append(f"{label}: {count}")

    lines.extend(["", "По дням"])
    for row in rows:
        lines.append(
            (
                f"{row['trade_date']}: {row['symbol'] or 'N/A'} {row['direction'] or 'N/A'} | "
                f"best={row['best_target_label'] or 'N/A'} | "
                f"reached={row['highest_reached_label'] or 'MISS'} | "
                f"entry={row['entry_price']:.3f} | "
                f"exit={row['exit_price']:.3f} | "
                f"reason={row['exit_reason']} | "
                f"ret={row['return_pct']:.2f}% | "
                f"cap={format_money(row['capital_after'])}"
            )
            if row["entry_price"] is not None and row["exit_price"] is not None
            else (
                f"{row['trade_date']}: {row['symbol'] or 'N/A'} {row['direction'] or 'N/A'} | "
                f"best={row['best_target_label'] or 'N/A'} | "
                f"reached={row['highest_reached_label'] or 'MISS'} | "
                f"reason={row['exit_reason']} | "
                f"ret={row['return_pct']:.2f}% | "
                f"cap={format_money(row['capital_after'])}"
            )
        )

    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    month_dir = Path(args.month_dir).resolve()
    entry_time = parse_entry_time(args.entry_time)
    watchlists = discover_watchlists(month_dir)
    if not watchlists:
        print("Не найдено ни одного watchlist_*.txt.", file=sys.stderr)
        return 1

    progress(f"Найдено {len(watchlists)} вотчлистов в {month_dir.name}")
    stock_cache, index_candles = bgw.build_candle_cache(args.workers, watchlists)
    bgw.patch_watchlist_data_access(stock_cache, index_candles)
    wbt.load_same_day_top_reranker_model()
    latest_daily_dates = {
        symbol: max(candle.trade_date for candle in candles)
        for symbol, candles in stock_cache.items()
        if candles
    }

    top_picks: list[TopPick | None] = []
    for entry in watchlists:
        progress(f"Анализ {entry.trade_date.isoformat()} {Path(entry.path).name}")
        top_picks.append(resolve_top_pick(entry))

    client = TBankInvestClient(user_agent="intraday-watchlist-simulator/1.0")
    instrument_ids: dict[str, str] = {}
    capital = args.start_capital
    trades: list[SimulatedTrade] = []

    for index, top_pick in enumerate(top_picks, start=1):
        if top_pick is None:
            trades.append(
                simulate_trade(
                    top_pick=None,
                    intraday_candles=[],
                    entry_time=entry_time,
                    capital_before=capital,
                    stop_loss_pct=args.stop_loss_pct,
                )
            )
            continue

        progress(
            f"[{index}/{len(top_picks)}] Intraday {top_pick.trade_date.isoformat()} {top_pick.symbol}"
        )
        instrument_id = instrument_ids.get(top_pick.symbol)
        if instrument_id is None:
            instrument_id = client.resolve_share_instrument_id(top_pick.symbol, wbt.DEFAULT_BOARD)
            instrument_ids[top_pick.symbol] = instrument_id
        candles = fetch_intraday_candles(client, instrument_id, top_pick.trade_date)
        trade = simulate_trade(
            top_pick=top_pick,
            intraday_candles=candles,
            entry_time=entry_time,
            capital_before=capital,
            stop_loss_pct=args.stop_loss_pct,
            market_data_ready=top_pick.trade_date <= latest_daily_dates.get(top_pick.symbol, top_pick.trade_date),
        )
        trades.append(trade)
        capital = trade.capital_after

    report = build_report(
        month_dir=month_dir,
        trades=trades,
        entry_time=entry_time,
        stop_loss_pct=args.stop_loss_pct,
        start_capital=args.start_capital,
    )
    text_report = format_report_text(report)

    default_base = f"intraday_strategy_{entry_time.strftime('%H%M')}_sl{args.stop_loss_pct:g}pct"
    json_output = Path(args.json_output).resolve() if args.json_output else month_dir / f"{default_base}.json"
    text_output = Path(args.text_output).resolve() if args.text_output else month_dir / f"{default_base}.txt"

    json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    text_output.write_text(text_report + "\n", encoding="utf-8")

    print(text_report)
    progress(f"Сохранен {json_output}")
    progress(f"Сохранен {text_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
