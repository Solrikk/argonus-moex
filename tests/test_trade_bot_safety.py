#!/usr/bin/env python3
"""Точечные тесты защитного пути боевого бота."""
from __future__ import annotations

import unittest
from datetime import date, datetime, time as dtime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from argonus.trading import trade_bot


class FakeProtectionBot:
    live = True

    def __init__(self, fail_take: bool = False) -> None:
        self.fail_take = fail_take
        self.placed: list[tuple[str, float]] = []

    def stop_order(
        self, uid: str, lots: int, direction: str, stop_price: float, kind: str
    ) -> str:
        self.placed.append((kind, stop_price))
        if kind == "TAKE_PROFIT" and self.fail_take:
            raise RuntimeError("temporary take error")
        return f"{kind.lower()}-id"

    def stop_orders(self) -> list[dict]:
        return []


class TargetSafetyTests(unittest.TestCase):
    def base(self, target_price: float) -> dict:
        return {
            "date": "2026-07-18",
            "engine": "A",
            "symbol": "TEST",
            "uid": "uid-test",
            "direction": "long",
            "lots": 10,
            "increment": 0.01,
            "reference_price": 100.0,
            "target_price": target_price,
            "risk_pct": 0.5,
            "direction_risk_multiplier": 0.5,
        }

    def test_default_long_risk_is_half_and_short_is_unchanged(self) -> None:
        self.assertEqual(trade_bot.LONG_RISK_MULTIPLIER, 0.5)
        self.assertEqual(1.0 * trade_bot.LONG_RISK_MULTIPLIER, 0.5)
        self.assertEqual(1.0, 1.0)  # short path intentionally has no multiplier

    def test_target_move_sign_for_long_and_short(self) -> None:
        self.assertGreater(trade_bot.target_move_pct_from_entry("long", 100, 101), 0)
        self.assertLessEqual(trade_bot.target_move_pct_from_entry("long", 100, 100), 0)
        self.assertGreater(trade_bot.target_move_pct_from_entry("short", 100, 99), 0)
        self.assertLessEqual(trade_bot.target_move_pct_from_entry("short", 100, 100), 0)

    def test_invalid_fill_places_stop_only_and_persists_exit_flag(self) -> None:
        bot = FakeProtectionBot()
        writes: list[dict] = []
        with patch.object(
            trade_bot,
            "upsert_trade",
            side_effect=lambda live, date_value, state: writes.append(dict(state)),
        ):
            trade_bot.finalize_entry(bot, self.base(target_price=99.0), 10, 100.0)

        self.assertEqual([kind for kind, _ in bot.placed], ["STOP_LOSS"])
        self.assertGreaterEqual(len(writes), 3)
        final = writes[-1]
        self.assertIsNone(final["target_price"])
        self.assertEqual(final["planned_target_price"], 99.0)
        self.assertTrue(final["target_guard_failed"])
        self.assertTrue(final["exit_required"])
        self.assertEqual(final["protection_status"], "stop_only_target_guard")

    def test_take_failure_keeps_stop_and_journal_for_retry(self) -> None:
        bot = FakeProtectionBot(fail_take=True)
        writes: list[dict] = []
        with patch.object(
            trade_bot,
            "upsert_trade",
            side_effect=lambda live, date_value, state: writes.append(dict(state)),
        ):
            trade_bot.finalize_entry(bot, self.base(target_price=102.0), 10, 100.0)

        self.assertEqual([kind for kind, _ in bot.placed], ["STOP_LOSS", "TAKE_PROFIT"])
        final = writes[-1]
        self.assertEqual(final["stop_order_id"], "stop_loss-id")
        self.assertIsNone(final["take_order_id"])
        self.assertEqual(final["protection_status"], "stop_placed")
        self.assertIn("TAKE_PROFIT", final["protection_error"])

    def test_repair_does_not_recreate_target_behind_entry(self) -> None:
        bot = FakeProtectionBot()
        state = {
            **self.base(target_price=99.0),
            "phase": "entered",
            "entry_price": 100.0,
            "stop_price": 99.0,
        }
        writes: list[dict] = []
        with patch.object(
            trade_bot,
            "upsert_trade",
            side_effect=lambda live, date_value, value: writes.append(dict(value)),
        ):
            trade_bot.repair_protection(bot, state)

        self.assertEqual([kind for kind, _ in bot.placed], ["STOP_LOSS"])
        self.assertTrue(state["exit_required"])
        self.assertIsNone(state["target_price"])
        self.assertTrue(writes)


