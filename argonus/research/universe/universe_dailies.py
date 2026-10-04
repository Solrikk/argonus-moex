#!/usr/bin/env python3
"""Шаг 1 генератор-проекта: дневные свечи всей вселенной TQBR + ранжирование по
ликвидности. Пишет dailies/<SYM>.json и manifest.json (top-N + вотчлист-символы)."""
from __future__ import annotations

from argonus.paths import UNIVERSE_DIR, WATCHLIST_DIR, PROJECT_ROOT
import os, sys, json, time, statistics
from datetime import date

PROJ = str(PROJECT_ROOT)
from argonus.market_data.tbank_market_data import TBankInvestClient

HERE = str(UNIVERSE_DIR)
DAILIES = os.path.join(HERE, "dailies")
os.makedirs(DAILIES, exist_ok=True)
START = date(2025, 8, 1)     # запас для индикаторов до Nov-2025
END = date(2026, 7, 16)
TOP_N = 100

def main() -> int:
    client = TBankInvestClient(user_agent="universe-dailies/1.0")
    shares = client.list_moex_shares()
    print(f"вселенная TQBR: {len(shares)} бумаг")
    turnover: dict[str, float] = {}
    done = 0
    for sh in shares:
        sym = sh.ticker
        path = os.path.join(DAILIES, f"{sym}.json")
        try:
            if os.path.exists(path):
                rows = json.load(open(path))
            else:
                candles = client.get_daily_candles(sh.instrument_id, start_date=START, end_date=END)
                rows = [[c.trade_date.isoformat(), c.open, c.high, c.low, c.close, c.volume_lots]
                        for c in candles]
                json.dump(rows, open(path, "w"))
                time.sleep(0.12)
            # медианный дневной оборот в рублях (лоты * цена * размер лота неизвестен здесь,
            # используем lots*close как прокси — для РАНЖИРОВАНИЯ этого достаточно)
            recent = rows[-120:]
            if len(recent) >= 60:
                med = statistics.median(r[5] * r[4] for r in recent)
                turnover[sym] = med
        except Exception as exc:
            print(f"  ERR {sym}: {exc}")
        done += 1
        if done % 50 == 0:
            print(f"  {done}/{len(shares)}")
    ranked = sorted(turnover, key=lambda s: -turnover[s])
    top = ranked[:TOP_N]
    # плюс все символы, когда-либо попадавшие в вотчлисты
    wl_syms = set()
    for d in sorted(os.listdir(WATCHLIST_DIR)):
        if d.startswith("generated_watchlists_"):
            for f in os.listdir(os.path.join(WATCHLIST_DIR, d)):
                if f.startswith("watchlist_"):
                    for line in open(os.path.join(WATCHLIST_DIR, d, f), encoding="utf-8"):
                        line = line.strip()
                        if line and line.isupper() and 2 <= len(line) <= 6 and line.isalpha():
                            wl_syms.add(line)
    selected = sorted(set(top) | (wl_syms & set(turnover)))
    manifest = {"selected": selected, "top_by_turnover": top,
                "watchlist_symbols": sorted(wl_syms & set(turnover)),
                "start": START.isoformat(), "end": END.isoformat()}
    json.dump(manifest, open(os.path.join(HERE, "manifest.json"), "w"), ensure_ascii=False, indent=1)
    print(f"готово: dailies={len(turnover)}, выбрано для 5-мин загрузки: {len(selected)}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
