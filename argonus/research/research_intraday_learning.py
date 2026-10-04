#!/usr/bin/env python3
"""Two fixed learners predicting NET intraday return, with temporal selection.

No ticker or date identity features; no reused legacy models. Fit Nov--Jan,
choose learner on Feb--Mar including cash, refit through March, evaluate later.
After the choice is frozen, refit through July 16 for the new MOEX period.
Every target includes commission, 5bps per-side slip and a five-minute delay.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")

import numpy as np
import sklearn
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from argonus.strategies import intraday_signals as signals
from argonus.research import research_intraday_portfolio as portfolio


from argonus.paths import PROJECT_ROOT as ROOT
MODEL_NAMES = ("ridge", "shallow_boosting")
FEATURE_NAMES = (
    "direction", "atr_pct", "signed_open_return", "signed_open_return_atr",
    "signed_relative_open", "signed_prior5", "signed_relative_prior5",
    "signed_market_open", "signed_market_prior5", "range_position_dir",
    "signed_breakout_distance_atr", "log_prior_liquidity", "log_recent_liquidity",
    "recent_prior_turnover_ratio",
)
MIN_PREDICTED_NET_PCT = .15


def vectors(snapshots: list[signals.Snapshot]) -> list[dict]:
    eligible = [s for s in snapshots if s.prior_turnover_lower_bound_rub >= 50_000_000
                and s.recent_turnover_lower_bound_rub >= 5_000_000]
    if not eligible:
        return []
    market_open = statistics.median(s.opening_return_pct for s in eligible)
    market_prior = statistics.median(s.prior_return5_pct for s in eligible)
    rows = []
    for s in eligible:
        width = max(s.opening_range_high - s.opening_range_low, .001 * s.reference_price)
        position = (s.reference_price - s.opening_range_low) / width
        for direction, sign in (("long", 1), ("short", -1)):
            extreme = s.opening_range_high if sign == 1 else s.opening_range_low
            features = [sign, s.atr_pct, sign * s.opening_return_pct,
                        sign * s.opening_return_pct / s.atr_pct,
                        sign * (s.opening_return_pct - market_open), sign * s.prior_return5_pct,
                        sign * (s.prior_return5_pct - market_prior), sign * market_open, sign * market_prior,
                        position if sign == 1 else 1 - position,
                        sign * (s.reference_price / extreme - 1) * 100 / s.atr_pct,
                        math.log1p(s.prior_turnover_lower_bound_rub), math.log1p(s.recent_turnover_lower_bound_rub),
                        s.recent_turnover_lower_bound_rub / s.prior_turnover_lower_bound_rub]
            rows.append({"date": s.date, "symbol": s.symbol, "direction": direction,
                         "features": features, "snapshot": asdict(s)})
    return rows


def signal(row: dict) -> signals.Signal:
    s = row["snapshot"]
    return signals.Signal(row["date"], row["symbol"], row["direction"], "net_return_model", "10:30",
                          s["reference_price"], 0, 1, 2, "18:35", 3,
                          s["recent_turnover_lower_bound_rub"], s["last_completed_bar"])


def dataset(sessions: dict, dailies: dict, first: str, last: str, *, labels: bool) -> list[dict]:
    from datetime import date
    dates = sorted({d for book in sessions.values() for d in book if first <= d <= last
                    and date.fromisoformat(d).weekday() < 5})
    result = []
    for ds in dates:
        snapshots = []
        for symbol in sorted(sessions):
            if ds not in sessions[symbol]:
                continue
            s = signals.snapshot(symbol, ds, dailies[symbol], sessions[symbol][ds], "10:30")
            if s:
                snapshots.append(s)
        rows = vectors(snapshots)
        if labels:
            for row in rows:
                leg = portfolio.replay(signal(row), sessions[row["symbol"]][ds], 5)
                # All signals stay in the ranking sample. Unfilled training
                # orders have zero outcome and are not deleted post-selection.
                row["net_return_pct"] = leg.net_return_pct
                row["fill_status"] = leg.reason
        result.extend(rows)
    return result


def fit_model(name: str, rows: list[dict], cutoff: str):
    train = [r for r in rows if r["date"] <= cutoff]
    if not train or name not in MODEL_NAMES:
        raise ValueError("Unknown model or empty training set")
    counts = Counter(r["date"] for r in train)
    # Equal total influence per day; scale mean weight to 1.
    weights = np.array([len(train) / len(counts) / counts[r["date"]] for r in train])
    x = np.array([r["features"] for r in train])
    y = np.array([r["net_return_pct"] for r in train])
    if name == "ridge":
        model = make_pipeline(StandardScaler(), Ridge(alpha=100.0))
        model.fit(x, y, ridge__sample_weight=weights)
    else:
        model = HistGradientBoostingRegressor(max_iter=100, max_depth=2, max_leaf_nodes=4,
                                              learning_rate=.05, min_samples_leaf=100,
                                              l2_regularization=20, early_stopping=False, random_state=20260910)
        model.fit(x, y, sample_weight=weights)
    return model


def pick_rows(model, rows: list[dict]) -> dict[str, list[dict]]:
    """Labels are never used by this function, including to reject a candidate."""
    if not rows:
        return {}
    predictions = model.predict(np.array([r["features"] for r in rows]))
    per_day = defaultdict(list)
    for row, value in zip(rows, predictions, strict=True):
        per_day.setdefault(row["date"], [])
        if math.isfinite(float(value)) and value >= MIN_PREDICTED_NET_PCT:
            per_day[row["date"]].append({**row, "predicted_net_pct": float(value)})
    selected = {}
    for ds, candidates in per_day.items():
        ranked = sorted(candidates, key=lambda r: (-r["predicted_net_pct"], r["symbol"], r["direction"]))
        chosen, seen = [], set()
        for row in ranked:
            if row["symbol"] in seen:
                continue
            chosen.append(row)
            seen.add(row["symbol"])
            if len(chosen) == 3:
                break
        selected[ds] = chosen
    return selected


def evaluate(model, rows: list[dict], sessions: dict, bps: float = 5.0, long_only=False) -> dict:
    picks = pick_rows(model, rows)
    paths = {ds: [portfolio.replay(signal(r), sessions[r["symbol"]][ds], bps)
                  for r in selected if not long_only or r["direction"] == "long"] for ds, selected in picks.items()}
    return portfolio.account(paths)


def fixed_rules_on_new_period(sessions: dict, dailies: dict, first: str, last: str) -> dict:
    """Evaluate the original, unchanged rule family; no new parameter search."""
    from datetime import date
    dates = sorted({d for book in sessions.values() for d in book if first <= d <= last
                    and date.fromisoformat(d).weekday() < 5})
    snapshots = {}
    for tm in {e.decision_time for e in signals.ENGINES}:
        for ds in dates:
            snapshots[(ds, tm)] = [s for symbol in sorted(sessions) if ds in sessions[symbol]
                                   if (s := signals.snapshot(symbol, ds, dailies[symbol], sessions[symbol][ds], tm))]
    result = {}
    for engine in signals.ENGINES:
        selected = {ds: signals.select_signals(snapshots[(ds, engine.decision_time)], engine) for ds in dates}
        paths = {ds: [portfolio.replay(s, sessions[s.symbol][ds], 5) for s in picks] for ds, picks in selected.items()}
        stress = {ds: [portfolio.replay(s, sessions[s.symbol][ds], 20) for s in picks] for ds, picks in selected.items()}
        result[engine.name] = {"base": portfolio.account(paths), "stress20": portfolio.compact(portfolio.account(stress))}
    return result


def build_report(output_dir: Path, new_data: Path | None) -> dict:
    sessions, dailies, hashes = portfolio.load_inputs()
    rows = dataset(sessions, dailies, portfolio.START, portfolio.END, labels=True)
    validation_rows = [r for r in rows if "2026-02-01" <= r["date"] <= portfolio.FIT_END]
    validations = {}
    for name in MODEL_NAMES:
        fitted = fit_model(name, rows, "2026-01-31")
        validations[name] = evaluate(fitted, validation_rows, sessions)
    chosen = "cash"
    best_score = 0.0
    for name in MODEL_NAMES:
        utility = portfolio.utility(validations[name])
        if utility > best_score:
            chosen, best_score = name, utility
    later = {}
    for name in MODEL_NAMES:
        fitted = fit_model(name, rows, portfolio.FIT_END)
        later[name] = evaluate(fitted, [r for r in rows if r["date"] > portfolio.FIT_END], sessions)
    report = {"mode": "offline_research", "production_activation_allowed": False,
              "software": {"python": sys.version.split()[0], "numpy": np.__version__, "scikit_learn": sklearn.__version__},
              "feature_names": FEATURE_NAMES, "models": MODEL_NAMES, "training_rows": len(rows),
              "minimum_predicted_net_pct": MIN_PREDICTED_NET_PCT,
              "selection": {"fit_end": "2026-01-31", "validation_end": portfolio.FIT_END, "chosen": chosen},
              "validation": validations, "later_april_july": later,
              "input_sha256": hashes,
              "code_sha256": {name: portfolio.hash_file(ROOT / name) for name in
                              ("argonus/strategies/intraday_signals.py", "argonus/research/research_intraday_portfolio.py", "argonus/research/research_intraday_learning.py")},
              "new_period": None}
    # Persist the choice before opening new-period prices. It cannot be changed
    # to the later winner by this command.
    from argonus.market_data.fetch_moex_research import write_json
    write_json(output_dir / "selection_before_new_period.json", {"selection": report["selection"],
                "code_sha256": report["code_sha256"], "feature_names": FEATURE_NAMES,
                "validation": {k: portfolio.compact(v) for k, v in validations.items()}})
    if new_data:
        manifest = json.loads((new_data / "manifest.json").read_text())
        if manifest["status"] != "complete":
            raise ValueError("New-period dataset is incomplete")
        new_sessions, new_daily, overlap_checks = {}, {}, {}
        for entry in manifest["symbols"]:
            symbol = entry["symbol"]
            fm, dj = new_data / "five_min" / f"{symbol}.json.gz", new_data / "dailies" / f"{symbol}.json"
            if portfolio.hash_file(fm) != entry["five_min_sha256"] or portfolio.hash_file(dj) != entry["daily_sha256"]:
                raise ValueError("New-period input hash mismatch")
            with gzip.open(fm, "rt") as handle:
                new_sessions[symbol] = json.load(handle)
            new_daily[symbol] = json.loads(dj.read_text())
            old_close = {r[0]: r[4] for r in dailies[symbol]}.get(portfolio.END)
            new_close = {r[0]: r[4] for r in new_daily[symbol]}.get(portfolio.END)
            overlap_checks[symbol] = {"date": portfolio.END, "archive_close": old_close, "moex_close": new_close,
                                      "matches": old_close is not None and new_close is not None
                                      and math.isclose(old_close, new_close, rel_tol=1e-8)}
        new_rows = dataset(new_sessions, new_daily, manifest["from"], manifest["till"], labels=False)
        evaluated = {}
        for name in MODEL_NAMES:
            # Model refit is scheduled using old data only, before evaluation.
            fitted = fit_model(name, rows, portfolio.END)
            evaluated[name] = {"base": evaluate(fitted, new_rows, new_sessions),
                               "stress20": portfolio.compact(evaluate(fitted, new_rows, new_sessions, 20)),
                               "long_only": portfolio.compact(evaluate(fitted, new_rows, new_sessions, 5, True))}
        report["new_period"] = {"manifest_sha256": portfolio.hash_file(new_data / "manifest.json"),
                                "from": manifest["from"], "till": manifest["till"], "models": evaluated,
                                "overlap_close_checks": overlap_checks,
                                "minute_candles": sum(item["minute_rows"] for item in manifest["symbols"]),
                                "universe_symbols": manifest["selected"],
                                "unchanged_rule_family": fixed_rules_on_new_period(new_sessions, new_daily, manifest["from"], manifest["till"]),
                                "selected_before_evaluation": chosen,
                                "note": "New period also changes to a pre-July top-20 universe and MOEX actual turnover; a transportability check, not identical data distribution."}
    report["verdict"] = "CASH_SELECTED" if chosen == "cash" else "REVIEW_NEW_PERIOD"
    if chosen != "cash" and report["new_period"]:
        selected = report["new_period"]["models"][chosen]
        report["verdict"] = "NO_ROBUST_IMPROVEMENT" if (selected["base"]["pnl_rub"] <= 0
                                                       or selected["stress20"]["pnl_rub"] <= 0) else "FORWARD_EXECUTION_REVIEW_REQUIRED"
    return report


def markdown(report: dict) -> str:
    lines = ["# Модель ожидаемой чистой доходности — 2026-09-10", "",
             f"Вердикт: **{report['verdict']}**. Выбор по февралe–марту: `{report['selection']['chosen']}`.", "",
             "Два заранее заданных варианта: Ridge с регуляризацией и неглубокий градиентный бустинг. "
             "Модель предсказывает доходность сделки после комиссии и проскальзывания, а не факт касания цели. "
             "Вход 10:35 по сигналу 10:30, стоп 1%, цель 2%, до трёх различных акций. "
             "Порог прогноза +0,15% net; неиспользованная доля капитала остаётся свободной.", "",
             "Начальный счёт 50 000 ₽, общий номинал до 150 000 ₽. Комиссия 0,04% и "
             "проскальзывание 0,05% на каждую сторону. Ни тикер, ни календарная дата не входят в признаки. "
             "Обучение — ноябрь–январь; выбор — февраль–март; затем переобучение по март и проверка апреля–июля.", "",
             "| Модель | Фев–мар: сделки | Фев–мар: доходность | Апр–июл: сделки | Апр–июл: доходность |",
             "|---|---:|---:|---:|---:|"]
    for name in MODEL_NAMES:
        v, t = report["validation"][name], report["later_april_july"][name]
        lines.append(f"| {name} | {v['trades']} | {v['return_pct']:+.2f}% | {t['trades']} | {t['return_pct']:+.2f}% |")
    if report["new_period"]:
        n = report["new_period"]
        lines += ["", f"## Новый период {n['from']} — {n['till']}", "",
                  "Выбор модели записан до открытия новых данных. Модели переобучены только на "
                  "архиве по 16 июля. Здесь используется фиксированная двадцатка по прошлому обороту "
                  "и свечи Московской биржи; смена источника и состава акций — дополнительное ограничение сравнения.", "",
                  "| Модель | Сделки | Доходность | PnL, ₽ | MDD по дням | 20 б.п. на сторону |",
                  "|---|---:|---:|---:|---:|---:|"]
        for name, stats in n["models"].items():
            r = stats["base"]
            lines.append(f"| {name} | {r['trades']} | {r['return_pct']:+.2f}% | {r['pnl_rub']:+,.0f} | "
                         f"{r['daily_close_mdd_pct']:.2f}% | {stats['stress20']['return_pct']:+.2f}% |")
        lines += ["", "Те же восемь простых правил на новом периоде (диагностика без повторного выбора):", "",
                  "| Правило | Сделки | Доходность | PnL, ₽ | При 20 б.п. на сторону |",
                  "|---|---:|---:|---:|---:|"]
        for name, stats in n["unchanged_rule_family"].items():
            r = stats["base"]
            lines.append(f"| {name} | {r['trades']} | {r['return_pct']:+.2f}% | {r['pnl_rub']:+,.0f} | {stats['stress20']['return_pct']:+.2f}% |")
    lines += ["", "Это исследовательские результаты без гарантии реального исполнения. Историческая "
              "доступность шорта и индивидуальная маржа неизвестны; объёмы не подтверждают стакан. "
              "Свежие цены загружены с публичного [MOEX ISS](https://www.moex.com/a2920). "
              "Реальные заявки и параметры боевого бота не изменены.", "", "```bash",
              "python3 -m argonus.research.research_intraday_learning --output-dir data/backtests/intraday_learning_2026-09-10 --new-data data/backtests/moex_new_period_2026-09-10",
              "```", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--new-data", type=Path)
    args = parser.parse_args()
    report = build_report(args.output_dir, args.new_data)
    from argonus.market_data.fetch_moex_research import write_json
    write_json(args.output_dir / "report.json", report)
    text = markdown(report)
    (args.output_dir / "REPORT.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
