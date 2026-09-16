import math
import unittest
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from app.analysis.industry import (
    IndustryObservation,
    IndustryRankingPolicy,
    IndustryRankedItem,
    IndustryRankingResult,
    rank_industries,
    gate_for,
)
from app.domain.evidence import (
    AnalysisEvidenceBundle,
    EvidenceStamp,
    EvidenceStatus,
    Freshness,
    GateEvidence,
    MAX_SOURCE_LENGTH,
)


def _aware_dt(
    year: int = 2026,
    month: int = 9,
    day: int = 16,
    hour: int = 9,
    minute: int = 30,
) -> datetime:
    return datetime(year, month, day, hour, minute, 0, tzinfo=timezone.utc)


def _valid_obs(
    code: str = "BK0420",
    name: str = "白酒",
    as_of: datetime | None = None,
    fetched_at: datetime | None = None,
    source: str = "eastmoney",
    return_1d_pct: float | Decimal | int = 2.5,
    return_5d_pct: float | Decimal | int = 5.0,
    return_20d_pct: float | Decimal | int = 8.0,
    turnover_rate: float | Decimal | int = 3.2,
    advancers: int = 18,
    decliners: int = 2,
) -> IndustryObservation:
    t_as_of = as_of or _aware_dt(2026, 9, 16, 9, 30)
    t_fetched = fetched_at or (t_as_of + timedelta(minutes=5))
    return IndustryObservation(
        code=code,
        name=name,
        as_of=t_as_of,
        fetched_at=t_fetched,
        source=source,
        return_1d_pct=return_1d_pct,
        return_5d_pct=return_5d_pct,
        return_20d_pct=return_20d_pct,
        turnover_rate=turnover_rate,
        advancers=advancers,
        decliners=decliners,
    )


def _valid_policy(
    weight_1d: float | Decimal | int = Decimal("0.2"),
    weight_5d: float | Decimal | int = Decimal("0.2"),
    weight_20d: float | Decimal | int = Decimal("0.2"),
    weight_breadth: float | Decimal | int = Decimal("0.2"),
    weight_activity: float | Decimal | int = Decimal("0.2"),
    min_coverage: int = 3,
    max_age_hours: int = 24,
    min_score: float | Decimal | int = 60.0,
) -> IndustryRankingPolicy:
    return IndustryRankingPolicy(
        weight_1d=Decimal(str(weight_1d)) if not isinstance(weight_1d, Decimal) else weight_1d,
        weight_5d=Decimal(str(weight_5d)) if not isinstance(weight_5d, Decimal) else weight_5d,
        weight_20d=Decimal(str(weight_20d)) if not isinstance(weight_20d, Decimal) else weight_20d,
        weight_breadth=Decimal(str(weight_breadth)) if not isinstance(weight_breadth, Decimal) else weight_breadth,
        weight_activity=Decimal(str(weight_activity)) if not isinstance(weight_activity, Decimal) else weight_activity,
        min_coverage=min_coverage,
        max_age_hours=max_age_hours,
        min_score=min_score,
    )


def _valid_item(
    code: str = "BK0420",
    name: str = "白酒",
    rank: int = 1,
    score: float | Decimal | int = 80.0,
    observation: IndustryObservation | None = None,
) -> IndustryRankedItem:
    obs = observation or _valid_obs(code=code, name=name)
    return IndustryRankedItem(
        code=code,
        name=name,
        rank=rank,
        score=score,
        observation=obs,
    )


_DEFAULT = object()


def _valid_result(
    items: Any = _DEFAULT,
    stamp: Any = _DEFAULT,
    coverage: Any = _DEFAULT,
    required: Any = _DEFAULT,
    policy: Any = _DEFAULT,
) -> IndustryRankingResult:
    pol = _valid_policy(min_coverage=2) if policy is _DEFAULT else policy
    t = _aware_dt(2026, 9, 16, 9, 30)
    t_fetch = t + timedelta(minutes=5)
    t_cutoff = _aware_dt(2026, 9, 16, 10, 0)
    if items is _DEFAULT:
        obs1 = _valid_obs(code="BK0001", name="行业1", as_of=t, fetched_at=t_fetch, return_1d_pct=10.0)
        obs2 = _valid_obs(code="BK0002", name="行业2", as_of=t, fetched_at=t_fetch, return_1d_pct=5.0)
        it1 = _valid_item(code="BK0001", name="行业1", rank=1, score=90.0, observation=obs1)
        it2 = _valid_item(code="BK0002", name="行业2", rank=2, score=60.0, observation=obs2)
        res_items = (it1, it2)
    else:
        res_items = items

    res_cov = (
        len(res_items)
        if (coverage is _DEFAULT and hasattr(res_items, "__len__"))
        else (0 if coverage is _DEFAULT else coverage)
    )
    res_req = getattr(pol, "min_coverage", 2) if required is _DEFAULT else required

    if stamp is _DEFAULT:
        if (
            isinstance(res_items, (tuple, list))
            and len(res_items) > 0
            and isinstance(res_items[0], IndustryRankedItem)
        ):
            res_stamp = EvidenceStamp(
                source=res_items[0].observation.source,
                as_of=res_items[0].observation.as_of,
                fetched_at=max(
                    it.observation.fetched_at
                    for it in res_items
                    if isinstance(it, IndustryRankedItem)
                ),
                cutoff=t_cutoff,
                market_date=res_items[0].observation.as_of.date(),
                status=EvidenceStatus.READY,
                freshness=Freshness.RECENT,
            )
        else:
            res_stamp = EvidenceStamp(
                source="industry",
                as_of=t_cutoff,
                fetched_at=t_cutoff,
                cutoff=t_cutoff,
                market_date=t_cutoff.date(),
                status=EvidenceStatus.DEGRADED,
                freshness=Freshness.UNKNOWN,
                reason_code="DEGRADED_COVERAGE",
            )
    else:
        res_stamp = stamp

    return IndustryRankingResult(
        items=res_items,
        stamp=res_stamp,
        coverage=res_cov,
        required=res_req,
        policy=pol,
    )




