#!/usr/bin/env python3
"""Regression tests for the dynamic RUB cap on smart-fill limit stages."""
from __future__ import annotations

import unittest

from argonus.trading import trade_bot


class SmartFillDynamicNotionalCapTests(unittest.TestCase):
    class OneStageFillBot:
        live = True
        smart_fill = trade_bot.TradeBot.smart_fill

        def __init__(self) -> None:
            self.posts: list[dict] = []

        @staticmethod
        def order_book_touch(uid: str) -> tuple[float, float]:
            # The frozen plan was sized around 100 RUB, but the first executable
            # limit has already moved to 110 RUB per share.
            return 110.0, 111.0

        def place_limit(
            self,
            uid: str,
            lots: int,
            direction: str,
            price: float,
            *,
            confirm_margin_trade: bool = False,
            request_id: str | None = None,
        ) -> tuple[str, dict]:
            self.posts.append(
                {
                    "request_id": request_id,
                    "lots": lots,
                    "price": price,
                    "direction": direction,
                }
            )
            return request_id, {
                "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
                "lotsExecuted": lots,
                "executedOrderPrice": trade_bot.float_to_quotation(price),
            }

    def test_higher_submit_price_reduces_lots_before_post_order(self) -> None:
        bot = self.OneStageFillBot()

        filled, _ = bot.smart_fill(
            "uid-test",
            150,  # frozen at 100 RUB/share: 150 lots * 10 * 100 = 150k
            "buy",
            0.01,
            100.0,
            must_fill=False,
            max_position_rub=150_000.0,
            lot_size=10,
        )

        self.assertEqual(filled, 136)
        self.assertEqual(len(bot.posts), 1)
        submitted = bot.posts[0]
        self.assertEqual(submitted["price"], 110.0)
        self.assertEqual(submitted["lots"], 136)
        self.assertLessEqual(
            submitted["lots"] * submitted["price"] * 10,
            150_000.0,
        )

    class PartialAcrossStagesBot:
        live = True
        smart_fill = trade_bot.TradeBot.smart_fill

        def __init__(self) -> None:
            self.book_index = 0
            self.posts: list[dict] = []
            self.state_by_request: dict[str, dict] = {}
            self.cancel_calls: list[tuple[str, bool]] = []

        def order_book_touch(self, uid: str) -> tuple[float, float]:
            books = (
                (100.0, 101.0),
                (109.0, 110.0),
                (119.0, 120.0),
            )
            book = books[self.book_index]
            self.book_index += 1
            return book

        def place_limit(
            self,
            uid: str,
            lots: int,
            direction: str,
            price: float,
            *,
            confirm_margin_trade: bool = False,
            request_id: str | None = None,
        ) -> tuple[str, dict]:
            fill_lots = (100, 40, 4)[len(self.posts)]
            state = {
                "status": "EXECUTION_REPORT_STATUS_CANCELLED",
                "lots": fill_lots,
                "avg_price": price,
            }
            self.state_by_request[request_id] = state
            self.posts.append(
                {
                    "request_id": request_id,
                    "lots": lots,
                    "price": price,
                    "fill_lots": fill_lots,
                }
            )
            return request_id, {
                "executionReportStatus": "EXECUTION_REPORT_STATUS_NEW",
                "lotsExecuted": 0,
            }

        def _wait_fill(self, order_id: str, seconds: int) -> dict:
            return dict(self.state_by_request[order_id])

        def cancel_order(
            self, order_id: str, *, suppress_errors: bool = True
        ) -> None:
            self.cancel_calls.append((order_id, suppress_errors))

        def order_state(self, order_id: str) -> dict:
            return dict(self.state_by_request[order_id])

    def test_cumulative_partial_fills_leave_only_remaining_rub_capacity(self) -> None:
        bot = self.PartialAcrossStagesBot()

        filled, average = bot.smart_fill(
            "uid-test",
            150,
            "buy",
            0.01,
            100.0,
            must_fill=False,
            max_position_rub=150_000.0,
            lot_size=10,
        )

        self.assertEqual([post["lots"] for post in bot.posts], [150, 45, 4])
        self.assertEqual([post["fill_lots"] for post in bot.posts], [100, 40, 4])
        self.assertEqual(filled, 144)
        self.assertGreater(average, 100.0)

        filled_notional_before_stage = 0.0
        for post in bot.posts:
            open_limit_notional = post["lots"] * post["price"] * 10
            self.assertLessEqual(
                filled_notional_before_stage + open_limit_notional,
                150_000.0,
                "filled notional plus the next live limit must stay under the cap",
            )
            filled_notional_before_stage += (
                post["fill_lots"] * post["price"] * 10
            )

        self.assertLessEqual(filled_notional_before_stage, 150_000.0)
        self.assertEqual(
            bot.cancel_calls,
            [(post["request_id"], False) for post in bot.posts],
        )

    class MarketableShortBot:
        live = True
        smart_fill = trade_bot.TradeBot.smart_fill

        def __init__(self) -> None:
            self.posts: list[dict] = []
            self.states: dict[str, dict] = {}

        @staticmethod
        def order_book_touch(uid: str) -> tuple[float, float]:
            return 100.2, 100.3

        def place_limit(
            self,
            uid: str,
            lots: int,
            direction: str,
            price: float,
            *,
            confirm_margin_trade: bool = False,
            request_id: str | None = None,
        ) -> tuple[str, dict]:
            stage = len(self.posts)
            fill_lots = lots if stage == 2 else 0
            fill_price = 100.2 if fill_lots else 0.0
            self.posts.append({"lots": lots, "limit_price": price})
            self.states[request_id] = {
                "status": (
                    "EXECUTION_REPORT_STATUS_FILL"
                    if fill_lots
                    else "EXECUTION_REPORT_STATUS_CANCELLED"
                ),
                "lots": fill_lots,
                "avg_price": fill_price,
            }
            return request_id, {
                "executionReportStatus": (
                    "EXECUTION_REPORT_STATUS_FILL"
                    if fill_lots
                    else "EXECUTION_REPORT_STATUS_NEW"
                ),
                "lotsExecuted": fill_lots,
                "executedOrderPrice": trade_bot.float_to_quotation(fill_price),
            }

        def _wait_fill(self, order_id: str, seconds: int) -> dict:
            return dict(self.states[order_id])

        @staticmethod
        def cancel_order(
            order_id: str, *, suppress_errors: bool = True
        ) -> None:
            return None

        def order_state(self, order_id: str) -> dict:
            return dict(self.states[order_id])

    def test_marketable_short_uses_bid_not_lower_sell_limit_for_cap(self) -> None:
        bot = self.MarketableShortBot()

        filled, average = bot.smart_fill(
            "uid-test",
            150,
            "sell",
            0.01,
            100.2,
            must_fill=False,
            max_position_rub=150_000.0,
            lot_size=10,
        )

        self.assertEqual([post["lots"] for post in bot.posts], [149, 149, 149])
        self.assertEqual(filled, 149)
        self.assertEqual(average, 100.2)
        self.assertLessEqual(filled * average * 10, 150_000.0)


if __name__ == "__main__":
    unittest.main()
