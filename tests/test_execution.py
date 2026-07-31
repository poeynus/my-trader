import tempfile
import unittest
from pathlib import Path

from trader.client import SafetyError
from trader.execution import ExecutionManager
from trader.strategy import StrategyConfig, SymbolConfig


class NoFundsClient:
    def domestic_balance(self):
        return {"positions": []}

    def domestic_buying_power(self, symbol, price):
        return {"nrcvb_buy_qty": "0"}

    def domestic_order(self, *args, **kwargs):
        raise AssertionError("주문 API가 호출되면 안 됩니다")


class FullExposureClient(NoFundsClient):
    def domestic_balance(self):
        return {"positions": [{"hldg_qty": "2", "pchs_avg_pric": "100000"}]}

    def domestic_buying_power(self, symbol, price):
        raise AssertionError("동시 투자 한도 초과 시 주문가능금액 API가 호출되면 안 됩니다")


class ExecutionTests(unittest.TestCase):
    def test_active_exposure_blocks_buy_until_position_is_sold(self):
        config = StrategyConfig.load(Path("strategy.json"))
        with tempfile.TemporaryDirectory() as directory:
            manager = ExecutionManager(FullExposureClient(), config, Path(directory) / "state.json")
            with self.assertRaisesRegex(SafetyError, "동시 투자 한도"):
                manager.execute_buy(SymbolConfig("kr", "005930", "NASDAQ", 60000), 1, 50000, True)

    def test_zero_buying_power_blocks_before_order(self):
        config = StrategyConfig.load(Path("strategy.json"))
        with tempfile.TemporaryDirectory() as directory:
            manager = ExecutionManager(NoFundsClient(), config, Path(directory) / "state.json")
            with self.assertRaises(SafetyError):
                manager.execute_buy(SymbolConfig("kr", "005930", "NASDAQ", 500000), 1, 100000, True)


if __name__ == "__main__":
    unittest.main()
