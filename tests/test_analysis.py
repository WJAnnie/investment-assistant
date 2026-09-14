import unittest

from app.analysis.boll import calculate_boll
from app.analysis.indicator import ema, sma
from app.analysis.macd import calculate_macd
from app.analysis.rsi import calculate_rsi
from app.analysis.technical_score import calculate_technical_score
from app.analysis.trend import trend_score


class AnalysisTests(unittest.TestCase):
    def test_indicators_return_aligned_values(self):
        values = [1, 2, 3, 4]

        self.assertEqual(len(ema(values, 3)), len(values))
        self.assertEqual(len(sma(values, 3)), len(values))
        self.assertEqual(len(calculate_boll(values, 3)["middle"]), len(values))

    def test_invalid_indicator_period_is_rejected(self):
        with self.assertRaises(ValueError):
            sma([1, 2], 0)

        with self.assertRaises(ValueError):
            calculate_rsi([1, 2], 0)

    def test_technical_score_uses_latest_macd_value(self):
        close = [100 + i * 0.5 for i in range(30)]
        result = calculate_technical_score(
            macd=calculate_macd(close),
            rsi={"value": 55},
            kdj={"k": 20, "j": 30},
            trend=trend_score(close),
        )

        self.assertGreater(result["score"], 50)
        self.assertIn("MACD修复", result["signals"])

    def test_macd_uses_r03_parameter_set(self):
        # R03: MACD 参数固定为 6/13/4。直接断言常量并用固定输入
        # 锁定计算结果，防止任何静默回退到旧的 12/26/9。
        from app.analysis.macd import FAST_PERIOD, SIGNAL_PERIOD, SLOW_PERIOD

        self.assertEqual((FAST_PERIOD, SLOW_PERIOD, SIGNAL_PERIOD), (6, 13, 4))

        close = [100 + i * 0.5 for i in range(30)]
        macd = calculate_macd(close)

        self.assertEqual(len(macd["dif"]), len(close))
        self.assertEqual(len(macd["dea"]), len(close))
        self.assertEqual(len(macd["hist"]), len(close))
        # 用固定输入的精确值锁定 6/13/4 的输出（12/26/9 会给出不同值）。
        self.assertAlmostEqual(macd["dif"][-1], 1.715743, places=6)
        self.assertAlmostEqual(macd["dea"][-1], 1.704408, places=6)
        self.assertAlmostEqual(macd["hist"][-1], 0.011335, places=6)


if __name__ == "__main__":
    unittest.main()
