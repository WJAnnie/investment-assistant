"""Tests for buy flag producer, pipeline integration, and two-stage core_signal wiring."""

from datetime import datetime, timedelta
from decimal import Decimal
import unittest
from zoneinfo import ZoneInfo

from app.chan.divergence import DivergenceResult
from app.chan.models import KLine
from app.chan.multi_cycle_confirm import BUY_SIGNALS, ConfirmOutcome
from app.chan.pipeline import analyze_chan, derive_buy_flags
from app.chan.signal import ChanSignal
from app.chan.zhongshu import ZhongShu
from app.domain.bars import BarStatus
from app.domain.timeframe import Timeframe
from app.portfolio.structure import (
    CycleInput,
    build_structure_evidence,
)
from app.report.portfolio import _format_decision_evidence

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _make_sample_k_lines(breakout: bool = True) -> list[KLine]:
    prices = [
        10, 12, 10,
        15, 20, 15,
        10, 8, 10,
        16, 22, 16,
        9, 7, 9,
        15, 21, 15,
        11, 9, 11,
        17, 23, 17,
        10, 8, 10,
        18, 25, 26 if breakout else 15,
    ]
    return [
        KLine(
            time=f"2026-01-{i+1:02d}",
            open=p - 1,
            high=p + 1,
            low=p - 2,
            close=p,
            volume=1000.0,
        )
        for i, p in enumerate(prices)
    ]


def _make_all_cycles(
    cutoff: datetime,
    exclude: set[Timeframe] | None = None,
) -> dict[Timeframe, CycleInput]:
    exclude = exclude or set()
    cycles: dict[Timeframe, CycleInput] = {}
    bar_counts = {
        Timeframe.WEEKLY: 5,
        Timeframe.DAILY: 5,
        Timeframe.MIN_120: 5,
        Timeframe.MIN_30: 5,
        Timeframe.MIN_15: 10,
        Timeframe.MIN_5: 15,
    }
    for tf, count in bar_counts.items():
        if tf in exclude:
            continue
        start_time = cutoff - timedelta(hours=count)
        bars = [
            KLine(
                time=(start_time + timedelta(hours=i + 1)).isoformat(),
                open=10.0,
                high=12.0,
                low=9.0,
                close=11.0,
                volume=1000.0,
            )
            for i in range(count)
        ]
        cycles[tf] = CycleInput(bars=tuple(bars), status=BarStatus.CLOSED, source="test")
    return cycles


