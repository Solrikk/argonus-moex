#!/usr/bin/env python3
"""Argonus: protected trading and durable reconciliation in T-Invest API.

run_tick.sh selects A_plus_mean on YOUR_ACCOUNT_NAME: the legacy A+RS5 primary enters
at 07:05, with target125 from the actual fill. A closed-bar morning scanner
adds independent and repeated entries from 07:20 through 09:30, ranked by
monthly models trained on strictly earlier days. Each additional trade has
its own journal identity, 1% stop and 2% target. All trades share at most
150,000 RUB / 3x net account equity, three positions and a 3% planned-stop
risk budget. New entries stop after a 3% daily equity loss.

Entries use manifest-pinned, fresh depth-50 FOK orders. Actual fills are
saved before STOP_LOSS, then TAKE_PROFIT. Lost order acknowledgements are
reconciled by the frozen request id; missing FOK requests are never replayed.
Existing positions retain their frozen contract. Time exit remains 18:35.
Legacy B, confidence sizing and pullback are optional, disabled by the active
launcher. Historical research is not a forecast of broker execution.

Commands: trade_bot.py tick/status/enter/exit/watch [--live]. The main tick
repairs protection, reconciles entries and exits, removes paired orphan
stops, closes overnight exposure, and blocks new entries on uncertainty.
The shared lock prevents overlapping ticks. run_candidate.sh schedules
aligned 30-second ticks; --dry-run --once never calls the broker.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import uuid
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from argonus.paths import PROJECT_ROOT, MODEL_DIR

PROJECT_DIR = str(PROJECT_ROOT)

from argonus.watchlists import watchlist_best_target as wbt
from argonus.trading import production_entry_0705 as entry0705
from argonus.trading import production_profit_target as profit_target
from argonus.watchlists import shadow_rs5_selector as rs5_shadow
from argonus.market_data.tbank_market_data import TBankApiError, TBankInvestClient, _get_field, quotation_to_float

STATE_PATH = os.path.join(PROJECT_DIR, "runtime/bot_state.json")
LOCK_PATH = os.path.join(PROJECT_DIR, "runtime/bot.lock")
WATCHLIST_PATH = os.path.join(PROJECT_DIR, "data/watchlists/watchlist.txt")
DEFAULT_ACCOUNT_NAME = os.environ.get("BOT_ACCOUNT_NAME", "Бот")
RUB_POSITION_FIGI = "RUB000UTSTOM"


def _parse_hhmm(raw: str, fallback: str) -> dtime:
    try:
        return dtime.fromisoformat(raw.strip())
    except (ValueError, AttributeError):
        return dtime.fromisoformat(fallback)


ENTRY_START = _parse_hhmm(os.environ.get("BOT_ENTRY_START", ""), "07:00")
ENTRY_DEADLINE = _parse_hhmm(os.environ.get("BOT_ENTRY_DEADLINE", ""), "08:00")
EXIT_TIME = _parse_hhmm(os.environ.get("BOT_EXIT_TIME", ""), "18:35")
ENTRY_EXECUTION_POLICY = os.environ.get(
    "BOT_ENTRY_EXECUTION_POLICY", "legacy_smart_fill"
).strip()
ENTRY_BOOK_DEPTH = int(os.environ.get("BOT_ENTRY_BOOK_DEPTH", str(entry0705.BOOK_DEPTH)))
ENTRY_MAX_QUOTE_AGE_MS = int(
    os.environ.get("BOT_ENTRY_MAX_QUOTE_AGE_MS", str(entry0705.MAX_QUOTE_AGE_MS))
)
ENTRY_MAX_IMPACT_BPS = float(
    os.environ.get("BOT_ENTRY_MAX_IMPACT_BPS", str(entry0705.MAX_IMPACT_BPS))
)
ENTRY_ACTIVATION_MANIFEST_PATH = os.environ.get(
    "BOT_ENTRY_ACTIVATION_MANIFEST",
    os.path.join(PROJECT_DIR, "config/entry_0705_activation_manifest.json"),
)
TARGET_DISTANCE_MULTIPLIER = float(os.environ.get("BOT_TARGET_DISTANCE_MULTIPLIER", "1.0"))
TARGET_ACTIVATION_MANIFEST_PATH = os.environ.get(
    "BOT_TARGET_ACTIVATION_MANIFEST",
    os.path.join(PROJECT_DIR, "config/profit_target_activation_manifest.json"),
)
if TARGET_DISTANCE_MULTIPLIER not in (1.0, profit_target.MULTIPLIER):
    raise RuntimeError("BOT_TARGET_DISTANCE_MULTIPLIER должен быть 1.0 или 1.25.")
if ENTRY_START > ENTRY_DEADLINE or ENTRY_DEADLINE >= EXIT_TIME:
    raise RuntimeError("Окно входа должно быть ENTRY_START <= ENTRY_DEADLINE < EXIT_TIME.")
if ENTRY_EXECUTION_POLICY not in ("legacy_smart_fill", entry0705.POLICY):
    raise RuntimeError(f"Неизвестный BOT_ENTRY_EXECUTION_POLICY={ENTRY_EXECUTION_POLICY!r}.")
if ENTRY_BOOK_DEPTH != entry0705.BOOK_DEPTH:
    raise RuntimeError(f"BOT_ENTRY_BOOK_DEPTH должен быть равен {entry0705.BOOK_DEPTH}.")
if ENTRY_MAX_QUOTE_AGE_MS < 0:
    raise RuntimeError("BOT_ENTRY_MAX_QUOTE_AGE_MS должен быть >= 0.")
if not math.isfinite(ENTRY_MAX_IMPACT_BPS) or ENTRY_MAX_IMPACT_BPS < 0:
    raise RuntimeError("BOT_ENTRY_MAX_IMPACT_BPS должен быть конечным числом >= 0.")
# Лимитная лесенка: сколько секунд держать заявку на каждой ступени и предел
# проскальзывания для агрессивной ступени (кросс стакана с запасом).
LIMIT_STAGE_SECONDS = int(os.environ.get("BOT_LIMIT_STAGE_SECONDS", "25"))
MAX_SLIPPAGE_PCT = float(os.environ.get("BOT_MAX_SLIPPAGE_PCT", "0.3"))
# Вход с откатом (валидировано на 6 месяцах, LOMO: EV/сделку +0.49% -> +0.51%,
# майский минус уходит; механизм — открытие в среднем возвращается к цене):
# лимит на open∓PULLBACK_PCT, ждём WAIT_MIN, дальше FALLBACK: market (войти
# лесенкой по текущей) или skip (нет отката — нет сделки). 0 = вход сразу.
PULLBACK_PCT = float(os.environ.get("BOT_PULLBACK_PCT", "0.3"))
PULLBACK_WAIT_MIN = int(os.environ.get("BOT_PULLBACK_WAIT_MIN", "60"))
PULLBACK_FALLBACK = os.environ.get("BOT_PULLBACK_FALLBACK", "market")
# Два мотора (валидировано 7 мес: +59% -> +95..150%, декабрь-стресс не хуже):
# A = основной пайплайн (когда фильтр дня пропускает), B = universe-шорт каждый
# день. Ставка по уверенности: рискуем 1.5% вместо 1% на сделках с высокой
# вероятностью (A: prob>=0.6, B: prob>=0.85; калибровка подтверждена).
SECOND_ENGINE = os.environ.get("BOT_SECOND_ENGINE", "1") != "0"
CONF_SIZING = os.environ.get("BOT_CONF_SIZING", "1") != "0"
MAX_LEVERAGE = float(os.environ.get("BOT_MAX_LEVERAGE", "2.6"))
TARGET_POSITION_RUB = float(os.environ.get("BOT_TARGET_POSITION_RUB", "0"))
RISK_PCT = float(os.environ.get("BOT_RISK_PCT", "1.0"))
if not math.isfinite(MAX_LEVERAGE) or MAX_LEVERAGE <= 0:
    raise RuntimeError("BOT_MAX_LEVERAGE должен быть конечным положительным числом.")
if not math.isfinite(TARGET_POSITION_RUB) or TARGET_POSITION_RUB < 0:
    raise RuntimeError("BOT_TARGET_POSITION_RUB должен быть конечным числом >= 0.")
# Направленный risk-control для мотора A. Это не дополнительное плечо: значение
# ниже 1 только уменьшает long-позицию. На 61-сделочном snapshot Nov'25–Jul'26
# long дал 6/13 и -5.0%; multiplier=0.5 улучшил net +36.0% -> +39.6% и
# trade-level maxDD -4.65% -> -3.48%. Выборка мала, поэтому ручка обратима и
# должна перепроверяться по forward-журналу, а не считаться доказанной альфой.
LONG_RISK_MULTIPLIER = float(os.environ.get("BOT_LONG_RISK_MULTIPLIER", "0.5"))
if not 0.0 < LONG_RISK_MULTIPLIER <= 1.0:
    raise RuntimeError("BOT_LONG_RISK_MULTIPLIER должен быть в диапазоне (0, 1].")
# Разбор минусовых месяцев (2026-07-17, 174 дня Nov'25–Jul'26, все исходы сшиты
# с фильтром по датам): фильтр дня для мотора A режет 52% дней почти монеткой
# (49 спасённых убытков / 41 упущенный плюс) и ухудшает ноябрь/декабрь — теперь
# выключен по умолчанию (BOT_DAY_FILTER=1 вернёт). Вместо него режимные ворота
# на готовом флаге market_bullish (индекс vs EMA20): шорт — только в медвежьем,
# лонг — только в бычьем (+5.1% -> +12.3% брутто, минус ~74 круга комиссий),
# плюс guard мотора B на шорты A: не шортить бумагу, выросшую >8% за 10 дней.
DAY_FILTER = os.environ.get("BOT_DAY_FILTER", "0") == "1"
REGIME_GATES = os.environ.get("BOT_REGIME_GATES", "1") != "0"
SHORT_RALLY_GUARD_PCT = float(os.environ.get("BOT_SHORT_RALLY_GUARD_PCT", "8.0"))
# Reward:risk floor (2026-07-17 автопсия 98 сделок, in-sample Nov'25–Jul'26):
# сделки, где цель выхода < 2× стопа (<2% при стопе 1%), убыточны как класс
# (n=37, winrate 43% < breakeven; сумма −1.6%). Отсекаем их — экспектанси-мат:
# при стопе 1% и цели T% безубыточный winrate = 1/(1+T), для T<2% нужен >40%.
# Эффект (моя реконструкция): +26.3%→+28.7%, красных месяцев 4→2 (январь и март
# уходят в плюс), maxDD −4.2%→−1.3%, устойчиво при обоих допущениях о филлах.
# Дефолт 0 = выкл (живое поведение не меняется); BOT_MIN_TARGET_PCT=2.0 включает.
MIN_TARGET_PCT = float(os.environ.get("BOT_MIN_TARGET_PCT", "0.0"))
# RS5 changes live selection only when both the explicit environment switch and
# a pinned, internally consistent activation manifest are present.  Any missing
# or mismatched artifact keeps the legacy rank-1 path unchanged.
RS5_ACTIVATION_REQUESTED = os.environ.get("BOT_RS5_SELECTOR", "0") == "1"
RS5_SELECTOR_THRESHOLD = float(
    os.environ.get("BOT_RS5_THRESHOLD", str(rs5_shadow.DEFAULT_THRESHOLD_PP))
)
if not math.isfinite(RS5_SELECTOR_THRESHOLD):
    raise RuntimeError("BOT_RS5_THRESHOLD должен быть конечным числом.")
RS5_SHADOW_DIR = os.environ.get(
    "BOT_RS5_SHADOW_DIR", os.path.join(PROJECT_DIR, "runtime/shadow_selector")
)
RS5_ACTIVATION_MANIFEST_PATH = os.environ.get(
    "BOT_RS5_ACTIVATION_MANIFEST",
    os.path.join(PROJECT_DIR, "config/rs5_activation_manifest.json"),
)
RS5_REQUIRED_RUNTIME_ARTIFACTS = {
    "argonus/paths.py", "argonus/serialization.py",
    "argonus/trading/trade_bot.py",
    "scripts/run_tick.sh",
    "argonus/trading/production_entry_0705.py",
    "config/entry_0705_activation_manifest.json",
    "argonus/trading/production_profit_target.py",
    "config/profit_target_activation_manifest.json",
    "data/backtests/RS5_SHADOW_RESEARCH_2026-07-18.md",
}
RS5_REQUIRED_CONFIG_KEYS = {
    "day_filter",
    "regime_gates",
    "short_rally_guard_pct",
    "minimum_target_pct",
    "long_risk_multiplier",
    "risk_pct",
    "second_engine",
    "confidence_sizing",
    "pullback_pct",
    "rs5_threshold_pp",
    "target_position_rub",
    "max_leverage",
    "entry_start",
    "entry_deadline",
    "entry_execution_policy",
    "entry_book_depth",
    "entry_max_quote_age_ms",
    "entry_max_impact_bps",
    "entry_time_in_force",
}
# Pre-registered forward experiments are telemetry only.  The production
# wrapper opts in explicitly; importing/running trade_bot directly keeps them
# off.  The research module is loaded lazily inside fail-open hooks so a
# missing/broken research artifact can never prevent the bot from importing.
FORWARD_SHADOW_ENABLED = os.environ.get("BOT_FORWARD_SHADOW", "0") == "1"
FORWARD_SHADOW_DIR = os.environ.get(
    "BOT_FORWARD_SHADOW_DIR", os.path.join(PROJECT_DIR, "runtime/forward_shadow")
)
FORWARD_SHADOW_MANIFEST_PATH = os.environ.get(
    "BOT_FORWARD_SHADOW_MANIFEST",
    os.path.join(PROJECT_DIR, "config/forward_shadow_manifest.json"),
)
REQUEST_ORDER_ID_TYPE = "ORDER_ID_TYPE_REQUEST"
TERMINAL_ORDER_STATUSES = {
    "EXECUTION_REPORT_STATUS_FILL",
    "EXECUTION_REPORT_STATUS_CANCELLED",
    "EXECUTION_REPORT_STATUS_REJECTED",
}


class OrderSubmissionUncertain(RuntimeError):
    """PostOrder may have reached the broker, so no second order is allowed."""

    def __init__(self, request_id: str, message: str):
        super().__init__(message)
        self.request_id = request_id


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def acquire_lock():
    """Один процесс бота за раз: tick может совпасть с ручным enter/exit."""
    handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        return None


def read_state() -> dict | None:
    if not os.path.isfile(STATE_PATH):
        return None
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def write_state(live: bool, data: dict) -> None:
    if not live:
        log(f"DRY-RUN журнал дня: {json.dumps(data, ensure_ascii=False)}")
        return
    tmp_path = STATE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, STATE_PATH)
    directory_fd = os.open(PROJECT_DIR, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def load_journal() -> dict | None:
    """Журнал дня v2: {"date": ..., "trades": [сделка, ...]} (сделка несёт "engine").
    Старый однослотовый формат читается как журнал с одной сделкой."""
    state = read_state()
    if not state:
        return None
    if "trades" in state:
        return state
    return {"date": state.get("date"), "trades": [state]}


def upsert_trade(live: bool, today: str, trade: dict) -> None:
    """Записать/обновить сделку своего мотора в журнале дня."""
    trade = {**trade, "date": today}
    if not live:
        log(f"DRY-RUN журнал [{trade.get('engine', '?')}]: {json.dumps(trade, ensure_ascii=False)}")
        return
    journal = load_journal()
    if not journal or journal.get("date") != today:
        journal = {"date": today, "trades": []}
    keep = [t for t in journal["trades"] if t.get("engine") != trade.get("engine")]
    write_state(True, {**journal, "date": today, "trades": keep + [trade]})


def float_to_quotation(value: float) -> dict:
    quantized = Decimal(str(value)).quantize(Decimal("0.000000001"))
    units = int(quantized)
    nano = int((quantized - units) * 1_000_000_000)
    return {"units": str(units), "nano": nano}


def round_to_increment(price: float, increment: float) -> float:
    if increment <= 0:
        return price
    steps = (Decimal(str(price)) / Decimal(str(increment))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(steps * Decimal(str(increment)))


def target_move_pct_from_entry(direction: str, entry_price: float, target_price: float) -> float:
    """Потенциал цели от фактической/ожидаемой цены входа, со знаком прибыли."""
    if entry_price <= 0 or target_price <= 0:
        return float("-inf")
    if direction == "long":
        return (target_price / entry_price - 1.0) * 100.0
    if direction == "short":
        return (1.0 - target_price / entry_price) * 100.0
    raise ValueError(f"Неизвестное направление: {direction}")


def latest_order_execution_time(response: dict) -> str | None:
    """Return the last broker-reported fill stage as a timezone-aware ISO string."""
    parsed: list[datetime] = []
    for stage in response.get("stages") or []:
        raw = _get_field(stage, "executionTime", "execution_time")
        if not raw or not isinstance(raw, str):
            continue
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if value.tzinfo is not None and value.utcoffset() is not None:
            parsed.append(value)
    if not parsed:
        return None
    return max(parsed).isoformat(timespec="seconds")


class TradeBot:
    def __init__(self, live: bool):
        self.live = live
        self.client = TBankInvestClient(user_agent="argonus-trade-bot/1.0")
        self.account_id = self._resolve_account()

    def _post(self, method: str, payload: dict) -> dict:
        return self.client._post(f"tinkoff.public.invest.api.contract.v1.{method}", payload)

    def _resolve_account(self) -> str:
        accounts = self._post("UsersService/GetAccounts", {}).get("accounts", [])
        for account in accounts:
            if account.get("name") == DEFAULT_ACCOUNT_NAME and account.get("status") == "ACCOUNT_STATUS_OPEN":
                if account.get("accessLevel") != "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS":
                    raise RuntimeError(f"Счёт «{DEFAULT_ACCOUNT_NAME}»: токен без полного доступа.")
                return str(account["id"])
        raise RuntimeError(f"Счёт с именем «{DEFAULT_ACCOUNT_NAME}» не найден среди доступных токену.")

    # ---------- данные счёта ----------

    def cash_rub(self) -> float:
        portfolio = self._post("OperationsService/GetPortfolio", {"accountId": self.account_id, "currency": "RUB"})
        for position in portfolio.get("positions", []):
            if position.get("figi") == RUB_POSITION_FIGI:
                return quotation_to_float(_get_field(position, "quantity"))
        return 0.0

    def share_positions(self) -> list[dict]:
        portfolio = self._post("OperationsService/GetPortfolio", {"accountId": self.account_id, "currency": "RUB"})
        return [p for p in portfolio.get("positions", []) if p.get("instrumentType") == "share"]

    def portfolio_snapshot(self) -> dict:
        """Net RUB equity and all gross share exposure from one broker snapshot."""
        portfolio = self._post("OperationsService/GetPortfolio", {"accountId": self.account_id, "currency": "RUB"})
        positions = portfolio.get("positions") or []
        equity = quotation_to_float(_get_field(portfolio, "totalAmountPortfolio"))
        if not math.isfinite(equity) or equity <= 0:
            raise RuntimeError("GetPortfolio не вернул положительный net equity в RUB")
        return {"equity_rub": equity,
                "positions": [p for p in positions if p.get("instrumentType") == "share"],
                "other_exposure": any(p.get("instrumentType") != "share" and p.get("figi") != RUB_POSITION_FIGI
                                      and abs(quotation_to_float(_get_field(p, "quantity"))) > 0 for p in positions)}

    def active_orders(self) -> list[dict]:
        return self._post("OrdersService/GetOrders", {"accountId": self.account_id}).get("orders") or []

    def stop_orders(self) -> list[dict]:
        response = self._post("StopOrdersService/GetStopOrders", {"accountId": self.account_id})
        return response.get("stopOrders") or []

    def share_info(self, symbol: str) -> dict:
        uid = self.client.resolve_share_instrument_id(symbol, wbt.DEFAULT_BOARD)
        if "-" in uid:
            payload = {"idType": "INSTRUMENT_ID_TYPE_UID", "id": uid}
        else:
            payload = {"idType": "INSTRUMENT_ID_TYPE_TICKER", "classCode": wbt.DEFAULT_BOARD, "id": symbol}
        share = self._post("InstrumentsService/ShareBy", payload).get("instrument") or {}
        return {
            "uid": share.get("uid") or uid,
            "lot": int(share.get("lot") or 1),
            "min_price_increment": quotation_to_float(_get_field(share, "minPriceIncrement")) or 0.01,
            "short_enabled": bool(share.get("shortEnabledFlag")),
            "api_trade_available": bool(share.get("apiTradeAvailableFlag")),
            "name": share.get("name") or symbol,
        }

    def last_price(self, uid: str) -> float:
        response = self._post("MarketDataService/GetLastPrices", {"instrumentId": [uid]})
        prices = response.get("lastPrices") or []
        if not prices:
            raise RuntimeError("Нет последней цены по инструменту.")
        return quotation_to_float(_get_field(prices[0], "price"))

    def trading_status(self, uid: str) -> dict:
        response = self._post("MarketDataService/GetTradingStatus", {"instrumentId": uid})
        return {
            "status": response.get("tradingStatus"),
            "market_order_available": bool(response.get("marketOrderAvailableFlag")),
            "limit_order_available": bool(response.get("limitOrderAvailableFlag")),
        }

    # ---------- заявки ----------

    def order_book_touch(self, uid: str) -> tuple[float | None, float | None]:
        try:
            book = self._post("MarketDataService/GetOrderBook", {"instrumentId": uid, "depth": 1})
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            bid = quotation_to_float(_get_field(bids[0], "price")) if bids else None
            ask = quotation_to_float(_get_field(asks[0], "price")) if asks else None
            return bid or None, ask or None
        except Exception as exc:  # noqa: BLE001
            log(f"стакан недоступен: {exc}")
            return None, None

    def order_book(self, uid: str, depth: int) -> dict:
        """One authoritative exchange-book snapshot; callers validate it."""
        return self._post(
            "MarketDataService/GetOrderBook",
            {"instrumentId": uid, "depth": int(depth)},
        )

    def estimate_order_price(self, uid: str, lots: int, direction: str, price: float) -> dict:
        """Read-only preflight стоимости и комиссии через OrdersService/GetOrderPrice.

        Это оценка до исполнения, не фактическая комиссия. Фактическое значение
        доступно позднее в executedCommission у terminal order state.
        """
        response = self._post(
            "OrdersService/GetOrderPrice",
            {
                "accountId": self.account_id,
                "instrumentId": uid,
                "quantity": str(lots),
                "direction": f"ORDER_DIRECTION_{direction.upper()}",
                "price": float_to_quotation(price),
            },
        )
        notional = quotation_to_float(_get_field(response, "initialOrderAmount"))
        commission_rub = (
            quotation_to_float(_get_field(response, "executedCommissionRub"))
            or quotation_to_float(_get_field(response, "executedCommission"))
        )
        return {
            "notional_rub": notional,
            "commission_rub": commission_rub,
            "fee_side_pct": commission_rub / notional * 100.0 if notional > 0 else None,
        }

    def max_order_lots(self, uid: str, direction: str, price: float) -> int:
        """Read-only broker limit including margin for a long/short entry."""
        response = self._post(
            "OrdersService/GetMaxLots",
            {
                "accountId": self.account_id,
                "instrumentId": uid,
                "price": float_to_quotation(price),
            },
        )
        if direction == "long":
            limits = _get_field(response, "buyMarginLimits", default={}) or {}
            raw = _get_field(limits, "buyMaxLots")
        elif direction == "short":
            limits = _get_field(response, "sellMarginLimits", default={}) or {}
            raw = _get_field(limits, "sellMaxLots")
        else:
            raise ValueError(f"неизвестное направление {direction!r}")
        try:
            lots = int(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"GetMaxLots не вернул маржинальный лимит для {direction}"
            ) from exc
        return max(lots, 0)

    def place_limit(
        self,
        uid: str,
        lots: int,
        direction: str,
        price: float,
        confirm_margin_trade: bool = False,
        request_id: str | None = None,
        time_in_force: str | None = None,
    ) -> tuple[str, dict]:
        order_id = request_id or str(uuid.uuid4())
        payload = {
            "accountId": self.account_id,
            "instrumentId": uid,
            "quantity": str(lots),
            "direction": f"ORDER_DIRECTION_{direction.upper()}",
            "orderType": "ORDER_TYPE_LIMIT",
            "price": float_to_quotation(price),
            "orderId": order_id,
        }
        if confirm_margin_trade:
            payload["confirmMarginTrade"] = True
        if time_in_force:
            payload["timeInForce"] = time_in_force
        response = self._post("OrdersService/PostOrder", payload)
        return order_id, response

    def place_fok_limit(
        self,
        uid: str,
        lots: int,
        direction: str,
        price: float,
        *,
        request_id: str,
        confirm_margin_trade: bool = False,
    ) -> tuple[str, dict]:
        """Submit the single marketable production-entry FOK request."""
        return self.place_limit(
            uid,
            lots,
            direction,
            price,
            confirm_margin_trade=confirm_margin_trade,
            request_id=request_id,
            time_in_force=entry0705.TIME_IN_FORCE,
        )

    def order_state(self, order_id: str) -> dict:
        response = self._post("OrdersService/GetOrderState", {
            "accountId": self.account_id,
            "orderId": order_id,
            "orderIdType": REQUEST_ORDER_ID_TYPE,
        })
        executed = int(response.get("lotsExecuted") or 0)
        avg_price = (
            quotation_to_float(_get_field(response, "averagePositionPrice"))
            or quotation_to_float(_get_field(response, "executedOrderPrice"))
        )
        return {
            "status": response.get("executionReportStatus"),
            "lots": executed,
            "avg_price": avg_price,
            "execution_time": latest_order_execution_time(response),
        }

    def cancel_order(self, order_id: str, *, suppress_errors: bool = True) -> None:
        try:
            self._post("OrdersService/CancelOrder", {
                "accountId": self.account_id,
                "orderId": order_id,
                "orderIdType": REQUEST_ORDER_ID_TYPE,
            })
        except Exception:  # noqa: BLE001
            if not suppress_errors:
                raise
            # Уже исполнена или снята — обычный caller заберёт факт из order_state.
            pass

    def _wait_fill(self, order_id: str, seconds: int) -> dict:
        waited = 0
        state = self.order_state(order_id)
        while state["status"] != "EXECUTION_REPORT_STATUS_FILL" and waited < seconds:
            step = min(5, max(1, seconds - waited))
            time.sleep(step)
            waited += step
            state = self.order_state(order_id)
        return state

    def smart_fill(
        self,
        uid: str,
        lots: int,
        direction: str,
        increment: float,
        reference_price: float,
        must_fill: bool,
        confirm_margin_trade: bool = False,
        absolute_target_price: float | None = None,
        max_position_rub: float | None = None,
        lot_size: int = 1,
        submission_intent_callback=None,
    ) -> tuple[int, float]:
        """Лимитная лесенка вместо рыночного ордера. Ступени (каждая ~LIMIT_STAGE_SECONDS):
        1) пассивно у своей стороны стакана (без проскальзывания);
        2) по встречной цене (marketable limit — исполнение сразу, цена ограничена);
        3) кросс стакана с запасом MAX_SLIPPAGE_PCT от текущей цены;
        4) только если must_fill (принудительное закрытие) — рыночный ордер.
        Неисполненный остаток каждой ступени снимается — заявки не висят.
        Возвращает (исполнено лотов, средняя цена)."""
        buy = direction == "buy"
        remaining = lots
        fills: list[tuple[int, float]] = []
        if max_position_rub is not None and (
            not math.isfinite(max_position_rub) or max_position_rub <= 0
        ):
            raise ValueError("max_position_rub должен быть положительным")
        if lot_size < 1:
            raise ValueError("lot_size должен быть >= 1")

        def stage_price(stage: int) -> tuple[float | None, float | None]:
            bid, ask = self.order_book_touch(uid)
            own = bid if buy else ask
            opposite = ask if buy else bid
            if stage == 0:
                order_price = own or opposite or reference_price
            elif stage == 1:
                order_price = opposite or reference_price
            else:
                base = opposite or reference_price
                cap = 1 + MAX_SLIPPAGE_PCT / 100 if buy else 1 - MAX_SLIPPAGE_PCT / 100
                order_price = base * cap
            # A marketable sell limit can receive price improvement above its
            # limit (stage 3 normally fills near the current bid).  Use that
            # current executable touch for the RUB cap, not the lower limit.
            notional_guard_price = (
                order_price if buy else max(order_price, bid or 0.0)
            )
            return order_price, notional_guard_price

        if not self.live:
            log(
                f"DRY-RUN smart_fill {direction} {lots} лот(ов): лесенка "
                f"[своя сторона → встречная → кросс ±{MAX_SLIPPAGE_PCT}%"
                + (" → market" if must_fill else "") + f"], ступень {LIMIT_STAGE_SECONDS}с"
            )
            return lots, reference_price

        for stage in (0, 1, 2):
            if remaining <= 0:
                break
            request_id: str | None = None
            try:
                raw_price, raw_notional_guard_price = stage_price(stage)
                if raw_price is None or raw_price <= 0:
                    continue
                price = round_to_increment(raw_price, increment)
                notional_guard_price = max(
                    price,
                    float(raw_notional_guard_price or price),
                )
                if absolute_target_price is not None:
                    trade_direction = "long" if buy else "short"
                    target_move = target_move_pct_from_entry(
                        trade_direction, price, absolute_target_price
                    )
                    if target_move <= 0:
                        log(
                            f"лесенка: ступень {stage + 1} @{price} пропущена — "
                            f"абсолютная цель {absolute_target_price} уже позади "
                            f"({target_move:+.2f}%)"
                        )
                        continue
                submit_lots = remaining
                if max_position_rub is not None:
                    filled_notional = sum(
                        qty * fill_price * lot_size for qty, fill_price in fills
                    )
                    available_notional = max(max_position_rub - filled_notional, 0.0)
                    submit_lots = min(
                        remaining,
                        int(available_notional // (notional_guard_price * lot_size)),
                    )
                    if submit_lots < 1:
                        log(
                            f"лесенка: ступень {stage + 1} @{price} пропущена — "
                            f"лимит позиции {max_position_rub:,.0f} ₽ исчерпан"
                        )
                        break
                request_id = str(uuid.uuid4())
                if submission_intent_callback is not None:
                    # The request id and frozen candidate plan reach durable
                    # storage before the first broker mutation.
                    submission_intent_callback(request_id, stage + 1, price, submit_lots)
                try:
                    order_id, response = self.place_limit(
                        uid,
                        submit_lots,
                        direction,
                        price,
                        confirm_margin_trade=confirm_margin_trade,
                        request_id=request_id,
                    )
                except Exception as exc:
                    if submission_intent_callback is not None:
                        raise OrderSubmissionUncertain(request_id, str(exc)) from exc
                    raise
                log(f"лимит {direction} {submit_lots} лот(ов) @{price} (ступень {stage + 1})")
                state = {"status": response.get("executionReportStatus"),
                         "lots": int(response.get("lotsExecuted") or 0),
                         "avg_price": quotation_to_float(_get_field(response, "executedOrderPrice"))}
                if state["status"] != "EXECUTION_REPORT_STATUS_FILL":
                    state = self._wait_fill(order_id, LIMIT_STAGE_SECONDS)
                if state["status"] != "EXECUTION_REPORT_STATUS_FILL":
                    # A new ladder stage is forbidden until the previous
                    # request is provably terminal.  Otherwise a lost cancel
                    # ACK can leave several full-size margin orders active.
                    self.cancel_order(order_id, suppress_errors=False)
                    state = self.order_state(order_id)
                    if state["status"] not in TERMINAL_ORDER_STATUSES:
                        raise OrderSubmissionUncertain(
                            request_id,
                            f"после CancelOrder request остаётся {state['status']}",
                        )
                executed = min(state["lots"], submit_lots)
                if executed > 0:
                    fills.append((executed, state["avg_price"] or price))
                    remaining -= executed
                    log(f"исполнено {executed} лот(ов) @{state['avg_price'] or price}, осталось {remaining}")
                    if submission_intent_callback is not None:
                        # Protect a partial entry immediately.  Starting another
                        # stage before journaling/protection would make recovery
                        # ambiguous if its ACK were then lost.
                        break
            except OrderSubmissionUncertain:
                # The broker may have accepted this exact request.  Continuing
                # the ladder could create a duplicate position.
                raise
            except Exception as exc:  # noqa: BLE001
                if submission_intent_callback is not None and request_id is not None:
                    # Any failure after a recorded PostOrder intent is treated
                    # as uncertain until the next tick reconciles request_id.
                    raise OrderSubmissionUncertain(request_id, str(exc)) from exc
                # ступень сломалась (заявка отклонена, сеть моргнула) — не бросаем
                # всю лесенку: для закрытия обязан остаться шанс дойти до market
                log(f"ступень {stage + 1} не удалась: {exc}")

        if remaining > 0 and must_fill:
            log(f"лесенка не добрала {remaining} лот(ов) — закрываю рыночным")
            request_id = str(uuid.uuid4())
            if submission_intent_callback is not None:
                submission_intent_callback(
                    request_id, 4, reference_price, remaining
                )
            try:
                response = self.market_order(
                    uid, remaining, direction, request_id=request_id
                )
            except Exception as exc:
                if submission_intent_callback is not None:
                    raise OrderSubmissionUncertain(request_id, str(exc)) from exc
                raise
            status = response.get("executionReportStatus")
            response_lots = min(
                max(int(response.get("lotsExecuted") or 0), 0), remaining
            )
            price = (
                quotation_to_float(_get_field(response, "executedOrderPrice"))
                or reference_price
            )
            executed = response_lots
            if status == "EXECUTION_REPORT_STATUS_FILL":
                # FILL is the broker's terminal acknowledgement for the whole
                # market request.  Some API fixtures omit lotsExecuted, so the
                # requested remainder is the only safe interpretation here.
                executed = response_lots or remaining
            elif status == "EXECUTION_REPORT_STATUS_PARTIALLYFILL":
                # PARTIALLYFILL is not a terminal state.  Never report the
                # whole remainder as filled and never restore protection while
                # an order can still over-close the position.
                try:
                    state = self.order_state(request_id)
                    if state["status"] not in TERMINAL_ORDER_STATUSES:
                        self.cancel_order(request_id, suppress_errors=False)
                        state = self.order_state(request_id)
                except Exception as exc:
                    if submission_intent_callback is not None:
                        raise OrderSubmissionUncertain(request_id, str(exc)) from exc
                    raise
                if state["status"] not in TERMINAL_ORDER_STATUSES:
                    raise OrderSubmissionUncertain(
                        request_id,
                        f"рыночный request остаётся {state['status']}",
                    )
                executed = min(
                    max(int(state.get("lots") or response_lots), 0), remaining
                )
                price = state.get("avg_price") or price
            if executed > 0:
                fills.append((executed, price))
                remaining -= executed
                log(
                    f"рыночным исполнено {executed} лот(ов) "
                    f"@{price}, осталось {remaining}"
                )

        total = sum(qty for qty, _ in fills)
        avg = sum(qty * px for qty, px in fills) / total if total else 0.0
        return total, avg

    def market_order(
        self,
        uid: str,
        lots: int,
        direction: str,
        *,
        request_id: str | None = None,
    ) -> dict:
        payload = {
            "accountId": self.account_id,
            "instrumentId": uid,
            "quantity": str(lots),
            "direction": f"ORDER_DIRECTION_{direction.upper()}",
            "orderType": "ORDER_TYPE_MARKET",
            "orderId": request_id or str(uuid.uuid4()),
        }
        if not self.live:
            log(f"DRY-RUN market order: {payload}")
            return {"dry_run": True}
        response = self._post("OrdersService/PostOrder", payload)
        status = response.get("executionReportStatus")
        executed = quotation_to_float(_get_field(response, "executedOrderPrice"))
        log(f"market {direction} {lots} лот(ов): статус {status}, средняя цена {executed}")
        if status not in ("EXECUTION_REPORT_STATUS_FILL", "EXECUTION_REPORT_STATUS_PARTIALLYFILL"):
            raise RuntimeError(f"Заявка не исполнена: {response}")
        return response

    def stop_order(self, uid: str, lots: int, direction: str, stop_price: float, kind: str) -> str | None:
        payload = {
            "accountId": self.account_id,
            "instrumentId": uid,
            "quantity": str(lots),
            "direction": f"STOP_ORDER_DIRECTION_{direction.upper()}",
            "expirationType": "STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL",
            "stopOrderType": f"STOP_ORDER_TYPE_{kind}",
            "exchangeOrderType": "EXCHANGE_ORDER_TYPE_MARKET",
            "stopPrice": float_to_quotation(stop_price),
        }
        if not self.live:
            log(f"DRY-RUN stop order ({kind}): {payload}")
            return None
        response = self._post("StopOrdersService/PostStopOrder", payload)
        stop_id = response.get("stopOrderId")
        log(f"стоп-заявка {kind} @{stop_price} выставлена: {stop_id}")
        return stop_id

    def cancel_stop_orders(self) -> int:
        cancelled = 0
        for order in self.stop_orders():
            stop_id = order.get("stopOrderId")
            if not self.live:
                log(f"DRY-RUN cancel stop order {stop_id}")
                continue
            self._post("StopOrdersService/CancelStopOrder", {"accountId": self.account_id, "stopOrderId": stop_id})
            cancelled += 1
            log(f"снята стоп-заявка {stop_id}")
        return cancelled


# ---------- команды ----------

def cmd_status(bot: TradeBot) -> int:
    log(f"счёт «{DEFAULT_ACCOUNT_NAME}» id={bot.account_id} режим={'LIVE' if bot.live else 'DRY-RUN'}")
    log(f"кэш: {bot.cash_rub():,.2f} ₽")
    positions = bot.share_positions()
    if positions:
        for p in positions:
            qty = quotation_to_float(_get_field(p, "quantity"))
            avg = quotation_to_float(_get_field(p, "averagePositionPrice"))
            cur = quotation_to_float(_get_field(p, "currentPrice"))
            log(f"позиция: figi={p.get('figi')} qty={qty} средняя={avg} текущая={cur}")
    else:
        log("открытых позиций нет")
    orders = bot.stop_orders()
    if orders:
        for o in orders:
            log(f"стоп-заявка: {o.get('stopOrderType')} @{quotation_to_float(_get_field(o, 'stopPrice'))} id={o.get('stopOrderId')}")
    else:
        log("активных стоп-заявок нет")
    if os.path.isfile(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as handle:
            log(f"state: {handle.read().strip()}")
    return 0


def read_watchlist(path: str | None) -> str:
    candidate = path or os.path.join(PROJECT_DIR, "data/watchlists/watchlist.txt")
    with open(candidate, "r", encoding="utf-8") as handle:
        return handle.read()


def _rs5_candidate_gate_reason(
    item: wbt.IdeaAnalysis,
    trade_date: date,
) -> str | None:
    """Mirror the pre-broker Engine-A gates for an RS5 top-3 candidate."""
    if item.skipped_reason:
        return f"анализ: {item.skipped_reason}"
    if item.direction not in {"long", "short"}:
        return f"неверное направление: {item.direction}"
    if DAY_FILTER:
        threshold = wbt.runner_day_confidence_threshold(item.direction)
        if (
            item.runner_day_prob is not None
            and threshold is not None
            and item.runner_day_prob < threshold
        ):
            return f"фильтр дня {item.runner_day_prob:.2f} < {threshold:.2f}"
    if REGIME_GATES:
        if item.direction == "short" and item.market_bullish:
            return "режим: short при индексе выше EMA20"
        if item.direction == "long" and not item.market_bullish:
            return "режим: long при индексе ниже EMA20"
    rally = getattr(item, "short_rally_10d_pct", None)
    if item.direction == "short" and SHORT_RALLY_GUARD_PCT > 0 and rally is not None:
        if rally > SHORT_RALLY_GUARD_PCT:
            return f"rally guard: +{rally:.2f}% > {SHORT_RALLY_GUARD_PCT:.2f}%"
    if item.exit_target is None:
        return "нет цели выхода"
    if MIN_TARGET_PCT > 0 and item.exit_target.move_pct_from_close < MIN_TARGET_PCT:
        return (
            f"цель {item.exit_target.move_pct_from_close:.2f}% < "
            f"RR-floor {MIN_TARGET_PCT:.2f}%"
        )
    return None


def _shadow_number(value: object) -> float | None:
    """JSON-safe finite number for the broker-free forward sidecar."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _forward_shadow_candidate_snapshot(
    item: wbt.IdeaAnalysis,
    rank: int,
) -> dict:
    """Copy primitive point-in-time fields without retaining/mutating analyses."""
    target = item.exit_target
    return {
        "rank": rank,
        "symbol": item.symbol,
        "direction": item.direction,
        "overall_score": _shadow_number(item.overall_score),
        "vol_expansion": _shadow_number(getattr(item, "vol_expansion", None)),
        "directional_rs5_pp": _shadow_number(
            getattr(item, "directional_rs5_pp", None)
        ),
        "target_label": target.label if target is not None else None,
        "target_price": _shadow_number(target.price) if target is not None else None,
        "target_move_pct": (
            _shadow_number(target.move_pct_from_close) if target is not None else None
        ),
        "regime": getattr(item, "regime", None),
        "market_bullish": bool(getattr(item, "market_bullish", False)),
        "short_rally_10d_pct": _shadow_number(
            getattr(item, "short_rally_10d_pct", None)
        ),
        "runner_day_prob": _shadow_number(getattr(item, "runner_day_prob", None)),
        "skipped_reason": item.skipped_reason,
    }


