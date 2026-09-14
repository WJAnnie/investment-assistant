import importlib
import unittest

from app.chan.models import KLine


class _FakeFrame:
    def __init__(self, rows):
        self.rows = rows

    def to_dict(self, orient):
        if orient != "records":
            raise AssertionError("unexpected orientation")
        return list(self.rows)


class _FakeAkShare:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def stock_zh_index_daily_tx(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeFrame(self.rows)


class TencentHistoryProviderTests(unittest.TestCase):
    @staticmethod
    def _provider_type():
        try:
            module = importlib.import_module("app.market.tencent")
        except ModuleNotFoundError:
            raise AssertionError("TencentHistoryProvider is missing") from None
        return module.TencentHistoryProvider

    def test_fetch_klines_maps_tencent_rows_and_requested_window(self):
        akshare = _FakeAkShare([
            {"date": "2026-09-11", "open": 10.0, "close": 10.4,
             "high": 10.5, "low": 9.9, "amount": 2000},
            {"date": "2026-09-10", "open": 9.8, "close": 10.0,
             "high": 10.1, "low": 9.7, "amount": 1000},
        ])
        provider = self._provider_type()(ak_module=akshare)

        lines = provider.fetch_klines(
            "SH.600660", start_date="20250901", end_date="20260913"
        )

        self.assertEqual(lines, [
            KLine("2026-09-10", 9.8, 10.1, 9.7, 10.0, 1000.0),
            KLine("2026-09-11", 10.0, 10.5, 9.9, 10.4, 2000.0),
        ])
        self.assertEqual(akshare.calls, [{
            "symbol": "sh600660",
            "start_date": "20250901",
            "end_date": "20260913",
        }])

    def test_fetch_klines_supports_shenzhen_etf_codes(self):
        akshare = _FakeAkShare([
            {"date": "2026-09-11", "open": 1.0, "close": 1.1,
             "high": 1.2, "low": 0.9, "amount": 100},
            {"date": "2026-09-12", "open": 1.1, "close": 1.2,
             "high": 1.3, "low": 1.0, "amount": 120},
        ])
        provider = self._provider_type()(ak_module=akshare)

        provider.fetch_klines("159500")

        self.assertEqual(akshare.calls[0]["symbol"], "sz159500")

    def test_fetch_klines_rejects_unsupported_period_and_invalid_rows(self):
        provider_type = self._provider_type()
        with self.assertRaisesRegex(ValueError, "daily"):
            provider_type(ak_module=_FakeAkShare([])).fetch_klines(
                "600660", period="weekly"
            )

        invalid = _FakeAkShare([
            {"date": "2026-09-11", "open": 10.0, "close": 10.4,
              "high": 9.0, "low": 9.9, "amount": 2000}
        ])
        with self.assertRaisesRegex(ValueError, "invalid Tencent K-line row"):
            provider_type(ak_module=invalid).fetch_klines("600660")

    def test_fetch_klines_maps_plain_shanghai_codes_and_forwards_daily_qfq(self):
        akshare = _FakeAkShare([
            {"date": "2026-09-11", "open": 4.0, "close": 4.1,
              "high": 4.2, "low": 3.9, "amount": 800},
        ])
        provider = self._provider_type()(ak_module=akshare)

        provider.fetch_klines("600660", period="daily", adjust="qfq")
        provider.fetch_klines("510300")

        self.assertEqual(
            [call["symbol"] for call in akshare.calls],
            ["sh600660", "sh510300"],
        )

    def test_fetch_klines_fails_closed_for_unsupported_adjustment(self):
        provider = self._provider_type()(ak_module=_FakeAkShare([]))

        with self.assertRaisesRegex(ValueError, "adjustment"):
            provider.fetch_klines("600660", adjust="hfq")

    def test_fetch_klines_fails_closed_for_invalid_codes(self):
        provider = self._provider_type()(ak_module=_FakeAkShare([]))

        for code in ("", "60066", "6006600", "HK.00700", "600660.SH", "sh600660"):
            with self.assertRaisesRegex(ValueError, "code"):
                provider.fetch_klines(code)

    def test_fetch_klines_fails_closed_for_non_finite_values_and_bad_dates(self):
        provider_type = self._provider_type()

        non_finite = _FakeAkShare([
            {"date": "2026-09-11", "open": 10.0, "close": "n/a",
              "high": 10.5, "low": 9.9, "amount": 2000}
        ])
        with self.assertRaisesRegex(ValueError, "invalid Tencent K-line row"):
            provider_type(ak_module=non_finite).fetch_klines("600660")

        bad_date = _FakeAkShare([
            {"date": "2026/09/11", "open": 10.0, "close": 10.4,
              "high": 10.5, "low": 9.9, "amount": 2000}
        ])
        with self.assertRaisesRegex(ValueError, "invalid Tencent K-line row"):
            provider_type(ak_module=bad_date).fetch_klines("600660")

        empty_date = _FakeAkShare([{}])
        with self.assertRaisesRegex(ValueError, "invalid Tencent K-line row"):
            provider_type(ak_module=empty_date).fetch_klines("600660")

    def test_fetch_klines_returns_empty_list_when_history_has_no_rows(self):
        provider = self._provider_type()(ak_module=_FakeAkShare([]))

        self.assertEqual(provider.fetch_klines("600660"), [])


if __name__ == "__main__":
    unittest.main()
