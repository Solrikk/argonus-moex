#!/usr/bin/env python3
"""Offline, execution-aware 07:05 exit research; never connects to a broker.

The archived selector already used these dates during development. Even the
chronological checks below are retrospective, not untouched forward evidence.
All eight challengers are declared below before evaluation; every result is
reported. Wider stops reduce exposure to preserve the baseline planned loss.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from argonus.research import research_entry_timing as legacy
from argonus.research import research_flat_month_exit_risk as data


@dataclass(frozen=True)
class Policy:
    name: str
    stop_pct: float = 1.0
    target_multiple: float = 1.0
    time_exit: str = "18:35"
    breakeven_trigger_pct: float | None = None
    min_reward_risk: float = 0.0


# A small, fixed family, including control. No fitting of continuous thresholds.
POLICIES = (
    Policy("baseline"),
    Policy("target125", target_multiple=1.25),
    Policy("exit1700", time_exit="17:00"),
    Policy("target125_exit1700", target_multiple=1.25, time_exit="17:00"),
    Policy("breakeven_after_1pct", breakeven_trigger_pct=1.0),
    Policy("min_rr2", min_reward_risk=2.0),
    Policy("stop075", stop_pct=0.75),
    Policy("stop125_target125_risk_capped", stop_pct=1.25, target_multiple=1.25),
    Policy("stop125_target125_exit1700_risk_capped", stop_pct=1.25,
           target_multiple=1.25, time_exit="17:00"),
)


@dataclass(frozen=True)
class FillModel:
    fee_side_pct: float = 0.04
    slippage_side_bps: float = 5.0

    def __post_init__(self):
        if any(not math.isfinite(x) or x < 0 for x in
               (self.fee_side_pct, self.slippage_side_bps)):
            raise ValueError("Fees and slippage must be finite and nonnegative")
        if self.slippage_side_bps >= 10_000:
            raise ValueError("Slippage must be below 10000 bps")


@dataclass(frozen=True)
class Execution:
    date: str
    month: str
    symbol: str
    direction: str
    policy: str
    traded: bool
    entry_price: float
    exit_price: float | None
    exit_time: str | None
    reason: str
    gross_return_pct: float
    net_return_pct: float
    exposure_fraction: float
    ambiguous: bool = False


def _minutes(hhmm: str) -> int:
    try:
        h, m = hhmm.split(":")
        if len(h) != 2 or len(m) != 2 or not (0 <= int(h) < 24 and 0 <= int(m) < 60):
            raise ValueError
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Invalid time: {hhmm!r}") from exc


def _validate(trade: data.Trade, policy: Policy) -> None:
    if trade.direction not in ("long", "short"):
        raise ValueError("Unsupported direction")
    values = (policy.stop_pct, policy.target_multiple, trade.target_price)
    if any(not math.isfinite(x) or x <= 0 for x in values) or policy.stop_pct >= 100:
        raise ValueError("Stop, target and target multiplier must be finite and positive")
    if not math.isfinite(policy.min_reward_risk) or policy.min_reward_risk < 0:
        raise ValueError("Minimum reward/risk must be finite and nonnegative")
    if policy.breakeven_trigger_pct is not None and (
        not math.isfinite(policy.breakeven_trigger_pct) or policy.breakeven_trigger_pct <= 0
    ):
        raise ValueError("Breakeven trigger must be finite and positive")
    if not _minutes("07:05") < _minutes(policy.time_exit) <= _minutes("18:35"):
        raise ValueError("Exit must be after 07:05 and no later than 18:35")
    if _minutes(policy.time_exit) % 5:
        raise ValueError("Exit must align with a five-minute boundary")
    times = [_minutes(c.time) for c in trade.candles]
    if times != sorted(set(times)):
        raise ValueError("Candles must have unique increasing timestamps")
    for c in trade.candles:
        if (any(not math.isfinite(x) or x <= 0 for x in (c.open, c.high, c.low, c.close))
                or c.low > min(c.open, c.close) or c.high < max(c.open, c.close)
                or c.low > c.high):
            raise ValueError(f"Invalid OHLC: {trade.date} {c.time}")


def simulate(trade: data.Trade, policy: Policy = POLICIES[0],
             fills: FillModel = FillModel()) -> Execution:
    """Enter at 07:05; exit at the first open >= deadline, ignoring its HLC.

    A stop-market gap fills at the worse of the open and stop plus adverse
    slippage. Target fills use the trigger price plus adverse slippage: the
    production take-profit is a broker stop order, not a resting limit.
    Close-derived breakeven becomes active only on the following candle.
    Missing entry/exit data fail explicitly, never become profitable skips.
    """
    _validate(trade, policy)
    window = [c for c in trade.candles if c.time >= "07:05"]
    if not window or window[0].time != "07:05":
        raise ValueError(f"Missing exact 07:05 candle: {trade.date} {trade.symbol}")
    sign = 1.0 if trade.direction == "long" else -1.0
    slip = fills.slippage_side_bps / 10_000
    entry = window[0].open * (1 + sign * slip)
    distance = sign * (trade.target_price - entry)
    exposure = min(1.0, 1.0 / policy.stop_pct)

    def result(price, time, reason, ambiguous=False):
        traded = price is not None
        gross = sign * (price / entry - 1) * 100 if traded else 0.0
        # Charge both actual traded notionals, also for a losing short.
        fee = fills.fee_side_pct * (1 + price / entry) if traded else 0.0
        return Execution(trade.date, trade.month, trade.symbol, trade.direction,
                         policy.name, traded, entry, price, time, reason,
                         gross, gross - fee, exposure, ambiguous)

    if distance <= 0:
        return result(None, None, "skip_target_behind_entry")
    target = entry + sign * distance * policy.target_multiple
    if target <= 0:
        raise ValueError("Scaled target must be positive")
    if distance / entry * 100 * policy.target_multiple / policy.stop_pct < policy.min_reward_risk:
        return result(None, None, "skip_reward_risk")
    stop = entry * (1 - sign * policy.stop_pct / 100)
    for c in window:
        # At a gap, the opening print precedes the unknown intrabar path.
        if sign * (c.open - stop) <= 0:
            return result(c.open * (1 - sign * slip), c.time, "stop_gap")
        if sign * (c.open - target) >= 0:
            return result(target * (1 - sign * slip), c.time, "target_at_open")
        if c.time >= policy.time_exit:
            return result(c.open * (1 - sign * slip), c.time, "time_exit_open")
        stop_hit = c.low <= stop if sign > 0 else c.high >= stop
        target_hit = c.high >= target if sign > 0 else c.low <= target
        if stop_hit:
            return result(stop * (1 - sign * slip), c.time,
                          "both_stop_first" if target_hit else "stop", target_hit)
        if target_hit:
            return result(target * (1 - sign * slip), c.time, "target")
        if (policy.breakeven_trigger_pct is not None
                and sign * (c.close / entry - 1) * 100 >= policy.breakeven_trigger_pct):
            stop = max(stop, entry) if sign > 0 else min(stop, entry)
    raise ValueError(f"Missing exit candle >= {policy.time_exit}: {trade.date} {trade.symbol}")


def account(rows: Sequence[Execution], start_equity: float = 50_000.0,
            position_cap: float = 150_000.0, max_leverage: float = 3.0) -> dict:
    if any(not math.isfinite(x) or x <= 0 for x in (start_equity, position_cap, max_leverage)):
        raise ValueError("Account limits must be finite and positive")
    dates = [row.date for row in rows]
    if dates != sorted(set(dates)):
        raise ValueError("Account requires one signal per date in chronological order")
    if any(not math.isfinite(r.net_return_pct) or not 0 < r.exposure_fraction <= 1 for r in rows):
        raise ValueError("Returns must be finite; exposure must be within (0, 1]")
    equity = peak = start_equity
    mdd = 0.0
    monthly = {}
    ledger = []
    for row in rows:
        month = monthly.setdefault(row.month, {"equity_start": equity, "pnl_rub": 0.0,
                                               "trades": 0, "wins": 0, "losses": 0})
        position = min(position_cap, max_leverage * equity) * row.exposure_fraction if row.traded else 0.0
        pnl = position * row.net_return_pct / 100
        before = equity
        equity += pnl
        peak = max(peak, equity)
        mdd = min(mdd, (equity / peak - 1) * 100)
        month["pnl_rub"] += pnl
        month["trades"] += int(row.traded)
        month["wins"] += int(row.traded and pnl > 0)
        month["losses"] += int(row.traded and pnl <= 0)
        month["equity_end"] = equity
        month["return_pct"] = (equity / month["equity_start"] - 1) * 100
        ledger.append({**asdict(row), "position_rub": position, "equity_before_rub": before,
                       "pnl_rub": pnl, "equity_after_rub": equity})
        if equity <= 0:
            raise ValueError(f"Account depleted on {row.date}")
    return {"trades": sum(r.traded for r in rows), "skips": sum(not r.traded for r in rows),
            "wins": sum(m["wins"] for m in monthly.values()),
            "losses": sum(m["losses"] for m in monthly.values()),
            "return_pct": (equity / start_equity - 1) * 100,
            "pnl_rub": equity - start_equity, "ending_equity_rub": equity,
            "closed_trade_mdd_pct": mdd, "monthly": monthly, "per_trade": ledger}


def summary(report: dict) -> dict:
    return {k: v for k, v in report.items() if k != "per_trade"}


def choose_policy(history: dict[str, list[Execution]]) -> str:
    """Train only: maximize PnL among policies whose observed DD is no worse."""
    baseline = account(history["baseline"])
    selected = "baseline"
    best = baseline["pnl_rub"]
    for policy in POLICIES[1:]:
        stats = account(history[policy.name])
        if (stats["closed_trade_mdd_pct"] >= baseline["closed_trade_mdd_pct"] - 1e-9
                and stats["pnl_rub"] > best + 1e-9):
            selected, best = policy.name, stats["pnl_rub"]
    return selected


def walk_forward(paths: dict[str, list[Execution]]) -> dict:
    months = sorted({r.month for r in paths["baseline"]})
    stitched, control, folds = [], [], []
    for month in months[3:]:
        selected = choose_policy({k: [r for r in rows if r.month < month]
                                  for k, rows in paths.items()})
        test = [r for r in paths[selected] if r.month == month]
        ref = [r for r in paths["baseline"] if r.month == month]
        stitched.extend(test)
        control.extend(ref)
        folds.append({"test_month": month, "training_end_month": months[months.index(month) - 1],
                      "policy": selected, "test_pnl_delta_rub": account(test)["pnl_rub"] - account(ref)["pnl_rub"]})
    selected_result, baseline_result = account(stitched), account(control)
    return {"folds": folds, "selector": summary(selected_result), "baseline": summary(baseline_result),
            "pnl_delta_rub": selected_result["pnl_rub"] - baseline_result["pnl_rub"]}


def paired_checks(candidate: Sequence[Execution], control: Sequence[Execution]) -> dict:
    if not candidate:
        raise ValueError("Paired checks require a nonempty sample")
    if [(r.date, r.symbol) for r in candidate] != [(r.date, r.symbol) for r in control]:
        raise ValueError("Paired paths have different trade identities")
    a, b = account(candidate), account(control)
    deltas = [x["pnl_rub"] - y["pnl_rub"] for x, y in zip(a["per_trade"], b["per_trade"], strict=True)]
    positive = sum(max(d, 0) for d in deltas)
    best = max(range(len(deltas)), key=deltas.__getitem__) if deltas else None
    months = sorted(b["monthly"])
    monthly_delta = [a["monthly"][m]["pnl_rub"] - b["monthly"][m]["pnl_rub"] for m in months]
    # Exact paired month-block sign randomization; no independence claim for trades.
    observed = sum(monthly_delta)
    p = sum(sum(s * d for s, d in zip(signs, monthly_delta)) >= observed - 1e-9
            for signs in itertools.product((-1, 1), repeat=len(months))) / 2 ** len(months)
    rng = random.Random(20260910)
    bootstrap = sorted(sum(rng.choice(monthly_delta) for _ in months) for _ in range(4000))
    halves = [months[:len(months) // 2], months[len(months) // 2:]]
    half_deltas = [account([r for r in candidate if r.month in half])["pnl_rub"]
                   - account([r for r in control if r.month in half])["pnl_rub"] for half in halves]
    return {"pnl_delta_rub": observed, "chronological_half_delta_rub": half_deltas,
            "positive_delta_months": sum(d > 1e-9 for d in monthly_delta),
            "month_block_sign_flip_p": p,
            "family_adjusted_p": min(1.0, p * (len(POLICIES) - 1)),
            "monthly_pnl_block_bootstrap_95pct_rub": [data.percentile(bootstrap, .025), data.percentile(bootstrap, .975)],
            "largest_positive_contributor_share_pct": max(deltas, default=0) / positive * 100 if positive else 0,
            "pnl_delta_without_best_contributor_rub": (
                account([r for i, r in enumerate(candidate) if i != best])["pnl_rub"]
                - account([r for i, r in enumerate(control) if i != best])["pnl_rub"])}


def build_report(fills: FillModel = FillModel()) -> dict:
    trades54, trades65, provenance = data.load_books()
    published = legacy.fixed_account(trades65, "07:05")
    if not math.isclose(published["return_pct"], 161.260523339778, abs_tol=1e-8):
        raise RuntimeError("Published Extended65 control no longer reproduces")
    report = {"schema_version": 1, "mode": "offline_research", "production_activation_allowed": False,
              "policies": [asdict(p) for p in POLICIES], "fills": asdict(fills), "provenance": provenance,
              "published_control": summary(published), "books": {},
              "limitations": [
                  "All dates and selectors were previously researched; chronological checks are retrospective.",
                  "54 and 65 books overlap; the 54 book is a sensitivity check, not independent evidence.",
                  "5m OHLC cannot establish order-book depth, fill probability, spread or intrabar event times.",
                  "Slippage is an assumption applied adversely per side, not a calibrated broker-fill model.",
                  "MDD uses closed trades only; intraday account drawdown can be larger.",
                  "Lot rounding, instrument margin limits, taxes and broker-specific financing fees are omitted.",
                  "Wider stops reduce notional; planned stop loss is capped, gap losses are not.",
                  "Bootstrap resamples monthly PnL contributions; it does not retrain the selector or prove significance.",
                  "Adjusted p covers the eight challengers here, not all earlier project experiments.",
              ]}
    for name, trades in (("Extended65", trades65), ("Frozen54", trades54)):
        paths = {p.name: [simulate(t, p, fills) for t in trades] for p in POLICIES}
        results = {p.name: account(paths[p.name]) for p in POLICIES}
        checks = {p.name: paired_checks(paths[p.name], paths["baseline"]) for p in POLICIES[1:]}
        stress = {}
        for bps in sorted({0.0, 5.0, 10.0, 20.0, fills.slippage_side_bps}):
            stress[str(bps)] = {p.name: summary(account([simulate(t, p, FillModel(fills.fee_side_pct, bps))
                                                       for t in trades])) for p in POLICIES}
        report["books"][name] = {"results": results, "paired_checks": checks,
                                 "walk_forward": walk_forward(paths), "slippage_stress": stress}
    # No automatic live promotion: these data have no untouched forward window.
    candidates = []
    for p in POLICIES[1:]:
        passes = []
        for book in report["books"].values():
            c = book["paired_checks"][p.name]
            a, b = book["results"][p.name], book["results"]["baseline"]
            passes.append(c["pnl_delta_rub"] > 0 and min(c["chronological_half_delta_rub"]) > 0
                          and a["closed_trade_mdd_pct"] >= b["closed_trade_mdd_pct"] - 1e-9
                          and c["pnl_delta_without_best_contributor_rub"] > 0
                          and c["family_adjusted_p"] <= .1
                          and c["largest_positive_contributor_share_pct"] <= 35
                          and all(v[p.name]["pnl_rub"] > v["baseline"]["pnl_rub"] for v in book["slippage_stress"].values()))
        if all(passes):
            candidates.append(p.name)
    report["historical_screen_passes"] = candidates
    report["verdict"] = "FORWARD_TEST_CANDIDATE" if candidates else "NO_ROBUST_IMPROVEMENT"
    report["source_sha256"] = data.sha256_file(Path(__file__).resolve())
    report["code_sha256"] = {name: data.sha256_file(data.ROOT / name) for name in (
        "argonus/research/research_0705_execution.py", "argonus/research/research_entry_timing.py", "argonus/research/research_flat_month_exit_risk.py",
        "argonus/research/research_fixed_150k_sizing.py", "argonus/research/research_october_engine_a_rs5.py", "argonus/backtesting/backtest_live_61_trades.py",
    )}
    return report


def markdown(report: dict) -> str:
    ext = report["books"]["Extended65"]
    rows = ext["results"]
    baseline = rows["baseline"]
    ranked = sorted(rows, key=lambda k: rows[k]["pnl_rub"], reverse=True)
    lines = ["# Проверка Extended65 @ 07:05 — 2026-09-10", "",
             f"Вердикт: **{report['verdict']}**. Реальные заявки и конфигурация бота не изменены.", "",
             "Старт 50 000 ₽; позиция до 150 000 ₽ и до 3× капитала перед сделкой. "
             "При стопе 1,25% позиция уменьшается до 80%, чтобы не повышать плановый убыток на стоп. "
             f"Комиссия {report['fills']['fee_side_pct']}% с оборота каждой стороны; "
             f"проскальзывание {report['fills']['slippage_side_bps']} б.п. на каждую сторону.", "",
             "Старый контроль воспроизведён: +161,2605%, 130 630,26 ₽. "
             "Новый replay учитывает гэпы через стоп, закрывается по open в 18:35 "
             "и считает комиссии с фактического оборота. Цена open остаётся допущением.", "",
             "| Правило | N / пропуски | Доходность | PnL, ₽ | MDD по закрытым сделкам | Δ PnL к новому контролю, ₽ |",
             "|---|---:|---:|---:|---:|---:|"]
    for name in ranked:
        r = rows[name]
        lines.append(f"| {name} | {r['trades']} / {r['skips']} | {r['return_pct']:+.2f}% | "
                     f"{r['pnl_rub']:,.0f} | {r['closed_trade_mdd_pct']:.2f}% | {r['pnl_rub'] - baseline['pnl_rub']:+,.0f} |")
    best_name = ranked[0]
    if best_name != "baseline":
        best_policy = next(p for p in report["policies"] if p["name"] == best_name)
        c = ext["paired_checks"][best_name]
        ci = c["monthly_pnl_block_bootstrap_95pct_rub"]
        lines += ["", f"Лучший по всей истории: `{best_name}`. Стоп {best_policy['stop_pct']}%, "
                  f"расстояние от фактического входа до frozen T4 умножается на {best_policy['target_multiple']}; "
                  f"выход {best_policy['time_exit']}. Для long: вход + множитель × (T4 − вход); "
                  "для short та же формула даёт более низкую цель. Это не увеличение самой цены T4 на 25%.", "",
                  f"95%-интервал прибавки PnL при bootstrap по месяцам: [{ci[0]:+,.0f}; {ci[1]:+,.0f}] ₽; "
                  f"p до поправки за перебор = {c['month_block_sign_flip_p']:.3f}. "
                  "Интервал включает убыток относительно контроля — улучшение не доказано.", "",
                  "| Месяц | Контроль, PnL ₽ | Кандидат, PnL ₽ | Δ, ₽ | Счёт кандидата, ₽ |",
                  "|---|---:|---:|---:|---:|"]
        for month, m in rows[best_name]["monthly"].items():
            control_pnl = baseline["monthly"][month]["pnl_rub"]
            lines.append(f"| {month} | {control_pnl:,.0f} | {m['pnl_rub']:,.0f} | "
                         f"{m['pnl_rub'] - control_pnl:+,.0f} | {m['equity_end']:,.0f} |")
    lines += ["", "## Проверка выбора на предыдущих месяцах", "",
              "Первые три месяца используются для выбора. На каждый следующий месяц правило "
              "выбирается только по прошлым сделкам: максимальный PnL при исторической просадке "
              "не хуже контроля. Базовое правило также можно выбрать. В обеих кривых один "
              "непрерывный счёт, без ежемесячного сброса капитала.", "",
              "| Книга | Контроль | Выбор по прошлым месяцам | Δ PnL, ₽ |", "|---|---:|---:|---:|"]
    for name, book in report["books"].items():
        w = book["walk_forward"]
        lines.append(f"| {name} | {w['baseline']['return_pct']:+.2f}% | "
                     f"{w['selector']['return_pct']:+.2f}% | {w['pnl_delta_rub']:+,.0f} |")
    lines += ["", "Результат выбора между несколькими правилами выше отличается от результата "
              "одного неизменного кандидата. В проверке без октября лучший на всей истории "
              "кандидат также должен пройти обе хронологические половины; подробности ниже.", "",
              "| Книга | Лучший кандидат всей истории | Δ ранняя / поздняя половина, ₽ | 95%-интервал Δ PnL, ₽ |",
              "|---|---|---:|---:|"]
    if best_name != "baseline":
        for name, book in report["books"].items():
            c = book["paired_checks"][best_name]
            h, ci = c["chronological_half_delta_rub"], c["monthly_pnl_block_bootstrap_95pct_rub"]
            lines.append(f"| {name} | {best_name} | {h[0]:+,.0f} / {h[1]:+,.0f} | [{ci[0]:+,.0f}; {ci[1]:+,.0f}] |")
    lines += ["", "Это ретроспективная проверка: прежние модели выбора акций уже обучались "
              "с использованием этой истории. Frozen54 входит в Extended65 и не является независимой выборкой.", "",
              "## Проверки кандидатов", "",
              "| Правило | Δ ранняя / поздняя половина, ₽ | Δ без лучшей сделки, ₽ | p с поправкой на 8 вариантов |",
              "|---|---:|---:|---:|"]
    for name in ranked:
        if name == "baseline":
            continue
        c = ext["paired_checks"][name]
        h = c["chronological_half_delta_rub"]
        lines.append(f"| {name} | {h[0]:+,.0f} / {h[1]:+,.0f} | "
                     f"{c['pnl_delta_without_best_contributor_rub']:+,.0f} | {c['family_adjusted_p']:.3f} |")
    lines += ["", "## Чувствительность к проскальзыванию", "",
              "| Б.п. на сторону | Контроль | Лучший вариант по всей истории |", "|---|---:|---:|"]
    for bps, values in ext["slippage_stress"].items():
        lines.append(f"| {bps} | {values['baseline']['return_pct']:+.2f}% | {values[ranked[0]]['return_pct']:+.2f}% |")
    lines += ["", "Полные сделки, месяцы, все параметры, хэши входов, bootstrap и выбранные правила "
              "для каждого месяца сохранены в JSON рядом с отчётом.", "",
              "Ни один исторический результат не активирует торговлю. Для подтверждения прироста "
              "нужны новые сделки с фактическими bid/ask и исполнениями. Свечи не подтверждают "
              "ликвидность на 150 000 ₽; лотность, индивидуальная маржа, финансирование и налоги "
              "здесь не моделируются. Внутридневная просадка может быть больше указанной.", "",
              "Риск подгонки при выборе лучшего бэктеста описан в "
              "[Bailey et al., The probability of backtest overfitting](https://escholarship.org/uc/item/4w1110bb).", "",
              "## Воспроизведение", "", "```bash",
              "python3 -m argonus.research.research_0705_execution --output-dir data/backtests/execution_0705_2026-09-10",
              "python3 -m unittest -q test_research_0705_execution", "```", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slippage-side-bps", type=float, default=5.0)
    parser.add_argument("--fee-side-pct", type=float, default=0.04)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    report = build_report(FillModel(args.fee_side_pct, args.slippage_side_bps))
    rendered = markdown(report)
    if args.output_dir is not None:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        (args.output_dir / "REPORT.md").write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
