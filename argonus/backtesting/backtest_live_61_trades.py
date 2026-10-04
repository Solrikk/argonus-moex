#!/usr/bin/env python3
"""Reproduce the frozen 61-trade Engine-A backtest from local 5-minute data.

The default execution model is the one behind the November 2025 through
16 July 2026 table:

* frozen rank-1, gate-passing candidates with T4 at least 2% away;
* entry at the open of the first 5-minute candle at/after 07:00 MSK;
* 1% stop, absolute T4 target, stop-first on an ambiguous candle;
* time exit on the last candle at/before 18:35 MSK;
* 0.08% round-trip fee;
* full-equity compounding, with independent long/short risk multipliers.

The risk multiplier is the deployed fraction of equity, so it scales both
gross PnL and the fee: (gross_return_pct - fee_pct) * risk_multiplier.
"""
from __future__ import annotations

import argparse
import calendar
import gzip
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


from argonus.paths import PROJECT_ROOT as ROOT
DEFAULT_SNAPSHOT = (
    ROOT / "data/backtests/live_rr2_t4_candidates_2025-11_2026-07.json"
)
DEFAULT_FIVE_MIN_DIR = ROOT / "data/intraday_universe/five_min"
DEFAULT_FALLBACK = ROOT / "data/backtests/five_min_fallback_5_sessions.json"

# Historical snapshot identifiers; actual locations are the defaults above.
LOCAL_SOURCE = "intraday_universe/five_min"
FALLBACK_SOURCE = "backtest_data/five_min_fallback_5_sessions.json"

RU_MONTHS = {
    1: "Январь",
    2: "Февраль",
    3: "Март",
    4: "Апрель",
    5: "Май",
    6: "Июнь",
    7: "Июль",
    8: "Август",
    9: "Сентябрь",
    10: "Октябрь",
    11: "Ноябрь",
    12: "Декабрь",
}


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    entry_time: str = "07:00"
    exit_time: str = "18:35"
    stop_loss_pct: float = 1.0
    fee_pct: float = 0.08
    long_risk: float = 1.0
    short_risk: float = 1.0
    start_capital: float = 150_000.0


@dataclass(frozen=True, slots=True)
class Candle:
    time: str
    open: float
    high: float
    low: float
    close: float


@dataclass(slots=True)
class TradeResult:
    date: str
    month: str
    symbol: str
    direction: str
    target_label: str
    target_price: float
    entry_time: str
    entry_price: float
    stop_price: float
    exit_time: str
    exit_price: float
    exit_reason: str
    ambiguous_candle: bool
    gross_return_pct: float
    fee_pct_at_full_risk: float
    net_return_pct_at_full_risk: float
    risk_multiplier: float
    return_pct: float
    data_source: str
    session_sha256: str
    capital_before: float = 0.0
    capital_after: float = 0.0
    pnl_rub: float = 0.0
    drawdown_pct: float = 0.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Воспроизводимый backtest таблицы из 61 сделки (07:00/SL1/T4/18:35)."
    )
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--five-min-dir", type=Path, default=DEFAULT_FIVE_MIN_DIR)
    parser.add_argument("--fallback", type=Path, default=DEFAULT_FALLBACK)
    parser.add_argument("--entry-time", default="07:00")
    parser.add_argument("--exit-time", default="18:35")
    parser.add_argument("--stop-loss-pct", type=float, default=1.0)
    parser.add_argument("--fee-pct", type=float, default=0.08)
    parser.add_argument("--long-risk", type=float, default=1.0)
    parser.add_argument("--short-risk", type=float, default=1.0)
    parser.add_argument("--start-capital", type=float, default=150_000.0)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Напечатать полный машиночитаемый отчет вместо таблицы.",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        help="Дополнительно сохранить полный отчет JSON по этому пути.",
    )
    parser.add_argument(
        "--verify-controls",
        action="store_true",
        help="Проверить контрольные итоги baseline и long-risk=0.5.",
    )
    parser.add_argument(
        "--skip-data-hash-check",
        action="store_true",
        help="Разрешить прогон на изменившихся свечах (по умолчанию snapshot строгий).",
    )
    return parser.parse_args()


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Файл не найден: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Некорректный JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Ожидался JSON-объект: {path}")
    return value


