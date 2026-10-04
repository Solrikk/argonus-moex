"""Guard model cutoffs and rank by expected net return, never outcome labels."""
import unittest
from dataclasses import replace

import numpy as np

from argonus.strategies import continuous_intraday as core
from argonus.research import research_scanner_expectancy as learner


class NetPredictionTests(unittest.TestCase):
    def contexts(self):
        return {symbol: {"liquidity": 1e8, "returns": {str(i): i / 1000 for i in range(20)}} for symbol in "AB"}

    def items(self):
        base = core.Opportunity("2026-01-05", "A", "long", "rolling_breakout", "10:30", 100, 1, 1, 2, 1e8)
        return [base, replace(base, symbol="B", score=100)]

    def test_prediction_threshold_and_order_override_raw_signal_strength(self):
        class Model:
            def predict(self, x):
                return np.array([.30, .10])
        selected = learner.gate(Model())(self.items(), {"efficiency": .5}, self.contexts())
        self.assertEqual([x.symbol for x in selected], ["A"])
        self.assertEqual(selected[0].score, .30)

    def test_feature_vector_ignores_future_outcome_and_ticker_identity(self):
        items = self.items()
        items[1] = replace(items[0], symbol="B")
        market = {"efficiency": .5, "market_return_pct": 1}
        expected = learner.vector(items[0], market, self.contexts())
        self.assertEqual(expected, learner.vector(items[1], market | {"future_return": 9999}, self.contexts()))
        self.assertEqual(len(expected), 12)

    def test_training_cutoff_ignores_future_labels_and_features(self):
        rows = [{"date": "2026-01-05", "features": [i] * 12, "net_return_pct": i / 10} for i in range(3)]
        past = learner.fit("ridge", rows, "2026-01-31")
        future = {"date": "2026-02-01", "features": [1e20] * 12, "net_return_pct": -1e20}
        together = learner.fit("ridge", rows + [future], "2026-01-31")
        np.testing.assert_allclose(past.predict([[1] * 12]), together.predict([[1] * 12]))

    def test_selection_keeps_current_architecture_when_a_new_rule_only_ties(self):
        values = {name: {"pnl_rub": 0, "daily_close_mdd_pct": 0} for name in learner.NAMES}
        values["current_target125"]["pnl_rub"] = 100
        values["target125_then_ridge"]["pnl_rub"] = 100
        self.assertEqual(learner.pick(values), "current_target125")

    def test_negative_expected_profit_produces_no_order_intent(self):
        class Model:
            def predict(self, x):
                return np.array([-.2, -.1])
        self.assertEqual(learner.gate(Model())(self.items(), {}, self.contexts()), [])


if __name__ == "__main__":
    unittest.main()
