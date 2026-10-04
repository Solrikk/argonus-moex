#!/usr/bin/env python3
"""Frozen, reproducible 60% win-rate study; never imports the trading bot.

First run --development-only to freeze a candidate using dates <= July 16.
Then run without flags to evaluate it on July 17--September 29. The complete
alternative table is exploratory; it cannot change the frozen candidate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict
from pathlib import Path

import numpy as np
from scipy.stats import binomtest

from argonus.research import research_0705_execution as base
from argonus.research import research_flat_month_exit_risk as books
from argonus.research import research_new_period_engine_a as inputs
from argonus.strategies.win_rate_policy import RULES, Rule, simulate


from argonus.paths import PROJECT_ROOT as ROOT
OUTPUT = ROOT / "data/backtests/win_rate_60_2026-10-03"
FIT_END = "2026-01-31"
DEVELOPMENT_END = "2026-07-16"
SOURCES = (
    ROOT / "data/backtests/new_period_engine_a_2026-09-30/market_cache.json.gz",
    ROOT / "data/backtests/inverted_direction_2026-09-30/history_cache.json.gz",
    ROOT / "data/backtests/all_time_engine_a_rs5_2026-09-29/report.json",
)


def write_json(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def sha(obj: dict) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def study_contract() -> dict:
    return {
        "rules": [asdict(rule) for rule in RULES],
        "fit_end": FIT_END, "selection_cutoff": DEVELOPMENT_END,
        "test_start": "2026-07-17", "test_end": "2026-09-29",
        "winner": "total position profit strictly positive AFTER all costs; partial legs count as ONE trade",
        "development_gates": {
            "fit_win_rate_min_pct": 55, "validation_win_rate_min_pct": 60,
            "combined_win_rate_min_pct": 60, "minimum_retention_fraction": 2 / 3,
            "minimum_pnl_fraction_of_control": .95,
            "mdd_no_worse_than_control": True,
        },
        "ranking": "highest development Wilson lower bound, then net PnL, then retained trades",
        "confirmation_gates": {
            "full_and_test_win_rate_min_pct": 60, "minimum_test_trades": 10,
            "full_and_test_pnl_no_worse_than_control": True,
            "full_and_test_mdd_no_worse_than_control": True,
            "net_positive_at_10bps_side_slippage": True,
        },
        "sample_status": "Reused archived chronological test, not untouched forward evidence",
        "new_orders_allowed": False, "automatic_activation_allowed": False,
        "sources": [{"path": str(p.relative_to(ROOT)), "sha256": inputs.october.file_sha256(p)} for p in SOURCES],
    }


def load_data(include_test: bool) -> tuple[list, dict[str, dict]]:
    _, frozen, _ = books.load_books()
    historical = inputs.read_json_gz(SOURCES[1])
    days = list(historical["policies"]["live"])
    trades = list(frozen)
    if include_test:
        cache = inputs.read_json_gz(SOURCES[0])
        days += cache["selection_days"]
        for day in cache["selection_days"]:
            selected = day["selected"]
            if not selected:
                continue
            key = f"{day['date']}|{selected['symbol']}"
            session = cache["sessions"][key]
            if session["status"] != "ok" or not session["rows"]:
                raise RuntimeError(f"Incomplete market data: {key}")
            if inputs.october.canonical_sha256(session["rows"]) != session["session_sha256"]:
                raise RuntimeError(f"Market-data hash mismatch: {key}")
            trades.append(inputs.to_trade(day, session))
    by_date = {day["date"]: day for day in days}
    features = {}
    for trade in trades:
        day = by_date[trade.date]
        selected = day["selected"]
        if (selected["symbol"], selected["direction"], selected["target_price"]) != (
            trade.symbol, trade.direction, trade.target_price
        ):
            raise RuntimeError(f"Archived selection mismatch: {trade.date}")
        candidate = next(row for row in day["rs5_policy"]["candidates"] if row["rank"] == selected["rank"])
        for key in ("stock_window", "index_window"):
            if candidate[key][-1] >= trade.date:
                raise RuntimeError("Feature uses trade-day/future data")
        sign = 1 if trade.direction == "long" else -1
        features[trade.date] = {
            "aligned_rs5_pp": candidate["aligned_rs5_pp"],
            "aligned_stock_return5_pct": sign * candidate["stock_return_5d_pct"],
            "aligned_market_return5_pct": sign * candidate["index_return_5d_pct"],
        }
    return sorted(trades, key=lambda trade: trade.date), features


def wilson(wins: int, total: int) -> tuple[float, float]:
    if not total:
        return 0.0, 100.0
    z = 1.959963984540054
    p = wins / total
    center = (p + z * z / (2 * total)) / (1 + z * z / total)
    width = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return 100 * (center - width), 100 * (center + width)


def metrics(rows: list) -> dict:
    account = base.account(rows)
    low, high = wilson(account["wins"], account["trades"])
    profits = [row["pnl_rub"] for row in account["per_trade"] if row["traded"]]
    positive, negative = sum(p for p in profits if p > 0), -sum(p for p in profits if p <= 0)
    return {
        **base.summary(account),
        "win_rate_pct": account["wins"] / account["trades"] * 100 if account["trades"] else 0.0,
        "win_rate_wilson_95_pct": [low, high],
        "profit_factor": positive / negative if negative else None,
        "average_trade_net_pct": float(np.mean([r.net_return_pct for r in rows if r.traded])) if account["trades"] else 0.0,
        "missing_execution_sessions": sum(r.reason.startswith("unavailable_") for r in rows),
    }


def evaluate(trades: list, features: dict, rules: tuple = RULES,
             fills: base.FillModel = base.FillModel()) -> dict[str, list]:
    result = {}
    for rule in rules:
        outcomes = []
        for trade in trades:
            try:
                outcomes.append(simulate(trade, rule, fills, features[trade.date]))
            except ValueError as exc:
                if not str(exc).startswith("Missing exact "):
                    raise
                # Record the original signal on its date. This rule is barred
                # from selection/promotion, so missing bars cannot improve it.
                outcomes.append(base.Execution(
                    trade.date, trade.month, trade.symbol, trade.direction,
                    rule.name, False, 0.0, None, None,
                    "unavailable_missing_exact_entry_candle", 0.0, 0.0,
                    min(1.0, 1.0 / rule.stop_pct),
                ))
        result[rule.name] = outcomes
    return result


def pick_development(paths: dict[str, list], cutoff: str = DEVELOPMENT_END) -> tuple[str, list[dict]]:
    control = metrics([row for row in paths["baseline"] if row.date <= cutoff])
    eligible = []
    audit = []
    for name, path in paths.items():
        rows = [row for row in path if row.date <= cutoff]
        full = metrics(rows)
        fit = metrics([row for row in rows if row.date <= FIT_END])
        validation = metrics([row for row in rows if row.date > FIT_END])
        gates = {
            "complete_execution_data": full["missing_execution_sessions"] == 0,
            "fit_win_rate": fit["win_rate_pct"] >= 55,
            "validation_win_rate": validation["win_rate_pct"] >= 60,
            "combined_win_rate": full["win_rate_pct"] >= 60,
            "retention": full["trades"] >= math.ceil(control["trades"] * 2 / 3),
            "pnl": full["pnl_rub"] >= .95 * control["pnl_rub"],
            "mdd": full["closed_trade_mdd_pct"] >= control["closed_trade_mdd_pct"] - 1e-10,
        }
        audit.append({"name": name, "fit": fit, "validation": validation, "development": full, "gates": gates})
        if name != "baseline" and all(gates.values()):
            eligible.append((full["win_rate_wilson_95_pct"][0], full["pnl_rub"], full["trades"], name))
    return (max(eligible)[-1] if eligible else "baseline"), audit


def uncertainty(candidate: list, control: list) -> dict:
    months = sorted({row.month for row in candidate})
    month_delta = np.array([
        sum(1500 * (c.net_return_pct * c.exposure_fraction * c.traded
                    - b.net_return_pct * b.exposure_fraction * b.traded)
            for c, b in zip(candidate, control) if c.month == month)
        for month in months
    ])
    rng = np.random.default_rng(20261003)
    sums = rng.choice(month_delta, (10000, len(month_delta)), replace=True).sum(axis=1)
    plus = minus = 0
    for c, b in zip(candidate, control):
        if not c.traded or not b.traded:
            continue
        plus += int(c.net_return_pct > 0 >= b.net_return_pct)
        minus += int(b.net_return_pct > 0 >= c.net_return_pct)
    p = float(binomtest(plus, plus + minus, .5, alternative="greater").pvalue) if plus + minus else 1.0
    return {
        "paired_month_block_bootstrap_delta_fixed_150k_rub_95": list(map(float, np.quantile(sums, [.025, .975]))),
        "bootstrap_description": "Paired month-block delta at fixed 150k, not a leveraged continuous-account confidence interval",
        "matched_trades_changed_to_win": plus, "matched_trades_changed_to_loss": minus,
        "paired_accuracy_sign_test_p": p,
        "exploratory_family_bonferroni_p": min(1.0, p * (len(RULES) - 1)),
        "pvalue_limitation": "Conditional historical diagnostic; prior selection and reused strategy models remain biased",
    }


def expanding(paths: dict[str, list]) -> dict:
    months = sorted({row.month for row in paths["baseline"]})
    candidate, control, selections = [], [], []
    for month in months:
        history = {name: [row for row in rows if row.month < month] for name, rows in paths.items()}
        chosen = "baseline"
        if sum(row.traded for row in history["baseline"]) >= 23 and month > "2026-02":
            cutoff = max(row.date for row in history["baseline"])
            chosen, _ = pick_development(history, cutoff)
        candidate.extend(row for row in paths[chosen] if row.month == month)
        control.extend(row for row in paths["baseline"] if row.month == month)
        selections.append({"month": month, "rule_selected_using_earlier_months": chosen})
    return {"candidate": metrics(candidate), "control": metrics(control), "selections": selections}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development-only", action="store_true")
    args = parser.parse_args()
    OUTPUT.mkdir(exist_ok=True)
    contract = study_contract()
    contract_path = OUTPUT / "study_manifest.json"
    if contract_path.exists():
        if json.loads(contract_path.read_text()) != contract:
            raise RuntimeError("Study contract or inputs changed; do not silently retune this study")
    else:
        write_json(contract_path, contract)
    selected_path = OUTPUT / "selected_candidate.json"
    if args.development_only:
        if selected_path.exists():
            raise RuntimeError("Candidate already frozen; run the evaluation without --development-only")
        trades, features = load_data(False)
        paths = evaluate(trades, features)
        name, audit = pick_development(paths)
        selected = {"rule": asdict(next(rule for rule in RULES if rule.name == name)),
                    "contract_sha256": sha(contract), "selection_cutoff": DEVELOPMENT_END,
                    "selection_did_not_read_test_outcomes": True,
                    "activation_status": "research_only", "audit": audit}
        write_json(selected_path, selected)
        print(json.dumps({"selected": name, "development": metrics(paths[name]), "baseline": metrics(paths["baseline"])}, ensure_ascii=False, indent=2))
        return
    if not selected_path.exists():
        raise RuntimeError("Run --development-only first to freeze the candidate")
    selected = json.loads(selected_path.read_text())
    if selected["contract_sha256"] != sha(contract):
        raise RuntimeError("Frozen candidate contract mismatch")
    chosen = Rule(**selected["rule"])
    if chosen not in RULES:
        raise RuntimeError("Candidate is not a declared rule")
    trades, features = load_data(True)
    paths = evaluate(trades, features)
    current = json.loads(SOURCES[2].read_text())["summary"]
    control = metrics(paths["baseline"])
    for key in ("trades", "wins", "losses", "ending_equity_rub", "closed_trade_mdd_pct"):
        if not math.isclose(control[key], current[key], abs_tol=1e-8):
            raise RuntimeError(f"All-time baseline mismatch: {key}")
    all_metrics = {}
    for name, rows in paths.items():
        all_metrics[name] = {
            "full": metrics(rows),
            "development": metrics([row for row in rows if row.date <= DEVELOPMENT_END]),
            "test": metrics([row for row in rows if row.date > DEVELOPMENT_END]),
        }
    candidate = all_metrics[chosen.name]
    test_control = all_metrics["baseline"]["test"]
    stress = {}
    for bps in (0, 5, 10, 20):
        stressed = evaluate(trades, features, (RULES[0], chosen), base.FillModel(.04, bps))
        stress[str(bps)] = {name: metrics(rows) for name, rows in stressed.items()}
    gates = {
        "complete_execution_data": candidate["full"]["missing_execution_sessions"] == 0,
        "new_rule_selected": chosen.name != "baseline",
        "full_win_rate_60": candidate["full"]["win_rate_pct"] >= 60,
        "test_win_rate_60": candidate["test"]["win_rate_pct"] >= 60,
        "test_minimum_10_trades": candidate["test"]["trades"] >= 10,
        "full_pnl_no_worse": candidate["full"]["pnl_rub"] >= control["pnl_rub"],
        "test_pnl_no_worse": candidate["test"]["pnl_rub"] >= test_control["pnl_rub"],
        "full_mdd_no_worse": candidate["full"]["closed_trade_mdd_pct"] >= control["closed_trade_mdd_pct"] - 1e-10,
        "test_mdd_no_worse": candidate["test"]["closed_trade_mdd_pct"] >= test_control["closed_trade_mdd_pct"] - 1e-10,
        "positive_at_10bps": stress["10"][chosen.name]["pnl_rub"] > 0,
    }
    report = {"contract": contract, "frozen_candidate": selected["rule"],
              "verdict": "FORWARD_CANDIDATE" if all(gates.values()) else "NO_CONFIRMED_60_PERCENT_IMPROVEMENT",
              "gates": gates, "selected": candidate, "baseline": all_metrics["baseline"],
              "alternatives_exploratory_not_used_for_selection": all_metrics,
              "uncertainty": uncertainty(paths[chosen.name], paths["baseline"]),
              "expanding_monthly_selection": expanding(paths), "slippage_stress": stress,
              "candidate_trades": base.account(paths[chosen.name])["per_trade"],
              "baseline_trades": base.account(paths["baseline"])["per_trade"],
              "code_sha256": {name: inputs.october.file_sha256(ROOT / name) for name in ("argonus/strategies/win_rate_policy.py", "argonus/research/research_win_rate_60.py")}}
    write_json(OUTPUT / "report.json", report)
    lines = ["# Исследование доли прибыльных сделок ≥60%", "",
             f"Вердикт: **{report['verdict']}**. Замороженный до проверки кандидат: `{chosen.name}`.", "",
             "Контроль: A + RS5; старт 50 000 ₽, позиция min(150 000 ₽, 3 × капитал), комиссия 0,04% и проскальзывание 0,05% на сторону. Победа — положительный итог всей позиции после расходов. Частичные закрытия не увеличивают число сделок.", "",
             "Отбор использует только сделки до 16 июля 2026; дальнейший отрезок — 17 июля–29 сентября. Это повторно используемая историческая проверка, а не нетронутый форвард. Все прежние модели выбора акций также содержат историческую подгонку.", "",
             "| Вариант | Период | Сделок | Прибыльных / убыточных | Доля прибыльных | Доходность | Просадка по закрытым сделкам |",
             "|---|---|---:|---:|---:|---:|---:|"]
    for name in ("baseline", chosen.name):
        for window in ("development", "test", "full"):
            row = all_metrics[name][window]
            lines.append(f"| {name} | {window} | {row['trades']} | {row['wins']}/{row['losses']} | {row['win_rate_pct']:.2f}% | {row['return_pct']:+.2f}% | {row['closed_trade_mdd_pct']:.2f}% |")
    lines += ["", "## Все заранее заданные правила", "",
              "Следующая таблица исследовательская: поздние результаты не использовались для смены выбранного кандидата.", "",
              "| Правило | N всей истории | Доля прибыльных | Доходность | Просадка | N позднего периода | Доля прибыльных позднего периода |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for name, windows in all_metrics.items():
        row, late = windows["full"], windows["test"]
        lines.append(f"| {name} | {row['trades']} | {row['win_rate_pct']:.2f}% | {row['return_pct']:+.2f}% | {row['closed_trade_mdd_pct']:.2f}% | {late['trades']} | {late['win_rate_pct']:.2f}% |")
    lines += ["", "## Проверки кандидата", ""]
    lines += [f"- {key}: {'PASS' if value else 'FAIL'}" for key, value in gates.items()]
    lines += ["", "## Научные основания", "",
              "Размер среднего выигрыша и проигрыша влияет на прибыль наряду с долей побед: [CME, Mathematical Expectation](https://www.cmegroup.com/education/courses/trading-psychology/the-mathematics-of-trading-success.hideSubnav.educationIframe.html?hideAddThisExt=y&hideFooter=y&hideHeader=y&hideRightRail=y). Поэтому проверены прибыль и просадка, а не только процент побед.", "",
              "Перебор вариантов создаёт риск ложного улучшения: [Bailey et al., The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf). Набор из 28 кандидатов и контроль зафиксированы до расчёта, поздний период исключён из отбора; опубликованы все результаты и интервалы неопределённости.", "",
              "[Moskowitz, Ooi, Pedersen, Time series momentum](https://fairmodel.econ.yale.edu/ec439/mosk.pdf) изучает более длинные горизонты и фьючерсы. Фильтры импульса здесь — проверяемая гипотеза переноса на внутридневные акции, а не вывод статьи об этих данных.", "",
              "Ни один исторический результат сам по себе не активирует заявки. Правила реализованы в чистом модуле `win_rate_policy.py`. Лотность, индивидуальная маржа, финансирование и налоги не моделируются.", "",
              "## Воспроизведение", "", "```bash", "python3 -m argonus.research.research_win_rate_60", "python3 -m unittest -q test_win_rate_policy test_research_0705_execution", "```", "",
              "Исходный выбор воспроизводится из `selected_candidate.json`; его менять по итогам позднего периода нельзя. Подробные сделки, все правила, интервалы, ежемесячный выбор по прошлой истории и хеши — в `report.json`."]
    (OUTPUT / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"candidate": chosen.name, "verdict": report["verdict"], "selected": candidate, "gates": gates}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