class IndustryObservationTests(unittest.TestCase):
    def test_valid_observation_construction_and_immutability(self):
        obs = _valid_obs()
        self.assertEqual(obs.code, "BK0420")
        self.assertEqual(obs.name, "白酒")
        self.assertEqual(obs.source, "eastmoney")
        self.assertEqual(obs.advancers, 18)
        self.assertEqual(obs.decliners, 2)
        with self.assertRaises(FrozenInstanceError):
            obs.code = "BK0001"  # type: ignore[misc]

    def test_rejects_empty_or_whitespace_or_non_string_code_and_name(self):
        for invalid in ("", "   ", None, 123, True):
            with self.subTest(code=invalid):
                with self.assertRaises((ValueError, TypeError)):
                    _valid_obs(code=invalid)  # type: ignore[arg-type]
            with self.subTest(name=invalid):
                with self.assertRaises((ValueError, TypeError)):
                    _valid_obs(name=invalid)  # type: ignore[arg-type]

    def test_rejects_single_stock_code(self):
        for stock_code in ("600519", "000001", "300750", "688981", "510300", "159915"):
            with self.subTest(stock_code=stock_code):
                with self.assertRaises(ValueError):
                    _valid_obs(code=stock_code)

    def test_rejects_config_sector_input_in_code_or_name(self):
        for sector_str in ("synthetic", "sector", "fixture", "sector:tech", "sector_finance"):
            with self.subTest(sector_str=sector_str):
                with self.assertRaises(ValueError):
                    _valid_obs(code=sector_str)
                with self.assertRaises(ValueError):
                    _valid_obs(name=sector_str)

    def test_rejects_naive_or_non_datetime(self):
        naive_dt = datetime(2026, 9, 16, 9, 30)
        aware_dt = _aware_dt(2026, 9, 16, 9, 30)
        with self.assertRaises(ValueError):
            _valid_obs(as_of=naive_dt, fetched_at=aware_dt)
        with self.assertRaises(ValueError):
            _valid_obs(as_of=aware_dt, fetched_at=naive_dt)
        with self.assertRaises(TypeError):
            _valid_obs(as_of="2026-09-16", fetched_at=aware_dt)  # type: ignore[arg-type]

    def test_rejects_as_of_later_than_fetched_at(self):
        t1 = _aware_dt(2026, 9, 16, 10, 0)
        t2 = _aware_dt(2026, 9, 16, 9, 30)
        with self.assertRaises(ValueError):
            _valid_obs(as_of=t1, fetched_at=t2)

    def test_rejects_non_finite_or_bool_numbers(self):
        for non_finite in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                _valid_obs(return_1d_pct=non_finite)
            with self.assertRaises(ValueError):
                _valid_obs(turnover_rate=non_finite)
        for bool_val in (True, False):
            with self.assertRaises(TypeError):
                _valid_obs(return_1d_pct=bool_val)
            with self.assertRaises(TypeError):
                _valid_obs(turnover_rate=bool_val)

    def test_rejects_negative_turnover_rate(self):
        with self.assertRaises(ValueError):
            _valid_obs(turnover_rate=-0.01)

    def test_rejects_non_strict_int_or_negative_advancers_decliners(self):
        for invalid_count in (True, False, 1.5, "10", None):
            with self.assertRaises(TypeError):
                _valid_obs(advancers=invalid_count)  # type: ignore[arg-type]
            with self.assertRaises(TypeError):
                _valid_obs(decliners=invalid_count)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            _valid_obs(advancers=-1)
        with self.assertRaises(ValueError):
            _valid_obs(decliners=-1)

    def test_rejects_zero_total_constituents(self):
        with self.assertRaises(ValueError):
            _valid_obs(advancers=0, decliners=0)

    def test_rejects_leading_and_trailing_whitespace_in_code_name_source(self):
        for bad_code in (" BK0420", "BK0420 ", "\tBK0420", "BK0420\n", "  BK0420  "):
            with self.subTest(bad_code=bad_code):
                with self.assertRaises(ValueError):
                    _valid_obs(code=bad_code)
        for bad_name in (" 白酒", "白酒 ", "\t白酒", "白酒\n", "  白酒  "):
            with self.subTest(bad_name=bad_name):
                with self.assertRaises(ValueError):
                    _valid_obs(name=bad_name)
        for bad_source in (" eastmoney", "eastmoney ", "\teastmoney", "eastmoney\n"):
            with self.subTest(bad_source=bad_source):
                with self.assertRaises(ValueError):
                    _valid_obs(source=bad_source)

    def test_exception_does_not_echo_raw_long_text(self):
        long_text = "A" * 2000
        try:
            _valid_obs(code=long_text)
            self.fail("Expected exception for long code")
        except (ValueError, TypeError) as exc:
            self.assertLess(len(str(exc)), 150)
            self.assertNotIn(long_text, str(exc))


