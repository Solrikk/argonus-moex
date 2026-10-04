#!/usr/bin/env python3
"""Production contracts for the Engine-A 07:05 depth-50 FOK entry.

These tests intentionally exercise only public pure quote helpers plus the
existing ``trade_bot.place_entry`` / reconciliation boundaries.  The broker is
always a fake: this module can never place a real order.
"""
from __future__ import annotations

import copy
import json
import math
import tempfile
import unittest
from datetime import date, datetime, time as dtime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from argonus.trading import production_entry_0705 as entry0705
from argonus.trading import trade_bot


MOSCOW = ZoneInfo("Europe/Moscow")
TRADE_DAY = "2026-07-20"
RECEIVED_AT = datetime(2026, 7, 20, 7, 5, 2, tzinfo=MOSCOW)


def quotation(value: float) -> dict[str, int | str]:
    units = int(value)
    nano = int(round((value - units) * 1_000_000_000))
    if nano == 1_000_000_000:
        units += 1
        nano = 0
    return {"units": str(units), "nano": nano}


def level(price: float, quantity: int) -> dict:
    return {"price": quotation(price), "quantity": str(quantity)}


def orderbook(
    *,
    timestamp: str = "2026-07-20T04:05:00Z",
    uid: str = "uid-test",
    ticker: str = "TEST",
    depth: int = 50,
    bids: list[dict] | None = None,
    asks: list[dict] | None = None,
) -> dict:
    return {
        "instrumentUid": uid,
        "ticker": ticker,
        "depth": depth,
        "orderbookTs": timestamp,
        "bids": bids if bids is not None else [level(100.00, 1), level(99.95, 4)],
        "asks": asks if asks is not None else [level(100.10, 2), level(100.20, 3)],
    }


def normalized(raw: dict | None = None) -> dict:
    return entry0705.normalize_orderbook(
        raw if raw is not None else orderbook(),
        expected_uid="uid-test",
        expected_symbol="TEST",
        expected_depth=50,
    )


def execution_quote(
    book: dict,
    *,
    direction: str,
    requested_lots: int,
    target_price: float,
    received_at: datetime = RECEIVED_AT,
    max_quote_age_ms: int = 3_000,
    max_impact_bps: float = 10.0,
) -> dict:
    return entry0705.build_execution_quote(
        book,
        direction=direction,
        requested_lots=requested_lots,
        target_price=target_price,
        received_at=received_at,
        max_quote_age_ms=max_quote_age_ms,
        max_impact_bps=max_impact_bps,
    )


