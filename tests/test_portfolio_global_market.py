"""Unit tests for the overnight global market calculation module.

Tests follow the strict TDD contract:
- Offline, pure-functional verification
- Boundary conditions for freshness and cutoff times
- Fallback guarantees for US Treasury yields (^TNX)
- Fail-closed behavior on untrusted / dirty observations
- Format output cleanliness and isolation from internal markers
"""

from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
import math
from types import MappingProxyType
import unittest
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from app.market.global_markets import (
    ERROR_CODES,
    ERROR_NETWORK,
    ERROR_RATE_LIMITED,
    GlobalMarketDataError,
    INSTRUMENTS,
    SourceObservation,
)
from app.portfolio.global_market import (
    FRESHNESS_LABELS,
    GLOBAL_SYMBOLS,
    STATUS_LABELS,
    SUMMARY_NOTE,
    build_global_market,
    data_limit_token,
    format_overnight_lines,
    gather_global_observations,
    summarize_overnight,
)


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
FIXED_CUTOFF = datetime(2026, 9, 17, 8, 30, tzinfo=SHANGHAI_TZ)
BASE_SESSION_DATE = "2026-09-16"
BASE_SOURCE_AS_OF = "2026-09-16T20:00:00+00:00"  # 2026-09-17 04:00 in Shanghai, ~4.5h before cutoff


def make_obs(
    symbol="^GSPC",
    value=5000.0,
    previous_value=4950.0,
    source_as_of=BASE_SOURCE_AS_OF,
    session_date=BASE_SESSION_DATE,
    source="yahoo_chart",
):
    return SourceObservation(
        symbol=symbol,
        value=value,
        previous_value=previous_value,
        source_as_of=source_as_of,
        session_date=session_date,
        source=source,
    )


def make_fred_obs(
    symbol="^TNX",
    value=4.25,
    previous_value=4.20,
    session_date=BASE_SESSION_DATE,
):
    return SourceObservation(
        symbol=symbol,
        value=value,
        previous_value=previous_value,
        source_as_of=None,
        session_date=session_date,
        source="fred_dgs10",
    )


def make_standard_observations():
    """Return 8 standard valid observations matching GLOBAL_SYMBOLS."""
    rows = []
    for sym in GLOBAL_SYMBOLS:
        if sym == "^TNX":
            obs = make_fred_obs(sym, value=4.25, previous_value=4.20)
            rows.append((sym, obs, None, "fred_dgs10"))
        else:
            obs = make_obs(sym, value=5000.0, previous_value=4950.0)
            rows.append((sym, obs, None, "yahoo_chart"))
    return tuple(rows)


