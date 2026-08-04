import unittest

from trader.pricing import domestic_tick_size, round_domestic_order_price


class PricingTests(unittest.TestCase):
    def test_observed_kospi_order_rounds_to_50_won_tick(self):
        self.assertEqual(round_domestic_order_price(14080 * 1.002, "buy", "KOSPI"), 14150)

    def test_observed_kosdaq_order_rounds_to_50_won_tick(self):
        self.assertEqual(round_domestic_order_price(48900 * 1.002, "buy", "KOSDAQ"), 49000)

    def test_sell_rounds_down(self):
        self.assertEqual(round_domestic_order_price(14080 * 0.998, "sell", "KOSPI"), 14050)

    def test_high_price_tick_differs_by_market(self):
        self.assertEqual(domestic_tick_size(150000, "KOSPI"), 500)
        self.assertEqual(domestic_tick_size(150000, "KOSDAQ"), 100)


if __name__ == "__main__":
    unittest.main()
