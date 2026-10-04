import unittest
import math
from dataclasses import replace
from datetime import date, timedelta

from argonus.strategies import intraday_signals as s
from argonus.research import research_intraday_portfolio as p


def daily():
    return [[(date(2025, 12, 1) + timedelta(days=i)).isoformat(), 100, 102, 98, 100, 1_000_000]
            for i in range(30)]


def bars():
    return [[f"{m // 60:02d}:{m % 60:02d}", 100, 101, 99, 100.5, 100_000]
            for m in range(590, 630, 5)]


def snap(symbol="TEST", move=1.0):
    return s.Snapshot("2026-01-05", symbol, "10:30", "10:25", 101, 2, 1,
                      100_000_000, 10_000_000, move, 100.5, 99)


def signal(direction="long"):
    return s.Signal("2026-01-05", "TEST", direction, "opening_follow", "10:30", 100,
                    1, 1, 2, "18:35", 3, 10_000_000, "10:25")


class SignalTests(unittest.TestCase):
    def test_native_turnover_is_not_multiplied_by_share_price_again(self):
        d = [row + [100_000_000] for row in daily()]
        b = [row + [2_000_000] for row in bars()]
        item = s.snapshot("TEST", "2026-01-05", d, b, "10:30")
        self.assertEqual(item.prior_turnover_lower_bound_rub, 100_000_000)
        self.assertEqual(item.recent_turnover_lower_bound_rub, 12_000_000)

    def test_modified_engine_and_nonfinite_snapshot_rejected(self):
        with self.assertRaises(ValueError):
            s.select_signals([snap()], replace(s.ENGINES[0], max_positions=2))
        with self.assertRaises(ValueError):
            s.select_signals([replace(snap(), atr_pct=math.nan)], s.ENGINES[0])

    def test_future_daily_and_intraday_values_cannot_change_snapshot(self):
        d, b = daily(), bars()
        expected = s.snapshot("TEST", "2026-01-05", d, b, "10:30")
        self.assertIsNotNone(expected)
        # Include extreme/invalid future observations; none may be inspected.
        d += [["2026-01-05", 100, 99999, 1, 0, -1], ["2026-01-06", 0, 0, 0, 0, 0]]
        b += [["10:30", 100, 99999, 1, 50000, 1e20]]
        self.assertEqual(expected, s.snapshot("TEST", "2026-01-05", d, b, "10:30"))

    def test_missing_final_completed_bar_is_stale(self):
        self.assertIsNone(s.snapshot("TEST", "2026-01-05", daily(), bars()[:-1], "10:30"))

    def test_missing_main_session_open_is_rejected(self):
        self.assertIsNone(s.snapshot("TEST", "2026-01-05", daily(), bars()[1:], "10:30"))

    def test_future_volume_does_not_enable_illiquid_security(self):
        item = replace(snap(), recent_turnover_lower_bound_rub=100)
        self.assertEqual(s.select_signals([item], s.ENGINES[0]), [])

    def test_ties_are_deterministic_and_top_three_distinct(self):
        rows = [snap(symbol) for symbol in ("D", "C", "B", "A")]
        self.assertEqual([v.symbol for v in s.select_signals(rows, s.ENGINES[0])], ["A", "B", "C"])

    def test_opposite_engines_use_opposite_sides(self):
        self.assertEqual(s.select_signals([snap()], s.ENGINES[0])[0].direction, "long")
        self.assertEqual(s.select_signals([snap()], s.ENGINES[1])[0].direction, "short")

    def test_relative_signal_depends_on_peer_median(self):
        rows = [snap("A", 1.0), snap("B", 2.0), snap("C", 2.2)]
        chosen = s.select_signals(rows, s.ENGINES[2])
        self.assertEqual(chosen[0].symbol, "A")
        self.assertEqual(chosen[0].direction, "short")

    def test_duplicate_or_different_date_snapshots_rejected(self):
        for rows in ([snap(), snap()], [snap(), replace(snap("B"), date="2026-01-06")]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                s.select_signals(rows, s.ENGINES[0])

    def test_decision_bar_is_never_accepted_as_completed(self):
        with self.assertRaises(ValueError):
            s.select_signals([replace(snap(), last_completed_bar="10:30")], s.ENGINES[0])


class ReplayTests(unittest.TestCase):
    def test_decision_bar_is_not_execution_bar(self):
        cs = [["10:30", 100, 104, 95, 100, 1], ["10:35", 100, 100.5, 99.5, 100, 1],
              ["18:35", 100, 105, 90, 103, 1]]
        leg = p.replay(signal(), cs, 0)
        self.assertEqual(leg.entry_time, "10:35")
        self.assertEqual(leg.reason, "time_exit_open")
        self.assertAlmostEqual(leg.net_return_pct, -.08)

    def test_missing_entry_is_unfilled_no_later_fallback(self):
        leg = p.replay(signal(), [["10:40", 100, 104, 99.5, 102, 1]], 0)
        self.assertEqual(leg.reason, "unfilled_missing_entry")
        self.assertEqual(leg.net_return_pct, 0)

    def test_gap_losses_exceed_stop_in_both_directions(self):
        for direction, opening in (("long", 97), ("short", 103)):
            cs = [["10:35", 100, 100.5, 99.5, 100, 1], ["10:40", opening, opening + .5, opening - .5, opening, 1]]
            r = p.replay(signal(direction), cs, 0)
            self.assertEqual(r.reason, "stop_gap")
            self.assertLess(r.net_return_pct, -3)

    def test_stop_first_intrabar(self):
        r = p.replay(signal(), [["10:35", 100, 103, 98, 100, 1]], 0)
        self.assertEqual(r.reason, "both_stop_first")
        self.assertAlmostEqual(r.net_return_pct, -1.0796)

    def test_target_at_open_precedes_intrabar_stop(self):
        cs = [["10:35", 100, 100.5, 99.5, 100, 1], ["10:40", 103, 104, 95, 99, 1]]
        r = p.replay(signal(), cs, 0)
        self.assertEqual(r.reason, "target_at_open")

    def test_short_cost_uses_actual_exit_turnover(self):
        r = p.replay(signal("short"), [["10:35", 100, 100.1, 97, 98, 1]], 0)
        self.assertAlmostEqual(r.net_return_pct, 2 - .04 * 1.98)

    def test_incomplete_exit_data_cannot_fabricate_pnl(self):
        with self.assertRaisesRegex(ValueError, "Missing exit data"):
            p.replay(signal(), [["10:35", 100, 100.5, 99.5, 100, 1]], 0)


class PortfolioTests(unittest.TestCase):
    def leg(self, symbol="A"):
        return replace(p.replay(signal(), [["10:35", 100, 100.5, 98, 99, 1]], 0), symbol=symbol)

    def test_three_simultaneous_legs_share_150k(self):
        result = p.account({"2026-01-05": [self.leg(x) for x in ("A", "B", "C")]})
        self.assertEqual(sum(r["position_rub"] for r in result["ledger"]), 150_000)
        self.assertTrue(all(r["position_rub"] == 50_000 for r in result["ledger"]))

    def test_single_leg_does_not_reuse_unallocated_capacity(self):
        result = p.account({"2026-01-05": [self.leg()]})
        self.assertEqual(result["ledger"][0]["position_rub"], 50_000)

    def test_risk_after_losing_day_uses_reduced_equity(self):
        first = self.leg()
        second = replace(first, date="2026-01-06")
        result = p.account({first.date: [first], second.date: [second]})
        self.assertAlmostEqual(result["ledger"][1]["position_rub"], result["daily"][0]["equity_after"])

    def test_basket_cannot_exceed_cap_or_duplicate_symbols(self):
        for legs in ([self.leg(), self.leg()], [replace(self.leg(x), weight=.5) for x in ("A", "B", "C")]):
            with self.subTest(legs=legs), self.assertRaises(ValueError):
                p.account({"2026-01-05": legs})

    def test_intrabar_proxy_detects_unrealized_loss_before_profitable_exit(self):
        cs = [["10:35", 100, 100.5, 99.2, 100, 1], ["10:40", 100, 103, 99.5, 102, 1]]
        leg = p.replay(signal(), cs, 0)
        r = p.account({leg.date: [leg]})
        self.assertGreater(r["pnl_rub"], 0)
        self.assertEqual(r["daily_close_mdd_pct"], 0)
        self.assertLess(r["intrabar_adverse_proxy_mdd_pct"], -.8)

    def test_training_choice_ignores_later_outcomes(self):
        good = replace(self.leg(), net_return_pct=2, marks=())
        future = replace(good, date="2026-04-01", net_return_pct=-10)
        paths = {"cash": {good.date: [], future.date: []}, "new": {good.date: [good], future.date: [future]}}
        self.assertEqual(p.choose(paths, p.FIT_END), "new")
        paths["new"][future.date] = [replace(future, net_return_pct=-99)]
        self.assertEqual(p.choose(paths, p.FIT_END), "new")

    def test_cash_wins_when_all_training_utilities_negative(self):
        leg = self.leg()
        self.assertEqual(p.choose({"cash": {leg.date: []}, "bad": {leg.date: [leg]}}, p.FIT_END), "cash")


from tests.local_artifacts import requires_local_files


@requires_local_files("data/backtests/live_rr2_t4_candidates_2025-11_2026-07.json")
class ArchivedBaselineTests(unittest.TestCase):
    def test_new_portfolio_engine_reproduces_every_frozen_baseline_fill(self):
        from argonus.research import research_flat_month_exit_risk as data
        from argonus.research import research_0705_execution as previous
        trades, _, _ = data.load_books()
        for trade in trades:
            with self.subTest(date=trade.date, symbol=trade.symbol):
                expected = previous.simulate(trade)
                sig = s.Signal(trade.date, trade.symbol, trade.direction, "baseline_A", "07:00",
                               trade.candles[0].open, 1, 1, 2, "18:35", 1, 0, "06:55")
                rows = [[c.time, c.open, c.high, c.low, c.close, 0] for c in trade.candles]
                actual = p.replay(sig, rows, entry_time="07:05", absolute_target=trade.target_price, weight=1)
                self.assertEqual(actual.exit_time, expected.exit_time)
                self.assertAlmostEqual(actual.entry_price, expected.entry_price)
                self.assertAlmostEqual(actual.exit_price, expected.exit_price)
                self.assertAlmostEqual(actual.net_return_pct, expected.net_return_pct)


if __name__ == "__main__":
    unittest.main()
