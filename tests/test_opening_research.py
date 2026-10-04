import copy
import unittest
from dataclasses import replace

import numpy as np

from argonus.strategies import continuous_intraday as core
from argonus.strategies import continuous_opening as scanner
from argonus.research import research_opening_expectancy as learner
from argonus.research import research_opening_consensus as consensus
from argonus.research import research_scanner_expectancy as labels
from argonus.research import opening_candidate_paper as paper
from argonus.research.research_signal_direction import replay_events


def fixture(ds="2026-02-02"):
    rows = [["07:00", 100, 100.1, 99.9, 100.05, 100000],
            ["07:05", 100.05, 100.25, 100, 100.2, 100000],
            ["07:10", 100.2, 100.4, 100.15, 100.35, 100000],
            ["07:15", 100.35, 100.85, 100.3, 100.8, 300000]]
    ctx = {"liquidity": 500000000, "previous_close": 100.,
           "returns": {f"2026-01-{i:02d}": .001 * (i % 3 - 1) for i in range(1, 21)}}
    other = [[r[0], 100, 100.01, 99.99, 100, 100000] for r in rows]
    sessions, contexts = {"AAA": rows, "BBB": other}, {"AAA": ctx, "BBB": copy.deepcopy(ctx)}
    items, market = scanner.scan(ds, "07:20", sessions, contexts)
    day = {"rows": {r[0]: {("scanner", s): r} for s, rs in sessions.items() for r in rs},
           "contexts": contexts, "legacy": None, "timeline": {"07:20": (items, market)}}
    # Both source symbols share each clock, as the real loader does.
    day["rows"] = {}
    for symbol, source in sessions.items():
        for row in source:
            day["rows"].setdefault(row[0], {})[("scanner", symbol)] = row
    return sessions, contexts, items, market, day