def _record_forward_selector_shadow(
    *,
    analyses: list[wbt.IdeaAnalysis],
    trade_date: date,
    control: wbt.IdeaAnalysis,
    legacy_top: wbt.IdeaAnalysis,
    watchlist_text: str,
    control_plan: dict,
    rs5_report: dict,
) -> None:
    """Write a pure top-6 challenger decision; never return a live candidate."""
    if TARGET_DISTANCE_MULTIPLIER != 1.0:
        raise RuntimeError("registered forward control uses absolute T4; target125 needs a new registration")
    # Lazy import is intentional: telemetry cannot become an import-time
    # dependency of order execution.
    from argonus.shadow import forward_shadow_research as forward_shadow

    manifest = forward_shadow.load_manifest(FORWARD_SHADOW_MANIFEST_PATH)
    registered_floor = float(
        manifest["policies"]["selector"]["directional_rs5_floor_pp"]
    )
    if abs(registered_floor - RS5_SELECTOR_THRESHOLD) > 1e-12:
        raise RuntimeError(
            "forward selector RS5 floor does not match the live selector: "
            f"{registered_floor} != {RS5_SELECTOR_THRESHOLD}"
        )

    snapshots = [
        _forward_shadow_candidate_snapshot(item, rank)
        for rank, item in enumerate(list(analyses)[:6], start=1)
    ]
    reasons: dict[int, str | None] = {}
    for rank, item in enumerate(list(analyses)[:6], start=1):
        reason = _rs5_candidate_gate_reason(item, trade_date)
        # The actual A+RS5 control already passed its live gates and complete
        # broker preflight.  Alternatives must additionally satisfy the exact
        # pre-registered RS5 floor used by the historical selector audit.
        if item.symbol == control.symbol:
            reason = None
        elif reason is None:
            rs5_value = _shadow_number(getattr(item, "directional_rs5_pp", None))
            if rs5_value is None:
                reason = "RS5 недоступен"
            elif rs5_value < registered_floor:
                reason = f"RS5 {rs5_value:+.2f} < {registered_floor:+.2f}"
        reasons[rank] = reason

    info = control_plan.get("info") or {}
    evidence = {
        "captured_before_order": True,
        "broker_validation": "existing_control_preflight_only",
        "challenger_broker_calls_pre_order": 0,
        "watchlist_sha256": hashlib.sha256(
            watchlist_text.encode("utf-8")
        ).hexdigest(),
        "generator_audit_sha256": rs5_shadow.sha256_file(
            os.path.join(PROJECT_DIR, "data/watchlists/watchlist_candidates.json")
        ),
        "analyzer_sha256": rs5_shadow.sha256_file(
            os.path.join(PROJECT_DIR, "argonus/watchlists/watchlist_best_target.py")
        ),
        "rs5_decision_id": rs5_report.get("decision_id"),
        "rs5_resolution_id": rs5_report.get("activation_resolution_id"),
        "rs5_activation_manifest_sha256": rs5_report.get(
            "activation_manifest_sha256"
        ),
        "point_in_time_candidates": snapshots,
        "control_preflight": {
            "symbol": control.symbol,
            "ready": bool(control_plan.get("ready")),
            "price": _shadow_number(control_plan.get("price")),
            "target_price": _shadow_number(control_plan.get("target_price")),
            "lots": control_plan.get("lots"),
            "lot_size": info.get("lot"),
            "min_price_increment": _shadow_number(
                info.get("min_price_increment")
            ),
            "api_trade_available": control_plan.get("api_trade_available"),
            "short_enabled": control_plan.get("short_enabled"),
            "trading_status": control_plan.get("trading_status"),
            "broker_max_lots": control_plan.get("broker_max_lots"),
        },
    }
    report = forward_shadow.evaluate_max_vol_expansion_top6(
        snapshots,
        trade_date=trade_date,
        eligibility_reasons=reasons,
        control_selected_symbol=control.symbol,
        legacy_rank1_symbol=legacy_top.symbol,
        manifest=manifest,
        optional_evidence=evidence,
    )
    path = forward_shadow.write_first_writer_wins(
        report,
        FORWARD_SHADOW_DIR,
        manifest=manifest,
    )
    log(
        "FORWARD SHADOW selector: "
        f"control={control.symbol}, challenger={report.get('recommended_symbol')}, "
        f"divergence={report.get('divergence')}, audit={path}"
    )


