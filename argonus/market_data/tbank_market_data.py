#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import ssl
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


REST_API_BASE_URL = "https://invest-public-api.tbank.ru/rest"
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_RETRY_COUNT = 4
DEFAULT_TOKEN_ENV_VARS = (
    "TINVEST_TOKEN",
    "T_INVEST_TOKEN",
    "T_BANK_TOKEN",
    "T_BANK_API_TOKEN",
    "TINKOFF_TOKEN",
    "INVEST_API_TOKEN",
)
MOEX_CLASS_CODE = "TQBR"
MOEX_INDEX_CLASS_CODE = "SNDX"
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
UTC = timezone.utc


class TBankApiError(RuntimeError):
    pass


@dataclass(slots=True)
class DailyCandle:
    trade_date: date
    open: float
    low: float
    high: float
    close: float
    volume_lots: int = 0


@dataclass(slots=True)
class ShareInstrument:
    ticker: str
    class_code: str
    lot: int = 1
    currency: str = ""
    uid: str | None = None
    api_trade_available: bool | None = None
    buy_available: bool | None = None
    sell_available: bool | None = None
    short_enabled: bool | None = None
    exchange: str | None = None
    real_exchange: str | None = None

    @property
    def instrument_id(self) -> str:
        if self.uid:
            return self.uid
        return build_instrument_id(self.ticker, self.class_code)


def build_instrument_id(ticker: str, class_code: str) -> str:
    return f"{ticker.strip().upper()}_{class_code.strip().upper()}"


