import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from app.portfolio.models import (
    AccountConfig, AccountSnapshot, HoldingConfig, HoldingSnapshot,
    PortfolioSnapshot, Valuation,
)
from app.report.portfolio import format_failure_reminder, format_portfolio_report


NOW = datetime(2026, 9, 11, 7, 0, tzinfo=timezone.utc)


def _valuation(code, freshness="fresh", price="10", change="1.25"):
    return Valuation(code, Decimal(price), Decimal(change), NOW, "fixture", freshness=freshness)


def _holding(account_id, code, name, market, mode, value, *, precision="baseline",
             valuation=None, weight="0.10", return_from_cost=None, profit=None,
             theme="core"):
    config = HoldingConfig(
        code=code, name=name, market=market, instrument_type="stock",
        valuation_mode=mode, cost_price=Decimal("9"), baseline_value=Decimal(value),
        baseline_price=Decimal("10"), sector="technology", theme=theme,
        quantity=Decimal("100") if precision == "exact" else None,
    )
    return HoldingSnapshot(
        account_id=account_id, holding=config, current_price=Decimal("10"),
        market_value=Decimal(value), account_weight=Decimal(weight),
        return_from_cost=return_from_cost, monetary_profit=profit,
        value_precision=precision, valuation=valuation,
        warnings=("集中度预警",) if code == "601888" else (),
    )


def _account(account_id, name, strategy, holdings, total):
    holdings = tuple(holdings)
    value = sum((item.market_value for item in holdings), Decimal("0"))
    return AccountSnapshot(
        account=AccountConfig(
            account_id=account_id, name=name, strategy=strategy,
            baseline_date=date(2026, 9, 11), total_assets=Decimal(total),
            cash=Decimal(total) - value,
            holdings=tuple(item.holding for item in holdings),
        ), holdings=holdings, holdings_value=value, cash=Decimal(total) - value,
        total_assets=Decimal(total), position_percent=value / Decimal(total),
    )


def _snapshot(accounts=None):
    a = _account("A", "A账户", "long_term_core", (
        _holding("A", "600000", "A股核心", "CN", "exchange", "60000",
                 valuation=_valuation("600000"), return_from_cost=Decimal("0.05")),
        _holding("A", "00700", "港股科技", "HK", "exchange", "20000",
                 valuation=_valuation("00700", price="320", change="2.5"), theme="hk_technology"),
        _holding("A", "016280", "全球医疗", "GLOBAL", "qdii_nav", "10000",
                 valuation=_valuation("016280")),
    ), "100000")
    b = _account("B", "B账户", "active_equity", (
        _holding("B", "601888", "中国中免", "CN", "exchange", "20000",
                 precision="exact", valuation=_valuation("601888", price="8", change="-3"),
                 return_from_cost=Decimal("-0.11"), profit=Decimal("-1000")),
        _holding("B", "110011", "B基金", "CN", "fund_nav", "10000",
                 valuation=_valuation("110011")),
    ), "50000")
    return PortfolioSnapshot(
        accounts=tuple(accounts or (b, a)), total_assets=Decimal("150000"),
        holdings_value=Decimal("120000"), cash=Decimal("30000"),
        position_percent=Decimal("0.8"),
    )


