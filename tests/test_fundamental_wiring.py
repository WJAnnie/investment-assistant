"""Offline unit and integration tests for fundamental & valuation evidence wiring.

Covers:
1. analyze_portfolio default path byte-for-byte equivalence (provider=None vs omitted)
2. _is_supported_a_share accepted codes (6/0/3/4/8/92)
3. _is_supported_a_share rejection of ETFs and funds
4. _is_supported_a_share rejection of non-CN markets
5. _is_supported_a_share rejection of non-stock instrument types
6. _is_supported_a_share rejection of invalid code formats (length, characters, non-str)
7. Only eligible A-share stocks are fetched; ETF/fund/HK/GLOBAL are skipped; tz-aware cutoff verified
8. Provider exception fail-closed (data_limits unavailable, item fundamental not_available)
9. evaluate_fundamental exception fail-closed (status not_available)
10. evaluate_valuation exception fail-closed (status not_available)
11. Single READY, other failed -> eligible > ready -> data_limits NOT available
12. All READY -> data_limits available, ready == eligible
13. Criterion passed count distinction from data readiness count
14. Item fundamental structure, keys, and primitive number serialization
15. Non-eligible holdings do not have fundamental key
16. Duplicate holdings across multiple accounts reuse cached fetch
17. Report morning available branch rendering (ready/eligible, criterion_passed, no forbidden tokens)
18. Report unavailable branch preserving legacy text without regression
19. Report zero fallback prevents None rendering
20. to_jsonable and json.dumps serialization safety
21. Static assertions: no pandas or app.market.fundamentals imports in target modules
22. Default policy constants match specification
23. run_portfolio_report passthrough of fundamental_provider
24. run_portfolio_report default fundamental_provider is None
25. _analysis_gaps fundamentals gap toggled on data_limits
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
import inspect
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.analysis.fundamental import (
    FundamentalPolicy,
    StockFundamentalObservation,
    StockValuationObservation,
    ValuationPolicy,
)
from app.domain.evidence import EvidenceStatus
from app.portfolio.analysis import (
    DEFAULT_FUNDAMENTAL_POLICY,
    DEFAULT_VALUATION_POLICY,
    _is_supported_a_share,
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
EVAL_TIME = datetime(2026, 9, 14, 9, 0, tzinfo=SHANGHAI_TZ)

FORBIDDEN_REPORT_TOKENS = (
    "manual_replay",
    "available_unverified",
    "RECENT",
    "STALE",
    "error_code",
    "provider",
    "None",
    "completed",
    "not_collected",
    "full_analysis_ready",
    "data_limits",
    "立即买入",
    "立即卖出",
    "自动执行交易",
)


@dataclass(frozen=True)
class FakeStockEvidence:
    fundamental: StockFundamentalObservation | None = None
    valuation: StockValuationObservation | None = None


class FakeStockEvidenceProvider:
    def __init__(self, mapping=None, error=None):
        self.mapping = dict(mapping) if mapping is not None else {}
        self.error = error
        self.fetch_calls = []

    def fetch(self, symbol, cutoff):
        self.fetch_calls.append((symbol, cutoff))
        if self.error is not None:
            raise self.error
        return self.mapping.get(symbol)


def make_fundamental_obs(
    symbol="600977",
    cutoff=None,
    roe=Decimal("12"),
    eps=Decimal("0.5"),
    rev_growth=Decimal("10"),
    prof_growth=Decimal("8"),
    age_days=60,
):
    now = cutoff or EVAL_TIME
    return StockFundamentalObservation(
        symbol=symbol,
        report_period=date(2025, 12, 31),
        published_at=now - timedelta(days=age_days),
        fetched_at=now - timedelta(days=1),
        source="cninfo_financials",
        revenue_growth_pct=rev_growth,
        net_profit_growth_pct=prof_growth,
        roe_pct=roe,
        eps=eps,
    )


def make_valuation_obs(
    symbol="600977",
    cutoff=None,
    pe=Decimal("25"),
    pb=Decimal("3"),
    ps=Decimal("4"),
    age_hours=18,
):
    now = cutoff or EVAL_TIME
    return StockValuationObservation(
        symbol=symbol,
        as_of=now - timedelta(hours=age_hours),
        fetched_at=now - timedelta(hours=1),
        source="em_valuation",
        pe_ttm=pe,
        pb=pb,
        ps=ps,
    )


def _make_holding_config(
    code,
    name="标的",
    market="CN",
    instrument_type="stock",
    valuation_mode="exchange",
    cost_price=Decimal("10.0"),
    quantity=Decimal("1000"),
    baseline_value=Decimal("10000.0"),
):
    return HoldingConfig(
        code=code,
        name=name,
        market=market,
        instrument_type=instrument_type,
        valuation_mode=valuation_mode,
        cost_price=cost_price,
        baseline_value=baseline_value,
        sector="TMT",
        theme="technology",
        quantity=quantity,
    )


def _make_portfolio_and_snapshot(holdings_a, holdings_b=None):
    acc_a = AccountConfig(
        account_id="A",
        name="主账户",
        strategy="long_term_core",
        baseline_date=date(2026, 1, 1),
        total_assets=Decimal("100000.0"),
        cash=Decimal("20000.0"),
        holdings=tuple(holdings_a),
    )
    accounts = [acc_a]
    if holdings_b is not None:
        acc_b = AccountConfig(
            account_id="B",
            name="增强账户",
            strategy="active_equity",
            baseline_date=date(2026, 1, 1),
            total_assets=Decimal("50000.0"),
            cash=Decimal("10000.0"),
            holdings=tuple(holdings_b),
        )
        accounts.append(acc_b)

    cfg = PortfolioConfig(schema_version=2, accounts=tuple(accounts))
    valuations = {}
    for acc in accounts:
        for h in acc.holdings:
            valuations[h.code] = Valuation(
                code=h.code,
                price=Decimal("12.0"),
                change_percent=Decimal("0.0"),
                as_of=EVAL_TIME,
                source="test",
                freshness="fresh",
            )
    snapshot = build_portfolio_snapshot(cfg, valuations)
    return cfg, snapshot


class TestFundamentalWiring(unittest.TestCase):
    def test_default_policies(self):
        self.assertEqual(DEFAULT_FUNDAMENTAL_POLICY.min_roe, Decimal("8"))
        self.assertEqual(DEFAULT_FUNDAMENTAL_POLICY.min_eps, Decimal("0"))
        self.assertEqual(DEFAULT_FUNDAMENTAL_POLICY.min_revenue_growth, Decimal("0"))
        self.assertEqual(DEFAULT_FUNDAMENTAL_POLICY.min_profit_growth, Decimal("0"))
        self.assertEqual(DEFAULT_FUNDAMENTAL_POLICY.max_age_days, 400)

        self.assertEqual(DEFAULT_VALUATION_POLICY.max_pe, Decimal("60"))
        self.assertEqual(DEFAULT_VALUATION_POLICY.max_pb, Decimal("10"))
        self.assertEqual(DEFAULT_VALUATION_POLICY.max_ps, Decimal("30"))
        self.assertEqual(DEFAULT_VALUATION_POLICY.max_age_days, 30)

    def test_is_supported_a_share_valid(self):
        valid_codes = [
            "600977",  # SH Main
            "601288",  # SH Main
            "002415",  # SZ Main
            "300750",  # ChiNext
            "430047",  # BSE / NEEQ
            "830799",  # BSE
            "920001",  # BSE 92-prefix
        ]
        for code in valid_codes:
            with self.subTest(code=code):
                self.assertTrue(_is_supported_a_share(code, "CN", "stock"))

    def test_is_supported_a_share_reject_etf_and_fund(self):
        rejected = [
            ("159500", "CN", "etf"),
            ("513330", "CN", "etf"),
            ("561160", "CN", "etf"),
            ("016496", "CN", "fund"),
            ("017633", "CN", "fund"),
            ("161907", "CN", "fund"),
            ("159500", "CN", "stock"),
            ("513330", "CN", "stock"),
            ("561160", "CN", "stock"),
        ]
        for code, mkt, itype in rejected:
            with self.subTest(code=code, mkt=mkt, itype=itype):
                self.assertFalse(_is_supported_a_share(code, mkt, itype))

    def test_is_supported_a_share_reject_non_cn_market(self):
        self.assertFalse(_is_supported_a_share("00700", "HK", "stock"))
        self.assertFalse(_is_supported_a_share("600977", "HK", "stock"))
        self.assertFalse(_is_supported_a_share("600977", "US", "stock"))
        self.assertFalse(_is_supported_a_share("600977", "GLOBAL", "stock"))
        self.assertFalse(_is_supported_a_share("600977", "", "stock"))
        self.assertFalse(_is_supported_a_share("600977", None, "stock"))

    def test_is_supported_a_share_reject_non_stock_instrument(self):
        self.assertFalse(_is_supported_a_share("600977", "CN", "etf"))
        self.assertFalse(_is_supported_a_share("600977", "CN", "fund"))
        self.assertFalse(_is_supported_a_share("600977", "CN", "bond"))
        self.assertFalse(_is_supported_a_share("600977", "CN", "index"))
        self.assertFalse(_is_supported_a_share("600977", "CN", None))

    def test_is_supported_a_share_reject_invalid_code_formats(self):
        invalids = [
            600977,
            None,
            "",
            "60097",
            "6009770",
            "60097 ",
            " 00977",
            "60097A",
            "900001",
            "100001",
            "200001",
            "700001",
            "６００９７７",
        ]
        for val in invalids:
            with self.subTest(val=val):
                self.assertFalse(_is_supported_a_share(val, "CN", "stock"))

    def test_default_path_byte_for_byte_equivalence(self):
        h1 = _make_holding_config("600977")
        h2 = _make_holding_config("159500", instrument_type="etf")
        cfg, snapshot = _make_portfolio_and_snapshot([h1, h2])

        res_default = analyze_portfolio(cfg, snapshot, now=EVAL_TIME)
        res_explicit_none = analyze_portfolio(cfg, snapshot, now=EVAL_TIME, fundamental_provider=None)

        self.assertEqual(res_default["data_limits"]["fundamental"], "unavailable")
        self.assertEqual(res_explicit_none["data_limits"]["fundamental"], "unavailable")
        self.assertNotIn("fundamental", res_default)
        self.assertNotIn("fundamental", res_explicit_none)

        for it in res_default["items"]:
            self.assertNotIn("fundamental", it)
        for it in res_explicit_none["items"]:
            self.assertNotIn("fundamental", it)

        self.assertEqual(res_default, res_explicit_none)

    def test_only_eligible_a_shares_fetched(self):
        h_stock_cn = _make_holding_config("600977", market="CN", instrument_type="stock")
        h_etf_cn = _make_holding_config("159500", market="CN", instrument_type="etf")
        h_fund_cn = _make_holding_config("016496", market="CN", instrument_type="fund", valuation_mode="fund_nav")
        h_stock_hk = _make_holding_config("00700", market="HK", instrument_type="stock")
        cfg, snapshot = _make_portfolio_and_snapshot([h_stock_cn, h_etf_cn, h_fund_cn, h_stock_hk])

        provider = FakeStockEvidenceProvider(
            mapping={
                "600977": FakeStockEvidence(
                    fundamental=make_fundamental_obs("600977", EVAL_TIME),
                    valuation=make_valuation_obs("600977", EVAL_TIME),
                ),
            }
        )

        res = analyze_portfolio(cfg, snapshot, now=EVAL_TIME, fundamental_provider=provider)

        self.assertEqual(len(provider.fetch_calls), 1)
        symbol, cutoff = provider.fetch_calls[0]
        self.assertEqual(symbol, "600977")
        self.assertEqual(cutoff.tzinfo, SHANGHAI_TZ)
        self.assertEqual(cutoff, EVAL_TIME)

        items_by_code = {it["code"]: it for it in res["items"]}
        self.assertIn("fundamental", items_by_code["600977"])
        self.assertNotIn("fundamental", items_by_code["159500"])
        self.assertNotIn("fundamental", items_by_code["016496"])
        self.assertNotIn("fundamental", items_by_code["00700"])

    def test_provider_exception_fail_closed(self):
        h = _make_holding_config("600977")
        cfg, snapshot = _make_portfolio_and_snapshot([h])

        provider = FakeStockEvidenceProvider(error=RuntimeError("connection refused"))

        res = analyze_portfolio(cfg, snapshot, now=EVAL_TIME, fundamental_provider=provider)

        self.assertEqual(res["data_limits"]["fundamental"], "unavailable")
        self.assertEqual(
            res["fundamental"],
            {
                "status": "not_available",
                "eligible": 1,
                "ready": 0,
                "criterion_passed": 0,
            },
        )
        item = res["items"][0]
        self.assertIn("fundamental", item)
        fund = item["fundamental"]
        self.assertEqual(fund["status"], "not_available")
        self.assertIsNone(fund["reason_code"])
        self.assertFalse(fund["complete"])
        self.assertFalse(fund["criterion_passed"])
        self.assertIsNone(fund["roe_pct"])
        self.assertIsNone(fund["pe_ttm"])

    def test_evaluate_fundamental_exception_fail_closed(self):
        h = _make_holding_config("600977")
        cfg, snapshot = _make_portfolio_and_snapshot([h])
        provider = FakeStockEvidenceProvider(
            mapping={
                "600977": FakeStockEvidence(
                    fundamental=make_fundamental_obs("600977", EVAL_TIME),
                    valuation=make_valuation_obs("600977", EVAL_TIME),
                ),
            }
        )

        with patch("app.portfolio.analysis.evaluate_fundamental", side_effect=ValueError("bad fundamental eval")):
            res = analyze_portfolio(cfg, snapshot, now=EVAL_TIME, fundamental_provider=provider)

        self.assertEqual(res["data_limits"]["fundamental"], "unavailable")
        self.assertEqual(res["fundamental"]["status"], "not_available")
        self.assertEqual(res["fundamental"]["ready"], 0)
        self.assertEqual(res["items"][0]["fundamental"]["status"], "not_available")

    def test_evaluate_valuation_exception_fail_closed(self):
        h = _make_holding_config("600977")
        cfg, snapshot = _make_portfolio_and_snapshot([h])
        provider = FakeStockEvidenceProvider(
            mapping={
                "600977": FakeStockEvidence(
                    fundamental=make_fundamental_obs("600977", EVAL_TIME),
                    valuation=make_valuation_obs("600977", EVAL_TIME),
                ),
            }
        )

        with patch("app.portfolio.analysis.evaluate_valuation", side_effect=TypeError("bad valuation eval")):
            res = analyze_portfolio(cfg, snapshot, now=EVAL_TIME, fundamental_provider=provider)

        self.assertEqual(res["data_limits"]["fundamental"], "unavailable")
        self.assertEqual(res["fundamental"]["status"], "not_available")
        self.assertEqual(res["fundamental"]["ready"], 0)
        self.assertEqual(res["items"][0]["fundamental"]["status"], "not_available")

    def test_single_ready_other_failed_eligible_greater_than_ready(self):
        h1 = _make_holding_config("600977")
        h2 = _make_holding_config("601288")
        cfg, snapshot = _make_portfolio_and_snapshot([h1, h2])

        provider = FakeStockEvidenceProvider(
            mapping={
                "600977": FakeStockEvidence(
                    fundamental=make_fundamental_obs("600977", EVAL_TIME),
                    valuation=make_valuation_obs("600977", EVAL_TIME),
                ),
                "601288": FakeStockEvidence(
                    fundamental=make_fundamental_obs("601288", EVAL_TIME),
                    valuation=make_valuation_obs("601288", EVAL_TIME, age_hours=24 * 100),
                ),
            }
        )

        res = analyze_portfolio(cfg, snapshot, now=EVAL_TIME, fundamental_provider=provider)

        self.assertEqual(res["fundamental"]["eligible"], 2)
        self.assertEqual(res["fundamental"]["ready"], 1)
        self.assertNotEqual(res["data_limits"]["fundamental"], "available")
        self.assertEqual(res["data_limits"]["fundamental"], "unavailable")
        self.assertEqual(res["fundamental"]["status"], "not_available")

    def test_all_ready_available(self):
        h1 = _make_holding_config("600977")
        h2 = _make_holding_config("601288")
        cfg, snapshot = _make_portfolio_and_snapshot([h1, h2])

        provider = FakeStockEvidenceProvider(
            mapping={
                "600977": FakeStockEvidence(
                    fundamental=make_fundamental_obs("600977", EVAL_TIME),
                    valuation=make_valuation_obs("600977", EVAL_TIME),
                ),
                "601288": FakeStockEvidence(
                    fundamental=make_fundamental_obs("601288", EVAL_TIME),
                    valuation=make_valuation_obs("601288", EVAL_TIME),
                ),
            }
        )

        res = analyze_portfolio(cfg, snapshot, now=EVAL_TIME, fundamental_provider=provider)

        self.assertEqual(res["fundamental"]["eligible"], 2)
        self.assertEqual(res["fundamental"]["ready"], 2)
        self.assertEqual(res["fundamental"]["status"], "available")
        self.assertEqual(res["data_limits"]["fundamental"], "available")

    def test_criterion_passed_vs_ready(self):
        h1 = _make_holding_config("600977")
        h2 = _make_holding_config("601288")
        cfg, snapshot = _make_portfolio_and_snapshot([h1, h2])

        provider = FakeStockEvidenceProvider(
            mapping={
                "600977": FakeStockEvidence(
                    fundamental=make_fundamental_obs("600977", EVAL_TIME, roe=Decimal("15")),
                    valuation=make_valuation_obs("600977", EVAL_TIME, pe=Decimal("20")),
                ),
                "601288": FakeStockEvidence(
                    fundamental=make_fundamental_obs("601288", EVAL_TIME, roe=Decimal("15")),
                    valuation=make_valuation_obs("601288", EVAL_TIME, pe=Decimal("80")),
                ),
            }
        )

        res = analyze_portfolio(cfg, snapshot, now=EVAL_TIME, fundamental_provider=provider)

        self.assertEqual(res["fundamental"]["ready"], 2)
        self.assertEqual(res["fundamental"]["eligible"], 2)
        self.assertEqual(res["data_limits"]["fundamental"], "available")
        self.assertEqual(res["fundamental"]["criterion_passed"], 1)

        items_by_code = {it["code"]: it for it in res["items"]}
        self.assertTrue(items_by_code["600977"]["fundamental"]["criterion_passed"])
        self.assertFalse(items_by_code["601288"]["fundamental"]["criterion_passed"])

    def test_item_fundamental_structure_and_primitive_types(self):
        h = _make_holding_config("600977")
        cfg, snapshot = _make_portfolio_and_snapshot([h])
        provider = FakeStockEvidenceProvider(
            mapping={
                "600977": FakeStockEvidence(
                    fundamental=make_fundamental_obs("600977", EVAL_TIME, roe=Decimal("12.5")),
                    valuation=make_valuation_obs("600977", EVAL_TIME, pe=Decimal("18.2")),
                ),
            }
        )

        res = analyze_portfolio(cfg, snapshot, now=EVAL_TIME, fundamental_provider=provider)
        fund = res["items"][0]["fundamental"]

        expected_keys = {
            "status",
            "reason_code",
            "criterion_passed",
            "complete",
            "roe_pct",
            "revenue_growth_pct",
            "net_profit_growth_pct",
            "eps",
            "pe_ttm",
            "pb",
            "ps",
        }
        self.assertEqual(set(fund.keys()), expected_keys)
        self.assertEqual(fund["status"], EvidenceStatus.READY.value)
        self.assertIsNone(fund["reason_code"])
        self.assertTrue(fund["complete"])
        self.assertTrue(fund["criterion_passed"])
        self.assertEqual(fund["roe_pct"], Decimal("12.5"))
        self.assertEqual(fund["pe_ttm"], Decimal("18.2"))

    def test_duplicate_holding_across_accounts_cached(self):
        h_a = _make_holding_config("600977")
        h_b = _make_holding_config("600977")
        cfg, snapshot = _make_portfolio_and_snapshot([h_a], [h_b])

        provider = FakeStockEvidenceProvider(
            mapping={
                "600977": FakeStockEvidence(
                    fundamental=make_fundamental_obs("600977", EVAL_TIME),
                    valuation=make_valuation_obs("600977", EVAL_TIME),
                ),
            }
        )

        res = analyze_portfolio(cfg, snapshot, now=EVAL_TIME, fundamental_provider=provider)

        self.assertEqual(len(provider.fetch_calls), 1)
        self.assertEqual(res["fundamental"]["eligible"], 2)
        self.assertEqual(res["fundamental"]["ready"], 2)
        self.assertEqual(len(res["items"]), 2)
        for it in res["items"]:
            self.assertIn("fundamental", it)
            self.assertEqual(it["fundamental"]["status"], EvidenceStatus.READY.value)

    def test_no_eligible_a_shares_limits_unavailable(self):
        h_etf = _make_holding_config("159500", instrument_type="etf")
        h_hk = _make_holding_config("00700", market="HK", instrument_type="stock")
        cfg, snapshot = _make_portfolio_and_snapshot([h_etf, h_hk])

        provider = FakeStockEvidenceProvider()
        res = analyze_portfolio(cfg, snapshot, now=EVAL_TIME, fundamental_provider=provider)

        self.assertEqual(provider.fetch_calls, [])
        self.assertEqual(res["fundamental"]["eligible"], 0)
        self.assertEqual(res["fundamental"]["ready"], 0)
        self.assertEqual(res["fundamental"]["status"], "not_available")
        self.assertEqual(res["data_limits"]["fundamental"], "unavailable")

    def test_report_morning_available_rendering(self):
        h = _make_holding_config("600977")
        cfg, snapshot = _make_portfolio_and_snapshot([h])
        provider = FakeStockEvidenceProvider(
            mapping={
                "600977": FakeStockEvidence(
                    fundamental=make_fundamental_obs("600977", EVAL_TIME, roe=Decimal("15")),
                    valuation=make_valuation_obs("600977", EVAL_TIME, pe=Decimal("20")),
                ),
            }
        )

        analysis = analyze_portfolio(cfg, snapshot, now=EVAL_TIME, fundamental_provider=provider)
        report = format_portfolio_report(snapshot, "morning", EVAL_TIME, analysis=analysis)

        expected_snippet = "基本面：已接入经校验的财报/估值数据源（1/1 个 A 股持仓通过校验，1 个满足基本面标准）。"
        self.assertIn(expected_snippet, report)
        self.assertNotIn("基本面：未接入经校验的财报/估值数据源", report)

        for forbidden in FORBIDDEN_REPORT_TOKENS:
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, report)

    def test_report_unavailable_rendering(self):
        h = _make_holding_config("600977")
        cfg, snapshot = _make_portfolio_and_snapshot([h])

        analysis = analyze_portfolio(cfg, snapshot, now=EVAL_TIME)
        report = format_portfolio_report(snapshot, "morning", EVAL_TIME, analysis=analysis)

        expected_snippet = "基本面：未接入经校验的财报/估值数据源，不生成基本面结论。"
        self.assertIn(expected_snippet, report)
        self.assertNotIn("已接入经校验的财报", report)

    def test_report_zero_fallback_no_none(self):
        h = _make_holding_config("600977")
        _, snapshot = _make_portfolio_and_snapshot([h])

        mock_analysis = {
            "data_limits": {"fundamental": "available"},
            "fundamental": {},
            "coverage": {"ready": 1, "total": 1, "unavailable": 0},
            "items": (),
        }
        report = format_portfolio_report(snapshot, "morning", EVAL_TIME, analysis=mock_analysis)
        self.assertIn("0/0 个 A 股持仓通过校验，0 个满足基本面标准", report)
        self.assertNotIn("None", report)

    def test_to_jsonable_and_json_dumps_safety(self):
        h = _make_holding_config("600977")
        cfg, snapshot = _make_portfolio_and_snapshot([h])
        provider = FakeStockEvidenceProvider(
            mapping={
                "600977": FakeStockEvidence(
                    fundamental=make_fundamental_obs("600977", EVAL_TIME),
                    valuation=make_valuation_obs("600977", EVAL_TIME),
                ),
            }
        )

        analysis = analyze_portfolio(cfg, snapshot, now=EVAL_TIME, fundamental_provider=provider)
        jsonable = to_jsonable(analysis)
        encoded = json.dumps(jsonable, ensure_ascii=False)
        self.assertIsInstance(encoded, str)
        restored = json.loads(encoded)
        self.assertEqual(restored["fundamental"]["status"], "available")
        self.assertEqual(restored["data_limits"]["fundamental"], "available")

    def test_static_assertions_no_pandas_no_market_fundamentals(self):
        repo_root = Path(__file__).resolve().parents[1]
        target_files = [
            repo_root / "app" / "portfolio" / "analysis.py",
            repo_root / "app" / "report" / "portfolio.py",
            repo_root / "app" / "workflow" / "portfolio.py",
        ]
        for path in target_files:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("pandas", text, f"{path.name} contains 'pandas'")
            self.assertNotIn("app.market.fundamentals", text, f"{path.name} contains 'app.market.fundamentals'")

    def test_analysis_gaps_with_and_without_available(self):
        analysis_unavail = {"data_limits": {"fundamental": "unavailable"}}
        gaps = _analysis_gaps(analysis_unavail, None, "trading")
        self.assertIn("fundamentals", gaps)

        analysis_avail = {"data_limits": {"fundamental": "available"}}
        gaps_avail = _analysis_gaps(analysis_avail, None, "trading")
        self.assertNotIn("fundamentals", gaps_avail)

    def test_run_portfolio_report_passthrough(self):
        h = _make_holding_config("600977")
        cfg, _ = _make_portfolio_and_snapshot([h])

        provider = FakeStockEvidenceProvider(
            mapping={
                "600977": FakeStockEvidence(
                    fundamental=make_fundamental_obs("600977", EVAL_TIME),
                    valuation=make_valuation_obs("600977", EVAL_TIME),
                ),
            }
        )

        class MinimalRouter:
            def value_portfolio(self, config, include_hstech=True):
                return {
                    "600977": Valuation(
                        code="600977",
                        price=Decimal("12.0"),
                        change_percent=Decimal("0.0"),
                        as_of=EVAL_TIME,
                        source="test",
                        freshness="fresh",
                    ),
                    "HK.HSTECH": Valuation(
                        code="HK.HSTECH",
                        price=Decimal("4000.0"),
                        change_percent=Decimal("0.0"),
                        as_of=EVAL_TIME,
                        source="test",
                        freshness="fresh",
                    ),
                }

        result = run_portfolio_report(
            "morning",
            config_loader=lambda: cfg,
            valuation_router=MinimalRouter(),
            now=EVAL_TIME,
            fundamental_provider=provider,
        )

        self.assertIn(result["status"], {"completed", "partial"})
        self.assertIsNotNone(result["analysis"])
        self.assertEqual(result["analysis"]["data_limits"]["fundamental"], "available")
        self.assertIn("已接入经校验的财报/估值数据源", result["report"])

    def test_run_portfolio_report_default_fundamental_provider_none(self):
        sig = inspect.signature(run_portfolio_report)
        self.assertIn("fundamental_provider", sig.parameters)
        self.assertIsNone(sig.parameters["fundamental_provider"].default)


if __name__ == "__main__":
    unittest.main()
