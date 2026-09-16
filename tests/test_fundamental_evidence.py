import inspect
import math
import unittest
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.analysis.fundamental import (
    FundamentalPolicy,
    StockFundamentalObservation,
    StockValuationObservation,
    ValuationPolicy,
    evaluate_fundamental,
    evaluate_valuation,
)
from app.domain.evidence import (
    AnalysisEvidenceBundle,
    EvidenceStatus,
    Freshness,
    GateEvidence,
)


def _aware_dt(year: int = 2026, month: int = 9, day: int = 16, hour: int = 9, minute: int = 30) -> datetime:
    return datetime(year, month, day, hour, minute, 0, tzinfo=timezone.utc)


class StockFundamentalObservationTests(unittest.TestCase):
    def test_valid_observation_construction(self):
        t_pub = _aware_dt(2026, 8, 25, 18, 0)
        t_fetch = _aware_dt(2026, 8, 25, 18, 5)
        obs = StockFundamentalObservation(
            symbol="600519",
            report_period=date(2026, 6, 30),
            published_at=t_pub,
            fetched_at=t_fetch,
            source="akshare",
            revenue_growth_pct=15.5,
            net_profit_growth_pct=18.2,
            roe_pct=21.0,
            eps=1.85,
        )
        self.assertEqual(obs.symbol, "600519")
        self.assertEqual(obs.report_period, date(2026, 6, 30))
        self.assertEqual(obs.published_at, t_pub)
        self.assertEqual(obs.fetched_at, t_fetch)
        self.assertEqual(obs.source, "akshare")
        self.assertEqual(obs.revenue_growth_pct, 15.5)

    def test_rejects_non_six_digit_a_share_symbol(self):
        t_pub = _aware_dt(2026, 8, 25)
        t_fetch = _aware_dt(2026, 8, 25)
        for invalid_symbol in ("00001", "6005199", "60051A", "ABCDEF", "", 600519, None):
            with self.subTest(symbol=invalid_symbol):
                with self.assertRaises((ValueError, TypeError)):
                    StockFundamentalObservation(
                        symbol=invalid_symbol,  # type: ignore[arg-type]
                        report_period=date(2026, 6, 30),
                        published_at=t_pub,
                        fetched_at=t_fetch,
                        source="akshare",
                        revenue_growth_pct=10.0,
                        net_profit_growth_pct=10.0,
                        roe_pct=10.0,
                        eps=1.0,
                    )

    def test_rejects_naive_or_non_datetime(self):
        naive_dt = datetime(2026, 8, 25, 18, 0)
        valid_dt = _aware_dt(2026, 8, 25, 18, 0)
        with self.assertRaises(ValueError):
            StockFundamentalObservation(
                symbol="600519",
                report_period=date(2026, 6, 30),
                published_at=naive_dt,
                fetched_at=valid_dt,
                source="akshare",
                revenue_growth_pct=10.0,
                net_profit_growth_pct=10.0,
                roe_pct=10.0,
                eps=1.0,
            )
        with self.assertRaises(ValueError):
            StockFundamentalObservation(
                symbol="600519",
                report_period=date(2026, 6, 30),
                published_at=valid_dt,
                fetched_at=naive_dt,
                source="akshare",
                revenue_growth_pct=10.0,
                net_profit_growth_pct=10.0,
                roe_pct=10.0,
                eps=1.0,
            )

    def test_rejects_datetime_as_report_period(self):
        t_pub = _aware_dt(2026, 8, 25, 18, 0)
        t_fetch = _aware_dt(2026, 8, 25, 18, 5)
        # Rejects both timezone-aware datetime and naive datetime, and other non-date types
        for invalid_period in (
            _aware_dt(2026, 6, 30, 0, 0),
            datetime(2026, 6, 30, 0, 0),
            "2026-06-30",
            20260630,
            None,
        ):
            with self.subTest(period=invalid_period):
                with self.assertRaises(TypeError):
                    StockFundamentalObservation(
                        symbol="600519",
                        report_period=invalid_period,  # type: ignore[arg-type]
                        published_at=t_pub,
                        fetched_at=t_fetch,
                        source="akshare",
                        revenue_growth_pct=10.0,
                        net_profit_growth_pct=10.0,
                        roe_pct=10.0,
                        eps=1.0,
                    )

    def test_rejects_date_order_violation(self):
        t1 = _aware_dt(2026, 8, 25, 10, 0)
        t2 = _aware_dt(2026, 8, 25, 9, 0)
        # published_at > fetched_at
        with self.assertRaises(ValueError):
            StockFundamentalObservation(
                symbol="600519",
                report_period=date(2026, 6, 30),
                published_at=t1,
                fetched_at=t2,
                source="akshare",
                revenue_growth_pct=10.0,
                net_profit_growth_pct=10.0,
                roe_pct=10.0,
                eps=1.0,
            )
        # report_period after published_at date
        with self.assertRaises(ValueError):
            StockFundamentalObservation(
                symbol="600519",
                report_period=date(2026, 9, 30),
                published_at=_aware_dt(2026, 8, 25),
                fetched_at=_aware_dt(2026, 8, 25),
                source="akshare",
                revenue_growth_pct=10.0,
                net_profit_growth_pct=10.0,
                roe_pct=10.0,
                eps=1.0,
            )

    def test_rejects_boolean_and_non_finite_metrics(self):
        t = _aware_dt(2026, 8, 25)
        for invalid_metric in (True, False, float("nan"), float("inf"), float("-inf"), "10.0", None):
            with self.subTest(metric=invalid_metric):
                with self.assertRaises((TypeError, ValueError)):
                    StockFundamentalObservation(
                        symbol="600519",
                        report_period=date(2026, 6, 30),
                        published_at=t,
                        fetched_at=t,
                        source="akshare",
                        revenue_growth_pct=invalid_metric,  # type: ignore[arg-type]
                        net_profit_growth_pct=10.0,
                        roe_pct=10.0,
                        eps=1.0,
                    )

    def test_accepts_negative_metrics_and_decimals(self):
        t = _aware_dt(2026, 8, 25)
        obs = StockFundamentalObservation(
            symbol="600519",
            report_period=date(2026, 6, 30),
            published_at=t,
            fetched_at=t,
            source="akshare",
            revenue_growth_pct=Decimal("-5.5"),
            net_profit_growth_pct=-12.3,
            roe_pct=-2.5,
            eps=Decimal("-0.15"),
        )
        self.assertEqual(obs.revenue_growth_pct, Decimal("-5.5"))
        self.assertEqual(obs.net_profit_growth_pct, -12.3)

    def test_is_frozen_immutable(self):
        t = _aware_dt(2026, 8, 25)
        obs = StockFundamentalObservation(
            symbol="600519",
            report_period=date(2026, 6, 30),
            published_at=t,
            fetched_at=t,
            source="akshare",
            revenue_growth_pct=10.0,
            net_profit_growth_pct=10.0,
            roe_pct=10.0,
            eps=1.0,
        )
        with self.assertRaises(FrozenInstanceError):
            obs.symbol = "000001"  # type: ignore[misc]

    def test_source_error_does_not_leak_raw_secret_value(self):
        t = _aware_dt(2026, 8, 25)
        secret_source = "SECRET_TOKEN_API_KEY_9999" + "X" * 200
        try:
            StockFundamentalObservation(
                symbol="600519",
                report_period=date(2026, 6, 30),
                published_at=t,
                fetched_at=t,
                source=secret_source,
                revenue_growth_pct=10.0,
                net_profit_growth_pct=10.0,
                roe_pct=10.0,
                eps=1.0,
            )
            self.fail("Should have raised ValueError")
        except ValueError as exc:
            self.assertNotIn("SECRET_TOKEN_API_KEY_9999", str(exc))


