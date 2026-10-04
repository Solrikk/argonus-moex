#!/usr/bin/env python3
from __future__ import annotations

from argonus.paths import WATCHLIST_DIR

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from argonus.backtesting import backtest_generated_watchlists as bgw
from argonus.market_data.tbank_market_data import TBankApiError, TBankInvestClient
from argonus.watchlists import watchlist_best_target as wbt


DIR_RE = re.compile(r"generated_watchlists_(\d{4})_(\d{2})$")
FILE_RE = re.compile(r"watchlist_(\d{2})(\d{2})\.txt$")
OUTCOME_LABELS = ("T1", "T2", "T3", "T4", "miss", "skipped")
RETRY_DELAYS = (3.0, 6.0, 12.0, 20.0)
REQUEST_PAUSE_SECONDS = 0.35


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Проверяет все generated_watchlists_* и строит сводку по тому, "
            "до какого intraday-таргета дошел top pick: T1/T2/T3/T4 или miss day."
        )
    )
    parser.add_argument(
        "--root",
        default=str(WATCHLIST_DIR),
        help="Папка с generated_watchlists_*. По умолчанию data/watchlists.",
    )
    parser.add_argument(
        "--json-output",
        default="target_hit_levels_all_months.json",
        help="Куда сохранить JSON-сводку. По умолчанию target_hit_levels_all_months.json.",
    )
    parser.add_argument(
        "--text-output",
        default="target_hit_levels_all_months.txt",
        help="Куда сохранить текстовую сводку. По умолчанию target_hit_levels_all_months.txt.",
    )
    parser.add_argument(
        "--refresh-existing",
        action="store_true",
        help="Пересчитать backtest_summary.json даже для месяцев, где он уже существует.",
    )
    return parser.parse_args()


def progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def discover_month_dirs(root: Path) -> list[Path]:
    months: list[Path] = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and DIR_RE.fullmatch(child.name):
            months.append(child)
    return months


def load_watchlists_for_month(month_dir: Path) -> list[bgw.WatchlistEntry]:
    dir_match = DIR_RE.fullmatch(month_dir.name)
    if not dir_match:
        raise RuntimeError(f"Некорректная папка месяца: {month_dir}")

    year = int(dir_match.group(1))
    month_from_dir = int(dir_match.group(2))
    watchlists: list[bgw.WatchlistEntry] = []

    for path in sorted(month_dir.glob("watchlist_*.txt")):
        file_match = FILE_RE.fullmatch(path.name)
        if not file_match:
            continue

        month = int(file_match.group(1))
        day = int(file_match.group(2))
        if month != month_from_dir:
            raise RuntimeError(f"Месяц в имени файла не совпадает с директорией: {path}")

        trade_date = date(year, month, day)
        raw_text = path.read_text(encoding="utf-8")
        symbols = tuple(idea.symbol for idea in wbt.parse_watchlist(raw_text))
        watchlists.append(
            bgw.WatchlistEntry(
                trade_date=trade_date,
                path=f"./{month_dir.name}/{path.name}",
                raw_text=raw_text,
                symbols=symbols,
            )
        )

    return watchlists


def fetch_candles_with_retry(
    client: TBankInvestClient,
    instrument_id: str,
    start_date: date,
    end_date: date,
) -> list:
    for attempt, delay in enumerate((0.0, *RETRY_DELAYS), start=1):
        if delay > 0:
            progress(f"  HTTP 429, пауза {delay:.0f}s перед повтором")
            time.sleep(delay)
        try:
            candles = client.get_daily_candles(
                instrument_id,
                start_date=start_date,
                end_date=end_date,
            )
            time.sleep(REQUEST_PAUSE_SECONDS)
            return candles
        except TBankApiError as exc:
            if "HTTP 429" not in str(exc) or attempt > len(RETRY_DELAYS):
                raise
    raise RuntimeError("Недостижимый код в fetch_candles_with_retry")