class IndustryRankingPolicyTests(unittest.TestCase):
    def test_valid_policy_and_immutability(self):
        p = _valid_policy()
        self.assertEqual(p.min_coverage, 3)
        self.assertEqual(p.max_age_hours, 24)
        self.assertEqual(p.min_score, 60.0)
        with self.assertRaises(FrozenInstanceError):
            p.min_coverage = 5  # type: ignore[misc]

    def test_weights_must_sum_to_one_decimal_exact(self):
        # Sum != 1
        with self.assertRaises(ValueError):
            IndustryRankingPolicy(
                weight_1d=Decimal("0.2"),
                weight_5d=Decimal("0.2"),
                weight_20d=Decimal("0.2"),
                weight_breadth=Decimal("0.2"),
                weight_activity=Decimal("0.19"),
                min_coverage=3,
                max_age_hours=24,
                min_score=60,
            )
        # Sum == 1 with floats/Decimals
        p = IndustryRankingPolicy(
            weight_1d=0.1,
            weight_5d=0.2,
            weight_20d=0.3,
            weight_breadth=0.2,
            weight_activity=0.2,
            min_coverage=3,
            max_age_hours=24,
            min_score=60,
        )
        self.assertIsNotNone(p)

    def test_rejects_negative_weights(self):
        with self.assertRaises(ValueError):
            IndustryRankingPolicy(
                weight_1d=Decimal("-0.1"),
                weight_5d=Decimal("0.3"),
                weight_20d=Decimal("0.3"),
                weight_breadth=Decimal("0.2"),
                weight_activity=Decimal("0.3"),
                min_coverage=3,
                max_age_hours=24,
                min_score=60,
            )

    def test_min_coverage_and_max_age_hours_must_be_positive_strict_int(self):
        for invalid in (0, -1, 1.5, True, False, "5"):
            with self.assertRaises((ValueError, TypeError)):
                IndustryRankingPolicy(
                    weight_1d=Decimal("0.2"),
                    weight_5d=Decimal("0.2"),
                    weight_20d=Decimal("0.2"),
                    weight_breadth=Decimal("0.2"),
                    weight_activity=Decimal("0.2"),
                    min_coverage=invalid,  # type: ignore[arg-type]
                    max_age_hours=24,
                    min_score=60,
                )
            with self.assertRaises((ValueError, TypeError)):
                IndustryRankingPolicy(
                    weight_1d=Decimal("0.2"),
                    weight_5d=Decimal("0.2"),
                    weight_20d=Decimal("0.2"),
                    weight_breadth=Decimal("0.2"),
                    weight_activity=Decimal("0.2"),
                    min_coverage=3,
                    max_age_hours=invalid,  # type: ignore[arg-type]
                    min_score=60,
                )

    def test_min_score_range_validation(self):
        for invalid_score in (-0.01, 100.01, float("nan"), True):
            with self.assertRaises((ValueError, TypeError)):
                _valid_policy(min_score=invalid_score)

    def test_rejects_string_weights(self):
        for w_field in ("weight_1d", "weight_5d", "weight_20d", "weight_breadth", "weight_activity"):
            kwargs: dict[str, object] = {
                "weight_1d": Decimal("0.2"),
                "weight_5d": Decimal("0.2"),
                "weight_20d": Decimal("0.2"),
                "weight_breadth": Decimal("0.2"),
                "weight_activity": Decimal("0.2"),
                "min_coverage": 3,
                "max_age_hours": 24,
                "min_score": 60,
            }
            kwargs[w_field] = "0.2"
            with self.subTest(w_field=w_field):
                with self.assertRaises(TypeError):
                    IndustryRankingPolicy(**kwargs)  # type: ignore[arg-type]



class RankIndustriesTests(unittest.TestCase):
    def test_rejects_duplicate_code(self):
        cutoff = _aware_dt(2026, 9, 16, 15, 0)
        t = _aware_dt(2026, 9, 16, 14, 0)
        policy = _valid_policy()
        obs1 = _valid_obs(code="BK0420", as_of=t, fetched_at=t)
        obs2 = _valid_obs(code="BK0420", as_of=t, fetched_at=t)
        with self.assertRaises(ValueError):
            rank_industries([obs1, obs2], cutoff, policy)

    def test_rejects_different_sources(self):
        cutoff = _aware_dt(2026, 9, 16, 15, 0)
        t = _aware_dt(2026, 9, 16, 14, 0)
        policy = _valid_policy()
        obs1 = _valid_obs(code="BK0420", source="eastmoney", as_of=t, fetched_at=t)
        obs2 = _valid_obs(code="BK0421", source="sina", as_of=t, fetched_at=t)
        with self.assertRaises(ValueError):
            rank_industries([obs1, obs2], cutoff, policy)

    def test_rejects_different_as_of_cross_section(self):
        cutoff = _aware_dt(2026, 9, 16, 15, 0)
        policy = _valid_policy()
        obs1 = _valid_obs(code="BK0420", as_of=_aware_dt(2026, 9, 16, 14, 0))
        obs2 = _valid_obs(code="BK0421", as_of=_aware_dt(2026, 9, 16, 14, 5))
        with self.assertRaises(ValueError):
            rank_industries([obs1, obs2], cutoff, policy)

    def test_rejects_as_of_later_than_cutoff(self):
        cutoff = _aware_dt(2026, 9, 16, 14, 0)
        policy = _valid_policy()
        obs = _valid_obs(code="BK0420", as_of=_aware_dt(2026, 9, 16, 14, 1))
        with self.assertRaises(ValueError):
            rank_industries([obs], cutoff, policy)

    def test_rejects_non_industry_observation(self):
        cutoff = _aware_dt(2026, 9, 16, 15, 0)
        policy = _valid_policy()
        with self.assertRaises(TypeError):
            rank_industries([{"code": "BK0420"}], cutoff, policy)  # type: ignore[list-item]

    def test_percentile_ranking_with_ties_and_score_descending_code_stable(self):
        cutoff = _aware_dt(2026, 9, 16, 15, 0)
        t = _aware_dt(2026, 9, 16, 14, 0)
        policy = _valid_policy(min_coverage=3)
        # 3 industries:
        # A has strictly highest returns, breadth, activity -> rank 1, score 100
        # B and C have identical values -> tied for ranks 2 and 3, average percentile
        obs_a = _valid_obs(
            code="BK0003",
            name="行业A",
            as_of=t,
            fetched_at=t,
            return_1d_pct=10.0,
            return_5d_pct=10.0,
            return_20d_pct=10.0,
            turnover_rate=10.0,
            advancers=10,
            decliners=0,  # breadth = 1.0
        )
        obs_b = _valid_obs(
            code="BK0002",
            name="行业B",
            as_of=t,
            fetched_at=t,
            return_1d_pct=1.0,
            return_5d_pct=1.0,
            return_20d_pct=1.0,
            turnover_rate=1.0,
            advancers=5,
            decliners=5,  # breadth = 0.5
        )
        obs_c = _valid_obs(
            code="BK0001",
            name="行业C",
            as_of=t,
            fetched_at=t,
            return_1d_pct=1.0,
            return_5d_pct=1.0,
            return_20d_pct=1.0,
            turnover_rate=1.0,
            advancers=5,
            decliners=5,  # breadth = 0.5
        )
        res = rank_industries([obs_a, obs_b, obs_c], cutoff, policy)
        self.assertEqual(res.coverage, 3)
        self.assertEqual(res.required, 3)
        self.assertEqual(res.stamp.status, EvidenceStatus.READY)
        self.assertIsNone(res.stamp.reason_code)

        items = res.items
        self.assertEqual(len(items), 3)
        # A is 1st
        self.assertEqual(items[0].code, "BK0003")
        self.assertEqual(items[0].rank, 1)
        self.assertAlmostEqual(items[0].score, 100.0, places=2)

        # B and C tie on score. Sorted stably by code asc: BK0001 before BK0002
        self.assertEqual(items[1].code, "BK0001")
        self.assertEqual(items[1].rank, 2)
        self.assertEqual(items[2].code, "BK0002")
        self.assertEqual(items[2].rank, 3)
        self.assertAlmostEqual(items[1].score, items[2].score, places=4)
        # Average rank for B and C is (1 + 2)/2 = 1.5, percentile = 1.5/3 * 100 = 50.0
        self.assertAlmostEqual(items[1].score, 50.0, places=2)

    def test_insufficient_coverage_degraded_status_and_retains_items(self):
        cutoff = _aware_dt(2026, 9, 16, 15, 0)
        t = _aware_dt(2026, 9, 16, 14, 0)
        policy = _valid_policy(min_coverage=5)  # requires 5, provide 2
        obs1 = _valid_obs(code="BK0001", as_of=t, fetched_at=t)
        obs2 = _valid_obs(code="BK0002", as_of=t, fetched_at=t)
        res = rank_industries([obs1, obs2], cutoff, policy)
        self.assertEqual(res.coverage, 2)
        self.assertEqual(res.required, 5)
        self.assertEqual(res.stamp.status, EvidenceStatus.DEGRADED)
        self.assertEqual(res.stamp.reason_code, "DEGRADED_COVERAGE")
        self.assertEqual(len(res.items), 2)

    def test_stale_data_not_ready_status(self):
        cutoff = _aware_dt(2026, 9, 16, 15, 0)
        # 30 hours old > max_age_hours=24
        t_stale = cutoff - timedelta(hours=30)
        policy = _valid_policy(min_coverage=2, max_age_hours=24)
        obs1 = _valid_obs(code="BK0001", as_of=t_stale, fetched_at=t_stale)
        obs2 = _valid_obs(code="BK0002", as_of=t_stale, fetched_at=t_stale)
        res = rank_industries([obs1, obs2], cutoff, policy)
        self.assertEqual(res.stamp.status, EvidenceStatus.NOT_READY)
        self.assertEqual(res.stamp.freshness, Freshness.STALE)
        self.assertEqual(res.stamp.reason_code, "DATA_STALE")