def _iter_token_file_candidates() -> list[str]:
    from argonus.paths import PROJECT_ROOT

    module_dir = str(PROJECT_ROOT)
    candidates = [
        os.path.join(os.getcwd(), ".env"),
        os.path.join(module_dir, ".env"),
        os.path.join(os.getcwd(), ".tbank_token"),
        os.path.join(module_dir, ".tbank_token"),
    ]

    unique_candidates: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = os.path.abspath(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique_candidates.append(normalized)
    return unique_candidates


def _read_token_from_file(path: str) -> str | None:
    if not os.path.isfile(path):
        return None

    with open(path, "r", encoding="utf-8") as handle:
        content = handle.read().strip()

    if not content:
        return None

    if os.path.basename(path) == ".tbank_token":
        return content.splitlines()[0].strip() or None

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() not in DEFAULT_TOKEN_ENV_VARS:
            continue
        cleaned = value.strip().strip('"').strip("'")
        if cleaned:
            return cleaned

    return None


def resolve_api_token(token: str | None = None) -> str:
    if token and token.strip():
        return token.strip()

    for env_name in DEFAULT_TOKEN_ENV_VARS:
        env_value = os.getenv(env_name)
        if env_value and env_value.strip():
            return env_value.strip()

    for candidate_path in _iter_token_file_candidates():
        file_token = _read_token_from_file(candidate_path)
        if file_token:
            return file_token

    supported = ", ".join(DEFAULT_TOKEN_ENV_VARS)
    raise TBankApiError(
        "Не найден токен T-Invest API. "
        f"Задай одну из переменных окружения: {supported}, "
        "или положи токен в .env/.tbank_token рядом с проектом."
    )


def quotation_to_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return float(value)
    if not isinstance(value, dict):
        raise TBankApiError(f"Неожиданный формат котировки: {type(value)!r}")

    units = int(value.get("units") or 0)
    nanos = int(value.get("nano") or value.get("nanos") or 0)
    return float(units) + float(nanos) / 1_000_000_000.0


def parse_api_datetime(raw: str) -> datetime:
    normalized = raw.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def to_api_datetime_range(start_date: date, end_date: date) -> tuple[str, str]:
    if end_date < start_date:
        raise TBankApiError("Некорректный диапазон дат для запроса свечей.")

    start_dt = datetime.combine(start_date, dt_time.min, MOSCOW_TZ).astimezone(UTC)
    end_dt = datetime.combine(end_date + timedelta(days=1), dt_time.min, MOSCOW_TZ).astimezone(UTC)
    return (
        start_dt.isoformat().replace("+00:00", "Z"),
        end_dt.isoformat().replace("+00:00", "Z"),
    )


def _get_field(payload: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in payload and payload[name] is not None:
            return payload[name]
    return default


def _to_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return bool(value)


def _extract_error_message(raw_body: bytes) -> str:
    if not raw_body:
        return ""
    body_text = raw_body.decode("utf-8", "ignore").strip()
    if not body_text:
        return ""

    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError:
        return body_text

    for key in ("message", "error", "description", "details"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return body_text


def _is_ssl_verification_error(error: BaseException) -> bool:
    if isinstance(error, ssl.SSLCertVerificationError):
        return True

    message = str(error)
    return (
        "CERTIFICATE_VERIFY_FAILED" in message
        or "certificate verify failed" in message.lower()
        or "self-signed certificate" in message.lower()
    )


class TBankInvestClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        retry_count: int = DEFAULT_RETRY_COUNT,
        user_agent: str = "t-invest-watchlist/1.0",
    ) -> None:
        self.token = resolve_api_token(token)
        self.timeout = timeout
        self.retry_count = retry_count
        self.user_agent = user_agent
        self._share_id_cache: dict[tuple[str, str], str] = {}
        self._index_id_cache: dict[str, str] = {}
        self._verify_ssl = True

    def _build_ssl_context(self) -> ssl.SSLContext:
        if self._verify_ssl:
            return ssl.create_default_context()
        return ssl._create_unverified_context()

    def _post(self, method_path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{REST_API_BASE_URL}/{method_path.lstrip('/')}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }

        for attempt in range(1, self.retry_count + 1):
            request = Request(url, data=body, headers=headers, method="POST")
            try:
                with urlopen(request, timeout=self.timeout, context=self._build_ssl_context()) as response:
                    return json.load(response)
            except HTTPError as exc:
                error_message = _extract_error_message(exc.read())
                if exc.code in {429, 500, 502, 503, 504} and attempt < self.retry_count:
                    retry_after = exc.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else 0.6 * (2 ** (attempt - 1))
                    time.sleep(delay)
                    continue
                detail = f": {error_message}" if error_message else ""
                raise TBankApiError(f"HTTP {exc.code} при запросе {url}{detail}") from exc
            except URLError as exc:
                if self._verify_ssl and _is_ssl_verification_error(exc.reason if exc.reason else exc):
                    self._verify_ssl = False
                    continue
                if attempt < self.retry_count:
                    time.sleep(0.6 * (2 ** (attempt - 1)))
                    continue
                raise TBankApiError(f"Ошибка сети при запросе {url}: {exc.reason}") from exc

        raise TBankApiError(f"Не удалось получить ответ от T-Invest API: {url}")

    def list_shares(
        self,
        *,
        instrument_status: str = "INSTRUMENT_STATUS_BASE",
    ) -> list[ShareInstrument]:
        payload = {"instrumentStatus": instrument_status}
        response = self._post("tinkoff.public.invest.api.contract.v1.InstrumentsService/Shares", payload)
        items = response.get("instruments") or response.get("shares") or []

        instruments: list[ShareInstrument] = []
        for item in items:
            ticker = str(_get_field(item, "ticker", default="") or "").upper().strip()
            class_code = str(_get_field(item, "classCode", "class_code", default="") or "").upper().strip()
            if not ticker or not class_code:
                continue

            lot_raw = _get_field(item, "lot", default=1)
            try:
                lot = int(lot_raw or 1)
            except (TypeError, ValueError):
                lot = 1

            instruments.append(
                ShareInstrument(
                    ticker=ticker,
                    class_code=class_code,
                    lot=max(lot, 1),
                    currency=str(_get_field(item, "currency", default="") or "").lower(),
                    uid=str(_get_field(item, "uid", "instrumentUid", "instrument_uid", default="") or "") or None,
                    api_trade_available=_to_bool(
                        _get_field(item, "apiTradeAvailableFlag", "api_trade_available_flag")
                    ),
                    buy_available=_to_bool(
                        _get_field(item, "buyAvailableFlag", "buy_available_flag")
                    ),
                    sell_available=_to_bool(
                        _get_field(item, "sellAvailableFlag", "sell_available_flag")
                    ),
                    short_enabled=_to_bool(
                        _get_field(item, "shortEnabledFlag", "short_enabled_flag")
                    ),
                    exchange=str(_get_field(item, "exchange", default="") or "") or None,
                    real_exchange=str(_get_field(item, "realExchange", "real_exchange", default="") or "") or None,
                )
            )
        return instruments

    def list_moex_shares(self, *, class_code: str = MOEX_CLASS_CODE) -> list[ShareInstrument]:
        shares: list[ShareInstrument] = []
        for item in self.list_shares():
            if item.class_code != class_code:
                continue
            if item.currency not in {"rub", "rur"}:
                continue
            if (
                item.api_trade_available is False
                and item.buy_available is False
                and item.sell_available is False
            ):
                continue
            shares.append(item)
        shares.sort(key=lambda instrument: instrument.ticker)
        return shares

    def find_instruments(
        self,
        query: str,
        *,
        instrument_kind: str | None = None,
        api_trade_available_flag: bool | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"query": query}
        if instrument_kind:
            payload["instrumentKind"] = instrument_kind
        if api_trade_available_flag is not None:
            payload["apiTradeAvailableFlag"] = api_trade_available_flag

        response = self._post("tinkoff.public.invest.api.contract.v1.InstrumentsService/FindInstrument", payload)
        return response.get("instruments") or response.get("items") or []

    def resolve_share_instrument_id(self, ticker: str, class_code: str = MOEX_CLASS_CODE) -> str:
        normalized_ticker = ticker.strip().upper()
        normalized_class = class_code.strip().upper()
        cache_key = (normalized_ticker, normalized_class)
        cached = self._share_id_cache.get(cache_key)
        if cached:
            return cached

        fallback = build_instrument_id(normalized_ticker, normalized_class)
        try:
            candidates = self.find_instruments(
                normalized_ticker,
                instrument_kind="INSTRUMENT_TYPE_SHARE",
                api_trade_available_flag=True,
            )
        except TBankApiError:
            self._share_id_cache[cache_key] = fallback
            return fallback

        best_id = fallback
        best_score = -1_000
        for item in candidates:
            item_ticker = str(_get_field(item, "ticker", default="") or "").upper()
            item_class_code = str(_get_field(item, "classCode", "class_code", default="") or "").upper()
            item_uid = str(_get_field(item, "uid", "instrumentUid", "instrument_uid", default="") or "") or None
            exchange = str(_get_field(item, "exchange", default="") or "").upper()
            real_exchange = str(_get_field(item, "realExchange", "real_exchange", default="") or "").upper()

            score = 0
            if item_ticker == normalized_ticker:
                score += 100
            if item_class_code == normalized_class:
                score += 60
            if "MOEX" in exchange or "MOEX" in real_exchange:
                score += 20
            if item_uid:
                score += 5
            elif item_ticker and item_class_code:
                score += 3

            if score > best_score:
                best_score = score
                best_id = item_uid or build_instrument_id(
                    item_ticker or normalized_ticker,
                    item_class_code or normalized_class,
                )

        self._share_id_cache[cache_key] = best_id
        return best_id

    def resolve_index_instrument_id(self, ticker: str, class_code_hint: str = MOEX_INDEX_CLASS_CODE) -> str:
        normalized_ticker = ticker.strip().upper()
        cached = self._index_id_cache.get(normalized_ticker)
        if cached:
            return cached

        fallback = build_instrument_id(normalized_ticker, class_code_hint)
        try:
            candidates = self.find_instruments(
                normalized_ticker,
                instrument_kind="INSTRUMENT_TYPE_INDEX",
            )
        except TBankApiError:
            self._index_id_cache[normalized_ticker] = fallback
            return fallback

        best_id = fallback
        best_score = -1_000
        for item in candidates:
            item_ticker = str(_get_field(item, "ticker", default="") or "").upper()
            item_class_code = str(_get_field(item, "classCode", "class_code", default="") or "").upper()
            item_uid = str(_get_field(item, "uid", "instrumentUid", "instrument_uid", default="") or "") or None
            exchange = str(_get_field(item, "exchange", default="") or "").upper()
            real_exchange = str(_get_field(item, "realExchange", "real_exchange", default="") or "").upper()

            score = 0
            if item_ticker == normalized_ticker:
                score += 100
            if item_class_code == class_code_hint:
                score += 35
            if "MOEX" in exchange or "MOEX" in real_exchange:
                score += 25
            if item_uid:
                score += 5

            if score > best_score:
                best_score = score
                best_id = item_uid or build_instrument_id(
                    item_ticker or normalized_ticker,
                    item_class_code or class_code_hint,
                )

        self._index_id_cache[normalized_ticker] = best_id
        return best_id

    def get_daily_candles(
        self,
        instrument_id: str,
        *,
        start_date: date,
        end_date: date,
    ) -> list[DailyCandle]:
        from_dt, to_dt = to_api_datetime_range(start_date, end_date)
        payload = {
            "instrumentId": instrument_id,
            "from": from_dt,
            "to": to_dt,
            "interval": "CANDLE_INTERVAL_DAY",
            "candleSourceType": "CANDLE_SOURCE_EXCHANGE",
        }
        response = self._post("tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles", payload)
        raw_candles = response.get("candles") or response.get("historicalCandles") or []

        candles_by_date: dict[date, DailyCandle] = {}
        for item in raw_candles:
            is_complete = _to_bool(_get_field(item, "isComplete", "is_complete", default=True))
            if is_complete is False:
                continue

            raw_time = _get_field(item, "time")
            if not raw_time:
                continue

            trade_date = parse_api_datetime(str(raw_time)).astimezone(MOSCOW_TZ).date()
            if trade_date < start_date or trade_date > end_date:
                continue

            candles_by_date[trade_date] = DailyCandle(
                trade_date=trade_date,
                open=quotation_to_float(_get_field(item, "open")),
                low=quotation_to_float(_get_field(item, "low")),
                high=quotation_to_float(_get_field(item, "high")),
                close=quotation_to_float(_get_field(item, "close")),
                volume_lots=int(_get_field(item, "volume", default=0) or 0),
            )

        return [candles_by_date[key] for key in sorted(candles_by_date)]