class BuyFlagProducerTests(unittest.TestCase):
    def setUp(self):
        self.sample_zhongshu = ZhongShu(start_index=0, end_index=10, high=100.0, low=80.0)
        self.div_true = DivergenceResult(detected=True, reason="bullish", score=80, kind="bullish")
        self.div_false = DivergenceResult(detected=False, reason="none", score=0, kind=None)

    def test_derive_buy_flags_zhongshu_breakout(self):
        chan_res = {
            "zhongshu": [self.sample_zhongshu],
            "divergence": self.div_false,
        }
        flags_above = derive_buy_flags(chan_res, close=100.5)
        self.assertTrue(flags_above["zhongshu_breakout"])
        self.assertTrue(flags_above["third_buy"])

        flags_equal = derive_buy_flags(chan_res, close=100.0)
        self.assertFalse(flags_equal["zhongshu_breakout"])
        self.assertFalse(flags_equal["third_buy"])

        flags_below = derive_buy_flags(chan_res, close=95.0)
        self.assertFalse(flags_below["zhongshu_breakout"])
        self.assertFalse(flags_below["third_buy"])

    def test_derive_buy_flags_second_buy(self):
        chan_res = {
            "zhongshu": [self.sample_zhongshu],
            "divergence": self.div_false,
        }
        self.assertTrue(derive_buy_flags(chan_res, close=90.0)["second_buy"])
        self.assertTrue(derive_buy_flags(chan_res, close=80.0)["second_buy"])
        self.assertTrue(derive_buy_flags(chan_res, close=100.0)["second_buy"])

        self.assertFalse(derive_buy_flags(chan_res, close=79.9)["second_buy"])
        self.assertFalse(derive_buy_flags(chan_res, close=100.1)["second_buy"])

    def test_derive_buy_flags_class_second(self):
        chan_div = {
            "zhongshu": [self.sample_zhongshu],
            "divergence": self.div_true,
        }
        self.assertTrue(derive_buy_flags(chan_div, close=90.5)["class_second"])
        self.assertFalse(derive_buy_flags(chan_div, close=90.0)["class_second"])
        self.assertFalse(derive_buy_flags(chan_div, close=85.0)["class_second"])

        chan_nodiv = {
            "zhongshu": [self.sample_zhongshu],
            "divergence": self.div_false,
        }
        self.assertFalse(derive_buy_flags(chan_nodiv, close=95.0)["class_second"])

    def test_derive_buy_flags_first_buy(self):
        chan_div = {
            "zhongshu": [self.sample_zhongshu],
            "divergence": self.div_true,
        }
        self.assertTrue(derive_buy_flags(chan_div, close=79.5)["first_buy"])
        self.assertFalse(derive_buy_flags(chan_div, close=80.0)["first_buy"])
        self.assertFalse(derive_buy_flags(chan_div, close=85.0)["first_buy"])

        chan_nodiv = {
            "zhongshu": [self.sample_zhongshu],
            "divergence": self.div_false,
        }
        self.assertFalse(derive_buy_flags(chan_nodiv, close=75.0)["first_buy"])

    def test_derive_buy_flags_third_buy_linkage_with_breakout(self):
        chan_res = {
            "zhongshu": [self.sample_zhongshu],
            "divergence": self.div_false,
        }
        flags_breakout = derive_buy_flags(chan_res, close=105.0)
        self.assertEqual(flags_breakout["third_buy"], flags_breakout["zhongshu_breakout"])
        self.assertTrue(flags_breakout["third_buy"])

        flags_no_breakout = derive_buy_flags(chan_res, close=95.0)
        self.assertEqual(flags_no_breakout["third_buy"], flags_no_breakout["zhongshu_breakout"])
        self.assertFalse(flags_no_breakout["third_buy"])

    def test_derive_buy_flags_invalid_close_rejected(self):
        chan_res = {
            "zhongshu": [self.sample_zhongshu],
            "divergence": self.div_false,
        }
        for invalid in (None, True, False, "100", [100], {"close": 100}):
            with self.subTest(invalid_close=invalid):
                with self.assertRaises(TypeError):
                    derive_buy_flags(chan_res, close=invalid)

        for invalid in (-10.0, -0.001, 0, 0.0, float("nan"), float("inf"), float("-inf")):
            with self.subTest(invalid_close=invalid):
                with self.assertRaises(ValueError):
                    derive_buy_flags(chan_res, close=invalid)

    def test_derive_buy_flags_missing_keys_rejected(self):
        with self.assertRaises(ValueError):
            derive_buy_flags({"divergence": self.div_false}, close=100.0)
        with self.assertRaises(ValueError):
            derive_buy_flags({"zhongshu": [self.sample_zhongshu]}, close=100.0)
        with self.assertRaises(TypeError):
            derive_buy_flags("not_a_dict", close=100.0)
        with self.assertRaises(TypeError):
            derive_buy_flags({"zhongshu": "not_list", "divergence": self.div_false}, close=100.0)

    def test_derive_buy_flags_empty_zhongshu_list(self):
        chan_res = {
            "zhongshu": [],
            "divergence": self.div_true,
        }
        flags = derive_buy_flags(chan_res, close=100.0)
        self.assertFalse(flags["zhongshu_breakout"])
        self.assertFalse(flags["second_buy"])
        self.assertFalse(flags["class_second"])
        self.assertFalse(flags["first_buy"])
        self.assertFalse(flags["third_buy"])

    def test_derive_buy_flags_divergence_false_suppresses_divergence_signals(self):
        chan_res = {
            "zhongshu": [self.sample_zhongshu],
            "divergence": self.div_false,
        }
        flags_low = derive_buy_flags(chan_res, close=50.0)
        self.assertFalse(flags_low["first_buy"])
        flags_high = derive_buy_flags(chan_res, close=95.0)
        self.assertFalse(flags_high["class_second"])

    def test_derive_buy_flags_accepts_mapping_and_decimal(self):
        zs_dict = {"high": 100, "low": 80, "center": 90}
        div_dict = {"detected": True}
        chan_res = {
            "zhongshu": [zs_dict],
            "divergence": div_dict,
        }
        flags = derive_buy_flags(chan_res, close=Decimal("95.5"))
        self.assertTrue(flags["second_buy"])
        self.assertTrue(flags["class_second"])
        self.assertFalse(flags["zhongshu_breakout"])


