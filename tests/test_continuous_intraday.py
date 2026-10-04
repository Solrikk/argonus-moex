"""Causality, repeated entries and shared-capital tests with synthetic bars."""
from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, timedelta
from unittest.mock import patch

from argonus.strategies import continuous_intraday as e


DAY = "2026-01-05"


def intent(symbol="A", tm="10:30", direction="long", **kwargs):
    return e.Opportunity(DAY, symbol, direction, "rolling_breakout", tm, 100, 1, 1, 2, 100_000_000, **kwargs)


def row(tm, open=100, high=100.2, low=99.8, close=100):
    return [tm, open, high, low, close, 100000, 10000000]


def history():
    first = date(2025, 11, 1)
    return [[(first + timedelta(days=i)).isoformat(), 100, 101, 99, 100 + .01 * i, 1e7]
            for i in range(30)]


class SignalTests(unittest.TestCase):
    def test_future_bars_and_future_daily_prices_cannot_change_signal(self):
        rows = [row(e.clock(tm)) for tm in range(590, 630, 5)]
        contexts = {"A": e.prior_context(history(), DAY)}
        before = e.scan_opportunities(DAY, "10:30", {"A": rows}, contexts)
        future = rows + [["10:30", 0, 99999, -2, 0, -1]]
        self.assertEqual(before, e.scan_opportunities(DAY, "10:30", {"A": future}, contexts))
        self.assertEqual(contexts["A"], e.prior_context(history() + [[DAY, 0, 0, 0, 0, -1]], DAY))

    def test_intraday_volume_breakout_is_generated_without_a_watchlist(self):
        rows = [row(e.clock(tm)) for tm in range(590, 630, 5)]
        rows[-1] = ["10:25", 100, 101.2, 100, 101, 500000, 50500000]
        candidates, _ = e.scan_opportunities(DAY, "10:30", {"A": rows}, {"A": e.prior_context(history(), DAY)})
        self.assertTrue(any(x.setup == "rolling_breakout" and x.direction == "long" for x in candidates))

    def test_regime_router_waits_in_ambiguous_market(self):
        self.assertEqual(e.route_opportunities([intent()], {"regime": "wait"}, "regime_router"), [])
        self.assertEqual(e.route_opportunities([intent()], {"regime": "range"}, "regime_router"), [])
        self.assertEqual(e.route_opportunities([intent()], {"regime": "trend"}, "regime_router"), [intent()])

    def test_stale_signal_and_missing_opening_history_are_rejected(self):
        rows = [row(e.clock(tm)) for tm in range(590, 630, 5)]
        contexts = {"A": e.prior_context(history(), DAY)}
        for bad in [rows[:-1], rows[1:]]:
            self.assertEqual(e.scan_opportunities(DAY, "10:30", {"A": bad}, contexts)[0], [])


