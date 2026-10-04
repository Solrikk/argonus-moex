"""Shared-capital and order-recovery contracts; every broker is an offline fake."""
import copy
import gzip
import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from argonus.strategies import continuous_intraday as core
from argonus.market_data import opening_market_data as data
from argonus.models import opening_profit_model as model
from argonus.trading import production_opening as live
from argonus.research import research_opening_expectancy as research
from argonus.runtime import run_candidate_loop as loop
from argonus.trading import trade_bot
from tests.test_opening_research import fixture
from tests.test_production_entry_0705 import quotation, level

DS = "2026-10-05"
NOW = datetime(2026, 10, 5, 7, 25, 1, tzinfo=data.MOSCOW)


def opportunity(symbol="AAA", direction="long"):
    return core.Opportunity(DS, symbol, direction, "opening_follow", "07:20", 100., .3,
                            1., 2., 10000000.)


def intent(item=None):
    item = item or opportunity()
    return {"date": DS, "engine": "OPEN:"+item.symbol, "phase": "opening_pending",
            "symbol": item.symbol, "direction": item.direction, "uid": "uid-"+item.symbol,
            "lot_size": 1, "increment": .01, "opportunity": asdict(item),
            "notional_reserve_rub": 50000., "opening_due_at": NOW.replace(second=0).isoformat(),
            "stop_pct": 1., "target_pct": 2.}


class Journal:
    TERMINAL_ORDER_STATUSES = trade_bot.TERMINAL_ORDER_STATUSES

    def __init__(self, trades=()):
        self.value = {"date": DS, "trades": copy.deepcopy(list(trades)),
                      "opening": {"day_start_equity_rub": 50000., "last_decision": "07:20"}}
        self.writes = []
        self.log = Mock()

    def load_journal(self):
        return copy.deepcopy(self.value)

    def write_state(self, enabled, value):
        if enabled:
            self.value = copy.deepcopy(value)

    def upsert_trade(self, enabled, day, trade):
        if not enabled:
            return
        self.writes.append(copy.deepcopy(trade))
        self.value["trades"] = [t for t in self.value["trades"] if t["engine"] != trade["engine"]]+[copy.deepcopy(trade)]

    def finalize_entry(self, bot, trade, lots, price, **kwargs):
        with patch.object(trade_bot, "upsert_trade", self.upsert_trade), patch.object(trade_bot, "log"), patch.object(trade_bot, "FORWARD_SHADOW_ENABLED", False):
            trade_bot.finalize_entry(bot, trade, lots, price, **kwargs)


class FakeBroker:
    live = True
    account_id = "offline-account"

    def __init__(self, journal, *, ack_lost=False, fill=500, stop_fails=False):
        self.journal, self.ack_lost, self.fill, self.stop_fails = journal, ack_lost, fill, stop_fails
        self.posts, self.protection = [], []
        self.maximum_lots = 500

    def portfolio_snapshot(self):
        return {"equity_rub": 50000., "positions": [], "other_exposure": False}

    def active_orders(self):
        return []

    def stop_orders(self):
        return []

    def share_info(self, symbol):
        return {"uid": "uid-"+symbol, "lot": 1, "api_trade_available": True,
                "short_enabled": True, "min_price_increment": .01}

    def trading_status(self, uid):
        return {"limit_order_available": True}

    def max_order_lots(self, *args):
        return self.maximum_lots

    def estimate_order_price(self, *args):
        return {"fee_side_pct": .04}

    def order_book(self, uid, depth):
        return {"instrumentUid": uid, "ticker": uid.removeprefix("uid-"), "depth": 50,
                "orderbookTs": "2026-10-05T04:25:00Z", "bids": [level(99.99, 1000)], "asks": [level(100., 1000)]}

    def place_fok_limit(self, uid, lots, direction, price, **kwargs):
        frozen = self.journal.writes[-1]
        assert frozen["phase"] == "entry_submitting"
        assert frozen["entry_order_id"] == kwargs["request_id"]
        assert frozen["lots"] == lots
        self.posts.append((uid, lots, direction, price, kwargs))
        if self.ack_lost:
            raise TimeoutError("offline lost ACK")
        status = "EXECUTION_REPORT_STATUS_FILL" if self.fill == lots else "EXECUTION_REPORT_STATUS_CANCELLED"
        return kwargs["request_id"], {"executionReportStatus": status, "lotsExecuted": self.fill,
                                     "executedOrderPrice": quotation(price)}

    def stop_order(self, uid, lots, direction, price, kind):
        assert self.journal.writes[-1]["phase"] == "entered"
        self.protection.append((lots, direction, price, kind))
        if kind == "STOP_LOSS" and self.stop_fails:
            raise RuntimeError("offline stop failure")
        return kind+"-id"