class AnalyzeChanIntegrationTests(unittest.TestCase):
    def test_analyze_chan_synthesized_breakout_produces_third_buy(self):
        lines = _make_sample_k_lines(breakout=True)
        res = analyze_chan(
            lines,
            buy_setup={"trend_confirm": True, "multi_cycle_confirm": True},
            min_span=1,
        )
        self.assertEqual(res["signal"], ChanSignal.THIRD_BUY)
        self.assertTrue(res["context"]["third_buy"])
        self.assertTrue(res["context"]["zhongshu_breakout"])

    def test_analyze_chan_synthesized_pullback_produces_second_buy(self):
        lines = _make_sample_k_lines(breakout=False)
        res = analyze_chan(
            lines,
            buy_setup={"trend_confirm": True, "multi_cycle_confirm": True},
            min_span=1,
        )
        self.assertEqual(res["signal"], ChanSignal.SECOND_BUY)
        self.assertTrue(res["context"]["second_buy"])
        self.assertFalse(res["context"]["zhongshu_breakout"])

    def test_analyze_chan_explicit_buy_setup_takes_precedence(self):
        lines = _make_sample_k_lines(breakout=True)
        res = analyze_chan(
            lines,
            buy_setup={
                "third_buy": False,
                "trend_confirm": True,
                "multi_cycle_confirm": True,
            },
            min_span=1,
        )
        self.assertFalse(res["context"]["third_buy"])
        self.assertEqual(res["signal"], ChanSignal.WAIT)

    def test_analyze_chan_without_confirmations_stays_wait(self):
        lines = _make_sample_k_lines(breakout=True)
        res1 = analyze_chan(lines, buy_setup={"trend_confirm": False, "multi_cycle_confirm": True}, min_span=1)
        self.assertEqual(res1["signal"], ChanSignal.WAIT)

        res2 = analyze_chan(lines, buy_setup={"trend_confirm": True, "multi_cycle_confirm": False}, min_span=1)
        self.assertEqual(res2["signal"], ChanSignal.WAIT)