def validate_clock(value: str, option: str) -> None:
    parts = value.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise RuntimeError(f"{option}: время должно быть в формате HH:MM, получено {value!r}")
    hour, minute = map(int, parts)
    if not (0 <= hour <= 23 and 0 <= minute <= 59) or value != f"{hour:02d}:{minute:02d}":
        raise RuntimeError(f"{option}: некорректное время {value!r}")


def load_snapshot(path: Path) -> dict[str, Any]:
    payload = load_json(path)
    if payload.get("schema_version") != 1:
        raise RuntimeError(f"Неподдерживаемая версия snapshot: {payload.get('schema_version')!r}")

    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise RuntimeError("В snapshot нет списка candidates.")
    expected_count = payload.get("expected_trade_count")
    if expected_count != len(candidates):
        raise RuntimeError(
            f"Snapshot поврежден: expected_trade_count={expected_count}, фактически {len(candidates)}."
        )
    expected_hash = payload.get("candidates_sha256")
    actual_hash = canonical_sha256(candidates)
    if expected_hash != actual_hash:
        raise RuntimeError(
            "Snapshot кандидатов изменился: "
            f"ожидался SHA256 {expected_hash}, получен {actual_hash}."
        )

    selection = payload.get("selection") or {}
    required = {
        "date",
        "month",
        "rank",
        "symbol",
        "direction",
        "gate",
        "target_label",
        "target_price",
        "target_move_pct",
        "data_source",
        "session_sha256",
    }
    seen_dates: set[str] = set()
    previous_date = ""
    for index, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, dict):
            raise RuntimeError(f"Кандидат #{index} не является объектом.")
        missing = sorted(required - candidate.keys())
        if missing:
            raise RuntimeError(f"Кандидат #{index}: нет полей {', '.join(missing)}.")
        trade_date = str(candidate["date"])
        if trade_date[:7] != candidate["month"]:
            raise RuntimeError(f"Кандидат #{index}: date/month не совпадают.")
        if trade_date in seen_dates:
            raise RuntimeError(f"В snapshot больше одной сделки на дату {trade_date}.")
        if previous_date and trade_date <= previous_date:
            raise RuntimeError("Кандидаты должны быть строго отсортированы по дате.")
        seen_dates.add(trade_date)
        previous_date = trade_date
        if candidate["direction"] not in {"long", "short"}:
            raise RuntimeError(f"Кандидат #{index}: неверное направление.")
        if candidate["rank"] != selection.get("rank"):
            raise RuntimeError(f"Кандидат #{index}: rank расходится с selection.")
        if candidate["gate"] != selection.get("gate"):
            raise RuntimeError(f"Кандидат #{index}: gate расходится с selection.")
        if candidate["target_label"] != selection.get("target_label"):
            raise RuntimeError(f"Кандидат #{index}: цель не совпадает с selection.")
        if float(candidate["target_move_pct"]) < float(
            selection.get("minimum_target_move_pct", 0.0)
        ):
            raise RuntimeError(f"Кандидат #{index}: цель не проходит RR floor.")
    return payload