class ModelContractTests(unittest.TestCase):
    def test_features_equal_research_and_future_candles_do_not_change_them(self):
        _, _, items, market, day = fixture()
        a = model.vector(items[0], market, day)
        self.assertEqual(a, research.vector(items[0], market, day))
        day["rows"]["07:20"] = {("scanner", "AAA"): ["07:20", 1, 10000, .1, 5000, 999999]}
        self.assertEqual(a, model.vector(items[0], market, day))

    def test_current_and_future_month_labels_cannot_enter_training(self):
        rows = [{"date": f"2026-01-{i:02d}", "features": [i/100]*19, "net_return_pct": i/100} for i in range(1, 21)]
        future = {"date": "2026-02-01", "features": [float("nan")]*19, "net_return_pct": 1e9}
        models, meta = model.train_models(rows+[future], "2026-02")
        self.assertEqual(meta["latest_training_date"], "2026-01-20")
        x = model.np.array([rows[0]["features"], rows[-1]["features"]])
        for name in model.LEARNERS:
            expected = research.fit(name, rows, "2026-01-31")
            model.np.testing.assert_allclose(models[name].predict(x), expected.predict(x), rtol=0, atol=1e-12)

    def test_missing_warmup_and_nan_past_labels_fail(self):
        with self.assertRaises(ValueError):
            model.train_models([], "2026-02")
        rows = [{"date": f"2026-01-{i:02d}", "features": [0]*19, "net_return_pct": float("nan")} for i in range(1,21)]
        with self.assertRaises(ValueError):
            model.train_models(rows, "2026-02")