class PureDepth50QuoteTests(unittest.TestCase):
    def test_long_consumes_asks_in_lots_and_uses_worst_as_fok_limit(self) -> None:
        quote = execution_quote(
            normalized(), direction="long", requested_lots=4, target_price=102.0
        )

        self.assertEqual(quote["side"], "asks")
        self.assertEqual(quote["covered_lots"], 4)
        self.assertAlmostEqual(quote["best_touch"], 100.10)
        self.assertAlmostEqual(quote["executable_vwap"], 100.15)
        self.assertAlmostEqual(quote["worst_price"], 100.20)
        self.assertAlmostEqual(quote["limit_price"], 100.20)
        self.assertEqual(quote["quote_age_ms"], 2_000.0)
        self.assertLessEqual(quote["impact_bps"], 10.0)

    def test_short_consumes_bids_in_lots_and_uses_worst_as_fok_limit(self) -> None:
        quote = execution_quote(
            normalized(), direction="short", requested_lots=3, target_price=98.0
        )

        self.assertEqual(quote["side"], "bids")
        self.assertEqual(quote["covered_lots"], 3)
        self.assertAlmostEqual(quote["best_touch"], 100.00)
        self.assertAlmostEqual(quote["executable_vwap"], (100.0 + 2 * 99.95) / 3)
        self.assertAlmostEqual(quote["worst_price"], 99.95)
        self.assertAlmostEqual(quote["limit_price"], 99.95)
        self.assertGreaterEqual(quote["impact_bps"], 0.0)
        self.assertLessEqual(quote["impact_bps"], 10.0)

    def test_book_quantities_are_lots_not_shares(self) -> None:
        # Four requested lots consume 2 + 2 book lots.  No instrument lot-size
        # factor belongs in this pure order-book calculation.
        quote = execution_quote(
            normalized(), direction="long", requested_lots=4, target_price=102.0
        )
        self.assertEqual(quote["covered_lots"], 4)
        self.assertAlmostEqual(quote["executable_vwap"], 100.15)

    def test_wrong_uid_depth_missing_timestamp_and_crossed_book_fail_closed(self) -> None:
        cases: list[tuple[str, dict]] = []
        wrong_uid = orderbook(uid="uid-other")
        cases.append(("uid", wrong_uid))
        wrong_depth = orderbook(depth=20)
        cases.append(("depth", wrong_depth))
        missing_timestamp = orderbook()
        missing_timestamp.pop("orderbookTs")
        cases.append(("timestamp", missing_timestamp))
        crossed = orderbook(
            bids=[level(100.10, 10)],
            asks=[level(100.10, 10)],
        )
        cases.append(("crossed", crossed))

        for label, raw in cases:
            with self.subTest(label=label):
                with self.assertRaises(entry0705.EntryQuoteError):
                    normalized(raw)

    def test_unsorted_duplicate_empty_or_nonpositive_levels_fail_closed(self) -> None:
        invalid_books = {
            "unsorted asks": orderbook(asks=[level(100.20, 1), level(100.10, 1)]),
            "duplicate bids": orderbook(bids=[level(100.00, 1), level(100.00, 2)]),
            "empty asks": orderbook(asks=[]),
            "zero quantity": orderbook(asks=[level(100.10, 0)]),
        }
        for label, raw in invalid_books.items():
            with self.subTest(label=label):
                with self.assertRaises(entry0705.EntryQuoteError):
                    normalized(raw)

    def test_stale_and_future_quotes_fail_at_three_second_gate(self) -> None:
        book = normalized()
        # Exactly 3000 ms is accepted.
        exact_boundary = execution_quote(
            book,
            direction="long",
            requested_lots=1,
            target_price=102.0,
            received_at=datetime(2026, 7, 20, 7, 5, 3, tzinfo=MOSCOW),
        )
        self.assertEqual(exact_boundary["quote_age_ms"], 3_000.0)

        with self.assertRaises(entry0705.EntryQuoteError):
            execution_quote(
                book,
                direction="long",
                requested_lots=1,
                target_price=102.0,
                received_at=datetime(2026, 7, 20, 7, 5, 3, 1000, tzinfo=MOSCOW),
            )
        with self.assertRaises(entry0705.EntryQuoteError):
            execution_quote(
                book,
                direction="long",
                requested_lots=1,
                target_price=102.0,
                received_at=datetime(2026, 7, 20, 7, 4, 59, 999000, tzinfo=MOSCOW),
            )

    def test_shallow_depth_rejects_instead_of_partially_pricing_order(self) -> None:
        book = normalized(
            orderbook(asks=[level(100.10, 1), level(100.20, 1)])
        )
        with self.assertRaises(entry0705.EntryQuoteError):
            execution_quote(
                book, direction="long", requested_lots=3, target_price=102.0
            )

    def test_target_must_be_ahead_of_worst_consumed_level(self) -> None:
        book = normalized()
        # VWAP is 100.15, but the long consumes through 100.20.  Equality to
        # the worst level is already invalid because no positive target remains.
        with self.assertRaises(entry0705.EntryQuoteError):
            execution_quote(
                book, direction="long", requested_lots=4, target_price=100.20
            )
        with self.assertRaises(entry0705.EntryQuoteError):
            execution_quote(
                book, direction="short", requested_lots=3, target_price=99.95
            )

    def test_market_impact_above_ten_bps_is_rejected(self) -> None:
        book = normalized(
            orderbook(
                bids=[level(99.90, 5)],
                # Impact is executable VWAP versus best touch.  One lot at
                # each level gives VWAP 100.11, i.e. 11 bps.
                asks=[level(100.00, 1), level(100.22, 3)],
            )
        )
        with self.assertRaises(entry0705.EntryQuoteError):
            execution_quote(
                book,
                direction="long",
                requested_lots=2,
                target_price=102.0,
                max_impact_bps=10.0,
            )

    def test_invalid_direction_lots_target_and_naive_clock_fail_closed(self) -> None:
        book = normalized()
        bad_calls = (
            {"direction": "buy", "requested_lots": 1, "target_price": 102.0},
            {"direction": "long", "requested_lots": 0, "target_price": 102.0},
            {"direction": "long", "requested_lots": 1, "target_price": math.nan},
        )
        for kwargs in bad_calls:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(entry0705.EntryQuoteError):
                    execution_quote(book, **kwargs)
        with self.assertRaises(entry0705.EntryQuoteError):
            execution_quote(
                book,
                direction="long",
                requested_lots=1,
                target_price=102.0,
                received_at=datetime(2026, 7, 20, 7, 5, 2),
            )


class FokPayloadTests(unittest.TestCase):
    class PayloadBot:
        account_id = "account-test"
        place_limit = trade_bot.TradeBot.place_limit
        place_fok_limit = trade_bot.TradeBot.place_fok_limit

        def __init__(self) -> None:
            self.method: str | None = None
            self.payload: dict | None = None

        def _post(self, method: str, payload: dict) -> dict:
            self.method = method
            self.payload = copy.deepcopy(payload)
            return {
                "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
                "lotsExecuted": payload["quantity"],
                "executedOrderPrice": payload["price"],
            }

    def test_post_order_payload_is_one_marketable_fill_or_kill_limit(self) -> None:
        bot = self.PayloadBot()
        request_id, _ = bot.place_fok_limit(
            "uid-test",
            4,
            "buy",
            100.20,
            confirm_margin_trade=True,
            request_id="frozen-request-id",
        )

        self.assertEqual(request_id, "frozen-request-id")
        self.assertEqual(bot.method, "OrdersService/PostOrder")
        assert bot.payload is not None
        self.assertEqual(bot.payload["orderId"], "frozen-request-id")
        self.assertEqual(bot.payload["instrumentId"], "uid-test")
        self.assertEqual(bot.payload["quantity"], "4")
        self.assertEqual(bot.payload["direction"], "ORDER_DIRECTION_BUY")
        self.assertEqual(bot.payload["orderType"], "ORDER_TYPE_LIMIT")
        self.assertEqual(bot.payload["timeInForce"], "TIME_IN_FORCE_FILL_OR_KILL")
        self.assertTrue(bot.payload["confirmMarginTrade"])
        self.assertAlmostEqual(trade_bot.quotation_to_float(bot.payload["price"]), 100.20)