class SessionStore:
    def __init__(
        self,
        five_min_dir: Path,
        fallback_path: Path,
        pinned_start: str,
        pinned_end: str,
        check_hashes: bool,
    ) -> None:
        self.five_min_dir = five_min_dir
        self.fallback_path = fallback_path
        self.pinned_start = pinned_start
        self.pinned_end = pinned_end
        self.check_hashes = check_hashes
        self._symbol_cache: dict[str, dict[str, Any]] = {}
        self._session_cache: dict[tuple[str, str, str], tuple[list[list[Any]], str]] = {}
        self._fallback_payload: dict[str, Any] | None = None

    def _load_fallback(self) -> dict[str, Any]:
        if self._fallback_payload is None:
            payload = load_json(self.fallback_path)
            if payload.get("schema_version") != 1:
                raise RuntimeError("Неподдерживаемая версия fallback свечей.")
            sessions = payload.get("sessions")
            if not isinstance(sessions, dict):
                raise RuntimeError("В fallback нет объекта sessions.")
            expected_count = payload.get("session_count")
            if expected_count != len(sessions):
                raise RuntimeError("Число fallback-сессий не совпадает с manifest.")
            if self.check_hashes:
                expected_hash = payload.get("sessions_sha256")
                actual_hash = canonical_sha256(sessions)
                if expected_hash != actual_hash:
                    raise RuntimeError(
                        "Fallback свечей изменился: "
                        f"ожидался SHA256 {expected_hash}, получен {actual_hash}."
                    )
            self._fallback_payload = payload
        return self._fallback_payload

    def _raw_session(self, candidate: dict[str, Any]) -> tuple[list[Any], str]:
        trade_date = str(candidate["date"])
        symbol = str(candidate["symbol"])
        expected_source = str(candidate["data_source"])
        if expected_source == LOCAL_SOURCE:
            if symbol not in self._symbol_cache:
                path = self.five_min_dir / f"{symbol}.json.gz"
                try:
                    with gzip.open(path, "rt", encoding="utf-8") as handle:
                        value = json.load(handle)
                except FileNotFoundError as exc:
                    raise RuntimeError(f"Нет локальных 5-минуток {path}") from exc
                except (gzip.BadGzipFile, json.JSONDecodeError) as exc:
                    raise RuntimeError(f"Не удалось прочитать {path}: {exc}") from exc
                if not isinstance(value, dict):
                    raise RuntimeError(f"Ожидался объект с датами в {path}")
                self._symbol_cache[symbol] = value
            raw = self._symbol_cache[symbol].get(trade_date)
            if raw is None:
                raise RuntimeError(f"В локальных свечах нет {trade_date} для {symbol}.")
            return raw, LOCAL_SOURCE
        if expected_source == FALLBACK_SOURCE:
            sessions = self._load_fallback()["sessions"]
            key = f"{trade_date}|{symbol}"
            raw = sessions.get(key)
            if raw is None:
                raise RuntimeError(f"В fallback нет сессии {key}.")
            return raw, FALLBACK_SOURCE
        raise RuntimeError(f"Неизвестный источник свечей: {expected_source}")

    def load(self, candidate: dict[str, Any]) -> tuple[list[Candle], str, str]:
        cache_key = (
            str(candidate["date"]),
            str(candidate["symbol"]),
            str(candidate["data_source"]),
        )
        cached = self._session_cache.get(cache_key)
        if cached is None:
            raw, source = self._raw_session(candidate)
            if not isinstance(raw, list):
                raise RuntimeError(f"Свечи {cache_key[0]} {cache_key[1]} не являются списком.")
            normalized: list[list[Any]] = []
            for index, item in enumerate(raw, start=1):
                if not isinstance(item, list) or len(item) < 5:
                    raise RuntimeError(
                        f"Некорректная свеча {cache_key[0]} {cache_key[1]} #{index}."
                    )
                clock = str(item[0])
                if self.pinned_start <= clock <= self.pinned_end:
                    if not all(isinstance(value, (int, float)) for value in item[1:5]):
                        raise RuntimeError(
                            f"Некорректные OHLC {cache_key[0]} {cache_key[1]} {clock}."
                        )
                    normalized.append([clock, *item[1:5]])
            if not normalized:
                raise RuntimeError(f"Пустая торговая сессия {cache_key[0]} {cache_key[1]}.")
            clocks = [item[0] for item in normalized]
            if clocks != sorted(clocks) or len(clocks) != len(set(clocks)):
                raise RuntimeError(f"Время свечей не отсортировано или дублируется: {cache_key}.")
            digest = canonical_sha256(normalized)
            if self.check_hashes and digest != candidate["session_sha256"]:
                raise RuntimeError(
                    f"Свечи изменились для {cache_key[0]} {cache_key[1]}: "
                    f"ожидался {candidate['session_sha256']}, получен {digest}."
                )
            self._session_cache[cache_key] = (normalized, source)
            cached = self._session_cache[cache_key]
        normalized, source = cached
        candles = [
            Candle(
                time=str(item[0]),
                open=float(item[1]),
                high=float(item[2]),
                low=float(item[3]),
                close=float(item[4]),
            )
            for item in normalized
        ]
        return candles, source, canonical_sha256(normalized)