class StockValuationObservationTests(unittest.TestCase):
    def test_valid_observation_construction(self):
        t_as_of = _aware_dt(2026, 9, 16, 9, 30)
        t_fetch = _aware_dt(2026, 9, 16, 9, 31)
        obs = StockValuationObservation(
            symbol="000001",
            as_of=t_as_of,
            fetched_at=t_fetch,
            source="akshare",
            pe_ttm=12.5,
            pb=1.1,
            ps=2.4,
        )
        self.assertEqual(obs.symbol, "000001")
        self.assertEqual(obs.as_of, t_as_of)
        self.assertEqual(obs.fetched_at, t_fetch)
        self.assertEqual(obs.pe_ttm, 12.5)

    def test_accepts_negative_pe_as_valid_observation(self):
        t = _aware_dt(2026, 9, 16)
        obs = StockValuationObservation(
            symbol="000001",
            as_of=t,
            fetched_at=t,
            source="akshare",
            pe_ttm=-8.5,
            pb=-0.5,
            ps=1.2,
        )
        self.assertEqual(obs.pe_ttm, -8.5)

    def test_rejects_non_six_digit_symbol(self):
        t = _aware_dt(2026, 9, 16)
        for invalid_symbol in ("1", "00001", "6005199", "SZ0001", None, True):
            with self.subTest(symbol=invalid_symbol):
                with self.assertRaises((ValueError, TypeError)):
                    StockValuationObservation(
                        symbol=invalid_symbol,  # type: ignore[arg-type]
                        as_of=t,
                        fetched_at=t,
                        source="akshare",
                        pe_ttm=10.0,
                        pb=1.0,
                        ps=1.0,
                    )

    def test_rejects_as_of_later_than_fetched_at(self):
        t1 = _aware_dt(2026, 9, 16, 10, 0)
        t2 = _aware_dt(2026, 9, 16, 9, 0)
        with self.assertRaises(ValueError):
            StockValuationObservation(
                symbol="000001",
                as_of=t1,
                fetched_at=t2,
                source="akshare",
                pe_ttm=10.0,
                pb=1.0,
                ps=1.0,
            )

    def test_as_of_error_does_not_leak_raw_content(self):
        t1 = _aware_dt(2026, 9, 16, 10, 0)
        t2 = _aware_dt(2026, 9, 16, 9, 0)
        try:
            StockValuationObservation(
                symbol="000001",
                as_of=t1,
                fetched_at=t2,
                source="akshare",
                pe_ttm=10.0,
                pb=1.0,
                ps=1.0,
            )
            self.fail("Should have raised ValueError")
        except ValueError as exc:
            self.assertNotIn(t1.isoformat(), str(exc))

    def test_rejects_booleans_and_non_finite_metrics(self):
        t = _aware_dt(2026, 9, 16)
        for invalid in (True, False, float("nan"), float("inf"), "10"):
            with self.subTest(val=invalid):
                with self.assertRaises((TypeError, ValueError)):
                    StockValuationObservation(
                        symbol="000001",
                        as_of=t,
                        fetched_at=t,
                        source="akshare",
                        pe_ttm=invalid,  # type: ignore[arg-type]
                        pb=1.0,
                        ps=1.0,
                    )

    def test_is_frozen_immutable(self):
        t = _aware_dt(2026, 9, 16)
        obs = StockValuationObservation(
            symbol="000001",
            as_of=t,
            fetched_at=t,
            source="akshare",
            pe_ttm=10.0,
            pb=1.0,
            ps=1.0,
        )
        with self.assertRaises(FrozenInstanceError):
            obs.pe_ttm = 20.0  # type: ignore[misc]


