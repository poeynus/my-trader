import unittest
from pathlib import Path

from trader.strategy import StrategyConfig, SymbolConfig
from trader.universe import UniverseSelector


class TrendClient:
    def __init__(self, closes):
        self.closes = closes

    def domestic_daily_prices(self, symbol, days):
        return [{"stck_clpr": str(x)} for x in self.closes]


class MomentumClient:
    def domestic_turnover_rank(self):
        return [
            {"mksc_shrn_iscd": "000001", "stck_prpr": "10000", "prdy_ctrt": "-3", "vol_inrt": "500",
             "lstn_stcn": "20000000", "acml_tr_pbmn": "999999999999"},
            {"mksc_shrn_iscd": "000002", "stck_prpr": "10000", "prdy_ctrt": "2", "vol_inrt": "100",
             "lstn_stcn": "20000000", "acml_tr_pbmn": "10000000000"},
            {"mksc_shrn_iscd": "000003", "stck_prpr": "10000", "prdy_ctrt": "5", "vol_inrt": "50",
             "lstn_stcn": "20000000", "acml_tr_pbmn": "10000000000"},
        ]


class UniverseTests(unittest.TestCase):
    def test_intraday_momentum_prefers_positive_stronger_move(self):
        config = StrategyConfig.load(Path("strategy.json"))
        symbols = UniverseSelector(MomentumClient(), config).momentum_candidates("kr")
        self.assertEqual([x.symbol for x in symbols], ["000003", "000002"])

    def test_trend_filter_accepts_fast_ma_above_slow_ma(self):
        config = StrategyConfig.load(Path("strategy.json"))
        newest_first = list(reversed([100] * 16 + [110, 112, 114, 116, 118]))
        selector = UniverseSelector(TrendClient(newest_first), config)
        self.assertTrue(selector._trend_up(SymbolConfig("kr", "005930", "NASDAQ", 200000)))

    def test_trend_filter_rejects_fast_ma_below_slow_ma(self):
        config = StrategyConfig.load(Path("strategy.json"))
        newest_first = list(reversed([100] * 16 + [90, 88, 86, 84, 82]))
        selector = UniverseSelector(TrendClient(newest_first), config)
        self.assertFalse(selector._trend_up(SymbolConfig("kr", "005930", "NASDAQ", 200000)))


if __name__ == "__main__":
    unittest.main()