def build_cache_for_watchlists(
    watchlists: list[bgw.WatchlistEntry],
) -> tuple[dict[str, list], list]:
    if not watchlists:
        return {}, []

    client = TBankInvestClient(user_agent="target-hit-levels/1.0")
    all_symbols = sorted({symbol for item in watchlists for symbol in item.symbols})
    min_trade_date = min(item.trade_date for item in watchlists)
    max_trade_date = max(item.trade_date for item in watchlists)
    history_start = min_trade_date - timedelta(days=500)
    history_end = max_trade_date

    progress(f"Загрузка справочника MOEX-акций для {len(all_symbols)} тикеров")
    share_ids = {share.ticker: share.instrument_id for share in client.list_moex_shares()}
    missing = [symbol for symbol in all_symbols if symbol not in share_ids]
    if missing:
        raise RuntimeError(f"Не удалось найти instrument_id для: {', '.join(missing)}")

    stock_cache: dict[str, list] = {}
    for index, symbol in enumerate(all_symbols, start=1):
        progress(f"[{index}/{len(all_symbols)}] Свечи {symbol}")
        stock_cache[symbol] = fetch_candles_with_retry(
            client,
            share_ids[symbol],
            history_start,
            history_end,
        )

    progress("Загрузка свечей индекса IMOEX")
    index_instrument_id = client.resolve_index_instrument_id(
        wbt.DEFAULT_INDEX_SYMBOL,
        class_code_hint=wbt.DEFAULT_INDEX_BOARD,
    )
    index_candles = fetch_candles_with_retry(
        client,
        index_instrument_id,
        history_start,
        history_end,
    )
    return stock_cache, index_candles