class PolicyValidationTests(unittest.TestCase):
    def test_fundamental_policy_valid(self):
        policy = FundamentalPolicy(
            min_roe=10.0,
            min_eps=0.2,
            min_revenue_growth=5.0,
            min_profit_growth=5.0,
            max_age_days=180,
        )
        self.assertEqual(policy.min_roe, 10.0)
        self.assertEqual(policy.max_age_days, 180)

    def test_fundamental_policy_rejects_booleans_or_invalid_max_age(self):
        for invalid_age in (0, -1, True, False, 180.0, 1.5, float("nan"), "180", None):
            with self.subTest(age=invalid_age):
                with self.assertRaises((ValueError, TypeError)):
                    FundamentalPolicy(
                        min_roe=10.0,
                        min_eps=0.2,
                        min_revenue_growth=5.0,
                        min_profit_growth=5.0,
                        max_age_days=invalid_age,  # type: ignore[arg-type]
                    )

    def test_fundamental_policy_rejects_boolean_thresholds(self):
        for invalid_threshold in (True, False, float("nan"), "5.0"):
            with self.subTest(val=invalid_threshold):
                with self.assertRaises((TypeError, ValueError)):
                    FundamentalPolicy(
                        min_roe=invalid_threshold,  # type: ignore[arg-type]
                        min_eps=0.2,
                        min_revenue_growth=5.0,
                        min_profit_growth=5.0,
                        max_age_days=180,
                    )

    def test_valuation_policy_valid(self):
        policy = ValuationPolicy(
            max_pe=30.0,
            max_pb=3.0,
            max_ps=5.0,
            max_age_days=30,
        )
        self.assertEqual(policy.max_pe, 30.0)

    def test_valuation_policy_rejects_non_positive_multiples_or_booleans(self):
        for invalid_pe in (0, -5.0, True, False, float("nan")):
            with self.subTest(pe=invalid_pe):
                with self.assertRaises((ValueError, TypeError)):
                    ValuationPolicy(
                        max_pe=invalid_pe,  # type: ignore[arg-type]
                        max_pb=3.0,
                        max_ps=5.0,
                        max_age_days=30,
                    )

    def test_valuation_policy_rejects_booleans_or_invalid_max_age(self):
        for invalid_age in (0, -1, True, False, 30.0, 1.5, float("nan"), "30", None):
            with self.subTest(age=invalid_age):
                with self.assertRaises((ValueError, TypeError)):
                    ValuationPolicy(
                        max_pe=30.0,
                        max_pb=3.0,
                        max_ps=5.0,
                        max_age_days=invalid_age,  # type: ignore[arg-type]
                    )

    def test_valuation_policy_supports_large_decimal_multiples(self):
        policy = ValuationPolicy(
            max_pe=Decimal("1e400"),
            max_pb=Decimal("1e400"),
            max_ps=Decimal("1e400"),
            max_age_days=30,
        )
        self.assertEqual(policy.max_pe, Decimal("1e400"))

    def test_policies_are_frozen(self):
        p1 = FundamentalPolicy(10.0, 0.2, 5.0, 5.0, 180)
        p2 = ValuationPolicy(30.0, 3.0, 5.0, 30)
        with self.assertRaises(FrozenInstanceError):
            p1.min_roe = 20.0  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            p2.max_pe = 40.0  # type: ignore[misc]


