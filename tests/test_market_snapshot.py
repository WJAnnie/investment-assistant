import unittest
from datetime import datetime, timezone

from app.market.models import Quote
from app.report.market_snapshot import run_market_snapshot


class _Collector:
    def __init__(self):
        self.quotes = {
            "SH.000001": Quote("SH.000001", "上证指数", 3864.5, -1.77, "now"),
            "SZ.399001": Quote("SZ.399001", "深证成指", 13310.6, -2.25, "now"),
        }

    def get_quote(self, code):
        return self.quotes[code]


class _Notifier:
    def __init__(self):
        self.messages = []

    def send(self, content):
        self.messages.append(content)
        return True


class MarketSnapshotTests(unittest.TestCase):
    def test_snapshot_builds_report_and_sends_it(self):
        notifier = _Notifier()

        result = run_market_snapshot(
            ["SH.000001", "SZ.399001"],
            collector=_Collector(),
            notifier=notifier,
            now=datetime(2026, 9, 11, 3, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["succeeded"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertIn("上证指数：3864.5000（-1.77%）", result["report"])
        self.assertEqual(notifier.messages, [result["report"]])

    def test_snapshot_keeps_other_quotes_when_one_code_fails(self):
        class PartialCollector(_Collector):
            def get_quote(self, code):
                if code == "SZ.399001":
                    raise RuntimeError("temporarily unavailable")
                return super().get_quote(code)

        result = run_market_snapshot(
            ["SH.000001", "SZ.399001"],
            collector=PartialCollector(),
        )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertIn("temporarily unavailable", result["errors"][0])

    def test_snapshot_supports_a_report_title_for_scheduled_runs(self):
        result = run_market_snapshot(
            ["SH.000001"],
            collector=_Collector(),
            title="午盘分析",
        )

        self.assertTrue(result["report"].startswith("📊 午盘分析"))

    def test_snapshot_has_different_content_for_different_report_kinds(self):
        morning = run_market_snapshot(
            ["SH.000001", "SZ.399001"],
            collector=_Collector(),
            title="投资晨报",
            report_kind="morning",
        )
        midday = run_market_snapshot(
            ["SH.000001", "SZ.399001"],
            collector=_Collector(),
            title="午盘分析",
            report_kind="midday",
        )

        self.assertIn("早盘观察", morning["report"])
        self.assertIn("午盘强弱", midday["report"])
        self.assertNotEqual(morning["report"], midday["report"])

    def test_trading_and_closing_reports_have_different_guidance(self):
        trading = run_market_snapshot(
            ["SH.000001", "SZ.399001"],
            collector=_Collector(),
            title="交易助手",
            report_kind="trading",
        )
        closing = run_market_snapshot(
            ["SH.000001", "SZ.399001"],
            collector=_Collector(),
            title="收盘复盘",
            report_kind="closing",
        )

        self.assertIn("午后执行", trading["report"])
        self.assertIn("收盘复盘", closing["report"])
        self.assertNotEqual(trading["report"], closing["report"])


if __name__ == "__main__":
    unittest.main()
