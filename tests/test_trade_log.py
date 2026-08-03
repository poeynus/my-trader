import json
import tempfile
import unittest
from pathlib import Path

from trader.trade_log import append_event, read_event_lines


class TradeLogTests(unittest.TestCase):
    def test_us_log_uses_us_market_day(self):
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            event = {"time": "2026-08-04T03:00:00+09:00", "market": "us", "action": "hold"}
            path = append_event(event, log_dir=log_dir)
            self.assertEqual(path.name, "2026-08-03-us.jsonl")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["market"], "us")

    def test_reader_combines_legacy_and_daily_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy, log_dir = root / "trades.jsonl", root / "logs"
            legacy.write_text('{"source":"legacy"}\n', encoding="utf-8")
            append_event({"time": "2026-08-04T10:00:00+09:00", "market": "kr", "source": "daily"}, log_dir=log_dir)
            lines = list(read_event_lines("kr", "2026-08-04", legacy_path=legacy, log_dir=log_dir))
            self.assertEqual([json.loads(x)["source"] for x in lines], ["legacy", "daily"])


if __name__ == "__main__":
    unittest.main()
