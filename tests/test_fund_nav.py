import unittest
from unittest.mock import patch

from app.chan.models import KLine
from app.market.fund_nav import AkShareFundNavProvider
from app.market.models import Quote


class _Frame:
    def __init__(self, records):
        self.records = records

    def to_dict(self, orient):
        assert orient == "records"
        return list(self.records)


class _FakeAkShare:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def fund_open_fund_info_em(self, **kwargs):
        self.calls.append(kwargs)
        return _Frame(self.records)


class FundNavProviderTests(unittest.TestCase):
    RECORDS = [
        {"净值日期": "2026-09-09", "单位净值": 1.1000, "日增长率": 1.0},
        {"净值日期": "2026-09-10", "单位净值": 1.1110, "日增长率": 1.0},
    ]

    def test_fetch_returns_latest_nav_quote_with_source_date(self):
        fake = _FakeAkShare(self.RECORDS)
        provider = AkShareFundNavProvider(ak_module=fake)

        quote = provider.fetch("016496")

        self.assertEqual(
            quote,
            Quote(
                "016496",
                "016496",
                1.111,
                1.0,
                "2026-09-10",
                "akshare_fund_nav",
                "2026-09-10",
            ),
        )
        self.assertEqual(
            fake.calls,
            [{"symbol": "016496", "indicator": "单位净值走势"}],
        )

    def test_fetch_klines_maps_all_nav_rows_to_flat_daily_klines(self):
        provider = AkShareFundNavProvider(ak_module=_FakeAkShare(self.RECORDS))

        lines = provider.fetch_klines("016496")

        self.assertEqual(
            lines,
            [
                KLine("2026-09-09", 1.1, 1.1, 1.1, 1.1, 0.0),
                KLine("2026-09-10", 1.111, 1.111, 1.111, 1.111, 0.0),
            ],
        )

    def test_rejects_invalid_fund_codes_and_nav_values(self):
        provider = AkShareFundNavProvider(ak_module=_FakeAkShare(self.RECORDS))
        with self.assertRaises(ValueError):
            provider.fetch("16496")

        invalid = _FakeAkShare([
            {"净值日期": "2026-09-10", "单位净值": 0, "日增长率": 1.0},
        ])
        with self.assertRaises(ValueError):
            AkShareFundNavProvider(ak_module=invalid).fetch("016496")

    def test_rejects_invalid_latest_row_instead_of_falling_back_to_older_nav(self):
        invalid_latest = _FakeAkShare([
            {"净值日期": "2026-09-09", "单位净值": 1.1000, "日增长率": 1.0},
            {"净值日期": "2026-09-10", "单位净值": float("nan"), "日增长率": 1.0},
        ])
        with self.assertRaises(ValueError):
            AkShareFundNavProvider(ak_module=invalid_latest).fetch("016496")

        overflowing_latest = _FakeAkShare([
            {"净值日期": "2026-09-09", "单位净值": 1.1000, "日增长率": 1.0},
            {"净值日期": "2026-09-10", "单位净值": 10 ** 1000, "日增长率": 1.0},
        ])
        with self.assertRaises(ValueError):
            AkShareFundNavProvider(ak_module=overflowing_latest).fetch("016496")

    def test_rejects_invalid_latest_date_and_change(self):
        for latest in (
            {"净值日期": "not-a-date", "单位净值": 1.2, "日增长率": 1.0},
            {"净值日期": "2026-09-10", "单位净值": 1.2, "日增长率": float("inf")},
        ):
            fake = _FakeAkShare([
                {"净值日期": "2026-09-09", "单位净值": 1.1, "日增长率": 1.0},
                latest,
            ])
            with self.subTest(latest=latest), self.assertRaises(ValueError):
                AkShareFundNavProvider(ak_module=fake).fetch("016496")

    def test_fetch_klines_ignores_missing_or_nan_daily_change(self):
        fake = _FakeAkShare([
            {"净值日期": "2026-09-09", "单位净值": 1.1},
            {"净值日期": "2026-09-10", "单位净值": 1.2, "日增长率": float("nan")},
        ])

        lines = AkShareFundNavProvider(ak_module=fake).fetch_klines("016496")

        self.assertEqual(
            lines,
            [
                KLine("2026-09-09", 1.1, 1.1, 1.1, 1.1, 0.0),
                KLine("2026-09-10", 1.2, 1.2, 1.2, 1.2, 0.0),
            ],
        )

    def test_fetch_klines_fails_closed_for_any_invalid_date_or_nav(self):
        for invalid_row in (
            {"净值日期": "2026-09-10 trailing", "单位净值": 1.2},
            {"净值日期": "2026-09-10", "单位净值": 0},
            {"净值日期": "2026-09-10", "单位净值": float("inf")},
        ):
            fake = _FakeAkShare([
                {"净值日期": "2026-09-09", "单位净值": 1.1},
                invalid_row,
            ])
            with self.subTest(invalid_row=invalid_row), self.assertRaises(ValueError):
                AkShareFundNavProvider(ak_module=fake).fetch_klines("016496")

    def test_date_parser_rejects_trailing_garbage(self):
        fake = _FakeAkShare([
            {"净值日期": "2026-09-10 trailing", "单位净值": 1.2, "日增长率": 1.0},
        ])
        with self.assertRaises(ValueError):
            AkShareFundNavProvider(ak_module=fake).fetch("016496")

    def test_fetch_imports_akshare_lazily(self):
        fake = _FakeAkShare(self.RECORDS)
        with patch("app.market.fund_nav.importlib.import_module", return_value=fake) as importer:
            AkShareFundNavProvider().fetch("016496")

        importer.assert_called_once_with("akshare")


if __name__ == "__main__":
    unittest.main()