class GateForTests(unittest.TestCase):
    def setUp(self):
        self.cutoff = _aware_dt(2026, 9, 16, 15, 0)
        self.t = _aware_dt(2026, 9, 16, 14, 0)
        self.policy = _valid_policy(min_coverage=2, min_score=60.0)

    def test_rejects_single_stock_or_config_sector_in_gate_for(self):
        obs1 = _valid_obs(code="BK0001", as_of=self.t, fetched_at=self.t)
        obs2 = _valid_obs(code="BK0002", as_of=self.t, fetched_at=self.t)
        res = rank_industries([obs1, obs2], self.cutoff, self.policy)
        for forbidden in ("600519", "000001", "synthetic", "sector", "sector:tech"):
            with self.subTest(forbidden=forbidden):
                with self.assertRaises(ValueError):
                    res.gate_for(forbidden)
                with self.assertRaises(ValueError):
                    gate_for(res, forbidden)

    def test_gate_for_missing_industry_fails(self):
        obs1 = _valid_obs(code="BK0001", as_of=self.t, fetched_at=self.t)
        obs2 = _valid_obs(code="BK0002", as_of=self.t, fetched_at=self.t)
        res = rank_industries([obs1, obs2], self.cutoff, self.policy)
        gate = res.gate_for("BK9999")
        self.assertEqual(gate.name, "industry")
        self.assertFalse(gate.passed)
        self.assertEqual(gate.stamp.status, EvidenceStatus.NOT_READY)
        self.assertEqual(gate.stamp.reason_code, "INDUSTRY_DATA_MISSING")
        self.assertFalse(gate.complete)
        self.assertFalse(gate.criterion_passed)
        self.assertEqual(gate.criterion_code, "INDUSTRY_CRITERION_FAILED")

    def test_gate_for_degraded_coverage_fails(self):
        policy = _valid_policy(min_coverage=10, min_score=50.0)
        obs1 = _valid_obs(code="BK0001", as_of=self.t, fetched_at=self.t)
        obs2 = _valid_obs(code="BK0002", as_of=self.t, fetched_at=self.t)
        res = rank_industries([obs1, obs2], self.cutoff, policy)
        gate = res.gate_for("BK0001")
        self.assertEqual(gate.name, "industry")
        self.assertFalse(gate.passed)
        self.assertEqual(gate.stamp.status, EvidenceStatus.DEGRADED)
        self.assertEqual(gate.stamp.reason_code, "DEGRADED_COVERAGE")

    def test_gate_for_stale_data_fails(self):
        t_stale = self.cutoff - timedelta(hours=30)
        obs1 = _valid_obs(code="BK0001", as_of=t_stale, fetched_at=t_stale)
        obs2 = _valid_obs(code="BK0002", as_of=t_stale, fetched_at=t_stale)
        res = rank_industries([obs1, obs2], self.cutoff, self.policy)
        gate = res.gate_for("BK0001")
        self.assertEqual(gate.name, "industry")
        self.assertFalse(gate.passed)
        self.assertEqual(gate.stamp.status, EvidenceStatus.NOT_READY)
        self.assertEqual(gate.stamp.reason_code, "DATA_STALE")

    def test_gate_for_ready_criterion_evaluation(self):
        # 2 industries: BK0001 high score (100.0), BK0002 low score (50.0)
        obs_high = _valid_obs(
            code="BK0001",
            as_of=self.t,
            fetched_at=self.t,
            return_1d_pct=10.0,
            return_5d_pct=10.0,
            return_20d_pct=10.0,
            turnover_rate=10.0,
            advancers=10,
            decliners=0,
        )
        obs_low = _valid_obs(
            code="BK0002",
            as_of=self.t,
            fetched_at=self.t,
            return_1d_pct=1.0,
            return_5d_pct=1.0,
            return_20d_pct=1.0,
            turnover_rate=1.0,
            advancers=0,
            decliners=10,
        )
        policy = _valid_policy(min_coverage=2, min_score=60.0)
        res = rank_industries([obs_high, obs_low], self.cutoff, policy)

        # High score passes gate
        gate_high = res.gate_for("BK0001")
        self.assertTrue(gate_high.passed)
        self.assertTrue(gate_high.criterion_passed)
        self.assertIsNone(gate_high.criterion_code)
        self.assertEqual(gate_high.stamp.status, EvidenceStatus.READY)

        # Low score fails criterion
        gate_low = res.gate_for("BK0002")
        self.assertFalse(gate_low.passed)
        self.assertFalse(gate_low.criterion_passed)
        self.assertEqual(gate_low.criterion_code, "INDUSTRY_CRITERION_FAILED")
        self.assertEqual(gate_low.stamp.status, EvidenceStatus.READY)

    def test_gate_evidence_integration_with_bundle(self):
        obs_high = _valid_obs(
            code="BK0001",
            as_of=self.t,
            fetched_at=self.t,
            return_1d_pct=10.0,
            return_5d_pct=10.0,
            return_20d_pct=10.0,
            turnover_rate=10.0,
            advancers=10,
            decliners=0,
        )
        obs_low = _valid_obs(
            code="BK0002",
            as_of=self.t,
            fetched_at=self.t,
            return_1d_pct=1.0,
            return_5d_pct=1.0,
            return_20d_pct=1.0,
            turnover_rate=1.0,
            advancers=0,
            decliners=10,
        )
        policy = _valid_policy(min_coverage=2, min_score=60.0)
        res = rank_industries([obs_high, obs_low], self.cutoff, policy)
        industry_gate = res.gate_for("BK0001")

        # Mock other gates
        from tests.test_analysis_evidence import _valid_stamp
        stamp = _valid_stamp()
        bundle = AnalysisEvidenceBundle(
            industry=industry_gate,
            fundamental=GateEvidence(name="fundamental", stamp=stamp, complete=True, criterion_passed=True),
            valuation=GateEvidence(name="valuation", stamp=stamp, complete=True, criterion_passed=True),
            structure=GateEvidence(name="structure", stamp=stamp, complete=True, criterion_passed=True),
        )
        self.assertTrue(bundle.ready)
        self.assertEqual(bundle.blocked_by, ())

    def test_gate_for_boundary_exact_min_score(self):
        # When score equals min_score exactly, it passes
        obs = _valid_obs(code="BK0001", as_of=self.t, fetched_at=self.t)
        # Single observation gets 100.0 on all percentiles -> score = 100.0
        policy = _valid_policy(min_coverage=1, min_score=100.0)
        res = rank_industries([obs], self.cutoff, policy)
        gate = res.gate_for("BK0001")
        self.assertTrue(gate.passed)
        self.assertTrue(gate.criterion_passed)

    def test_gate_for_invalid_argument_types(self):
        obs = _valid_obs(code="BK0001", as_of=self.t, fetched_at=self.t)
        res = rank_industries([obs], self.cutoff, _valid_policy(min_coverage=1))
        for invalid_arg in (123, None, ["BK0001"], {"code": "BK0001"}, True):
            with self.assertRaises(TypeError):
                res.gate_for(invalid_arg)  # type: ignore[arg-type]
            with self.assertRaises(TypeError):
                gate_for(res, invalid_arg)  # type: ignore[arg-type]

    def test_gate_for_long_string_no_echo(self):
        obs = _valid_obs(code="BK0001", as_of=self.t, fetched_at=self.t)
        res = rank_industries([obs], self.cutoff, _valid_policy(min_coverage=1))
        long_str = "B" * 3000
        try:
            res.gate_for(long_str)
            self.fail("Expected ValueError for long code")
        except ValueError as exc:
            self.assertLess(len(str(exc)), 150)
            self.assertNotIn(long_str, str(exc))

    def test_empty_observations(self):
        policy = _valid_policy(min_coverage=3)
        res = rank_industries([], self.cutoff, policy)
        self.assertEqual(res.coverage, 0)
        self.assertEqual(res.required, 3)
        self.assertEqual(res.items, ())
        self.assertEqual(res.stamp.status, EvidenceStatus.DEGRADED)
        self.assertEqual(res.stamp.freshness, Freshness.UNKNOWN)
        self.assertEqual(res.stamp.reason_code, "DEGRADED_COVERAGE")
        gate = res.gate_for("BK0001")
        self.assertFalse(gate.passed)
        self.assertEqual(gate.stamp.reason_code, "INDUSTRY_DATA_MISSING")

    def test_both_stale_and_insufficient_coverage_yields_data_stale(self):
        # When both stale and coverage < required, NOT_READY + DATA_STALE takes precedence
        t_stale = self.cutoff - timedelta(hours=50)
        policy = _valid_policy(min_coverage=5, max_age_hours=24)
        obs = _valid_obs(code="BK0001", as_of=t_stale, fetched_at=t_stale)
        res = rank_industries([obs], self.cutoff, policy)
        self.assertEqual(res.coverage, 1)
        self.assertEqual(res.stamp.status, EvidenceStatus.NOT_READY)
        self.assertEqual(res.stamp.reason_code, "DATA_STALE")

    def test_all_five_factors_contribute_to_ranking(self):
        # Verify that policy weights properly affect the final ranking
        # by creating an industry that wins on 1d only vs an industry that wins on all other 4 factors
        t = self.t
        # Obs 1 wins heavily on 1d, loses on 5d, 20d, breadth, activity
        obs1 = _valid_obs(
            code="BK0001",
            as_of=t,
            fetched_at=t,
            return_1d_pct=20.0,
            return_5d_pct=1.0,
            return_20d_pct=1.0,
            turnover_rate=1.0,
            advancers=1,
            decliners=9,
        )
        # Obs 2 loses on 1d, wins on 5d, 20d, breadth, activity
        obs2 = _valid_obs(
            code="BK0002",
            as_of=t,
            fetched_at=t,
            return_1d_pct=1.0,
            return_5d_pct=20.0,
            return_20d_pct=20.0,
            turnover_rate=20.0,
            advancers=9,
            decliners=1,
        )
        # With high 1d weight (0.8) and low other weights (0.05 each), Obs 1 wins
        policy_heavy_1d = IndustryRankingPolicy(
            weight_1d=Decimal("0.8"),
            weight_5d=Decimal("0.05"),
            weight_20d=Decimal("0.05"),
            weight_breadth=Decimal("0.05"),
            weight_activity=Decimal("0.05"),
            min_coverage=2,
            max_age_hours=24,
            min_score=50,
        )
        res_heavy_1d = rank_industries([obs1, obs2], self.cutoff, policy_heavy_1d)
        self.assertEqual(res_heavy_1d.items[0].code, "BK0001")

        # With low 1d weight (0.04) and high 5d weight (0.84), Obs 2 wins
        policy_heavy_5d = IndustryRankingPolicy(
            weight_1d=Decimal("0.04"),
            weight_5d=Decimal("0.84"),
            weight_20d=Decimal("0.04"),
            weight_breadth=Decimal("0.04"),
            weight_activity=Decimal("0.04"),
            min_coverage=2,
            max_age_hours=24,
            min_score=50,
        )
        res_heavy_5d = rank_industries([obs1, obs2], self.cutoff, policy_heavy_5d)
        self.assertEqual(res_heavy_5d.items[0].code, "BK0002")