class OpeningResearchTests(unittest.TestCase):
    def test_morning_continuation_uses_entire_eligible_market(self):
        _, _, items, market, _ = fixture()
        self.assertEqual(market["eligible_symbols"], 2)
        self.assertEqual([(i.symbol, i.direction, i.setup) for i in items], [("AAA", "long", "opening_follow")])

    def test_future_extreme_cannot_create_or_remove_signal(self):
        sessions, contexts, items, market, _ = fixture()
        sessions["AAA"].append(["07:20", 100.8, 10000, 1, 1, 100000000])
        self.assertEqual(scanner.scan("2026-02-02", "07:20", sessions, contexts), (items, market))

    def test_missing_completed_signal_bar_cannot_be_interpolated(self):
        sessions, contexts, _, _, _ = fixture()
        sessions["AAA"].pop(-1)
        items, _ = scanner.scan("2026-02-02", "07:20", sessions, contexts)
        self.assertEqual(items, [])

    def test_direction_reversal_requires_closed_reversal_confirmation(self):
        sessions, contexts, _, _, _ = fixture()
        sessions["AAA"][-1] = ["07:15", 101.1, 101.2, 100.7, 100.85, 300000]
        sessions["AAA"][-2][2] = 101.3
        items, _ = scanner.scan("2026-02-02", "07:20", sessions, contexts)
        self.assertEqual([(i.direction, i.setup) for i in items], [("short", "opening_fade")])

    def test_unrecovered_gap_has_previous_close_reference(self):
        sessions, contexts, _, _, _ = fixture()
        contexts["AAA"]["previous_close"] = 101.8
        items, _ = scanner.scan("2026-02-02", "07:20", sessions, contexts)
        self.assertIn(("long", "gap_reclaim"), [(i.direction, i.setup) for i in items])

    def test_feature_vector_ignores_future_prices_and_labels(self):
        _, _, items, market, day = fixture()
        before = learner.vector(items[0], market, day)
        day["rows"]["07:25"] = {("scanner", "AAA"): ["07:25", 200, 300, 1, 2, 100000000]}
        day["net_return_pct"] = 1000000
        self.assertEqual(before, learner.vector(items[0], market, day))
        self.assertEqual(len(before), 19)

    def test_fixed_training_cannot_use_future_outcomes(self):
        rows = [{"date": "2026-01-30", "features": [float(i), 1.], "net_return_pct": float(i) / 10} for i in range(10)]
        before = learner.fit("fixed_ridge", rows, "2026-01-31").predict(np.array([[1., 1.]]))
        rows.append({"date": "2026-02-01", "features": [1., 1.], "net_return_pct": 10000000})
        after = learner.fit("fixed_ridge", rows, "2026-01-31").predict(np.array([[1., 1.]]))
        np.testing.assert_array_equal(before, after)

    def test_missing_exact_next_entry_stays_unfilled(self):
        _, _, items, _, day = fixture()
        day["rows"]["07:30"] = {("scanner", "AAA"): ["07:30", 100.8, 104, 100, 103, 10000]}
        result = replay_events({"2026-02-02": day}, "opening_follow", choose=scanner.gate("opening_follow"))
        self.assertEqual(result["trades"], 0)
        self.assertEqual(result["counters"]["unfilled_missing_entry"], 1)
        self.assertEqual(labels.label(items[0], day), 0.)

    def test_time_exit_waits_for_actual_later_print(self):
        _, _, _, _, day = fixture()
        day["rows"]["07:25"] = {("scanner", "AAA"): ["07:25", 100.8, 100.9, 100.7, 100.8, 10000]}
        day["rows"]["18:45"] = {("scanner", "AAA"): ["18:45", 101.2, 200, 1, 1, 10000]}
        result = replay_events({"2026-02-02": day}, "opening_follow", choose=scanner.gate("opening_follow"))
        self.assertEqual(result["trades"], 1)
        self.assertEqual(result["ledger"][0]["exit_time"], "18:45")
        self.assertEqual(result["ledger"][0]["reason"], "time_exit_open")
        # H/L/C at and after the deadline cannot affect a liquidation at open.
        day["rows"]["18:45"][("scanner", "AAA")][2:] = [101.2, 101.2, 101.2, 10000]
        again = replay_events({"2026-02-02": day}, "opening_follow", choose=scanner.gate("opening_follow"))
        self.assertAlmostEqual(result["pnl_rub"], again["pnl_rub"])

    def test_label_matches_portfolio_net_execution(self):
        _, _, items, _, day = fixture()
        day["rows"]["07:25"] = {("scanner", "AAA"): ["07:25", 100.8, 100.9, 100.7, 100.8, 10000]}
        day["rows"]["07:30"] = {("scanner", "AAA"): ["07:30", 100.8, 104, 100.6, 103, 10000]}
        result = replay_events({"2026-02-02": day}, "opening_follow", choose=scanner.gate("opening_follow"))
        self.assertAlmostEqual(labels.label(items[0], day), result["ledger"][0]["net_return_pct"])

    def test_source_data_required_to_close_position(self):
        _, _, _, _, day = fixture()
        day["rows"]["07:25"] = {("scanner", "AAA"): ["07:25", 100.8, 100.9, 100.7, 100.8, 10000]}
        with self.assertRaisesRegex(ValueError, "Unclosed exposure"):
            replay_events({"2026-02-02": day}, "opening_follow", choose=scanner.gate("opening_follow"))

    def test_ensemble_weights_are_fixed_and_missing_forecast_blocks_entry(self):
        _, _, items, _, _ = fixture()
        item = items[0]
        p = {"rolling_ridge": {item.identity: .4}, "rolling_boosting": {item.identity: .2}}
        self.assertAlmostEqual(consensus.combine(item, p, "mean"), .3)
        self.assertAlmostEqual(consensus.combine(item, p, "consensus"), .2)
        self.assertAlmostEqual(consensus.combine(item, p, "mean_costguard"), .2)
        p["rolling_boosting"].clear()
        import math
        self.assertTrue(math.isnan(consensus.combine(item, p, "mean")))

    def test_paper_month_training_excludes_current_month_and_future(self):
        rows = [{"date": ds} for ds in ("2026-07-31", "2026-08-01", "2026-08-14", "2026-09-01")]
        self.assertEqual(paper.training_rows_for_date(rows, "2026-08-14"), [{"date": "2026-07-31"}])

    def test_more_profit_cannot_bypass_prediction_floor(self):
        _, _, items, _, day = fixture()
        item = items[0]
        day["rows"]["07:25"] = {("scanner", "AAA"): ["07:25", 100.8, 100.9, 100.7, 100.8, 10000]}
        day["rows"]["07:30"] = {("scanner", "AAA"): ["07:30", 100.8, 1000, 100.6, 103, 10000]}
        predictions = {"rolling_ridge": {item.identity: .14}, "rolling_boosting": {item.identity: .14}}
        result = consensus.evaluate({"2026-02-02": day}, "mean", predictions)
        self.assertEqual(result["trades"], 0)


if __name__ == "__main__":
    unittest.main()