class ShareInfoRegressionTests(unittest.TestCase):
    """The 07:05 recovery branch must never leak into instrument lookup."""

    class ShareBot:
        client = SimpleNamespace(
            resolve_share_instrument_id=lambda symbol, board: "TEST_TQBR"
        )
        share_info = trade_bot.TradeBot.share_info

        def __init__(self) -> None:
            self.payload = None

        def _post(self, method, payload):
            if method != "InstrumentsService/ShareBy":
                raise AssertionError(method)
            self.payload = dict(payload)
            return {
                "instrument": {
                    "uid": "uid-test",
                    "lot": 10,
                    "minPriceIncrement": quotation(0.01),
                    "shortEnabledFlag": True,
                    "apiTradeAvailableFlag": True,
                    "name": "Test share",
                }
            }

    def test_ticker_resolution_path_has_no_entry_reconcile_dependency(self) -> None:
        bot = self.ShareBot()
        result = bot.share_info("TEST")
        self.assertEqual(result["uid"], "uid-test")
        self.assertEqual(result["lot"], 10)
        self.assertEqual(
            bot.payload,
            {
                "idType": "INSTRUMENT_ID_TYPE_TICKER",
                "classCode": trade_bot.wbt.DEFAULT_BOARD,
                "id": "TEST",
            },
        )


class FixedTradingDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 7, 20)


def clock_at(hour: int, minute: int, second: int = 0):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 20, hour, minute, second, tzinfo=MOSCOW)
            return value if tz is None else value.astimezone(tz)

    return FixedDatetime


def base_state(direction: str = "long", lots: int = 4) -> dict:
    return {
        "date": TRADE_DAY,
        "engine": "A",
        "entry_route": "delayed_0705_depth50_fok",
        "symbol": "TEST",
        "uid": "uid-test",
        "direction": direction,
        "lots": lots,
        "lot_size": 10,
        "increment": 0.01,
        "reference_price": 100.0,
        "target_price": 102.0 if direction == "long" else 98.0,
        "desired_position_rub": 150_000.0,
        "actual_position_rub": lots * 10 * 100.0,
        "confirm_margin_trade": True,
        "selector_final_symbol": "TEST",
        "selector_resolution_id": "a" * 64,
        "risk_pct": 3.0,
    }


def share_info() -> dict:
    return {
        "uid": "uid-test",
        "lot": 10,
        "min_price_increment": 0.01,
        "api_trade_available": True,
        "short_enabled": True,
    }


class RouteBot:
    """Flexible fake for the public delayed route, with no network dependency."""

    live = True

    def __init__(self, response: dict | None, writes: list[dict], *, raw_book: dict | None = None):
        self.response = response
        self.raw_book = raw_book if raw_book is not None else orderbook()
        self.writes = writes
        self.book_calls: list[tuple[str, int]] = []
        self.fok_calls: list[dict] = []
        self.protection: list[tuple[str, str, int, str, float]] = []
        self.raise_post: Exception | None = None
        self.reconcile_result: dict | None = None
        self.order_state_error: Exception | None = None
        self.order_state_calls: list[str] = []
        self.positions: list[dict] = []

    def _book(self, uid: str, depth: int = 50) -> dict:
        self.book_calls.append((uid, depth))
        return copy.deepcopy(self.raw_book)

    # These aliases keep the test about behaviour rather than the private name
    # of the new TradeBot market-data wrapper.
    get_order_book = _book
    get_order_book_depth = _book
    order_book = _book
    order_book_depth = _book
    order_book_snapshot = _book

    def place_fok_limit(
        self,
        uid,
        lots,
        direction,
        price,
        confirm_margin_trade=False,
        request_id=None,
    ):
        self._record_fok(uid, lots, direction, price, confirm_margin_trade, request_id)
        if self.raise_post is not None:
            raise self.raise_post
        return request_id, copy.deepcopy(self.response)

    place_limit_fok = place_fok_limit

    def _record_fok(self, uid, lots, direction, price, confirm_margin_trade, request_id):
        if not self.writes:
            raise AssertionError("entry intent was not persisted before PostOrder")
        intent = self.writes[-1]
        if intent.get("phase") != "entry_submitting":
            raise AssertionError("entry_submitting was not durable before PostOrder")
        if intent.get("entry_order_id") != request_id:
            raise AssertionError("durable request id differs from PostOrder orderId")
        if intent.get("entry_route") != "delayed_0705_depth50_fok":
            raise AssertionError("durable entry route is not frozen")
        self.fok_calls.append(
            {
                "uid": uid,
                "lots": lots,
                "direction": direction,
                "price": price,
                "confirm_margin_trade": confirm_margin_trade,
                "request_id": request_id,
            }
        )

    def smart_fill(self, *args, **kwargs):
        raise AssertionError("07:05 production entry must not run the three-stage ladder")

    def stop_order(self, uid, lots, direction, price, kind):
        self.protection.append((kind, uid, lots, direction, price))
        return f"{kind.lower()}-id"

    def stop_orders(self):
        return []

    def order_state(self, request_id):
        self.order_state_calls.append(request_id)
        if self.order_state_error is not None:
            raise self.order_state_error
        if self.reconcile_result is None:
            raise AssertionError("unexpected GetOrderState")
        return copy.deepcopy(self.reconcile_result)

    def share_positions(self):
        return copy.deepcopy(self.positions)

    def cancel_order(self, *args, **kwargs):
        raise AssertionError("terminal FOK must not be cancelled again")