class SharedPortfolioTests(unittest.TestCase):
    def capacity(self, equity=50000., trades=(), positions=(), item=None, **kwargs):
        return live.portfolio_capacity({"equity_rub": equity, "positions": list(positions)}, list(trades), {},
                                       item or opportunity(), day_start=50000., now=NOW, **kwargs)

    def test_pending_intents_reserve_capital_and_slots(self):
        self.assertEqual(self.capacity(), 50000.)
        self.assertEqual(self.capacity(trades=[intent()], item=opportunity("BBB", "short")), 50000.)
        trades = [intent(opportunity(s, "short")) for s in ("AAA", "BBB", "CCC")]
        self.assertEqual(self.capacity(trades=trades, item=opportunity("DDD")), 0.)

    def test_daily_loss_cutoff_and_same_symbol_block(self):
        self.assertEqual(self.capacity(equity=48500.), 0.)
        self.assertEqual(self.capacity(trades=[intent()]), 0.)

    def test_pending_own_reservation_is_released_only_for_its_submission(self):
        self.assertEqual(self.capacity(trades=[intent()], own_engine="OPEN:AAA"), 50000.)

    def test_short_gross_uses_net_equity_and_stop_risk_limits(self):
        t = {**intent(opportunity("BBB", "short")), "phase": "entered", "entry_price": 100.,
             "actual_position_rub": 100000., "stop_pct": 1.}
        p = {"instrumentUid": t["uid"], "quantity": quotation(-1000), "currentPrice": quotation(100)}
        self.assertEqual(self.capacity(trades=[t], positions=[p]), 50000.)
        t["stop_pct"] = 1.4
        self.assertAlmostEqual(self.capacity(trades=[t], positions=[p]), 10000.)

    def test_unknown_exposure_or_unresolved_ack_blocks(self):
        p = {"instrumentUid": "unknown", "quantity": quotation(1), "currentPrice": quotation(100)}
        with self.assertRaises(ValueError):
            self.capacity(positions=[p])
        with self.assertRaises(ValueError):
            self.capacity(trades=[{**intent(), "phase": "entry_submitting"}])
        with self.assertRaises(ValueError):
            live.portfolio_capacity({"equity_rub": 50000., "positions": [], "other_exposure": True}, [], {}, opportunity(), day_start=50000.)

    def test_repeat_limit_and_cooldown_include_completed_entries(self):
        closed = {**intent(), "phase": "closed", "entry_price": 100., "closed_at": NOW.isoformat()}
        self.assertEqual(self.capacity(trades=[closed]), 0.)
        closed["closed_at"] = (NOW-timedelta(minutes=31)).isoformat()
        self.assertEqual(self.capacity(trades=[closed]), 50000.)
        self.assertEqual(self.capacity(trades=[closed, {**closed, "engine": "OPEN:2"}]), 0.)
        dozen = [{**closed, "symbol": f"S{i}", "engine": f"OPEN:{i}"} for i in range(12)]
        self.assertEqual(self.capacity(trades=dozen), 0.)

    def test_missing_correlation_history_cannot_authorize_same_direction(self):
        self.assertEqual(self.capacity(trades=[intent()], item=opportunity("BBB")), 0.)

    def test_portfolio_snapshot_uses_total_equity_not_short_cash(self):
        bot = object.__new__(trade_bot.TradeBot)
        bot.account_id = "offline"
        bot._post = Mock(return_value={"totalAmountPortfolio": quotation(50000), "positions": [
            {"figi": trade_bot.RUB_POSITION_FIGI, "quantity": quotation(200000), "instrumentType": "currency"},
            {"instrumentUid": "uid-AAA", "quantity": quotation(-1500), "currentPrice": quotation(100), "instrumentType": "share"}]})
        value = bot.portfolio_snapshot()
        self.assertEqual(value["equity_rub"], 50000.)
        self.assertFalse(value["other_exposure"])

    def test_unique_engines_preserve_closed_trades_and_day_metadata(self):
        journal = Journal([{**intent(), "phase": "closed"}])
        with patch.object(trade_bot, "load_journal", journal.load_journal), patch.object(trade_bot, "write_state", journal.write_state):
            trade_bot.upsert_trade(True, DS, {**intent(), "engine": "OPEN:next"})
        self.assertEqual(len(journal.value["trades"]), 2)
        self.assertEqual(journal.value["opening"]["day_start_equity_rub"], 50000.)

    def test_exit_quantity_without_quantity_lots_respects_instrument_lot_size(self):
        self.assertEqual(trade_bot.position_lot_count({"quantity": quotation(-500)},10),50)
        self.assertEqual(trade_bot.position_lot_count({"quantity": quotation(500),"quantityLots":quotation(50)},10),50)
        with self.assertRaises(ValueError):
            trade_bot.position_lot_count({"quantity": quotation(501)},10)