def validate_config(config: BacktestConfig, pinned_start: str, pinned_end: str) -> None:
    validate_clock(config.entry_time, "--entry-time")
    validate_clock(config.exit_time, "--exit-time")
    if config.entry_time > config.exit_time:
        raise RuntimeError("Время входа позже времени выхода.")
    if config.entry_time < pinned_start or config.exit_time > pinned_end:
        raise RuntimeError(
            f"Frozen-свечи покрывают только {pinned_start}–{pinned_end}; "
            "запрошенное окно выходит за эти границы."
        )
    if config.stop_loss_pct <= 0:
        raise RuntimeError("--stop-loss-pct должен быть больше нуля.")
    if config.fee_pct < 0:
        raise RuntimeError("--fee-pct не может быть отрицательной.")
    if config.long_risk < 0 or config.short_risk < 0:
        raise RuntimeError("Risk multipliers не могут быть отрицательными.")
    if config.start_capital <= 0:
        raise RuntimeError("--start-capital должен быть больше нуля.")


def simulate_trade(
    candidate: dict[str, Any],
    candles: list[Candle],
    source: str,
    session_sha256: str,
    config: BacktestConfig,
) -> TradeResult:
    window = [
        candle
        for candle in candles
        if config.entry_time <= candle.time <= config.exit_time
    ]
    if not window:
        raise RuntimeError(
            f"Нет свечей в окне {config.entry_time}–{config.exit_time}: "
            f"{candidate['date']} {candidate['symbol']}"
        )

    first = window[0]
    entry = first.open
    if entry <= 0:
        raise RuntimeError(f"Неположительная цена входа: {candidate['date']} {candidate['symbol']}")
    direction = str(candidate["direction"])
    sign = 1.0 if direction == "long" else -1.0
    target = float(candidate["target_price"])
    if target <= 0:
        raise RuntimeError(f"Неположительная цель: {candidate['date']} {candidate['symbol']}")
    stop = entry * (1.0 - sign * config.stop_loss_pct / 100.0)

    exit_time = window[-1].time
    exit_price = window[-1].close
    exit_reason = "time_exit"
    ambiguous = False
    gross_return = sign * (exit_price / entry - 1.0) * 100.0

    for candle in window:
        if direction == "long":
            hit_stop = candle.low <= stop
            hit_target = candle.high >= target
        else:
            hit_stop = candle.high >= stop
            hit_target = candle.low <= target
        # Conservative rule: stop wins whenever both levels occur in one bar.
        if hit_stop:
            ambiguous = hit_target
            exit_time = candle.time
            exit_price = stop
            exit_reason = "both_stop_first" if ambiguous else "stop"
            gross_return = -config.stop_loss_pct
            break
        if hit_target:
            exit_time = candle.time
            exit_price = target
            exit_reason = "target_t4"
            gross_return = sign * (target / entry - 1.0) * 100.0
            break

    risk = config.long_risk if direction == "long" else config.short_risk
    net_full_risk = gross_return - config.fee_pct
    sized_return = net_full_risk * risk
    return TradeResult(
        date=str(candidate["date"]),
        month=str(candidate["month"]),
        symbol=str(candidate["symbol"]),
        direction=direction,
        target_label=str(candidate["target_label"]),
        target_price=target,
        entry_time=first.time,
        entry_price=entry,
        stop_price=stop,
        exit_time=exit_time,
        exit_price=exit_price,
        exit_reason=exit_reason,
        ambiguous_candle=ambiguous,
        gross_return_pct=gross_return,
        fee_pct_at_full_risk=config.fee_pct,
        net_return_pct_at_full_risk=net_full_risk,
        risk_multiplier=risk,
        return_pct=sized_return,
        data_source=source,
        session_sha256=session_sha256,
    )


def month_label(month: str, period_end: str) -> str:
    year, month_number = map(int, month.split("-"))
    label = f"{RU_MONTHS[month_number]} {year}"
    if period_end.startswith(f"{month}-"):
        end_day = int(period_end[-2:])
        last_day = calendar.monthrange(year, month_number)[1]
        if end_day < last_day:
            label += f" (1–{end_day})"
    return label