class ProductionRouteTests(unittest.TestCase):
    def invoke(self, bot: RouteBot, state: dict | None = None) -> tuple[int, list[dict]]:
        writes = bot.writes
        with (
            patch.object(trade_bot, "datetime", clock_at(7, 5, 2)),
            patch.object(
                trade_bot,
                "upsert_trade",
                side_effect=lambda live, day, value: writes.append(dict(value)),
            ),
            patch.object(trade_bot, "PULLBACK_PCT", 0.0),
            patch.object(trade_bot, "ENTRY_START", dtime(7, 5)),
            patch.object(trade_bot, "ENTRY_DEADLINE", dtime(7, 7)),
            patch.object(trade_bot, "ENTRY_EXECUTION_POLICY", entry0705.POLICY),
            patch.object(
                trade_bot,
                "validate_entry_activation_manifest",
                return_value=(True, "valid", "a" * 64),
            ),
        ):
            rc = trade_bot.place_entry(
                bot,
                state if state is not None else base_state(),
                100.0,
                (state if state is not None else base_state())["lots"],
                share_info(),
            )
        return rc, writes

    def test_full_fill_uses_one_worst_price_fok_then_protects_actual_fill(self) -> None:
        writes: list[dict] = []
        bot = RouteBot(
            {
                "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
                "lotsExecuted": "4",
                "executedOrderPrice": quotation(100.16),
            },
            writes,
        )

        rc, writes = self.invoke(bot)

        self.assertEqual(rc, 0)
        self.assertEqual(bot.book_calls, [("uid-test", 50)])
        self.assertEqual(len(bot.fok_calls), 1)
        self.assertEqual(bot.fok_calls[0]["direction"], "buy")
        self.assertEqual(bot.fok_calls[0]["lots"], 4)
        self.assertAlmostEqual(bot.fok_calls[0]["price"], 100.20)
        self.assertTrue(bot.fok_calls[0]["confirm_margin_trade"])
        self.assertTrue(bot.fok_calls[0]["request_id"])
        self.assertEqual([item[0] for item in bot.protection], ["STOP_LOSS", "TAKE_PROFIT"])
        self.assertTrue(all(item[2] == 4 for item in bot.protection))
        final = writes[-1]
        self.assertEqual(final["phase"], "entered")
        self.assertAlmostEqual(final["entry_price"], 100.16)
        self.assertAlmostEqual(final["stop_price"], 99.16)
        self.assertAlmostEqual(final["target_price"], 102.0)
        self.assertEqual(final["protection_status"], "protected")

    def test_short_uses_bids_buy_to_close_and_one_percent_stop_from_fill(self) -> None:
        writes: list[dict] = []
        bot = RouteBot(
            {
                "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
                "lotsExecuted": "3",
                "executedOrderPrice": quotation(99.97),
            },
            writes,
        )
        state = base_state("short", lots=3)

        rc, writes = self.invoke(bot, state)

        self.assertEqual(rc, 0)
        self.assertEqual(len(bot.fok_calls), 1)
        self.assertEqual(bot.fok_calls[0]["direction"], "sell")
        self.assertAlmostEqual(bot.fok_calls[0]["price"], 99.95)
        self.assertEqual([item[3] for item in bot.protection], ["buy", "buy"])
        self.assertAlmostEqual(writes[-1]["stop_price"], 100.97)
        self.assertAlmostEqual(writes[-1]["target_price"], 98.0)

    def test_defensive_partial_terminal_fill_protects_only_executed_lots(self) -> None:
        writes: list[dict] = []
        bot = RouteBot(
            {
                "executionReportStatus": "EXECUTION_REPORT_STATUS_CANCELLED",
                "lotsExecuted": "2",
                "executedOrderPrice": quotation(100.18),
            },
            writes,
        )

        rc, writes = self.invoke(bot)

        self.assertEqual(rc, 0)
        self.assertEqual(len(bot.fok_calls), 1)
        self.assertEqual([item[2] for item in bot.protection], [2, 2])
        self.assertEqual(writes[-1]["lots"], 2)
        self.assertEqual(writes[-1]["protection_status"], "protected")

    def test_zero_terminal_fill_is_skipped_without_retry_or_protection(self) -> None:
        writes: list[dict] = []
        bot = RouteBot(
            {
                "executionReportStatus": "EXECUTION_REPORT_STATUS_CANCELLED",
                "lotsExecuted": "0",
                "executedOrderPrice": quotation(0.0),
            },
            writes,
        )

        rc, writes = self.invoke(bot)

        self.assertEqual(rc, 0)
        self.assertEqual(len(bot.fok_calls), 1)
        self.assertEqual(bot.protection, [])
        self.assertEqual(writes[-1]["phase"], "skipped")
        self.assertNotEqual(writes[-1]["phase"], "retry")

    def test_stale_shallow_crossed_or_target_behind_never_reaches_post_order(self) -> None:
        bad_books = {
            "stale": orderbook(timestamp="2026-07-20T04:04:58Z"),
            "shallow": orderbook(asks=[level(100.10, 1)]),
            "crossed": orderbook(bids=[level(100.10, 4)], asks=[level(100.10, 4)]),
        }
        for label, raw in bad_books.items():
            with self.subTest(label=label):
                writes: list[dict] = []
                bot = RouteBot(None, writes, raw_book=raw)
                rc, _ = self.invoke(bot)
                self.assertNotEqual(rc, 0)
                self.assertEqual(bot.fok_calls, [])
                self.assertEqual(bot.protection, [])

        writes = []
        bot = RouteBot(None, writes)
        state = {**base_state(), "target_price": 100.20}
        rc, _ = self.invoke(bot, state)
        self.assertNotEqual(rc, 0)
        self.assertEqual(bot.fok_calls, [])

    def test_durable_intent_latency_rechecks_freshness_before_post_order(self) -> None:
        class AdvancingDatetime(datetime):
            current = datetime(2026, 7, 20, 7, 5, 2, tzinfo=MOSCOW)

            @classmethod
            def now(cls, tz=None):
                return cls.current if tz is None else cls.current.astimezone(tz)

        writes: list[dict] = []
        bot = RouteBot(None, writes)

        def persist_then_age_quote(live, day, value):
            writes.append(dict(value))
            if value.get("phase") == "entry_submitting":
                # Model atomic replace + fsync consuming two seconds.  The
                # 07:05:00 order book was valid before persistence but is now
                # four seconds old and must never reach PostOrder.
                AdvancingDatetime.current = datetime(
                    2026, 7, 20, 7, 5, 4, tzinfo=MOSCOW
                )

        with (
            patch.object(trade_bot, "datetime", AdvancingDatetime),
            patch.object(trade_bot, "upsert_trade", side_effect=persist_then_age_quote),
            patch.object(trade_bot, "PULLBACK_PCT", 0.0),
            patch.object(trade_bot, "ENTRY_START", dtime(7, 5)),
            patch.object(trade_bot, "ENTRY_DEADLINE", dtime(7, 7)),
            patch.object(trade_bot, "ENTRY_EXECUTION_POLICY", entry0705.POLICY),
            patch.object(
                trade_bot,
                "validate_entry_activation_manifest",
                return_value=(True, "valid", "a" * 64),
            ),
        ):
            rc = trade_bot.place_entry(
                bot,
                base_state(),
                100.0,
                base_state()["lots"],
                share_info(),
            )

        self.assertEqual(rc, 0)
        self.assertEqual(bot.fok_calls, [])
        self.assertEqual(bot.protection, [])
        self.assertTrue(any(item.get("phase") == "entry_submitting" for item in writes))
        self.assertEqual(writes[-1]["phase"], "skipped")
        self.assertIn("устарел", writes[-1]["reason"])

    def test_lost_ack_keeps_exact_request_and_restart_never_posts_duplicate(self) -> None:
        writes: list[dict] = []
        bot = RouteBot(None, writes)
        bot.raise_post = TimeoutError("ACK lost after broker may have accepted request")

        rc, writes = self.invoke(bot)

        self.assertEqual(rc, 1)
        self.assertEqual(len(bot.fok_calls), 1)
        frozen = dict(writes[-1])
        self.assertEqual(frozen["phase"], "entry_submitting")
        self.assertEqual(frozen["entry_order_id"], bot.fok_calls[0]["request_id"])

        bot.raise_post = None
        bot.reconcile_result = {
            "status": "EXECUTION_REPORT_STATUS_FILL",
            "lots": 4,
            "avg_price": 100.17,
            "execution_time": "2026-07-20T04:05:02+00:00",
        }
        with patch.object(
            trade_bot,
            "upsert_trade",
            side_effect=lambda live, day, value: writes.append(dict(value)),
        ):
            reconcile_rc = trade_bot.reconcile_submitting(bot, frozen)

        self.assertEqual(reconcile_rc, 0)
        self.assertEqual(len(bot.fok_calls), 1, "restart must not submit a second order")
        self.assertEqual(bot.order_state_calls, [frozen["entry_order_id"]])
        self.assertEqual([item[0] for item in bot.protection], ["STOP_LOSS", "TAKE_PROFIT"])

    def test_404_inside_window_never_replays_stale_frozen_quote(self) -> None:
        writes: list[dict] = []
        bot = RouteBot(None, writes)
        bot.raise_post = TimeoutError("initial ACK lost")
        rc, writes = self.invoke(bot)
        self.assertEqual(rc, 1)
        frozen = dict(writes[-1])
        bot.raise_post = None
        bot.order_state_error = trade_bot.TBankApiError("HTTP 404: Order not found")
        with (
            patch.object(trade_bot, "datetime", clock_at(7, 5, 30)),
            patch.object(trade_bot, "ENTRY_START", dtime(7, 5)),
            patch.object(trade_bot, "ENTRY_DEADLINE", dtime(7, 7)),
            patch.object(
                trade_bot,
                "upsert_trade",
                side_effect=lambda live, day, value: writes.append(dict(value)),
            ),
        ):
            reconcile_rc = trade_bot.reconcile_submitting(bot, frozen)

        self.assertEqual(reconcile_rc, 0)
        self.assertEqual(len(bot.fok_calls), 1, "404 must never replay a stale quote")
        self.assertEqual(writes[-1]["phase"], "skipped")
        self.assertIn("replay запрещён", writes[-1]["reason"])
        self.assertEqual(bot.protection, [])

    def test_prior_date_404_in_new_entry_window_never_replays_old_request(self) -> None:
        writes: list[dict] = []
        bot = RouteBot(None, writes)
        bot.order_state_error = trade_bot.TBankApiError("HTTP 404: Order not found")
        frozen = {
            **base_state(),
            "date": "2026-07-19",
            "phase": "entry_submitting",
            "entry_order_id": "request-from-prior-day",
            "entry_submit_price": 100.20,
            "entry_submit_lots": 4,
        }

        with (
            patch.object(trade_bot, "datetime", clock_at(7, 5, 30)),
            patch.object(trade_bot, "ENTRY_START", dtime(7, 5)),
            patch.object(trade_bot, "ENTRY_DEADLINE", dtime(7, 7)),
            patch.object(
                trade_bot,
                "upsert_trade",
                side_effect=lambda live, day, value: writes.append(dict(value)),
            ),
        ):
            reconcile_rc = trade_bot.reconcile_submitting(bot, frozen)

        self.assertEqual(reconcile_rc, 0)
        self.assertEqual(bot.fok_calls, [], "a prior-date request must never be replayed")
        self.assertEqual(bot.protection, [])
        self.assertEqual(writes[-1]["date"], "2026-07-19")
        self.assertEqual(writes[-1]["phase"], "skipped")
        self.assertIn("replay запрещён", writes[-1]["reason"])

    def test_404_after_deadline_never_replays_and_skips_only_if_portfolio_is_empty(self) -> None:
        writes: list[dict] = []
        bot = RouteBot(None, writes)
        bot.raise_post = TimeoutError("initial ACK lost")
        rc, writes = self.invoke(bot)
        self.assertEqual(rc, 1)
        frozen = dict(writes[-1])
        bot.raise_post = None
        bot.order_state_error = trade_bot.TBankApiError("HTTP 404: Order not found")

        with (
            patch.object(trade_bot, "datetime", clock_at(7, 7, 1)),
            patch.object(trade_bot, "ENTRY_START", dtime(7, 5)),
            patch.object(trade_bot, "ENTRY_DEADLINE", dtime(7, 7)),
            patch.object(
                trade_bot,
                "upsert_trade",
                side_effect=lambda live, day, value: writes.append(dict(value)),
            ),
        ):
            reconcile_rc = trade_bot.reconcile_submitting(bot, frozen)

        self.assertEqual(reconcile_rc, 0)
        self.assertEqual(len(bot.fok_calls), 1, "late 404 must not replay PostOrder")
        self.assertEqual(writes[-1]["phase"], "skipped")
        self.assertEqual(bot.protection, [])

    def test_404_after_deadline_adopts_matching_frozen_position_and_protects_it(self) -> None:
        writes: list[dict] = []
        bot = RouteBot(None, writes)
        bot.raise_post = TimeoutError("initial ACK lost")
        rc, writes = self.invoke(bot)
        self.assertEqual(rc, 1)
        frozen = dict(writes[-1])
        bot.raise_post = None
        bot.order_state_error = trade_bot.TBankApiError("HTTP 404: Order not found")
        bot.positions = [
            {
                "instrumentUid": "uid-test",
                "quantity": quotation(40.0),
                "quantityLots": {"units": "4", "nano": 0},
                "averagePositionPrice": quotation(100.17),
            }
        ]

        with (
            patch.object(trade_bot, "datetime", clock_at(7, 7, 1)),
            patch.object(trade_bot, "ENTRY_START", dtime(7, 5)),
            patch.object(trade_bot, "ENTRY_DEADLINE", dtime(7, 7)),
            patch.object(
                trade_bot,
                "upsert_trade",
                side_effect=lambda live, day, value: writes.append(dict(value)),
            ),
        ):
            reconcile_rc = trade_bot.reconcile_submitting(bot, frozen)

        self.assertEqual(reconcile_rc, 0)
        self.assertEqual(len(bot.fok_calls), 1, "late 404 with a position must not replay")
        self.assertEqual([item[0] for item in bot.protection], ["STOP_LOSS", "TAKE_PROFIT"])
        self.assertTrue(all(item[2] == 4 for item in bot.protection))
        self.assertEqual(writes[-1]["phase"], "entered")
        self.assertAlmostEqual(writes[-1]["entry_price"], 100.17)

    def test_404_position_must_match_frozen_direction_and_exact_fok_lots(self) -> None:
        cases = {
            "opposite direction": {
                "instrumentUid": "uid-test",
                "quantity": quotation(-40.0),
                "quantityLots": quotation(-4.0),
                "averagePositionPrice": quotation(100.17),
            },
            "oversized manual position": {
                "instrumentUid": "uid-test",
                "quantity": quotation(50.0),
                "quantityLots": quotation(5.0),
                "averagePositionPrice": quotation(100.17),
            },
        }
        for label, position in cases.items():
            with self.subTest(label=label):
                writes: list[dict] = []
                bot = RouteBot(None, writes)
                bot.order_state_error = trade_bot.TBankApiError(
                    "HTTP 404: Order not found"
                )
                bot.positions = [position]
                frozen = {
                    **base_state(),
                    "phase": "entry_submitting",
                    "entry_order_id": "uncertain-fok-request",
                    "entry_submit_price": 100.20,
                    "entry_submit_lots": 4,
                }

                with patch.object(
                    trade_bot,
                    "upsert_trade",
                    side_effect=lambda live, day, value: writes.append(dict(value)),
                ):
                    reconcile_rc = trade_bot.reconcile_submitting(bot, frozen)

                self.assertEqual(reconcile_rc, 1)
                self.assertEqual(bot.fok_calls, [])
                self.assertEqual(bot.protection, [])
                self.assertEqual(writes, [])