class PortfolioReportTests(unittest.TestCase):
    def test_frozen_trial_failure_never_suggests_retrying_or_exposes_details(self):
        report = format_failure_reminder(
            "morning", NOW, ["private-fixture-value"], allow_retry=False
        )
        self.assertIn("WAIT", report)
        self.assertIn("本时点不重跑补账", report)
        self.assertIn("保留失败记录", report)
        self.assertNotIn("再重试", report)
        self.assertNotIn("private-fixture-value", report)

    def test_legacy_failure_keeps_its_manual_retry_guidance(self):
        report = format_failure_reminder("morning", NOW, ["private-fixture-value"])
        self.assertIn("补齐后再重试", report)
        self.assertNotIn("private-fixture-value", report)

    def test_morning_and_trading_cover_all_holdings_without_top_four_truncation(self):
        holdings = tuple(
            _holding("A", f"DEMO{i}", f"合成持仓{i}", "GLOBAL" if i == 5 else "CN",
                     "qdii_nav" if i == 5 else "exchange", "100",
                     valuation=_valuation(f"DEMO{i}"))
            for i in range(6)
        )
        snapshot = _snapshot((_account("A", "A账户", "long_term_core", holdings, "1000"),))
        for kind in ("morning", "trading", "closing"):
            report = format_portfolio_report(snapshot, kind, NOW)
            for i in range(6):
                self.assertIn(f"合成持仓{i}", report)

    def test_midday_and_closing_never_label_cost_pnl_as_daily_contribution(self):
        for kind in ("midday", "closing"):
            report = format_portfolio_report(_snapshot(), kind, NOW)
            self.assertIn("晨报基线", report)
            self.assertIn("无法", report)
            self.assertNotIn("贡献最强", report)
            self.assertNotIn("贡献最弱", report)
        closing = format_portfolio_report(_snapshot(), "closing", NOW)
        self.assertIn("事前判断", closing)
        self.assertIn("无法评价", closing)
        self.assertIn("不自动调整", closing)

    def test_trading_separates_technical_score_from_complete_analysis_and_defaults_wait(self):
        report = format_portfolio_report(_snapshot(), "trading", NOW)
        for term in ("周线", "日线", "120分钟", "30分钟", "5分钟", "WAIT", "+0%", "未就绪"):
            self.assertIn(term, report)
        for term in ("87/A", "二买确认", "建议：+5%"):
            self.assertNotIn(term, report)

    def test_all_stages_are_distinct_and_have_deterministic_a_then_b_order(self):
        reports = {
            kind: format_portfolio_report(_snapshot(), kind, NOW)
            for kind in ("global", "morning", "midday", "trading", "closing")
        }
        self.assertEqual(len(set(reports.values())), 5)
        for kind, heading in {
            "global": "隔夜与全球影响", "morning": "开盘前观察", "midday": "午盘变化复核",
            "trading": "执行条件复核", "closing": "A/H股收盘复盘与恒生科技",
        }.items():
            self.assertIn(heading, reports[kind])
        morning = reports["morning"]
        self.assertLess(morning.index("A账户｜长期核心配置"), morning.index("B账户｜主动个股增强"))
        self.assertIn("全部账户｜总资产：150000.00", morning)
        self.assertIn("基准值估算", morning)
        self.assertIn("精确数量估值", morning)
        self.assertIn("中国中免：集中度预警", reports["closing"])

    def test_each_stage_preserves_its_operating_scenario(self):
        reports = {
            kind: format_portfolio_report(_snapshot(), kind, NOW)
            for kind in ("global", "morning", "midday", "trading", "closing")
        }
        required_terms = {
            "global": ("隔夜海外风险", "港股传导", "全球医疗估值"),
            "morning": ("前一交易日收盘条件", "缺口", "开盘观察清单"),
            "midday": ("晨报基线", "半日波动", "下午仍可能修正"),
            "trading": ("复核仓位", "集中度", "执行条件"),
            "closing": (
                "A 股收盘价", "事前判断", "恒生科技指数",
                "港股科技敞口", "基金净值日期", "收盘后检查",
            ),
        }
        for kind, terms in required_terms.items():
            with self.subTest(report_kind=kind):
                report = reports[kind]
                for term in terms:
                    self.assertIn(term, report)
                self.assertIn("A账户｜长期核心配置", report)
                self.assertIn("B账户｜主动个股增强", report)
                self.assertIn("全部账户｜总资产：150000.00", report)
                self.assertNotIn("立即买入", report)
                self.assertNotIn("立即卖出", report)

    def test_each_stage_has_human_action_and_manual_execution_boundary(self):
        for kind in ("global", "morning", "midday", "trading", "closing"):
            with self.subTest(report_kind=kind):
                report = format_portfolio_report(_snapshot(), kind, NOW)
                self.assertIn("人工操作提醒", report)
                self.assertIn("进入人工复核", report)
                self.assertIn("WAIT", report)
                self.assertIn("不自动下单", report)
                self.assertIn("本人确认并手动执行", report)
                self.assertNotIn("自动执行交易", report)

    def test_closing_merges_cn_attribution_and_hk_technology_at_1610(self):
        report = format_portfolio_report(
            _snapshot(), "closing", NOW,
            _valuation("HK.HSTECH", price="4123.45", change="1.37"),
        )
        self.assertIn("A股核心", report)
        self.assertIn("中国中免", report)
        self.assertIn("无法比较", report)
        self.assertNotIn("贡献最强", report)
        self.assertNotIn("贡献最弱", report)
        self.assertIn("恒生科技指数：4123.45（+1.37%）", report)
        self.assertIn("港股科技敞口", report)
        self.assertIn("00700 港股科技", report)
        self.assertIn("全球医疗", report)
        self.assertIn("B基金", report)

    def test_unknown_accounts_are_sorted_after_a_and_b(self):
        base = _snapshot()
        extra = _account("C", "C账户", "satellite", (), "1000")
        snapshot = _snapshot((extra, base.accounts[1], base.accounts[0]))
        report = format_portfolio_report(snapshot, "morning", NOW)
        self.assertLess(report.index("A账户"), report.index("B账户"))
        self.assertLess(report.index("B账户"), report.index("C账户"))

    def test_unavailable_analysis_hides_raw_provider_errors_from_user_report(self):
        raw_reason = (
            "历史K线获取失败：MarketDataUnavailable: providers failed: "
            "AkShareProvider: HTTPSConnectionPool(host='push2his.eastmoney.com') "
            "ProxyError; url=/api/qt/stock/kline/get?fields1=f1%2Cf2"
        )
        analysis = {
            "items": ({
                "account_id": "A",
                "code": "600000",
                "status": "unavailable",
                "reason": raw_reason,
                "decision": None,
            },),
            "benchmark": None,
            "coverage": {"ready": 0, "total": 1, "unavailable": 1},
        }

        report = format_portfolio_report(
            _snapshot(), "closing", NOW, analysis=analysis
        )

        self.assertIn(
            "技术/缠论：600000 未分析（历史行情暂不可用，已降级为持仓与风险分析）。",
            report,
        )
        self.assertNotIn("HTTPSConnectionPool", report)
        self.assertNotIn("push2his.eastmoney.com", report)
        self.assertNotIn("ProxyError", report)
        self.assertEqual(analysis["items"][0]["reason"], raw_reason)

    def test_rejects_unknown_report_kind(self):
        with self.assertRaisesRegex(ValueError, "unsupported report_kind"):
            format_portfolio_report(_snapshot(), "unknown", NOW)

    def test_unsafe_holding_and_benchmark_data_never_emit_immediate_trade_language(self):
        for freshness in ("stale", "lagged", "failed"):
            with self.subTest(freshness=freshness):
                holding = _holding("B", "601888", "中国中免", "CN", "exchange", "20000",
                                   precision="exact", valuation=_valuation("601888", freshness))
                base = _snapshot()
                snapshot = _snapshot((_account("B", "B账户", "active_equity", (holding,), "50000"),
                                      base.accounts[0]))
                report = format_portfolio_report(snapshot, "trading", NOW)
                self.assertNotIn("立即买入", report)
                self.assertNotIn("立即卖出", report)
                self.assertIn("数据已陈旧", report)
        for freshness in ("stale", "lagged", "failed"):
            with self.subTest(benchmark_freshness=freshness):
                report = format_portfolio_report(
                    _snapshot(), "closing", NOW,
                    _valuation("HK.HSTECH", freshness, price="4123.45", change="1.37"),
                )
                self.assertNotIn("立即买入", report)
                self.assertNotIn("立即卖出", report)
                if freshness == "failed":
                    self.assertNotIn("恒生科技指数：4123.45", report)
                    self.assertIn("未返回有效点位", report)


if __name__ == "__main__":
    unittest.main()