def _record_forward_exit_shadow(
    bot: TradeBot,
    state: dict,
    *,
    entry_request_id: str | None = None,
    entry_execution_time: str | None = None,
) -> None:
    """Freeze the exit challenger only after the real STOP is protected."""
    from argonus.shadow import forward_shadow_research as forward_shadow

    if state.get("engine") != "A":
        return
    if state.get("target_distance_multiplier", 1.0) != 1.0:
        raise RuntimeError("registered forward control uses absolute T4; target125 needs a new registration")
    if not state.get("stop_order_id"):
        raise RuntimeError("forward exit capture requires a confirmed STOP order")
    selector_path = os.path.join(
        FORWARD_SHADOW_DIR,
        forward_shadow.SELECTOR_POLICY,
        "selector_shadow_decision",
        f"{state.get('date')}.json",
    )
    try:
        with open(selector_path, "r", encoding="utf-8") as handle:
            selector_plan = json.load(handle)
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            "forward exit capture requires the same day's immutable selector decision"
        ) from exc
    if selector_plan.get("control_selected_symbol") != state.get("symbol"):
        raise RuntimeError(
            "forward selector control does not match the filled symbol: "
            f"{selector_plan.get('control_selected_symbol')} != {state.get('symbol')}"
        )
    broker_execution_time = entry_execution_time
    execution_time_source = "already_observed_order_state"
    if broker_execution_time is None and entry_request_id:
        # This read-only call deliberately happens after STOP/TAKE handling.
        # GetOrderState stages carry the exchange fill time and cannot mutate
        # an order.  Failure merely drops this research record.
        order = bot.order_state(entry_request_id)
        broker_execution_time = order.get("execution_time")
        execution_time_source = "post_protection_GetOrderState.stages[-1]"
    if not broker_execution_time:
        raise RuntimeError(
            "forward exit capture requires broker stages[].executionTime; "
            "local observation time is not accepted as a fill timestamp"
        )
    if EXIT_TIME != dtime(18, 35):
        raise RuntimeError(
            "forward exit control requires BOT_EXIT_TIME=18:35; "
            f"runtime is {EXIT_TIME.strftime('%H:%M')}"
        )
    stop_pct = float(state.get("stop_pct") or wbt.STOP_LOSS_PCT)
    if abs(stop_pct - 1.0) > 1e-12:
        raise RuntimeError(f"forward exit control requires stop 1.0%, got {stop_pct}")

    target_price = state.get("planned_target_price")
    entry_price = state.get("entry_price")
    if target_price is None or entry_price is None:
        raise RuntimeError("forward exit capture requires actual entry and frozen target")
    fee_side = _shadow_number(state.get("estimated_fee_side_pct"))
    assumed_round_trip_cost = 2.0 * fee_side if fee_side is not None else 0.08
    manifest = forward_shadow.load_manifest(FORWARD_SHADOW_MANIFEST_PATH)
    evidence = {
        "actual_fill_authoritative": True,
        "entry_request_id": entry_request_id,
        "broker_execution_time_source": execution_time_source,
        "filled_lots": state.get("lots"),
        "lot_size": state.get("lot_size"),
        "min_price_increment": state.get("increment"),
        "actual_stop_price": state.get("stop_price"),
        "stop_order_id": state.get("stop_order_id"),
        "take_order_id": state.get("take_order_id"),
        "protection_status": state.get("protection_status"),
        "protection_error": state.get("protection_error"),
        "target_guard_failed": bool(state.get("target_guard_failed")),
        "live_exit_time": EXIT_TIME.strftime("%H:%M"),
        "selector_shadow_decision_id": selector_plan.get("decision_id"),
        "selector_decision_id": state.get("selector_decision_id"),
        "selector_resolution_id": state.get("selector_resolution_id"),
    }
    plan = forward_shadow.build_exit_shadow_plan(
        trade_date=state["date"],
        symbol=state["symbol"],
        direction=state["direction"],
        target_price=float(target_price),
        entry_price=float(entry_price),
        actual_entry_timestamp=broker_execution_time,
        assumed_round_trip_cost_pct=assumed_round_trip_cost,
        manifest=manifest,
        optional_evidence=evidence,
    )
    path = forward_shadow.write_first_writer_wins(
        plan,
        FORWARD_SHADOW_DIR,
        manifest=manifest,
    )
    log(f"FORWARD SHADOW exit: plan={plan['plan_id']}, audit={path}")


ENTRY_REQUIRED_RUNTIME_ARTIFACTS = {
    "argonus/paths.py", "argonus/serialization.py",
    "argonus/trading/trade_bot.py",
    "scripts/run_tick.sh",
    "argonus/trading/production_entry_0705.py",
    "argonus/trading/production_profit_target.py",
    "config/profit_target_activation_manifest.json",
    "data/backtests/ENTRY_TIMING_RESEARCH_2026-07-18.md",
}


def entry_runtime_config() -> dict:
    return {
        "entry_start": ENTRY_START.strftime("%H:%M"),
        "entry_deadline": ENTRY_DEADLINE.strftime("%H:%M"),
        "entry_execution_policy": ENTRY_EXECUTION_POLICY,
        "entry_book_depth": ENTRY_BOOK_DEPTH,
        "entry_max_quote_age_ms": ENTRY_MAX_QUOTE_AGE_MS,
        "entry_max_impact_bps": ENTRY_MAX_IMPACT_BPS,
        "entry_time_in_force": entry0705.TIME_IN_FORCE,
        "target_position_rub": TARGET_POSITION_RUB,
        "max_leverage": MAX_LEVERAGE,
        "stop_loss_pct": float(wbt.STOP_LOSS_PCT),
        "exit_time": EXIT_TIME.strftime("%H:%M"),
    }


def profit_target_runtime_config() -> dict:
    return {
        **entry_runtime_config(),
        "target_distance_multiplier": TARGET_DISTANCE_MULTIPLIER,
        "day_filter": DAY_FILTER,
        "regime_gates": REGIME_GATES,
        "short_rally_guard_pct": SHORT_RALLY_GUARD_PCT,
        "minimum_target_pct": MIN_TARGET_PCT,
        "long_risk_multiplier": LONG_RISK_MULTIPLIER,
        "risk_pct": RISK_PCT,
        "second_engine": SECOND_ENGINE,
        "confidence_sizing": CONF_SIZING,
        "pullback_pct": PULLBACK_PCT,
        "rs5_selector": RS5_ACTIVATION_REQUESTED,
        "rs5_threshold_pp": RS5_SELECTOR_THRESHOLD,
    }


def validate_profit_target_activation_manifest(
    manifest_path: str = TARGET_ACTIVATION_MANIFEST_PATH, *, trade_date: date | None = None,
) -> tuple[bool, str, str | None]:
    return profit_target.validate_activation(
        manifest_path, project_dir=PROJECT_DIR,
        runtime_config=profit_target_runtime_config(), trade_date=trade_date or date.today(),
    )


def validate_entry_activation_manifest(
    manifest_path: str = ENTRY_ACTIVATION_MANIFEST_PATH,
    *,
    trade_date: date | None = None,
) -> tuple[bool, str, str | None]:
    """Pin the explicit override, execution code and live entry parameters."""
    if not manifest_path or not os.path.isfile(manifest_path):
        return False, "entry activation manifest отсутствует", None
    try:
        with open(manifest_path, "rb") as handle:
            raw = handle.read()
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, f"entry manifest не читается: {exc}", None
    if not isinstance(manifest, dict):
        return False, "entry manifest должен быть JSON object", None

    def invalid(reason: str) -> tuple[bool, str, str | None]:
        return False, reason, None

    if manifest.get("schema_version") != 1:
        return invalid("entry manifest schema_version не поддерживается")
    if manifest.get("policy") != entry0705.POLICY or manifest.get("approved") is not True:
        return invalid("entry manifest policy/approved не совпадает")
    if manifest.get("production_activation_allowed") is not True:
        return invalid("entry manifest запрещает production activation")
    try:
        effective = date.fromisoformat(str(manifest.get("effective_from")))
    except ValueError:
        return invalid("entry manifest effective_from имеет неверный формат")
    if (trade_date or date.today()) < effective:
        return invalid(f"entry engine действует не раньше {effective.isoformat()}")

    evidence = manifest.get("activation_evidence")
    if not isinstance(evidence, dict):
        return invalid("entry activation_evidence отсутствует")
    explicit_override = (
        evidence.get("status") == "explicit_user_override"
        and evidence.get("risk_acknowledged") is True
        and bool(evidence.get("approved_by"))
        and bool(evidence.get("approval_date"))
        and evidence.get("forward_gate_passed") is False
    )
    if not explicit_override:
        return invalid("entry explicit override не зафиксирован честно")
    source = manifest.get("source_research") or {}
    if source.get("production_verdict") != "NO_GO":
        return invalid("entry manifest скрывает NO-GO исходного исследования")

    artifacts = manifest.get("runtime_artifacts")
    if not isinstance(artifacts, dict):
        return invalid("entry runtime_artifacts должен быть object")
    missing = ENTRY_REQUIRED_RUNTIME_ARTIFACTS - set(artifacts)
    if missing:
        return invalid(
            "entry runtime_artifacts не содержит pins: " + ", ".join(sorted(missing))
        )
    project_real = os.path.realpath(PROJECT_DIR)
    for relative_path, expected_hash in artifacts.items():
        candidate = os.path.realpath(os.path.join(PROJECT_DIR, str(relative_path)))
        try:
            inside = os.path.commonpath((project_real, candidate)) == project_real
        except ValueError:
            inside = False
        if not inside:
            return invalid(f"entry runtime artifact вне проекта: {relative_path}")
        if not expected_hash or rs5_shadow.sha256_file(candidate) != expected_hash:
            return invalid(f"entry runtime artifact {relative_path} не совпадает")

    required_config = manifest.get("required_config")
    actual_config = entry_runtime_config()
    if not isinstance(required_config, dict):
        return invalid("entry required_config должен быть object")
    if set(required_config) != set(actual_config):
        return invalid("entry required_config имеет неполный или лишний набор keys")
    for key, expected in required_config.items():
        if actual_config.get(key) != expected:
            return invalid(
                f"entry config {key}={actual_config.get(key)!r}, требуется {expected!r}"
            )
    return True, "valid_explicit_user_override", hashlib.sha256(raw).hexdigest()


def _rs5_forward_evidence_is_approved(evidence: object) -> tuple[bool, str]:
    """Validate either the documented forward gate or an explicit user override."""
    if not isinstance(evidence, dict):
        return False, "forward_evidence отсутствует"
    try:
        gate_passed = (
            int(evidence.get("baseline_eligible_days", 0)) >= 30
            and int(evidence.get("divergences", 0)) >= 8
            and float(evidence.get("paired_net_delta_after_costs_pp", 0.0)) > 0.0
            and evidence.get("forward_mdd_not_worse") is True
            and evidence.get("single_replacement_dominated") is False
        )
    except (TypeError, ValueError):
        gate_passed = False
    if gate_passed:
        return True, "forward_gate_passed"

    # The user explicitly asked to activate before the forward sample reached
    # the research gate.  This is allowed only when the manifest records that
    # exception honestly; it must never masquerade as completed evidence.
    explicit_override = (
        evidence.get("status") == "explicit_user_override"
        and evidence.get("risk_acknowledged") is True
        and bool(evidence.get("approved_by"))
        and bool(evidence.get("approval_date"))
    )
    if explicit_override:
        return True, "explicit_user_override"
    return False, "forward_evidence gate не пройден и override не зафиксирован"


