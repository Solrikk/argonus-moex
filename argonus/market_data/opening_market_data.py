"""Read-only market collection and completed-day labels for opening models."""
from __future__ import annotations
from argonus.serialization import load_pickle_bytes

import gzip
import hashlib
import json
import pickle
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from argonus.strategies import continuous_intraday as core
from argonus.strategies import continuous_opening as scanner
from argonus.models import opening_profit_model as model
from argonus.market_data.tbank_market_data import parse_api_datetime, quotation_to_float

MOSCOW = ZoneInfo("Europe/Moscow")
from argonus.paths import PROJECT_ROOT as ROOT
RUNTIME = ROOT / "runtime/opening"


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    import os
    with temporary.open("w") as handle:
        json.dump(value, handle, ensure_ascii=False, allow_nan=False)
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def candle_rows(response, ds, now, lot):
    """Discard incomplete bars and retain only already ended Moscow bars.

    Broker volume is lots. Convert to shares, and keep a RUB turnover proxy
    in column seven; no current lot size is applied to historical archives.
    """
    rows = {}
    for c in response.get("candles") or []:
        if c.get("isComplete") is not True:
            continue
        stamp = parse_api_datetime(str(c["time"])).astimezone(MOSCOW)
        if stamp.date().isoformat() != ds or stamp + timedelta(minutes=5) > now:
            continue
        o, h, low, close = [quotation_to_float(c[k]) for k in ("open", "high", "low", "close")]
        volume = int(c.get("volume") or 0) * lot
        tm = stamp.strftime("%H:%M")
        if stamp.minute % 5 or min(o, h, low, close) <= 0 or low > min(o, close) or h < max(o, close):
            raise ValueError("Invalid live opening OHLC")
        rows[tm] = [tm, o, h, low, close, volume, volume * (h+low+close)/3]
    return [r for _, r in sorted(rows.items())]


class MarketCollector:
    def __init__(self, bot, symbols, directory=RUNTIME):
        self.bot, self.symbols, self.directory = bot, tuple(symbols), Path(directory)

    def collect(self, now, refresh_symbols=None):
        ds = now.date().isoformat()
        path = self.directory / "market" / f"{ds}.json"
        value = json.loads(path.read_text()) if path.exists() else {"date": ds, "infos": {}, "daily": {}, "sessions": {}}
        if value["date"] != ds:
            raise ValueError("Market snapshot date mismatch")
        errors = dict(value.get("errors", {})) if refresh_symbols is not None else {}
        for symbol in self.symbols if refresh_symbols is None else refresh_symbols:
            try:
                info = value["infos"].get(symbol)
                if not info:
                    info = self.bot.share_info(symbol)
                    value["infos"][symbol] = info
                if symbol not in value["daily"]:
                    daily = self.bot.client.get_daily_candles(info["uid"], start_date=now.date()-timedelta(days=65), end_date=now.date()-timedelta(days=1))
                    value["daily"][symbol] = [[c.trade_date.isoformat(), c.open, c.high, c.low, c.close,
                                                c.volume_lots*info["lot"], c.volume_lots*info["lot"]*c.close] for c in daily]
                response = self.bot._post("MarketDataService/GetCandles", {
                    "instrumentId": info["uid"], "interval": "CANDLE_INTERVAL_5_MIN",
                    "candleSourceType": "CANDLE_SOURCE_EXCHANGE",
                    "from": datetime.combine(now.date(), time(7), MOSCOW).astimezone(timezone.utc).isoformat(),
                    "to": now.astimezone(timezone.utc).isoformat(),
                })
                value["sessions"][symbol] = candle_rows(response, ds, now, info["lot"])
                errors.pop(symbol, None)
            except Exception as exc:
                # Never retain an older session snapshot as a fresh signal.
                value["sessions"].pop(symbol, None)
                errors[symbol] = f"{type(exc).__name__}: {exc}"
        value["observed_at"], value["errors"] = now.isoformat(), errors
        value["volume_contract"] = "broker lots converted to shares; OHLC typical-price RUB turnover proxy"
        write_json(path, value)
        contexts, rows = {}, {}
        for symbol, session in value["sessions"].items():
            if symbol not in self.symbols:
                continue
            past = [r for r in value["daily"].get(symbol, []) if r[0] < ds]
            ctx = core.prior_context(past, ds)
            if ctx:
                contexts[symbol] = {**ctx, "previous_close": past[-1][4]}
            for row in session:
                rows.setdefault(row[0], {})[("scanner", symbol)] = row
        return {"date": ds, "sessions": value["sessions"], "contexts": contexts,
                "rows": rows, "infos": value["infos"], "errors": errors}