class OrderLifecycleTests(unittest.TestCase):
    def run_due(self, broker, journal, trade=None, now=NOW):
        with patch.object(live, "datetime") as clock:
            clock.fromisoformat = datetime.fromisoformat
            clock.now.return_value = now
            return live.submit_due(broker, journal, trade or intent(), {"contexts": {}}, now)

    def test_fill_is_durable_then_stop_then_take_from_actual_fill(self):
        j = Journal([intent()]); b = FakeBroker(j)
        self.assertEqual(self.run_due(b, j), 0)
        self.assertEqual(len(b.posts), 1)
        self.assertEqual([r[3] for r in b.protection], ["STOP_LOSS", "TAKE_PROFIT"])
        self.assertEqual(j.value["trades"][0]["target_price"], 102.)
        self.assertEqual(j.value["trades"][0]["stop_price"], 99.)

    def test_partial_terminal_fill_protects_only_executed_lots(self):
        j = Journal([intent()]); b = FakeBroker(j, fill=100)
        self.assertEqual(self.run_due(b, j), 0)
        self.assertEqual([r[0] for r in b.protection], [100, 100])
        self.assertEqual(j.value["trades"][0]["actual_position_rub"], 10000.)

    def test_lost_ack_remains_frozen_and_404_never_replays(self):
        j = Journal([intent()]); b = FakeBroker(j, ack_lost=True)
        self.assertEqual(self.run_due(b, j), 1)
        frozen = j.value["trades"][0]
        self.assertEqual(frozen["phase"], "entry_submitting")
        b.order_state = Mock(side_effect=RuntimeError("HTTP 404"))
        b.share_positions = Mock(return_value=[])
        with patch.object(trade_bot, "upsert_trade", j.upsert_trade), patch.object(trade_bot, "log"):
            self.assertEqual(trade_bot.reconcile_submitting(b, frozen), 0)
        self.assertEqual(len(b.posts), 1)
        self.assertEqual(j.value["trades"][0]["phase"], "skipped")

    def test_404_exact_position_without_deprecated_quantity_lots_is_protected(self):
        j = Journal([intent()]); b = FakeBroker(j, ack_lost=True)
        self.run_due(b, j)
        b.order_state = Mock(side_effect=RuntimeError("HTTP 404"))
        b.share_positions = Mock(return_value=[{"instrumentUid": "uid-AAA", "quantity": quotation(500), "averagePositionPrice": quotation(100.1)}])
        with patch.object(trade_bot, "finalize_entry") as final, patch.object(trade_bot, "log"):
            self.assertEqual(trade_bot.reconcile_submitting(b, j.value["trades"][0]), 0)
        self.assertEqual(final.call_args.args[2:4], (500, 100.1))
        self.assertEqual(len(b.posts), 1)

    def test_stop_failure_leaves_actual_exposure_for_next_tick(self):
        j = Journal([intent()]); b = FakeBroker(j, stop_fails=True)
        with self.assertRaises(RuntimeError):
            self.run_due(b, j)
        self.assertEqual(j.value["trades"][0]["phase"], "entered")
        self.assertIn("STOP_LOSS", j.value["trades"][0]["protection_error"])
        self.assertEqual(len(b.posts), 1)

    def test_expired_stale_quote_and_tiny_broker_capacity_do_not_submit(self):
        j = Journal([intent()]); b = FakeBroker(j)
        self.assertEqual(self.run_due(b, j, now=NOW+timedelta(seconds=46)), 0)
        self.assertEqual(b.posts, [])
        j = Journal([intent()]); b = FakeBroker(j)
        with self.assertRaises(live.execution.EntryQuoteError):
            self.run_due(b, j, now=NOW+timedelta(seconds=4))
        self.assertEqual(b.posts, [])
        j = Journal([intent()]); b = FakeBroker(j); b.maximum_lots = 1
        self.assertEqual(self.run_due(b, j), 0)
        self.assertEqual(b.posts, [])

    def test_primary_does_not_stack_over_shared_reservations(self):
        j = Journal([intent()]); b = FakeBroker(j)
        with patch.object(live, "validate_manifest"), patch.object(live, "prepare_day", return_value=True):
            with self.assertRaises(ValueError):
                live.primary_entry_guard(b, j, DS)

    def test_repeat_entry_requires_confirmed_cancellation_of_old_paired_stop(self):
        old = {**intent(),"engine":"OPEN:previous","phase":"closed","entry_price":100.,
               "closed_at":(NOW-timedelta(minutes=31)).isoformat(),"take_order_id":"old-take"}
        j = Journal([old,intent()]); b = FakeBroker(j)
        stop = {"instrumentUid":"uid-AAA","stopOrderId":"old-take"}
        b.stop_orders = Mock(side_effect=[[stop],[stop]])
        b._post = Mock(return_value={})
        self.assertEqual(self.run_due(b,j),0)
        self.assertEqual(b.posts,[])
        b._post.assert_called_once_with("StopOrdersService/CancelStopOrder",{"accountId":"offline-account","stopOrderId":"old-take"})
        j = Journal([old,intent()]); b = FakeBroker(j)
        b.stop_orders = Mock(side_effect=[[stop],[]]); b._post = Mock(return_value={})
        self.assertEqual(self.run_due(b,j),0)
        self.assertEqual(len(b.posts),1)

    def test_due_intents_execute_before_full_universe_refresh(self):
        j = Journal([intent()]); b = FakeBroker(j); events = []
        day = {"errors": {}, "sessions": {}, "contexts": {}, "rows": {}}
        collector = Mock()
        collector.collect.side_effect = lambda now, **kw: (events.append("held" if "refresh_symbols" in kw else "universe") or day)
        with patch.object(live, "validate_manifest", return_value={"universe": [], "training_base": "ignored"}), patch.object(live, "prepare_day", return_value=True), patch.object(data, "MarketCollector", return_value=collector), patch.object(live, "submit_due", side_effect=lambda *a: events.append("submit") or 0), patch.object(data, "monthly_models", return_value=({}, {})), patch.object(live.scanner, "scan", return_value=([], {})):
            self.assertEqual(live.tick(b, j, NOW), 0)
        self.assertEqual(events, ["held", "submit", "universe"])


