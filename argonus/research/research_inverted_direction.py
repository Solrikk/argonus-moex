#!/usr/bin/env python3
"""Research-only test of trading Engine A in reverse: short its longs, long its shorts.

Three selections are replayed on the same symbols and sessions: live (with its
skips), every day with RS5, and every day rank-1.  Each trade is simulated
three ways with the 07:05 model of research_0705_execution:

* as selected;
* reversed with the same rules: 1% stop, the T4 distance mirrored around the
  07:05 open, exit at 18:35;
* the exact counterparty of the original trade: it exits where the original
  exits, so its target is the original stop and its stop is the original T4.

Periods: the new sessions 2026-07-17..09-29 from the caches written by
research_new_period_engine_a.py, and the stored watchlists 2025-10-01..2026-07-16,
whose analyzer data and 5-minute sessions are downloaded once into a cache
(``--offline`` then rebuilds the report without an API client).  Nothing here
places, modifies or cancels orders.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
from datetime import date
from pathlib import Path
from typing import Any

from argonus.research import research_0705_execution as r0705
from argonus.research import research_flat_month_exit_risk as book_data
from argonus.research import research_new_period_engine_a as rnp
from argonus.research import research_october_engine_a_rs5 as october
from argonus.market_data.tbank_market_data import TBankApiError, TBankInvestClient


from argonus.paths import PROJECT_ROOT as ROOT
OUTPUT_DIR = ROOT / "data/backtests/inverted_direction_2026-09-30"
NEW_PERIOD_DIR = rnp.DEFAULT_OUTPUT_DIR
HISTORY_FIRST_DIR = "generated_watchlists_2025_10"
HISTORY_LAST_DIR = "generated_watchlists_2026_07"

VARIANTS = {
    "live": "с пропусками (боевой)",
    "no_skip": "каждый день, RS5 остаётся",
    "rank1": "каждый день, всегда первый",
}
MODES = {
    "as_is": "как есть",
    "reversed": "наоборот, те же правила",
    "counterparty": "точное зеркало",
}
FILLS = r0705.FillModel()


# ─── Inputs ─────────────────────────────────────────────────────────────────


def new_period_inputs() -> dict[str, Any]:
    cache = rnp.read_json_gz(NEW_PERIOD_DIR / "market_cache.json.gz")
    no_skip = rnp.read_json_gz(NEW_PERIOD_DIR / "no_skip_cache.json.gz")
    return {
        "period": cache["period"],
        "policies": {"live": cache["selection_days"], **no_skip["policies"]},
        "sessions": {**cache["sessions"], **no_skip["sessions"]},
    }


def history_paths() -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for directory in sorted((ROOT / "data/watchlists").glob("generated_watchlists_*")):
        if not HISTORY_FIRST_DIR <= directory.name <= HISTORY_LAST_DIR:
            continue
        _, _, year, month = directory.name.split("_")
        for path in sorted(directory.glob("watchlist_*.txt")):
            mmdd = path.stem.split("_")[1]
            if mmdd[:2] != month:
                raise RuntimeError(f"Watchlist {path} is not in its month folder")
            paths[date(int(year), int(month), int(mmdd[2:])).isoformat()] = path
    return paths


def collect_history(workers: int) -> dict[str, Any]:
    paths = history_paths()
    dates = sorted(paths)
    client = TBankInvestClient(user_agent=rnp.USER_AGENT)
    symbols = rnp.analyzer_symbols(paths)
    rnp.progress(f"История: {len(paths)} сессий, {len(symbols)} бумаг")
    stock_cache, index_candles, errors = rnp.fetch_analyzer_data(
        client, symbols, date.fromisoformat(dates[0]), date.fromisoformat(dates[-1]), workers
    )
    if errors:
        # Delisted names: the analyzer marks those ideas as skipped.
        rnp.progress(f"  нет дневной истории: {sorted(errors)}")
    rnp.progress("Анализатор: с воротами …")
    live_days = rnp.select(paths, stock_cache, index_candles)
    rnp.progress("Анализатор: без ворот …")
    with rnp.gates_disabled():
        ungated = rnp.select(paths, stock_cache, index_candles)
    live_by_date = {day["date"]: day for day in live_days}
    policies = {
        "live": live_days,
        **{name: rnp.apply_no_skip_policy(ungated, stock_cache, live_by_date, name) for name in rnp.NO_SKIP_POLICIES},
    }
    keys = sorted(
        {f"{day['date']}|{day['selected']['symbol']}" for days in policies.values() for day in days if day["selected"]}
    )
    rnp.progress(f"5-мин сессии: {len(keys)}")
    sessions: dict[str, Any] = {}
    for index, key in enumerate(keys, start=1):
        trade_date, symbol = key.split("|")
        try:
            rows = rnp.fetch_five_minute_session(client, symbol, date.fromisoformat(trade_date))
            status = "ok" if rows else "unavailable_no_5m_candles"
        except TBankApiError as exc:
            rows, status = [], f"error: {exc}"
        sessions[key] = {"status": status, "rows": rows, "session_sha256": october.canonical_sha256(rows)}
        if index % 50 == 0 or index == len(keys):
            rnp.progress(f"  {index}/{len(keys)}")
    return {
        "schema_version": 1,
        "period": {"start": dates[0], "end": dates[-1]},
        "analyzer_errors": errors,
        "policies": policies,
        "sessions": sessions,
    }


# ─── Replays ────────────────────────────────────────────────────────────────


def reversed_trade(trade: book_data.Trade) -> book_data.Trade:
    """Same symbol and session in the other direction; T4 distance mirrored around the 07:05 open."""
    anchor = next(candle.open for candle in trade.candles if candle.time == "07:05")
    return dataclasses.replace(
        trade,
        direction="short" if trade.direction == "long" else "long",
        target_price=2.0 * anchor - trade.target_price,
    )


def counterparty(execution: r0705.Execution, fills: r0705.FillModel = FILLS) -> r0705.Execution:
    """Stand exactly opposite the original trade: same entry and exit moments, other side."""
    sign = 1.0 if execution.direction == "long" else -1.0
    slip = fills.slippage_side_bps / 10_000
    raw_entry = execution.entry_price / (1 + sign * slip)
    raw_exit = execution.exit_price / (1 - sign * slip)
    entry = raw_entry * (1 - sign * slip)
    exit_price = raw_exit * (1 + sign * slip)
    gross = -sign * (exit_price / entry - 1) * 100
    fee = fills.fee_side_pct * (1 + exit_price / entry)
    return dataclasses.replace(
        execution,
        direction="short" if execution.direction == "long" else "long",
        policy="counterparty",
        entry_price=entry,
        exit_price=exit_price,
        reason=f"counterparty_of_{execution.reason}",
        gross_return_pct=gross,
        net_return_pct=gross - fee,
    )


def replay_variant(days: list[dict[str, Any]], sessions: dict[str, Any]) -> dict[str, Any]:
    trades = []
    unavailable = []
    for day in days:
        if not day["selected"]:
            continue
        session = sessions[f"{day['date']}|{day['selected']['symbol']}"]
        if not session["rows"]:
            unavailable.append({"date": day["date"], "reason": session["status"]})
            continue
        trades.append(rnp.to_trade(day, session))
    as_is, failures = rnp.replay_0705(trades, FILLS)
    unavailable.extend(failures)
    simulated = {row.date for row in as_is}
    reversed_rows, reversed_failures = rnp.replay_0705(
        [reversed_trade(trade) for trade in trades if trade.date in simulated], FILLS
    )
    if reversed_failures:
        raise RuntimeError(f"Reversed replay failed: {reversed_failures}")
    return {
        "as_is": as_is,
        "reversed": reversed_rows,
        "counterparty": [counterparty(row) if row.traded else row for row in as_is],
        "unavailable": unavailable,
    }


def summarize(rows: list[r0705.Execution]) -> dict[str, Any]:
    account = r0705.account(rows)
    traded = [row for row in rows if row.traded]
    return {
        "trades": account["trades"],
        "wins": account["wins"],
        "losses": account["losses"],
        "return_pct": account["return_pct"],
        "ending_equity_rub": account["ending_equity_rub"],
        "closed_trade_mdd_pct": account["closed_trade_mdd_pct"],
        "avg_gross_pct": sum(row.gross_return_pct for row in traded) / len(traded) if traded else 0.0,
        "avg_net_pct": sum(row.net_return_pct for row in traded) / len(traded) if traded else 0.0,
        "exit_reasons": rnp.trade_stats(rows)["exit_reasons"],
        "per_trade": account["per_trade"],
    }


def frozen_book_match(live_days: list[dict[str, Any]]) -> dict[str, Any]:
    """How closely today's analyzer reproduces the frozen Extended65 book on the stored watchlists."""
    _, trades65, _ = book_data.load_books()
    frozen = {trade.date: (trade.symbol, trade.direction) for trade in trades65}
    replay = {
        day["date"]: (day["selected"]["symbol"], day["selected"]["direction"])
        for day in live_days if day["selected"]
    }
    return {
        "frozen_trades": len(frozen),
        "replay_trades": len(replay),
        "same_pick": sum(replay.get(day) == pick for day, pick in frozen.items()),
        "only_frozen": sorted(set(frozen) - set(replay)),
        "only_replay": sorted(set(replay) - set(frozen)),
        "different_pick": sorted(day for day in set(frozen) & set(replay) if frozen[day] != replay[day]),
    }