def _validate_rs5_activation_manifest(
    manifest_path: str,
    report: dict,
) -> tuple[bool, str, str, dict | None]:
    """Validate policy, provenance, optional runtime pins, and effective date."""
    if not manifest_path or not os.path.isfile(manifest_path):
        return False, "absent", "activation manifest отсутствует", None
    try:
        with open(manifest_path, "rb") as handle:
            raw = handle.read()
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, "invalid", f"manifest не читается: {exc}", None
    if not isinstance(manifest, dict):
        return False, "invalid", "manifest должен быть JSON object", None

    def invalid(reason: str) -> tuple[bool, str, str, dict]:
        return False, "invalid", reason, manifest

    if manifest.get("schema_version") != 1:
        return invalid("неподдерживаемая schema_version")
    if manifest.get("policy") != "directional_rs5_top3":
        return invalid("неверный policy")
    if manifest.get("approved") is not True:
        return invalid("manifest не approved")
    if manifest.get("feature") != report.get("feature"):
        return invalid("feature не совпадает")
    try:
        threshold_matches = math.isclose(
            float(manifest.get("threshold_pp")),
            float(report.get("threshold_pp")),
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        top_k_matches = int(manifest.get("top_k")) == int(report.get("top_k"))
    except (TypeError, ValueError):
        return invalid("threshold/top_k имеют неверный тип")
    if not threshold_matches or not top_k_matches:
        return invalid("threshold/top_k не совпадают")

    effective_from = manifest.get("effective_from")
    if not effective_from:
        return invalid("effective_from обязателен")
    try:
        if date.fromisoformat(str(report.get("trade_date"))) < date.fromisoformat(str(effective_from)):
            return False, "not_effective_yet", f"effective_from={effective_from}", manifest
    except ValueError:
        return invalid("effective_from имеет неверный формат")

    provenance = report.get("provenance") or {}
    artifacts = manifest.get("artifacts") or {}
    for key in ("analyzer", "selector"):
        expected = artifacts.get(key) or {}
        actual = provenance.get(key) or {}
        if (
            expected.get("name") != actual.get("name")
            or expected.get("sha256") != actual.get("sha256")
            or not expected.get("sha256")
        ):
            return invalid(f"artifact {key} не совпадает")
    expected_models = artifacts.get("models") or {}
    actual_models = provenance.get("models") or {}
    if set(expected_models) != set(actual_models):
        return invalid("набор model artifacts не совпадает")
    for name, expected in expected_models.items():
        actual = actual_models.get(name) or {}
        if (
            expected.get("name") != actual.get("name")
            or expected.get("sha256") != actual.get("sha256")
            or not expected.get("sha256")
        ):
            return invalid(f"model artifact {name} не совпадает")

    # Optional release pins cover the production orchestrator and research
    # record as well as the decision artifacts embedded in the report.
    runtime_artifacts = manifest.get("runtime_artifacts") or {}
    if not isinstance(runtime_artifacts, dict):
        return invalid("runtime_artifacts должен быть object")
    missing_runtime = RS5_REQUIRED_RUNTIME_ARTIFACTS - set(runtime_artifacts)
    if missing_runtime:
        return invalid(
            "runtime_artifacts не содержит обязательные pins: "
            + ", ".join(sorted(missing_runtime))
        )
    project_real = os.path.realpath(PROJECT_DIR)
    for relative_path, expected_hash in runtime_artifacts.items():
        candidate_path = os.path.realpath(os.path.join(PROJECT_DIR, str(relative_path)))
        try:
            inside_project = os.path.commonpath((project_real, candidate_path)) == project_real
        except ValueError:
            inside_project = False
        if not inside_project:
            return invalid(f"runtime artifact вне проекта: {relative_path}")
        actual_hash = rs5_shadow.sha256_file(candidate_path)
        if not expected_hash or actual_hash != expected_hash:
            return invalid(f"runtime artifact {relative_path} не совпадает")

    required_config = manifest.get("required_config") or {}
    runtime_config = report.get("runtime_config") or {}
    if not isinstance(required_config, dict):
        return invalid("required_config должен быть object")
    missing_config = RS5_REQUIRED_CONFIG_KEYS - set(required_config)
    if missing_config:
        return invalid(
            "required_config не содержит обязательные keys: "
            + ", ".join(sorted(missing_config))
        )
    for key, expected in required_config.items():
        if runtime_config.get(key) != expected:
            return invalid(
                f"config {key}={runtime_config.get(key)!r}, требуется {expected!r}"
            )

    evidence_ok, evidence_status = _rs5_forward_evidence_is_approved(
        manifest.get("forward_evidence")
    )
    if not evidence_ok:
        return invalid(evidence_status)
    manifest["_sha256"] = hashlib.sha256(raw).hexdigest()
    manifest["_evidence_status"] = evidence_status
    return True, "valid", evidence_status, manifest


def _stamp_rs5_activation_resolution(report: dict) -> str:
    """Bind the immutable identity to release, final symbol, and entry preflight."""
    resolution_basis = {
        "decision_id": report.get("decision_id"),
        "manifest_sha256": report.get("activation_manifest_sha256"),
        "manifest_status": report.get("activation_manifest_status"),
        "mode": report.get("mode"),
        "decision": report.get("decision"),
        "live_selected_symbol": report.get("live_selected_symbol"),
        "activation_applied": report.get("activation_applied"),
        "fallback_applied": report.get("activation_fallback_applied"),
        "fallback_reason": report.get("activation_fallback_reason"),
        "final_preflight": report.get("final_preflight"),
    }
    canonical = json.dumps(
        resolution_basis,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    value = hashlib.sha256(canonical).hexdigest()
    report["activation_resolution_id"] = value
    return value


def resolve_rs5_activation(
    analyses: list[wbt.IdeaAnalysis],
    report: dict,
    *,
    activation_requested: bool,
    activation_manifest_path: str,
    candidate_info_provider,
) -> tuple[wbt.IdeaAnalysis | None, bool, dict]:
    """Resolve RS5 to a live candidate without performing any broker mutation.

    ``candidate_info_provider`` is invoked only for a replacement and must be a
    read-only preflight.  Once this function returns a candidate, callers must
    freeze it: there is deliberately no fallback after an order attempt.
    """
    resolved = dict(report)
    legacy = analyses[0] if analyses else None
    resolved.update(
        {
            "activation_requested": bool(activation_requested),
            "activation_available": False,
            "activation_applied": False,
            "activation_fallback_applied": False,
            "activation_manifest_status": "not_requested",
            "mode": "shadow",
            "live_selected_symbol": legacy.symbol if legacy is not None else None,
            "live_selected_rank": 1 if legacy is not None else None,
        }
    )

    def finish(
        selected: wbt.IdeaAnalysis | None,
        skip: bool,
    ) -> tuple[wbt.IdeaAnalysis | None, bool, dict]:
        _stamp_rs5_activation_resolution(resolved)
        return selected, skip, resolved

    if not activation_requested or legacy is None:
        return finish(legacy, False)

    valid, status, reason, manifest = _validate_rs5_activation_manifest(
        activation_manifest_path, resolved
    )
    resolved["activation_manifest_status"] = status
    resolved["activation_manifest_reason"] = reason
    resolved["activation_manifest_path"] = os.path.abspath(activation_manifest_path)
    if manifest and manifest.get("_sha256"):
        resolved["activation_manifest_sha256"] = manifest["_sha256"]
        resolved["activation_evidence_status"] = manifest.get("_evidence_status")
    if not valid:
        resolved["activation_blocked_reason"] = reason
        return finish(legacy, False)

    resolved.pop("activation_blocked_reason", None)
    resolved.pop("activation_resolution_pending", None)
    resolved["activation_available"] = True
    resolved["mode"] = "active"
    decision = resolved.get("decision")
    if decision == "skip":
        resolved["activation_applied"] = True
        resolved["live_selected_symbol"] = None
        resolved["live_selected_rank"] = None
        return finish(None, True)
    if decision == "keep":
        resolved["activation_applied"] = True
        return finish(legacy, False)
    if decision != "replacement":
        resolved["activation_blocked_reason"] = f"decision={decision} не меняет legacy path"
        return finish(legacy, False)

    try:
        recommended_rank = int(resolved.get("recommended_rank"))
    except (TypeError, ValueError):
        recommended_rank = 0
    if recommended_rank < 2 or recommended_rank > len(analyses):
        resolved["activation_fallback_applied"] = True
        resolved["activation_fallback_reason"] = "recommended rank отсутствует в analyses"
        return finish(legacy, False)
    replacement = analyses[recommended_rank - 1]
    if replacement.symbol != resolved.get("recommended_symbol"):
        resolved["activation_fallback_applied"] = True
        resolved["activation_fallback_reason"] = "recommended symbol/rank mismatch"
        return finish(legacy, False)

    try:
        metadata = candidate_info_provider(replacement)
    except Exception as exc:  # noqa: BLE001 - read-only preflight falls back safely
        metadata = None
        provider_error = str(exc)
    else:
        provider_error = ""
    rejection_reason = None
    if metadata is None:
        rejection_reason = f"replacement broker metadata отсутствует{': ' + provider_error if provider_error else ''}"
    elif metadata.get("ready") is False or metadata.get("ok") is False:
        rejection_reason = str(metadata.get("reason") or "replacement preflight rejected")
    elif metadata.get("api_trade_available") is not True:
        rejection_reason = "replacement api_trade_available=false"
    elif replacement.direction == "short" and metadata.get("short_enabled") is not True:
        rejection_reason = "replacement short_enabled=false"

    if rejection_reason:
        resolved["activation_applied"] = False
        resolved["activation_fallback_applied"] = True
        resolved["activation_fallback_reason"] = rejection_reason
        resolved["live_selected_symbol"] = legacy.symbol
        resolved["live_selected_rank"] = 1
        return finish(legacy, False)

    resolved["activation_applied"] = True
    resolved["live_selected_symbol"] = replacement.symbol
    resolved["live_selected_rank"] = recommended_rank
    return finish(replacement, False)


def run_rs5_shadow_selector(
    analyses: list[wbt.IdeaAnalysis],
    trade_date: date,
    *,
    force: bool = False,
    selector_enabled: bool | None = None,
    threshold_pp: float | None = None,
    output_dir: str | None = None,
    watchlist_text: str | None = None,
    persist: bool = True,
) -> dict:
    """Build the causal RS5 report; optional persistence stays broker-agnostic."""
    if not analyses:
        return {}
    enabled = RS5_ACTIVATION_REQUESTED if selector_enabled is None else selector_enabled
    threshold = RS5_SELECTOR_THRESHOLD if threshold_pp is None else threshold_pp
    directory = RS5_SHADOW_DIR if output_dir is None else output_dir
    top_items = analyses[: rs5_shadow.DEFAULT_TOP_K]
    precomputed_metrics_by_rank: dict[int, dict] = {}
    data_errors: dict[str, str] = {}
    for rank, item in enumerate(top_items, start=1):
        aligned_rs5 = getattr(item, "directional_rs5_pp", None)
        stock_return = getattr(item, "rs5_stock_return_5d_pct", None)
        index_return = getattr(item, "rs5_index_return_5d_pct", None)
        stock_window = getattr(item, "rs5_stock_window", None)
        index_window = getattr(item, "rs5_index_window", None)
        if (
            aligned_rs5 is None
            or stock_return is None
            or index_return is None
            or stock_window is None
            or index_window is None
        ):
            data_errors[f"rank_{rank}_{item.symbol}"] = "precomputed RS5 unavailable"
            continue
        precomputed_metrics_by_rank[rank] = {
            "stock_return_5d_pct": stock_return,
            "index_return_5d_pct": index_return,
            "aligned_rs5_pp": aligned_rs5,
            "stock_window": list(stock_window),
            "index_window": list(index_window),
        }

    eligibility_reasons = {
        rank: _rs5_candidate_gate_reason(item, trade_date)
        for rank, item in enumerate(top_items, start=1)
    }
    report = rs5_shadow.evaluate_rs5_policy(
        top_items,
        trade_date,
        {},
        (),
        eligibility_reasons,
        threshold_pp=threshold,
        precomputed_metrics_by_rank=precomputed_metrics_by_rank,
    )
    report.update(
        {
            "capture_mode": rs5_shadow.capture_mode_for(trade_date),
            "activation_requested": bool(enabled),
            "activation_available": False,
            "activation_manifest_status": "not_resolved",
            "activation_suppressed_by_force": bool(force and enabled),
            "activation_applied": False,
            "mode": "shadow",
            "runtime_config": {
                "day_filter": DAY_FILTER,
                "regime_gates": REGIME_GATES,
                "short_rally_guard_pct": SHORT_RALLY_GUARD_PCT,
                "minimum_target_pct": MIN_TARGET_PCT,
                "long_risk_multiplier": LONG_RISK_MULTIPLIER,
                "risk_pct": RISK_PCT,
                "second_engine": SECOND_ENGINE,
                "confidence_sizing": CONF_SIZING,
                "pullback_pct": PULLBACK_PCT,
                "rs5_threshold_pp": threshold,
                "target_position_rub": TARGET_POSITION_RUB,
                "max_leverage": MAX_LEVERAGE,
                "entry_start": ENTRY_START.strftime("%H:%M"),
                "entry_deadline": ENTRY_DEADLINE.strftime("%H:%M"),
                "entry_execution_policy": ENTRY_EXECUTION_POLICY,
                "entry_book_depth": ENTRY_BOOK_DEPTH,
                "entry_max_quote_age_ms": ENTRY_MAX_QUOTE_AGE_MS,
                "entry_max_impact_bps": ENTRY_MAX_IMPACT_BPS,
                "entry_time_in_force": entry0705.TIME_IN_FORCE,
            },
            "data_errors": data_errors,
            "vetoed_current_leader": bool(
                report.get("candidates")
                and report["candidates"][0].get("passes_existing_gates")
                and not report["candidates"][0].get("passes_rs5")
            ),
        }
    )
    current = analyses[0]
    if enabled:
        report["activation_resolution_pending"] = True
        report["activation_blocked_reason"] = (
            "activation manifest and broker preflight have not been resolved by cmd_enter"
        )
    report["live_selected_symbol"] = current.symbol
    model_paths = {
        filename: str(MODEL_DIR / filename)
        for filename in (
            wbt.SAME_DAY_TOP_RERANKER_MODEL_FILENAME,
            wbt.SAME_DAY_TOP_WINNER_RERANKER_MODEL_FILENAME,
            wbt.SAME_DAY_TOP_T2PLUS_RERANKER_MODEL_FILENAME,
            wbt.RUNNER_DAY_CONFIDENCE_MODEL_FILENAME,
        )
    }
    rs5_shadow.attach_decision_provenance(
        report,
        watchlist_text=watchlist_text,
        analyzer_path=wbt.__file__,
        model_paths=model_paths,
    )
    if not persist:
        return report
    persist_rs5_report(report, directory)
    return report


def persist_rs5_report(report: dict, directory: str | None = None) -> bool:
    """Publish the decision and verify first-writer-wins identity.

    A different immutable same-day report means the current activation cannot
    be audited faithfully, so callers must keep the legacy rank-1 candidate.
    """
    target_dir = RS5_SHADOW_DIR if directory is None else directory
    try:
        report_path = rs5_shadow.write_atomic_report(report, target_dir)
        with open(report_path, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except Exception as exc:  # noqa: BLE001 - audit failure disables activation only
        report["audit_error"] = f"atomic audit write failed: {exc}"
        log(f"RS5 audit: не удалось записать/проверить audit: {exc}")
        return False

    identity_key = (
        "activation_resolution_id"
        if report.get("activation_resolution_id") is not None
        else "decision_id"
    )
    identity_matches = stored.get(identity_key) == report.get(identity_key)
    report["audit_path"] = str(report_path)
    report["audit_identity_key"] = identity_key
    report["audit_identity_matches"] = identity_matches
    if not identity_matches:
        report["audit_conflict"] = (
            f"immutable {identity_key}={stored.get(identity_key)!r}, "
            f"current={report.get(identity_key)!r}"
        )

    details = []
    for row in report.get("candidates", []):
        rs5_value = row.get("aligned_rs5_pp")
        rendered = "NA" if rs5_value is None else f"{rs5_value:+.2f}"
        gate = row.get("eligibility_reason") or "ok"
        details.append(f"r{row['rank']} {row['symbol']} RS5={rendered} gate={gate}")
    log(
        f"RS5 {report['mode'].upper()}: {'; '.join(details)} | "
        f"decision={report.get('decision')} -> {report.get('live_selected_symbol') or 'SKIP'} | "
        f"audit={report_path} | identity={'ok' if identity_matches else 'CONFLICT'}"
    )
    return identity_matches


def preflight_engine_a_candidate(
    bot: TradeBot,
    item: wbt.IdeaAnalysis,
    cash_snapshot_provider,
) -> dict:
    """Build a complete immutable entry plan using read-only broker calls only."""
    plan = {
        "symbol": item.symbol,
        "direction": item.direction,
        "ready": False,
        "retry": False,
        "reason": None,
    }

    def reject(reason: str, *, retry: bool = False) -> dict:
        plan["reason"] = reason
        plan["retry"] = retry
        return plan

    try:
        info = bot.share_info(item.symbol)
    except Exception as exc:  # noqa: BLE001 - read-only failure, no order attempted
        return reject(f"{item.symbol}: share_info недоступен: {exc}", retry=True)
    plan.update(
        {
            "info": info,
            "api_trade_available": bool(info.get("api_trade_available")),
            "short_enabled": bool(info.get("short_enabled")),
        }
    )
    if not plan["api_trade_available"]:
        return reject(f"{item.symbol} недоступен для торговли через API")
    if item.direction == "short" and not plan["short_enabled"]:
        return reject(f"по {item.symbol} шорт недоступен (shortEnabledFlag=false)")

    try:
        status = bot.trading_status(info["uid"])
    except Exception as exc:  # noqa: BLE001
        return reject(f"{item.symbol}: TradingStatus недоступен: {exc}", retry=True)
    plan["trading_status"] = status
    # Both entry routes use LIMIT orders.  marketOrderAvailableFlag is relevant
    # only to forced exits and must not be the entry capability check.
    if not status.get("limit_order_available"):
        return reject(
            f"{item.symbol}: лимитные заявки недоступны (статус {status.get('status')})",
            retry=True,
        )

    if item.exit_target is None:
        return reject(f"{item.symbol}: нет цели выхода (T4)")
    try:
        price = bot.last_price(info["uid"])
    except Exception as exc:  # noqa: BLE001
        return reject(f"{item.symbol}: последняя цена недоступна: {exc}", retry=True)
    if not math.isfinite(price) or price <= 0:
        return reject(f"{item.symbol}: некорректная последняя цена {price!r}", retry=True)
    target_price = round_to_increment(item.exit_target.price, info["min_price_increment"])
    target_move_at_quote = target_move_pct_from_entry(item.direction, price, target_price)
    if target_move_at_quote <= 0:
        return reject(
            f"{item.symbol}: цель {target_price} уже позади текущей цены {price} "
            f"(потенциал {target_move_at_quote:+.2f}%)"
        )

    try:
        cash_before, budget = cash_snapshot_provider()
    except Exception as exc:  # noqa: BLE001
        return reject(f"снимок доступного кэша недоступен: {exc}", retry=True)
    direction_risk_multiplier = LONG_RISK_MULTIPLIER if item.direction == "long" else 1.0
    confidence_risk_multiplier = (
        1.5
        if CONF_SIZING and item.runner_day_prob is not None and item.runner_day_prob >= 0.6
        else 1.0
    )
    risk = RISK_PCT * direction_risk_multiplier * confidence_risk_multiplier
    lot_cost = price * info["lot"]
    risk_sized_position_rub = budget * risk / wbt.STOP_LOSS_PCT
    if TARGET_POSITION_RUB > 0:
        desired_position_rub = min(TARGET_POSITION_RUB, budget * MAX_LEVERAGE)
    else:
        desired_position_rub = risk_sized_position_rub
    desired_lots = int(desired_position_rub // lot_cost)
    broker_max_lots = None
    if TARGET_POSITION_RUB > 0:
        try:
            broker_max_lots = bot.max_order_lots(info["uid"], item.direction, price)
        except Exception as exc:  # noqa: BLE001 - margin capacity is required in target mode
            return reject(f"{item.symbol}: GetMaxLots недоступен: {exc}", retry=True)
        lots = min(desired_lots, broker_max_lots)
    else:
        lots = desired_lots
    if lots < 1:
        return reject(
            f"не хватает денег на 1 лот {item.symbol} "
            f"(лот={info['lot']} шт ≈ {lot_cost:,.0f} ₽, "
            f"цель позиции {desired_position_rub:,.0f} ₽, "
            f"маржинальный лимит {broker_max_lots})"
        )
    actual_position_rub = lots * lot_cost
    confirm_margin_trade = item.direction == "short" or actual_position_rub > cash_before
    account_risk_pct = (
        actual_position_rub * wbt.STOP_LOSS_PCT / cash_before
        if cash_before > 0
        else None
    )

    plan.update(
        {
            "ready": True,
            "price": price,
            "target_price": target_price,
            "target_move_at_quote": target_move_at_quote,
            "cash_before": cash_before,
            "budget": budget,
            "direction_risk_multiplier": direction_risk_multiplier,
            "confidence_risk_multiplier": confidence_risk_multiplier,
            "risk": risk,
            "target_position_rub": TARGET_POSITION_RUB or None,
            "desired_position_rub": desired_position_rub,
            "actual_position_rub": actual_position_rub,
            "position_rub": actual_position_rub,
            "broker_max_lots": broker_max_lots,
            "confirm_margin_trade": confirm_margin_trade,
            "account_risk_pct": account_risk_pct,
            "lot_cost": lot_cost,
            "lots": lots,
        }
    )
    return plan


def cmd_enter(bot: TradeBot, args: argparse.Namespace) -> int:
    today = date.today()
    if today.weekday() >= 5 and not args.force:
        log("выходной — входа нет")
        return 0
    target_manifest_sha = None
    if TARGET_DISTANCE_MULTIPLIER != 1.0:
        valid, status, target_manifest_sha = validate_profit_target_activation_manifest(trade_date=today)
        if not valid:
            log(f"СТОП: target125 activation manifest: {status}")
            return 1
    journal = load_journal()
    if journal and journal.get("date") == today.isoformat() and any(
        trade.get("adopted") for trade in journal["trades"]
    ):
        log("СТОП: сегодня была усыновлена позиция без journal — новый вход запрещён")
        return 1
    if journal and journal.get("date") == today.isoformat() and any(
        t.get("engine") == "A"
        and t.get("phase") in ("entered", "entry_pending", "entry_submitting", "exit_submitting")
        for t in journal["trades"]
    ):
        log("СТОП: мотор A сегодня уже в работе — второй вход запрещён")
        return 1

    if args.generate:
        log("генерирую свежий вотчлист…")
        result = subprocess.run(
            [sys.executable, "-m", "argonus.watchlists.generate_watchlist", "-o",
             os.path.join(PROJECT_DIR, "data/watchlists/watchlist.txt")],
            cwd=PROJECT_DIR, capture_output=True, text=True, timeout=1800,
        )
        if result.returncode != 0:
            log(f"СТОП: generate_watchlist.py упал: {result.stderr[-500:]}")
            return 1

    text = read_watchlist(args.input)
    analyses = wbt.analyze_watchlist(text, today)
    if not analyses:
        log("СТОП: анализ не дал валидного выбора")
        return 1
    warning = wbt.detect_reference_close_mismatch(analyses) or wbt.detect_watchlist_mismatch(analyses)
    if warning:
        log(f"СТОП: вотчлист несвежий/не совпадает с рынком: {warning}")
        return 1

    legacy_top = analyses[0]
    top = legacy_top
    rs5_report: dict = {}
    rs5_skip = False

    def skip_day(reason: str, *, selector_report: dict | None = None) -> int:
        log(f"МОТОР A — пропуск: {reason}" + (" (день закроет мотор B)" if SECOND_ENGINE else ""))
        record = {"engine": "A", "phase": "skipped", "reason": reason}
        if selector_report:
            record.update(
                {
                    "selector_policy": "directional_rs5_top3",
                    "selector_decision": selector_report.get("decision"),
                    "selector_decision_id": selector_report.get("decision_id"),
                    "selector_resolution_id": selector_report.get("activation_resolution_id"),
                    "selector_manifest_sha256": selector_report.get("activation_manifest_sha256"),
                    "selector_original_symbol": legacy_top.symbol,
                    "selector_recommended_symbol": selector_report.get("recommended_symbol"),
                    "selector_final_symbol": selector_report.get("live_selected_symbol"),
                }
            )
        upsert_trade(bot.live, today.isoformat(), record)
        return 0

    cash_snapshot: tuple[float, float] | None = None
    preflight_cache: dict[tuple[str, str], dict] = {}

    def get_cash_snapshot() -> tuple[float, float]:
        nonlocal cash_snapshot
        if cash_snapshot is None:
            cash_before_value = bot.cash_rub()
            budget_value = cash_before_value
            max_budget = os.environ.get("BOT_MAX_BUDGET")
            if max_budget:
                budget_value = min(budget_value, float(max_budget))
            cash_snapshot = (cash_before_value, budget_value)
        return cash_snapshot

    def get_preflight(item: wbt.IdeaAnalysis) -> dict:
        key = (item.symbol, item.direction)
        if key not in preflight_cache:
            preflight_cache[key] = preflight_engine_a_candidate(
                bot, item, get_cash_snapshot
            )
        return preflight_cache[key]

    # Build the causal decision first.  Replacement feasibility is then checked
    # entirely with read-only calls; after place_entry starts there is no path
    # that can switch back to another symbol.
    try:
        rs5_report = run_rs5_shadow_selector(
            analyses,
            today,
            force=args.force,
            watchlist_text=text,
            persist=False,
        )
        effective_activation = (
            RS5_ACTIVATION_REQUESTED
            and bot.live
            and not args.force
            and args.input is None
        )
        if RS5_ACTIVATION_REQUESTED and not effective_activation:
            if not bot.live:
                suppressed_reason = "dry-run without --live"
            elif args.force:
                suppressed_reason = "--force"
            else:
                suppressed_reason = "custom --input watchlist"
            rs5_report["activation_suppressed_reason"] = suppressed_reason
        top, rs5_skip, rs5_report = resolve_rs5_activation(
            analyses,
            rs5_report,
            activation_requested=effective_activation,
            activation_manifest_path=RS5_ACTIVATION_MANIFEST_PATH,
            candidate_info_provider=get_preflight,
        )

        # keep/legacy requires its own complete plan; replacement was already
        # preflighted by the resolver.  Active skip intentionally performs zero
        # broker calls.
        if (
            top is not None
            and not rs5_skip
            and rs5_report.get("activation_available")
            and rs5_report.get("decision") in ("keep", "replacement")
        ):
            final_plan = get_preflight(top)
            rs5_report["final_preflight"] = {
                key: final_plan.get(key)
                for key in ("symbol", "direction", "ready", "retry", "reason", "lots", "price", "target_price")
            }
            _stamp_rs5_activation_resolution(rs5_report)

        audit_directory = (
            RS5_SHADOW_DIR
            if effective_activation
            else os.path.join(RS5_SHADOW_DIR, "suppressed_manual")
        )
        audit_ok = persist_rs5_report(rs5_report, audit_directory)
        if not audit_ok and (rs5_skip or top is not legacy_top):
            log("RS5 ACTIVE: audit не подтверждён — безопасный возврат к legacy rank1")
            rs5_report["runtime_audit_fallback"] = True
            rs5_report["activation_applied"] = False
            rs5_report["activation_fallback_applied"] = True
            rs5_report["activation_fallback_reason"] = "audit write/identity failure"
            rs5_report["live_selected_symbol"] = legacy_top.symbol
            rs5_report["live_selected_rank"] = 1
            top = legacy_top
            rs5_skip = False
            final_plan = get_preflight(top)
            rs5_report["final_preflight"] = {
                key: final_plan.get(key)
                for key in ("symbol", "direction", "ready", "retry", "reason", "lots", "price", "target_price")
            }
            _stamp_rs5_activation_resolution(rs5_report)
            persist_rs5_report(
                rs5_report,
                os.path.join(
                    RS5_SHADOW_DIR,
                    "runtime_fallback",
                    rs5_report["activation_resolution_id"],
                ),
            )
    except Exception as exc:  # noqa: BLE001 - selector/preflight fails open before any order
        log(f"RS5 недоступен, используем legacy rank1: {exc}")
        top = legacy_top
        rs5_skip = False
        rs5_report = {"mode": "shadow", "activation_error": str(exc)}

    if rs5_skip:
        return skip_day(
            f"RS5 active: {rs5_report.get('decision_reason') or 'нет кандидата выше порога'}",
            selector_report=rs5_report,
        )
    if top is None:
        top = legacy_top
    if top is not legacy_top:
        log(
            f"RS5 ACTIVE: rank1 {legacy_top.symbol} заменён на "
            f"rank{rs5_report.get('live_selected_rank')} {top.symbol}"
        )
    elif rs5_report.get("activation_fallback_applied"):
        log(
            f"RS5 FALLBACK: остаётся rank1 {legacy_top.symbol}: "
            f"{rs5_report.get('activation_fallback_reason')}"
        )

    if top.skipped_reason:
        log(f"СТОП: анализ не дал валидного выбора: {top.skipped_reason}")
        return 1

    if DAY_FILTER:
        threshold = wbt.runner_day_confidence_threshold(top.direction)
        if top.runner_day_prob is not None and threshold is not None and top.runner_day_prob < threshold:
            if args.force:
                log(f"фильтр дня: {top.runner_day_prob:.2f} < {threshold:.2f}, но --force — торгуем")
            else:
                return skip_day(f"уверенность {top.runner_day_prob:.2f} < порога {threshold:.2f}")

    if REGIME_GATES and not args.force:
        if top.direction == "short" and top.market_bullish:
            return skip_day("режим: индекс выше EMA20 — шорт в растущем рынке не торгуем")
        if top.direction == "long" and not top.market_bullish:
            return skip_day("режим: индекс ниже EMA20 — лонг в падающем рынке не торгуем")

    if top.direction == "short" and SHORT_RALLY_GUARD_PCT > 0 and not args.force:
        rally = getattr(top, "short_rally_10d_pct", None)
        if rally is not None and rally > SHORT_RALLY_GUARD_PCT:
            return skip_day(f"{top.symbol} +{rally:.1f}% за 10 дней — риск шорт-сквиза (guard как в моторе B)")

    if top.exit_target is None:
        return skip_day(f"{top.symbol}: нет цели выхода (T4)")

    if MIN_TARGET_PCT > 0 and not args.force and top.exit_target.move_pct_from_close < MIN_TARGET_PCT:
        rr = top.exit_target.move_pct_from_close / wbt.STOP_LOSS_PCT
        return skip_day(
            f"{top.symbol}: цель {top.exit_target.move_pct_from_close:.1f}% < {MIN_TARGET_PCT:.1f}% "
            f"(reward:risk {rr:.1f}:1 при стопе {wbt.STOP_LOSS_PCT:.0f}% — низкая экспектанси)"
        )

    plan = get_preflight(top)
    if not plan.get("ready"):
        failed = list(preflight_cache.values())
        reasons = "; ".join(
            f"{item.get('symbol')}: {item.get('reason')}" for item in failed
        )
        if any(item.get("retry") for item in failed):
            log(f"СТОП (повторим позже): RS5/legacy preflight — {reasons}")
            return 1
        return skip_day(reasons, selector_report=rs5_report)

    info = plan["info"]
    price = plan["price"]
    target_price = plan["target_price"]
    target_move_at_quote = plan["target_move_at_quote"]
    cash_before = plan["cash_before"]
    budget = plan["budget"]
    direction_risk_multiplier = plan["direction_risk_multiplier"]
    confidence_risk_multiplier = plan["confidence_risk_multiplier"]
    risk = plan["risk"]
    position_rub = plan["position_rub"]
    lot_cost = plan["lot_cost"]
    lots = plan["lots"]
    confirm_margin_trade = bool(plan.get("confirm_margin_trade"))
    account_risk_pct = plan.get("account_risk_pct")

    entry_direction = "buy" if top.direction == "long" else "sell"
    fee_estimate: dict | None = None
    try:
        fee_estimate = bot.estimate_order_price(info["uid"], lots, entry_direction, price)
        if fee_estimate["fee_side_pct"] is not None:
            log(
                f"оценка комиссии GetOrderPrice: {fee_estimate['commission_rub']:.2f} ₽ "
                f"за сторону ({fee_estimate['fee_side_pct']:.4f}%), "
                f"круг ≈ {2 * fee_estimate['fee_side_pct']:.4f}%"
            )
    except Exception as exc:  # noqa: BLE001
        # Оценка комиссии полезна для forward-аудита, но краткий сбой read-only
        # preflight не должен сам по себе лишать валидного входа.
        log(f"GetOrderPrice недоступен, продолжаю без оценки комиссии: {exc}")

    stop_price = price * (1 - wbt.STOP_LOSS_PCT / 100) if top.direction == "long" else price * (1 + wbt.STOP_LOSS_PCT / 100)
    stop_price = round_to_increment(stop_price, info["min_price_increment"])
    if plan.get("target_position_rub") is not None:
        risk_text = (
            f"риск по стопу ≈{account_risk_pct:.2f}% счёта; "
            f"цель позиции до {plan['target_position_rub']:,.0f} ₽"
        )
    else:
        risk_text = (
            f"риск {risk:.2f}% (множитель направления "
            f"{direction_risk_multiplier:.2f})"
        )
    log(
        f"ВХОД: {top.symbol} {top.direction} | {lots} лот(ов) × {info['lot']} шт ≈ {lots * lot_cost:,.0f} ₽ | "
        f"цена ~{price} | стоп {stop_price} (−{wbt.STOP_LOSS_PCT}%) | исходная {top.exit_target.label} {target_price} | "
        f"множитель дистанции цели {TARGET_DISTANCE_MULTIPLIER:g} от фактического входа | "
        f"потенциал {target_move_at_quote:.2f}% ({target_move_at_quote / wbt.STOP_LOSS_PCT:.2f}:1) | "
        f"{risk_text} | "
        f"уверенность дня {top.runner_day_prob if top.runner_day_prob is None else round(top.runner_day_prob, 2)}"
    )

    base_state = {
        "date": today.isoformat(),
        "engine": "A",
        "symbol": top.symbol,
        "uid": info["uid"],
        "direction": top.direction,
        "lots": lots,
        "lot_size": info["lot"],
        "increment": info["min_price_increment"],
        "reference_price": price,
        "target_price": target_price,
        "original_t4_price": float(top.exit_target.price),
        "target_distance_multiplier": TARGET_DISTANCE_MULTIPLIER,
        "target_policy": profit_target.POLICY if TARGET_DISTANCE_MULTIPLIER != 1.0 else "absolute_t4",
        "target_activation_manifest_sha256": target_manifest_sha,
        "cash_before": cash_before,
        "target_position_rub": plan.get("target_position_rub"),
        "desired_position_rub": plan.get("desired_position_rub"),
        "actual_position_rub": plan.get("actual_position_rub"),
        "broker_max_lots": plan.get("broker_max_lots"),
        "confirm_margin_trade": confirm_margin_trade,
        "risk_pct": account_risk_pct if plan.get("target_position_rub") is not None else risk,
        "sizing_risk_pct": risk,
        "direction_risk_multiplier": direction_risk_multiplier,
        "target_move_pct_at_quote": target_move_at_quote,
        "estimated_entry_commission_rub": (
            fee_estimate.get("commission_rub") if fee_estimate is not None else None
        ),
        "estimated_fee_side_pct": (
            fee_estimate.get("fee_side_pct") if fee_estimate is not None else None
        ),
        "runner_day_prob": top.runner_day_prob,
        "selector_policy": "directional_rs5_top3",
        "selector_mode": rs5_report.get("mode"),
        "selector_decision": rs5_report.get("decision"),
        "selector_decision_id": rs5_report.get("decision_id"),
        "selector_resolution_id": rs5_report.get("activation_resolution_id"),
        "selector_manifest_sha256": rs5_report.get("activation_manifest_sha256"),
        "selector_original_symbol": legacy_top.symbol,
        "selector_recommended_symbol": rs5_report.get("recommended_symbol"),
        "selector_final_symbol": top.symbol,
        "selector_activation_applied": bool(rs5_report.get("activation_applied")),
        "selector_fallback_applied": bool(rs5_report.get("activation_fallback_applied")),
        "selector_fallback_reason": rs5_report.get("activation_fallback_reason"),
        "entry_time_force_override": bool(args.force),
    }

    forward_shadow_capture = bool(
        FORWARD_SHADOW_ENABLED
        and bot.live
        and not args.force
        and args.input is None
    )
    if forward_shadow_capture:
        try:
            _record_forward_selector_shadow(
                analyses=analyses,
                trade_date=today,
                control=top,
                legacy_top=legacy_top,
                watchlist_text=text,
                control_plan=plan,
                rs5_report=rs5_report,
            )
        except Exception as exc:  # noqa: BLE001 - telemetry must fail open
            log(f"FORWARD SHADOW selector недоступен; live-вход не меняю: {exc}")

    return place_entry(bot, base_state, price, lots, info)


def place_entry_delayed_0705(
    bot: TradeBot,
    base_state: dict,
    lots: int,
    info: dict,
) -> int:
    """Execute Engine A from one fresh depth-50 snapshot and one FOK limit.

    The quote is deliberately acquired after selector/audit/fee preflights so
    no slower read-only work can make it stale before PostOrder.  The complete
    request intent is durable before the only broker mutation.
    """
    now = datetime.now()

    def skip(reason: str, source: dict | None = None) -> int:
        frozen_source = source or base_state
        log(f"07:05 FOK — пропуск {base_state.get('symbol')}: {reason}")
        upsert_trade(
            bot.live,
            base_state["date"],
            {
                **frozen_source,
                "engine": base_state.get("engine", "A"),
                "phase": "skipped",
                "entry_policy": entry0705.POLICY,
                "reason": reason,
            },
        )
        return 0

    if (
        bot.live
        and not base_state.get("entry_time_force_override")
        and not (ENTRY_START <= now.time() <= ENTRY_DEADLINE)
    ):
        return skip(
            f"вне frozen-окна {ENTRY_START.strftime('%H:%M')}–"
            f"{ENTRY_DEADLINE.strftime('%H:%M')} МСК"
        )

    if bot.live and not base_state.get("entry_time_force_override"):
        valid, manifest_status, manifest_sha = validate_entry_activation_manifest(
            trade_date=date.fromisoformat(str(base_state["date"]))
        )
        if not valid:
            return skip(f"activation manifest: {manifest_status}")
        base_state = {
            **base_state,
            "entry_activation_manifest_sha256": manifest_sha,
            "entry_activation_status": manifest_status,
        }

    try:
        raw_book = bot.order_book(info["uid"], ENTRY_BOOK_DEPTH)
        received_at = datetime.now().astimezone()
        book = entry0705.normalize_orderbook(
            raw_book,
            expected_uid=info["uid"],
            expected_symbol=base_state["symbol"],
            expected_depth=ENTRY_BOOK_DEPTH,
        )
        quote = entry0705.build_execution_quote(
            book,
            direction=base_state["direction"],
            requested_lots=lots,
            target_price=float(base_state["target_price"]),
            received_at=received_at,
            max_quote_age_ms=ENTRY_MAX_QUOTE_AGE_MS,
            max_impact_bps=ENTRY_MAX_IMPACT_BPS,
        )
    except Exception as exc:  # noqa: BLE001 - no order has been attempted
        # Until the strict deadline a later tick may obtain a healthy snapshot;
        # after it cmd_tick records the day as skipped.  No legacy entry route
        # is allowed as a fallback.
        log(f"07:05 FOK preflight недоступен (без заявки): {exc}")
        return 1

    limit_price = entry0705.round_marketable_limit(
        quote["limit_price"], info["min_price_increment"], base_state["direction"]
    )
    if target_move_pct_from_entry(
        base_state["direction"], limit_price, float(base_state["target_price"])
    ) <= 0.0:
        return skip("после округления FOK-лимит оказался у frozen T4 или за ней")
    desired_position = float(base_state.get("desired_position_rub") or 0.0)
    lot_size = int(base_state.get("lot_size") or info.get("lot") or 1)
    if desired_position > 0.0:
        # Buy can execute no worse than its limit.  Sell can receive price
        # improvement up to the current best bid; use the larger value for the
        # RUB-cap guard.  Lots may shrink, but are never increased here.
        guard_price = max(limit_price, float(quote["best_touch"]))
        capped_lots = int(desired_position // (guard_price * lot_size))
        lots = min(lots, capped_lots)
        if lots < 1:
            return skip("лимит позиции не допускает ни одного лота по стакану 07:05")
        if lots != int(quote["requested_lots"]):
            try:
                quote = entry0705.build_execution_quote(
                    book,
                    direction=base_state["direction"],
                    requested_lots=lots,
                    target_price=float(base_state["target_price"]),
                    received_at=received_at,
                    max_quote_age_ms=ENTRY_MAX_QUOTE_AGE_MS,
                    max_impact_bps=ENTRY_MAX_IMPACT_BPS,
                )
                limit_price = entry0705.round_marketable_limit(
                    quote["limit_price"], info["min_price_increment"], base_state["direction"]
                )
                if target_move_pct_from_entry(
                    base_state["direction"], limit_price, float(base_state["target_price"])
                ) <= 0.0:
                    return skip("после округления уменьшенный FOK-лимит достиг frozen T4")
            except Exception as exc:  # noqa: BLE001
                return skip(f"уменьшенный объём не прошёл depth-50: {exc}")

    # Re-evaluate age immediately before journaling/PostOrder; sizing and
    # manifest checks must not consume the remaining freshness budget.
    try:
        quote = entry0705.build_execution_quote(
            book,
            direction=base_state["direction"],
            requested_lots=lots,
            target_price=float(base_state["target_price"]),
            received_at=datetime.now().astimezone(),
            max_quote_age_ms=ENTRY_MAX_QUOTE_AGE_MS,
            max_impact_bps=ENTRY_MAX_IMPACT_BPS,
        )
        limit_price = entry0705.round_marketable_limit(
            quote["limit_price"], info["min_price_increment"], base_state["direction"]
        )
    except Exception as exc:  # noqa: BLE001 - still no broker mutation
        log(f"07:05 FOK final freshness check failed (без заявки): {exc}")
        return 1
    if target_move_pct_from_entry(
        base_state["direction"], limit_price, float(base_state["target_price"])
    ) <= 0.0:
        return skip("финальный FOK-лимит достиг frozen T4")

    entry_direction = "buy" if base_state["direction"] == "long" else "sell"
    request_id = str(uuid.uuid4())
    frozen = {
        **base_state,
        "lots": lots,
        "reference_price": float(quote["executable_vwap"]),
        "actual_position_rub": lots * lot_size * float(quote["executable_vwap"]),
        "phase": "entry_submitting",
        "entry_policy": entry0705.POLICY,
        "entry_route": "delayed_0705_depth50_fok",
        "entry_order_id": request_id,
        "entry_submit_price": limit_price,
        "entry_submit_lots": lots,
        "entry_submitted_at": datetime.now().isoformat(timespec="milliseconds"),
        "entry_time_in_force": entry0705.TIME_IN_FORCE,
        "entry_quote": quote,
        "entry_order_payload": {
            "uid": info["uid"],
            "lots": lots,
            "direction": entry_direction,
            "price": limit_price,
            "confirm_margin_trade": bool(base_state.get("confirm_margin_trade")),
            "time_in_force": entry0705.TIME_IN_FORCE,
        },
    }
    log(
        f"07:05 FOK: {base_state['symbol']} {base_state['direction']} {lots} лот(ов) | "
        f"VWAP {quote['executable_vwap']:.6f}, worst/limit {limit_price}, "
        f"impact {quote['impact_bps']:.3f} bps, age {quote['quote_age_ms']:.0f} ms"
    )
    upsert_trade(bot.live, base_state["date"], frozen)

    if not bot.live:
        finalize_entry(bot, frozen, lots, float(quote["executable_vwap"]), entry_request_id=request_id)
        return 0

    # The durable intent write includes an atomic replace and fsync.  It is
    # intentionally followed by one last no-I/O age/window check immediately
    # before PostOrder, so persistence latency cannot turn a valid snapshot
    # into a stale live request.
    submit_now = datetime.now()
    if (
        not base_state.get("entry_time_force_override")
        and not (ENTRY_START <= submit_now.time() <= ENTRY_DEADLINE)
    ):
        return skip("окно 07:05 закончилось после durable intent, заявка не отправлена", frozen)
    try:
        submit_quote = entry0705.build_execution_quote(
            book,
            direction=base_state["direction"],
            requested_lots=lots,
            target_price=float(base_state["target_price"]),
            received_at=submit_now.astimezone(),
            max_quote_age_ms=ENTRY_MAX_QUOTE_AGE_MS,
            max_impact_bps=ENTRY_MAX_IMPACT_BPS,
        )
        submit_limit = entry0705.round_marketable_limit(
            submit_quote["limit_price"],
            info["min_price_increment"],
            base_state["direction"],
        )
    except Exception as exc:  # noqa: BLE001 - intent exists, PostOrder does not
        return skip(f"стакан устарел после durable intent: {exc}", frozen)
    if not math.isclose(submit_limit, limit_price, rel_tol=0.0, abs_tol=1e-12):
        return skip("FOK-лимит изменился после durable intent", frozen)
    if target_move_pct_from_entry(
        base_state["direction"], submit_limit, float(base_state["target_price"])
    ) <= 0.0:
        return skip("frozen T4 достигнута после durable intent", frozen)

    try:
        _, response = bot.place_fok_limit(
            info["uid"],
            lots,
            entry_direction,
            limit_price,
            request_id=request_id,
            confirm_margin_trade=bool(base_state.get("confirm_margin_trade")),
        )
    except Exception as exc:  # noqa: BLE001 - request may exist; reconcile only
        log(f"07:05 FOK request {request_id}: ACK неопределён: {exc}")
        return 1

    status = response.get("executionReportStatus")
    executed = min(max(int(response.get("lotsExecuted") or 0), 0), lots)
    avg_entry = (
        quotation_to_float(_get_field(response, "executedOrderPrice"))
        or float(quote["executable_vwap"])
    )
    if status == "EXECUTION_REPORT_STATUS_FILL":
        finalize_entry(
            bot,
            frozen,
            executed or lots,
            avg_entry,
            entry_request_id=request_id,
        )
        return 0
    if status in TERMINAL_ORDER_STATUSES:
        if executed > 0:
            # FOK should be all-or-none, but an observed terminal partial fill
            # is real exposure and must be protected immediately.
            finalize_entry(
                bot,
                frozen,
                executed,
                avg_entry,
                entry_request_id=request_id,
            )
            return 0
        return skip(f"FOK terminal={status}, исполнено 0", frozen)

    log(f"07:05 FOK request {request_id} пока {status}; следующий tick выполнит reconcile")
    return 1


def place_entry(bot: TradeBot, base_state: dict, price: float, lots: int, info: dict) -> int:
    """Общая механика входа (основной пайплайн и план Б): откат-лимит или лесенка."""
    if os.environ.get("BOT_OPENING_SCANNER", "0") == "1":
        try:
            from argonus.trading import production_opening
            production_opening.primary_entry_guard(bot, sys.modules[__name__], base_state["date"])
        except Exception as exc:
            reason = f"shared opening portfolio: {exc}"
            upsert_trade(bot.live, base_state["date"], {**base_state, "phase": "skipped", "reason": reason})
            log(f"СТОП: {reason}")
            return 1
    if base_state.get("engine", "A") == "A" and base_state.get("target_distance_multiplier", 1.0) != 1.0:
        valid, status, digest = validate_profit_target_activation_manifest(
            trade_date=date.fromisoformat(base_state["date"])
        )
        if base_state["target_distance_multiplier"] != TARGET_DISTANCE_MULTIPLIER:
            valid, status = False, "frozen target multiplier differs from runtime config"
        if not valid:
            reason = f"target125 activation manifest: {status}"
            log(f"СТОП: {reason}")
            upsert_trade(bot.live, base_state["date"], {**base_state, "phase": "skipped", "reason": reason})
            return 1
        base_state = {**base_state, "target_activation_manifest_sha256": digest}
    if (
        base_state.get("engine", "A") == "A"
        and ENTRY_EXECUTION_POLICY == entry0705.POLICY
    ):
        return place_entry_delayed_0705(bot, base_state, lots, info)
    direction = base_state["direction"]
    entry_direction = "buy" if direction == "long" else "sell"
    if PULLBACK_PCT > 0:
        sign = 1.0 if direction == "long" else -1.0
        limit_price = round_to_increment(
            price * (1 - sign * PULLBACK_PCT / 100), info["min_price_increment"]
        )
        if base_state.get("target_price") is not None:
            target_move_at_limit = target_move_pct_from_entry(
                direction, limit_price, base_state["target_price"]
            )
            if target_move_at_limit <= 0:
                reason = (
                    f"лимит {limit_price} уже за целью {base_state['target_price']} "
                    f"({target_move_at_limit:+.2f}%)"
                )
                log(f"вход пропущен: {reason}")
                upsert_trade(
                    bot.live,
                    base_state["date"],
                    {"engine": base_state.get("engine", "A"), "phase": "skipped", "reason": reason},
                )
                return 0
        deadline = (datetime.now() + timedelta(minutes=PULLBACK_WAIT_MIN)).isoformat(timespec="seconds")
        goal = (f"цель {base_state['target_price']}" if base_state.get("target_price")
                else f"цель +{base_state.get('target_pct')}% от входа")
        log(
            f"ВХОД С ОТКАТОМ [{base_state.get('strategy', 'main')}]: {base_state['symbol']} {direction} | "
            f"{lots} лот(ов) | лимит {limit_price} (откат {PULLBACK_PCT}% от ~{price}) | "
            f"ждём до {deadline[11:16]}, потом {PULLBACK_FALLBACK} | {goal}, "
            f"стоп {base_state.get('stop_pct', wbt.STOP_LOSS_PCT)}% от входа"
        )
        if not bot.live:
            upsert_trade(bot.live, base_state["date"], {**base_state, "phase": "entry_pending",
                                                        "limit_price": limit_price, "entry_deadline": deadline})
            return 0
        order_id = str(uuid.uuid4())
        upsert_trade(
            bot.live,
            base_state["date"],
            {
                **base_state,
                "phase": "entry_submitting",
                "entry_route": "pullback_limit",
                "entry_order_id": order_id,
                "entry_submit_price": limit_price,
                "entry_submit_lots": lots,
                "entry_submitted_at": datetime.now().isoformat(timespec="seconds"),
                "entry_deadline": deadline,
            },
        )
        try:
            order_id, response = bot.place_limit(
                info["uid"],
                lots,
                entry_direction,
                limit_price,
                confirm_margin_trade=bool(
                    base_state.get("confirm_margin_trade", direction == "short")
                ),
                request_id=order_id,
            )
        except Exception as exc:  # ACK may be lost after broker accepted request_id
            log(f"PostOrder ACK неопределён для {order_id}: {exc} — ждём reconcile")
            return 1
        status = response.get("executionReportStatus")
        executed_lots = int(response.get("lotsExecuted") or 0)
        log(
            f"лимит принят брокером: status={status}, request_id={order_id}, "
            f"broker_order_id={response.get('orderId') or '-'}, executed={executed_lots}/{lots}"
        )
        if status == "EXECUTION_REPORT_STATUS_FILL":
            avg = quotation_to_float(_get_field(response, "executedOrderPrice")) or limit_price
            log("лимит исполнился немедленно (цена уже там)")
            finalize_entry(
                bot,
                base_state,
                lots,
                avg,
                entry_request_id=order_id,
            )
            return 0
        if status in TERMINAL_ORDER_STATUSES:
            if executed_lots > 0:
                avg = quotation_to_float(_get_field(response, "executedOrderPrice")) or limit_price
                log(f"лимит завершён частичным исполнением {executed_lots}/{lots}")
                finalize_entry(
                    bot,
                    base_state,
                    executed_lots,
                    avg,
                    entry_request_id=order_id,
                )
                return 0
            log(f"лимит не активен сразу после выставления: {status} — повторим на следующем tick")
            upsert_trade(bot.live, base_state["date"], {**base_state, "phase": "entry_pending",
                                                        "entry_order_id": order_id,
                                                        "entry_broker_order_id": response.get("orderId"),
                                                        "entry_order_status": status,
                                                        "limit_price": limit_price,
                                                        "entry_deadline": deadline})
            return 1
        upsert_trade(bot.live, base_state["date"], {**base_state, "phase": "entry_pending",
                                                    "entry_order_id": order_id,
                                                    "entry_broker_order_id": response.get("orderId"),
                                                    "entry_order_status": status,
                                                    "limit_price": limit_price,
                                                    "entry_deadline": deadline})
        log("лимит выставлен, ждём откат (управление — в tick)")
        return 0

    last_entry_request_id: str | None = None

    def record_submission_intent(
        request_id: str,
        stage: int,
        submit_price: float,
        submit_lots: int,
    ) -> None:
        nonlocal last_entry_request_id
        last_entry_request_id = request_id
        upsert_trade(
            bot.live,
            base_state["date"],
            {
                **base_state,
                "phase": "entry_submitting",
                "entry_route": "smart_fill_limit",
                "entry_order_id": request_id,
                "entry_submit_stage": stage,
                "entry_submit_price": submit_price,
                "entry_submit_lots": submit_lots,
                "entry_submitted_at": datetime.now().isoformat(timespec="seconds"),
            },
        )

    try:
        filled, avg_entry = bot.smart_fill(
            info["uid"], lots, entry_direction, info["min_price_increment"], price,
            must_fill=False,
            confirm_margin_trade=bool(
                base_state.get("confirm_margin_trade", direction == "short")
            ),
            absolute_target_price=base_state.get("target_price"),
            max_position_rub=base_state.get("desired_position_rub"),
            lot_size=int(base_state.get("lot_size") or 1),
            submission_intent_callback=record_submission_intent,
        )
    except OrderSubmissionUncertain as exc:
        log(
            f"PostOrder ACK неопределён для {exc.request_id}: {exc} — "
            "кандидат заморожен до reconcile"
        )
        return 1
    if filled < 1:
        log("вход лимитной лесенкой не исполнился (рынок убежал) — повторим на следующем тике")
        upsert_trade(
            bot.live,
            base_state["date"],
            {
                **base_state,
                "phase": "retry",
                "reason": "все лимитные ступени подтверждённо сняты без исполнения",
            },
        )
        return 1
    finalize_entry(
        bot,
        base_state,
        filled,
        avg_entry if bot.live else price,
        entry_request_id=last_entry_request_id,
    )
    return 0


def try_enter_universe(bot: TradeBot, today: date, trade_a: dict | None) -> int:
    """Мотор B: universe-шорт КАЖДЫЙ день (кроме акции мотора A).

    Валидация (7 мес, интрадей): два мотора вместо одного = +59% -> +95..150%
    за янв-июн при maxDD ~ −5..6%, декабрь-стресс не хуже одиночного.
    Отключение: BOT_SECOND_ENGINE=0. Риск 1% бюджета (1.5% при prob>=0.85),
    суммарное плечо двух моторов ограничено BOT_MAX_LEVERAGE."""

    def give_up(extra: str) -> int:
        log(f"МОТОР B — пропуск: {extra}")
        upsert_trade(bot.live, today.isoformat(),
                     {"engine": "B", "phase": "skipped", "reason": extra})
        return 0

    log("мотор B: скан всей вселенной TQBR…")
    exclude = trade_a.get("symbol") if trade_a and trade_a.get("phase") in (
        "entered", "entry_pending", "entry_submitting", "exit_submitting"
    ) else None
    try:
        from argonus.watchlists.universe_backup_pick import find_backup_short
        pick = find_backup_short(today, exclude_symbol=exclude)
    except Exception as exc:  # noqa: BLE001
        log(f"мотор B не отработал ({exc}) — повторим на следующем тике")
        return 1
    if pick is None:
        return give_up("нет кандидата (нет модели или ликвидных акций)")

    log(f"мотор B: {pick['symbol']} short, prob={pick['prob']:.2f}")
    info = bot.share_info(pick["symbol"])
    if not info["api_trade_available"] or not info["short_enabled"]:
        return give_up(f"{pick['symbol']} недоступен для шорта через API")
    status = bot.trading_status(info["uid"])
    if not status["market_order_available"]:
        log(f"СТОП (повторим позже): рыночные заявки недоступны (статус {status['status']})")
        return 1

    price = bot.last_price(info["uid"])
    cash_before = bot.cash_rub()
    budget = cash_before
    max_budget = os.environ.get("BOT_MAX_BUDGET")
    if max_budget:
        budget = min(budget, float(max_budget))
    high_conf = pick.get("high_conf_threshold", 0.85)
    risk = RISK_PCT * (1.5 if CONF_SIZING and pick["prob"] >= high_conf else 1.0)
    position_rub = budget * risk / pick["stop_pct"]
    # плечо на двоих: позиция A + позиция B <= бюджет × MAX_LEVERAGE
    if trade_a and trade_a.get("phase") in (
        "entered", "entry_pending", "entry_submitting", "exit_submitting"
    ):
        a_value = (trade_a.get("reference_price") or 0) * trade_a.get("lots", 0) * trade_a.get("lot_size", 1)
        position_rub = min(position_rub, max(budget * MAX_LEVERAGE - a_value, 0.0))
    lot_cost = price * info["lot"]
    lots = int(position_rub // lot_cost)
    if lots < 1:
        return give_up(
            f"не хватает на 1 лот {pick['symbol']} (лот ≈ {lot_cost:,.0f} ₽, доступно {position_rub:,.0f} ₽)"
        )
    base_state = {
        "date": today.isoformat(),
        "engine": "B",
        "symbol": pick["symbol"],
        "uid": info["uid"],
        "direction": "short",
        "lots": lots,
        "lot_size": info["lot"],
        "increment": info["min_price_increment"],
        "reference_price": price,
        "target_price": None,
        "target_pct": pick["target_pct"],
        "stop_pct": pick["stop_pct"],
        "cash_before": cash_before,
        "risk_pct": risk,
        "runner_day_prob": pick["prob"],
        "strategy": "universe_short",
    }
    log(
        f"МОТОР B ВХОД: {pick['symbol']} short | {lots} лот(ов) × {info['lot']} шт ≈ {lots * lot_cost:,.0f} ₽ "
        f"(риск {risk:.1f}% бюджета при стопе {pick['stop_pct']}%) | цель +{pick['target_pct']}% от входа"
    )
    return place_entry(bot, base_state, price, lots, info)


def finalize_entry(
    bot: TradeBot,
    base: dict,
    filled: int,
    avg_entry: float,
    *,
    entry_request_id: str | None = None,
    entry_execution_time: str | None = None,
) -> None:
    """Позиция набрана (полностью или частично) — защитить и записать в журнал.

    У мотора A стоп STOP_LOSS_PCT; target125 считает цель от реального fill и
    исходной T4. Старые journal без множителя сохраняют абсолютную T4.
    У плана Б — свои проценты от фактического входа
    (stop_pct/target_pct из universe-модели)."""
    direction = base["direction"]
    sign = 1.0 if direction == "long" else -1.0
    close_direction = "sell" if direction == "long" else "buy"
    entry_price = avg_entry or base.get("reference_price") or 0.0
    if filled < base["lots"]:
        log(f"частичное исполнение входа: {filled}/{base['lots']} лот(ов) — работаем с {filled}")
    stop_pct = float(base.get("stop_pct") or wbt.STOP_LOSS_PCT)
    stop_price = round_to_increment(entry_price * (1 - sign * stop_pct / 100), base["increment"])
    target_price = base.get("target_price")
    target_policy_error = None
    if base.get("target_pct"):
        target_price = round_to_increment(
            entry_price * (1 + sign * float(base["target_pct"]) / 100), base["increment"]
        )
        base = {**base, "target_price": target_price}
    elif base.get("engine", "A") == "A" and base.get("target_distance_multiplier", 1.0) != 1.0:
        try:
            target_price = profit_target.scaled_target_price(
                direction, entry_price, base["original_t4_price"], base["increment"],
                base["target_distance_multiplier"],
            )
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            # Exposure already exists: a target error must never prevent STOP.
            target_policy_error = str(exc)
            log(f"ВНИМАНИЕ: target125 не рассчитан: {exc} — только STOP_LOSS и выход на следующем tick")
    planned_target_price = target_price
    target_move_at_fill = (
        target_move_pct_from_entry(direction, entry_price, planned_target_price)
        if planned_target_price is not None else float("-inf")
    )
    target_guard_failed = target_policy_error is not None or not math.isfinite(target_move_at_fill) or target_move_at_fill <= 0
    if target_guard_failed:
        # До заявки цель проверяется по свежей котировке. Эта ветка означает, что
        # рынок успел перескочить T4 во время исполнения; оставляем заметный след
        # в логе/журнале, чтобы следующий tick не принял это за обычный fill.
        if target_policy_error is None:
            log(
                f"ВНИМАНИЕ: после исполнения цель {planned_target_price} позади входа {entry_price} "
                f"({target_move_at_fill:+.2f}%) — TAKE_PROFIT не выставляю, закрою на следующем tick"
            )
        target_price = None
    base = {
        **base,
        "target_price": target_price,
        "planned_target_price": planned_target_price,
        "target_guard_failed": target_guard_failed,
        "exit_required": target_guard_failed,
    }
    if target_policy_error is not None:
        base["target_policy_error"] = target_policy_error
    state = {k: v for k, v in base.items() if k not in ("entry_order_id", "limit_price", "entry_deadline")}
    state.update({
        "phase": "entered",
        "lots": filled,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "stop_order_id": None,
        "take_order_id": None,
        "target_move_pct_at_fill": target_move_at_fill,
        "protection_status": "pending",
    })
    if (
        base.get("target_position_rub") is not None
        or base.get("actual_position_rub") is not None
    ):
        state["actual_position_rub"] = (
            filled * float(base.get("lot_size") or 1) * entry_price
        )
    # Сначала фиксируем fill: если сеть упадёт между входом и
    # защитой, следующий tick увидит позицию и доставит STOP_LOSS.
    upsert_trade(bot.live, base["date"], state)
    try:
        state["stop_order_id"] = bot.stop_order(
            base["uid"], filled, close_direction, stop_price, "STOP_LOSS"
        )
        state["protection_status"] = "stop_placed"
        upsert_trade(bot.live, base["date"], state)
    except Exception as exc:
        state["protection_error"] = f"STOP_LOSS: {exc}"
        upsert_trade(bot.live, base["date"], state)
        raise
    if target_price is not None:
        try:
            state["take_order_id"] = bot.stop_order(
                base["uid"], filled, close_direction, target_price, "TAKE_PROFIT"
            )
            state["protection_status"] = "protected"
            state.pop("protection_error", None)
        except Exception as exc:
            # STOP_LOSS уже стоит, а journal сохранён: следующий
            # tick безопасно повторит только TAKE_PROFIT.
            state["protection_error"] = f"TAKE_PROFIT: {exc}"
            log(f"тейк-профит не выставлен, STOP_LOSS активен: {exc}")
    else:
        state["protection_status"] = "stop_only_target_guard"
    upsert_trade(bot.live, base["date"], state)
    log(f"вход выполнен ({'LIVE' if bot.live else 'DRY-RUN'}): {json.dumps(state, ensure_ascii=False)}")
    if FORWARD_SHADOW_ENABLED and bot.live:
        try:
            _record_forward_exit_shadow(
                bot,
                dict(state),
                entry_request_id=entry_request_id,
                entry_execution_time=entry_execution_time,
            )
        except Exception as exc:  # noqa: BLE001 - protection/order path is authoritative
            log(f"FORWARD SHADOW exit недоступен; защиту/live-state не меняю: {exc}")


def ensure_fresh_watchlist(bot: TradeBot) -> bool:
    """Вотчлист должен быть сгенерирован сегодня (06:40 кроном или здесь же)."""
    if os.path.isfile(WATCHLIST_PATH):
        mtime = date.fromtimestamp(os.path.getmtime(WATCHLIST_PATH))
        if mtime == date.today():
            return True
    log("вотчлист несвежий — генерирую (несколько минут)…")
    result = subprocess.run(
        [sys.executable, "-m", "argonus.watchlists.generate_watchlist", "-o", WATCHLIST_PATH],
        cwd=PROJECT_DIR, capture_output=True, text=True, timeout=1800,
    )
    if result.returncode != 0:
        log(f"генерация вотчлиста упала (повторим позже): {result.stderr[-300:]}")
        return False
    return True


def repair_protection(bot: TradeBot, state: dict) -> None:
    """Позиция есть, но каких-то стоп-заявок нет (сеть упала между ордерами) — доставить.
    Смотрим только заявки СВОЕГО инструмента: моторы не должны чинить друг друга."""
    uid = state["uid"]
    own = [o for o in bot.stop_orders()
           if (o.get("instrumentUid") or o.get("figi")) in (uid, state.get("symbol"))
           or (o.get("stopOrderId") in (state.get("stop_order_id"), state.get("take_order_id")))]
    present = {order.get("stopOrderType") for order in own}
    close_direction = "sell" if state["direction"] == "long" else "buy"
    changed = False
    target_price = state.get("target_price")
    entry_price = float(state.get("entry_price") or 0.0)
    if target_price is not None and entry_price > 0:
        target_move = target_move_pct_from_entry(state["direction"], entry_price, target_price)
        if target_move <= 0:
            log(
                f"tick[{state.get('engine', '?')}]: цель {target_price} позади входа "
                f"{entry_price} ({target_move:+.2f}%) — не восстанавливаю TAKE_PROFIT"
            )
            state["planned_target_price"] = target_price
            state["target_price"] = None
            state["target_guard_failed"] = True
            state["target_move_pct_at_fill"] = target_move
            state["exit_required"] = True
            state["take_order_id"] = None
            target_price = None
            changed = True
    if "STOP_ORDER_TYPE_STOP_LOSS" not in present:
        log(f"tick[{state.get('engine', '?')}]: у позиции нет стоп-лосса — восстанавливаю")
        state["stop_order_id"] = bot.stop_order(uid, state["lots"], close_direction, state["stop_price"], "STOP_LOSS")
        changed = True
    if "STOP_ORDER_TYPE_TAKE_PROFIT" not in present and target_price:
        log(f"tick[{state.get('engine', '?')}]: у позиции нет тейк-профита — восстанавливаю")
        state["take_order_id"] = bot.stop_order(uid, state["lots"], close_direction, target_price, "TAKE_PROFIT")
        changed = True
    if changed:
        upsert_trade(bot.live, state["date"], state)


def cancel_trade_protection(bot: TradeBot, trade: dict) -> None:
    """Снять стоп-заявки ИМЕННО этой сделки (по сохранённым id)."""
    for key in ("stop_order_id", "take_order_id"):
        order_id = trade.get(key)
        if not order_id:
            continue
        if not bot.live:
            log(f"DRY-RUN cancel stop order {order_id}")
            continue
        try:
            bot._post("StopOrdersService/CancelStopOrder",
                      {"accountId": bot.account_id, "stopOrderId": order_id})
            log(f"снята стоп-заявка {order_id}")
        except Exception:  # noqa: BLE001
            pass  # уже снята или исполнена


def mark_trade_closed(bot: TradeBot, trade: dict, note: str) -> None:
    record = dict(trade)
    record["phase"] = "closed"
    record["closed_note"] = note
    record["closed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    upsert_trade(bot.live, trade["date"], record)
    log(f"сделка [{trade.get('engine', '?')}] {trade.get('symbol')}: {note}")


def adopt_position(bot: TradeBot, position: dict) -> None:
    """Позиция есть, а журнала за сегодня нет (ответ API потерялся при входе) —
    восстановить журнал из портфеля и доставить защитные заявки."""
    uid = position.get("instrumentUid") or position.get("figi")
    quantity = quotation_to_float(_get_field(position, "quantity"))
    lot_units = int(position.get("quantityLots", {}).get("units", 0) or 0)
    lots = abs(lot_units) if lot_units else int(abs(quantity))
    direction = "long" if quantity > 0 else "short"
    avg_price = quotation_to_float(_get_field(position, "averagePositionPrice"))
    stop_price = avg_price * (1 - wbt.STOP_LOSS_PCT / 100) if direction == "long" else avg_price * (1 + wbt.STOP_LOSS_PCT / 100)

    symbol, target_price, increment = None, None, 0.01
    try:
        instrument = bot._post(
            "InstrumentsService/GetInstrumentBy", {"idType": "INSTRUMENT_ID_TYPE_UID", "id": uid}
        ).get("instrument") or {}
        symbol = instrument.get("ticker")
        increment = quotation_to_float(_get_field(instrument, "minPriceIncrement")) or 0.01
    except Exception as exc:  # noqa: BLE001
        log(f"tick: не удалось определить тикер позиции: {exc}")
    if symbol:
        try:
            analyses = wbt.analyze_watchlist(read_watchlist(None), date.today())
            for item in analyses:
                if item.symbol == symbol and item.exit_target is not None and item.direction == direction:
                    target_price = round_to_increment(item.exit_target.price, increment)
                    break
        except Exception as exc:  # noqa: BLE001
            log(f"tick: цель для усыновлённой позиции не восстановлена: {exc}")

    planned_target_price = target_price
    target_move = None
    target_guard_failed = False
    if target_price is not None:
        target_move = target_move_pct_from_entry(direction, avg_price, target_price)
        if target_move <= 0:
            target_guard_failed = True
            target_price = None
            log(
                f"tick: усыновлённая цель {planned_target_price} позади средней "
                f"цены {avg_price} ({target_move:+.2f}%) — только STOP_LOSS"
            )

    log(f"tick: УСЫНОВЛЯЮ позицию {symbol or uid} {direction} {lots} лот(ов) по ~{avg_price} (журнал был потерян)")
    state = {
        "date": date.today().isoformat(),
        "engine": f"adopted-{str(uid)[:6]}",
        "phase": "entered",
        "symbol": symbol or uid,
        "uid": uid,
        "direction": direction,
        "lots": lots,
        "increment": increment,
        "entry_price": avg_price,
        "stop_price": round_to_increment(stop_price, increment),
        "target_price": target_price,
        "planned_target_price": planned_target_price,
        "target_guard_failed": target_guard_failed,
        "target_move_pct_at_fill": target_move,
        "exit_required": target_guard_failed,
        "adopted": True,
    }
    upsert_trade(bot.live, state["date"], state)
    repair_protection(bot, state)


def reconcile_submitting(bot: TradeBot, state: dict) -> int:
    """Resolve a PostOrder whose broker ACK may have been lost.

    The frozen symbol/request id remains authoritative.  Until the request is
    terminal, this function never permits a new selector run or another entry.
    """
    engine = state.get("engine", "A")
    request_id = state.get("entry_order_id")
    delayed_fok = state.get("entry_route") in ("delayed_0705_depth50_fok", "opening_depth50_fok")
    if not request_id:
        log(f"tick[{engine}]: entry_submitting без request_id — блокирую новый вход")
        return 1
    try:
        order = bot.order_state(request_id)
    except Exception as exc:  # noqa: BLE001 - uncertainty must remain frozen
        is_not_found = "HTTP 404" in str(exc) or "Order not found" in str(exc)
        if delayed_fok and is_not_found:
            # Never replay a missing 07:05 request.  If the first PostOrder did
            # not reach the broker, its frozen quote is already too old; if it
            # did, a portfolio position is the only authoritative exposure we
            # may adopt and protect.  This rule is date-independent, so a stale
            # journal can never submit yesterday's payload next morning.
            try:
                matching = next(
                    (
                        position
                        for position in bot.share_positions()
                        if (position.get("instrumentUid") or position.get("figi"))
                        == state.get("uid")
                    ),
                    None,
                )
            except Exception as portfolio_exc:  # noqa: BLE001
                log(
                    f"tick[{engine}]: FOK request 404 и портфель недоступен: "
                    f"{portfolio_exc}"
                )
                return 1
            if matching is not None:
                quantity = quotation_to_float(_get_field(matching, "quantity"))
                quantity_lots = quotation_to_float(
                    _get_field(matching, "quantityLots")
                )
                if matching.get("quantityLots") is None and float(state.get("lot_size") or 0)>0:
                    quantity_lots = quantity/float(state["lot_size"])
                intended = int(state.get("lots") or 0)
                expected_sign = 1.0 if state.get("direction") == "long" else -1.0
                direction_matches = (
                    quantity * expected_sign > 0.0
                    and quantity_lots * expected_sign > 0.0
                )
                lots_match = intended > 0 and math.isclose(
                    abs(quantity_lots),
                    float(intended),
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                if not direction_matches or not lots_match:
                    log(
                        f"tick[{engine}]: FOK request 404, но позиция не "
                        f"совпадает с frozen intent: direction={state.get('direction')}, "
                        f"quantity={quantity}, quantityLots={quantity_lots}, "
                        f"intendedLots={intended} — защитные заявки заблокированы"
                    )
                    return 1
                # FOK is all-or-none.  Only an exact same-direction portfolio
                # exposure can be attributed to the uncertain request; a
                # manual/opposite/oversized position must never inherit its
                # STOP/TAKE orders.
                filled = intended
                avg_price = (
                    quotation_to_float(_get_field(matching, "averagePositionPrice"))
                    or float(state.get("entry_submit_price") or 0.0)
                )
                if filled > 0 and avg_price > 0:
                    log(
                        f"tick[{engine}]: FOK request 404, но frozen-позиция "
                        f"существует — защищаю {filled} лот(ов)"
                    )
                    finalize_entry(
                        bot,
                        state,
                        filled,
                        avg_price,
                        entry_request_id=request_id,
                    )
                    return 0
                log(f"tick[{engine}]: FOK request 404 противоречит позиции — блокирую")
                return 1
            reason = (
                f"FOK request {request_id} не найден, позиции нет — "
                "replay запрещён, день пропущен"
            )
            log(f"tick[{engine}]: {reason}")
            upsert_trade(
                bot.live,
                state["date"],
                {**state, "phase": "skipped", "reason": reason},
            )
            return 0
        else:
            log(f"tick[{engine}]: request {request_id} пока не подтверждён: {exc}")
            return 1

    status = order.get("status")
    if status not in TERMINAL_ORDER_STATUSES:
        try:
            bot.cancel_order(request_id, suppress_errors=False)
            order = bot.order_state(request_id)
            status = order.get("status")
        except Exception as exc:  # noqa: BLE001
            log(
                f"tick[{engine}]: не удалось безопасно снять/проверить request "
                f"{request_id}: {exc}"
            )
            return 1
    if status not in TERMINAL_ORDER_STATUSES:
        log(f"tick[{engine}]: request {request_id} всё ещё {status} — новый вход заблокирован")
        return 1

    executed = int(order.get("lots") or 0)
    if delayed_fok and status == "EXECUTION_REPORT_STATUS_FILL" and executed == 0:
        executed = int(state.get("lots") or 0)
    if executed > 0:
        intended = int(state.get("lots") or executed)
        filled = min(executed, intended)
        avg_price = (
            float(order.get("avg_price") or 0.0)
            or float(state.get("entry_submit_price") or 0.0)
            or float(state.get("reference_price") or 0.0)
        )
        log(
            f"tick[{engine}]: request {request_id} исполнил {filled} лот(ов) "
            f"при потерянном ACK — защищаю ту же frozen-позицию {state.get('symbol')}"
        )
        finalize_entry(
            bot,
            state,
            filled,
            avg_price,
            entry_request_id=request_id,
            entry_execution_time=order.get("execution_time"),
        )
        return 0

    # A fresh portfolio snapshot closes the tiny race between the tick's first
    # portfolio read and terminal order reconciliation.  Contradictory
    # position/order evidence blocks retry instead of risking a duplicate.
    try:
        matching_positions = [
            position
            for position in bot.share_positions()
            if (position.get("instrumentUid") or position.get("figi")) == state.get("uid")
        ]
    except Exception as exc:  # noqa: BLE001
        log(f"tick[{engine}]: не удалось подтвердить отсутствие позиции: {exc}")
        return 1
    if matching_positions:
        log(
            f"tick[{engine}]: request {request_id} terminal без lots, но позиция "
            f"{state.get('symbol')} существует — retry заблокирован"
        )
        return 1

    if delayed_fok:
        reason = f"FOK request {request_id} terminal={status}, исполнено 0 — день пропущен"
        log(f"tick[{engine}]: {reason}")
        upsert_trade(
            bot.live,
            state["date"],
            {**state, "phase": "skipped", "reason": reason},
        )
        return 0

    reason = f"request {request_id} terminal={status}, исполнено 0 — разрешён retry того же дня"
    log(f"tick[{engine}]: {reason}")
    upsert_trade(
        bot.live,
        state["date"],
        {**state, "phase": "retry", "reason": reason},
    )
    return 0


def reconcile_pending(bot: TradeBot, state: dict) -> int:
    """Фаза entry_pending: лимит на откате выставлен, решаем что дальше."""
    now = datetime.now()
    engine = state.get("engine", "A")

    def skip(reason: str) -> int:
        upsert_trade(
            bot.live,
            state["date"],
            {**state, "engine": engine, "phase": "skipped", "reason": reason},
        )
        return 0

    def replace_limit(reason: str) -> int:
        entry_direction = "buy" if state["direction"] == "long" else "sell"
        if state.get("target_price") is not None:
            target_move = target_move_pct_from_entry(
                state["direction"], state["limit_price"], state["target_price"]
            )
            if target_move <= 0:
                return skip(
                    f"лимит {state['limit_price']} уже за абсолютной целью "
                    f"{state['target_price']} ({target_move:+.2f}%)"
                )
        log(f"tick[{engine}]: {reason} — переустанавливаю лимит входа")
        new_order_id = str(uuid.uuid4())
        upsert_trade(
            bot.live,
            state["date"],
            {
                **state,
                "phase": "entry_submitting",
                "entry_route": "pullback_limit_retry",
                "entry_order_id": new_order_id,
                "entry_submit_price": state["limit_price"],
                "entry_submit_lots": state["lots"],
                "entry_submitted_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
        try:
            new_order_id, response = bot.place_limit(
                state["uid"], state["lots"], entry_direction, state["limit_price"],
                confirm_margin_trade=bool(
                    state.get("confirm_margin_trade", state["direction"] == "short")
                ),
                request_id=new_order_id,
            )
        except Exception as exc:  # noqa: BLE001
            log(
                f"tick[{engine}]: ACK переустановки {new_order_id} неопределён: "
                f"{exc} — новый вход заблокирован до reconcile"
            )
            return 1
        status = response.get("executionReportStatus")
        executed_lots = int(response.get("lotsExecuted") or 0)
        log(
            f"tick[{engine}]: лимит переустановлен: status={status}, "
            f"request_id={new_order_id}, broker_order_id={response.get('orderId') or '-'}, "
            f"executed={executed_lots}/{state['lots']}"
        )
        if status == "EXECUTION_REPORT_STATUS_FILL":
            finalize_entry(
                bot, state, state["lots"],
                quotation_to_float(_get_field(response, "executedOrderPrice")) or state["limit_price"],
                entry_request_id=new_order_id,
            )
            return cmd_exit(bot) if now.time() >= EXIT_TIME else 0
        if status in TERMINAL_ORDER_STATUSES and executed_lots > 0:
            finalize_entry(
                bot, state, executed_lots,
                quotation_to_float(_get_field(response, "executedOrderPrice")) or state["limit_price"],
                entry_request_id=new_order_id,
            )
            return cmd_exit(bot) if now.time() >= EXIT_TIME else 0
        upsert_trade(bot.live, state["date"], {
            **state,
            "phase": "entry_pending",
            "entry_order_id": new_order_id,
            "entry_broker_order_id": response.get("orderId"),
            "entry_order_status": status,
        })
        return 0 if status not in TERMINAL_ORDER_STATUSES else 1

    order_id = state.get("entry_order_id")
    if not order_id:
        skip("pending без order_id (сбой при выставлении)")
        return 1
    deadline = datetime.fromisoformat(state["entry_deadline"])
    try:
        st = bot.order_state(order_id)
    except TBankApiError as exc:
        text = str(exc)
        if "HTTP 404" in text or "Order not found" in text:
            reason = "лимит входа не найден у брокера"
            log(f"tick[{engine}]: {reason}: {exc}")
            if now < deadline and now.time() < EXIT_TIME:
                return replace_limit(reason)
            st = {"status": "EXECUTION_REPORT_STATUS_CANCELLED", "lots": 0, "avg_price": 0.0}
        else:
            log(f"tick[{engine}]: не удалось проверить лимит входа: {exc}")
            return 1
    if st["status"] == "EXECUTION_REPORT_STATUS_FILL":
        log(f"tick[{engine}]: лимит на откате исполнился")
        finalize_entry(
            bot,
            state,
            state["lots"],
            st["avg_price"] or state["limit_price"],
            entry_request_id=order_id,
            entry_execution_time=st.get("execution_time"),
        )
        return cmd_exit(bot) if now.time() >= EXIT_TIME else 0
    if st["status"] in ("EXECUTION_REPORT_STATUS_CANCELLED", "EXECUTION_REPORT_STATUS_REJECTED"):
        if st["lots"] > 0:
            log(f"tick[{engine}]: лимит завершён частичным исполнением {st['lots']}/{state['lots']}")
            finalize_entry(
                bot,
                state,
                st["lots"],
                st["avg_price"] or state["limit_price"],
                entry_request_id=order_id,
                entry_execution_time=st.get("execution_time"),
            )
            return cmd_exit(bot) if now.time() >= EXIT_TIME else 0
        reason = f"лимит входа не активен: {st['status']}"
        log(f"tick[{engine}]: {reason}")
        if now < deadline and now.time() < EXIT_TIME:
            return replace_limit(reason)

    if now < deadline and now.time() < EXIT_TIME:
        return 0  # заявка стоит, откат ещё может случиться

    try:
        bot.cancel_order(order_id, suppress_errors=False)
    except Exception as exc:  # noqa: BLE001
        log(f"tick[{engine}]: отмена {order_id} не подтверждена: {exc}")
        return 1
    st = bot.order_state(order_id)
    if st["status"] not in TERMINAL_ORDER_STATUSES:
        log(f"tick[{engine}]: после отмены {order_id} всё ещё {st['status']} — fallback запрещён")
        return 1
    if st["lots"] > 0:
        log(f"tick[{engine}]: дедлайн отката, частичное исполнение {st['lots']}/{state['lots']}")
        finalize_entry(
            bot,
            state,
            st["lots"],
            st["avg_price"] or state["limit_price"],
            entry_request_id=order_id,
            entry_execution_time=st.get("execution_time"),
        )
        return cmd_exit(bot) if now.time() >= EXIT_TIME else 0
    if now.time() >= EXIT_TIME or PULLBACK_FALLBACK == "skip":
        reason = "отката не было" + ("" if now.time() < EXIT_TIME else " (день закончился)")
        log(f"tick[{engine}]: {reason} — сделки не будет")
        return skip(reason)
    log(f"tick[{engine}]: отката не дождались — вход лесенкой по текущей цене")
    entry_direction = "buy" if state["direction"] == "long" else "sell"
    try:
        reference = bot.last_price(state["uid"])
    except Exception:  # noqa: BLE001
        reference = state.get("reference_price") or state["limit_price"]
    if state.get("target_price") is not None:
        target_move = target_move_pct_from_entry(
            state["direction"], reference, state["target_price"]
        )
        if target_move <= 0:
            return skip(
                f"к fallback цель {state['target_price']} уже позади цены "
                f"{reference} ({target_move:+.2f}%)"
            )
    last_fallback_request_id: str | None = None

    def record_fallback_intent(
        request_id: str,
        stage: int,
        submit_price: float,
        submit_lots: int,
    ) -> None:
        nonlocal last_fallback_request_id
        last_fallback_request_id = request_id
        upsert_trade(
            bot.live,
            state["date"],
            {
                **state,
                "phase": "entry_submitting",
                "entry_route": "pullback_fallback_smart_fill",
                "entry_order_id": request_id,
                "entry_submit_stage": stage,
                "entry_submit_price": submit_price,
                "entry_submit_lots": submit_lots,
                "entry_submitted_at": datetime.now().isoformat(timespec="seconds"),
            },
        )

    try:
        filled, avg = bot.smart_fill(
            state["uid"], state["lots"], entry_direction, state["increment"], reference,
            must_fill=False,
            confirm_margin_trade=bool(
                state.get("confirm_margin_trade", state["direction"] == "short")
            ),
            absolute_target_price=state.get("target_price"),
            max_position_rub=state.get("desired_position_rub"),
            lot_size=int(state.get("lot_size") or 1),
            submission_intent_callback=record_fallback_intent,
        )
    except OrderSubmissionUncertain as exc:
        log(f"tick[{engine}]: fallback request {exc.request_id} неопределён — ждём reconcile")
        return 1
    if filled < 1:
        return skip("fallback-лесенка не исполнилась")
    finalize_entry(
        bot,
        state,
        filled,
        avg,
        entry_request_id=last_fallback_request_id,
    )
    return 0


def position_lot_count(position: dict, lot_size: int = 1) -> int:
    """quantity is shares; quantityLots is deprecated and may be absent."""
    lot_size = int(lot_size)
    if lot_size < 1:
        raise ValueError("Invalid instrument lot size")
    raw = _get_field(position, "quantityLots")
    count = abs(quotation_to_float(raw)) if raw is not None else abs(quotation_to_float(_get_field(position, "quantity")))/lot_size
    if not math.isfinite(count) or not math.isclose(count, round(count), abs_tol=1e-9, rel_tol=0):
        raise ValueError("Portfolio quantity is not a whole number of lots")
    return int(round(count))


def close_trade_position(bot: TradeBot, trade: dict, position: dict, note: str) -> None:
    """Закрыть только позицию этой сделки, не затрагивая второй мотор."""
    quantity = quotation_to_float(_get_field(position, "quantity"))
    lots = position_lot_count(position, int(trade.get("lot_size") or 1))
    if lots < 1:
        mark_trade_closed(bot, trade, note)
        return
    close_direction = "sell" if quantity > 0 else "buy"
    try:
        reference = bot.last_price(trade["uid"])
    except Exception:  # noqa: BLE001
        reference = quotation_to_float(_get_field(position, "currentPrice")) or trade.get("entry_price")
    cancel_trade_protection(bot, trade)

    def record_exit_intent(
        request_id: str,
        stage: int,
        submit_price: float,
        submit_lots: int,
    ) -> None:
        upsert_trade(
            bot.live,
            trade["date"],
            {
                **trade,
                "phase": "exit_submitting",
                "exit_order_id": request_id,
                "exit_submit_stage": stage,
                "exit_submit_price": submit_price,
                "exit_submit_lots": submit_lots,
                "exit_position_lots_before": lots,
                "exit_direction": close_direction,
                "exit_note": note,
                "exit_submitted_at": datetime.now().isoformat(timespec="seconds"),
            },
        )

    try:
        filled, _ = bot.smart_fill(
            trade["uid"], lots, close_direction, float(trade.get("increment") or 0.01),
            reference, must_fill=True,
            submission_intent_callback=record_exit_intent,
        )
    except OrderSubmissionUncertain as exc:
        # The exact request id is already durable.  Re-adding protection while
        # an exit may still be active could itself over-close the position.
        log(
            f"выход request {exc.request_id} неопределён — "
            "ждём reconcile, повторное закрытие запрещено"
        )
        raise
    except Exception:
        # Если аварийное закрытие не прошло, не оставляем позицию
        # без защиты: ставим STOP_LOSS обратно и даём tick ошибку для retry.
        repair_protection(bot, trade)
        raise
    if filled < lots:
        trade = {**trade, "lots": lots - filled}
        upsert_trade(bot.live, trade["date"], trade)
        repair_protection(bot, trade)
        log(f"аварийное закрытие частичное: {filled}/{lots} лот(ов)")
        return
    mark_trade_closed(bot, trade, note)


def reconcile_exit_submitting(bot: TradeBot, state: dict) -> int:
    """Resolve an uncertain close without risking a duplicate/reversal."""
    engine = state.get("engine", "?")
    request_id = state.get("exit_order_id")
    if not request_id:
        log(f"tick[{engine}]: exit_submitting без request_id — повторный выход запрещён")
        return 1
    try:
        order = bot.order_state(request_id)
        if order.get("status") not in TERMINAL_ORDER_STATUSES:
            bot.cancel_order(request_id, suppress_errors=False)
            order = bot.order_state(request_id)
    except Exception as exc:  # noqa: BLE001 - keep the frozen close request
        log(f"tick[{engine}]: exit request {request_id} пока не подтверждён: {exc}")
        return 1
    if order.get("status") not in TERMINAL_ORDER_STATUSES:
        log(
            f"tick[{engine}]: exit request {request_id} всё ещё "
            f"{order.get('status')} — повторный выход запрещён"
        )
        return 1

    try:
        positions = bot.share_positions()
    except Exception as exc:  # noqa: BLE001
        log(f"tick[{engine}]: портфель после exit {request_id} недоступен: {exc}")
        return 1
    uid = state.get("uid")
    position = next(
        (
            item
            for item in positions
            if (item.get("instrumentUid") or item.get("figi")) == uid
        ),
        None,
    )
    if position is None:
        mark_trade_closed(
            bot,
            state,
            state.get("exit_note") or f"exit request {request_id} исполнен",
        )
        return 0

    quantity = quotation_to_float(_get_field(position, "quantity"))
    if (state.get("direction") == "long" and quantity < 0) or (
        state.get("direction") == "short" and quantity > 0
    ):
        log(
            f"tick[{engine}]: КРИТИЧНО — exit {request_id} развернул позицию; "
            "автоматический retry заблокирован"
        )
        return 1
    remaining_lots = position_lot_count(position, int(state.get("lot_size") or 1))
    executed_lots = int(order.get("lots") or 0)
    before_lots = int(state.get("exit_position_lots_before") or state.get("lots") or 0)
    expected_remaining = max(before_lots - executed_lots, 0)
    if executed_lots > 0 and remaining_lots > expected_remaining:
        log(
            f"tick[{engine}]: exit {request_id} terminal, но портфель ещё не "
            "отразил исполнение — ждём следующий tick"
        )
        return 1

    restored = dict(state)
    for key in (
        "exit_order_id",
        "exit_submit_stage",
        "exit_submit_price",
        "exit_submit_lots",
        "exit_position_lots_before",
        "exit_direction",
        "exit_note",
        "exit_submitted_at",
    ):
        restored.pop(key, None)
    restored.update({"phase": "entered", "lots": remaining_lots})
    current_price = quotation_to_float(_get_field(position, "currentPrice"))
    if current_price > 0:
        restored["actual_position_rub"] = (
            remaining_lots * float(restored.get("lot_size") or 1) * current_price
        )
    upsert_trade(bot.live, state["date"], restored)
    repair_protection(bot, restored)
    log(
        f"tick[{engine}]: exit {request_id} terminal, осталось "
        f"{remaining_lots} лот(ов) — можно безопасно повторить"
    )
    return 0


def reconcile_trade(bot: TradeBot, trade: dict, positions: list[dict]) -> int:
    """Привести одну сделку журнала в согласие с реальностью счёта."""
    phase = trade.get("phase")
    if phase == "exit_submitting":
        return reconcile_exit_submitting(bot, trade)
    if phase == "entry_submitting":
        return reconcile_submitting(bot, trade)
    if phase == "entry_pending":
        return reconcile_pending(bot, trade)
    if phase != "entered":
        return 0
    uid = trade.get("uid")
    pos = next((p for p in positions if (p.get("instrumentUid") or p.get("figi")) == uid), None)
    if pos is not None:
        if trade.get("exit_required"):
            log(
                f"tick[{trade.get('engine', '?')}]: {trade.get('symbol')} — цель была позади fill, "
                "закрываю только эту позицию"
            )
            close_trade_position(bot, trade, pos, "аварийное закрытие: цель позади fill")
            return 0
        repair_protection(bot, trade)
        return 0
    # позиция закрыта биржей (стоп или тейк) — снять ЕЁ стоп-заявки, записать итог
    log(f"tick[{trade.get('engine', '?')}]: {trade.get('symbol')} закрыт биржей — снимаю парную стоп-заявку")
    cancel_trade_protection(bot, trade)
    mark_trade_closed(bot, trade, "закрыта биржей (стоп или тейк)")
    return 0


def run_opening_tick(bot: TradeBot) -> int:
    if os.environ.get("BOT_OPENING_SCANNER", "0") != "1":
        return 0
    try:
        from argonus.trading import production_opening
        return production_opening.tick(bot, sys.modules[__name__])
    except Exception as exc:
        log(f"OPENING: новые входы отложены, существующая защита сохранена: {exc}")
        return 1


def cmd_tick(bot: TradeBot) -> int:
    """Идемпотентный реконсилер: привести счёт в правильное для текущего момента
    состояние, что бы ни случилось до этого (сон ноутбука, обрыв сети).
    A/B и дополнительные OPEN-сделки имеют отдельные записи журнала,
    собственные защитные заявки и фазы исполнения."""
    now = datetime.now()
    today = date.today().isoformat()
    journal = load_journal()
    trades_today = journal["trades"] if journal and journal.get("date") == today else []
    positions = bot.share_positions()
    resolved_stale_entry = False

    # A lost ACK or pending limit from an earlier date must be resolved before
    # today's selector is allowed to run, even when no position is visible yet.
    if journal and journal.get("date") != today:
        stale_submissions = [
            trade for trade in journal["trades"] if trade.get("phase") == "entry_submitting"
        ]
        resolved_stale_entry = bool(stale_submissions)
        for trade in stale_submissions:
            if reconcile_submitting(bot, trade) != 0:
                return 1
        stale_exits = [
            trade for trade in journal["trades"] if trade.get("phase") == "exit_submitting"
        ]
        for trade in stale_exits:
            if reconcile_exit_submitting(bot, trade) != 0:
                return 1
        journal = load_journal()
        stale_trades = journal["trades"] if journal else []
        if any(trade.get("phase") == "entry_pending" for trade in stale_trades):
            log("tick: со вчера остался pending-вход — сначала безопасно снимаю его")
            return cmd_exit(bot)
        positions = bot.share_positions()

    # 1. Остатки позиций с прошлого дня (проспали закрытие) — закрыть немедленно.
    if positions and journal and journal.get("date") != today and any(
            t.get("phase") in (
                "entered", "entry_pending", "entry_submitting", "exit_submitting"
            ) for t in journal["trades"]):
        log("tick: висит позиция с прошлого дня — закрываю остаток")
        return cmd_exit(bot)

    # 2. Реконсилер по каждой сделке дня.
    reconcile_rc = 0
    for trade in trades_today:
        reconcile_rc = max(reconcile_rc, reconcile_trade(bot, trade, positions) or 0)
    if reconcile_rc:
        return reconcile_rc

    # 3. Время выхода прошло — закрыть всё оставшееся.
    if now.time() >= EXIT_TIME:
        journal = load_journal()
        trades_today = journal["trades"] if journal and journal.get("date") == today else []
        if bot.share_positions() or any(
            t.get("phase") in ("entry_pending", "entry_submitting", "exit_submitting")
            for t in trades_today
        ):
            log("tick: время выхода — закрываю всё")
            return cmd_exit(bot)
        return run_opening_tick(bot)

    # 4. Сначала усыновить позиции без journal. Иначе после потери ACK
    # бот успеет войти второй раз до того, как заметит старую позицию.
    journal = load_journal()
    trades_today = journal["trades"] if journal and journal.get("date") == today else []
    known_uids = {
        t.get("uid")
        for t in trades_today
        if t.get("phase") in (
            "entered", "entry_pending", "entry_submitting", "exit_submitting"
        )
    }
    unknown_positions = [
        position for position in bot.share_positions()
        if (position.get("instrumentUid") or position.get("figi")) not in known_uids
    ]
    if unknown_positions:
        for position in unknown_positions:
            adopt_position(bot, position)
        log("tick: найдена позиция без journal — новые входы сегодня блокирую")
        return 0
    if any(trade.get("adopted") for trade in trades_today):
        log("tick: усыновлённая позиция уже есть в journal — новые входы сегодня запрещены")
        return 0

    # Resolving an uncertain request from an earlier date is the only broker
    # workflow allowed in this tick.  Defer today's selector to the next tick
    # so stale reconciliation and a fresh PostOrder can never share one run.
    if resolved_stale_entry:
        log("tick: вчерашний entry_submitting resolved — новый вход отложен до следующего tick")
        return 0

    # 5. Legacy entry remains once/day; opening trades have unique identities.
    if os.environ.get("BOT_OPENING_SCANNER", "0") == "1":
        if now.weekday() >= 5:
            return 0
        try:
            from argonus.trading import production_opening
            if not production_opening.prepare_day(bot, sys.modules[__name__]):
                return 1
        except Exception as exc:
            log(f"OPENING: day-start equity недоступен, новые входы заблокированы: {exc}")
            return 1
    if now.time() < ENTRY_START:
        return 0
    journal = load_journal()
    trades_today = journal["trades"] if journal and journal.get("date") == today else []
    engines = {t.get("engine") for t in trades_today if t.get("phase") != "retry"}
    rc = 0
    if "A" not in engines:
        if now.time() > ENTRY_DEADLINE:
            log("tick: окно входа упущено (машина спала?) — мотор A пропускает день")
            upsert_trade(bot.live, today, {"engine": "A", "phase": "skipped",
                                           "reason": f"вход не состоялся до {ENTRY_DEADLINE.strftime('%H:%M')}"})
        elif not ensure_fresh_watchlist(bot):
            rc = 1
        else:
            rc = max(rc, cmd_enter(bot, argparse.Namespace(generate=False, force=False, input=None)) or 0)
    if SECOND_ENGINE and "B" not in engines:
        if now.time() > ENTRY_DEADLINE:
            upsert_trade(bot.live, today, {"engine": "B", "phase": "skipped",
                                           "reason": f"вход не состоялся до {ENTRY_DEADLINE.strftime('%H:%M')}"})
        else:
            journal = load_journal()
            trades_today = journal["trades"] if journal and journal.get("date") == today else []
            trade_a = next((t for t in trades_today if t.get("engine") == "A"), None)
            rc = max(rc, try_enter_universe(bot, date.today(), trade_a) or 0)

    return max(rc, run_opening_tick(bot)) if rc == 0 else rc


def cmd_watch(bot: TradeBot) -> int:
    """Позиции нет, а стоп-заявки остались (сработал стоп или тейк) — снять их.

    Иначе вторая заявка при развороте цены откроет НОВУЮ позицию без пары.
    Запускать по крону каждые 15 минут в течение сессии.
    """
    if bot.share_positions():
        log("watch: позиция открыта, стоп-заявки на месте")
        return 0
    orders = bot.stop_orders()
    if not orders:
        return 0
    log(f"watch: позиции нет, но висят {len(orders)} стоп-заявок — снимаю")
    bot.cancel_stop_orders()
    return 0


def cmd_exit(bot: TradeBot) -> int:
    today = date.today().isoformat()
    journal = load_journal()
    all_trades = journal["trades"] if journal else []
    trades_today = all_trades if journal and journal.get("date") == today else []
    # An earlier close may have reached the broker while its ACK was lost.
    # Resolve that exact request before any new close can be submitted.
    for trade in all_trades:
        if trade.get("phase") == "exit_submitting":
            if reconcile_exit_submitting(bot, trade) != 0:
                log("выход отложен: неопределённая exit-заявка ещё не reconciled")
                return 1
    journal = load_journal()
    all_trades = journal["trades"] if journal else []
    trades_today = all_trades if journal and journal.get("date") == today else []
    # висящие лимиты входа — снять, чтобы не исполнились после закрытия
    for trade in all_trades:
        if trade.get("phase") == "entry_submitting":
            if reconcile_submitting(bot, trade) != 0:
                log("выход отложен: неопределённая entry-заявка ещё не reconciled")
                return 1
            continue
        if trade.get("phase") == "entry_pending" and trade.get("entry_order_id") and bot.live:
            request_id = trade["entry_order_id"]
            try:
                pending_state = bot.order_state(request_id)
                if pending_state.get("status") not in TERMINAL_ORDER_STATUSES:
                    bot.cancel_order(request_id, suppress_errors=False)
                    pending_state = bot.order_state(request_id)
            except Exception as exc:  # noqa: BLE001
                log(f"выход отложен: статус/отмена entry {request_id} не подтверждены: {exc}")
                return 1
            if pending_state.get("status") not in TERMINAL_ORDER_STATUSES:
                log(
                    f"выход отложен: entry {request_id} всё ещё "
                    f"{pending_state.get('status')}"
                )
                return 1
            executed = int(pending_state.get("lots") or 0)
            if executed > 0:
                finalize_entry(
                    bot,
                    trade,
                    min(executed, int(trade.get("lots") or executed)),
                    float(pending_state.get("avg_price") or trade.get("limit_price") or trade.get("reference_price") or 0.0),
                    entry_request_id=request_id,
                    entry_execution_time=pending_state.get("execution_time"),
                )
            else:
                upsert_trade(
                    bot.live,
                    trade["date"],
                    {
                        **trade,
                        "phase": "skipped",
                        "reason": f"entry {request_id} снят перед выходом без исполнения",
                    },
                )
            log(f"снят ожидающий лимит входа [{trade.get('engine', '?')}] {request_id}")
    bot.cancel_stop_orders()
    positions = bot.share_positions()
    if not positions:
        log("позиций нет — закрывать нечего")
        for trade in trades_today:
            if trade.get("phase") == "entered":
                mark_trade_closed(bot, trade, "позиция уже была закрыта биржей (стоп или тейк)")
            elif trade.get("phase") in ("entry_pending", "entry_submitting"):
                mark_trade_closed(bot, trade, "вход не исполнился, лимит снят")
        return 0
    for position in positions:
        uid = position.get("instrumentUid") or position.get("figi")
        quantity = quotation_to_float(_get_field(position, "quantity"))
        direction = "sell" if quantity > 0 else "buy"
        increment = 0.01
        frozen_trade = next((t for t in trades_today if t.get("uid") == uid), {})
        lot_size = int(frozen_trade.get("lot_size") or 1)
        ticker = position.get("figi") or uid
        try:
            instrument = bot._post(
                "InstrumentsService/GetInstrumentBy", {"idType": "INSTRUMENT_ID_TYPE_UID", "id": uid}
            ).get("instrument") or {}
            increment = quotation_to_float(_get_field(instrument, "minPriceIncrement")) or 0.01
            lot_size = int(instrument.get("lot") or 1)
            ticker = instrument.get("ticker") or ticker
        except Exception:  # noqa: BLE001
            pass
        lots = position_lot_count(position, lot_size)
        log(f"закрываю {position.get('figi')}: {lots} лот(ов), направление {direction}")
        try:
            reference = bot.last_price(uid)
        except Exception:  # noqa: BLE001
            reference = quotation_to_float(_get_field(position, "currentPrice"))
        trade = next(
            (
                item
                for item in all_trades
                if item.get("uid") == uid
                and item.get("phase") in ("entered", "exit_submitting")
            ),
            None,
        )
        if trade is None:
            position_direction = "long" if quantity > 0 else "short"
            sign = 1.0 if position_direction == "long" else -1.0
            stop_price = round_to_increment(
                reference * (1 - sign * wbt.STOP_LOSS_PCT / 100), increment
            )
            trade = {
                "date": today,
                "engine": f"exit-{str(uid)[:6]}",
                "phase": "entered",
                "symbol": ticker,
                "uid": uid,
                "direction": position_direction,
                "lots": lots,
                "lot_size": lot_size,
                "increment": increment,
                "reference_price": reference,
                "entry_price": reference,
                "stop_price": stop_price,
                "target_price": None,
                "adopted_for_exit": True,
            }
            upsert_trade(bot.live, today, trade)
            all_trades.append(trade)
        close_trade_position(bot, trade, position, "принудительное закрытие")

    remaining_positions = bot.share_positions()
    if remaining_positions:
        log(
            f"закрытие частичное: осталось позиций {len(remaining_positions)} — "
            "повторим после reconcile"
        )
        return 1
    if bot.live:
        cash = bot.cash_rub()
        befores = [t.get("cash_before") for t in trades_today if t.get("cash_before") is not None]
        if befores:
            log(f"итог дня: {cash - min(befores):+,.2f} ₽ | кэш {cash:,.2f} ₽")
        else:
            log(f"остаток закрыт, кэш: {cash:,.2f} ₽")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Торговый бот Argonus (T-Invest API).")
    parser.add_argument("command", choices=["status", "enter", "exit", "watch", "tick"])
    parser.add_argument("--live", action="store_true", help="Боевой режим: реально выставлять заявки.")
    parser.add_argument("--generate", action="store_true", help="enter: сначала сгенерировать вотчлист.")
    parser.add_argument("--force", action="store_true", help="enter: игнорировать фильтр дня/выходной.")
    parser.add_argument("--input", help="enter: файл вотчлиста (по умолчанию watchlist.txt).")
    args = parser.parse_args()

    if args.command != "status":
        lock = acquire_lock()
        if lock is None:
            log(f"{args.command}: другой процесс бота уже работает — выхожу")
            return 0

    bot = TradeBot(live=args.live)
    if args.command == "status":
        return cmd_status(bot)
    if args.command == "enter":
        return cmd_enter(bot, args)
    if args.command == "watch":
        return cmd_watch(bot)
    if args.command == "tick":
        return cmd_tick(bot)
    return cmd_exit(bot)


if __name__ == "__main__":
    raise SystemExit(main())
