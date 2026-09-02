import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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
            opened = datetime.now(timezone.utc) - timedelta(minutes=31)
            result = self._trader(directory, 99.1, opened_at=opened).run_once()[0]
        self.assertEqual((result["action"], result["reason"]), ("sell", "time_stop"))

    def test_one_percent_stop_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._trader(directory, 98.9).run_once()[0]
        self.assertEqual((result["action"], result["reason"]), ("sell", "stop_loss"))

    def test_overnight_position_exits_before_other_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            opened = datetime.now(timezone.utc) - timedelta(days=1)
            result = self._trader(directory, 100.1, opened_at=opened).run_once()[0]
        self.assertEqual((result["action"], result["reason"]), ("sell", "overnight_exit"))

    def test_intraday_entry_waits_for_pullback_then_confirms_reclaim(self):
        with tempfile.TemporaryDirectory() as directory:
            trader = self._trader(directory, 100)
            key = "kr:005930"
            now = datetime.now(timezone.utc)
            trader.state["price_samples"] = {key: [
                {"time": (now - timedelta(seconds=120)).isoformat(), "price": 100, "volume": 1000},
                {"time": (now - timedelta(seconds=60)).isoformat(), "price": 100.1, "volume": 1100},
            ]}
            armed = trader._intraday_entry_signal(key, 100.8, 1200, 1.0, 99.0)
            pulled_back = trader._intraday_entry_signal(key, 100.4, 1300, 1.0, 99.0)
            first_reclaim = trader._intraday_entry_signal(key, 100.72, 1450, 1.0, 99.0)
            confirmed = trader._intraday_entry_signal(key, 100.75, 1650, 1.0, 99.0)
        self.assertEqual(armed["reason"], "intraday_wait_pullback")
        self.assertEqual(pulled_back["reason"], "intraday_wait_reclaim")
        self.assertFalse(first_reclaim["ready"])
        self.assertTrue(confirmed["ready"])
        self.assertEqual(confirmed["reason"], "intraday_pullback_reclaim")

    def test_intraday_momentum_keeps_daily_average_as_support_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            trader = self._trader(directory, 100)
            key = "kr:005930"
            now = datetime.now(timezone.utc)
            trader.state["price_samples"] = {key: [
                {"time": (now - timedelta(seconds=120)).isoformat(), "price": 100, "volume": 1000},
                {"time": (now - timedelta(seconds=60)).isoformat(), "price": 100.1, "volume": 1100},
            ]}
            signal = trader._intraday_entry_signal(key, 100.8, 1200, 1.0, 101.0)
        self.assertFalse(signal["ready"])
        self.assertEqual(signal["reason"], "intraday_below_daily_support")

    def test_intraday_entry_rejects_overextended_move(self):
        with tempfile.TemporaryDirectory() as directory:
            trader = self._trader(directory, 100)
            key = "kr:005930"
            now = datetime.now(timezone.utc)
            trader.state["price_samples"] = {key: [
                {"time": (now - timedelta(seconds=120)).isoformat(), "price": 100, "volume": 1000},
                {"time": (now - timedelta(seconds=60)).isoformat(), "price": 100.5, "volume": 1100},
            ]}
            signal = trader._intraday_entry_signal(key, 101.6, 1300, 2.0, 99.0)
        self.assertFalse(signal["ready"])
        self.assertEqual(signal["reason"], "intraday_overextended")

    def test_kr_entry_window_closes_at_ten(self):
        with tempfile.TemporaryDirectory() as directory:
            trader = self._trader(directory, 100)
            before = datetime(2026, 9, 2, 9, 59, tzinfo=ZoneInfo("Asia/Seoul"))
            cutoff = datetime(2026, 9, 2, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))
            self.assertFalse(trader._entry_window_closed("kr", before))
            self.assertTrue(trader._entry_window_closed("kr", cutoff))

    def test_first_cost_adjusted_loss_blocks_second_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            trader = self._trader(directory, 100)
            trader.state["intraday"] = {}
            trader._record_exit(self.item, 100000, 1, 100100)
            intraday = trader._intraday("kr")
        self.assertLess(intraday["first_exit_net_pnl"], 0)


if __name__ == "__main__":
    unittest.main()
