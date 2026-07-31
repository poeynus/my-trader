import tempfile
import unittest
from pathlib import Path

from trader.client import KISClient, SafetyError
from trader.config import Settings
from trader.http import Response


class FakeTransport:
    def __init__(self):
        self.calls = []

    def request(self, method, url, headers, params=None, body=None, timeout=10):
        self.calls.append({"method": method, "url": url, "headers": headers, "params": params, "body": body})
        if url.endswith("/oauth2/tokenP"):
            return Response({"access_token": "token", "expires_in": 3600}, {})
        if url.endswith("/inquire-price"):
            return Response({"rt_cd": "0", "output": {"stck_prpr": "70000"}}, {})
        return Response({"rt_cd": "0", "output": {"ok": True}}, {})


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.settings = Settings("key", "secret", "12345678", enable_real_trading=True, token_cache_path=Path(self.tempdir.name) / "token.json")
        self.transport = FakeTransport()
        self.client = KISClient(self.settings, self.transport)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_order_requires_confirmation(self):
        with self.assertRaises(SafetyError):
            self.client.domestic_order("buy", "005930", 1, 70000, False)
        self.assertEqual(self.transport.calls, [])

    def test_order_limit_blocks_request(self):
        with self.assertRaises(SafetyError):
            self.client.domestic_order("buy", "005930", 100, 70000, True)
        self.assertEqual(self.transport.calls, [])

    def test_us_buy_uses_official_real_tr_id_and_exchange(self):
        self.client.us_order("buy", "aapl", "NASDAQ", 1, 150, True)
        order = self.transport.calls[-1]
        self.assertEqual(order["headers"]["tr_id"], "TTTT1002U")
        self.assertEqual(order["body"]["OVRS_EXCG_CD"], "NASD")
        self.assertEqual(order["body"]["PDNO"], "AAPL")

    def test_real_order_is_locked(self):
        real = Settings("key", "secret", "12345678", token_cache_path=Path(self.tempdir.name) / "real.json")
        with self.assertRaises(SafetyError):
            KISClient(real, self.transport).us_order("sell", "AAPL", "NASDAQ", 1, 150, True)

    def test_domestic_market_order_checks_estimated_notional(self):
        with self.assertRaises(SafetyError):
            self.client.domestic_order("buy", "005930", 100, 0, True)
        self.assertFalse(any(call["url"].endswith("/order-cash") for call in self.transport.calls))


if __name__ == "__main__":
    unittest.main()