def build_report(
    snapshot: dict[str, Any],
    store: SessionStore,
    config: BacktestConfig,
) -> dict[str, Any]:
    candidates = snapshot["candidates"]
    trades: list[TradeResult] = []
    for candidate in candidates:
        candles, source, digest = store.load(candidate)
        trades.append(simulate_trade(candidate, candles, source, digest, config))

    capital = config.start_capital
    peak_capital = capital
    peak_date = "start"
    max_drawdown = 0.0
    max_drawdown_peak_date = peak_date
    max_drawdown_trough_date = "start"
    max_drawdown_peak_capital = peak_capital
    max_drawdown_trough_capital = capital
    for trade in trades:
        trade.capital_before = capital
        factor = 1.0 + trade.return_pct / 100.0
        if factor <= 0:
            raise RuntimeError(
                f"Сделка {trade.date} {trade.symbol} обнуляет капитал: {trade.return_pct:+.4f}%."
            )
        capital *= factor
        trade.capital_after = capital
        trade.pnl_rub = trade.capital_after - trade.capital_before
        if capital > peak_capital:
            peak_capital = capital
            peak_date = trade.date
        trade.drawdown_pct = (capital / peak_capital - 1.0) * 100.0
        if trade.drawdown_pct < max_drawdown:
            max_drawdown = trade.drawdown_pct
            max_drawdown_peak_date = peak_date
            max_drawdown_trough_date = trade.date
            max_drawdown_peak_capital = peak_capital
            max_drawdown_trough_capital = capital

    by_month: dict[str, list[TradeResult]] = defaultdict(list)
    for trade in trades:
        by_month[trade.month].append(trade)
    period_end = str(snapshot["analysis_period"]["to"])
    monthly: list[dict[str, Any]] = []
    through_capital = config.start_capital
    for month in sorted(by_month):
        month_trades = by_month[month]
        month_factor = math.prod(1.0 + trade.return_pct / 100.0 for trade in month_trades)
        month_return = (month_factor - 1.0) * 100.0
        through_capital *= month_factor
        wins = sum(trade.net_return_pct_at_full_risk > 0 for trade in month_trades)
        losses = sum(trade.net_return_pct_at_full_risk <= 0 for trade in month_trades)
        monthly.append(
            {
                "month": month,
                "label": month_label(month, period_end),
                "trades": len(month_trades),
                "wins": wins,
                "losses": losses,
                "return_pct": month_return,
                "capital_on_start_capital": config.start_capital * month_factor,
                "through_capital": through_capital,
            }
        )

    wins = sum(trade.net_return_pct_at_full_risk > 0 for trade in trades)
    losses = sum(trade.net_return_pct_at_full_risk <= 0 for trade in trades)
    summary = {
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "final_capital": capital,
        "total_return_pct": (capital / config.start_capital - 1.0) * 100.0,
        "total_pnl_rub": capital - config.start_capital,
        "trade_level_max_drawdown_pct": max_drawdown,
        "max_drawdown_peak_date": max_drawdown_peak_date,
        "max_drawdown_trough_date": max_drawdown_trough_date,
        "max_drawdown_peak_capital": max_drawdown_peak_capital,
        "max_drawdown_trough_capital": max_drawdown_trough_capital,
        "directions": dict(sorted(Counter(trade.direction for trade in trades).items())),
        "exit_reasons": dict(sorted(Counter(trade.exit_reason for trade in trades).items())),
        "data_sources": dict(sorted(Counter(trade.data_source for trade in trades).items())),
    }
    return {
        "schema_version": 1,
        "snapshot": {
            "description": snapshot.get("description"),
            "source_artifact": snapshot.get("source_artifact"),
            "source_sha256": snapshot.get("source_sha256"),
            "candidates_sha256": snapshot.get("candidates_sha256"),
            "analysis_period": snapshot.get("analysis_period"),
            "selection": snapshot.get("selection"),
        },
        "config": asdict(config),
        "summary": summary,
        "monthly": monthly,
        "trades": [asdict(trade) for trade in trades],
    }


def signed_pct(value: float, decimals: int = 1) -> str:
    sign = "+" if value >= 0 else "−"
    return f"{sign}{abs(value):.{decimals}f}%"


