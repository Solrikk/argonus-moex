#!/usr/bin/env python3
"""Research-only replay of the current Engine A on September 2025, before every stored book.

One watchlist per weekday session is rebuilt with generate_watchlist.py's own scan
through research_new_period_engine_a (cached client, every date sees only earlier
sessions).  The current analyzer, regime gates, rally guard, T4 >= 2% floor and RS5
selector choose the trade, and the trades are replayed by the same event portfolio as
backtest_opening_all_months' control: current_target125, 50,000 RUB, 150k / 3x cap,
0.04% fee and 5 bps slippage per side, 07:05 entry, 1% stop, 18:35 exit.

October 2025 is rebuilt in the same run as a control: its watchlists are compared
with generated_watchlists_2025_10 and its trades with the frozen all-months ledger.
``--no-rerankers`` adds a replay with both learned rerankers disabled, because their
training months include September 2025.  ``--scanner`` adds the A_plus_mean morning
scanner on public MOEX minute bars of the frozen 20-name production universe, scored
by the deployed 2026-10 model (trained on 2025-11..2026-09-09, so September 2025 is
outside its training data; a strictly causal model cannot exist for this month).
Nothing here can place, modify or cancel orders.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

from argonus.strategies import continuous_intraday as core
from argonus.strategies import continuous_opening as opening
from argonus.market_data import fetch_moex_research as moex
from argonus.market_data import opening_market_data as market_data
from argonus.models import opening_profit_model as model
from argonus.research import research_0705_execution as r0705
from argonus.research import research_new_period_engine_a as rnp
from argonus.watchlists import watchlist_best_target as wbt
from argonus.research.research_signal_direction import replay_events

from argonus.paths import PROJECT_ROOT as ROOT
DEFAULT_OUTPUT_DIR = ROOT / "data/backtests/september_2025_2026-10-03"
START, END = date(2025, 9, 1), date(2025, 10, 31)
TEST_MONTH, CONTROL_MONTH = "2025-09", "2025-10"
STORED_OCTOBER = ROOT / "data/watchlists/generated_watchlists_2025_10"
ALL_MONTHS = ROOT / "data/backtests/opening_integration_2026-10-03/all_months.json"
OPENING_MANIFEST = ROOT / "config/opening_activation_manifest.json"
DEPLOYED_MODEL_MONTH = "2026-10"
SCANNER_DAILY_FROM = "2025-05-01"


def load_cache(output_dir: Path, workers: int) -> dict[str, Any]:
    path = output_dir / "market_cache.json.gz"
    if path.exists():
        return rnp.read_json_gz(path)
    cache = rnp.collect_period(START, END, output_dir, workers)
    rnp.write_json_gz(path, cache)
    return cache


def trades_for(days: list[dict[str, Any]], sessions: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    trades, missing = {}, []
    for day in days:
        selected = day.get("selected")
        if not selected:
            continue
        key = f"{day['date']}|{selected['symbol']}"
        session = sessions.get(key)
        if not session or session["status"] != "ok" or not session["rows"]:
            missing.append({"date": day["date"], "symbol": selected["symbol"], "reason": "no 5-minute session"})
            continue
        if rnp.october.canonical_sha256(session["rows"]) != session["session_sha256"]:
            raise RuntimeError(f"Session hash mismatch: {key}")
        trades[day["date"]] = rnp.to_trade(day, session)
    return trades, missing


def replay(calendar: list[str], trades: dict[str, Any], month: str) -> dict[str, Any]:
    combined = {}
    for ds in calendar:
        if not ds.startswith(month):
            continue
        day = {"rows": {}, "contexts": {}, "legacy": trades.get(ds), "timeline": {}}
        if day["legacy"]:
            for c in day["legacy"].candles:
                day["rows"].setdefault(c.time, {})[("legacy", day["legacy"].symbol)] = [
                    c.time, c.open, c.high, c.low, c.close, 0]
        combined[ds] = day
    return replay_events(combined, "current_target125")


def reselect(cache: dict[str, Any], output_dir: Path, *, rerankers: bool) -> list[dict[str, Any]]:
    paths = {day: output_dir / "watchlists" / f"watchlist_{day}.txt" for day in cache["calendar"]}
    stock = {k: [rnp.row_to_candle(r) for r in v] for k, v in cache["analyzer_stock"].items()}
    index = [rnp.row_to_candle(r) for r in cache["analyzer_index"]]
    saved = (wbt.load_same_day_top_reranker_model, wbt.load_same_day_top_winner_reranker_model)
    if not rerankers:
        wbt.load_same_day_top_reranker_model = lambda: None
        wbt.load_same_day_top_winner_reranker_model = lambda: None
    try:
        return rnp.select(paths, stock, index)
    finally:
        wbt.load_same_day_top_reranker_model, wbt.load_same_day_top_winner_reranker_model = saved


def pick(day: dict[str, Any]) -> tuple[str, str] | None:
    s = day.get("selected")
    return (s["symbol"], s["direction"]) if s else None


def october_control(cache: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    days = [ds for ds in cache["calendar"] if ds.startswith(CONTROL_MONTH)]
    identical = []
    for ds in days:
        stored = STORED_OCTOBER / f"watchlist_{ds[5:7]}{ds[8:10]}.txt"
        identical.append(stored.is_file() and stored.read_text(encoding="utf-8") == cache["watchlists"][ds])
    frozen = json.loads(ALL_MONTHS.read_text(encoding="utf-8"))["control"]["ledger"]
    frozen = {t["date"]: (t["symbol"], t["direction"]) for t in frozen if t["date"].startswith(CONTROL_MONTH)}
    regenerated = {d["date"]: pick(d) for d in cache["selection_days"] if d["date"].startswith(CONTROL_MONTH)}
    rows = [{"date": ds, "frozen": frozen.get(ds), "regenerated": regenerated.get(ds),
             "watchlist_identical": same} for ds, same in zip(days, identical)]
    return {"sessions": len(days), "watchlists_identical": sum(identical),
            "decisions_match": sum(r["frozen"] == r["regenerated"] for r in rows), "days": rows}


def moex_books(output_dir: Path, symbols: list[str], first: str, last: str) -> tuple[dict, dict]:
    """Full 07:00--23:49 five-minute sessions exactly as research_continuous_opening."""
    pages = output_dir / "moex_pages"
    books, dailies = {}, {}
    for symbol in symbols:
        minute = moex.fetch_pages(symbol, first, last, 1, pages)
        daily = moex.fetch_pages(symbol, SCANNER_DAILY_FROM, last, 24, pages)
        dailies[symbol] = [[r[0][:10], *r[1:]] for r in daily]
        groups = defaultdict(list)
        for r in minute:
            ds, tm = r[0][:10], r[0][11:16]
            if not "07:00" <= tm <= "23:49":
                continue
            hh, mm = map(int, tm.split(":"))
            groups[(ds, core.clock(hh * 60 + mm // 5 * 5))].append(r)
        days = defaultdict(list)
        for (ds, tm), rows in sorted(groups.items()):
            days[ds].append([tm, rows[0][1], max(r[2] for r in rows), min(r[3] for r in rows),
                             rows[-1][4], sum(r[5] for r in rows), sum(r[6] for r in rows)])
        books[symbol] = dict(days)
    return books, dailies


def scanner_days(calendar: list[str], symbols: list[str], books: dict, dailies: dict,
                 trades: dict[str, Any]) -> dict[str, Any]:
    combined = {}
    for ds in calendar:
        contexts, rows = {}, {}
        for symbol in symbols:
            if ds not in books[symbol]:
                continue
            ctx = core.prior_context(dailies[symbol], ds)
            if ctx:
                previous = [r for r in dailies[symbol] if r[0] < ds]
                ctx["previous_close"] = previous[-1][4] if previous else None
                contexts[symbol] = ctx
            for row in books[symbol][ds]:
                rows.setdefault(row[0], {})[("scanner", symbol)] = row
        trade = trades.get(ds)
        if trade:
            for c in trade.candles:
                rows.setdefault(c.time, {})[("legacy", trade.symbol)] = [c.time, c.open, c.high, c.low, c.close, 0]
        sessions = defaultdict(list)
        for tm, by_source in sorted(rows.items()):
            for (source, symbol), r in by_source.items():
                if source == "scanner":
                    sessions[symbol].append(r)
        day = {"rows": rows, "contexts": contexts, "legacy": trade}
        day["timeline"] = {core.clock(m): opening.scan(ds, core.clock(m), sessions, contexts)
                           for m in range(440, 571, 5)}
        combined[ds] = day
    return combined


def scanner_replay(cache: dict[str, Any], trades: dict[str, Any], output_dir: Path, month: str) -> dict[str, Any]:
    manifest = json.loads(OPENING_MANIFEST.read_text(encoding="utf-8"))
    symbols = manifest["universe"]
    calendar = [ds for ds in cache["calendar"] if ds.startswith(month)]
    books, dailies = moex_books(output_dir, symbols, calendar[0], calendar[-1])
    combined = scanner_days(calendar, symbols, books, dailies, trades)

    base = ROOT / manifest["training_base"]
    models, provenance = market_data.monthly_models(base, DEPLOYED_MODEL_MONTH)
    training_dates = sorted({r["date"] for r in market_data.training_rows(base, DEPLOYED_MODEL_MONTH)})
    if any(ds.startswith(month) for ds in training_dates):
        raise ValueError("Deployed model was trained on the test month")
    provenance = {**provenance, "first_training_date": training_dates[0]}
    scores, candidates = {}, 0
    for ds, day in combined.items():
        for items, market in day["timeline"].values():
            if not items:
                continue
            ranked, _ = model.rank_opportunities(items, market, day, models)
            candidates += len(items)
            scores.update({item.identity: item.score for item in ranked})

    def choose(items, market, contexts):
        return sorted([replace(i, score=scores[i.identity]) for i in items if i.identity in scores],
                      key=lambda i: (-i.score, i.symbol, i.setup))

    candidate = replay_events(combined, "A_plus_mean", choose=choose)
    control = replay_events(combined, "current_target125")
    coverage = {symbol: sum(ds in books[symbol] for ds in calendar) for symbol in symbols}
    return {"model": provenance, "universe_sessions": coverage, "scanner_candidates": candidates,
            "scored_above_threshold": len(scores), "candidate": candidate, "control": control}


def rub(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def pct(value: float) -> str:
    return f"{value:+.2f}%".replace(".", ",")


def whole(value: float, signed: bool = False) -> str:
    return (f"{value:+,.0f}" if signed else f"{value:,.0f}").replace(",", " ")


def index_move(cache: dict[str, Any], month: str) -> float:
    closes = sorted((row[0], row[4]) for row in cache["analyzer_index"])
    before = [c for d, c in closes if d < month + "-01"][-1]
    inside = [c for d, c in closes if d.startswith(month)][-1]
    return (inside / before - 1) * 100


def markdown(report: dict[str, Any], cache: dict[str, Any]) -> str:
    sep, octo = report[TEST_MONTH]["summary"], report[CONTROL_MONTH]["summary"]
    control = report["october_control"]
    lines = [
        "# Сентябрь 2025 — текущая система на месяце до всех книг",
        "",
        f"Вотчлисты на каждую из {sum(d.startswith(TEST_MONTH) for d in cache['calendar'])} сессий сентября 2025 "
        "заново построены генератором `generate_watchlist.py` (каждая дата видит только прошлые сессии). "
        "Сделку выбирают текущий анализатор, режимные ворота, rally-guard 8%, порог T4 ≥ 2% и RS5. "
        "Исполнение — тот же событийный портфель, что в таблице всех месяцев: target125, вход 07:05, стоп 1%, "
        "выход 18:35, комиссия 0,04% и проскальзывание 5 б.п. на сторону, позиция min(150 000 ₽, 3 × капитал), "
        "старт 50 000 ₽.",
        "",
        f"**Мотор A + RS5 + target125: 50 000 ₽ → {rub(sep['ending_equity_rub'])} ({pct(sep['return_pct'])}); "
        f"сделок {sep['trades']}, прибыльных {sep['wins']}; просадка по закрытию дня {pct(sep['daily_close_mdd_pct'])}.**",
        "",
        f"Независимый симулятор `research_0705_execution` даёт тот же итог; с исходной целью T4 вместо target125 — "
        f"{rub(report['cross_check']['baseline'])} ({pct(report['cross_check']['baseline'] / 500 - 100)}).",
    ]
    scanner = report.get("scanner")
    if scanner:
        cand = scanner["candidate"]["summary"]
        legs = scanner["candidate"]["ledger"]
        extra = [t for t in legs if t["source"] == "scanner"]
        lines += ["",
                  f"**С утренним сканером A_plus_mean: 50 000 ₽ → {rub(cand['ending_equity_rub'])} "
                  f"({pct(cand['return_pct'])}); сделок {cand['trades']} (сканер {len(extra)}, "
                  f"прибыльных {sum(t['pnl_rub'] > 0 for t in extra)}, итог сканера "
                  f"{rub(sum(t['pnl_rub'] for t in extra))}); просадка {pct(cand['daily_close_mdd_pct'])}.**",
                  "",
                  f"Модель сканера — развёрнутая `{DEPLOYED_MODEL_MONTH}` (обучение "
                  f"{scanner['model']['first_training_date']} … {scanner['model']['latest_training_date']}, "
                  f"{scanner['model']['training_days']} дней): сентября 2025 в обучении нет, но обучение идёт "
                  "на более поздних данных, строго причинной модели для этого месяца не существует. "
                  "Вселенная — 20 бумаг из манифеста, данные — публичные минутные свечи MOEX. "
                  f"Сканер нашёл {scanner['scanner_candidates']} утренних сетапов, прогноз не ниже "
                  f"{pct(model.MIN_NET_PCT)} дали {scanner['scored_above_threshold']}."]
    plain = report.get("no_rerankers")
    if plain:
        p = plain[TEST_MONTH]["summary"]
        changed = [c for c in plain["changed_days"] if c["date"].startswith(TEST_MONTH)]
        lines += ["",
                  f"Оба реранкера обучались на месяцах, включающих сентябрь 2025. Без них: "
                  f"{rub(p['ending_equity_rub'])} ({pct(p['return_pct'])}), сделок {p['trades']}; "
                  f"изменилось дней: {len(changed)} — "
                  + (", ".join(f"{c['date']}: {c['live'] and ' '.join(c['live']) or 'пропуск'} → "
                               f"{c['plain'] and ' '.join(c['plain']) or 'пропуск'}" for c in changed) or "нет") + "."]
    lines += ["",
              f"IMOEX за сентябрь 2025: {pct(index_move(cache, TEST_MONTH))} — падающий рынок, "
              "благоприятный для шортов режим этой стратегии.",
              "",
              "## Контроль на октябре 2025",
              "",
              f"Тот же прогон восстановил октябрь: решения совпали с замороженной книгой в "
              f"{control['decisions_match']} из {control['sessions']} сессий, итог октября "
              f"{rub(octo['ending_equity_rub'])} ({pct(octo['return_pct'])}) — как в таблице всех месяцев. "
              f"Побайтно совпали {control['watchlists_identical']} из {control['sessions']} вотчлистов; "
              "в остальных отличается одна-две бумаги из-за текущего списка инструментов, на решения это не повлияло.",
              "",
              "## Сделки по дням",
              "",
              "| Дата | Решение | Выход | Чистая доходность | Прибыль, ₽ | Капитал, ₽ |",
              "|---|---|---|---:|---:|---:|"]
    ledger = {t["date"]: t for t in report[TEST_MONTH]["ledger"]}
    equity = {d["date"]: d["equity_after"] for d in report[TEST_MONTH]["daily"]}
    for day in report["selection_days"]:
        if not day["date"].startswith(TEST_MONTH):
            continue
        s, t = day.get("selected"), ledger.get(day["date"])
        if s and t:
            lines.append(f"| {day['date']} | {s['symbol']} {s['direction']} | {t['reason']} {t['exit_time']} | "
                         f"{pct(t['net_return_pct'])} | {whole(t['pnl_rub'], True)} | {whole(equity[day['date']])} |")
        else:
            lines.append(f"| {day['date']} | пропуск: {day['decision_reason']} | — | — | — | — |")
    if scanner:
        lines += ["", "## Сделки сканера", "",
                  "| Дата | Бумага | Сетап | Вход | Выход | Чистая доходность | Прибыль, ₽ |",
                  "|---|---|---|---|---|---:|---:|"]
        for t in scanner["candidate"]["ledger"]:
            if t["source"] == "scanner":
                lines.append(f"| {t['date']} | {t['symbol']} {t['direction']} | {t['setup']} | {t['entry_time']} | "
                             f"{t['reason']} {t['exit_time']} | {pct(t['net_return_pct'])} | {whole(t['pnl_rub'], True)} |")
    lines += ["", "## Ограничения", "",
              "- Один месяц и один рыночный режим (падение); слабый для стратегии режим — ралли — этим месяцем не проверен.",
              "- Список инструментов — текущий снимок T-Invest: бумаги, исключённые с торгов после сентября 2025, в генератор не попали.",
              "- Правила, ворота и target125 подбирались на октябре 2025 — июле 2026, сентябрь 2025 в подборе не участвовал; "
              "реранкеры его видели (эффект показан выше).",
              "- Дробные акции, без исторического стакана и доступности шорта; это бэктест, не результат счёта.",
              "",
              "```bash",
              "python3 -m argonus.research.research_september_2025 --no-rerankers --scanner",
              "```",
              "",
              "Все сделки, решения и вотчлисты: [report.json](report.json), [watchlists/](watchlists/)."]
    return "\n".join(lines) + "\n"


def summary(result: dict[str, Any]) -> dict[str, Any]:
    keys = ("ending_equity_rub", "pnl_rub", "return_pct", "trades", "wins", "daily_close_mdd_pct",
            "intrabar_adverse_proxy_mdd_pct", "cost_rub")
    return {k: result[k] for k in keys}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--no-rerankers", action="store_true", help="Also replay with both rerankers disabled.")
    parser.add_argument("--scanner", action="store_true", help="Also replay the A_plus_mean morning scanner.")
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    cache = load_cache(output_dir, args.workers)
    calendar = cache["calendar"]

    trades, missing = trades_for(cache["selection_days"], cache["sessions"])
    report: dict[str, Any] = {
        "mode": "research_only_rebuilt_watchlists",
        "period": cache["period"],
        "october_control": october_control(cache, output_dir),
        "missing_sessions": missing,
        "selection_days": cache["selection_days"],
    }
    for month in (TEST_MONTH, CONTROL_MONTH):
        result = replay(calendar, trades, month)
        report[month] = {"summary": summary(result), "ledger": result["ledger"], "daily": result["daily"]}

    # Independent replay engine: the same trades through research_0705_execution.
    month_trades = [t for ds, t in sorted(trades.items()) if ds.startswith(TEST_MONTH)]
    report["cross_check"] = {}
    for policy in r0705.POLICIES[:2]:
        executions = [r0705.simulate(t, policy, r0705.FillModel()) for t in month_trades]
        report["cross_check"][policy.name] = 50_000.0 + r0705.account(executions)["pnl_rub"]
    if abs(report["cross_check"]["target125"] - report[TEST_MONTH]["summary"]["ending_equity_rub"]) > 1e-6:
        raise ValueError("Independent 07:05 replay differs from the event portfolio")

    if args.no_rerankers:
        plain_days = reselect(cache, output_dir, rerankers=False)
        live_days = {d["date"]: d for d in cache["selection_days"]}
        changed = [d for d in plain_days if pick(d) != pick(live_days[d["date"]])]
        extra_path = output_dir / "no_rerankers_sessions.json.gz"
        extra = rnp.read_json_gz(extra_path) if extra_path.exists() else {}
        need = [d for d in changed if d.get("selected") and f"{d['date']}|{d['selected']['symbol']}" not in
                {**cache["sessions"], **extra}]
        if need:
            client = rnp.TBankInvestClient(user_agent=rnp.USER_AGENT)
            extra.update(rnp.fetch_selected_sessions(client, need))
            rnp.write_json_gz(extra_path, extra)
        plain_trades, plain_missing = trades_for(plain_days, {**cache["sessions"], **extra})
        report["no_rerankers"] = {
            "changed_days": [{"date": d["date"], "live": pick(live_days[d["date"]]), "plain": pick(d)} for d in changed],
            "missing_sessions": plain_missing,
            "selection_days": plain_days,
        }
        for month in (TEST_MONTH, CONTROL_MONTH):
            result = replay(calendar, plain_trades, month)
            report["no_rerankers"][month] = {"summary": summary(result), "ledger": result["ledger"]}

    if args.scanner:
        value = scanner_replay(cache, trades, output_dir, TEST_MONTH)
        if abs(value["control"]["ending_equity_rub"] - report[TEST_MONTH]["summary"]["ending_equity_rub"]) > 1e-6:
            raise ValueError("Scanner control differs from the Engine A replay")
        report["scanner"] = {**{k: v for k, v in value.items() if k not in ("candidate", "control")},
                             "candidate": {"summary": summary(value["candidate"]),
                                           "ledger": value["candidate"]["ledger"],
                                           "daily": value["candidate"]["daily"],
                                           "counters": value["candidate"]["counters"]}}

    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    (output_dir / "REPORT.md").write_text(markdown(report, cache), encoding="utf-8")
    print(json.dumps({
        "scanner": {k: (v["summary"] if k == "candidate" else v) for k, v in report.get("scanner", {}).items()},
        "october_control": {k: v for k, v in report["october_control"].items() if k != "days"},
        TEST_MONTH: report[TEST_MONTH]["summary"],
        CONTROL_MONTH: report[CONTROL_MONTH]["summary"],
        "missing_sessions": missing,
        "no_rerankers": {k: (v["summary"] if isinstance(v, dict) and "summary" in v else v)
                         for k, v in report.get("no_rerankers", {}).items() if k != "selection_days"},
    }, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
