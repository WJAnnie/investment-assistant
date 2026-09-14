import unittest

from app.chan.signal import ChanSignal
from app.chan.models import KLine
from app.decision.engine import build_decision
from app.market.models import Quote
from app.workflow.daily import daily_report_job


ALL_GATES = {
    "market": True,
    "asset": True,
    "fundamental": True,
    "valuation": True,
    "structure": True,
    "risk": True,
    "execution": True,
}


class _Collector:
    def get_quote(self, code):
        return Quote(code, "示例标的", 115.0, 1.2, "2026-09-11T14:30:00+08:00")


class _HistoryCollector(_Collector):
    def __init__(self, lines):
        self.lines = lines
        self.requests = []

    def get_klines(self, code, **kwargs):
        self.requests.append((code, kwargs))
        return self.lines


class _Notifier:
    def __init__(self):
        self.messages = []

    def send(self, content):
        self.messages.append(content)
        return True


class WorkflowTests(unittest.TestCase):
    def test_daily_job_can_compute_chan_structure_from_k_lines(self):
        prices = [100 + index * 0.5 for index in range(30)]
        lines = [
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

        result = daily_report_job(
            "AAA",
            prices=prices,
            k_lines=lines,
            chan_min_span=1,
            chan_context={
                "second_buy": True,
                "zhongshu": True,
                "trend_confirm": True,
                "multi_cycle_confirm": True,
            },
            collector=_Collector(),
        )

        self.assertIn("chan", result)
        self.assertGreaterEqual(len(result["chan"]["strokes"]), 4)
        self.assertEqual(result["signal"], ChanSignal.SECOND_BUY)

    def test_daily_job_can_fetch_history_before_running_analysis(self):
        lines = [
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
        collector = _HistoryCollector(lines)

        result = daily_report_job(
            "000001",
            collector=collector,
            fetch_history=True,
            history_start="20260901",
            history_end="20260911",
            chan_min_span=1,
            chan_context={
                "second_buy": True,
                "zhongshu": True,
                "trend_confirm": True,
                "multi_cycle_confirm": True,
            },
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["quote"].code, "000001")
        self.assertIn("chan", result)
        self.assertEqual(collector.requests, [
            (
                "000001",
                {
                    "start_date": "20260901",
                    "end_date": "20260911",
                    "period": "daily",
                    "adjust": "",
                },
            )
        ])

    def test_buy_requires_risk_context_and_is_never_automatic(self):
        decision = build_decision(
            ChanSignal.SECOND_BUY,
            technical_score=85,
            max_position=0.30,
            gates=ALL_GATES,
        )

        self.assertEqual(decision.action, "WAIT")
        self.assertEqual(decision.position_percent, 0.0)
        self.assertFalse(decision.auto_execute)
        self.assertTrue(any("未提供风险上下文" in r for r in decision.reasons))

    def test_daily_job_runs_data_to_analysis_to_report_to_notification(self):
        prices = [100 + index * 0.5 for index in range(30)]
        notifier = _Notifier()

        result = daily_report_job(
            "AAA",
            prices=prices,
            highs=[price + 1 for price in prices],
            lows=[price - 1 for price in prices],
            chan_context={
                "second_buy": True,
                "zhongshu": True,
                "trend_confirm": True,
                "multi_cycle_confirm": True,
            },
            collector=_Collector(),
            notifier=notifier,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["quote"].code, "AAA")
        self.assertIn("technical", result)
        self.assertEqual(result["signal"], ChanSignal.SECOND_BUY)
        self.assertEqual(result["decision"].action, "WAIT")  # 无风险上下文 → 研究参考
        self.assertTrue(result["notified"])
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("不自动交易", notifier.messages[0])


if __name__ == "__main__":
    unittest.main()