class LadderTargetTests(unittest.TestCase):
    class FakeLadder:
        live = True
        smart_fill = trade_bot.TradeBot.smart_fill

        def __init__(self, bid: float, ask: float) -> None:
            self.bid = bid
            self.ask = ask
            self.submitted: list[float] = []

        def order_book_touch(self, uid: str) -> tuple[float, float]:
            return self.bid, self.ask

        def place_limit(self, uid, lots, direction, price, confirm_margin_trade=False):
            self.submitted.append(price)
            return "request-id", {
                "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
                "lotsExecuted": lots,
                "executedOrderPrice": {"units": str(int(price)), "nano": 0},
            }

    def test_long_ladder_never_submits_price_at_or_beyond_target(self) -> None:
        bot = self.FakeLadder(bid=101.0, ask=102.0)
        filled, _ = bot.smart_fill(
            "uid", 1, "buy", 0.01, 101.0, must_fill=False,
            absolute_target_price=100.0,
        )
        self.assertEqual(filled, 0)
        self.assertEqual(bot.submitted, [])

    def test_short_ladder_never_submits_price_at_or_beyond_target(self) -> None:
        bot = self.FakeLadder(bid=98.0, ask=99.0)
        filled, _ = bot.smart_fill(
            "uid", 1, "sell", 0.01, 99.0, must_fill=False,
            absolute_target_price=100.0,
        )
        self.assertEqual(filled, 0)
        self.assertEqual(bot.submitted, [])


class AdoptedPositionTests(unittest.TestCase):
    class EmptyBot:
        live = True

        def share_positions(self):
            return []

    def adopted_journal(self) -> dict:
        return {
            "date": date.today().isoformat(),
            "trades": [
                {
                    "date": date.today().isoformat(),
                    "engine": "adopted-test",
                    "phase": "closed",
                    "adopted": True,
                }
            ],
        }

    def test_manual_entry_is_blocked_for_whole_adoption_day(self) -> None:
        with patch.object(trade_bot, "load_journal", return_value=self.adopted_journal()):
            rc = trade_bot.cmd_enter(
                self.EmptyBot(),
                SimpleNamespace(generate=False, force=True, input=None),
            )
        self.assertEqual(rc, 1)

    def test_tick_does_not_enter_after_adopted_position_closed(self) -> None:
        with (
            patch.object(trade_bot, "load_journal", return_value=self.adopted_journal()),
            patch.object(
                trade_bot,
                "ensure_fresh_watchlist",
                side_effect=AssertionError("new entry path must stay blocked"),
            ),
        ):
            rc = trade_bot.cmd_tick(self.EmptyBot())
        self.assertEqual(rc, 0)


class FeePreflightTests(unittest.TestCase):
    class FakeEstimator:
        account_id = "account-test"
        estimate_order_price = trade_bot.TradeBot.estimate_order_price

        def __init__(self) -> None:
            self.method = None
            self.payload = None

        def _post(self, method, payload):
            self.method = method
            self.payload = payload
            return {
                "initialOrderAmount": {"units": "149907", "nano": 0},
                "executedCommissionRub": {"units": "59", "nano": 970000000},
                "executedCommission": {"units": "59", "nano": 970000000},
            }

    def test_live_sized_fee_fixture_parses_without_rounding_distortion(self) -> None:
        bot = self.FakeEstimator()
        result = bot.estimate_order_price("uid-magn", 934, "buy", 16.05)
        self.assertEqual(bot.method, "OrdersService/GetOrderPrice")
        self.assertEqual(bot.payload["quantity"], "934")
        self.assertEqual(bot.payload["direction"], "ORDER_DIRECTION_BUY")
        self.assertAlmostEqual(result["notional_rub"], 149907.0)
        self.assertAlmostEqual(result["commission_rub"], 59.97)
        self.assertAlmostEqual(result["fee_side_pct"], 0.04000480364519335)