def rub(value: float) -> str:
    return f"{round(value):,}".replace(",", " ")


def print_table(report: dict[str, Any]) -> None:
    config = report["config"]
    summary = report["summary"]
    print(
        f"Модель: {config['entry_time']} / SL {config['stop_loss_pct']:g}% / T4 / "
        f"{config['exit_time']} / stop-first / fee {config['fee_pct']:g}% | "
        f"risk long={config['long_risk']:g}, short={config['short_risk']:g}"
    )
    print()
    print(
        f"{'Месяц':<23} {'Сделок':>6} {'W/L':>7} {'Итог':>8} "
        f"{'₽ на 150k':>12} {'Сквозной капитал':>18}"
    )
    for row in report["monthly"]:
        print(
            f"{row['label']:<23} {row['trades']:>6} "
            f"{row['wins']:>2}/{row['losses']:<2} "
            f"{signed_pct(row['return_pct']):>8} "
            f"{rub(row['capital_on_start_capital']):>12} "
            f"{rub(row['through_capital']):>18}"
        )
    print(
        f"{'Итог':<23} {summary['trades']:>6} "
        f"{summary['wins']:>2}/{summary['losses']:<2} "
        f"{signed_pct(summary['total_return_pct']):>8} "
        f"{'—':>12} {rub(summary['final_capital']):>18}"
    )
    print()
    print(
        "Trade-level MDD: "
        f"{signed_pct(summary['trade_level_max_drawdown_pct'], 3)} "
        f"({summary['max_drawdown_peak_date']} → {summary['max_drawdown_trough_date']})"
    )
    print(
        "Свечи: "
        + ", ".join(
            f"{source}={count}" for source, count in summary["data_sources"].items()
        )
    )


def verify_controls(
    snapshot: dict[str, Any],
    store: SessionStore,
    start_capital: float,
) -> str:
    expected = snapshot.get("expected_controls") or {}
    cases = [
        (
            "baseline long=1 short=1",
            BacktestConfig(start_capital=start_capital),
            "baseline_long_1_short_1_total_return_pct",
        ),
        (
            "long=0.5 short=1",
            BacktestConfig(long_risk=0.5, start_capital=start_capital),
            "half_long_0_5_short_1_total_return_pct",
        ),
    ]
    lines = []
    for label, config, key in cases:
        result = build_report(snapshot, store, config)
        actual = float(result["summary"]["total_return_pct"])
        wanted = float(expected[key])
        if not math.isclose(actual, wanted, rel_tol=0.0, abs_tol=1e-9):
            raise RuntimeError(
                f"Контроль {label} не совпал: ожидалось {wanted:.12f}%, "
                f"получено {actual:.12f}%."
            )
        lines.append(f"{label}: {actual:+.6f}%")
    return "Контрольные итоги: PASS | " + " | ".join(lines)


def main() -> int:
    args = parse_args()
    snapshot = load_snapshot(args.snapshot.resolve())
    model = snapshot.get("execution_model") or {}
    pinned_start = str(model.get("entry_time_msk", "07:00"))
    pinned_end = str(model.get("time_exit_msk", "18:35"))
    config = BacktestConfig(
        entry_time=args.entry_time,
        exit_time=args.exit_time,
        stop_loss_pct=args.stop_loss_pct,
        fee_pct=args.fee_pct,
        long_risk=args.long_risk,
        short_risk=args.short_risk,
        start_capital=args.start_capital,
    )
    validate_config(config, pinned_start, pinned_end)
    store = SessionStore(
        five_min_dir=args.five_min_dir.resolve(),
        fallback_path=args.fallback.resolve(),
        pinned_start=pinned_start,
        pinned_end=pinned_end,
        check_hashes=not args.skip_data_hash_check,
    )
    report = build_report(snapshot, store, config)

    control_message = None
    if args.verify_controls:
        control_message = verify_controls(snapshot, store, args.start_capital)

    if args.output_json:
        output_path = args.output_json.resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if control_message:
            print(control_message, file=sys.stderr)
    else:
        print_table(report)
        if args.output_json:
            print(f"JSON: {args.output_json.resolve()}")
        if control_message:
            print(control_message)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        raise SystemExit(2)