class IndustryRankedItemValidationTests(unittest.TestCase):
    def test_valid_construction_and_immutability(self):
        obs = _valid_obs(code="BK0420", name="白酒")
        item = IndustryRankedItem(
            code="BK0420",
            name="白酒",
            rank=1,
            score=85.5,
            observation=obs,
        )
        self.assertEqual(item.code, "BK0420")
        self.assertEqual(item.name, "白酒")
        self.assertEqual(item.rank, 1)
        self.assertEqual(item.score, 85.5)
        self.assertEqual(item.observation, obs)
        with self.assertRaises(FrozenInstanceError):
            item.rank = 2  # type: ignore[misc]

    def test_rejects_non_string_empty_or_whitespace_code_and_name(self):
        obs = _valid_obs()
        for invalid_code in ("", "   ", " BK0420", "BK0420 ", "  BK0420  ", None, 123, True):
            with self.subTest(code=invalid_code):
                with self.assertRaises((ValueError, TypeError)):
                    IndustryRankedItem(code=invalid_code, name="白酒", rank=1, score=80.0, observation=obs)  # type: ignore[arg-type]
        for invalid_name in ("", "   ", " 白酒", "白酒 ", "  白酒  ", None, 123, True):
            with self.subTest(name=invalid_name):
                with self.assertRaises((ValueError, TypeError)):
                    IndustryRankedItem(code="BK0420", name=invalid_name, rank=1, score=80.0, observation=obs)  # type: ignore[arg-type]

    def test_rejects_single_stock_or_config_sector_in_item(self):
        obs = _valid_obs()
        for forbidden in ("600519", "000001", "300750", "synthetic", "sector", "sector:tech"):
            with self.subTest(forbidden=forbidden):
                with self.assertRaises(ValueError):
                    IndustryRankedItem(code=forbidden, name="行业", rank=1, score=80.0, observation=obs)
        for forbidden in ("synthetic", "sector", "config_sector:tech"):
            with self.subTest(forbidden_name=forbidden):
                with self.assertRaises(ValueError):
                    IndustryRankedItem(code="BK0420", name=forbidden, rank=1, score=80.0, observation=obs)

    def test_rejects_invalid_rank(self):
        obs = _valid_obs()
        for inv_rank in (0, -1, -5):
            with self.subTest(inv_rank=inv_rank):
                with self.assertRaises(ValueError):
                    IndustryRankedItem(code="BK0420", name="白酒", rank=inv_rank, score=80.0, observation=obs)
        for inv_rank in (True, False, 1.5, "1", None):
            with self.subTest(inv_rank=inv_rank):
                with self.assertRaises(TypeError):
                    IndustryRankedItem(code="BK0420", name="白酒", rank=inv_rank, score=80.0, observation=obs)  # type: ignore[arg-type]

    def test_rejects_invalid_score(self):
        obs = _valid_obs()
        for inv_score in (-0.01, -1.0, 100.01, 150.0, float("nan"), float("inf"), float("-inf")):
            with self.subTest(inv_score=inv_score):
                with self.assertRaises(ValueError):
                    IndustryRankedItem(code="BK0420", name="白酒", rank=1, score=inv_score, observation=obs)
        for inv_score in (True, False, "80.0", None, [80.0]):
            with self.subTest(inv_score=inv_score):
                with self.assertRaises(TypeError):
                    IndustryRankedItem(code="BK0420", name="白酒", rank=1, score=inv_score, observation=obs)  # type: ignore[arg-type]
        # Valid scores at boundary
        self.assertIsNotNone(IndustryRankedItem(code="BK0420", name="白酒", rank=1, score=0.0, observation=obs))
        self.assertIsNotNone(IndustryRankedItem(code="BK0420", name="白酒", rank=1, score=100.0, observation=obs))
        self.assertIsNotNone(IndustryRankedItem(code="BK0420", name="白酒", rank=1, score=Decimal("50.0"), observation=obs))
        self.assertIsNotNone(IndustryRankedItem(code="BK0420", name="白酒", rank=1, score=50, observation=obs))

    def test_rejects_invalid_observation(self):
        for inv_obs in (None, "not_an_obs", {"code": "BK0420"}):
            with self.subTest(inv_obs=inv_obs):
                with self.assertRaises(TypeError):
                    IndustryRankedItem(code="BK0420", name="白酒", rank=1, score=80.0, observation=inv_obs)  # type: ignore[arg-type]

        # observation code mismatch
        obs = _valid_obs(code="BK0420", name="白酒")
        with self.assertRaises(ValueError):
            IndustryRankedItem(code="BK0421", name="白酒", rank=1, score=80.0, observation=obs)

        # observation name mismatch
        with self.assertRaises(ValueError):
            IndustryRankedItem(code="BK0420", name="啤酒", rank=1, score=80.0, observation=obs)