class EvaluateFundamentalTests(unittest.TestCase):
    def setUp(self):
        self.policy = FundamentalPolicy(
            min_roe=10.0,
            min_eps=0.2,
            min_revenue_growth=5.0,
            min_profit_growth=5.0,
            max_age_days=90,
        )
        self.cutoff = _aware_dt(2026, 9, 16, 9, 30)

    def test_fresh_passing_observation_returns_ready_gate_passed(self):
        obs = StockFundamentalObservation(
            symbol="600519",
            report_period=date(2026, 6, 30),
            published_at=self.cutoff - timedelta(days=20),
            fetched_at=self.cutoff - timedelta(days=20),
            source="akshare",
            revenue_growth_pct=15.0,
            net_profit_growth_pct=18.0,
            roe_pct=12.0,
            eps=1.5,
        )
        gate = evaluate_fundamental(obs, self.cutoff, self.policy)
        self.assertEqual(gate.name, "fundamental")
        self.assertEqual(gate.stamp.status, EvidenceStatus.READY)
        self.assertEqual(gate.stamp.freshness, Freshness.RECENT)
        self.assertEqual(gate.stamp.market_date, date(2026, 6, 30))
        self.assertIsNone(gate.stamp.reason_code)
        self.assertTrue(gate.complete)
        self.assertTrue(gate.criterion_passed)
        self.assertIsNone(gate.criterion_code)
        self.assertTrue(gate.passed)

    def test_fetched_at_later_than_cutoff_allowed(self):
        # 真实场景：14:30 截止，14:34 采集
        t_pub = self.cutoff - timedelta(hours=1)
        t_fetch = self.cutoff + timedelta(minutes=4)
        obs = StockFundamentalObservation(
            symbol="600519",
            report_period=date(2026, 6, 30),
            published_at=t_pub,
            fetched_at=t_fetch,
            source="akshare",
            revenue_growth_pct=15.0,
            net_profit_growth_pct=18.0,
            roe_pct=12.0,
            eps=1.5,
        )
        gate = evaluate_fundamental(obs, self.cutoff, self.policy)
        self.assertEqual(gate.stamp.fetched_at, t_fetch)
        self.assertEqual(gate.stamp.as_of, t_pub)
        self.assertEqual(gate.stamp.cutoff, self.cutoff)
        self.assertEqual(gate.stamp.market_date, date(2026, 6, 30))
        self.assertTrue(gate.passed)

    def test_market_date_strictly_equals_report_period_not_cutoff_date(self):
        obs = StockFundamentalObservation(
            symbol="600519",
            report_period=date(2026, 3, 31),
            published_at=self.cutoff - timedelta(days=20),
            fetched_at=self.cutoff - timedelta(days=20),
            source="akshare",
            revenue_growth_pct=15.0,
            net_profit_growth_pct=18.0,
            roe_pct=12.0,
            eps=1.5,
        )
        gate = evaluate_fundamental(obs, self.cutoff, self.policy)
        self.assertEqual(gate.stamp.market_date, date(2026, 3, 31))
        self.assertNotEqual(gate.stamp.market_date, self.cutoff.date())

    def test_signature_explicit_and_no_fuzzy_dispatch(self):
        params = list(inspect.signature(evaluate_fundamental).parameters.keys())
        self.assertEqual(params, ["observation", "cutoff", "policy"])
        obs = StockFundamentalObservation(
            symbol="600519",
            report_period=date(2026, 6, 30),
            published_at=self.cutoff - timedelta(days=20),
            fetched_at=self.cutoff - timedelta(days=20),
            source="akshare",
            revenue_growth_pct=15.0,
            net_profit_growth_pct=18.0,
            roe_pct=12.0,
            eps=1.5,
        )
        # Calling with 2 positional args must raise TypeError
        with self.assertRaises(TypeError):
            evaluate_fundamental(obs, self.cutoff)  # type: ignore[call-arg]
        # Calling with 1 positional arg must raise TypeError
        with self.assertRaises(TypeError):
            evaluate_fundamental(obs)  # type: ignore[call-arg]

    def test_huge_finite_decimal_no_overflow(self):
        policy = FundamentalPolicy(
            min_roe=Decimal("1e400"),
            min_eps=Decimal("1e400"),
            min_revenue_growth=Decimal("1e400"),
            min_profit_growth=Decimal("1e400"),
            max_age_days=90,
        )
        obs_pass = StockFundamentalObservation(
            symbol="600519",
            report_period=date(2026, 6, 30),
            published_at=self.cutoff - timedelta(days=20),
            fetched_at=self.cutoff - timedelta(days=20),
            source="akshare",
            revenue_growth_pct=Decimal("1e400"),
            net_profit_growth_pct=Decimal("1e400"),
            roe_pct=Decimal("1e400"),
            eps=Decimal("1e400"),
        )
        gate_pass = evaluate_fundamental(obs_pass, self.cutoff, policy)
        self.assertTrue(gate_pass.passed)

        obs_fail = StockFundamentalObservation(
            symbol="600519",
            report_period=date(2026, 6, 30),
            published_at=self.cutoff - timedelta(days=20),
            fetched_at=self.cutoff - timedelta(days=20),
            source="akshare",
            revenue_growth_pct=Decimal("9e399"),
            net_profit_growth_pct=Decimal("1e400"),
            roe_pct=Decimal("1e400"),
            eps=Decimal("1e400"),
        )
        gate_fail = evaluate_fundamental(obs_fail, self.cutoff, policy)
        self.assertFalse(gate_fail.criterion_passed)
        self.assertEqual(gate_fail.criterion_code, "FUNDAMENTAL_CRITERION_FAILED")

    def test_criterion_fails_when_metric_below_policy(self):
        obs = StockFundamentalObservation(
            symbol="600519",
            report_period=date(2026, 6, 30),
            published_at=self.cutoff - timedelta(days=20),
            fetched_at=self.cutoff - timedelta(days=20),
            source="akshare",
            revenue_growth_pct=2.0,  # Below min 5.0
            net_profit_growth_pct=18.0,
            roe_pct=12.0,
            eps=1.5,
        )
        gate = evaluate_fundamental(obs, self.cutoff, self.policy)
        self.assertEqual(gate.stamp.status, EvidenceStatus.READY)
        self.assertIsNone(gate.stamp.reason_code)
        self.assertTrue(gate.complete)
        self.assertFalse(gate.criterion_passed)
        self.assertEqual(gate.criterion_code, "FUNDAMENTAL_CRITERION_FAILED")
        self.assertFalse(gate.passed)

    def test_stale_observation_returns_not_ready_data_stale(self):
        obs = StockFundamentalObservation(
            symbol="600519",
            report_period=date(2026, 3, 31),
            published_at=self.cutoff - timedelta(days=100),  # > 90 days
            fetched_at=self.cutoff - timedelta(days=100),
            source="akshare",
            revenue_growth_pct=15.0,
            net_profit_growth_pct=18.0,
            roe_pct=12.0,
            eps=1.5,
        )
        gate = evaluate_fundamental(obs, self.cutoff, self.policy)
        self.assertEqual(gate.stamp.status, EvidenceStatus.NOT_READY)
        self.assertEqual(gate.stamp.freshness, Freshness.STALE)
        self.assertEqual(gate.stamp.reason_code, "DATA_STALE")
        self.assertFalse(gate.passed)

    def test_future_data_rejected_with_value_error(self):
        obs = StockFundamentalObservation(
            symbol="600519",
            report_period=date(2026, 6, 30),
            published_at=self.cutoff + timedelta(minutes=1),
            fetched_at=self.cutoff + timedelta(minutes=2),
            source="akshare",
            revenue_growth_pct=15.0,
            net_profit_growth_pct=18.0,
            roe_pct=12.0,
            eps=1.5,
        )
        with self.assertRaises(ValueError):
            evaluate_fundamental(obs, self.cutoff, self.policy)

    def test_missing_observation_returns_not_ready_missing(self):
        gate = evaluate_fundamental(None, self.cutoff, self.policy)
        self.assertEqual(gate.name, "fundamental")
        self.assertEqual(gate.stamp.status, EvidenceStatus.NOT_READY)
        self.assertEqual(gate.stamp.reason_code, "FUNDAMENTAL_DATA_MISSING")
        self.assertFalse(gate.complete)
        self.assertFalse(gate.criterion_passed)
        self.assertEqual(gate.criterion_code, "FUNDAMENTAL_CRITERION_FAILED")
        self.assertFalse(gate.passed)