class TwoStageWiringTests(unittest.TestCase):
    def test_two_stage_wiring_data_gate_failure_keeps_core_signal_none_and_wait(self):
        cutoff = datetime(2026, 9, 18, 15, 0, tzinfo=SHANGHAI)
        cycles = _make_all_cycles(cutoff, exclude={Timeframe.MIN_120})

        probe = build_structure_evidence(
            code="000001",
            market="SZ",
            cycles=cycles,
            cutoff=cutoff,
            core_signal=None,
            trend_confirm=True,
        )
        data_gates_passed = probe.blocked_by == ("core_signal",)
        self.assertFalse(data_gates_passed)
        self.assertIn("120m", probe.blocked_by)

        lines = _make_sample_k_lines(breakout=True)
        chan = analyze_chan(
            lines,
            buy_setup={"trend_confirm": True, "multi_cycle_confirm": data_gates_passed},
            min_span=1,
        )
        self.assertEqual(chan["signal"], ChanSignal.WAIT)

        core_signal_candidate = (
            chan["signal"] if data_gates_passed and chan["signal"] in BUY_SIGNALS else None
        )
        self.assertIsNone(core_signal_candidate)

        final_struct = build_structure_evidence(
            code="000001",
            market="SZ",
            cycles=cycles,
            cutoff=cutoff,
            core_signal=core_signal_candidate,
            trend_confirm=True,
        )
        self.assertEqual(final_struct.outcome, ConfirmOutcome.WAIT)
        self.assertFalse(final_struct.ready)

    def test_two_stage_wiring_full_cycles_and_real_structure_confirms(self):
        cutoff = datetime(2026, 9, 18, 15, 0, tzinfo=SHANGHAI)
        cycles = _make_all_cycles(cutoff)

        probe = build_structure_evidence(
            code="000001",
            market="SZ",
            cycles=cycles,
            cutoff=cutoff,
            core_signal=None,
            trend_confirm=True,
        )
        data_gates_passed = probe.blocked_by == ("core_signal",)
        self.assertTrue(data_gates_passed)

        lines = _make_sample_k_lines(breakout=True)
        chan = analyze_chan(
            lines,
            buy_setup={"trend_confirm": True, "multi_cycle_confirm": data_gates_passed},
            min_span=1,
        )
        self.assertEqual(chan["signal"], ChanSignal.THIRD_BUY)
        self.assertIn(chan["signal"], BUY_SIGNALS)

        core_signal_candidate = (
            chan["signal"] if data_gates_passed and chan["signal"] in BUY_SIGNALS else None
        )
        self.assertEqual(core_signal_candidate, ChanSignal.THIRD_BUY)

        final_struct = build_structure_evidence(
            code="000001",
            market="SZ",
            cycles=cycles,
            cutoff=cutoff,
            core_signal=core_signal_candidate,
            trend_confirm=True,
        )
        self.assertEqual(final_struct.outcome, ConfirmOutcome.CONFIRMED)
        self.assertTrue(final_struct.ready)
        self.assertEqual(final_struct.confirm.core_signal, ChanSignal.THIRD_BUY)

        payload = {
            "outcome": final_struct.outcome.value,
            "ready": final_struct.ready,
            "limit": final_struct.limit,
            "reason_code": final_struct.reason_code,
            "blocked_by": tuple(final_struct.blocked_by),
            "per_cycle_status": dict(final_struct.per_cycle_status),
            "stale_cycles": tuple(final_struct.stale_cycles),
            "insufficient_cycles": tuple(final_struct.insufficient_cycles),
            "missing_cycles": (),
            "core_signal": final_struct.confirm.core_signal.value,
            "confirm_core_signal": final_struct.confirm.core_signal.value,
        }
        report_text = _format_decision_evidence({"structure": payload})
        self.assertIn("结构已确认（信号 THIRD_BUY）；动作仍为研究参考，须本人复核后执行，不自动交易。", report_text)

    def test_two_stage_wiring_structure_payload_contains_core_signal(self):
        cutoff = datetime(2026, 9, 18, 15, 0, tzinfo=SHANGHAI)
        cycles = _make_all_cycles(cutoff)
        struct = build_structure_evidence(
            code="000001",
            market="SZ",
            cycles=cycles,
            cutoff=cutoff,
            core_signal=ChanSignal.SECOND_BUY,
            trend_confirm=True,
        )
        payload = {
            "outcome": struct.outcome.value,
            "core_signal": struct.confirm.core_signal.value,
            "confirm_core_signal": struct.confirm.core_signal.value,
        }
        self.assertEqual(payload["core_signal"], "SECOND_BUY")
        self.assertEqual(payload["confirm_core_signal"], "SECOND_BUY")


if __name__ == "__main__":
    unittest.main()
