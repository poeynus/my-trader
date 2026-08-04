import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from trader.autopilot import Autopilot
from trader.strategy import StrategyConfig
from trader.universe import UniverseSelector


class BalanceClient:
    def us_balance(self):
        return {"positions": [
            {"ovrs_pdno": "CMCSA", "ovrs_cblc_qty": "1", "ovrs_excg_cd": "NASD"},
            {"ovrs_pdno": "NOW", "ovrs_cblc_qty": "1", "ovrs_excg_cd": "NYSE"},
        ]}


class AutopilotTests(unittest.TestCase):
    def test_refresh_restores_held_symbol_missing_from_universe(self):
        config = replace(StrategyConfig.load(Path("strategy.json")), live_markets=["us"])
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            try:
                os.chdir(directory)
                UniverseSelector.save([])
                autopilot = Autopilot(BalanceClient(), config,
                                      state_path=Path("autopilot.json"), lock_path=Path("lock"))
                with patch.object(UniverseSelector, "momentum_candidates", return_value=[]):
                    autopilot._refresh_universe("us")
                symbols = UniverseSelector.load()
            finally:
                os.chdir(previous)
        self.assertEqual([(x.symbol, x.exchange) for x in symbols],
                         [("CMCSA", "NASDAQ"), ("NOW", "NYSE")])


if __name__ == "__main__":
    unittest.main()
