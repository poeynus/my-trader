import tempfile
import unittest
from pathlib import Path

from trader.client import SafetyError
from trader.execution import ExecutionManager
from trader.strategy import StrategyConfig, SymbolConfig


class NoFundsClient:
    def domestic_buying_power(self, symbol, price):
        return {"nrcvb_buy_qty": "0"}

    def domestic_order(self, *args, **kwargs):
        raise AssertionError("주문 API가 호출되면 안 됩니다")


class ExecutionTests(unittest.TestCase):
    def test_zero_buying_power_blocks_before_order(self):
        config = StrategyConfig.load(Path("strategy.json"))
        with tempfile.TemporaryDirectory() as directory:
            manager = ExecutionManager(NoFundsClient(), config, Path(directory) / "state.json")
            with self.assertRaises(SafetyError):
                manager.execute_buy(SymbolConfig("kr", "005930", "NASDAQ", 500000), 1, 100000, True)


if __name__ == "__main__":
    unittest.main()
