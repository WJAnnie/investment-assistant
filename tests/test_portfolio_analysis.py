import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.chan.models import KLine
from app.portfolio.analysis import analyze_portfolio
from app.portfolio.calculator import build_portfolio_snapshot
from app.portfolio.models import AccountConfig, HoldingConfig, PortfolioConfig
from app.report.portfolio import format_portfolio_report


NOW = datetime(2026, 9, 11, 7, 30, tzinfo=timezone.utc)


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


def _lines(count=35):
    return [
        KLine(
            (date(2026, 7, 1) + timedelta(days=index)).isoformat(),
            100 + index,
            101 + index,
            99 + index,
            100 + index,
            1000,
        )
        for index in range(count)
    ]


class PortfolioAnalysisTests(unittest.TestCase):
    def _analyze_lines(self, lines, *, now=NOW, market="CN", period="daily"):
        config = _config()
        account = config.accounts[0]
        holding = replace(account.holdings[0], market=market)
        config = replace(config, accounts=(replace(account, holdings=(holding,)),))
        return analyze_portfolio(
            config, build_portfolio_snapshot(config, {}),
            history_loader=lambda _target, **_kwargs: list(lines),
            now=now, history_period=period,
        )

    def test_sufficient_history_runs_phase_two_to_four_chain(self):
        config = _config()
        snapshot = build_portfolio_snapshot(config, {})
        requests = []

        def loader(target, **kwargs):
            requests.append((getattr(target, "code", target), kwargs))
            return _lines()

        result = analyze_portfolio(
            config,
            snapshot,
            history_loader=loader,
            now=NOW,
            min_history_bars=30,
        )

        item = result["items"][0]
        self.assertEqual(item["status"], "ready")
        self.assertEqual(result["coverage"], {"ready": 1, "total": 1, "unavailable": 0})
        self.assertIn("score", item["technical"])
        self.assertIn("macd_hist", item["technical"])
        self.assertIn("rsi", item["technical"])
        self.assertIn("kdj", item["technical"])
        self.assertIn("boll", item["technical"])
        self.assertEqual(
            set(item["chan"]),
            {"fractals", "strokes", "segments", "zhongshu", "divergence"},
        )
        self.assertIn(item["signal"], {"WAIT", "FIRST_BUY", "CLASS_SECOND_BUY", "SECOND_BUY", "THIRD_BUY", "SELL_RISK"})
        self.assertEqual(requests[0][0], "600000")
        self.assertEqual(requests[0][1]["period"], "daily")

    def test_incomplete_analysis_cannot_promote_internal_candidate_to_trade_advice(self):
        config = _config()
        snapshot = build_portfolio_snapshot(config, {})
        analysis = analyze_portfolio(
            config,
            snapshot,
            history_loader=lambda _target, **_kwargs: _lines(),
            now=NOW,
            min_history_bars=30,
        )
        analysis["items"][0]["decision"] = {"action": "BUY"}

        report = format_portfolio_report(snapshot, "closing", NOW, analysis=analysis)

        self.assertIn("决策 WAIT（完整证据未就绪，待本人复核）", report)
        self.assertIn("非综合评分", report)
        self.assertNotIn("决策 BUY", report)

    def test_history_failure_is_explicit_and_does_not_block_report(self):
        config = _config()
        snapshot = build_portfolio_snapshot(config, {})

        def loader(_target, **_kwargs):
            raise RuntimeError("provider offline")

        analysis = analyze_portfolio(
            config,
            snapshot,
            history_loader=loader,
            now=NOW,
        )
        item = analysis["items"][0]
        self.assertEqual(item["status"], "unavailable")
        self.assertIn("provider offline", item["reason"])
        report = format_portfolio_report(snapshot, "trading", NOW, analysis=analysis)
        self.assertIn("未分析", report)
        self.assertIn("基本面：未接入", report)
        self.assertNotIn("立即买入", report)
        self.assertNotIn("立即卖出", report)

    def test_current_daily_bar_is_excluded_until_market_close(self):
        config = _config()
        snapshot = build_portfolio_snapshot(config, {})
        lines = [
            KLine(f"2026-08-{day:02d}", 100 + day, 101 + day, 99 + day, 100 + day, 1000)
            for day in range(1, 31)
        ]
        lines.append(KLine("2026-09-11", 140, 141, 90, 91, 500))

        analysis = analyze_portfolio(
            config,
            snapshot,
            history_loader=lambda _target, **_kwargs: list(lines),
            now=datetime(2026, 9, 11, 6, 30, tzinfo=timezone.utc),
            min_history_bars=30,
        )

        item = analysis["items"][0]
        self.assertEqual(item["bars"], 30)
        self.assertEqual(item["as_of"], "2026-08-30")
        self.assertEqual(item["bar_status"], "completed_only")
        self.assertEqual(item["excluded_incomplete_bars"], 1)
        self.assertEqual(analysis["data_cutoff"], "2026-09-11T14:30:00+08:00")

    def test_current_daily_bar_is_available_after_market_close(self):
        config = _config()
        snapshot = build_portfolio_snapshot(config, {})
        lines = [
            KLine(f"2026-08-{day:02d}", 100 + day, 101 + day, 99 + day, 100 + day, 1000)
            for day in range(1, 31)
        ]
        lines.append(KLine("2026-09-11", 140, 141, 139, 140, 500))

        analysis = analyze_portfolio(
            config,
            snapshot,
            history_loader=lambda _target, **_kwargs: list(lines),
            now=datetime(2026, 9, 11, 8, 10, tzinfo=timezone.utc),
            min_history_bars=30,
        )

        item = analysis["items"][0]
        self.assertEqual(item["bars"], 31)
        self.assertEqual(item["as_of"], "2026-09-11")
        self.assertEqual(item["excluded_incomplete_bars"], 0)

    def test_unverified_non_daily_periods_are_unavailable(self):
        for period in ("1m", "60m", "weekly", "unknown", None):
            with self.subTest(period=period):
                result = self._analyze_lines(_lines(), period=period)
                self.assertEqual(result["items"][0]["status"], "unavailable")
                self.assertIsNone(result["items"][0]["decision"])
                self.assertEqual(result["coverage"]["ready"], 0)

    def test_invalid_daily_dates_fail_closed(self):
        for invalid in (None, "not-a-date", "2026-13-01", "2026-08-04junk", "2026-08-04T00:00:00"):
            with self.subTest(invalid=invalid):
                lines = _lines()
                lines[-1] = replace(lines[-1], time=invalid)
                result = self._analyze_lines(lines)
                self.assertEqual(result["items"][0]["status"], "unavailable")

    def test_duplicate_and_out_of_order_daily_dates_fail_closed(self):
        lines = _lines()
        for bad_lines in (lines + [lines[-1]], list(reversed(lines))):
            with self.subTest(last_date=bad_lines[-1].time):
                result = self._analyze_lines(bad_lines)
                self.assertEqual(result["items"][0]["status"], "unavailable")

    def test_nonfinite_boolean_and_nonpositive_ohlc_fail_closed(self):
        for field in ("open", "high", "low", "close"):
            for invalid in (True, 0, -1, float("nan"), float("inf"), Decimal("sNaN"), None):
                with self.subTest(field=field, invalid=invalid):
                    lines = _lines()
                    lines[-1] = replace(lines[-1], **{field: invalid})
                    result = self._analyze_lines(lines)
                    self.assertEqual(result["items"][0]["status"], "unavailable")

    def test_open_and_close_outside_high_low_fail_closed(self):
        for field in ("open", "close"):
            for invalid in (1, 1000):
                with self.subTest(field=field, invalid=invalid):
                    lines = _lines()
                    lines[-1] = replace(lines[-1], **{field: invalid})
                    result = self._analyze_lines(lines)
                    self.assertEqual(result["items"][0]["status"], "unavailable")

    def test_aware_iso_and_datetime_bars_use_shanghai_calendar_date(self):
        for timestamp in (
            "2026-09-10T17:00:00Z",
            "2026-09-10T17:00:00+00:00",
            datetime(2026, 9, 10, 17, tzinfo=timezone.utc),
        ):
            with self.subTest(timestamp=timestamp):
                lines = _lines(30) + [replace(_lines()[-1], time=timestamp)]
                before_close = datetime(2026, 9, 11, 6, 30, tzinfo=timezone.utc)
                item = self._analyze_lines(lines, now=before_close)["items"][0]
                self.assertEqual(item["status"], "ready")
                self.assertEqual(item["bars"], 30)
                self.assertEqual(item["excluded_incomplete_bars"], 1)

    def test_future_daily_bar_is_excluded(self):
        lines = _lines(30) + [replace(_lines()[-1], time="2026-09-12")]
        item = self._analyze_lines(lines)["items"][0]
        self.assertEqual(item["status"], "ready")
        self.assertEqual(item["bars"], 30)
        self.assertEqual(item["excluded_incomplete_bars"], 1)

    def test_exact_cn_and_hk_close_boundaries(self):
        lines = _lines(30) + [replace(_lines()[-1], time="2026-09-11")]
        for market, hour, minute in (("CN", 7, 0), ("HK", 8, 10)):
            cutoff = datetime(2026, 9, 11, hour, minute, tzinfo=timezone.utc)
            for delta, expected in ((-1, 30), (0, 31), (1, 31)):
                with self.subTest(market=market, seconds=delta):
                    item = self._analyze_lines(
                        lines, now=cutoff + timedelta(seconds=delta), market=market
                    )["items"][0]
                    self.assertEqual(item["status"], "ready")
                    self.assertEqual(item["bars"], expected)

    def test_unknown_market_does_not_borrow_cn_closure(self):
        result = self._analyze_lines(_lines(), market="UNKNOWN")
        self.assertEqual(result["items"][0]["status"], "unavailable")

    def test_naive_analysis_time_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self._analyze_lines(_lines(), now=datetime(2026, 9, 11, 16, 30))

    def test_malformed_holding_does_not_block_other_holdings(self):
        config = _config()
        account = config.accounts[0]
        holdings = (*account.holdings, replace(account.holdings[0], code="600001"))
        config = replace(config, accounts=(replace(account, holdings=holdings),))

        def loader(target, **_kwargs):
            lines = _lines()
            if getattr(target, "code", target) == "600000":
                lines[-1] = replace(lines[-1], time="broken")
            return lines

        result = analyze_portfolio(
            config, build_portfolio_snapshot(config, {}),
            history_loader=loader, now=NOW,
        )
        self.assertEqual([item["status"] for item in result["items"]], ["unavailable", "ready"])
        self.assertEqual(result["coverage"], {"ready": 1, "total": 2, "unavailable": 1})
        self.assertEqual(result["benchmark"]["status"], "ready")


if __name__ == "__main__":
    unittest.main()
