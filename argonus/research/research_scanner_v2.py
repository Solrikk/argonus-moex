"""Universe-wide event-driven trading research, independent of watchlists.

No live bot import, account access or order submission. Fit selects a fixed
architecture before validation and the reused late historical evaluation.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from collections import Counter
from dataclasses import asdict
from datetime import date
from pathlib import Path

from argonus.strategies import continuous_intraday as engine
from argonus.research import research_win_rate_60 as historical
from argonus.research import research_0705_execution as legacy

from argonus.paths import PROJECT_ROOT as ROOT
OUTPUT = ROOT / "data/backtests/scanner_v2_2026-10-03"
FIT_END, VALIDATION_END = "2026-03-31", "2026-07-16"
NEW = ROOT / "data/backtests/moex_new_period_2026-09-10"


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def compact(result):
    return {k: v for k, v in result.items() if k not in ("ledger", "daily")}


def load_data(include_later=True):
    old = json.loads((ROOT / "data/intraday_universe/manifest.json").read_text())
    new = json.loads((NEW / "manifest.json").read_text())
    if new["status"] != "complete":
        raise ValueError("Public market archive is incomplete")
    old_books, old_daily, new_books, new_daily, hashes = {}, {}, {}, {}, {}
    groups = [
        ("old", old["selected"], ROOT / "data/intraday_universe", old_books, old_daily),
        ("new", new["selected"], NEW, new_books, new_daily),
    ]
    for group, names, directory, books, daily in groups:
        if group == "new" and not include_later:
            continue
        for name in sorted(names):
            path = directory / "five_min" / f"{name}.json.gz"
            with gzip.open(path, "rt") as handle:
                books[name] = json.load(handle)
            dp = directory / "dailies" / f"{name}.json"
            daily[name] = json.loads(dp.read_text())
            for p in (path, dp):
                hashes[str(p.relative_to(ROOT))] = hashlib.sha256(p.read_bytes()).hexdigest()
    trades, _ = historical.load_data(include_later)
    by_date = {t.date: t for t in trades}
    days = sorted({ds for books in (old_books, new_books) for book in books.values() for ds in book
                   if "2025-11-03" <= ds <= (new["till"] if include_later else FIT_END)
                   and date.fromisoformat(ds).weekday() < 5})
    counts = Counter()
    prepared = {}
    for i, ds in enumerate(days):
        books, daily = (old_books, old_daily) if ds <= VALIDATION_END else (new_books, new_daily)
        sessions = {symbol: rows[ds] for symbol, rows in books.items() if ds in rows}
        contexts = {symbol: engine.prior_context(daily[symbol], ds) for symbol in sessions}
        contexts = {s: c for s, c in contexts.items() if c is not None}
        timeline = {}
        for minute in range(630, 1021, 5):
            tm = engine.clock(minute)
            opportunities, market = engine.scan_opportunities(ds, tm, sessions, contexts)
            timeline[tm] = (opportunities, market)
            counts["scanner_decisions"] += 1
            counts["setup_candidates"] += len(opportunities)
            counts["eligible_symbol_decisions"] += market.get("eligible_symbols", 0)
        market_rows = {}
        for symbol, rows in sessions.items():
            for row in rows:
                market_rows.setdefault(row[0], {})[("scanner", symbol)] = row
        trade = by_date.get(ds)
        if trade:
            for candle in trade.candles:
                row = [candle.time, candle.open, candle.high, candle.low, candle.close, 0]
                market_rows.setdefault(row[0], {})[("legacy", trade.symbol)] = row
        prepared[ds] = {"timeline": timeline, "rows": market_rows, "contexts": contexts, "legacy": trade}
        if i % 40 == 0:
            print(f"Prepared {i + 1}/{len(days)} sessions", flush=True)
    return prepared, {"dates": [days[0], days[-1]], "weekday_sessions": len(days),
                      "old_symbols": len(old_books), "late_symbols": len(new_books),
                      "opportunities": dict(counts), "source_hashes": hashes}


def replay(prepared, strategy, *, bps=5, fee=.04, first=None, last=None, opportunity_filter=None):
    if strategy not in engine.STRATEGIES:
        raise ValueError("Unknown strategy")
    portfolio = engine.EventPortfolio(bps=bps, fee_pct=fee)
    for ds, day in prepared.items():
        if (first and ds < first) or (last and ds > last):
            continue
        portfolio.begin_day(ds)
        trade = day["legacy"]
        if trade and strategy in ("current_target125", "target125_then_router"):
            portfolio.submit(engine.Opportunity(ds, trade.symbol, trade.direction, "current_target125",
                                                "07:00", trade.target_price, 0, 1, 2, 0,
                                                "legacy", trade.target_price), day["contexts"])
        for minute in range(420, 1116, 5):
            tm = engine.clock(minute)
            portfolio.step(tm, day["rows"].get(tm, {}))
            decision = engine.clock(minute + 5)
            if strategy in ("cash", "current_target125") or decision not in day["timeline"]:
                continue
            opportunities, market = day["timeline"][decision]
            candidates = (opportunity_filter(opportunities, market, day["contexts"])
                          if opportunity_filter else engine.route_opportunities(opportunities, market, strategy))
            for item in candidates:
                portfolio.submit(item, day["contexts"])
        portfolio.end_day()
    return portfolio.result()


def select(results):
    eligible = [(0, "cash")]
    for name, result in results.items():
        if (result["pnl_rub"] > 0 and result["daily_close_mdd_pct"] >= -15
                and not result["counters"].get("missing_bar_while_open", 0)):
            eligible.append((result["pnl_rub"], name))
    return max(eligible)[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development-only", action="store_true")
    args = parser.parse_args()
    manifest = json.loads((OUTPUT / "study_manifest.json").read_text())
    if manifest["strategies"] != list(engine.STRATEGIES):
        raise ValueError("Registered architecture family changed")
    prepared, audit = load_data(include_later=not args.development_only)
    write(OUTPUT / "data_audit.json", audit)
    choice_path = OUTPUT / "selected_architecture.json"
    if args.development_only:
        if choice_path.exists():
            raise ValueError("Architecture already frozen")
        fit = {name: compact(replay(prepared, name, last=FIT_END)) for name in engine.STRATEGIES}
        chosen = select(fit)
        write(choice_path, {"name": chosen, "cutoff": FIT_END, "fit_results": fit,
                            "manifest_sha256": hashlib.sha256((OUTPUT / "study_manifest.json").read_bytes()).hexdigest(),
                            "status": "research_only"})
        print(json.dumps({"chosen": chosen, "fit": fit}, ensure_ascii=False, indent=2))
        return
    choice = json.loads(choice_path.read_text())
    if choice["manifest_sha256"] != hashlib.sha256((OUTPUT / "study_manifest.json").read_bytes()).hexdigest():
        raise ValueError("Frozen contract differs")
    chosen = choice["name"]
    windows = {"full": (None, None), "fit": (None, FIT_END),
               "validation": ("2026-04-01", VALIDATION_END), "later": ("2026-07-17", None)}
    results = {}
    for name in engine.STRATEGIES:
        results[name] = {}
        for window, (first, last) in windows.items():
            value = replay(prepared, name, first=first, last=last)
            results[name][window] = compact(value)
            if window == "full":
                write(OUTPUT / f"ledger_{name}.json", value)
        print("Evaluated:", name, flush=True)
    # Verify the untouched old control on exactly the shared dates and capital.
    control_rows = [legacy.simulate(day["legacy"], legacy.Policy("target125", target_multiple=1.25))
                    for day in prepared.values() if day["legacy"]]
    expected = legacy.account(control_rows)
    actual = results["current_target125"]["full"]
    if abs(expected["ending_equity_rub"] - actual["ending_equity_rub"]) > 1e-6:
        raise ValueError(f"Current strategy does not reproduce: {actual['ending_equity_rub']} vs {expected['ending_equity_rub']}")
    stress = {}
    for bps in (0, 10, 20):
        stress[str(bps)] = {name: compact(replay(prepared, name, bps=bps, first="2026-07-17"))
                           for name in {chosen, "current_target125", "regime_router"}}
    c, b = results[chosen], results["current_target125"]
    gates = {"validation_profit_improved": c["validation"]["pnl_rub"] > b["validation"]["pnl_rub"],
             "later_profit_improved": c["later"]["pnl_rub"] > b["later"]["pnl_rub"],
             "full_drawdown_no_worse": c["full"]["daily_close_mdd_pct"] >= b["full"]["daily_close_mdd_pct"],
             "later_positive_at_10bps": stress["10"][chosen]["pnl_rub"] > 0,
             "multi_trade_architecture_chosen": chosen not in ("cash", "current_target125"),
             "complete_position_data": not c["full"]["counters"].get("missing_bar_while_open", 0)}
    report = {"selected": chosen, "verdict": "RESEARCH_CANDIDATE" if all(gates.values()) else "NO_CONFIRMED_PROFIT_IMPROVEMENT",
              "gates": gates, "data_audit": audit, "results": results, "late_slippage_stress": stress,
              "production_unchanged": True, "automatic_activation_allowed": False,
              "limitations": ["Early 100-name membership was selected through July16: retrospective membership bias remains.",
                              "July17--Sep9 is a reused chronological archive, not fresh forward evidence.",
                              "New Sep10--Oct2 data could not be downloaded: both public MOEX and read-only broker API are unavailable.",
                              "Fractional shares; no historical lot sizes, order books or short borrow permissions.",
                              "Many signals share the same days: trades are not independent observations.",
                              "Intrabar drawdown is an OHLC stress proxy, not tick-accurate."]}
    write(OUTPUT / "report.json", report)
    lines = ["# Новый внутридневной движок: постоянный поиск и общий капитал", "",
             f"Выбранная на данных до 31 марта архитектура: **{chosen}**. Вердикт: **{report['verdict']}**.", "",
             "Поиск каждые пять минут по всей доступной ликвидной вселенной. До трёх одновременно открытых позиций; капитал освобождается после выхода и снова используется. Повторный вход в бумагу разрешён через 30 минут, максимум дважды в день. Нет зависимости от вотчлиста или его T4.", "",
             "Начальный счёт 50 000 ₽; общий номинал до 150 000 ₽ и 3× капитала. Новая позиция до 50 000 ₽. Комиссия 0,04% на сторону, проскальзывание 5 б.п. Вход через пять минут после сигнала, только по последующим данным. Общий плановый риск стопов до 3% капитала; при дневном снижении на 3% новые входы прекращаются.", "",
             f"Общий период для всех архитектур: **{audit['dates'][0]} — {audit['dates'][1]}**, {audit['weekday_sessions']} будних сессий. Старый архив: {audit['old_symbols']} акций; поздний: {audit['late_symbols']} акций, зафиксированных до начала отрезка. Это другой период, чем расчёт 136 495 ₽, поэтому абсолютные итоги напрямую не сопоставляются.", "",
             "| Архитектура | Сделок | Дней с несколькими сделками | Итог, ₽ | Просадка по дням | Прибыль апр–16 июл, ₽ | Прибыль 17 июл–9 сен, ₽ |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for name, value in results.items():
        full = value["full"]
        lines.append(f"| {name} | {full['trades']} | {full['days_with_multiple_trades']} | {full['ending_equity_rub']:,.2f} | {full['daily_close_mdd_pct']:.2f}% | {value['validation']['pnl_rub']:,.2f} | {value['later']['pnl_rub']:,.2f} |")
    lines += ["", "## Что изменено в логике", "",
              "- Пробой диапазона последних 30 минут с подтверждением объёмом.",
              "- Возврат к скользящей средней и продолжение движения с относительной силой.",
              "- Возврат к средней цене после экстремального отклонения и свечи отвержения.",
              "- Маршрутизация по эффективности пути широкого рынка за последний час; промежуточный режим допускает ожидание.",
              "- Единый портфель, ограничение коррелированных позиций по прошлым доходностям, ликвидность и запрет погони за ушедшей ценой.",
              "- Одни и те же функции сканера и событий портфеля пригодны для воспроизведения и будущего бумажного контура.", "",
              "## Проверки выбранной архитектуры", ""]
    lines += [f"- {key}: {'PASS' if passed else 'FAIL'}" for key, passed in gates.items()]
    lines += ["", "## Издержки на позднем отрезке", "", "| Проскальзывание на сторону | Архитектура | Прибыль, ₽ |", "|---|---|---:|"]
    for bps, values in stress.items():
        for name, value in sorted(values.items()):
            lines.append(f"| {bps} б.п. | {name} | {value['pnl_rub']:,.2f} |")
    lines += ["", "## Качество выборки и границы вывода", "", *[f"- {text}" for text in report["limitations"]], "",
              "Количество сделок не заменяет независимые рыночные дни. Неудачные варианты показаны вместе с успешными; архитектура заморожена до поздней проверки. Прежние подходы с разовой дневной корзиной здесь не выдаются за постоянный поиск.", "",
              "[MOEX ISS](https://www.moex.com/a8531) — описание биржевых данных. [Bailey et al.](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf) — риск подгонки при переборе. [FINRA](https://www.finra.org/investors/investing/investment-products/stocks/day-trading) — частая торговля создаёт значимые комиссионные издержки; американские регуляторные правила к этому российскому счёту здесь не применяются.", "",
              "Боевой target125 и его манифесты сохранены. Движок находится в исследовательском контуре, реальных заявок не отправляет.", "",
              "Воспроизведение: `python3 -m argonus.research.research_scanner_v2 --development-only` для нового каталога отбора; затем `python3 -m argonus.research.research_scanner_v2`. Тесты: `python3 -m unittest -q test_continuous_intraday`."]
    (OUTPUT / "REPORT.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({"selected": chosen, "verdict": report["verdict"], "gates": gates}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
