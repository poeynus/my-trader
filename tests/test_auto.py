import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trader.auto import AutoTrader, DAILY_CACHE
from trader.strategy import StrategyConfig, SymbolConfig


class PriceClient:
    def __init__(self, price):
        self.price = price

    def domestic_quote(self, symbol):
        return {"stck_prpr": str(self.price)}

    def domestic_daily_prices(self, symbol, days):
        return [{"stck_clpr": str(x)} for x in reversed([100] * 16 + [101, 102, 103, 104, 105])]


class AutoTraderTests(unittest.TestCase):
    def setUp(self):
        DAILY_CACHE.clear()
        self.config = StrategyConfig.load(Path("strategy.json"))
        self.item = SymbolConfig("kr", "005930", "NASDAQ", 200000)

    def _trader(self, directory, price, peak=0, opened_at=None):
        state = {
            "virtual_positions": {"kr:005930": {"quantity": 1, "average_price": 100}},
            "last_actions": {}, "intraday": {},
            "position_meta": {"kr:005930": {"opened_at": (opened_at or datetime.now(timezone.utc)).isoformat(),
                                                "peak_profit_percent": peak}},
        }
        state_path = Path(directory) / "state.json"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        return AutoTrader(PriceClient(price), replace(self.config, symbols=[self.item], execution_mode="dry_run", live_markets=[]),
                          state_path=state_path, log_path=Path(directory) / "trades.jsonl")

    def test_force_exit_sells_dry_run_position(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._trader(directory, 101).run_once(False, allow_new_entries=False, force_exit=True)[0]
        self.assertEqual((result["action"], result["reason"]), ("sell", "market_close"))

    def test_trailing_stop_sells_after_giveback(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._trader(directory, 101.4, peak=2.0).run_once()[0]
        self.assertEqual((result["action"], result["reason"]), ("sell", "trailing_stop"))

    def test_time_stop_releases_stale_losing_position(self):
        with tempfile.TemporaryDirectory() as directory:
            opened = datetime.now(timezone.utc) - timedelta(minutes=16)
            result = self._trader(directory, 99.6, opened_at=opened).run_once()[0]
        self.assertEqual((result["action"], result["reason"]), ("sell", "time_stop"))

    def test_overnight_position_exits_before_other_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            opened = datetime.now(timezone.utc) - timedelta(days=1)
            result = self._trader(directory, 100.1, opened_at=opened).run_once()[0]
        self.assertEqual((result["action"], result["reason"]), ("sell", "overnight_exit"))


if __name__ == "__main__":
    unittest.main()