class EvaluateValuationTests(unittest.TestCase):
    def setUp(self):
        self.policy = ValuationPolicy(
            max_pe=30.0,
            max_pb=3.0,
            max_ps=5.0,
            max_age_days=30,
        )
        self.cutoff = _aware_dt(2026, 9, 16, 9, 30)

    def test_fresh_passing_observation_returns_ready_gate_passed(self):
        obs = StockValuationObservation(
            symbol="000001",
            as_of=self.cutoff - timedelta(days=1),
            fetched_at=self.cutoff - timedelta(days=1),
            source="akshare",
            pe_ttm=15.0,
            pb=1.5,
            ps=2.0,
        )
        gate = evaluate_valuation(obs, self.cutoff, self.policy)
        self.assertEqual(gate.name, "valuation")
        self.assertEqual(gate.stamp.status, EvidenceStatus.READY)
        self.assertEqual(gate.stamp.freshness, Freshness.RECENT)
        self.assertEqual(gate.stamp.market_date, (self.cutoff - timedelta(days=1)).date())
        self.assertIsNone(gate.stamp.reason_code)
        self.assertTrue(gate.complete)
        self.assertTrue(gate.criterion_passed)
        self.assertIsNone(gate.criterion_code)
        self.assertTrue(gate.passed)

    def test_fetched_at_later_than_cutoff_allowed(self):
        # 真实场景：14:30 截止，14:34 采集
        t_as_of = self.cutoff
        t_fetch = self.cutoff + timedelta(minutes=4)
        obs = StockValuationObservation(
            symbol="000001",
            as_of=t_as_of,
            fetched_at=t_fetch,
            source="akshare",
            pe_ttm=15.0,
            pb=1.5,
            ps=2.0,
        )
        gate = evaluate_valuation(obs, self.cutoff, self.policy)
        self.assertEqual(gate.stamp.fetched_at, t_fetch)
        self.assertEqual(gate.stamp.as_of, t_as_of)
        self.assertEqual(gate.stamp.cutoff, self.cutoff)
        self.assertEqual(gate.stamp.market_date, t_as_of.date())
        self.assertTrue(gate.passed)

    def test_market_date_strictly_equals_as_of_date_not_cutoff_date(self):
        t_as_of = _aware_dt(2026, 9, 15, 15, 0)
        obs = StockValuationObservation(
            symbol="000001",
            as_of=t_as_of,
            fetched_at=self.cutoff,
            source="akshare",
            pe_ttm=15.0,
            pb=1.5,
            ps=2.0,
        )
        gate = evaluate_valuation(obs, self.cutoff, self.policy)
        self.assertEqual(gate.stamp.market_date, date(2026, 9, 15))
        self.assertNotEqual(gate.stamp.market_date, self.cutoff.date())

    def test_signature_explicit_and_no_fuzzy_dispatch(self):
        params = list(inspect.signature(evaluate_valuation).parameters.keys())
        self.assertEqual(params, ["observation", "cutoff", "policy"])
        obs = StockValuationObservation(
            symbol="000001",
            as_of=self.cutoff - timedelta(days=1),
            fetched_at=self.cutoff - timedelta(days=1),
            source="akshare",
            pe_ttm=15.0,
            pb=1.5,
            ps=2.0,
        )
        with self.assertRaises(TypeError):
            evaluate_valuation(obs, self.cutoff)  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            evaluate_valuation(obs)  # type: ignore[call-arg]

    def test_huge_finite_decimal_no_overflow(self):
        policy = ValuationPolicy(
            max_pe=Decimal("1e400"),
            max_pb=Decimal("1e400"),
            max_ps=Decimal("1e400"),
            max_age_days=30,
        )
        obs_pass = StockValuationObservation(
            symbol="000001",
            as_of=self.cutoff - timedelta(days=1),
            fetched_at=self.cutoff - timedelta(days=1),
            source="akshare",
            pe_ttm=Decimal("1e400"),
            pb=Decimal("1e400"),
            ps=Decimal("1e400"),
        )
        gate_pass = evaluate_valuation(obs_pass, self.cutoff, policy)
        self.assertTrue(gate_pass.passed)

        obs_fail = StockValuationObservation(
            symbol="000001",
            as_of=self.cutoff - timedelta(days=1),
            fetched_at=self.cutoff - timedelta(days=1),
            source="akshare",
            pe_ttm=Decimal("2e400"),
            pb=Decimal("1e400"),
            ps=Decimal("1e400"),
        )
        gate_fail = evaluate_valuation(obs_fail, self.cutoff, policy)
        self.assertEqual(gate_fail.stamp.status, EvidenceStatus.READY)
        self.assertIsNone(gate_fail.stamp.reason_code)
        self.assertFalse(gate_fail.criterion_passed)
        self.assertEqual(gate_fail.criterion_code, "VALUATION_CRITERION_FAILED")

    def test_negative_pe_fails_criterion_without_pretending_missing(self):
        # 负PE等有效数据只能criterion false，不能冒充缺失
        obs = StockValuationObservation(
            symbol="000001",
            as_of=self.cutoff - timedelta(days=1),
            fetched_at=self.cutoff - timedelta(days=1),
            source="akshare",
            pe_ttm=-15.0,  # Negative PE
            pb=1.5,
            ps=2.0,
        )
        gate = evaluate_valuation(obs, self.cutoff, self.policy)
        self.assertEqual(gate.stamp.status, EvidenceStatus.READY)
        self.assertIsNone(gate.stamp.reason_code)
        self.assertTrue(gate.complete)
        self.assertFalse(gate.criterion_passed)
        self.assertEqual(gate.criterion_code, "VALUATION_CRITERION_FAILED")
        self.assertFalse(gate.passed)

    def test_high_pe_fails_criterion(self):
        obs = StockValuationObservation(
            symbol="000001",
            as_of=self.cutoff - timedelta(days=1),
            fetched_at=self.cutoff - timedelta(days=1),
            source="akshare",
            pe_ttm=55.0,  # Exceeds max 30
            pb=1.5,
            ps=2.0,
        )
        gate = evaluate_valuation(obs, self.cutoff, self.policy)
        self.assertEqual(gate.stamp.status, EvidenceStatus.READY)
        self.assertIsNone(gate.stamp.reason_code)
        self.assertFalse(gate.criterion_passed)
        self.assertEqual(gate.criterion_code, "VALUATION_CRITERION_FAILED")
        self.assertFalse(gate.passed)

    def test_stale_valuation_data_returns_not_ready_data_stale(self):
        obs = StockValuationObservation(
            symbol="000001",
            as_of=self.cutoff - timedelta(days=45),  # > 30 days
            fetched_at=self.cutoff - timedelta(days=45),
            source="akshare",
            pe_ttm=15.0,
            pb=1.5,
            ps=2.0,
        )
        gate = evaluate_valuation(obs, self.cutoff, self.policy)
        self.assertEqual(gate.stamp.status, EvidenceStatus.NOT_READY)
        self.assertEqual(gate.stamp.freshness, Freshness.STALE)
        self.assertEqual(gate.stamp.reason_code, "DATA_STALE")
        self.assertFalse(gate.passed)

    def test_future_valuation_data_rejected_with_value_error(self):
        obs = StockValuationObservation(
            symbol="000001",
            as_of=self.cutoff + timedelta(minutes=5),
            fetched_at=self.cutoff + timedelta(minutes=5),
            source="akshare",
            pe_ttm=15.0,
            pb=1.5,
            ps=2.0,
        )
        with self.assertRaises(ValueError):
            evaluate_valuation(obs, self.cutoff, self.policy)

    def test_missing_valuation_observation_returns_not_ready(self):
        gate = evaluate_valuation(None, self.cutoff, self.policy)
        self.assertEqual(gate.name, "valuation")
        self.assertEqual(gate.stamp.status, EvidenceStatus.NOT_READY)
        self.assertEqual(gate.stamp.reason_code, "VALUATION_DATA_MISSING")
        self.assertFalse(gate.complete)
        self.assertFalse(gate.criterion_passed)
        self.assertEqual(gate.criterion_code, "VALUATION_CRITERION_FAILED")
        self.assertFalse(gate.passed)


if __name__ == "__main__":
    unittest.main()