class MarketAndActivationTests(unittest.TestCase):
    def test_only_complete_ended_moscow_bars_and_share_volume_are_used(self):
        def candle(time, complete=True):
            return {"time": time, "isComplete": complete, "volume": "10", **{k: quotation(100) for k in ("open","high","low","close")}}
        value = {"candles": [candle("2026-10-05T04:15:00Z"), candle("2026-10-05T04:20:00Z", False), candle("2026-10-05T04:25:00Z")]}
        rows = data.candle_rows(value, DS, NOW, 10)
        self.assertEqual([r[0] for r in rows], ["07:15"])
        self.assertEqual(rows[0][5:], [100, 10000])

    def test_current_month_runtime_labels_are_excluded_and_base_days_not_duplicated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); base = root/"base.gz"
            row = {"date": "2026-09-01", "features": [0]*19, "net_return_pct": .5}
            with gzip.open(base, "wt") as handle:
                json.dump({"rows": [row]}, handle)
            schema = hashlib.sha256((data.ROOT/"argonus/models/opening_profit_model.py").read_bytes()).hexdigest()
            for ds in ("2026-09-01", "2026-09-02", "2026-10-01"):
                data.write_json(root/"training"/(ds+".json"), {"date": ds, "feature_schema_sha256": schema, "rows": [{**row,"date":ds}]})
            rows = data.training_rows(base, "2026-10", root)
            self.assertEqual([r["date"] for r in rows], ["2026-09-01", "2026-09-02"])

    def test_no_candle_exit_cannot_create_a_completed_training_label(self):
        with self.assertRaises(ValueError):
            data.label(opportunity(), {"contexts": {}, "rows": {"07:25": {("scanner", "AAA"): ["07:25",100,100.1,99.9,100,100000]}}})

    def test_activation_hash_or_environment_drift_blocks_new_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts = {}
            for name in live.REQUIRED_ARTIFACTS:
                (root/name).parent.mkdir(parents=True, exist_ok=True)
                (root/name).write_text("test artifact")
                artifacts[name] = hashlib.sha256((root/name).read_bytes()).hexdigest()
            manifest = {"policy":model.POLICY,"approved":True,"production_activation_allowed":True,"effective_from":DS,
                        "contract":live.contract(),"universe":[f"S{i}" for i in range(20)],"runtime_artifacts":artifacts,
                        "required_environment":live.REQUIRED_ENV,"training_base":"models/opening_model_training.json.gz"}
            path = root/"manifest.json"; path.write_text(json.dumps(manifest))
            with patch.object(live,"ROOT",root), patch.dict(live.os.environ,live.REQUIRED_ENV):
                self.assertEqual(live.validate_manifest(DS,path)["policy"],model.POLICY)
                with patch.dict(live.os.environ,{"BOT_SECOND_ENGINE":"1"}):
                    with self.assertRaises(ValueError):
                        live.validate_manifest(DS,path)
                (root/"argonus/trading/trade_bot.py").write_text("changed")
                with self.assertRaises(ValueError):
                    live.validate_manifest(DS,path)

    def test_dry_run_never_collects_or_creates_order_intents(self):
        with patch.object(data,"MarketCollector") as collector:
            j = Journal(); b = FakeBroker(j); b.live = False
            self.assertEqual(live.tick(b,j,NOW),0)
        collector.assert_not_called()
        self.assertEqual(j.writes,[])

    def test_scheduler_rejects_weekends_and_runs_during_morning(self):
        self.assertTrue(loop.due(NOW))
        self.assertFalse(loop.due(NOW+timedelta(days=5)))
        self.assertFalse(loop.due(NOW.replace(hour=23,minute=50)))


if __name__ == "__main__":
    unittest.main()