def build_period(inputs: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"period": inputs["period"], "sessions": len(inputs["policies"]["live"]), "variants": {}}
    for name in VARIANTS:
        replay = replay_variant(inputs["policies"][name], inputs["sessions"])
        result["variants"][name] = {
            "modes": {mode: summarize(replay[mode]) for mode in MODES},
            "unavailable": replay["unavailable"],
        }
    return result


# ─── Markdown ───────────────────────────────────────────────────────────────


def cell(summary: dict[str, Any]) -> str:
    return (
        f"{rnp.pct(summary['return_pct'], 1)} · {summary['wins']}/{summary['losses']} · "
        f"MDD {rnp.pct(summary['closed_trade_mdd_pct'], 1)}"
    )


def period_lines(title: str, period: dict[str, Any]) -> list[str]:
    lines = [
        "",
        f"## {title}: {period['period']['start']} … {period['period']['end']}, {period['sessions']} сессий",
        "",
        "Итог на 50 000 ₽ · прибыльные/убыточные · максимальная просадка по закрытым сделкам.",
        "",
        "| Выбор сделок | Сделки | " + " | ".join(MODES.values()) + " |",
        "|---|---:|" + "---:|" * len(MODES),
    ]
    for name, label in VARIANTS.items():
        modes = period["variants"][name]["modes"]
        lines.append(
            f"| {label} | {modes['as_is']['trades']} | " + " | ".join(cell(modes[mode]) for mode in MODES) + " |"
        )
    lines += [
        "",
        "Средняя сделка до издержек (gross) → после издержек (net):",
        "",
        "| Выбор сделок | " + " | ".join(MODES.values()) + " |",
        "|---|" + "---:|" * len(MODES),
    ]
    for name, label in VARIANTS.items():
        modes = period["variants"][name]["modes"]
        lines.append(
            f"| {label} | "
            + " | ".join(
                f"{rnp.pct(modes[mode]['avg_gross_pct'])} → {rnp.pct(modes[mode]['avg_net_pct'])}" for mode in MODES
            )
            + " |"
        )
    missing = {name: len(period["variants"][name]["unavailable"]) for name in VARIANTS}
    if any(missing.values()):
        lines += [
            "",
            "Без 5-мин бара 07:05 или без данных (сделка не считается): "
            + ", ".join(f"{VARIANTS[name]} — {count}" for name, count in missing.items() if count)
            + ".",
        ]
    return lines


