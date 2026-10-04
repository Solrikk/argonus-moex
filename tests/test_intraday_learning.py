import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from argonus.market_data import fetch_moex_research as fetch
from argonus.research import research_intraday_learning as learning
from tests.test_intraday_portfolio import snap


class DataTests(unittest.TestCase):
    def test_minute_aggregation_keeps_actual_turnover_and_no_synthetic_prices(self):
        rows = [["2026-07-17 09:50:00", 100, 101, 99, 100.5, 100, 10_050],
                ["2026-07-17 09:53:00", 100.5, 102, 100, 101, 200, 20_200],
                ["2026-07-17 09:55:00", 101, 102, 100, 100.5, 50, 5_025],
                ["2026-07-17 18:40:00", 900, 950, 800, 940, 1000, 900_000]]
        result = fetch.aggregate_five_min(rows)["2026-07-17"]
        self.assertEqual(result[0], ["09:50", 100, 102, 99, 101, 300, 30_250])
        self.assertEqual(len(result), 2)

    def test_empty_minute_data_not_fabricated(self):
        self.assertEqual(fetch.aggregate_five_min([]), {})

    def test_cache_replays_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            value = [["2026-07-17 09:50:00", 100, 101, 99, 100, 1, 100]]
            fetch.write_json(path / "SBER_1_2026-07-17_2026-07-17_0.json", value)
            with patch("argonus.market_data.fetch_moex_research.urlopen", side_effect=AssertionError("network used")):
                self.assertEqual(fetch.fetch_pages("SBER", "2026-07-17", "2026-07-17", 1, path), value)


class LearningTests(unittest.TestCase):
    def test_vectors_exclude_ticker_and_date_identity(self):
        from dataclasses import replace
        one = learning.vectors([snap("A")])
        two = learning.vectors([replace(snap("B"), date="2026-02-01")])
        self.assertEqual(one[0]["features"], two[0]["features"])
        self.assertEqual(len(one[0]["features"]), len(learning.FEATURE_NAMES))

    def test_train_cannot_read_future_labels(self):
        rows = []
        for i in range(25):
            rows.append({"date": f"2026-01-{i+1:02d}", "features": [i, i % 3], "net_return_pct": i / 10 - 1})
        future = {"date": "2026-04-01", "features": [999, 999], "net_return_pct": 100}
        model = learning.fit_model("ridge", rows + [future], "2026-01-31")
        future["net_return_pct"] = -100000
        second = learning.fit_model("ridge", rows + [future], "2026-01-31")
        np.testing.assert_allclose(model.predict([[4, 1], [9, 2]]), second.predict([[4, 1], [9, 2]]))

    def test_select_uses_predictions_not_realized_labels(self):
        class Predictor:
            def predict(self, x):
                return x[:, 0]
        rows = [{"date": "2026-01-05", "symbol": "A", "direction": "long", "features": [.5], "net_return_pct": -99},
                {"date": "2026-01-05", "symbol": "B", "direction": "short", "features": [.1], "net_return_pct": 999}]
        picks = learning.pick_rows(Predictor(), rows)
        self.assertEqual([r["symbol"] for r in picks["2026-01-05"]], ["A"])

    def test_never_selects_both_directions_of_one_symbol(self):
        class Predictor:
            def predict(self, x):
                return np.ones(len(x))
        rows = [{"date": "2026-01-05", "symbol": symbol, "direction": direction, "features": [.5]}
                for symbol in ("A", "B", "C", "D") for direction in ("long", "short")]
        picks = learning.pick_rows(Predictor(), rows)["2026-01-05"]
        self.assertEqual(len(picks), 3)
        self.assertEqual(len({r["symbol"] for r in picks}), 3)


if __name__ == "__main__":
    unittest.main()