class IndustryRankingResultValidationTests(unittest.TestCase):
    def test_items_must_be_strict_tuple_and_rejects_list_mapping(self):
        it1 = _valid_item(code="BK0001", name="行业1", rank=1, score=90.0)
        # list
        with self.assertRaises(TypeError):
            _valid_result(items=[it1])  # type: ignore[arg-type]
        # mapping
        with self.assertRaises(TypeError):
            _valid_result(items={"BK0001": it1})  # type: ignore[arg-type]
        # tuple containing non-item
        with self.assertRaises(TypeError):
            _valid_result(items=(it1, "not_an_item"))  # type: ignore[arg-type]

    def test_stamp_and_policy_type_validation(self):
        with self.assertRaises(TypeError):
            _valid_result(stamp="not_a_stamp")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            _valid_result(policy="not_a_policy")  # type: ignore[arg-type]

    def test_coverage_and_required_strict_int_and_bounds(self):
        for inv in (True, False, 1.5, "2", None):
            with self.subTest(coverage=inv):
                with self.assertRaises(TypeError):
                    _valid_result(coverage=inv)  # type: ignore[arg-type]
            with self.subTest(required=inv):
                with self.assertRaises(TypeError):
                    _valid_result(required=inv)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            _valid_result(coverage=-1)
        with self.assertRaises(ValueError):
            _valid_result(required=0)
        with self.assertRaises(ValueError):
            _valid_result(required=-1)

    def test_coverage_equals_len_items_and_required_equals_policy_min_coverage(self):
        # coverage != len(items)
        with self.assertRaises(ValueError):
            _valid_result(coverage=1)
        with self.assertRaises(ValueError):
            _valid_result(coverage=3)
        # required != policy.min_coverage
        pol = _valid_policy(min_coverage=3)
        with self.assertRaises(ValueError):
            _valid_result(required=2, policy=pol)

    def test_code_must_be_unique_in_items(self):
        t = _aware_dt()
        obs1 = _valid_obs(code="BK0001", name="行业1", as_of=t, fetched_at=t)
        obs2 = _valid_obs(code="BK0001", name="行业1", as_of=t, fetched_at=t)
        it1 = _valid_item(code="BK0001", name="行业1", rank=1, score=90.0, observation=obs1)
        it2 = _valid_item(code="BK0001", name="行业1", rank=2, score=80.0, observation=obs2)
        with self.assertRaises(ValueError):
            _valid_result(items=(it1, it2))

    def test_ranks_must_be_exactly_one_to_n(self):
        t = _aware_dt()
        obs1 = _valid_obs(code="BK0001", name="行业1", as_of=t, fetched_at=t)
        obs2 = _valid_obs(code="BK0002", name="行业2", as_of=t, fetched_at=t)
        # rank starting at 2
        it1 = _valid_item(code="BK0001", name="行业1", rank=2, score=90.0, observation=obs1)
        it2 = _valid_item(code="BK0002", name="行业2", rank=3, score=80.0, observation=obs2)
        with self.assertRaises(ValueError):
            _valid_result(items=(it1, it2))

        # inverted ranks: 2, 1
        it1_rev = _valid_item(code="BK0001", name="行业1", rank=2, score=90.0, observation=obs1)
        it2_rev = _valid_item(code="BK0002", name="行业2", rank=1, score=80.0, observation=obs2)
        with self.assertRaises(ValueError):
            _valid_result(items=(it1_rev, it2_rev))

        # duplicate ranks: 1, 1
        it2_dup = _valid_item(code="BK0002", name="行业2", rank=1, score=80.0, observation=obs2)
        with self.assertRaises(ValueError):
            _valid_result(items=(it1, it2_dup))

    def test_items_must_be_sorted_by_score_descending_and_code_ascending(self):
        t = _aware_dt()
        obs1 = _valid_obs(code="BK0001", name="行业1", as_of=t, fetched_at=t)
        obs2 = _valid_obs(code="BK0002", name="行业2", as_of=t, fetched_at=t)
        # wrong score order: 60 then 90
        it1 = _valid_item(code="BK0001", name="行业1", rank=1, score=60.0, observation=obs1)
        it2 = _valid_item(code="BK0002", name="行业2", rank=2, score=90.0, observation=obs2)
        with self.assertRaises(ValueError):
            _valid_result(items=(it1, it2))

        # tied score with wrong code order: BK0002 before BK0001
        it1_tie_bad = _valid_item(code="BK0002", name="行业2", rank=1, score=80.0, observation=obs2)
        it2_tie_bad = _valid_item(code="BK0001", name="行业1", rank=2, score=80.0, observation=obs1)
        with self.assertRaises(ValueError):
            _valid_result(items=(it1_tie_bad, it2_tie_bad))

        # tied score with correct code order: BK0001 before BK0002
        it1_tie_good = _valid_item(code="BK0001", name="行业1", rank=1, score=80.0, observation=obs1)
        it2_tie_good = _valid_item(code="BK0002", name="行业2", rank=2, score=80.0, observation=obs2)
        res = _valid_result(items=(it1_tie_good, it2_tie_good))
        self.assertIsNotNone(res)

    def test_cross_section_source_as_of_and_fetched_at_consistency(self):
        t = _aware_dt()
        t_fetch1 = t + timedelta(minutes=5)
        t_fetch2 = t + timedelta(minutes=10)
        obs1 = _valid_obs(code="BK0001", name="行业1", source="eastmoney", as_of=t, fetched_at=t_fetch1)
        obs2 = _valid_obs(code="BK0002", name="行业2", source="eastmoney", as_of=t, fetched_at=t_fetch2)
        it1 = _valid_item(code="BK0001", name="行业1", rank=1, score=90.0, observation=obs1)
        it2 = _valid_item(code="BK0002", name="行业2", rank=2, score=80.0, observation=obs2)

        # stamp source mismatch
        stamp_bad_source = EvidenceStamp(
            source="sina",
            as_of=t,
            fetched_at=t_fetch2,
            cutoff=t + timedelta(hours=1),
            market_date=t.date(),
            status=EvidenceStatus.READY,
            freshness=Freshness.RECENT,
        )
        with self.assertRaises(ValueError):
            _valid_result(items=(it1, it2), stamp=stamp_bad_source)

        # stamp as_of mismatch
        stamp_bad_as_of = EvidenceStamp(
            source="eastmoney",
            as_of=t - timedelta(minutes=1),
            fetched_at=t_fetch2,
            cutoff=t + timedelta(hours=1),
            market_date=t.date(),
            status=EvidenceStatus.READY,
            freshness=Freshness.RECENT,
        )
        with self.assertRaises(ValueError):
            _valid_result(items=(it1, it2), stamp=stamp_bad_as_of)

        # stamp fetched_at mismatch (t_fetch1 instead of max t_fetch2)
        stamp_bad_fetched_at = EvidenceStamp(
            source="eastmoney",
            as_of=t,
            fetched_at=t_fetch1,
            cutoff=t + timedelta(hours=1),
            market_date=t.date(),
            status=EvidenceStatus.READY,
            freshness=Freshness.RECENT,
        )
        with self.assertRaises(ValueError):
            _valid_result(items=(it1, it2), stamp=stamp_bad_fetched_at)

        # individual observation source mismatch
        obs_bad_source = _valid_obs(code="BK0002", name="行业2", source="sina", as_of=t, fetched_at=t_fetch2)
        it2_bad_source = _valid_item(code="BK0002", name="行业2", rank=2, score=80.0, observation=obs_bad_source)
        with self.assertRaises(ValueError):
            _valid_result(items=(it1, it2_bad_source))

        # individual observation as_of mismatch
        obs_bad_as_of = _valid_obs(code="BK0002", name="行业2", source="eastmoney", as_of=t - timedelta(minutes=1), fetched_at=t_fetch2)
        it2_bad_as_of = _valid_item(code="BK0002", name="行业2", rank=2, score=80.0, observation=obs_bad_as_of)
        with self.assertRaises(ValueError):
            _valid_result(items=(it1, it2_bad_as_of))

    def test_rejects_forged_ready_results(self):
        t = _aware_dt()
        cutoff = t + timedelta(hours=1)
        policy = _valid_policy(min_coverage=5)

        # Forged READY with empty items
        stamp_ready_empty = EvidenceStamp(
            source="industry",
            as_of=t,
            fetched_at=t,
            cutoff=cutoff,
            market_date=t.date(),
            status=EvidenceStatus.READY,
            freshness=Freshness.RECENT,
        )
        with self.assertRaises(ValueError):
            IndustryRankingResult(items=(), stamp=stamp_ready_empty, coverage=0, required=5, policy=policy)

        # Forged READY with coverage < required
        obs1 = _valid_obs(code="BK0001", name="行业1", as_of=t, fetched_at=t)
        it1 = _valid_item(code="BK0001", name="行业1", rank=1, score=90.0, observation=obs1)
        stamp_ready = EvidenceStamp(
            source="eastmoney",
            as_of=t,
            fetched_at=t,
            cutoff=cutoff,
            market_date=t.date(),
            status=EvidenceStatus.READY,
            freshness=Freshness.RECENT,
        )
        with self.assertRaises(ValueError):
            IndustryRankingResult(items=(it1,), stamp=stamp_ready, coverage=1, required=5, policy=policy)

        # Forged READY with stale data
        t_stale = cutoff - timedelta(hours=30)
        obs_stale = _valid_obs(code="BK0001", name="行业1", as_of=t_stale, fetched_at=t_stale)
        it_stale = _valid_item(code="BK0001", name="行业1", rank=1, score=90.0, observation=obs_stale)
        policy_stale = _valid_policy(min_coverage=1, max_age_hours=24)
        stamp_stale_ready = EvidenceStamp(
            source="eastmoney",
            as_of=t_stale,
            fetched_at=t_stale,
            cutoff=cutoff,
            market_date=t_stale.date(),
            status=EvidenceStatus.READY,
            freshness=Freshness.RECENT,
        )
        with self.assertRaises(ValueError):
            IndustryRankingResult(items=(it_stale,), stamp=stamp_stale_ready, coverage=1, required=1, policy=policy_stale)

    def test_empty_items_constraints(self):
        policy = _valid_policy(min_coverage=3)
        t = _aware_dt()
        stamp_degraded = EvidenceStamp(
            source="industry",
            as_of=t,
            fetched_at=t,
            cutoff=t,
            market_date=t.date(),
            status=EvidenceStatus.DEGRADED,
            freshness=Freshness.UNKNOWN,
            reason_code="DEGRADED_COVERAGE",
        )
        # coverage != 0 when items empty
        with self.assertRaises(ValueError):
            IndustryRankingResult(items=(), stamp=stamp_degraded, coverage=1, required=3, policy=policy)

        # valid empty items result
        res = IndustryRankingResult(items=(), stamp=stamp_degraded, coverage=0, required=3, policy=policy)
        self.assertEqual(len(res.items), 0)
        self.assertEqual(res.coverage, 0)
        self.assertEqual(res.stamp.status, EvidenceStatus.DEGRADED)