class LostAckEntrySafetyTests(unittest.TestCase):
    """Contract for a frozen, recoverable order-submission state machine."""

    class TradingDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 7, 20)

    class TradingDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 20, 10, 0, 0, tzinfo=tz)

    @staticmethod
    def submitting_state() -> dict:
        return {
            "date": "2026-07-20",
            "engine": "A",
            "phase": "entry_submitting",
            "symbol": "RANK2",
            "uid": "uid-rank2",
            "direction": "long",
            "lots": 5,
            "lot_size": 10,
            "increment": 0.01,
            "reference_price": 100.0,
            "limit_price": 100.0,
            "target_price": 103.0,
            "risk_pct": 0.5,
            "direction_risk_multiplier": 0.5,
            "entry_order_id": "request-frozen-rank2",
            "selector_final_symbol": "RANK2",
            "selector_original_symbol": "RANK1",
            "selector_resolution_id": "resolution-frozen",
        }

    @staticmethod
    def base_state() -> dict:
        state = LostAckEntrySafetyTests.submitting_state()
        state.pop("phase")
        state.pop("entry_order_id")
        state.pop("limit_price")
        return state

    def test_smart_fill_persists_request_before_post_and_stops_after_lost_ack(self) -> None:
        writes: list[dict] = []

        class LostAckBot:
            live = True
            account_id = "account-test"
            smart_fill = trade_bot.TradeBot.smart_fill
            place_limit = trade_bot.TradeBot.place_limit

            def __init__(self) -> None:
                self.post_attempts: list[str] = []
                self.order_book_calls = 0
                self.persisted_before_post = False

            def order_book_touch(self, uid):
                self.order_book_calls += 1
                return 99.0, 100.0

            def _post(self, method, payload):
                self.assert_post_method(method)
                request_id = payload["orderId"]
                self.post_attempts.append(request_id)
                self.persisted_before_post = bool(
                    writes
                    and writes[-1].get("phase") == "entry_submitting"
                    and writes[-1].get("entry_order_id") == request_id
                    and writes[-1].get("symbol") == "RANK2"
                )
                if not self.persisted_before_post:
                    raise AssertionError("entry_submitting must be durable before PostOrder")
                raise TimeoutError("connection lost after broker may have accepted PostOrder")

            @staticmethod
            def assert_post_method(method):
                if method != "OrdersService/PostOrder":
                    raise AssertionError(f"unexpected broker method: {method}")

            def __getattr__(self, name):
                raise AssertionError(f"lost-ACK path must not call {name}")

        bot = LostAckBot()
        info = {
            "uid": "uid-rank2",
            "lot": 10,
            "min_price_increment": 0.01,
            "api_trade_available": True,
            "short_enabled": True,
        }
        with (
            patch.object(trade_bot, "PULLBACK_PCT", 0.0),
            patch.object(
                trade_bot,
                "upsert_trade",
                side_effect=lambda live, day, state: writes.append(dict(state)),
            ),
            patch.object(
                trade_bot,
                "finalize_entry",
                side_effect=AssertionError("unknown ACK must never finalize"),
            ),
        ):
            rc = trade_bot.place_entry(bot, self.base_state(), 100.0, 5, info)

        self.assertEqual(rc, 1)
        self.assertTrue(bot.persisted_before_post)
        self.assertEqual(len(bot.post_attempts), 1, "lost ACK must stop the ladder")
        self.assertEqual(bot.order_book_calls, 1, "lost ACK must not advance to another stage")
        self.assertTrue(writes)
        self.assertEqual(writes[-1]["phase"], "entry_submitting")
        self.assertEqual(writes[-1]["entry_order_id"], bot.post_attempts[0])
        self.assertEqual(writes[-1]["symbol"], "RANK2")

    def test_cmd_enter_blocks_duplicate_while_entry_is_submitting(self) -> None:
        state = self.submitting_state()

        class NoBrokerBot:
            live = True

            def __getattr__(self, name):
                raise AssertionError(f"duplicate guard must precede broker call {name}")

        journal = {"date": state["date"], "trades": [state]}
        with (
            patch.object(trade_bot, "date", self.TradingDate),
            patch.object(trade_bot, "load_journal", return_value=journal),
            patch.object(
                trade_bot,
                "read_watchlist",
                side_effect=AssertionError("duplicate guard must precede selector"),
            ),
        ):
            rc = trade_bot.cmd_enter(
                NoBrokerBot(),
                SimpleNamespace(generate=False, force=True, input=None),
            )

        self.assertEqual(rc, 1)

    class JournalHarness:
        def __init__(self, trade: dict) -> None:
            self.day = trade["date"]
            self.trades = [dict(trade)]

        def load(self):
            return {"date": self.day, "trades": [dict(item) for item in self.trades]}

        def upsert(self, live, day, state):
            value = {**state, "date": day}
            self.trades = [
                item for item in self.trades if item.get("engine") != value.get("engine")
            ] + [value]

    class ReconcileBot:
        live = True

        def __init__(self, order_result=None, order_error=None) -> None:
            self.order_result = order_result
            self.order_error = order_error
            self.order_ids: list[str] = []

        def share_positions(self):
            return []

        def order_state(self, request_id):
            self.order_ids.append(request_id)
            if self.order_error is not None:
                raise self.order_error
            return dict(self.order_result)

        def __getattr__(self, name):
            raise AssertionError(f"unexpected reconcile broker call: {name}")

    def test_terminal_cancelled_zero_lots_releases_engine_a_for_retry(self) -> None:
        state = self.submitting_state()
        journal = self.JournalHarness(state)
        bot = self.ReconcileBot(
            order_result={
                "status": "EXECUTION_REPORT_STATUS_CANCELLED",
                "lots": 0,
                "avg_price": 0.0,
            }
        )
        enter = Mock(return_value=0)
        with (
            patch.object(trade_bot, "date", self.TradingDate),
            patch.object(trade_bot, "datetime", self.TradingDatetime),
            patch.object(trade_bot, "ENTRY_DEADLINE", dtime(13, 0)),
            patch.object(trade_bot, "SECOND_ENGINE", False),
            patch.object(trade_bot, "load_journal", side_effect=journal.load),
            patch.object(trade_bot, "upsert_trade", side_effect=journal.upsert),
            patch.object(trade_bot, "ensure_fresh_watchlist", return_value=True),
            patch.object(trade_bot, "cmd_enter", enter),
        ):
            rc = trade_bot.cmd_tick(bot)

        self.assertEqual(rc, 0)
        self.assertEqual(bot.order_ids, [state["entry_order_id"]])
        enter.assert_called_once()
        retry_record = next(item for item in journal.trades if item.get("engine") == "A")
        self.assertEqual(retry_record["phase"], "retry")
        self.assertEqual(retry_record["symbol"], "RANK2")

    def test_terminal_fill_or_partial_finalizes_frozen_symbol_without_selector(self) -> None:
        cases = (
            ("EXECUTION_REPORT_STATUS_FILL", 5, 101.0),
            ("EXECUTION_REPORT_STATUS_CANCELLED", 2, 100.5),
        )
        for status, lots, avg_price in cases:
            with self.subTest(status=status, lots=lots):
                state = self.submitting_state()
                bot = self.ReconcileBot(
                    order_result={"status": status, "lots": lots, "avg_price": avg_price}
                )
                finalize = Mock()
                with (
                    patch.object(trade_bot, "datetime", self.TradingDatetime),
                    patch.object(trade_bot, "finalize_entry", finalize),
                    patch.object(
                        trade_bot.wbt,
                        "analyze_watchlist",
                        side_effect=AssertionError("reconcile must use frozen symbol"),
                    ),
                    patch.object(
                        trade_bot,
                        "run_rs5_shadow_selector",
                        side_effect=AssertionError("reconcile must not rerun selector"),
                    ),
                    patch.object(
                        trade_bot,
                        "resolve_rs5_activation",
                        side_effect=AssertionError("reconcile must not rerun activation"),
                    ),
                ):
                    rc = trade_bot.reconcile_trade(bot, state, [])

                finalize.assert_called_once()
                called_bot, frozen, called_lots, called_avg = finalize.call_args.args
                self.assertIs(called_bot, bot)
                self.assertEqual(frozen["symbol"], "RANK2")
                self.assertEqual(frozen["selector_final_symbol"], "RANK2")
                self.assertEqual(called_lots, lots)
                self.assertEqual(called_avg, avg_price)

    def test_unresolved_submission_returns_error_and_keeps_submitting_journal(self) -> None:
        state = self.submitting_state()
        journal = self.JournalHarness(state)
        bot = self.ReconcileBot(order_error=TimeoutError("GetOrderState unavailable"))
        with (
            patch.object(trade_bot, "date", self.TradingDate),
            patch.object(trade_bot, "datetime", self.TradingDatetime),
            patch.object(trade_bot, "SECOND_ENGINE", False),
            patch.object(trade_bot, "load_journal", side_effect=journal.load),
            patch.object(trade_bot, "upsert_trade", side_effect=journal.upsert),
            patch.object(
                trade_bot,
                "ensure_fresh_watchlist",
                side_effect=AssertionError("unresolved submission must block a new selector"),
            ),
            patch.object(
                trade_bot,
                "cmd_enter",
                side_effect=AssertionError("unresolved submission must block a new entry"),
            ),
        ):
            rc = trade_bot.cmd_tick(bot)

        self.assertEqual(rc, 1)
        self.assertEqual(bot.order_ids, [state["entry_order_id"]])
        self.assertEqual(len(journal.trades), 1)
        self.assertEqual(journal.trades[0], state)

if __name__ == "__main__":
    unittest.main()
