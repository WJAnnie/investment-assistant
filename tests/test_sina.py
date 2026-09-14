import unittest

from app.market.models import Quote
from app.market.sina import SinaProvider


class _FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class _FakeSession:
    def __init__(self, text):
        self.text = text
        self.requests = []

    def get(self, url, headers, timeout):
        self.requests.append((url, headers, timeout))
        return _FakeResponse(self.text)


class SinaProviderTests(unittest.TestCase):
    def test_fetch_parses_realtime_index_quote(self):
        session = _FakeSession(
            'var hq_str_s_sh000001="上证指数,3864.3593,-70.0443,-1.78,3821485,60148803";'
        )
        provider = SinaProvider(session=session, clock=lambda: 0)

        quote = provider.fetch("SH.000001")

        self.assertEqual(
            quote,
            Quote("SH.000001", "上证指数", 3864.3593, -1.78, "1970-01-01T00:00:00+00:00"),
        )
        self.assertEqual(session.requests[0][0], "https://hq.sinajs.cn/list=s_sh000001")

    def test_fetch_parses_realtime_stock_quote_and_rejects_hk(self):
        session = _FakeSession(
            'var hq_str_sz000001="平安银行,10.10,10.00,10.25,10.30,9.98,10.24,10.25,120000,1230000,10.24,100,10.23,200";'
        )
        provider = SinaProvider(session=session, clock=lambda: 0)

        quote = provider.fetch("000001")
        with self.assertRaises(ValueError):
            provider.fetch("00700.HK")

        self.assertEqual(quote.name, "平安银行")
        self.assertAlmostEqual(quote.price, 10.25)
        self.assertAlmostEqual(quote.change, 2.5)


if __name__ == "__main__":
    unittest.main()
