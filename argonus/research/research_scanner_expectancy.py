"""Learn expected net opportunity returns for a continuous shared portfolio.

Round two explicitly follows the unsuccessful fixed-setup study. Fixed model
choice is made before April; prior research reuse remains disclosed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
from collections import Counter
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from argonus.strategies import continuous_intraday as core
from argonus.strategies import intraday_signals as signals
from argonus.research import research_intraday_portfolio as execution
from argonus.research import research_scanner_v2 as study

OUTPUT = study.OUTPUT / "net_expectancy"
NAMES = ("current_target125", "ridge", "boosting", "target125_then_ridge", "target125_then_boosting", "cash")
MIN_NET = .15


def vector(item, market, contexts):
    sign = 1 if item.direction == "long" else -1
    context = contexts[item.symbol]
    return [sign, *(int(item.setup == name) for name in ("rolling_breakout", "rolling_pullback", "rolling_reversion")),
            item.score, item.stop_pct, math.log1p(item.recent_turnover), math.log1p(context["liquidity"]),
            market.get("efficiency", 0), sign * market.get("market_return_pct", 0),
            signals.minutes(item.decision_time) / 60, statistics.pstdev(context["returns"].values()) * 100]


def label(item, day):
    entry_clock = core.clock(signals.minutes(item.decision_time) + 5)
    bar = day["rows"].get(entry_clock, {}).get(("scanner", item.symbol))
    if not bar:
        return 0.0
    sign = 1 if item.direction == "long" else -1
    actual = bar[1] * (1 + sign * .0005)
    if abs(actual / item.reference - 1) * 100 > .25 * item.stop_pct:
        return 0.0
    candles = [rows[("scanner", item.symbol)] for tm, rows in sorted(day["rows"].items())
               if tm >= entry_clock and ("scanner", item.symbol) in rows]
    candidate = signals.Signal(item.date, item.symbol, item.direction, "continuous_net_learner",
                               item.decision_time, item.reference, item.score, item.stop_pct,
                               item.target_pct, "18:35", 3, item.recent_turnover,
                               core.clock(signals.minutes(item.decision_time) - 5))
    return execution.replay(candidate, candles, 5).net_return_pct


def dataset(prepared, last):
    rows = []
    for ds, day in prepared.items():
        if ds > last:
            continue
        for items, market in day["timeline"].values():
            for item in items:
                rows.append({"date": ds, "features": vector(item, market, day["contexts"]),
                             "net_return_pct": label(item, day)})
    return rows


def fit(name, rows, cutoff):
    train = [row for row in rows if row["date"] <= cutoff]
    if not train or name not in ("ridge", "boosting"):
        raise ValueError("Unknown model or empty past training sample")
    counts = Counter(row["date"] for row in train)
    weights = np.array([len(train) / len(counts) / counts[row["date"]] for row in train])
    x, y = np.array([r["features"] for r in train]), np.array([r["net_return_pct"] for r in train])
    if name == "ridge":
        model = make_pipeline(StandardScaler(), Ridge(alpha=1000))
        model.fit(x, y, ridge__sample_weight=weights)
    else:
        model = HistGradientBoostingRegressor(max_iter=100, max_depth=2, max_leaf_nodes=4,
                                              learning_rate=.05, min_samples_leaf=200,
                                              l2_regularization=25, random_state=20261003,
                                              early_stopping=False)
        model.fit(x, y, sample_weight=weights)
    return model


def gate(model):
    def choose(items, market, contexts):
        if not items:
            return []
        predictions = model.predict(np.array([vector(item, market, contexts) for item in items]))
        selected = [replace(item, score=float(value)) for item, value in zip(items, predictions)
                    if math.isfinite(value) and value >= MIN_NET]
        return sorted(selected, key=lambda item: (-item.score, item.symbol, item.setup))
    return choose


def evaluate(prepared, name, models, first=None, last=None, bps=5):
    if name in ("cash", "current_target125"):
        return study.replay(prepared, name, first=first, last=last, bps=bps)
    model_name = "ridge" if name.endswith("ridge") else "boosting"
    underlying = "target125_then_router" if name.startswith("target125_then_") else "regime_router"
    return study.replay(prepared, underlying, first=first, last=last, bps=bps,
                        opportunity_filter=gate(models[model_name]))


def pick(values):
    chosen = "current_target125" if values["current_target125"]["pnl_rub"] > 0 else "cash"
    best = values[chosen]["pnl_rub"]
    for name in NAMES:
        value = values[name]
        if value["daily_close_mdd_pct"] >= -15 and value["pnl_rub"] > best + 1e-8:
            chosen, best = name, value["pnl_rub"]
    return chosen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection-only", action="store_true")
    args = parser.parse_args()
    manifest_path = OUTPUT / "study_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if list(NAMES) != manifest["candidates"]:
        raise ValueError("Registered model family differs")
    prepared, audit = study.load_data(include_later=not args.selection_only)
    rows = dataset(prepared, "2026-03-31")
    print("Labeled past opportunities:", len(rows), "from", len({r['date'] for r in rows}), "days", flush=True)
    selected_path = OUTPUT / "selected_architecture.json"
    if args.selection_only:
        if selected_path.exists():
            raise ValueError("Net architecture already frozen")
        models = {name: fit(name, rows, "2026-01-31") for name in ("ridge", "boosting")}
        values = {name: study.compact(evaluate(prepared, name, models, first="2026-02-01", last="2026-03-31")) for name in NAMES}
        chosen = pick(values)
        study.write(selected_path, {"name": chosen, "selection_cutoff": "2026-03-31", "results": values,
                                    "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                                    "status": "research_only"})
        print(json.dumps({"chosen": chosen, "selection_results": values}, ensure_ascii=False, indent=2))
        return
    chosen = json.loads(selected_path.read_text())["name"]
    if json.loads(selected_path.read_text())["manifest_sha256"] != hashlib.sha256(manifest_path.read_bytes()).hexdigest():
        raise ValueError("Frozen model contract changed")
    models = {name: fit(name, rows, "2026-03-31") for name in ("ridge", "boosting")}
    windows = {"test": ("2026-04-01", None), "validation": ("2026-04-01", "2026-07-16"), "later": ("2026-07-17", None)}
    results = {}
    for name in NAMES:
        results[name] = {}
        for window, (first, last) in windows.items():
            result = evaluate(prepared, name, models, first, last)
            results[name][window] = study.compact(result)
            if window == "test":
                study.write(OUTPUT / f"ledger_{name}.json", result)
        print("Evaluated expected return:", name, flush=True)
    control, candidate = results["current_target125"], results[chosen]
    stress = {str(bps): {name: study.compact(evaluate(prepared, name, models, "2026-04-01", bps=bps))
                        for name in {chosen, "current_target125"}} for bps in (10, 20)}
    gates = {"test_profit_improved": candidate["test"]["pnl_rub"] > control["test"]["pnl_rub"],
             "validation_profit_improved": candidate["validation"]["pnl_rub"] > control["validation"]["pnl_rub"],
             "later_profit_improved": candidate["later"]["pnl_rub"] > control["later"]["pnl_rub"],
             "drawdown_no_worse": candidate["test"]["daily_close_mdd_pct"] >= control["test"]["daily_close_mdd_pct"],
             "still_improved_at_10bps": stress["10"][chosen]["pnl_rub"] > stress["10"]["current_target125"]["pnl_rub"],
             "new_architecture_chosen": chosen not in ("current_target125", "cash"),
             "complete_held_bar_data": not candidate["test"]["counters"].get("missing_bar_while_open", 0)}
    report = {"selected": chosen, "verdict": "RESEARCH_CANDIDATE" if all(gates.values()) else "NO_CONFIRMED_PROFIT_IMPROVEMENT",
              "gates": gates, "results": results, "stress": stress, "data_audit": audit,
              "training_opportunities": len(rows), "training_days": len({r['date'] for r in rows}),
              "production_unchanged": True, "orders_allowed": False,
              "limitations": ["Same reused archive and membership limitations as the fixed-scanner study.",
                              "Rows are overlapping opportunities, not independent trades.",
                              "Rankings contain no future labels, ticker identities or future fill prices.",
                              "Refit is only through March31; the later data cannot select or refit the model.",
                              "Both market APIs are currently unavailable, so new Sep10--Oct2 data were not obtained."]}
    study.write(OUTPUT / "report.json", report)
    lines = ["# Постоянный сканер с оценкой ожидаемой чистой прибыли", "",
             f"Выбран по февралю–марту: **{chosen}**. Вердикт: **{report['verdict']}**.", "",
             "Этот отдельный этап зарегистрирован после отрицательного результата простых непрерывных правил. Исходная таблица не переписана. Два заранее заданных алгоритма оценивают доходность возможности после комиссий, проскальзывания, задержки входа и отказа от погони за ценой. Вход разрешён только при прогнозе не менее +0,15% чистой доходности; прогноз не является обещанием.", "",
             f"Для окончательной оценки модели обучены по {len(rows):,} возможностям из {len({r['date'] for r in rows})} прошлых дней до 31 марта. Эти возможности пересекаются во времени; размер таблицы не выдаётся за число независимых наблюдений.", "",
             "Тест апреля–9 сентября начинается с 50 000 ₽, той же общей ёмкости 150 000 ₽ и портфельных ограничений нового движка. Каждое отдельное окно ниже также начинается с 50 000 ₽.", "",
             "| Архитектура | Сделок апр–сен | Итог теста, ₽ | Просадка по дням | PnL апр–16 июл, ₽ | PnL 17 июл–9 сен, ₽ |", "|---|---:|---:|---:|---:|---:|"]
    for name, result in results.items():
        value = result["test"]
        lines.append(f"| {name} | {value['trades']} | {value['ending_equity_rub']:,.2f} | {value['daily_close_mdd_pct']:.2f}% | {result['validation']['pnl_rub']:,.2f} | {result['later']['pnl_rub']:,.2f} |")
    lines += ["", "## Проверки выбранного варианта", "", *[f"- {key}: {'PASS' if value else 'FAIL'}" for key, value in gates.items()], "",
              "## Ограничения", "", *[f"- {text}" for text in report["limitations"]], "",
              "Боевой target125 не изменён. Новые алгоритмы не имеют доступа к отправке заявок. [Исходный непрерывный движок и проблемы данных](../REPORT.md).", "",
              "Воспроизведение: `python3 -m argonus.research.research_scanner_expectancy --selection-only` для нового отбора; затем `python3 -m argonus.research.research_scanner_expectancy`."]
    (OUTPUT / "REPORT.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({"selected": chosen, "verdict": report["verdict"], "gates": gates}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
