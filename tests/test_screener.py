import unittest

from trader.realtime import RealtimeExitMonitor


class RealtimeParserTests(unittest.TestCase):
    def test_domestic_tick_uses_last_and_bid(self):
        fields = ["005930", "101010", "70000"] + ["0"] * 8 + ["69900"] + ["0"] * 34
        rows = RealtimeExitMonitor.parse_message("0|H0STCNT0|001|" + "^".join(fields))
        self.assertEqual(rows[0]["symbol"], "005930")
        self.assertEqual(rows[0]["last"], "70000")
        self.assertEqual(rows[0]["bid"], "69900")

    def test_system_message_is_ignored(self):
        self.assertEqual(RealtimeExitMonitor.parse_message('{"header": {"tr_id": "PINGPONG"}}'), [])


if __name__ == "__main__":
    unittest.main()
