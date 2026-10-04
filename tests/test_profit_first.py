from dataclasses import replace
import unittest

from argonus.research import research_0705_execution as base
from argonus.research import research_profit_first as study
from argonus.strategies.profit_first_profile import plan
from tests.test_research_0705_execution import candle, trade, ZERO


class ProfitFirstTests(unittest.TestCase):
    def test_actual_entry_distance_is_scaled_not_the_t4_price(self):
        long = plan("long", "100", "103", "0.01")
        short = plan("short", "100", "97", "0.01")
        self.assertEqual(float(long["target_price"]), 103.75)
        self.assertEqual(float(short["target_price"]), 96.25)
        self.assertEqual(float(long["stop_price"]), 99)
        self.assertEqual(float(short["stop_price"]), 101)
        self.assertFalse(long["orders_allowed"])

    def test_tick_rounding_does_not_increase_planned_stop_risk(self):
        for direction, t4 in (("long", 103), ("short", 97)):
            result = plan(direction, "100.03", t4, ".025")
            self.assertLessEqual(result["planned_stop_distance_pct"], 1)
            from decimal import Decimal
            self.assertEqual(Decimal(result["target_price"]) % Decimal(".025"), 0)
            self.assertEqual(Decimal(result["stop_price"]) % Decimal(".025"), 0)

    def test_invalid_geometry_and_missing_protection_rejected(self):
        cases = [("long", 100, 99, .01), ("short", 100, 101, .01),
                 ("long", 100, 103, 5), ("long", "nan", 103, .01),
                 ("long", 100, 103, 0), ("sideways", 100, 103, .01)]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                plan(*args)

    def test_selection_is_by_profit_even_with_fewer_wins(self):
        t = trade(candle("07:05"), candle("18:35", 101, 101.1, 100.9, 101))
        row = base.simulate(t, fills=ZERO)
        first = replace(row, date="2026-01-05", net_return_pct=.5)
        second = replace(row, date="2026-01-06", net_return_pct=.5)
        high_profit = [replace(first, net_return_pct=4), replace(second, net_return_pct=-.01)]
        high_accuracy = [replace(first, net_return_pct=1), replace(second, net_return_pct=1)]
        chosen, _ = study.pick({"baseline": [first, replace(second, net_return_pct=-.5)],
                               "profitable": high_profit, "accurate": high_accuracy})
        self.assertEqual(chosen, "profitable")

    def test_future_outcomes_cannot_change_candidate_selection(self):
        t = trade(candle("07:05"), candle("18:35", 101, 101.1, 100.9, 101))
        row = base.simulate(t, fills=ZERO)
        old = replace(row, date="2026-01-05", net_return_pct=.5)
        future = replace(row, date="2026-09-30", month="2026-09", net_return_pct=1000)
        paths = {"baseline": [old, future], "better_prior": [replace(old, net_return_pct=1), replace(future, net_return_pct=-1)]}
        chosen, _ = study.pick(paths)
        self.assertEqual(chosen, "better_prior")

    def test_unpaired_bootstrap_data_rejected(self):
        t = trade(candle("07:05"), candle("18:35"))
        row = base.simulate(t, fills=ZERO)
        with self.assertRaises(ValueError):
            study.paired_profit([row], [replace(row, date="2026-01-06")])
        with self.assertRaises(ValueError):
            study.paired_profit([], [])


if __name__ == "__main__":
    unittest.main()