class TestPortfolioGlobalMarket(unittest.TestCase):
    def test_constants_and_catalogs(self):
        """Verify exports, symbols sequence and label dictionaries."""
        self.assertEqual(GLOBAL_SYMBOLS, tuple(INSTRUMENTS))
        self.assertEqual(len(GLOBAL_SYMBOLS), 8)
        self.assertIsInstance(FRESHNESS_LABELS, Mapping)
        self.assertEqual(
            dict(FRESHNESS_LABELS),
            {
                "RECENT": "数据较新",
                "DELAYED_OR_HOLIDAY": "可能因延迟或休市未更新",
                "STALE": "数据已陈旧，请谨慎核对",
            },
        )
        self.assertIsInstance(STATUS_LABELS, Mapping)
        self.assertIn("complete", STATUS_LABELS)
        self.assertIn("partial", STATUS_LABELS)
        self.assertIn("unavailable", STATUS_LABELS)
        self.assertIn("not_collected", STATUS_LABELS)
        self.assertIn("仅为隔夜事实归纳", SUMMARY_NOTE)
        self.assertIn("不构成", SUMMARY_NOTE)

    def test_gather_global_observations_all_success_with_treasury_fallback(self):
        """Yahoo works for non-TNX, Yahoo fails for TNX, FRED succeeds."""
        mock_yahoo = MagicMock()
        mock_fred = MagicMock()

        def yahoo_fetch(sym):
            if sym == "^TNX":
                raise GlobalMarketDataError(ERROR_NETWORK)
            return make_obs(sym)

        mock_yahoo.fetch.side_effect = yahoo_fetch
        mock_fred.fetch.return_value = make_fred_obs("^TNX")

        results = gather_global_observations(mock_yahoo, mock_fred)
        self.assertEqual(len(results), 8)
        self.assertEqual(tuple(r[0] for r in results), GLOBAL_SYMBOLS)

        for sym, obs, err, src in results:
            self.assertIsNone(err)
            self.assertIsNotNone(obs)
            if sym == "^TNX":
                self.assertEqual(src, "fred_dgs10")
                self.assertEqual(obs.source, "fred_dgs10")
            else:
                self.assertEqual(src, "yahoo_chart")
                self.assertEqual(obs.source, "yahoo_chart")

        mock_fred.fetch.assert_called_once_with("^TNX")

    def test_gather_global_observations_treasury_double_failure(self):
        """Yahoo fails for TNX and FRED fallback also fails."""
        mock_yahoo = MagicMock()
        mock_fred = MagicMock()

        def yahoo_fetch(sym):
            if sym == "^TNX":
                raise GlobalMarketDataError(ERROR_NETWORK)
            return make_obs(sym)

        mock_yahoo.fetch.side_effect = yahoo_fetch
        mock_fred.fetch.side_effect = RuntimeError("fred-down")

        results = gather_global_observations(mock_yahoo, mock_fred)
        tnx_row = [r for r in results if r[0] == "^TNX"][0]
        self.assertIsNone(tnx_row[1])
        self.assertEqual(tnx_row[2], "TREASURY_SOURCES_UNAVAILABLE")
        self.assertIsNone(tnx_row[3])

    def test_gather_non_treasury_never_calls_fallback(self):
        """When non-TNX symbols fail, treasury_fallback must NEVER be called."""
        mock_yahoo = MagicMock()
        mock_fred = MagicMock()

        def yahoo_fetch(sym):
            if sym == "^SOX":
                raise GlobalMarketDataError(ERROR_RATE_LIMITED)
            if sym == "^TNX":
                return make_obs(sym, source="fred_dgs10")
            return make_obs(sym)

        mock_yahoo.fetch.side_effect = yahoo_fetch
        results = gather_global_observations(mock_yahoo, mock_fred)

        sox_row = [r for r in results if r[0] == "^SOX"][0]
        self.assertIsNone(sox_row[1])
        self.assertEqual(sox_row[2], ERROR_RATE_LIMITED)
        mock_fred.fetch.assert_not_called()

    def test_gather_exception_isolation(self):
        """Remote exception messages must never leak into error_code or anywhere else."""
        mock_yahoo = MagicMock()
        secret_msg = "fixture-secret-xyz"
        mock_yahoo.fetch.side_effect = RuntimeError(secret_msg)

        results = gather_global_observations(mock_yahoo, None)
        text_dump = str(results)
        self.assertNotIn(secret_msg, text_dump)
        for sym, obs, err, src in results:
            self.assertIsNone(obs)
            self.assertIsNone(src)
            if sym == "^TNX":
                self.assertEqual(err, "TREASURY_SOURCES_UNAVAILABLE")
            else:
                self.assertEqual(err, "PROVIDER_UNAVAILABLE")

    def test_gather_hostile_source_label_is_not_leaked(self):
        """Hostile self-reported source strings must never leak into index 3 or quotes."""
        mock_provider = MagicMock()
        mock_provider.fetch.side_effect = lambda sym: make_obs(sym, source="__evil__")
        mock_fallback = MagicMock()
        mock_fallback.fetch.side_effect = lambda sym: make_obs(sym, source="__evil__")

        results = gather_global_observations(mock_provider, mock_fallback)

        # (a) str(结果元组) 中不出现 "__evil__" (元组 index 3 即 source label)
        labels = tuple(r[3] for r in results)
        self.assertNotIn("__evil__", str(labels))
        for r in results:
            self.assertNotIn("__evil__", str(r[3]))

        # (b) 所有 index3 只能是 None/"yahoo_chart"/"fred_dgs10"
        allowed = {None, "yahoo_chart", "fred_dgs10"}
        for r in results:
            self.assertIn(r[3], allowed)

        # (c) 把该结果喂 build_global_market 后，所有 quote 的 source is None 且 error_code == "INVALID_OBSERVATION"
        res = build_global_market(results, FIXED_CUTOFF)
        self.assertEqual(len(res["quotes"]), 8)
        for quote in res["quotes"]:
            self.assertIsNone(quote["source"])
            self.assertEqual(quote["error_code"], "INVALID_OBSERVATION")

        # 验证 fallback 为 None 时，TNX 的 index 3 为 None，依然不泄漏且符合允许值
        results_no_fb = gather_global_observations(mock_provider, None)
        labels_no_fb = tuple(r[3] for r in results_no_fb)
        self.assertNotIn("__evil__", str(labels_no_fb))
        for r in results_no_fb:
            self.assertIn(r[3], allowed)

    def test_gather_treasury_falls_back_when_yahoo_serves_tnx(self):
        """When global_provider returns Yahoo-shaped ^TNX (source='yahoo_chart'), must fall back to FRED."""
        mock_yahoo = MagicMock()
        mock_fred = MagicMock()

        def yahoo_fetch(sym):
            if sym == "^TNX":
                return make_obs("^TNX", value=4.30, previous_value=4.25, source="yahoo_chart")
            return make_obs(sym)

        mock_yahoo.fetch.side_effect = yahoo_fetch
        fred_tnx_obs = make_fred_obs("^TNX", value=4.25, previous_value=4.20)
        mock_fred.fetch.return_value = fred_tnx_obs

        results = gather_global_observations(mock_yahoo, mock_fred)

        # (a) treasury_fallback.fetch 恰好被调用一次且参数为 "^TNX"
        mock_fred.fetch.assert_called_once_with("^TNX")

        # (b) ^TNX 行 obs is not None 且 index3 == "fred_dgs10"
        tnx_row = [r for r in results if r[0] == "^TNX"][0]
        self.assertIsNotNone(tnx_row[1])
        self.assertEqual(tnx_row[3], "fred_dgs10")

        # (c) build_global_market 后 ^TNX 记录 error_code is None、value 等于 FRED 观测的 value
        res = build_global_market(results, FIXED_CUTOFF)
        tnx_quote = [q for q in res["quotes"] if q["symbol"] == "^TNX"][0]
        self.assertIsNone(tnx_quote["error_code"])
        self.assertEqual(tnx_quote["value"], fred_tnx_obs.value)
        self.assertEqual(tnx_quote["source"], "fred_dgs10")

    def test_build_global_market_cutoff_validation(self):
        """Cutoff must be timezone-aware datetime."""
        obs = make_standard_observations()
        with self.assertRaises(ValueError) as cm:
            build_global_market(obs, datetime(2026, 9, 17, 8, 0))
        self.assertEqual(str(cm.exception), "cutoff must be timezone-aware")

    def test_build_global_market_all_eight_success(self):
        """Verify keys order, catalog fields, change algorithms, and freshness."""
        obs = make_standard_observations()
        res = build_global_market(obs, FIXED_CUTOFF)

        self.assertEqual(tuple(res.keys()), ("status", "cutoff", "quotes", "summary"))
        self.assertEqual(res["status"], "complete")
        self.assertEqual(res["cutoff"], FIXED_CUTOFF.isoformat())
        self.assertEqual(len(res["quotes"]), 8)

        expected_keys = (
            "category", "symbol", "name", "value", "previous_value", "value_unit",
            "change", "change_unit", "source", "source_as_of", "session_date",
            "freshness", "error_code"
        )
        for record, sym in zip(res["quotes"], GLOBAL_SYMBOLS):
            self.assertEqual(tuple(record.keys()), expected_keys)
            cat_entry = INSTRUMENTS[sym]
            self.assertEqual(record["symbol"], sym)
            self.assertEqual(record["category"], cat_entry.category)
            self.assertEqual(record["name"], cat_entry.name)
            self.assertEqual(record["value_unit"], cat_entry.value_unit)
            self.assertIsNone(record["error_code"])
            self.assertIn(record["freshness"], ("RECENT", "DELAYED_OR_HOLIDAY", "STALE"))

            if sym == "^TNX":
                self.assertEqual(record["change_unit"], "bp")
                self.assertAlmostEqual(record["change"], 5.0, places=4)
                self.assertEqual(record["source"], "fred_dgs10")
                self.assertIsNone(record["source_as_of"])
            else:
                self.assertEqual(record["change_unit"], "pct")
                self.assertAlmostEqual(record["change"], (5000.0 / 4950.0 - 1.0) * 100.0, places=4)
                self.assertEqual(record["source"], "yahoo_chart")

    def test_freshness_three_tier_boundaries(self):
        """Test exact thresholds: <= 36h (RECENT), <= 120h (DELAYED_OR_HOLIDAY), > 120h (STALE)."""
        cutoff = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)

        cases = [
            (timedelta(hours=35), "RECENT"),
            (timedelta(hours=36), "RECENT"),
            (timedelta(hours=36, seconds=1), "DELAYED_OR_HOLIDAY"),
            (timedelta(hours=119), "DELAYED_OR_HOLIDAY"),
            (timedelta(hours=120), "DELAYED_OR_HOLIDAY"),
            (timedelta(hours=121), "STALE"),
        ]

        for delta, expected_freshness in cases:
            with self.subTest(delta=delta, expected=expected_freshness):
                src_time = cutoff - delta
                obs_time_str = src_time.isoformat()
                test_obs = (
                    ("^GSPC", make_obs("^GSPC", source_as_of=obs_time_str), None, "yahoo_chart"),
                )
                res = build_global_market(test_obs, cutoff)
                record = res["quotes"][0]
                self.assertEqual(record["error_code"], None)
                self.assertEqual(record["freshness"], expected_freshness)

    def test_stale_gspc_does_not_drive_direction(self):
        """STALE ^GSPC must gate the summary and prevent direction derivation."""
        cutoff = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)
        stale_time = (cutoff - timedelta(days=10)).isoformat()
        obs = [
            ("^GSPC", make_obs("^GSPC", source_as_of=stale_time, session_date="2026-09-15"), None, "yahoo_chart"),
        ]
        for sym in GLOBAL_SYMBOLS[1:]:
            obs.append((sym, None, "UNAVAILABLE", None))

        res = build_global_market(obs, cutoff)
        summary = res["summary"]
        self.assertTrue(summary["gated"])
        self.assertEqual(summary["reason"], "隔夜关键数据缺失或已陈旧，本次不作方向判断。")
        self.assertIsNone(summary["direction"])
        self.assertIsNone(summary["technology"])
        self.assertIsNone(summary["impact"])

        lines = format_overnight_lines(res)
        joined = "\n".join(lines)
        self.assertIn("隔夜关键数据缺失或已陈旧，本次不作方向判断。", joined)

    def test_partial_failure_sox(self):
        """^SOX failure yields status=partial, line '暂不可用', and technology='数据不足'."""
        rows = []
        for sym in GLOBAL_SYMBOLS:
            if sym == "^SOX":
                rows.append((sym, None, "PROVIDER_UNAVAILABLE", None))
            elif sym == "^TNX":
                rows.append((sym, make_fred_obs(sym), None, "fred_dgs10"))
            else:
                rows.append((sym, make_obs(sym, value=5050.0, previous_value=5000.0), None, "yahoo_chart"))

        res = build_global_market(rows, FIXED_CUTOFF)
        self.assertEqual(res["status"], "partial")
        summary = res["summary"]
        self.assertFalse(summary["gated"])
        self.assertEqual(summary["direction"], "上涨")
        self.assertEqual(summary["technology"], "数据不足")
        self.assertEqual(summary["impact"], "科技方向数据不足，暂不判断")

        lines = format_overnight_lines(res)
        sox_name = INSTRUMENTS["^SOX"].name
        self.assertTrue(any(f"- {sox_name}：暂不可用。" in line for line in lines))

    def test_all_failure_unavailable(self):
        """All symbols failing yields status=unavailable and data_limit_token=unavailable."""
        rows = [(sym, None, "NETWORK_UNAVAILABLE", None) for sym in GLOBAL_SYMBOLS]
        res = build_global_market(rows, FIXED_CUTOFF)

        self.assertEqual(res["status"], "unavailable")
        self.assertEqual(data_limit_token(res), "unavailable")
        self.assertTrue(res["summary"]["gated"])

    def test_future_data_protection(self):
        """Source time > cutoff + 5min or session_date > cutoff local date fails with INVALID_SOURCE_TIME."""
        future_time = (FIXED_CUTOFF + timedelta(hours=1)).isoformat()
        obs_future = [
            ("^GSPC", make_obs("^GSPC", source_as_of=future_time), None, "yahoo_chart"),
        ]
        res1 = build_global_market(obs_future, FIXED_CUTOFF)
        self.assertEqual(res1["quotes"][0]["error_code"], "INVALID_SOURCE_TIME")

        exact_time = FIXED_CUTOFF.isoformat()
        obs_exact = [
            ("^GSPC", make_obs("^GSPC", source_as_of=exact_time), None, "yahoo_chart"),
        ]
        res2 = build_global_market(obs_exact, FIXED_CUTOFF)
        self.assertIsNone(res2["quotes"][0]["error_code"])

        cutoff_shanghai_date = FIXED_CUTOFF.astimezone(SHANGHAI_TZ).date()
        future_date = (cutoff_shanghai_date + timedelta(days=1)).isoformat()
        obs_future_date = [
            ("^GSPC", make_obs("^GSPC", session_date=future_date), None, "yahoo_chart"),
        ]
        res3 = build_global_market(obs_future_date, FIXED_CUTOFF)
        self.assertEqual(res3["quotes"][0]["error_code"], "INVALID_SOURCE_TIME")

    def test_dirty_data_fails_closed_without_exceptions(self):
        """Dirty numbers, bad ISO dates, mismatched source fail closed with INVALID_OBSERVATION."""
        dirty_cases = [
            ("zero_val", make_obs(value=0.0)),
            ("neg_val", make_obs(value=-1.0)),
            ("bool_val", make_obs(value=True)),
            ("nan_val", make_obs(value=float("nan"))),
            ("inf_val", make_obs(value=float("inf"))),
            ("zero_prev", make_obs(previous_value=0.0)),
            ("bad_date_fmt1", make_obs(session_date="2026-9-1")),
            ("bad_date_fmt2", make_obs(session_date="2026-09-01T00:00:00")),
            ("empty_date", make_obs(session_date="")),
            ("mismatched_source", make_obs(source="fred_dgs10")),
            ("tnx_with_as_of", make_fred_obs("^TNX")),
        ]

        for label, dirty_obs in dirty_cases:
            if label == "tnx_with_as_of":
                dirty_obs = SourceObservation(
                    symbol="^TNX",
                    value=4.25,
                    previous_value=4.20,
                    source_as_of="2026-09-16T20:00:00+00:00",
                    session_date="2026-09-16",
                    source="fred_dgs10",
                )
            with self.subTest(label=label):
                res = build_global_market([(dirty_obs.symbol, dirty_obs, None, dirty_obs.source)], FIXED_CUTOFF)
                record = res["quotes"][0]
                self.assertEqual(record["error_code"], "INVALID_OBSERVATION")
                self.assertIsNone(record["value"])
                self.assertIsNone(record["previous_value"])
                self.assertIsNone(record["change"])
                self.assertIsNone(record["change_unit"])
                self.assertIsNone(record["source"])
                self.assertIsNone(record["freshness"])

    def test_collected_false_behavior(self):
        """collected=False sets status=not_collected and NOT_COLLECTED_FOR_STAGE for all 8."""
        obs = make_standard_observations()
        res = build_global_market(obs, FIXED_CUTOFF, collected=False)

        self.assertEqual(res["status"], "not_collected")
        self.assertEqual(data_limit_token(res), "not_collected")
        self.assertEqual(len(res["quotes"]), 8)

        for q in res["quotes"]:
            self.assertEqual(q["error_code"], "NOT_COLLECTED_FOR_STAGE")
            self.assertIsNone(q["value"])

        lines = format_overnight_lines(res)
        self.assertEqual(lines[1], STATUS_LABELS["not_collected"])

    def test_format_overnight_lines_no_internal_markers(self):
        """No internal logs or labels (manual_replay, yahoo_chart, RECENT, None, etc.) in text."""
        obs = make_standard_observations()
        res = build_global_market(obs, FIXED_CUTOFF)
        lines = format_overnight_lines(res)

        full_text = "\n".join(lines)
        forbidden_markers = [
            "manual_replay",
            "available_unverified",
            "yahoo_chart",
            "fred_dgs10",
            "RECENT",
            "STALE",
            "DELAYED_OR_HOLIDAY",
            "error_code",
            "provider",
            "None",
            "completed",
        ]
        for marker in forbidden_markers:
            self.assertNotIn(marker, full_text)

        self.assertTrue(lines[0].startswith("🌍 隔夜全球市场"))
        self.assertEqual(lines[1], STATUS_LABELS["complete"])

    def test_summarize_overnight_robustness_on_invalid_payload(self):
        """summarize_overnight fails closed on None, {}, or malformed quotes without raising."""
        for bad_payload in (None, {}, {"quotes": 123}, "not-a-dict", []):
            with self.subTest(payload=bad_payload):
                res = summarize_overnight(bad_payload)
                self.assertIsInstance(res, dict)
                self.assertTrue(res["gated"])
                self.assertIsNotNone(res["reason"])
                self.assertIsNone(res["direction"])
                self.assertIsNone(res["technology"])
                self.assertIsNone(res["impact"])
                self.assertEqual(res["note"], SUMMARY_NOTE)

    def test_data_limit_token_robustness(self):
        """data_limit_token returns expected tokens without raising on invalid payload."""
        self.assertEqual(data_limit_token(None), "unavailable")
        self.assertEqual(data_limit_token({}), "dated_observations")
        self.assertEqual(data_limit_token({"status": "unavailable"}), "unavailable")
        self.assertEqual(data_limit_token({"status": "not_collected"}), "not_collected")
        self.assertEqual(data_limit_token({"status": "complete"}), "dated_observations")
        self.assertEqual(data_limit_token(12345), "unavailable")

    def test_market_direction_and_tech_combinations(self):
        """Verify direction (up/down/flat) and tech (strong/weak/mixed) logic."""
        # Case 1: GSPC up >= 0.5%, Tech both >= 0.5% -> Up, 偏强, A股科技方向偏正面
        obs1 = [
            ("^GSPC", make_obs("^GSPC", value=5050.0, previous_value=5000.0), None, "yahoo_chart"),
            ("^IXIC", make_obs("^IXIC", value=16100.0, previous_value=16000.0), None, "yahoo_chart"),
            ("^SOX", make_obs("^SOX", value=5050.0, previous_value=5000.0), None, "yahoo_chart"),
        ]
        res1 = build_global_market(obs1, FIXED_CUTOFF)
        self.assertEqual(res1["summary"]["direction"], "上涨")
        self.assertEqual(res1["summary"]["technology"], "偏强")
        self.assertEqual(res1["summary"]["impact"], "A股科技方向偏正面")

        # Case 2: GSPC down <= -0.5%, Tech both <= -0.5% -> Down, 偏弱, A股科技方向偏负面
        obs2 = [
            ("^GSPC", make_obs("^GSPC", value=4950.0, previous_value=5000.0), None, "yahoo_chart"),
            ("^IXIC", make_obs("^IXIC", value=15800.0, previous_value=16000.0), None, "yahoo_chart"),
            ("^SOX", make_obs("^SOX", value=4950.0, previous_value=5000.0), None, "yahoo_chart"),
        ]
        res2 = build_global_market(obs2, FIXED_CUTOFF)
        self.assertEqual(res2["summary"]["direction"], "下跌")
        self.assertEqual(res2["summary"]["technology"], "偏弱")
        self.assertEqual(res2["summary"]["impact"], "A股科技方向偏负面")

        # Case 3: GSPC flat, Tech mixed -> 震荡, 分化, A股科技方向暂不明朗
        obs3 = [
            ("^GSPC", make_obs("^GSPC", value=5005.0, previous_value=5000.0), None, "yahoo_chart"),
            ("^IXIC", make_obs("^IXIC", value=16100.0, previous_value=16000.0), None, "yahoo_chart"),
            ("^SOX", make_obs("^SOX", value=4980.0, previous_value=5000.0), None, "yahoo_chart"),
        ]
        res3 = build_global_market(obs3, FIXED_CUTOFF)
        self.assertEqual(res3["summary"]["direction"], "震荡")
        self.assertEqual(res3["summary"]["technology"], "分化")
        self.assertEqual(res3["summary"]["impact"], "A股科技方向暂不明朗")


if __name__ == "__main__":
    unittest.main()