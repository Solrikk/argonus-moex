#!/usr/bin/env python3
"""Download public MOEX candles to a separate research cache, without credentials.

Twenty names are frozen by turnover *before* 2026-07-17. Minute candles are
aggregated into real 5m bars (never interpolate missing prices). Native MOEX
ruble turnover is retained as a seventh column. SSL verification stays on.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import statistics
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


from argonus.paths import PROJECT_ROOT as ROOT
FROM = "2026-07-17"
TILL = "2026-09-09"
TOP_N = 20


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    os.replace(temp, path)


def freeze_universe() -> list[str]:
    names = json.loads((ROOT / "data/intraday_universe/manifest.json").read_text())["selected"]
    scores = []
    for name in names:
        rows = json.loads((ROOT / "data/intraday_universe/dailies" / f"{name}.json").read_text())
        past = [r for r in rows if r[0] < FROM]
        if len(past) >= 20:
            scores.append((statistics.median(r[4] * r[5] for r in past[-20:]), name))
    return [name for _, name in sorted(scores, key=lambda x: (-x[0], x[1]))[:TOP_N]]


def fetch_pages(symbol: str, start: str, end: str, interval: int, cache: Path) -> list[list]:
    rows, offset = [], 0
    while True:
        page = cache / f"{symbol}_{interval}_{start}_{end}_{offset}.json"
        if page.exists():
            chunk = json.loads(page.read_text())
        else:
            params = urlencode({"iss.meta": "off", "from": start, "till": end, "interval": interval,
                                "start": offset, "candles.columns": "begin,open,high,low,close,volume,value"})
            url = f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/{symbol}/candles.json?{params}"
            for attempt in range(3):
                try:
                    request = Request(url, headers={"User-Agent": "Argonus-offline-research/1.0"})
                    with urlopen(request, timeout=25) as response:
                        table = json.load(response)["candles"]
                    if table["columns"] != ["begin", "open", "high", "low", "close", "volume", "value"]:
                        raise ValueError("Unexpected MOEX candle schema")
                    chunk = table["data"]
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    time.sleep(.5 * 2 ** attempt)
            write_json(page, chunk)
            time.sleep(.05)
        if not chunk:
            break
        if rows and chunk[0][0] <= rows[-1][0]:
            raise ValueError(f"MOEX pagination did not advance: {symbol}")
        if [r[0] for r in chunk] != sorted({r[0] for r in chunk}):
            raise ValueError(f"MOEX duplicated or unordered timestamps: {symbol}")
        rows.extend(chunk)
        offset += len(chunk)
        if len(chunk) < 500:
            break
    return rows


def aggregate_five_min(rows: list[list]) -> dict[str, list[list]]:
    groups = defaultdict(list)
    for row in rows:
        ts = datetime.fromisoformat(row[0])
        if not "09:50" <= ts.strftime("%H:%M") <= "18:39":
            continue
        key = (ts.date().isoformat(), f"{ts.hour:02d}:{ts.minute // 5 * 5:02d}")
        groups[key].append(row)
    days = defaultdict(list)
    for (ds, tm), bars in sorted(groups.items()):
        # No prices or volume are invented for minutes without trades.
        days[ds].append([tm, bars[0][1], max(r[2] for r in bars), min(r[3] for r in bars),
                         bars[-1][4], sum(r[5] for r in bars), sum(r[6] for r in bars)])
    return dict(days)


def fetch_symbol(symbol: str, directory: Path) -> dict:
    cache = directory / "pages"
    minute_rows = fetch_pages(symbol, FROM, TILL, 1, cache)
    daily_rows = fetch_pages(symbol, "2025-08-01", TILL, 24, cache)
    sessions = aggregate_five_min(minute_rows)
    candles = [[r[0][:10], *r[1:]] for r in daily_rows]
    write_json(directory / "dailies" / f"{symbol}.json", candles)
    path = directory / "five_min" / f"{symbol}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Fixed mtime yields stable hashes of identical downloads.
    path.write_bytes(gzip.compress(json.dumps(sessions, separators=(",", ":"), allow_nan=False).encode(), mtime=0))
    return {"symbol": symbol, "minute_rows": len(minute_rows), "daily_rows": len(candles),
            "sessions": len(sessions), "first_date": min(sessions) if sessions else None,
            "last_date": max(sessions) if sessions else None,
            "five_min_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "daily_sha256": hashlib.sha256((directory / "dailies" / f"{symbol}.json").read_bytes()).hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/backtests/moex_new_period_2026-09-10")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.workers <= 8:
        parser.error("workers must be between 1 and 8")
    names = freeze_universe()
    manifest = {"schema_version": 1, "mode": "public_data_only", "source": "https://iss.moex.com/iss/",
                "from": FROM, "till": TILL, "selected": names,
                "universe_rule": "Top 20 by last 20 daily median lots*close strictly before July 17",
                "selection_input_sha256": {f"data/intraday_universe/dailies/{s}.json":
                    hashlib.sha256((ROOT / "data/intraday_universe/dailies" / f"{s}.json").read_bytes()).hexdigest() for s in names},
                "volume_units": "native MOEX; column 7 is actual turnover in RUB",
                "retrieved_at": datetime.now(timezone.utc).isoformat(), "status": "collecting"}
    manifest_path = args.output_dir / "manifest.json"
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text())
        if any(previous[k] != manifest[k] for k in ("from", "till", "selected", "selection_input_sha256")):
            raise ValueError("Existing cache manifest belongs to a different research universe")
    write_json(manifest_path, manifest)
    successes, errors = [], []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch_symbol, symbol, args.output_dir): symbol for symbol in names}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                item = future.result()
                successes.append(item)
                print(f"{len(successes)}/{len(names)} {symbol}: {item['minute_rows']} minutes, {item['sessions']} sessions", flush=True)
            except Exception as exc:
                errors.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
                print(f"FAILED {symbol}: {type(exc).__name__}", flush=True)
    manifest.update(status="complete" if not errors else "incomplete", symbols=sorted(successes, key=lambda x: x["symbol"]), errors=errors)
    write_json(manifest_path, manifest)
    if errors:
        raise SystemExit("Incomplete dataset; rerun to resume cached pages")


if __name__ == "__main__":
    main()