def markdown(report: dict[str, Any]) -> str:
    match = report["history_frozen_match"]
    lines = [
        "# Торговля наоборот — проверка теории",
        "",
        "Те же бумаги и те же дни, что выбирает движок A, но в обратную сторону. Модель исполнения как в "
        "`research_0705_execution.py`: вход по open бара 07:05 + 5 б.п., комиссия 0,04% с оборота каждой стороны, "
        "позиция min(150 000 ₽, 3× капитала), старт 50 000 ₽ в каждом периоде.",
        "",
        "- **Наоборот, те же правила:** стоп 1% против новой позиции, цель на том же расстоянии от open 07:05, "
        "что и T4, только в другую сторону, выход в 18:35.",
        "- **Точное зеркало:** позиция ровно напротив исходной сделки. Выходит там же, где исходная: её стоп — "
        "наша прибыль, её цель — наш стоп.",
    ]
    lines += period_lines("Новый период", report["new_period"])
    lines += period_lines("История", report["history"])
    lines += [
        "",
        f"Выбор «с пропусками» на истории сделан текущим анализатором по сохранённым watchlist'ам: совпал с "
        f"зафиксированной книгой в {match['same_pick']} из {match['frozen_trades']} её сделок"
        + (f"; расхождения: {', '.join(match['different_pick'] + match['only_frozen'] + match['only_replay'])}" if match["same_pick"] != match["frozen_trades"] or match["only_replay"] else "")
        + ".",
        "",
        "## Ограничения",
        "",
        "- Модели ранжирования обучены на истории до 16 июля, поэтому на истории исходное направление в выигрышном "
        "положении. Новый период для моделей вневыборочный, но именно по нему возникла теория.",
        "- Исполнение по open бара плюс проскальзывание; стакан, лотность и доступность шорта по каждой бумаге не "
        "проверялись. Просадка по закрытым сделкам, внутри дня глубже.",
        "",
        "Воспроизведение: `python3 -m argonus.research.research_inverted_direction` (история загружается один раз в кэш; "
        "`--offline` — только из кэша).",
    ]
    return "\n".join(lines) + "\n"


