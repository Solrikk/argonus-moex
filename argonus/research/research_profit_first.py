#!/usr/bin/env python3
"""Profit-first follow-up after the user's corrected objective.

Candidate selection reads only the 65 trades before July 17. It does not
alter or reopen the frozen win-rate study. Later results cannot retune this
candidate; all other late comparisons are labelled exploratory.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict
from pathlib import Path

import numpy as np

from argonus.research import research_0705_execution as base
from argonus.research import research_win_rate_60 as study


from argonus.paths import PROJECT_ROOT as ROOT
OUTPUT = ROOT / "data/backtests/profit_first_2026-10-03"
EXITS = tuple(policy for policy in base.POLICIES if policy.name != "baseline")


def contract() -> dict:
    return {
        "objective": "Maximize net profit; win rate is descriptive, with no 60% constraint",
        "reason_for_new_study": "User explicitly corrected the objective to profit first",
        "selection_cutoff": study.DEVELOPMENT_END,
        "rules": [asdict(rule) for rule in study.RULES],
        "additional_exit_policies": [asdict(policy) for policy in EXITS],
        "selection": "Highest development net PnL among complete rules retaining >=2/3 of trades and with no worse closed-trade drawdown than control",
        "test": "July 17--September 29 is reused historical evidence; September 30--October 2 is a separate newly collected tail",
        "account": "Start 50k; notional min(150k, 3*equity); wider stops reduce exposure; no deposits",
        "fees": asdict(base.FillModel()),
        "source_hashes": [{"path": str(p.relative_to(ROOT)), "sha256": study.inputs.october.file_sha256(p)} for p in study.SOURCES],
        "orders_allowed": False, "automatic_activation_allowed": False,
    }


def evaluate(trades: list, features: dict, fills=base.FillModel()) -> dict:
    paths = study.evaluate(trades, features, fills=fills)
    for policy in EXITS:
        paths[policy.name] = [base.simulate(trade, policy, fills) for trade in trades]
    return paths


def pick(paths: dict) -> tuple[str, list]:
    # Guard the cutoff even if a caller accidentally supplies later trades.
    paths = {name: [row for row in rows if row.date <= study.DEVELOPMENT_END]
             for name, rows in paths.items()}
    control = study.metrics(paths["baseline"])
    candidates, audit = [], []
    for name, rows in paths.items():
        m = study.metrics(rows)
        gates = {
            "complete_execution_data": m["missing_execution_sessions"] == 0,
            "retention": m["trades"] >= math.ceil(control["trades"] * 2 / 3),
            "mdd_no_worse": m["closed_trade_mdd_pct"] >= control["closed_trade_mdd_pct"] - 1e-10,
        }
        audit.append({"name": name, "development": m, "gates": gates})
        if all(gates.values()):
            candidates.append((m["pnl_rub"], m["closed_trade_mdd_pct"], name))
    return max(candidates)[-1], audit


def select_rule(name: str) -> dict:
    for rule in study.RULES:
        if rule.name == name:
            return {"implementation": "argonus.strategies.win_rate_policy.Rule", "parameters": asdict(rule)}
    for policy in EXITS:
        if policy.name == name:
            return {"implementation": "argonus.research.research_0705_execution.Policy", "parameters": asdict(policy)}
    raise ValueError("Unknown rule")


def summarize(m: dict) -> dict:
    return {key: m[key] for key in ("trades", "wins", "losses", "win_rate_pct", "return_pct",
                                  "pnl_rub", "ending_equity_rub", "closed_trade_mdd_pct")}


def cash_curve(rows: list) -> list:
    return [50000.0] + [row["equity_after_rub"] for row in base.account(rows)["per_trade"]]


def paired_profit(candidate: list, control: list) -> dict:
    # Respect month-level dependence and keep shared dates paired. This is a
    # conditional historical interval, not proof after prior strategy mining.
    if not candidate or [r.date for r in candidate] != [r.date for r in control]:
        raise ValueError("Paired paths must have the same nonempty date calendar")
    months = sorted({row.month for row in candidate})
    blocks = {month: [(c, b) for c, b in zip(candidate, control) if c.month == month] for month in months}
    rng = np.random.default_rng(20261003)
    samples = []
    for _ in range(5000):
        eq_c = eq_b = 50000.0
        for month in rng.choice(months, len(months), replace=True):
            for c, b in blocks[month]:
                if c.traded:
                    eq_c += min(150000.0, 3 * eq_c) * c.exposure_fraction * c.net_return_pct / 100
                if b.traded:
                    eq_b += min(150000.0, 3 * eq_b) * b.exposure_fraction * b.net_return_pct / 100
        samples.append(eq_c - eq_b)
    delta = [1500 * (c.exposure_fraction * c.net_return_pct * c.traded
                     - b.exposure_fraction * b.net_return_pct * b.traded)
             for c, b in zip(candidate, control)]
    best_index = int(np.argmax(delta))
    c_without = base.account([r for i, r in enumerate(candidate) if i != best_index])
    b_without = base.account([r for i, r in enumerate(control) if i != best_index])
    return {
        "paired_month_bootstrap_account_pnl_delta_95_rub": list(map(float, np.quantile(samples, [.025, .975]))),
        "bootstrap_probability_delta_positive": float(np.mean(np.array(samples) > 0)),
        "without_best_contributor_delta_rub": c_without["pnl_rub"] - b_without["pnl_rub"],
        "best_contributor_date": candidate[best_index].date,
        "limitation": "Conditional historical diagnostic after reuse; not an independent significance claim",
    }


def newly_collected_tail(chosen: str, historical_paths: dict) -> dict | None:
    path = OUTPUT / "fresh_tail/market_cache.json.gz"
    if not path.exists():
        return None
    cache = study.inputs.read_json_gz(path)
    trades, features = [], {}
    for day in cache["selection_days"]:
        selected = day["selected"]
        if not selected:
            continue
        key = f"{day['date']}|{selected['symbol']}"
        session = cache["sessions"][key]
        if session["status"] != "ok" or not session["rows"]:
            raise RuntimeError(f"Incomplete fresh session: {key}")
        if study.inputs.october.canonical_sha256(session["rows"]) != session["session_sha256"]:
            raise RuntimeError("Fresh session hash mismatch")
        trades.append(study.inputs.to_trade(day, session))
        c = next(c for c in day["rs5_policy"]["candidates"] if c["rank"] == selected["rank"])
        sign = 1 if selected["direction"] == "long" else -1
        if any(c[k][-1] >= day["date"] for k in ("stock_window", "index_window")):
            raise RuntimeError("Fresh feature leakage")
        features[day["date"]] = {
            "aligned_rs5_pp": c["aligned_rs5_pp"],
            "aligned_stock_return5_pct": sign * c["stock_return_5d_pct"],
            "aligned_market_return5_pct": sign * c["index_return_5d_pct"],
        }
    fresh_paths = {"baseline": [base.simulate(t) for t in trades]}
    policy = next((p for p in EXITS if p.name == chosen), None)
    if policy is not None:
        fresh_paths[chosen] = [base.simulate(t, policy) for t in trades]
    else:
        rule = next(r for r in study.RULES if r.name == chosen)
        fresh_paths.update(study.evaluate(trades, features, rules=(rule,)))
    return {
        "period": cache["period"], "sessions": len(cache["calendar"]),
        "candidate_frozen_before_collection": True,
        "source_sha256": study.inputs.october.file_sha256(path),
        "standalone": {name: study.metrics(rows) for name, rows in fresh_paths.items()},
        "continuous_with_archive": {name: study.metrics(historical_paths[name] + rows)
                                    for name, rows in fresh_paths.items()},
        "trades": {name: base.account(rows)["per_trade"] for name, rows in fresh_paths.items()},
        "selection_days": cache["selection_days"],
        "limitation": "Only three sessions; current universe/trading flags snapshot, insufficient confirmation",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development-only", action="store_true")
    args = parser.parse_args()
    OUTPUT.mkdir(exist_ok=True)
    manifest = contract()
    manifest_path = OUTPUT / "study_manifest.json"
    if manifest_path.exists():
        if json.loads(manifest_path.read_text()) != manifest:
            raise RuntimeError("Profit study contract/inputs changed")
    else:
        study.write_json(manifest_path, manifest)
    selected_path = OUTPUT / "selected_candidate.json"
    if args.development_only:
        if selected_path.exists():
            raise RuntimeError("Profit candidate is already frozen")
        trades, features = study.load_data(False)
        paths = evaluate(trades, features)
        chosen, audit = pick(paths)
        profile = {"name": chosen, "rule": select_rule(chosen),
                   "contract_sha256": study.sha(manifest), "selection_cutoff": study.DEVELOPMENT_END,
                   "status": "research_only", "development_audit": audit}
        study.write_json(selected_path, profile)
        print(json.dumps({"selected": chosen, "candidate": summarize(study.metrics(paths[chosen])),
                          "control": summarize(study.metrics(paths["baseline"]))}, ensure_ascii=False, indent=2))
        return
    selected = json.loads(selected_path.read_text())
    if selected["contract_sha256"] != study.sha(manifest) or selected["rule"] != select_rule(selected["name"]):
        raise RuntimeError("Frozen profit candidate mismatch")
    chosen = selected["name"]
    trades, features = study.load_data(True)
    paths = evaluate(trades, features)
    windows = {}
    for name, rows in paths.items():
        windows[name] = {"full": study.metrics(rows),
                         "development": study.metrics([r for r in rows if r.date <= study.DEVELOPMENT_END]),
                         "later": study.metrics([r for r in rows if r.date > study.DEVELOPMENT_END])}
    baseline = json.loads(study.SOURCES[2].read_text())["summary"]
    for key in ("trades", "wins", "losses", "ending_equity_rub", "closed_trade_mdd_pct"):
        if not math.isclose(windows["baseline"]["full"][key], baseline[key], abs_tol=1e-8):
            raise RuntimeError(f"Baseline drift: {key}")
    stress = {}
    for bps in (0, 5, 10, 20):
        stress_paths = evaluate(trades, features, base.FillModel(.04, bps))
        stress[str(bps)] = {name: study.metrics(stress_paths[name]) for name in ("baseline", chosen)}
    c, b = windows[chosen], windows["baseline"]
    late_rows = [r for r in paths[chosen] if r.date > study.DEVELOPMENT_END]
    late_base = [r for r in paths["baseline"] if r.date > study.DEVELOPMENT_END]
    uncertainty = paired_profit(paths[chosen], paths["baseline"])
    late_uncertainty = paired_profit(late_rows, late_base)
    gates = {
        "full_pnl_improved": c["full"]["pnl_rub"] > b["full"]["pnl_rub"],
        "late_pnl_improved": c["later"]["pnl_rub"] > b["later"]["pnl_rub"],
        "full_mdd_no_worse": c["full"]["closed_trade_mdd_pct"] >= b["full"]["closed_trade_mdd_pct"],
        "late_mdd_no_worse": c["later"]["closed_trade_mdd_pct"] >= b["later"]["closed_trade_mdd_pct"],
        "slippage_stresses_delta_positive": all(v[chosen]["pnl_rub"] > v["baseline"]["pnl_rub"] for v in stress.values()),
        "without_best_contributor_positive": uncertainty["without_best_contributor_delta_rub"] > 0,
        "late_bootstrap_lower_positive": late_uncertainty["paired_month_bootstrap_account_pnl_delta_95_rub"][0] > 0,
    }
    report = {"objective": manifest["objective"], "manifest": manifest, "selected_profile": selected["rule"],
              "selected_name": chosen, "candidate": c, "baseline": b,
              "verdict": "FORWARD_CANDIDATE" if all(gates.values()) else "HISTORICAL_CANDIDATE_NOT_CONFIRMED",
              "gates": gates, "uncertainty": uncertainty, "late_uncertainty": late_uncertainty,
              "all_rules_exploratory": windows, "slippage_stress": stress,
              "candidate_trades": base.account(paths[chosen])["per_trade"],
              "baseline_trades": base.account(paths["baseline"])["per_trade"],
              "frozen_profile_sha256": study.inputs.october.file_sha256(selected_path),
              "code_sha256": {name: study.inputs.october.file_sha256(ROOT / name)
                              for name in ("argonus/research/research_profit_first.py", "argonus/strategies/profit_first_profile.py", "argonus/strategies/win_rate_policy.py")}}
    report["fresh_tail"] = newly_collected_tail(chosen, paths)
    study.write_json(OUTPUT / "report.json", report)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    dates = [np.datetime64(trades[0].date)] + [np.datetime64(r.date) for r in paths["baseline"]]
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.plot(dates, cash_curve(paths["baseline"]), label="Current A + RS5")
    ax.plot(dates, cash_curve(paths[chosen]), label=chosen)
    ax.axvline(np.datetime64("2026-07-17"), color="gray", linestyle="--", label="Later historical check")
    ax.set(ylabel="Account equity, RUB", title="Profit-first study: same risk and execution costs")
    ax.legend(); ax.grid(alpha=.2); fig.tight_layout()
    fig.savefig(OUTPUT / "equity_comparison.png", dpi=150)
    plt.close(fig)
    lines = ["# Исследование с приоритетом прибыли", "",
             f"Выбранный по данным до 16 июля кандидат: **{chosen}**. Вердикт: **{report['verdict']}**.", "",
             "Доля побед не ограничивает отбор. Главное — чистая прибыль; дополнительные проверки — просадка, сохранение количества сделок и результаты более позднего периода. При увеличении стопа номинал уменьшается, плановый риск не растёт.", "",
             "| Правило | Период | N | Доля прибыльных | Доходность | Итоговый счёт, ₽ | Просадка |",
             "|---|---|---:|---:|---:|---:|---:|"]
    for name in ("baseline", chosen):
        for window in ("development", "later", "full"):
            m = windows[name][window]
            lines.append(f"| {name} | {window} | {m['trades']} | {m['win_rate_pct']:.2f}% | {m['return_pct']:+.2f}% | {m['ending_equity_rub']:.2f} | {m['closed_trade_mdd_pct']:.2f}% |")
    lines += ["", "Начальный капитал 50 000 ₽; позиция min(150 000 ₽, 3 × капитал), комиссия 0,04% и проскальзывание 0,05% на сторону, вход 07:05. Отбор заморожен до поздней проверки. Архив уже исследовался, поэтому эта проверка не является новым нетронутым форвардом.", "",
              "![Сравнение капитала](equity_comparison.png)", "", "## Проверки", ""]
    lines += [f"- {key}: {'PASS' if value else 'FAIL'}" for key, value in gates.items()]
    lines += ["", f"95%-интервал исторической прибавки прибыли по bootstrap месяцев: {uncertainty['paired_month_bootstrap_account_pnl_delta_95_rub']} ₽.",
              f"Интервал на позднем отрезке: {late_uncertainty['paired_month_bootstrap_account_pnl_delta_95_rub']} ₽.", "",
              "## Все варианты (исследовательское сравнение)", "",
              "| Правило | N | Доходность всей истории | Просадка | Поздняя доходность |",
              "|---|---:|---:|---:|---:|"]
    for name, w in sorted(windows.items(), key=lambda item: -item[1]["full"]["pnl_rub"]):
        f, late = w["full"], w["later"]
        lines.append(f"| {name} | {f['trades']} | {f['return_pct']:+.2f}% | {f['closed_trade_mdd_pct']:.2f}% | {late['return_pct']:+.2f}% |")
    fresh = report["fresh_tail"]
    if fresh is not None:
        lines += ["", "## Дополнительные данные: 30 сентября–2 октября", "",
                  "Кандидат зафиксирован до загрузки этих данных. Проверены только контроль и выбранный кандидат; остальные правила по этому отрезку не подбирались.", "",
                  "| Правило | Сделок | Прибыль на отдельном старте 50k, ₽ | Итог всей истории по 2 октября, ₽ |",
                  "|---|---:|---:|---:|"]
        for name, m in fresh["standalone"].items():
            full = fresh["continuous_with_archive"][name]
            lines.append(f"| {name} | {m['trades']} | {m['pnl_rub']:.2f} | {full['ending_equity_rub']:.2f} |")
        lines += ["", "Три сессии недостаточны для подтверждения преимущества; снимок состава рынка и торговых флагов — текущий, без исторического point-in-time справочника."]
    lines += ["", "## Реализация кандидата", "",
              "Новый тейк = фактический вход + 1,25 × (исходная T4 − фактический вход). Стоп остаётся 1%, номинал и число сделок не увеличиваются. Этот кандидат увеличивает размер некоторых побед; на данном архиве их число не меняется.", "",
              "`profit_first_profile.py` строит отдельный исследовательский план по фактической цене входа и шагу цены бумаги. Округление сохраняет плановый стоп не шире 1%. Клиента брокера и отправки заявок в модуле нет. Пример: `python3 -m argonus.strategies.profit_first_profile --direction long --entry 100 --t4 103 --tick 0.01` даёт цель 103,75 и стоп 99.", "",
              "Проверки: `python3 -m unittest -q test_profit_first test_win_rate_policy test_research_0705_execution`.", "",
              "## Основания и ограничения", "",
              "Прибыль определяется и размером выигрыша/проигрыша, и частотой: [CME, Mathematical Expectation](https://www.cmegroup.com/education/courses/trading-psychology/the-mathematics-of-trading-success.hideSubnav.educationIframe.html?hideAddThisExt=y&hideFooter=y&hideHeader=y&hideRightRail=y). Перебор повышает риск подгонки: [Bailey et al.](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf). Поэтому опубликованы все 36 альтернатив, кандидат выбирается до просмотра поздней части, а доказанность отделяется от исторической прибыли.", "",
              "Лотность, индивидуальная маржа, финансирование и налоги не моделируются. Свечи не подтверждают спред и исполнение объёма 150 000 ₽. Просадка — по закрытым сделкам. Заявки и боевые настройки этот исследовательский модуль не меняет.", "",
              "Воспроизведение: `python3 -m argonus.research.research_profit_first`. Подробности: `report.json`, замороженный выбор: `selected_candidate.json`."]
    (OUTPUT / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"chosen": chosen, "verdict": report["verdict"],
                      "candidate": {k: summarize(v) for k, v in c.items()},
                      "control": {k: summarize(v) for k, v in b.items()},
                      "gates": gates, "uncertainty": uncertainty, "late_uncertainty": late_uncertainty,
                      "fresh_tail": {"sessions": fresh['sessions'],
                                     "standalone": {k: summarize(v) for k, v in fresh['standalone'].items()},
                                     "continuous": {k: summarize(v) for k, v in fresh['continuous_with_archive'].items()}}
                      if fresh is not None else None}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