def evaluate_watchlists(
    watchlists: list[bgw.WatchlistEntry],
    stock_cache: dict[str, list],
    index_candles: list,
) -> list[bgw.DayResult]:
    bgw.patch_watchlist_data_access(stock_cache, index_candles)
    wbt.load_same_day_top_reranker_model()

    results: list[bgw.DayResult] = []
    for index, entry in enumerate(watchlists, start=1):
        progress(f"[{index}/{len(watchlists)}] Оценка {entry.trade_date.isoformat()}")
        try:
            results.append(bgw.evaluate_watchlist(stock_cache, entry))
        except Exception as exc:  # noqa: BLE001
            results.append(
                bgw.DayResult(
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
            )
    return results


def ensure_backtest_summaries(
    month_dirs: list[Path],
    refresh_existing: bool,
) -> dict[str, dict[str, object]]:
    summaries: dict[str, dict[str, object]] = {}
    missing_month_dirs: list[Path] = []

    for month_dir in month_dirs:
        summary_path = month_dir / "backtest_summary.json"
        if summary_path.exists() and not refresh_existing:
            summaries[month_dir.name] = json.loads(summary_path.read_text(encoding="utf-8"))
        else:
            missing_month_dirs.append(month_dir)

    if not missing_month_dirs:
        return summaries

    progress(
        "Нужно досчитать backtest_summary.json для: "
        + ", ".join(month_dir.name for month_dir in missing_month_dirs)
    )

    month_watchlists: dict[str, list[bgw.WatchlistEntry]] = {}
    all_watchlists: list[bgw.WatchlistEntry] = []
    for month_dir in missing_month_dirs:
        watchlists = load_watchlists_for_month(month_dir)
        month_watchlists[month_dir.name] = watchlists
        all_watchlists.extend(watchlists)

    stock_cache, index_candles = build_cache_for_watchlists(all_watchlists)
    all_results = evaluate_watchlists(all_watchlists, stock_cache, index_candles)

    results_by_month: dict[str, list[bgw.DayResult]] = defaultdict(list)
    for result in all_results:
        month_key = result.month.replace("-", "_")
        results_by_month[f"generated_watchlists_{month_key}"].append(result)

    for month_dir in missing_month_dirs:
        report = bgw.summarize(results_by_month[month_dir.name])
        summary_path = month_dir / "backtest_summary.json"
        summary_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        summaries[month_dir.name] = report
        progress(f"Сохранен {summary_path}")

    return summaries


def classify_result(result: dict[str, object]) -> str:
    skipped_reason = str(result.get("skipped_reason") or "")
    if skipped_reason:
        # Если по инструменту нет дневной свечи на дату, для целевой метрики
        # это практический miss day: target в течение дня не подтвержден.
        if "нет дневной свечи" in skipped_reason.lower():
            return "miss"
        return "skipped"
    label = result.get("highest_hit_target_label")
    if label in {"T1", "T2", "T3", "T4"}:
        return str(label)
    return "miss"


def make_bucket() -> dict[str, object]:
    return {
        "days": 0,
        "T1": 0,
        "T2": 0,
        "T3": 0,
        "T4": 0,
        "miss": 0,
        "skipped": 0,
    }


def add_percentages(bucket: dict[str, object]) -> dict[str, object]:
    days = int(bucket["days"])
    with_percentages = dict(bucket)
    with_percentages["rates"] = {
        label: (bucket[label] / days * 100.0 if days else 0.0)
        for label in OUTCOME_LABELS
    }
    return with_percentages


def build_analysis(
    month_dirs: list[Path],
    summaries: dict[str, dict[str, object]],
) -> dict[str, object]:
    totals = make_bucket()
    monthly: dict[str, dict[str, object]] = {}
    day_results: list[dict[str, object]] = []

    for month_dir in month_dirs:
        report = summaries[month_dir.name]
        bucket = make_bucket()
        for result in report["results"]:
            outcome = classify_result(result)
            bucket["days"] += 1
            bucket[outcome] += 1
            totals["days"] += 1
            totals[outcome] += 1

            enriched = dict(result)
            enriched["outcome"] = outcome
            day_results.append(enriched)

        month_key = month_dir.name.removeprefix("generated_watchlists_").replace("_", "-")
        monthly[month_key] = add_percentages(bucket)

    day_results.sort(key=lambda item: (item["trade_date"], item["path"]))
    return {
        "totals": add_percentages(totals),
        "monthly": dict(sorted(monthly.items())),
        "days": day_results,
    }


def format_percent(value: float) -> str:
    return f"{value:.1f}%"


def format_text_report(report: dict[str, object]) -> str:
    totals = report["totals"]
    monthly = report["monthly"]

    lines = [
        "Общий итог",
        (
            f"Дней: {totals['days']} | "
            f"T1={totals['T1']} ({format_percent(totals['rates']['T1'])}) | "
            f"T2={totals['T2']} ({format_percent(totals['rates']['T2'])}) | "
            f"T3={totals['T3']} ({format_percent(totals['rates']['T3'])}) | "
            f"T4={totals['T4']} ({format_percent(totals['rates']['T4'])}) | "
            f"miss={totals['miss']} ({format_percent(totals['rates']['miss'])}) | "
            f"skipped={totals['skipped']} ({format_percent(totals['rates']['skipped'])})"
        ),
        "",
        "По месяцам",
    ]

    for month, stats in monthly.items():
        lines.append(
            (
                f"{month}: days={stats['days']} | "
                f"T1={stats['T1']} ({format_percent(stats['rates']['T1'])}) | "
                f"T2={stats['T2']} ({format_percent(stats['rates']['T2'])}) | "
                f"T3={stats['T3']} ({format_percent(stats['rates']['T3'])}) | "
                f"T4={stats['T4']} ({format_percent(stats['rates']['T4'])}) | "
                f"miss={stats['miss']} ({format_percent(stats['rates']['miss'])}) | "
                f"skipped={stats['skipped']} ({format_percent(stats['rates']['skipped'])})"
            )
        )

    lines.append("")
    lines.append("Miss days")
    miss_days = [item for item in report["days"] if item["outcome"] == "miss"]
    if not miss_days:
        lines.append("Нет miss days.")
    else:
        for item in miss_days:
            reason = item.get("skipped_reason")
            if reason:
                lines.append(
                    f"{item['trade_date']}: {item.get('symbol') or 'N/A'} | miss | {reason} | path={item['path']}"
                )
            else:
                lines.append(
                    (
                        f"{item['trade_date']}: {item.get('symbol') or 'N/A'} | "
                        f"{item.get('direction') or 'N/A'} | "
                        f"best={item.get('best_target_label') or 'N/A'} | "
                        f"path={item['path']}"
                    )
                )

    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    month_dirs = discover_month_dirs(root)
    if not month_dirs:
        print("Не найдено ни одной папки generated_watchlists_*.", file=sys.stderr)
        return 1

    summaries = ensure_backtest_summaries(month_dirs, refresh_existing=args.refresh_existing)
    report = build_analysis(month_dirs, summaries)

    json_output = root / args.json_output
    text_output = root / args.text_output
    json_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    text_output.write_text(format_text_report(report) + "\n", encoding="utf-8")

    print(format_text_report(report))
    progress(f"Сохранен {json_output}")
    progress(f"Сохранен {text_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
