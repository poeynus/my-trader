import tempfile
import unittest
from pathlib import Path

from trader.report import DailyReporter
from trader.strategy import StrategyConfig


class ReportTests(unittest.TestCase):
    def test_estimated_net_pnl_includes_both_side_commission_and_sell_cost(self):
        config = StrategyConfig.load(Path("strategy.json"))
        events = [
            {"action": "buy", "order": {"filled_quantity": 1, "average_price": 100}},
            {"action": "sell", "order": {"filled_quantity": 1, "average_price": 101}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            reporter = DailyReporter(None, config, reports_dir=Path(directory))
            metrics = reporter._estimated_cost_metrics("us", events, 1.0)
        expected_cost = 201 * 0.25 / 100 + 101 * 0.003 / 100
        self.assertAlmostEqual(metrics["estimated_trading_cost"], expected_cost)
        self.assertAlmostEqual(metrics["estimated_net_pnl"], 1.0 - expected_cost)

    def test_simulated_orders_are_included_in_turnover(self):
        config = StrategyConfig.load(Path("strategy.json"))
        events = [{"action": "buy", "quantity": 2, "limit_price": 10,
                   "order": {"simulated": True}}]
        with tempfile.TemporaryDirectory() as directory:
            reporter = DailyReporter(None, config, reports_dir=Path(directory))
            metrics = reporter._estimated_cost_metrics("us", events, 0)
        self.assertEqual(metrics["buy_notional"], 20)


if __name__ == "__main__":
    unittest.main()
