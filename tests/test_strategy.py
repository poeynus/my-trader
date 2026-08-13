import json
import tempfile
import unittest
from pathlib import Path

from trader.strategy import StrategyConfig, StrategyError, moving_average_signal


class StrategyTests(unittest.TestCase):
    def test_auto_discover_does_not_require_symbols(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategy.json"
            path.write_text(json.dumps({"auto_discover": True}), encoding="utf-8")
            config = StrategyConfig.load(path)
        self.assertEqual(config.symbols, [])

    def test_live_markets_enable_both_markets(self):
        config = StrategyConfig.load(Path("strategy.json"))
        self.assertEqual(config.mode_for("kr"), "live")
        self.assertEqual(config.mode_for("us"), "live")
        self.assertTrue(config.has_live_market)

    def test_cross_up(self):
        oldest_first = [10, 10, 10, 10, 10, 9, 12]
        result = moving_average_signal(list(reversed(oldest_first)), fast=2, slow=5)
        self.assertEqual(result["signal"], "cross_up")

    def test_requires_enough_prices(self):
        with self.assertRaises(StrategyError):
            moving_average_signal([1, 2], fast=2, slow=5)


if __name__ == "__main__":
    unittest.main()
