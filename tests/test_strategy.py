import unittest

from trader.strategy import StrategyError, moving_average_signal


class StrategyTests(unittest.TestCase):
    def test_cross_up(self):
        oldest_first = [10, 10, 10, 10, 10, 9, 12]
        result = moving_average_signal(list(reversed(oldest_first)), fast=2, slow=5)
        self.assertEqual(result["signal"], "cross_up")

    def test_requires_enough_prices(self):
        with self.assertRaises(StrategyError):
            moving_average_signal([1, 2], fast=2, slow=5)


if __name__ == "__main__":
    unittest.main()