def label(item, day):
    portfolio = core.EventPortfolio()
    portfolio.begin_day(item.date)
    portfolio.submit(item, day["contexts"])
    for minute in range(420, 1426, 5):
        tm = core.clock(minute)
        portfolio.step(tm, day["rows"].get(tm, {}))
        if minute >= 1115 and not portfolio.positions and not portfolio.pending:
            break
    portfolio.end_day()
    return portfolio.ledger[0]["net_return_pct"] if portfolio.ledger else 0.


def collect_training_day(day, directory=RUNTIME):
    ds = day["date"]
    path = Path(directory) / "training" / f"{ds}.json"
    if path.exists():
        return path  # completed training days are immutable
    # Incomplete market coverage cannot create a seemingly clean sample.
    if day["errors"]:
        raise ValueError("Cannot label day with failed market requests")
    result = []
    for minute in range(440, 571, 5):
        tm = core.clock(minute)
        items, market = scanner.scan(ds, tm, day["sessions"], day["contexts"])
        for item in items:
            result.append({"date": ds, "features": model.vector(item, market, day),
                           "net_return_pct": label(item, day)})
    value = {"date": ds, "feature_schema_sha256": hashlib.sha256((ROOT/"argonus/models/opening_profit_model.py").read_bytes()).hexdigest(),
             "mode": "completed_day_counterfactual_net_labels", "rows": result}
    write_json(path, value)
    return path


def training_rows(base_path, inference_month, directory=RUNTIME):
    with gzip.open(base_path, "rt") as handle:
        base = json.load(handle)["rows"]
    cutoff = inference_month + "-01"
    rows = [r for r in base if r["date"] < cutoff]
    known_days = {r["date"] for r in rows}
    schema = hashlib.sha256((ROOT/"argonus/models/opening_profit_model.py").read_bytes()).hexdigest()
    for path in sorted((Path(directory)/"training").glob("*.json")):
        if path.stem >= cutoff or path.stem in known_days:
            continue
        day = json.loads(path.read_text())
        if day["date"] != path.stem or day["feature_schema_sha256"] != schema or any(r["date"] != path.stem for r in day["rows"]):
            raise ValueError("Completed-day model training provenance mismatch")
        rows.extend(day["rows"])
    return rows


def monthly_models(base_path, month, directory=RUNTIME):
    path = Path(directory)/"models"/f"{month}.pkl"
    meta_path = path.with_suffix(".json")
    schema = hashlib.sha256((ROOT/"argonus/models/opening_profit_model.py").read_bytes()).hexdigest()
    base_sha = hashlib.sha256(Path(base_path).read_bytes()).hexdigest()
    if path.exists() and meta_path.exists():
        meta = json.loads(meta_path.read_text())
        if (meta.get("feature_schema_sha256") != schema or meta.get("base_sha256") != base_sha
                or meta["latest_training_date"] >= month+"-01"
                or meta.get("model_sha256") != hashlib.sha256(path.read_bytes()).hexdigest()):
            raise ValueError("Frozen monthly opening model changed")
        return load_pickle_bytes(path.read_bytes()), meta
    rows = training_rows(base_path, month, directory)
    models, meta = model.train_models(rows, month)
    meta.update({"feature_schema_sha256": schema, "base_sha256": base_sha})
    path.parent.mkdir(parents=True, exist_ok=True)
    content = pickle.dumps(models, protocol=5)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(content)
    import os
    os.replace(temporary, path)
    meta["model_sha256"] = hashlib.sha256(content).hexdigest()
    write_json(meta_path, meta)
    return models, meta