class TickTimingTests(unittest.TestCase):
    class EmptyBot:
        live = True

        def share_positions(self):
            return []

    def test_runtime_and_launcher_pin_0705_start_and_0707_deadline(self) -> None:
        with open(trade_bot.PROJECT_DIR + "/scripts/run_tick.sh", "r", encoding="utf-8") as handle:
            wrapper = handle.read()
        self.assertIn('export BOT_ENTRY_START=07:05', wrapper)
        self.assertIn('export BOT_ENTRY_DEADLINE=07:07', wrapper)
        self.assertIn(
            'export BOT_ENTRY_EXECUTION_POLICY=delayed_0705_depth50_fok', wrapper
        )
        self.assertEqual(trade_bot._parse_hhmm("07:05", "07:00"), dtime(7, 5))
        self.assertEqual(trade_bot._parse_hhmm("07:07", "08:00"), dtime(7, 7))

    def test_tick_before_0705_performs_no_selector_or_entry(self) -> None:
        with (
            patch.object(trade_bot, "date", FixedTradingDate),
            patch.object(trade_bot, "datetime", clock_at(7, 4, 59)),
            patch.object(trade_bot, "ENTRY_START", dtime(7, 5)),
            patch.object(trade_bot, "ENTRY_DEADLINE", dtime(7, 7)),
            patch.object(trade_bot, "SECOND_ENGINE", False),
            patch.object(trade_bot, "load_journal", return_value=None),
            patch.object(
                trade_bot,
                "ensure_fresh_watchlist",
                side_effect=AssertionError("early tick must not start the selector"),
            ),
            patch.object(
                trade_bot,
                "cmd_enter",
                side_effect=AssertionError("early tick must not enter"),
            ),
        ):
            rc = trade_bot.cmd_tick(self.EmptyBot())
        self.assertEqual(rc, 0)

    def test_tick_after_0707_marks_engine_skipped_without_entry(self) -> None:
        writes: list[dict] = []
        with (
            patch.object(trade_bot, "date", FixedTradingDate),
            patch.object(trade_bot, "datetime", clock_at(7, 7, 1)),
            patch.object(trade_bot, "ENTRY_START", dtime(7, 5)),
            patch.object(trade_bot, "ENTRY_DEADLINE", dtime(7, 7)),
            patch.object(trade_bot, "SECOND_ENGINE", False),
            patch.object(trade_bot, "load_journal", return_value=None),
            patch.object(
                trade_bot,
                "upsert_trade",
                side_effect=lambda live, day, value: writes.append(dict(value)),
            ),
            patch.object(
                trade_bot,
                "ensure_fresh_watchlist",
                side_effect=AssertionError("late tick must not run selector"),
            ),
            patch.object(
                trade_bot,
                "cmd_enter",
                side_effect=AssertionError("late tick must not enter"),
            ),
        ):
            rc = trade_bot.cmd_tick(self.EmptyBot())

        self.assertEqual(rc, 0)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["engine"], "A")
        self.assertEqual(writes[0]["phase"], "skipped")
        self.assertIn("07:07", writes[0]["reason"])

    def test_exact_0707_boundary_is_still_inside_entry_window(self) -> None:
        enter = Mock(return_value=0)
        with (
            patch.object(trade_bot, "date", FixedTradingDate),
            patch.object(trade_bot, "datetime", clock_at(7, 7, 0)),
            patch.object(trade_bot, "ENTRY_START", dtime(7, 5)),
            patch.object(trade_bot, "ENTRY_DEADLINE", dtime(7, 7)),
            patch.object(trade_bot, "SECOND_ENGINE", False),
            patch.object(trade_bot, "load_journal", return_value=None),
            patch.object(trade_bot, "ensure_fresh_watchlist", return_value=True),
            patch.object(trade_bot, "cmd_enter", enter),
        ):
            rc = trade_bot.cmd_tick(self.EmptyBot())
        self.assertEqual(rc, 0)
        enter.assert_called_once()

    def test_prior_date_404_defers_today_entry_until_next_tick(self) -> None:
        stale = {
            **base_state(),
            "date": "2026-07-19",
            "phase": "entry_submitting",
            "entry_order_id": "request-from-prior-day",
            "entry_submit_price": 100.20,
            "entry_submit_lots": 4,
        }
        journal_box = {
            "value": {"date": "2026-07-19", "trades": [dict(stale)]}
        }
        bot = RouteBot(None, [])
        bot.order_state_error = trade_bot.TBankApiError("HTTP 404: Order not found")
        enter = Mock(side_effect=AssertionError("same tick must not enter today"))
        selector = Mock(
            side_effect=AssertionError("same tick must not run today's selector")
        )

        def load_box():
            return copy.deepcopy(journal_box["value"])

        def persist(live, day, value):
            journal_box["value"] = {"date": day, "trades": [dict(value)]}

        with (
            patch.object(trade_bot, "date", FixedTradingDate),
            patch.object(trade_bot, "datetime", clock_at(7, 5, 30)),
            patch.object(trade_bot, "ENTRY_START", dtime(7, 5)),
            patch.object(trade_bot, "ENTRY_DEADLINE", dtime(7, 7)),
            patch.object(trade_bot, "SECOND_ENGINE", False),
            patch.object(trade_bot, "load_journal", side_effect=load_box),
            patch.object(trade_bot, "upsert_trade", side_effect=persist),
            patch.object(trade_bot, "ensure_fresh_watchlist", selector),
            patch.object(trade_bot, "cmd_enter", enter),
        ):
            rc = trade_bot.cmd_tick(bot)

        self.assertEqual(rc, 0)
        selector.assert_not_called()
        enter.assert_not_called()
        self.assertEqual(bot.fok_calls, [])
        self.assertEqual(journal_box["value"]["trades"][0]["phase"], "skipped")


