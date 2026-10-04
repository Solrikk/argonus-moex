#!/usr/bin/env python3
"""Шаг 2 генератор-проекта: 5-минутные свечи для manifest.selected за Nov'25–Jul'26.
Возобновляемый (кэш five_min/<SYM>.json.gz), прогресс в fetch.log,
жёсткий стоп в 06:00 МСК — чтобы не делить лимиты API с живым ботом утром."""
from __future__ import annotations

from argonus.paths import UNIVERSE_DIR, WATCHLIST_DIR, PROJECT_ROOT
import os, sys, json, gzip, time
from datetime import date, datetime

PROJ = str(PROJECT_ROOT)
from argonus.market_data.tbank_market_data import (TBankInvestClient, to_api_datetime_range, parse_api_datetime,
                               quotation_to_float, _get_field, MOSCOW_TZ)

HERE = str(UNIVERSE_DIR)
FM = os.path.join(HERE, "five_min")
os.makedirs(FM, exist_ok=True)
LOG = os.path.join(HERE, "fetch.log")
W_START, W_END = "2025-11-01", "2026-07-16"

def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as fh:
        fh.write(line + "\n")

def hard_stop() -> bool:
    now = datetime.now()
    return now.hour >= 6 and now.hour < 20 and now.strftime("%H:%M") >= "06:00" and now.strftime("%H:%M") < "20:00" and False or (now.strftime("%H:%M") >= "06:00" and now.strftime("%H:%M") <= "06:59")

def main() -> int:
    manifest = json.load(open(os.path.join(HERE, "manifest.json")))
    symbols = manifest["selected"]
    client = TBankInvestClient(user_agent="universe-5min/1.0")
    total_req = 0
    for si, sym in enumerate(symbols):
        gz = os.path.join(FM, f"{sym}.json.gz")
        cache = {}
        if os.path.exists(gz):
            try:
                cache = json.loads(gzip.open(gz, "rt").read())
            except Exception:
                cache = {}
        try:
            dailies = json.load(open(os.path.join(HERE, "dailies", f"{sym}.json")))
        except Exception:
            continue
        days = [r[0] for r in dailies if W_START <= r[0] <= W_END]
        missing = [d for d in days if d not in cache]
        if not missing:
            continue
        try:
            iid = client.resolve_share_instrument_id(sym, "TQBR")
        except Exception as exc:
            log(f"{sym}: resolve err {exc}")
            continue
        err = 0
        for di, ds in enumerate(missing):
            if hard_stop():
                json.dump  # noop
                with gzip.open(gz, "wt") as fh:
                    fh.write(json.dumps(cache))
                log(f"HARD STOP 06:00 — сохранено, выходим ({sym} {di}/{len(missing)})")
                return 0
            try:
                y, m, dd = map(int, ds.split("-"))
                f, t = to_api_datetime_range(date(y, m, dd), date(y, m, dd))
                resp = client._post(
                    "tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles",
                    {"instrumentId": iid, "from": f, "to": t,
                     "interval": "CANDLE_INTERVAL_5_MIN",
                     "candleSourceType": "CANDLE_SOURCE_EXCHANGE"})
                rows = []
                for item in resp.get("candles") or []:
                    ts = parse_api_datetime(str(_get_field(item, "time"))).astimezone(MOSCOW_TZ)
                    rows.append([ts.strftime("%H:%M"),
                                 quotation_to_float(_get_field(item, "open")),
                                 quotation_to_float(_get_field(item, "high")),
                                 quotation_to_float(_get_field(item, "low")),
                                 quotation_to_float(_get_field(item, "close")),
                                 int(_get_field(item, "volume", default=0) or 0)])
                cache[ds] = rows
                total_req += 1
            except Exception as exc:
                err += 1
                if err <= 3:
                    log(f"{sym} {ds}: {exc}")
                if err > 20:
                    log(f"{sym}: слишком много ошибок, дальше")
                    break
                time.sleep(1.0)
            time.sleep(0.15)
            if (di + 1) % 60 == 0:
                with gzip.open(gz, "wt") as fh:
                    fh.write(json.dumps(cache))
        with gzip.open(gz, "wt") as fh:
            fh.write(json.dumps(cache))
        log(f"{sym}: {len(cache)}/{len(days)} дней ({si+1}/{len(symbols)}), всего запросов {total_req}")
    log(f"ГОТОВО: {len(symbols)} бумаг, запросов {total_req}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