class PortfolioTests(unittest.TestCase):
    def portfolio(self, **kwargs):
        p = e.EventPortfolio(**kwargs)
        p.begin_day(DAY)
        return p

    def test_shared_cap_and_no_duplicate_symbol(self):
        p = self.portfolio()
        with patch.object(e, "correlated", return_value=False):
            contexts = {name: {} for name in "ABCD"}
            self.assertTrue(p.submit(intent("A"), contexts))
            self.assertFalse(p.submit(intent("A"), contexts))
            self.assertTrue(p.submit(intent("B"), contexts))
            self.assertTrue(p.submit(intent("C"), contexts))
            self.assertFalse(p.submit(intent("D"), contexts))
        p.step("10:35", {("scanner", name): row("10:35") for name in "ABC"})
        self.assertEqual(len(p.positions), 3)
        self.assertLessEqual(sum(x["notional_rub"] for x in p.positions.values()), 150000)
        self.assertTrue(all(x["notional_rub"] <= 50000 for x in p.positions.values()))

    def test_reentry_after_cooldown_and_freed_capital(self):
        p = self.portfolio(bps=0)
        p.submit(intent(), {})
        p.step("10:35", {("scanner", "A"): row("10:35", high=102.5)})
        self.assertEqual(len(p.ledger), 1)
        self.assertFalse(p.submit(intent(tm="11:00"), {}))
        self.assertTrue(p.submit(intent(tm="11:10"), {}))
        p.step("11:15", {("scanner", "A"): row("11:15", high=102.5)})
        self.assertEqual(len(p.ledger), 2)
        self.assertFalse(p.submit(intent(tm="12:00"), {}))
        p.end_day()
        self.assertEqual(p.result()["days_with_multiple_trades"], 1)

    def test_fill_is_next_bar_and_does_not_chase_old_price(self):
        p = self.portfolio()
        p.submit(intent(), {})
        p.step("10:30", {})
        self.assertFalse(p.positions)
        p.step("10:35", {("scanner", "A"): row("10:35", open=101, high=101.2, low=100.8, close=101)})
        self.assertFalse(p.positions)
        self.assertEqual(p.counters["unfilled_chase"], 1)

    def test_both_barriers_hit_stop_first_for_both_directions(self):
        for direction in ("long", "short"):
            p = self.portfolio(bps=0)
            p.submit(intent(direction=direction), {})
            p.step("10:35", {("scanner", "A"): row("10:35", high=104, low=96)})
            self.assertEqual(p.ledger[0]["reason"], "both_stop_first")
            self.assertLess(p.ledger[0]["pnl_rub"], -500)

    def test_gap_stop_uses_observed_open_and_not_nominal_stop(self):
        p = self.portfolio(bps=0)
        p.submit(intent(), {})
        p.step("10:35", {("scanner", "A"): row("10:35")})
        p.step("10:40", {("scanner", "A"): row("10:40", open=95, high=96, low=94, close=95)})
        self.assertEqual(p.ledger[0]["reason"], "stop_gap")
        self.assertLess(p.ledger[0]["net_return_pct"], -5)

    def test_missing_entry_is_unfilled_and_not_retried_later(self):
        p = self.portfolio()
        p.submit(intent(), {})
        p.step("10:35", {})
        p.step("10:40", {("scanner", "A"): row("10:40")})
        self.assertFalse(p.positions)
        self.assertFalse(p.pending)
        self.assertEqual(p.counters["unfilled_missing_entry"], 1)

    def test_correlated_positions_compete_for_shared_risk(self):
        p = self.portfolio()
        contexts = {name: {"returns": {str(i): i / 1000 for i in range(20)}} for name in "AB"}
        self.assertTrue(p.submit(intent("A"), contexts))
        self.assertFalse(p.submit(intent("B"), contexts))
        self.assertTrue(p.submit(intent("B", direction="short"), contexts))

    def test_legacy_holds_same_shared_capacity_and_target125_geometry(self):
        p = self.portfolio(bps=0)
        p.submit(intent(source="legacy", original_t4=103), {})
        p.step("10:35", {("legacy", "A"): row("10:35")})
        self.assertEqual(p.positions["A"]["notional_rub"], 150000)
        self.assertEqual(p.positions["A"]["target_price"], 103.75)
        p.submit(intent("B", tm="10:40"), {})
        p.step("10:45", {("legacy", "A"): row("10:45"), ("scanner", "B"): row("10:45")})
        self.assertNotIn("B", p.positions)

    def test_duplicate_tick_cannot_duplicate_fills(self):
        p = self.portfolio()
        p.submit(intent(), {})
        p.step("10:35", {("scanner", "A"): row("10:35")})
        with self.assertRaises(ValueError):
            p.step("10:35", {("scanner", "A"): row("10:35")})

    def test_fees_on_both_notionals_and_time_exit(self):
        p = self.portfolio(bps=0)
        p.submit(intent(), {})
        p.step("10:35", {("scanner", "A"): row("10:35")})
        p.step("18:35", {("scanner", "A"): row("18:35", open=100.5, high=110, low=90, close=105)})
        p.end_day()
        expected = 500 * .5 - 50000 * .0004 - 500 * 100.5 * .0004
        self.assertAlmostEqual(p.ledger[0]["pnl_rub"], expected)
        self.assertAlmostEqual(p.cash, 50000 + expected)
        self.assertEqual(p.ledger[0]["reason"], "time_exit_open")

    def test_missing_bar_while_open_is_recorded_and_blocks_new_risk(self):
        p = self.portfolio()
        p.submit(intent(), {})
        p.step("10:35", {("scanner", "A"): row("10:35")})
        p.step("10:40", {})
        self.assertTrue(p.positions["A"]["stale"])
        self.assertFalse(p.submit(intent("B", tm="10:40", direction="short"), {}))

    def test_daily_loss_cutoff_prevents_more_trades(self):
        p = self.portfolio(bps=0)
        p.submit(intent(source="legacy", original_t4=103), {})
        p.step("10:35", {("legacy", "A"): row("10:35", low=98)})
        self.assertLess(p.cash, .97 * p.day_start)
        self.assertFalse(p.submit(intent("B", tm="11:30"), {}))


if __name__ == "__main__":
    unittest.main()