from tests.local_artifacts import requires_local_files


@requires_local_files("config/entry_0705_activation_manifest.json")
class ActivationManifestTests(unittest.TestCase):
    def production_config(self):
        return (
            patch.object(trade_bot, "ENTRY_START", dtime(7, 5)),
            patch.object(trade_bot, "ENTRY_DEADLINE", dtime(7, 7)),
            patch.object(trade_bot, "ENTRY_EXECUTION_POLICY", entry0705.POLICY),
            patch.object(trade_bot, "ENTRY_BOOK_DEPTH", 50),
            patch.object(trade_bot, "ENTRY_MAX_QUOTE_AGE_MS", 3000),
            patch.object(trade_bot, "ENTRY_MAX_IMPACT_BPS", 10.0),
            patch.object(trade_bot, "TARGET_POSITION_RUB", 150000.0),
            patch.object(trade_bot, "MAX_LEVERAGE", 3.0),
            patch.object(trade_bot, "EXIT_TIME", dtime(18, 35)),
        )

    def test_actual_manifest_pins_code_config_and_honest_override(self) -> None:
        patches = self.production_config()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        valid, status, digest = trade_bot.validate_entry_activation_manifest(
            trade_date=date(2026, 7, 20)
        )
        self.assertTrue(valid, status)
        self.assertEqual(status, "valid_explicit_user_override")
        self.assertEqual(len(digest or ""), 64)

    def test_manifest_fails_closed_if_override_or_config_is_tampered(self) -> None:
        source = Path(trade_bot.ENTRY_ACTIVATION_MANIFEST_PATH)
        manifest = json.loads(source.read_text(encoding="utf-8"))
        manifest["activation_evidence"]["forward_gate_passed"] = True
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "manifest.json"
            target.write_text(json.dumps(manifest), encoding="utf-8")
            patches = self.production_config()
            for item in patches:
                item.start()
                self.addCleanup(item.stop)
            valid, reason, digest = trade_bot.validate_entry_activation_manifest(
                str(target), trade_date=date(2026, 7, 20)
            )
        self.assertFalse(valid)
        self.assertIn("override", reason)
        self.assertIsNone(digest)

    def test_manifest_is_not_effective_before_registered_date(self) -> None:
        patches = self.production_config()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        valid, reason, _ = trade_bot.validate_entry_activation_manifest(
            trade_date=date(2026, 7, 19)
        )
        self.assertFalse(valid)
        self.assertIn("не раньше", reason)


if __name__ == "__main__":
    unittest.main()
