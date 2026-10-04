"""A_plus_mean live scanner: shared account, durable intents, depth-50 FOK.

The existing bot reconciles all uniquely named opening trades and their
protective orders. This module never calls the legacy one-trade entry route.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

from argonus.strategies import continuous_intraday as core
from argonus.strategies import continuous_opening as scanner
from argonus.market_data import opening_market_data as market_data
from argonus.models import opening_profit_model as predictor
from argonus.trading import production_entry_0705 as execution
from argonus.market_data.tbank_market_data import quotation_to_float

from argonus.paths import PROJECT_ROOT as ROOT
MANIFEST = ROOT / "config/opening_activation_manifest.json"
ROUTE = "opening_depth50_fok"
ACTIVE = {"entered", "entry_submitting", "exit_submitting", "entry_pending", "opening_pending"}
FILLED = {"entered", "closed", "exit_submitting"}
ENTRY_GRACE_SECONDS = 45
REQUIRED_ARTIFACTS = {"argonus/trading/production_opening.py", "argonus/market_data/opening_market_data.py", "argonus/models/opening_profit_model.py",
                      "argonus/strategies/continuous_opening.py", "argonus/strategies/continuous_intraday.py", "argonus/strategies/intraday_signals.py",
                      "argonus/trading/production_entry_0705.py", "argonus/trading/trade_bot.py", "scripts/run_tick.sh", "models/opening_model_training.json.gz"}
REQUIRED_ENV = {"BOT_OPENING_SCANNER": "1", "BOT_ACCOUNT_NAME": "YOUR_ACCOUNT_NAME", "BOT_SECOND_ENGINE": "0",
                "BOT_CONF_SIZING": "0", "BOT_PULLBACK_PCT": "0", "BOT_TARGET_POSITION_RUB": "150000",
                "BOT_MAX_LEVERAGE": "3.0", "BOT_TARGET_DISTANCE_MULTIPLIER": "1.25",
                "BOT_EXIT_TIME": "18:35", "TZ": "Europe/Moscow"}

REQUIRED_ARTIFACTS.update({"argonus/paths.py", "argonus/serialization.py"})


def requested():
    return os.environ.get("BOT_OPENING_SCANNER", "0") == "1"


def validate_manifest(today, path=MANIFEST):
    value = json.loads(Path(path).read_text())
    if (value.get("policy") != predictor.POLICY or value.get("approved") is not True
            or value.get("production_activation_allowed") is not True or today < value["effective_from"]):
        raise ValueError("Opening activation is not approved/effective")
    if value["contract"] != contract():
        raise ValueError("Opening execution contract differs")
    if len(value["universe"]) != 20 or len(set(value["universe"])) != 20:
        raise ValueError("Opening universe must contain the frozen 20 distinct names")
    if not REQUIRED_ARTIFACTS <= set(value["runtime_artifacts"]) or value["required_environment"] != REQUIRED_ENV:
        raise ValueError("Opening manifest is missing required runtime pins/settings")
    date.fromisoformat(value["effective_from"])
    if value["training_base"] != "models/opening_model_training.json.gz":
        raise ValueError("Unexpected opening training base")
    for name, sha in value["runtime_artifacts"].items():
        if not (ROOT/name).resolve().is_relative_to(ROOT):
            raise ValueError("Opening artifact outside workspace")
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest() != sha:
            raise ValueError(f"Opening artifact changed: {name}")
    for key, expected in value["required_environment"].items():
        if os.environ.get(key) != expected:
            raise ValueError(f"Opening runtime setting differs: {key}")
    return value


def contract():
    return {"policy": "A_plus_mean", "decision_window_msk": ["07:20", "09:30"],
            "decision_step_minutes": 5, "entry_delay_minutes": 5, "entry_grace_seconds": ENTRY_GRACE_SECONDS,
            "minimum_expected_net_pct": .15, "new_stop_pct": 1., "new_target_pct": 2.,
            "maximum_total_notional_rub": 150000., "maximum_leverage": 3.,
            "maximum_new_position_rub": 50000., "max_positions": 3,
            "minimum_new_position_rub": 5000.,
            "max_entries_per_day": 12, "max_entries_per_symbol": 2, "cooldown_minutes": 30,
            "daily_new_entry_cutoff_pct": -3., "maximum_planned_stop_risk_pct": 3.,
            "max_new_position_recent_turnover_fraction": .01, "max_entry_reference_deviation_pct": .25,
            "book_depth": 50, "max_book_age_ms": 3000, "max_vwap_impact_bps": 10.,
            "maximum_fee_side_pct": .04, "time_in_force": "TIME_IN_FORCE_FILL_OR_KILL",
            "monthly_training": "strictly before the inference month, fixed mean of ridge and boosting"}


def prepare_day(bot, api, now=None):
    now = now or datetime.now(market_data.MOSCOW)
    ds = now.date().isoformat()
    journal = api.load_journal()
    if journal and journal.get("date") != ds:
        if any(t.get("phase") in ACTIVE-{"opening_pending"} for t in journal["trades"]):
            return False
        if bot.live:
            market_data.write_json(market_data.RUNTIME/"journals"/f"{journal['date']}.json", journal)
        journal = None
    if journal and journal.get("opening", {}).get("day_start_equity_rub"):
        return True
    snapshot = bot.portfolio_snapshot()
    if not math.isfinite(snapshot["equity_rub"]) or snapshot["equity_rub"] <= 0:
        raise ValueError("Opening day needs a positive account-equity snapshot")
    journal = journal or {"date": ds, "trades": []}
    journal["opening"] = {"policy": predictor.POLICY, "day_start_equity_rub": snapshot["equity_rub"],
                           "last_decision": None, "initialized_at": now.isoformat()}
    api.write_state(bot.live, journal)
    return True


def portfolio_capacity(snapshot, trades, contexts, item=None, own_engine=None, day_start=None, now=None):
    eq = float(snapshot["equity_rub"])
    if not math.isfinite(eq) or eq <= 0:
        raise ValueError("Unknown or non-positive portfolio equity")
    managed = {t.get("uid"): t for t in trades if t.get("phase") in ACTIVE and t.get("phase") != "opening_pending"}
    positions, gross, risk = {}, 0., 0.
    for p in snapshot["positions"]:
        uid = p.get("instrumentUid") or p.get("figi")
        t = managed.get(uid)
        qty, price = quotation_to_float(p.get("quantity")), quotation_to_float(p.get("currentPrice"))
        if not t or t.get("adopted") or not math.isfinite(qty*price) or qty == 0 or price <= 0:
            raise ValueError("Unmanaged or unpriced portfolio position")
        if (qty > 0) != (t["direction"] == "long"):
            raise ValueError("Position direction contradicts opening journal")
        value = abs(qty*price)
        gross += value
        risk += max(value, float(t.get("actual_position_rub") or value)) * float(t.get("stop_pct") or 1.)/100
        positions[t["symbol"]] = t
    pending = [t for t in trades if t.get("phase") in ACTIVE and t.get("engine") != own_engine
               and t.get("uid") not in {p.get("instrumentUid") or p.get("figi") for p in snapshot["positions"]}]
    if any(t.get("phase") not in ("opening_pending",) for t in pending):
        raise ValueError("Unresolved entry/exit request blocks new opening entries")
    reserved = sum(float(t["notional_reserve_rub"]) for t in pending)
    reserved_risk = sum(float(t["notional_reserve_rub"])*float(t.get("stop_pct") or 1)/100 for t in pending)
    occupied = set(positions) | {t["symbol"] for t in pending}
    if day_start is None or not math.isfinite(day_start) or day_start <= 0:
        raise ValueError("Missing day-start equity")
    if eq <= .97*day_start:
        return 0.
    fills = [t for t in trades if t.get("phase") in FILLED and float(t.get("entry_price") or 0)>0]
    if len(occupied) >= 3 or len(fills)+len(pending) >= 12:
        return 0.
    if item:
        if item.symbol in occupied or sum(t.get("symbol")==item.symbol for t in fills)>=2:
            return 0.
        if now:
            for t in fills:
                if t.get("symbol") != item.symbol or t.get("phase") != "closed":
                    continue
                stamp = datetime.fromisoformat(t["closed_at"])
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=market_data.MOSCOW)
                if now < stamp+timedelta(minutes=30):
                    return 0.
        for t in [*positions.values(), *pending]:
            if t["direction"] == item.direction and (t["symbol"] not in contexts or item.symbol not in contexts
                    or core.correlated(item.symbol, t["symbol"], contexts)):
                return 0.
    if snapshot.get("other_exposure"):
        raise ValueError("Non-share account exposure is outside the opening contract")
    stop = item.stop_pct if item else 1.
    return max(0., min(50000., min(150000., 3*eq)-gross-reserved,
                       (.03*eq-risk-reserved_risk)/(stop/100),
                       .01*item.recent_turnover if item else 50000.))


def _journal_day(api, ds):
    journal = api.load_journal()
    if not journal or journal["date"] != ds:
        raise ValueError("Opening journal is not initialized for this date")
    return journal


def _skip(bot, api, trade, reason):
    api.upsert_trade(bot.live, trade["date"], {**trade, "phase": "skipped", "reason": reason})
    api.log(f"OPENING {trade['symbol']}: {reason}")
    return 0


def _freshness(now, trade):
    due = datetime.fromisoformat(trade["opening_due_at"])
    return due <= now <= due+timedelta(seconds=ENTRY_GRACE_SECONDS)


def primary_entry_guard(bot, api, ds):
    """The 07:05 primary must never stack its 150k on an existing portfolio."""
    validate_manifest(ds)
    if not bot.live:
        return
    if not prepare_day(bot, api):
        raise ValueError("Previous-day requests must be reconciled before the primary entry")
    snapshot = bot.portfolio_snapshot()
    journal = _journal_day(api, ds)
    if snapshot["positions"] or snapshot.get("other_exposure") or bot.active_orders() or bot.stop_orders():
        raise ValueError("Primary entry requires the shared portfolio to be flat")
    if any(t.get("phase") in ACTIVE for t in journal["trades"]):
        raise ValueError("Reserved/uncertain shared exposure blocks the primary entry")


def held_bars_complete(day, journal, tm):
    previous = core.clock(int(tm[:2])*60+int(tm[3:])-5)
    for t in journal["trades"]:
        if t.get("phase") == "entered":
            session = day["sessions"].get(t["symbol"], [])
            if not session or session[-1][0] < previous:
                return False
    return True


def submit_due(bot, api, trade, day, now):
    """One exact-time FOK request, saved before submission; no order retry."""
    if not _freshness(now, trade):
        return _skip(bot, api, trade, "intent expired; no late entry")
    item = core.Opportunity(**trade["opportunity"])
    info = bot.share_info(item.symbol)
    if info["uid"] != trade["uid"] or info["lot"] != trade["lot_size"]:
        return _skip(bot, api, trade, "instrument identity/lot changed")
    if not info["api_trade_available"] or (item.direction == "short" and not info["short_enabled"]):
        return _skip(bot, api, trade, "instrument cannot be traded in this direction")
    status = bot.trading_status(info["uid"])
    if not status["limit_order_available"]:
        return _skip(bot, api, trade, "limit orders unavailable")
    if bot.active_orders():
        return _skip(bot, api, trade, "outstanding broker orders block new entry")
    journal = _journal_day(api, item.date)
    amount = portfolio_capacity(bot.portfolio_snapshot(), journal["trades"], day["contexts"], item,
                                trade["engine"], journal["opening"]["day_start_equity_rub"], now)
    amount = min(amount, trade["notional_reserve_rub"])
    # A failed cancellation of an old paired STOP/TAKE must not reopen or
    # reverse the repeated trade. Retry only our known, already closed pairs.
    closed_ids = {t.get(key) for t in journal["trades"] if t.get("phase")=="closed"
                  for key in ("stop_order_id", "take_order_id") if t.get(key)}
    for stop in bot.stop_orders():
        if (stop.get("instrumentUid") or stop.get("figi")) != info["uid"]:
            continue
        if stop.get("stopOrderId") not in closed_ids:
            return _skip(bot, api, trade, "unattributed protective order blocks repeat entry")
        bot._post("StopOrdersService/CancelStopOrder", {"accountId": bot.account_id, "stopOrderId": stop["stopOrderId"]})
    if any((s.get("instrumentUid") or s.get("figi"))==info["uid"] for s in bot.stop_orders()):
        return _skip(bot, api, trade, "old protective order cancellation not confirmed")
    lots = int(amount//(item.reference*info["lot"]))
    lots = min(lots, bot.max_order_lots(info["uid"], item.direction, item.reference*1.0025))
    if lots < 1 or amount < 5000:
        return _skip(bot, api, trade, "shared portfolio has insufficient capacity")
    sign = 1 if item.direction == "long" else -1
    target_guard = item.reference*(1+sign*.02)
    # Fee estimation is read-only and precedes the final fresh book.
    fee = bot.estimate_order_price(info["uid"], lots, "buy" if sign>0 else "sell", item.reference)
    if fee.get("fee_side_pct") is None or fee["fee_side_pct"] > .040000001:
        return _skip(bot, api, trade, "broker commission exceeds researched 0.04% per side")
    book = execution.normalize_orderbook(bot.order_book(info["uid"], 50), expected_uid=info["uid"],
                                         expected_symbol=item.symbol, expected_depth=50)

    def quote(clock):
        return execution.build_execution_quote(book, direction=item.direction, requested_lots=lots,
                                                target_price=target_guard, received_at=clock,
                                                max_quote_age_ms=3000, max_impact_bps=10.)
    q = quote(datetime.now(market_data.MOSCOW))
    limit = execution.round_marketable_limit(q["limit_price"], info["min_price_increment"], item.direction)
    lots = min(lots, int(amount//(max(limit, q["best_touch"])*info["lot"])))
    if lots < 1:
        return _skip(bot, api, trade, "RUB cap does not cover a whole lot")
    q = quote(datetime.now(market_data.MOSCOW))
    limit = execution.round_marketable_limit(q["limit_price"], info["min_price_increment"], item.direction)
    if lots*info["lot"]*q["executable_vwap"] < 5000:
        return _skip(bot, api, trade, "whole-lot broker capacity is below 5,000 RUB")
    if max(abs(q["executable_vwap"]/item.reference-1), abs(limit/item.reference-1))*100 > .25:
        return _skip(bot, api, trade, "entry price is beyond 0.25% of the closed signal")
    request_id = str(uuid.uuid4())
    frozen = {**trade, "phase": "entry_submitting", "entry_route": ROUTE, "entry_order_id": request_id,
              "entry_submit_price": limit, "entry_submit_lots": lots, "lots": lots,
              "reference_price": q["executable_vwap"], "actual_position_rub": lots*info["lot"]*q["executable_vwap"],
              "target_price": None, "target_pct": 2., "stop_pct": 1., "confirm_margin_trade": True,
              "entry_submitted_at": datetime.now(market_data.MOSCOW).isoformat(),
              "entry_time_in_force": execution.TIME_IN_FORCE, "entry_quote": q}
    api.upsert_trade(bot.live, item.date, frozen)
    if not bot.live:
        api.log(f"DRY-RUN OPENING FOK: {item.symbol} {item.direction} {lots} lots")
        return 0
    stamp = datetime.now(market_data.MOSCOW)
    if not _freshness(stamp, trade):
        return _skip(bot, api, frozen, "intent expired after durable write; no submission")
    final = quote(stamp)
    final_limit = execution.round_marketable_limit(final["limit_price"], info["min_price_increment"], item.direction)
    if not math.isclose(final_limit, limit, abs_tol=1e-12, rel_tol=0):
        return _skip(bot, api, frozen, "quote limit changed after durable write")
    try:
        _, response = bot.place_fok_limit(info["uid"], lots, "buy" if sign>0 else "sell", limit,
                                         request_id=request_id, confirm_margin_trade=True)
    except Exception as exc:
        api.log(f"OPENING request {request_id}: uncertain ACK, reconcile only: {exc}")
        return 1
    status = response.get("executionReportStatus")
    executed = int(response.get("lotsExecuted") or 0)
    if executed > lots:
        api.log("OPENING fill exceeds frozen lots; blocked for manual reconciliation")
        return 1
    if status == "EXECUTION_REPORT_STATUS_FILL" or executed > 0:
        if status not in api.TERMINAL_ORDER_STATUSES:
            # A nonterminal partial requires cancellation/reconciliation first.
            return 1
        api.finalize_entry(bot, frozen, executed or lots,
                           quotation_to_float(response.get("executedOrderPrice")) or q["executable_vwap"],
                           entry_request_id=request_id)
        return 0
    if status in api.TERMINAL_ORDER_STATUSES:
        return _skip(bot, api, frozen, f"FOK terminal={status}; zero fill")
    return 1


def tick(bot, api, now=None):
    now = now or datetime.now(market_data.MOSCOW)
    ds, tm = now.date().isoformat(), core.clock(now.hour*60+now.minute//5*5)
    if not bot.live:
        api.log("DRY-RUN A_plus_mean: persistent scanner intents/orders disabled; use the paper replay CLI")
        return 0
    if now.weekday()>=5 or not "07:10" <= tm <= "23:45":
        return 0
    manifest = validate_manifest(ds)
    if not prepare_day(bot, api, now):
        return 1
    journal = _journal_day(api, ds)
    if tm < "07:20":
        if journal["opening"].get("last_warmup") != tm:
            day = market_data.MarketCollector(bot, manifest["universe"]).collect(now)
            market_data.monthly_models(ROOT/manifest["training_base"], ds[:7])
            if day["errors"]:
                return 1
            journal["opening"]["last_warmup"] = tm
            api.write_state(bot.live, journal)
        return 0
    for t in journal["trades"]:
        if t.get("phase")=="opening_pending" and now>datetime.fromisoformat(t["opening_due_at"])+timedelta(seconds=ENTRY_GRACE_SECONDS):
            _skip(bot, api, t, "intent expired; no late entry")
    journal = _journal_day(api, ds)
    # After the deadline positions have already been managed by the main bot.
    training_path = market_data.RUNTIME/"training"/f"{ds}.json"
    if tm >= "18:40":
        if bot.live and not training_path.exists():
            day = market_data.MarketCollector(bot, manifest["universe"]).collect(now)
            market_data.collect_training_day(day)
        return 0
    due = [t for t in journal["trades"] if t.get("phase")=="opening_pending"
           and datetime.fromisoformat(t["opening_due_at"])<=now]
    decision_needed = "07:20" <= tm <= "09:30" and journal["opening"].get("last_decision")!=tm
    if not due and not decision_needed:
        return 0
    collector = market_data.MarketCollector(bot, manifest["universe"])
    if due:
        # Execute already frozen intents before the twenty-symbol rescan.
        # Only held names need fresh bars; entry itself uses a fresh broker book.
        held = [t["symbol"] for t in journal["trades"] if t.get("phase")=="entered"]
        day = collector.collect(now, refresh_symbols=held)
        if day["errors"] or not held_bars_complete(day, journal, tm):
            api.log("OPENING: incomplete held/signal snapshot blocks due entries")
            return 1
        for trade in due:
            if submit_due(bot, api, trade, day, now):
                return 1
    if not decision_needed:
        return 0
    day = collector.collect(now)
    journal = _journal_day(api, ds)
    if day["errors"] or not held_bars_complete(day, journal, tm):
        api.log("OPENING: incomplete universe/held bars; new signals deferred")
        return 1
    journal = _journal_day(api, ds)
    models, provenance = market_data.monthly_models(ROOT/manifest["training_base"], ds[:7])
    items, market = scanner.scan(ds, tm, day["sessions"], day["contexts"])
    ranked, forecasts = predictor.rank_opportunities(items, market, day, models)
    snapshot = bot.portfolio_snapshot()
    if bot.active_orders():
        return 1
    for item in ranked:
        journal = _journal_day(api, ds)
        amount = portfolio_capacity(snapshot, journal["trades"], day["contexts"], item,
                                    day_start=journal["opening"]["day_start_equity_rub"], now=now)
        if amount < 5000:
            continue
        info = day["infos"][item.symbol]
        engine = "OPEN:"+hashlib.sha256(json.dumps(item.identity).encode()).hexdigest()[:20]
        if any(t.get("engine")==engine for t in journal["trades"]):
            continue
        due_at = datetime.combine(now.date(), datetime.strptime(tm,"%H:%M").time(), market_data.MOSCOW)+timedelta(minutes=5)
        record = {"date": ds, "engine": engine, "phase": "opening_pending", "strategy": predictor.POLICY,
                  "symbol": item.symbol, "direction": item.direction, "uid": info["uid"], "lot_size": info["lot"],
                  "increment": info["min_price_increment"], "stop_pct": 1., "target_pct": 2.,
                  "opening_due_at": due_at.isoformat(), "opportunity": asdict(item),
                  "notional_reserve_rub": amount, "forecast": forecasts[item.identity],
                  "model_provenance": provenance, "opening_manifest_sha256": hashlib.sha256(MANIFEST.read_bytes()).hexdigest()}
        api.upsert_trade(bot.live, ds, record)
    if bot.live:
        journal = _journal_day(api, ds)
        journal["opening"]["last_decision"] = tm
        api.write_state(True, journal)
    api.log(f"OPENING {tm}: {len(items)} setups, {len(ranked)} positive-net forecasts")
    return 0