class IndustryRankingFloatNarrowingRegressionTests(unittest.TestCase):
    def test_ranking_float_narrowing_tie_break_regression(self):
        t = _aware_dt(2026, 9, 16, 9, 30)
        observations = [
            IndustryObservation(
                code=f"BK{i:04d}",
                name=f"行业{i}",
                as_of=t,
                fetched_at=t + timedelta(minutes=1),
                source="eastmoney",
                return_1d_pct=i % 17,
                return_5d_pct=i % 19,
                return_20d_pct=i % 23,
                turnover_rate=(i % 13) + 1,
                advancers=(i % 10) + 1,
                decliners=((i + 3) % 10) + 1,
            )
            for i in range(495)
        ]
        policy = IndustryRankingPolicy(
            weight_1d=0.30,
            weight_5d=0.25,
            weight_20d=0.20,
            weight_breadth=0.15,
            weight_activity=0.10,
            min_coverage=8,
            max_age_hours=96,
            min_score=60,
        )
        cutoff = t + timedelta(hours=1)
        result = rank_industries(observations, cutoff, policy)
        self.assertEqual(len(result.items), len(observations))
        for i in range(len(result.items) - 1):
            prev_it = result.items[i]
            next_it = result.items[i + 1]
            self.assertGreaterEqual(prev_it.score, next_it.score)
            if prev_it.score == next_it.score:
                self.assertLess(prev_it.code, next_it.code)


if __name__ == "__main__":
    unittest.main()


