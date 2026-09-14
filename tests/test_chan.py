import unittest

from app.chan.bi import build_strokes
from app.chan.divergence import DivergenceResult, detect_macd_divergence
from app.chan.fractal import detect_fenxing
from app.chan.inclusion import normalize_k_lines
from app.chan.models import FenXing, KLine
from app.chan.pipeline import analyze_chan
from app.chan.segment import Segment, build_segments
from app.chan.signal import ChanSignal, ChanSignalEngine
from app.chan.zhongshu import find_zhongshu


class ChanTests(unittest.TestCase):
    def test_chan_pipeline_connects_k_lines_to_signal(self):
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

        analysis = analyze_chan(
            lines,
            buy_setup={"second_buy": True},
            min_span=1,
        )

        self.assertEqual(len(analysis["normalized_lines"]), len(lines))
        self.assertGreaterEqual(len(analysis["fractals"]), 5)
        self.assertGreaterEqual(len(analysis["strokes"]), 4)
        self.assertIn(analysis["signal"], ChanSignal)

    def test_inclusion_is_normalized_before_fractal_detection(self):
        lines = [
            KLine("1", 9, 10, 8, 9.5),
            KLine("2", 9.5, 11, 7.5, 10.5),
            KLine("3", 10.5, 12, 8.5, 9.0),
        ]

        normalized = normalize_k_lines(lines)

        self.assertEqual(len(normalized), 2)
        self.assertEqual(normalized[0].high, 11)
        self.assertEqual(normalized[0].low, 8)

    def test_strokes_require_alternating_fractals_and_minimum_span(self):
        fractals = [
            FenXing(0, 10, "bottom"),
            FenXing(4, 14, "top"),
            FenXing(8, 9, "bottom"),
            FenXing(12, 16, "top"),
        ]

        strokes = build_strokes(fractals, min_span=3)

        self.assertEqual([stroke.direction for stroke in strokes], ["up", "down", "up"])
        self.assertEqual(strokes[0].length, 4)


    def test_strokes_remain_alternating_when_short_fractal_gap_is_skipped(self):
        fractals = [
            FenXing(0, 10, "bottom"),
            FenXing(4, 14, "top"),
            FenXing(6, 12, "bottom"),
            FenXing(10, 16, "top"),
            FenXing(14, 9, "bottom"),
        ]

        strokes = build_strokes(fractals, min_span=4)

        self.assertEqual([stroke.direction for stroke in strokes], ["up", "down"])
        self.assertEqual([(stroke.start_index, stroke.end_index) for stroke in strokes], [(0, 10), (10, 14)])
        self.assertEqual(build_segments(strokes), [])
    def test_segments_are_built_from_minimum_three_strokes(self):
        strokes = [
            *build_strokes(
                [
                    FenXing(0, 10, "bottom"),
                    FenXing(4, 14, "top"),
                    FenXing(8, 9, "bottom"),
                    FenXing(12, 16, "top"),
                    FenXing(16, 11, "bottom"),
                    FenXing(20, 18, "top"),
                ],
                min_span=3,
            )
        ]

        segments = build_segments(strokes)

        self.assertGreaterEqual(len(segments), 1)
        self.assertGreaterEqual(len(segments[0].strokes), 3)

    def test_zhongshu_is_the_intersection_of_three_segment_ranges(self):
        segments = [
            Segment([], "up", high=10, low=4),
            Segment([], "down", high=9, low=5),
            Segment([], "up", high=8, low=6),
        ]

        result = find_zhongshu(segments)

        self.assertEqual(len(result), 1)
        self.assertEqual((result[0].low, result[0].high), (6, 8))

    def test_divergence_detects_lower_price_low_with_stronger_macd(self):
        result = detect_macd_divergence(
            [10, 9, 8, 9, 7, 8],
            [-1, -2, -3, -2, -1.5, -1],
        )

        self.assertTrue(result.detected)
        self.assertEqual(result.kind, "bullish")

    def test_signal_engine_applies_risk_before_buy_signal(self):
        engine = ChanSignalEngine()

        self.assertEqual(
            engine.evaluate(
                {
                    "second_buy": True,
                    "zhongshu": True,
                    "trend_confirm": True,
                    "multi_cycle_confirm": True,
                }
            ),
            ChanSignal.SECOND_BUY,
        )
        self.assertEqual(
            engine.evaluate({"second_buy": True, "risk_blocked": True}),
            ChanSignal.SELL_RISK,
        )

    def test_signal_engine_does_not_treat_undetected_divergence_as_true(self):
        signal = ChanSignalEngine().evaluate(
            {
                "first_buy": True,
                "divergence": DivergenceResult(False, "no divergence", 0),
                "trend_confirm": True,
                "multi_cycle_confirm": True,
            }
        )

        self.assertEqual(signal, ChanSignal.WAIT)

    def test_class_second_buy_requires_trend_and_multi_cycle_confirmation(self):
        engine = ChanSignalEngine()
        base = {"class_second": True, "divergence": True}
        self.assertEqual(engine.evaluate(base), ChanSignal.WAIT)
        self.assertEqual(engine.evaluate({**base, "trend_confirm": True}), ChanSignal.WAIT)
        self.assertEqual(
            engine.evaluate({**base, "trend_confirm": True, "multi_cycle_confirm": True}),
            ChanSignal.CLASS_SECOND_BUY,
        )

if __name__ == "__main__":
    unittest.main()
