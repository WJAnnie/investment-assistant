"""Offline unit and integration tests for industry ranking wiring.

Covers:
1. analyze_portfolio default path byte-for-byte equivalence
2. provider exception fail-closed
3. rank_industries exception fail-closed
4. empty observations DEGRADED handling
5. insufficient coverage (< min_coverage) DEGRADED handling
6. stale observations NOT_READY handling
7. normal READY ranking and consecutive 1..n ranks
8. _latest_closed_mainland_market_date business day and holiday rollover
9. to_jsonable and json.dumps serialization safety
10. report available branch rendering and removing legacy label disclaimer
11. report unavailable branch preserving legacy text
12. report maximum 10 items rendering
13. report non-finite score fail-closed skipping
14. morning and global report redlines (forbidden tokens & mandatory phrases)
15. run_portfolio_report passthrough of industry_provider
16. run_portfolio_report default industry_provider is None
17. DEFAULT_INDUSTRY_POLICY weights sum to 1
18. _analysis_gaps with and without available industry limits
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import json
import math
import unittest
from zoneinfo import ZoneInfo

from app.analysis.industry import (
    IndustryObservation,
    IndustryRankingPolicy,
)
from app.domain.evidence import EvidenceStatus
from app.portfolio.analysis import (
    DEFAULT_INDUSTRY_POLICY,
    _latest_closed_mainland_market_date,
    analyze_portfolio,
)
from app.portfolio.calculator import build_portfolio_snapshot
from app.portfolio.models import (
    AccountConfig,
    AccountSnapshot,
    HoldingConfig,
    HoldingSnapshot,
    PortfolioConfig,
    PortfolioSnapshot,
    Valuation,
)
from app.report.portfolio import format_portfolio_report
from app.utils.serialization import to_jsonable
from app.workflow.portfolio import _analysis_gaps, run_portfolio_report


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
FRIDAY_CLOSE = datetime(2026, 9, 11, 15, 0, tzinfo=SHANGHAI_TZ)
MONDAY_MORNING = datetime(2026, 9, 14, 9, 0, tzinfo=SHANGHAI_TZ)

REDLINE_FORBIDDEN_TOKENS = (
    "manual_replay",
    "available_unverified",
    "RECENT",
    "STALE",
    "yahoo_chart",
    "fred_dgs10",
    "error_code",
    "provider",
    "None",
    "completed",
    "not_collected",
    "INVALID_OBSERVATION",
    "full_analysis_ready",
    "data_limits",
    "立即买入",
    "立即卖出",
    "自动执行交易",
)

MANDATORY_PHRASES = (
    "不自动下单",
    "人工操作提醒",
    "进入人工复核",
    "WAIT",
    "本人确认并手动执行",
)


@dataclass(frozen=True)
class FakeIndustryFetch:
    observations: tuple[IndustryObservation, ...]
    as_of_start: datetime | None = None
    as_of_end: datetime | None = None


class FakeIndustryProvider:
    def __init__(self, observations=None, error=None, as_of_start=None, as_of_end=None):
        self.observations = tuple(observations) if observations is not None else None
        self.error = error
        self.as_of_start = as_of_start
        self.as_of_end = as_of_end
        self.fetch_calls = []

    def fetch(self, cutoff, market_date):
        self.fetch_calls.append({"cutoff": cutoff, "market_date": market_date})
        if self.error is not None:
            raise self.error
        return FakeIndustryFetch(
            observations=self.observations or (),
            as_of_start=self.as_of_start,
            as_of_end=self.as_of_end,
        )


def _build_observations(count=10, as_of=None, source="test_source"):
    as_of = as_of or FRIDAY_CLOSE
    names = (
        "电子", "计算机", "通信", "医药生物", "银行",
        "非银金融", "食品饮料", "家用电器", "机械设备", "国防军工",
        "电力设备", "基础化工", "汽车", "有色金属", "商贸零售",
    )
    obs = []
    for idx in range(count):
        name = names[idx % len(names)] if idx < len(names) else f"板块{idx}"
        code = f"BK{idx + 1:04d}"
        obs.append(
            IndustryObservation(
                code=code,
                name=name,
                as_of=as_of,
                fetched_at=as_of,
                source=source,
                return_1d_pct=1.0 + idx * 0.1,
                return_5d_pct=2.0 + idx * 0.1,
                return_20d_pct=3.0 + idx * 0.1,
                turnover_rate=0.05 + idx * 0.005,
                advancers=50 + idx,
                decliners=50 - idx,
            )
        )
    return tuple(obs)


def _config():
    holding = HoldingConfig(
        code="600000",
        name="浦发银行",
        market="CN",
        instrument_type="stock",
        valuation_mode="exchange",
        cost_price=Decimal("9"),
        baseline_value=Decimal("1000"),
        sector="financials",
        theme="bank",
        quantity=Decimal("100"),
    )
    account = AccountConfig(
        account_id="A",
        name="A账户",
        strategy="long_term_core",
        baseline_date=date(2026, 9, 11),
        total_assets=Decimal("1500"),
        cash=Decimal("500"),
        holdings=(holding,),
    )
    return PortfolioConfig(schema_version=2, accounts=(account,))


def _snapshot():
    cfg = _config()
    return build_portfolio_snapshot(cfg, {})


def _report_snapshot():
    val = Valuation(
        "600000",
        Decimal("10"),
        Decimal("0.5"),
        FRIDAY_CLOSE,
        "test",
        freshness="fresh",
    )
    cfg = _config()
    holding = cfg.accounts[0].holdings[0]
    holding_snap = HoldingSnapshot(
        account_id="A",
        holding=holding,
        current_price=Decimal("10"),
        market_value=Decimal("1000"),
        account_weight=Decimal("0.6667"),
        return_from_cost=Decimal("0.1111"),
        monetary_profit=Decimal("100"),
        value_precision="exact",
        valuation=val,
        warnings=(),
    )
    account_snap = AccountSnapshot(
        account=cfg.accounts[0],
        holdings=(holding_snap,),
        holdings_value=Decimal("1000"),
        cash=Decimal("500"),
        total_assets=Decimal("1500"),
        position_percent=Decimal("0.6667"),
        warnings=(),
    )
    return PortfolioSnapshot(
        accounts=(account_snap,),
        total_assets=Decimal("1500"),
        holdings_value=Decimal("1000"),
        cash=Decimal("500"),
        position_percent=Decimal("0.6667"),
    )


class IndustryWiringAnalysisTests(unittest.TestCase):
    def test_default_path_byte_for_byte_equivalent(self):
        cfg = _config()
        snap = _snapshot()
        res_default = analyze_portfolio(cfg, snap, now=MONDAY_MORNING)
        res_explicit_none = analyze_portfolio(cfg, snap, now=MONDAY_MORNING, industry_provider=None)

        self.assertNotIn("industry_ranking", res_default)
        self.assertNotIn("industry_ranking", res_explicit_none)
        self.assertEqual(res_default["data_limits"]["industry"], "configured_labels_only")
        self.assertEqual(res_explicit_none["data_limits"]["industry"], "configured_labels_only")
        self.assertEqual(res_default, res_explicit_none)

    def test_provider_exception_fail_closed(self):
        cfg = _config()
        snap = _snapshot()
        provider = FakeIndustryProvider(error=RuntimeError("provider network dropped"))
        res = analyze_portfolio(cfg, snap, now=MONDAY_MORNING, industry_provider=provider)

        self.assertIn("industry_ranking", res)
        ranking = res["industry_ranking"]
        self.assertEqual(ranking["status"], "not_available")
        self.assertIsNone(ranking["reason_code"])
        self.assertIsNone(ranking["as_of"])
        self.assertEqual(ranking["coverage"], 0)
        self.assertEqual(ranking["required"], DEFAULT_INDUSTRY_POLICY.min_coverage)
        self.assertEqual(ranking["items"], ())
        self.assertEqual(res["data_limits"]["industry"], "configured_labels_only")

    def test_rank_industries_exception_fail_closed(self):
        cfg = _config()
        snap = _snapshot()
        # Mixed source observations trigger ValueError in rank_industries
        obs1 = _build_observations(1, source="src_a")[0]
        obs2 = _build_observations(1, source="src_b")[0]
        provider = FakeIndustryProvider(observations=(obs1, obs2))
        res = analyze_portfolio(cfg, snap, now=MONDAY_MORNING, industry_provider=provider)

        self.assertIn("industry_ranking", res)
        ranking = res["industry_ranking"]
        self.assertEqual(ranking["status"], "not_available")
        self.assertEqual(ranking["items"], ())
        self.assertEqual(res["data_limits"]["industry"], "configured_labels_only")

    def test_empty_observations_degraded(self):
        cfg = _config()
        snap = _snapshot()
        provider = FakeIndustryProvider(observations=())
        res = analyze_portfolio(cfg, snap, now=MONDAY_MORNING, industry_provider=provider)

        self.assertIn("industry_ranking", res)
        ranking = res["industry_ranking"]
        self.assertEqual(ranking["status"], EvidenceStatus.DEGRADED.value)
        self.assertEqual(ranking["coverage"], 0)
        self.assertEqual(ranking["items"], ())
        self.assertEqual(res["data_limits"]["industry"], "configured_labels_only")

    def test_insufficient_coverage_degraded(self):
        cfg = _config()
        snap = _snapshot()
        # 5 observations < min_coverage=8
        provider = FakeIndustryProvider(observations=_build_observations(5))
        res = analyze_portfolio(cfg, snap, now=MONDAY_MORNING, industry_provider=provider)

        self.assertIn("industry_ranking", res)
        ranking = res["industry_ranking"]
        self.assertEqual(ranking["status"], EvidenceStatus.DEGRADED.value)
        self.assertEqual(ranking["coverage"], 5)
        self.assertEqual(len(ranking["items"]), 5)
        self.assertEqual(res["data_limits"]["industry"], "configured_labels_only")

    def test_stale_observations_not_ready(self):
        cfg = _config()
        snap = _snapshot()
        stale_as_of = MONDAY_MORNING - timedelta(hours=100)  # > 96h max_age_hours
        provider = FakeIndustryProvider(observations=_build_observations(10, as_of=stale_as_of))
        res = analyze_portfolio(cfg, snap, now=MONDAY_MORNING, industry_provider=provider)

        self.assertIn("industry_ranking", res)
        ranking = res["industry_ranking"]
        self.assertEqual(ranking["status"], EvidenceStatus.NOT_READY.value)
        self.assertEqual(res["data_limits"]["industry"], "configured_labels_only")

    def test_normal_ready_ranking_and_consecutive_ranks(self):
        cfg = _config()
        snap = _snapshot()
        provider = FakeIndustryProvider(observations=_build_observations(10))
        res = analyze_portfolio(cfg, snap, now=MONDAY_MORNING, industry_provider=provider)

        self.assertIn("industry_ranking", res)
        ranking = res["industry_ranking"]
        self.assertEqual(ranking["status"], EvidenceStatus.READY.value)
        self.assertIsNone(ranking["reason_code"])
        self.assertEqual(ranking["coverage"], 10)
        self.assertEqual(ranking["required"], 8)
        self.assertEqual(res["data_limits"]["industry"], "available")

        items = ranking["items"]
        self.assertEqual(len(items), 10)
        ranks = [it["rank"] for it in items]
        self.assertEqual(ranks, list(range(1, 11)))
        for it in items:
            self.assertIn("code", it)
            self.assertIn("name", it)
            self.assertIn("rank", it)
            self.assertIn("score", it)
            self.assertIsInstance(it["score"], float)

    def test_to_jsonable_and_json_dumps_safe(self):
        cfg = _config()
        snap = _snapshot()
        provider = FakeIndustryProvider(observations=_build_observations(10))
        res = analyze_portfolio(cfg, snap, now=MONDAY_MORNING, industry_provider=provider)

        jsonable = to_jsonable(res)
        dumped = json.dumps(jsonable, ensure_ascii=False)
        self.assertIsInstance(dumped, str)
        restored = json.loads(dumped)
        self.assertEqual(restored["data_limits"]["industry"], "available")
        self.assertEqual(restored["industry_ranking"]["status"], "READY")
        self.assertEqual(len(restored["industry_ranking"]["items"]), 10)

    def test_default_policy_weights_sum_to_one(self):
        policy = DEFAULT_INDUSTRY_POLICY
        total = (
            policy.weight_1d
            + policy.weight_5d
            + policy.weight_20d
            + policy.weight_breadth
            + policy.weight_activity
        )
        self.assertEqual(total, Decimal("1"))
        self.assertEqual(policy.min_coverage, 8)
        self.assertEqual(policy.max_age_hours, 96)
        self.assertEqual(policy.min_score, Decimal("60"))


class MainlandMarketDateTests(unittest.TestCase):
    def test_friday_morning_rolls_to_thursday(self):
        dt = datetime(2026, 9, 18, 9, 0, tzinfo=SHANGHAI_TZ)
        self.assertEqual(_latest_closed_mainland_market_date(dt), date(2026, 9, 17))

    def test_monday_morning_rolls_to_previous_friday(self):
        dt = datetime(2026, 9, 21, 9, 0, tzinfo=SHANGHAI_TZ)
        self.assertEqual(_latest_closed_mainland_market_date(dt), date(2026, 9, 18))

    def test_monday_after_close_returns_monday(self):
        dt = datetime(2026, 9, 21, 16, 0, tzinfo=SHANGHAI_TZ)
        self.assertEqual(_latest_closed_mainland_market_date(dt), date(2026, 9, 21))

    def test_saturday_rolls_to_friday(self):
        dt = datetime(2026, 9, 19, 10, 0, tzinfo=SHANGHAI_TZ)
        self.assertEqual(_latest_closed_mainland_market_date(dt), date(2026, 9, 18))

    def test_national_day_holiday_rolls_to_september_30(self):
        # 2026-10-08 09:00 -> holiday from 10-01 to 10-07 -> last closed trading date is 2026-09-30
        dt = datetime(2026, 10, 8, 9, 0, tzinfo=SHANGHAI_TZ)
        self.assertEqual(_latest_closed_mainland_market_date(dt), date(2026, 9, 30))


class IndustryReportFormattingTests(unittest.TestCase):
    def test_report_available_branch_rendering(self):
        snap = _report_snapshot()
        obs = _build_observations(10, as_of=FRIDAY_CLOSE)
        provider = FakeIndustryProvider(observations=obs)
        analysis = analyze_portfolio(_config(), snap, now=MONDAY_MORNING, industry_provider=provider)

        report = format_portfolio_report(snap, "morning", MONDAY_MORNING, analysis=analysis)

        self.assertIn("行业排序：已接入经校验的行业数据（截至 2026-09-11，共 10 个板块参与横向比较）。", report)
        self.assertIn("  1. ", report)
        self.assertIn("行业排序说明：评分为同一截面内的横向百分位加权，仅用于相对排序，不构成买入信号。", report)
        self.assertNotIn("行业：仅使用持仓配置中的行业标签，用于暴露统计，不等同于行业景气判断。", report)

    def test_report_unavailable_branch_rendering(self):
        snap = _report_snapshot()
        analysis = analyze_portfolio(_config(), snap, now=MONDAY_MORNING, industry_provider=None)

        report = format_portfolio_report(snap, "morning", MONDAY_MORNING, analysis=analysis)

        self.assertIn("行业：仅使用持仓配置中的行业标签，用于暴露统计，不等同于行业景气判断。", report)
        self.assertIn("行业排序：未就绪；没有可比较的行业数据，不编造行业评分或资金流向。", report)
        self.assertNotIn("行业排序说明", report)

    def test_report_maximum_ten_items_rendered(self):
        snap = _report_snapshot()
        obs = _build_observations(15, as_of=FRIDAY_CLOSE)
        provider = FakeIndustryProvider(observations=obs)
        analysis = analyze_portfolio(_config(), snap, now=MONDAY_MORNING, industry_provider=provider)

        report = format_portfolio_report(snap, "morning", MONDAY_MORNING, analysis=analysis)

        self.assertIn("  1. ", report)
        self.assertIn("  10. ", report)
        self.assertNotIn("  11. ", report)

    def test_report_non_finite_score_skipped(self):
        snap = _report_snapshot()
        analysis = {
            "coverage": {"ready": 1, "total": 1, "unavailable": 0},
            "data_limits": {"industry": "available"},
            "industry_ranking": {
                "status": "READY",
                "as_of": "2026-09-11T15:00:00+08:00",
                "coverage": 2,
                "items": (
                    {"code": "BK0001", "name": "有效板块", "rank": 1, "score": 88.0},
                    {"code": "BK0002", "name": "无效板块", "rank": 2, "score": float("nan")},
                ),
            },
        }

        report = format_portfolio_report(snap, "morning", MONDAY_MORNING, analysis=analysis)

        self.assertIn("  1. 有效板块 88.0", report)
        self.assertNotIn("无效板块", report)

    def test_report_window_rendering(self):
        snap = _report_snapshot()
        t_start = datetime(2026, 9, 17, 15, 39, 32, tzinfo=SHANGHAI_TZ)
        t_end = datetime(2026, 9, 17, 15, 40, 0, tzinfo=SHANGHAI_TZ)
        analysis = {
            "coverage": {"ready": 2, "total": 2, "unavailable": 0},
            "data_limits": {"industry": "available"},
            "industry_ranking": {
                "status": "READY",
                "as_of": t_end.isoformat(),
                "as_of_start": t_start.isoformat(),
                "as_of_end": t_end.isoformat(),
                "coverage": 2,
                "items": (
                    {"code": "BK0420", "name": "半导体", "rank": 1, "score": 85.0},
                    {"code": "BK0425", "name": "互联网服务", "rank": 2, "score": 70.0},
                ),
            },
        }

        report = format_portfolio_report(snap, "morning", MONDAY_MORNING, analysis=analysis)

        self.assertIn("（截面窗口）", report)
        self.assertIn("数据时间：09-17 15:39–15:40（截面窗口）", report)
        for token in ("None", "error_code", "provider"):
            self.assertNotIn(token, report)

    def test_morning_and_global_compliance_redlines(self):
        snap = _report_snapshot()
        obs = _build_observations(10, as_of=FRIDAY_CLOSE)
        provider = FakeIndustryProvider(observations=obs)
        analysis = analyze_portfolio(_config(), snap, now=MONDAY_MORNING, industry_provider=provider)

        for stage in ("morning", "global"):
            with self.subTest(stage=stage):
                report = format_portfolio_report(snap, stage, MONDAY_MORNING, analysis=analysis)
                for forbidden in REDLINE_FORBIDDEN_TOKENS:
                    self.assertNotIn(forbidden, report, f"Forbidden token '{forbidden}' leaked in {stage} report")
                for mandatory in MANDATORY_PHRASES:
                    self.assertIn(mandatory, report, f"Mandatory phrase '{mandatory}' missing in {stage} report")


class WorkflowWiringTests(unittest.TestCase):
    def test_run_portfolio_report_wires_industry_provider(self):
        obs = _build_observations(10, as_of=FRIDAY_CLOSE)
        provider = FakeIndustryProvider(observations=obs)

        # Use dummy router
        class DummyRouter:
            def value_portfolio(self, config, include_hstech=True):
                return {
                    "600000": Valuation(
                        "600000", Decimal("10"), Decimal("0.5"), FRIDAY_CLOSE, "test", freshness="fresh"
                    )
                }

        result = run_portfolio_report(
            "morning",
            config_loader=_config,
            valuation_router=DummyRouter(),
            strategy_loader=lambda: {},
            analysis_enabled=True,
            clock=lambda: MONDAY_MORNING,
            industry_provider=provider,
        )

        self.assertEqual(len(provider.fetch_calls), 1)
        self.assertEqual(provider.fetch_calls[0]["market_date"], date(2026, 9, 11))
        self.assertIn("analysis", result)
        self.assertIn("industry_ranking", result["analysis"])
        self.assertEqual(result["analysis"]["data_limits"]["industry"], "available")

    def test_run_portfolio_report_default_none(self):
        class DummyRouter:
            def value_portfolio(self, config, include_hstech=True):
                return {
                    "600000": Valuation(
                        "600000", Decimal("10"), Decimal("0.5"), FRIDAY_CLOSE, "test", freshness="fresh"
                    )
                }

        result = run_portfolio_report(
            "morning",
            config_loader=_config,
            valuation_router=DummyRouter(),
            strategy_loader=lambda: {},
            analysis_enabled=True,
            clock=lambda: MONDAY_MORNING,
        )

        self.assertIn("analysis", result)
        self.assertNotIn("industry_ranking", result["analysis"])
        self.assertEqual(result["analysis"]["data_limits"]["industry"], "configured_labels_only")

    def test_analysis_gaps_contract(self):
        analysis_available = {"data_limits": {"industry": "available"}}
        gaps_available = _analysis_gaps(analysis_available, None, "morning")
        self.assertNotIn("industry_ranking", gaps_available)

        analysis_unavailable = {"data_limits": {"industry": "configured_labels_only"}}
        gaps_unavailable = _analysis_gaps(analysis_unavailable, None, "morning")
        self.assertIn("industry_ranking", gaps_unavailable)


if __name__ == "__main__":
    unittest.main()
