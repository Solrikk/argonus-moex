#!/usr/bin/env python3
"""Offline comparison of fresh intraday engines and frozen Engine A.

All policies trade one basket a day, with a shared cap of 150k RUB on 50k
equity. Signals use completed bars; execution waits an additional five-minute
bar. Selection for the final chronological slice is frozen on Nov--Mar only.
The archive/universe has been used before; this is retrospective research.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from argonus.strategies import intraday_signals as signals
from argonus.research import research_flat_month_exit_risk as frozen


from argonus.paths import PROJECT_ROOT as ROOT
FIT_END = "2026-03-31"
START = "2025-11-01"
END = "2026-07-16"
FEE_SIDE_PCT = .04
START_EQUITY = 50_000.0
POSITION_CAP = 150_000.0
MAX_LEVERAGE = 3.0


@dataclass(frozen=True)
class Leg:
    date: str
    symbol: str
    direction: str
    engine: str
    entry_time: str
    entry_price: float
    exit_time: str | None
    exit_price: float | None
    net_return_pct: float
    weight: float
    reason: str
    # (time, cumulative return %, adverse return %), relative to leg notional.
    marks: tuple[tuple[str, float, float], ...]


def hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def next_bar(time: str) -> str:
    m = signals.minutes(time) + 5
    if m >= 24 * 60:
        raise ValueError("Next bar is outside the session")
    return f"{m // 60:02d}:{m % 60:02d}"


def replay(signal: signals.Signal, candles: list[list], slippage_bps: float = 5.0,
           *, entry_time: str | None = None, absolute_target: float | None = None,
           weight: float | None = None) -> Leg:
    """Stop-first, adverse per-side slip, fees on turnover, deadline open.

    Missing entry = unfilled order with no fallback symbol. Missing exit data
    while still in position = error, never a fabricated profitable exit.
    The open at decision+5m is still a fill assumption, not depth evidence.
    """
    if not math.isfinite(slippage_bps) or not 0 <= slippage_bps < 10000:
        raise ValueError("Invalid slippage")
    if signal.direction not in ("long", "short") or signal.max_positions <= 0:
        raise ValueError("Invalid signal direction or position count")
    if not 0 < signal.stop_pct < 100 or not 0 < signal.target_pct < 100:
        raise ValueError("Invalid stop/target")
    entry_time = entry_time or next_bar(signal.decision_time)
    if entry_time > signal.exit_time:
        raise ValueError("Entry after exit deadline")
    weight = weight if weight is not None else 1 / signal.max_positions
    if not 0 < weight <= 1:
        raise ValueError("Invalid portfolio weight")
    window = [r for r in candles if r[0] >= entry_time]
    signals._validate_rows(window)
    if not window or window[0][0] != entry_time:
        return Leg(signal.date, signal.symbol, signal.direction, signal.engine, entry_time,
                   0, None, None, 0, weight, "unfilled_missing_entry", ())
    sign = 1 if signal.direction == "long" else -1
    slip = slippage_bps / 10_000
    entry = window[0][1] * (1 + sign * slip)
    target = absolute_target if absolute_target is not None else entry * (1 + sign * signal.target_pct / 100)
    if not math.isfinite(target) or target <= 0:
        raise ValueError("Invalid target price")
    if sign * (target - entry) <= 0:
        return Leg(signal.date, signal.symbol, signal.direction, signal.engine, entry_time,
                   0, None, None, 0, weight, "unfilled_target_behind_entry", ())
    stop = entry * (1 - sign * signal.stop_pct / 100)
    marks = []

    def net(price):
        return sign * (price / entry - 1) * 100 - FEE_SIDE_PCT * (1 + price / entry)

    def finish(price, time, reason, adverse_price):
        fill = price * (1 - sign * slip)
        result = net(fill)
        adverse = min(result, net(adverse_price * (1 - sign * slip)))
        marks.append((time, result, adverse))
        return Leg(signal.date, signal.symbol, signal.direction, signal.engine, entry_time,
                   entry, time, fill, result, weight, reason, tuple(marks))

    for row in window:
        tm, opening, high, low, close, _ = row[:6]
        if sign * (opening - stop) <= 0:
            return finish(opening, tm, "stop_gap", opening)
        if sign * (opening - target) >= 0:
            return finish(target, tm, "target_at_open", target)
        if tm >= signal.exit_time:
            return finish(opening, tm, "time_exit_open", opening)
        stop_hit = low <= stop if sign > 0 else high >= stop
        target_hit = high >= target if sign > 0 else low <= target
        adverse_price = low if sign > 0 else high
        if stop_hit:
            return finish(stop, tm, "both_stop_first" if target_hit else "stop", stop)
        if target_hit:
            return finish(target, tm, "target", adverse_price)
        marks.append((tm, net(close * (1 - sign * slip)), net(adverse_price * (1 - sign * slip))))
    raise ValueError(f"Missing exit data: {signal.date} {signal.symbol}")


def account(days: dict[str, list[Leg]]) -> dict:
    equity = close_peak = mark_peak = START_EQUITY
    close_mdd = adverse_mdd = 0.0
    daily, ledger, monthly = [], [], {}
    for ds, legs in sorted(days.items()):
        if any(leg.date != ds for leg in legs) or len({x.symbol for x in legs}) != len(legs):
            raise ValueError("Basket identities invalid")
        if sum(leg.weight for leg in legs) > 1 + 1e-9:
            raise ValueError("Portfolio overallocated")
        if any(not math.isfinite(leg.net_return_pct) or not 0 < leg.weight <= 1 for leg in legs):
            raise ValueError("Invalid leg return/weight")
        month = monthly.setdefault(ds[:7], {"start_equity": equity, "trades": 0, "pnl_rub": 0.0})
        budget = min(POSITION_CAP, equity * MAX_LEVERAGE)
        before = equity
        mark_tables = [dict((t, (c, a)) for t, c, a in leg.marks) for leg in legs]
        latest = [0.0] * len(legs)
        for tm in sorted({t for leg in legs for t, _, _ in leg.marks}):
            adverse = latest.copy()
            for i, table in enumerate(mark_tables):
                if tm in table:
                    latest[i], adverse[i] = table[tm]
            mark_equity = before + sum(budget * leg.weight * value / 100 for leg, value in zip(legs, latest))
            low_equity = before + sum(budget * leg.weight * value / 100 for leg, value in zip(legs, adverse))
            # Low compared with peaks known from previous bar closes. This is
            # a stress proxy, not exact tick-level MDD; highs are unknown.
            adverse_mdd = min(adverse_mdd, (low_equity / mark_peak - 1) * 100)
            mark_peak = max(mark_peak, mark_equity)
        total_pnl = 0.0
        filled = 0
        for leg in legs:
            traded = leg.exit_price is not None
            notional = budget * leg.weight if traded else 0.0
            pnl = notional * leg.net_return_pct / 100
            total_pnl += pnl
            filled += int(traded)
            ledger.append({k: v for k, v in asdict(leg).items() if k != "marks"}
                          | {"position_rub": notional, "pnl_rub": pnl})
        equity += total_pnl
        if equity <= 0:
            raise ValueError(f"Account depleted: {ds}")
        close_peak = max(close_peak, equity)
        close_mdd = min(close_mdd, (equity / close_peak - 1) * 100)
        month["trades"] += filled
        month["pnl_rub"] += total_pnl
        month["equity_end"] = equity
        month["return_pct"] = (equity / month["start_equity"] - 1) * 100
        daily.append({"date": ds, "equity_before": before, "pnl_rub": total_pnl, "equity_after": equity,
                      "return_pct": total_pnl / before * 100, "trades": filled,
                      "position_rub": sum(budget * l.weight for l in legs if l.exit_price is not None)})
    positive = sum(max(row["pnl_rub"], 0) for row in ledger)
    by_symbol = defaultdict(float)
    for row in ledger:
        by_symbol[row["symbol"]] += row["pnl_rub"]
    return {"days": len(days), "active_days": sum(d["trades"] > 0 for d in daily),
            "trades": sum(d["trades"] for d in daily),
            "wins": sum(r["pnl_rub"] > 0 for r in ledger),
            "losses": sum(r["exit_price"] is not None and r["pnl_rub"] <= 0 for r in ledger),
            "unfilled": sum(r["exit_price"] is None for r in ledger),
            "pnl_rub": equity - START_EQUITY, "return_pct": (equity / START_EQUITY - 1) * 100,
            "equity_end": equity, "daily_close_mdd_pct": close_mdd,
            "intrabar_adverse_proxy_mdd_pct": adverse_mdd, "monthly": monthly,
            "max_leg_positive_share_pct": max((r["pnl_rub"] for r in ledger), default=0) / positive * 100 if positive else 0,
            "symbol_pnl": dict(sorted(by_symbol.items(), key=lambda kv: -kv[1])),
            "daily": daily, "ledger": ledger}


def compact(report: dict) -> dict:
    return {k: v for k, v in report.items() if k not in ("daily", "ledger")}


def utility(report: dict) -> float:
    # One ruble of maximum daily-close drawdown penalizes one ruble of PnL.
    return report["pnl_rub"] + START_EQUITY * report["daily_close_mdd_pct"] / 100


def choose(paths: dict[str, dict[str, list[Leg]]], cutoff: str) -> str:
    best, score = "cash", 0.0
    for name in sorted(paths):
        report = account({d: rows for d, rows in paths[name].items() if d <= cutoff})
        value = utility(report)
        if value > score + 1e-9:
            best, score = name, value
    return best


def load_inputs() -> tuple[dict, dict, dict]:
    manifest_path = ROOT / "data/intraday_universe/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    sessions, dailies, hashes = {}, {}, {str(manifest_path.relative_to(ROOT)): hash_file(manifest_path)}
    for symbol in sorted(manifest["selected"]):
        fm = ROOT / "data/intraday_universe/five_min" / f"{symbol}.json.gz"
        dj = ROOT / "data/intraday_universe/dailies" / f"{symbol}.json"
        if not fm.exists() or not dj.exists():
            raise ValueError(f"Missing manifest input: {symbol}")
        with gzip.open(fm, "rt") as handle:
            sessions[symbol] = json.load(handle)
        dailies[symbol] = json.loads(dj.read_text())
        hashes[str(fm.relative_to(ROOT))] = hash_file(fm)
        hashes[str(dj.relative_to(ROOT))] = hash_file(dj)
    return sessions, dailies, hashes


def prepare_signals(sessions: dict, dailies: dict) -> tuple[dict, list[str], dict]:
    dates = sorted({ds for book in sessions.values() for ds in book
                    if START <= ds <= END and date.fromisoformat(ds).weekday() < 5})
    snapshots = {}
    counts = Counter()
    for tm in sorted({e.decision_time for e in signals.ENGINES}):
        for ds in dates:
            rows = []
            for symbol in sorted(sessions):
                if ds not in sessions[symbol]:
                    counts["missing_session"] += 1
                    continue
                item = signals.snapshot(symbol, ds, dailies[symbol], sessions[symbol][ds], tm)
                if item is not None:
                    rows.append(item)
                else:
                    counts["insufficient_signal_history"] += 1
            snapshots[(ds, tm)] = rows
    selected = {engine.name: {ds: signals.select_signals(snapshots[(ds, engine.decision_time)], engine)
                              for ds in dates} for engine in signals.ENGINES}
    return selected, dates, dict(counts)


def build_paths(selected: dict, dates: list[str], sessions: dict, legacy_trades: list,
                bps: float, long_only: bool = False) -> dict:
    paths = {name: {} for name in selected}
    for name, day_signals in selected.items():
        for ds in dates:
            # Do not fill a removed short's allocation with another symbol.
            paths[name][ds] = [replay(s, sessions[s.symbol][ds], bps)
                               for s in day_signals[ds] if not long_only or s.direction == "long"]
    baseline = {ds: [] for ds in dates}
    for trade in legacy_trades:
        if trade.date not in baseline or (long_only and trade.direction != "long"):
            continue
        sig = signals.Signal(trade.date, trade.symbol, trade.direction, "baseline_A", "07:00",
                             trade.candles[0].open, 1, 1, 2, "18:35", 1, 0, "06:55")
        candles = [[c.time, c.open, c.high, c.low, c.close, 0] for c in trade.candles]
        baseline[trade.date] = [replay(sig, candles, bps, entry_time="07:05",
                                     absolute_target=trade.target_price, weight=1.0)]
    # A's eligibility is a frozen pre-entry decision, not whether A later wins.
    a_dates = {t.date for t in legacy_trades}
    for name in selected:
        paths[f"A_then_{name}"] = {ds: baseline[ds] if ds in a_dates else paths[name][ds] for ds in dates}
    paths["baseline_A"] = baseline
    paths["cash"] = {ds: [] for ds in dates}
    return paths


def walk_forward(paths: dict) -> dict:
    dates = sorted(paths["cash"])
    months = sorted({d[:7] for d in dates})
    stitched, folds = {}, []
    for month in months[3:]:
        prior = max(d for d in dates if d[:7] < month)
        chosen = choose(paths, prior)
        test = {d: legs for d, legs in paths[chosen].items() if d[:7] == month}
        stitched.update(test)
        folds.append({"month": month, "training_end": prior, "chosen": chosen})
    result = compact(account(stitched))
    result["folds"] = folds
    result["baseline_same_dates"] = compact(account({d: paths["baseline_A"][d] for d in stitched}))
    return result


def build_report() -> dict:
    sessions, dailies, hashes = load_inputs()
    selected, dates, coverage = prepare_signals(sessions, dailies)
    legacy, _, provenance = frozen.load_books()
    paths = build_paths(selected, dates, sessions, legacy, 5.0)
    chosen = choose(paths, FIT_END)
    results = {}
    for name, days in paths.items():
        full = account(days)
        results[name] = {"full": full,
                         "train": compact(account({d: legs for d, legs in days.items() if d <= FIT_END})),
                         "later": compact(account({d: legs for d, legs in days.items() if d > FIT_END})),
                         "april_may": compact(account({d: legs for d, legs in days.items() if "2026-04" <= d[:7] <= "2026-05"})),
                         "june_july": compact(account({d: legs for d, legs in days.items() if d[:7] >= "2026-06"}))}
    stresses = {}
    for bps in (0.0, 10.0, 20.0):
        stress_paths = build_paths(selected, dates, sessions, legacy, bps)
        stresses[str(bps)] = {name: {
            "full": compact(account(stress_paths[name])),
            "later": compact(account({d: legs for d, legs in stress_paths[name].items() if d > FIT_END})),
        } for name in paths}
    long_paths = build_paths(selected, dates, sessions, legacy, 5.0, long_only=True)
    long_only = {name: compact(account(days)) for name, days in long_paths.items()}
    paired = {}
    for name, days in paths.items():
        if name == "cash":
            continue
        chosen_later = results[name]["later"]
        baseline_later = results["baseline_A"]["later"]
        contributors = results[name]["full"]["daily"]
        best_day = max(contributors, key=lambda d: d["pnl_rub"])["date"]
        paired[name] = {
            "later_pnl_delta_rub": chosen_later["pnl_rub"] - baseline_later["pnl_rub"],
            "best_day": best_day,
            "full_pnl_without_best_day_rub": account({d: legs for d, legs in days.items() if d != best_day})["pnl_rub"],
        }
    # Keep all comparisons visible; do not replace the frozen training winner
    # with the best performer after seeing April--July.
    chosen_result, baseline = results[chosen], results["baseline_A"]
    passes = (chosen not in ("cash", "baseline_A")
              and chosen_result["later"]["pnl_rub"] > baseline["later"]["pnl_rub"]
              and all(chosen_result[part]["pnl_rub"] > baseline[part]["pnl_rub"] for part in ("april_may", "june_july"))
              and chosen_result["later"]["daily_close_mdd_pct"] >= baseline["later"]["daily_close_mdd_pct"]
              and stresses["20.0"][chosen]["later"]["pnl_rub"] > 0)
    return {"schema_version": 1, "mode": "offline_research", "production_activation_allowed": False,
            "verdict": "CANDIDATE_FOR_NEW_DATA" if passes else "NO_ROBUST_REPLACEMENT",
            "fit_end": FIT_END, "selected_using_training_only": chosen,
            "period": [dates[0], dates[-1]], "calendar_days": len(dates),
            "engines": [asdict(e) for e in signals.ENGINES],
            "costs": {"fee_side_pct": FEE_SIDE_PCT, "slippage_side_bps": 5.0},
            "coverage": coverage, "input_sha256": hashes, "legacy_provenance": provenance,
            "code_sha256": {f: hash_file(ROOT / f) for f in ("argonus/strategies/intraday_signals.py", "argonus/research/research_intraday_portfolio.py")},
            "results": results, "walk_forward": walk_forward(paths), "slippage_stress": stresses,
            "long_only_sensitivity": long_only, "diagnostics": paired,
            "limitations": [
                "Universe membership selected through July: survivorship bias remains.",
                "Liquidity ranks use only past data; lots*price is a lower bound, not actual turnover.",
                "No historical short availability, margin limits, order book or lot rounding.",
                "Fixed entry at decision+5m open plus slippage is a fill assumption.",
                "Costs omit instrument-specific borrowing fees and taxes.",
                "All dates were previously researched; later slices are retrospective.",
                "Intrabar adverse MDD compares adverse prices with prior-close peaks, not exact ticks.",
                "Cash and baseline are included in training choices; every tested policy is reported.",
            ]}


def markdown(report: dict) -> str:
    results = report["results"]
    chosen = report["selected_using_training_only"]
    lines = ["# Новые внутридневные стратегии — 2026-09-10", "",
             f"Вердикт: **{report['verdict']}**. По данным до 31 марта выбран `{chosen}`.", "",
             f"Архив: {report['period'][0]} — {report['period'][1]}, {report['calendar_days']} будних сессий. "
             "Начальный счёт 50 000 ₽; общий лимит всех позиций 150 000 ₽ и не более 3× текущего капитала. "
             "Новые стратегии держат до трёх позиций по 50 000 ₽; при одном сигнале остальные деньги не занимают.", "",
             "Сигналы в 10:30 или 13:30 используют только завершённые свечи. Вход моделируется "
             "через пять минут по open плюс 5 б.п. проскальзывания; столько же на выходе. "
             "Комиссия 0,04% на сторону. Гэпы через стоп исполняются по худшему open; неоднозначные свечи — stop-first; "
             "выход по времени использует open в 18:35. Ликвидность берётся из прошлых 20 дней и последних "
             "30 минут, без отбора по будущему объёму.", "",
             "`A_then_*` означает прежнюю стратегию A в её дни и новую в остальные дни; "
             "дополнительное плечо для совмещения не используется.", "",
             "| Стратегия | Сделки / активные дни | Вся история | Апр–июл | MDD по дням | Внутридневной стресс-прокси |",
             "|---|---:|---:|---:|---:|---:|"]
    for name in sorted(results, key=lambda n: -results[n]["train"]["pnl_rub"]):
        r = results[name]
        f = r["full"]
        lines.append(f"| {name} | {f['trades']} / {f['active_days']} | {f['return_pct']:+.2f}% | "
                     f"{r['later']['return_pct']:+.2f}% | {f['daily_close_mdd_pct']:.2f}% | {f['intrabar_adverse_proxy_mdd_pct']:.2f}% |")
    lines += ["", "## Выбор до просмотра последующих месяцев", "",
              "Оценка на обучении: прибыль минус величина максимальной просадки по закрытию дня в рублях "
              "начального счёта. Разрешены прежняя стратегия и деньги без позиции. "
              "Параметры восьми новых правил фиксированы в коде; вся семья из 18 вариантов показана выше.", "",
              "| Окно | Текущая A | Выбранный вариант | Δ PnL, ₽ |", "|---|---:|---:|---:|"]
    for part, title in (("train", "Ноя–мар, выбор"), ("april_may", "Апр–май"), ("june_july", "Июн–июл"), ("later", "Апр–июл")):
        c, b = results[chosen][part], results["baseline_A"][part]
        lines.append(f"| {title} | {b['return_pct']:+.2f}% | {c['return_pct']:+.2f}% | {c['pnl_rub'] - b['pnl_rub']:+,.0f} |")
    w = report["walk_forward"]
    lines += ["", f"Ежемесячный выбор по расширяющейся прошлой истории: {w['return_pct']:+.2f}% "
              f"против {w['baseline_same_dates']['return_pct']:+.2f}% у A на тех же датах. "
              f"Выборы: {', '.join(f['month'] + '=' + f['chosen'] for f in w['folds'])}.", "",
              "## Издержки", "", "| Проскальзывание, б.п. на сторону | A, апр–июл | Выбранный, апр–июл |",
              "|---|---:|---:|"]
    for bps, stress in report["slippage_stress"].items():
        lines.append(f"| {bps} | {stress['baseline_A']['later']['return_pct']:+.2f}% | {stress[chosen]['later']['return_pct']:+.2f}% |")
    lines += ["", "## Ограничения", "",
              "Текущий список акций сформирован с использованием данных по июль; фильтрация ликвидности "
              "по прошлому не устраняет смещение состава списка. История уже использовалась в прежних исследованиях, "
              "поэтому апрель–июль не является полностью новой проверкой. Архив не содержит доступности шорта, "
              "реального стакана и индивидуальных лимитов брокера. Лотность, финансирование и налоги не моделируются. "
              "Отдельный расчёт без шортов, все сделки, месяцы, выбранные правила и хэши сохранены в JSON.", "",
              "Внутридневная просадка — приближённый стресс по неблагоприятным ценам свечей относительно "
              "предыдущих закрытий; фактический максимум между тиками может отличаться. "
              "Наличие свечей не гарантирует исполнение заявок на указанный объём.", "",
              "Объём свечей API выражен в лотах: "
              "[T-Invest Market Data](https://tinkoff.github.io/investAPI/marketdata/). "
              "Здесь произведение объёма на цену считается нижней оценкой оборота, без выдуманного исторического размера лота.", "",
              "Реальные заявки, состояние счёта и файлы боевой активации не изменены.", "",
              "```bash", "python3 -m argonus.research.research_intraday_portfolio --output-dir data/backtests/intraday_portfolio_2026-09-10",
              "python3 -m unittest -q test_intraday_portfolio", "```", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    report = build_report()
    text = markdown(report)
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        (args.output_dir / "REPORT.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