def strip_ledgers(period: dict[str, Any]) -> dict[str, Any]:
    return {
        **period,
        "variants": {
            name: {
                **variant,
                "modes": {mode: {k: v for k, v in summary.items() if k != "per_trade"} for mode, summary in variant["modes"].items()},
                "ledgers": {mode: summary["per_trade"] for mode, summary in variant["modes"].items()},
            }
            for name, variant in period["variants"].items()
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--offline", action="store_true", help="Только из кэша истории, без API.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    history_cache = output_dir / "history_cache.json.gz"
    if history_cache.exists():
        history_inputs = rnp.read_json_gz(history_cache)
    elif args.offline:
        raise RuntimeError(f"--offline requires {history_cache}")
    else:
        history_inputs = collect_history(args.workers)
        rnp.write_json_gz(history_cache, history_inputs)

    report = {
        "schema_version": 1,
        "artifact_type": "argonus_inverted_direction_test",
        "fills": dataclasses.asdict(FILLS),
        "new_period": build_period(new_period_inputs()),
        "history": build_period(history_inputs),
        "history_frozen_match": frozen_book_match(history_inputs["policies"]["live"]),
    }
    text = markdown(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "REPORT.md").write_text(text, encoding="utf-8")
    serializable = {
        **report,
        "new_period": strip_ledgers(report["new_period"]),
        "history": strip_ledgers(report["history"]),
    }
    (output_dir / "report.json").write_text(
        json.dumps(serializable, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
