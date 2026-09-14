import unittest

from app.chan.models import KLine
from app.market.models import Quote
from app.workflow.batch import run_daily_reports


class _BatchCollector:
    def __init__(self):
        self.lines = [
            KLine("0", 8, 10, 5, 8),
            KLine("1", 11, 12, 7, 11),
            KLine("2", 5, 9, 4, 5),
            KLine("3", 10, 11, 6, 10),
            KLine("4", 4, 8, 3, 4),
            KLine("5", 12, 13, 7, 12),
            KLine("6", 6, 10, 5, 6),
            KLine("7", 13, 14, 8, 13),
            KLine("8", 5, 9, 4, 5),
        ]

    def get_quote(self, code):
        return Quote(code, code, 10.0, 1.0, "2026-09-11")

    def get_klines(self, code, **kwargs):
        if code == "BAD":
            raise RuntimeError("history unavailable")
        return self.lines


class BatchWorkflowTests(unittest.TestCase):
    def test_batch_continues_after_one_symbol_fails_and_reports_partial_status(self):
        result = run_daily_reports(
            ["GOOD", "BAD"],
            collector=_BatchCollector(),
            chan_min_span=1,
            chan_context={
                "second_buy": True,
                "zhongshu": True,
                "trend_confirm": True,
                "multi_cycle_confirm": True,
            },
        )

        self.assertEqual(result["status"], "partial")
        self.assertEqual((result["succeeded"], result["failed"]), (1, 1))
        self.assertEqual(result["results"][0]["status"], "completed")
        self.assertEqual(result["results"][1]["status"], "failed")
        self.assertIn("history unavailable", result["results"][1]["error"])

    def test_batch_rejects_empty_code_list(self):
        with self.assertRaises(ValueError):
            run_daily_reports([], collector=_BatchCollector())


if __name__ == "__main__":
    unittest.main()
