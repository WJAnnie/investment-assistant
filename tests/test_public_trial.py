"""Synthetic, public-only regression tests for ``app.integration.public_trial``.

Stdlib ``unittest`` only (no pytest/yaml). Every provider is a local fixture;
no network, config, credentials or account data is touched.
"""

from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import textwrap
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.integration import public_trial as trial
from app.market.global_markets import INSTRUMENTS, SourceObservation
from app.market.models import Quote
from app.analysis.industry import IndustryObservation
from app.market.industries import IndustryFetch, SOURCE_AKSHARE_EASTMONEY_BOARD


ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")
MONDAY = datetime(2026, 9, 14, tzinfo=TZ)  # weekday
SATURDAY = datetime(2026, 9, 19, tzinfo=TZ)  # weekend
SECRET = "SINA-PROVIDER-SECRET-9c1f"
SECRET_NAME = "SECRET-QUOTE-NAME-7b2a"
AUTO = object()
STAGE_CLOCKS = {"morning": "09:00", "midday": "11:30", "decision": "14:30", "closing": "16:10"}
COVERAGE = {
    "global_markets": "PUBLIC_OBSERVATIONS", "industry_ranking": "PUBLIC_OBSERVATIONS",
    "morning_baseline": "MISSING", "multi_timeframe": "NOT_IMPLEMENTED",
    "prediction_pairing": "MISSING", "exchange_calendar": "NOT_IMPLEMENTED",
}
TOP_FIELDS = {
    "schema_version", "data_classification", "purpose", "stage", "execution_mode",
    "requested_at", "generated_at", "scheduled_for", "timing_status", "market",
    "global_market", "industries", "coverage", "synthetic_account", "decision", "real_account_trial_day",
}
QUOTE_FIELDS = {"code", "name", "price", "change_pct", "source_as_of", "freshness", "error_code"}
GLOBAL_FIELDS = {
    "category", "symbol", "name", "value", "previous_value", "value_unit",
    "change", "change_unit", "source", "source_as_of", "session_date",
    "freshness", "error_code",
}
SYNTHETIC = {
    "id": "SIMULATED-ONLY",
    "positions": [{"symbol": "DEMO_A", "weight_pct": 10}, {"symbol": "DEMO_B", "weight_pct": 5}],
    "cash_pct": 85,
}
DECISION = {
    "action": "WAIT", "position_change_pct": 0, "score": None,
    "manual_confirmation_required": True, "auto_trade_enabled": False,
}
BLOCKED_MODULES = ("app.portfolio", "app.workflow", "app.market.factory",
                   "app.integration.github_export", "dotenv")
LABELS = ("WAIT", "合成", "非交易信号")


def clock(stage):
    raw = trial.STAGES[stage]
    text = raw.strftime("%H:%M") if hasattr(raw, "strftime") else str(raw)
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).strftime("%H:%M")
        except ValueError:
            pass
    raise AssertionError(f"unparsable stage clock: {text!r}")


def slot(stage, day=MONDAY):
    hour, minute = (int(part) for part in clock(stage).split(":"))
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def quote_for(code, **changes):
    values = dict(code=code, name=SECRET_NAME, price=3210.5, change=0.42,
                  timestamp=SECRET, source=SECRET, market_time=SECRET)
    values.update(changes)
    return Quote(**values)


class FakeProvider:
    """Local provider stub: no I/O, no real data, optional per-code overrides."""

    def __init__(self, overrides=None):
        self.calls = []
        self.overrides = dict(overrides or {})

    def fetch(self, code):
        self.calls.append(code)
        result = self.overrides.get(code)
        return result(code) if callable(result) else (result if code in self.overrides
                                                     else quote_for(code))


class FakeGlobalProvider:
    def __init__(self, overrides=None):
        self.calls = []
        self.overrides = dict(overrides or {})

    def fetch(self, symbol):
        self.calls.append(symbol)
        if symbol in self.overrides:
            value = self.overrides[symbol]
            return value(symbol) if callable(value) else value
        return SourceObservation(
            symbol=symbol,
            value=4.61 if symbol == "^TNX" else 102.0,
            previous_value=4.60 if symbol == "^TNX" else 100.0,
            source_as_of="2026-09-14T00:00:00+00:00",
            session_date="2026-09-13",
            source="yahoo_chart",
        )


class FailingGlobalProvider(FakeGlobalProvider):
    def fetch(self, symbol):
        self.calls.append(symbol)
        raise RuntimeError(SECRET)


class FakeIndustryProvider:
    def __init__(self, *, raises=False, empty=False):
        self.calls = []
        self.raises = raises
        self.empty = empty

    def fetch(self, cutoff, market_date):
        self.calls.append((cutoff, market_date))
        if self.raises:
            raise RuntimeError(SECRET)
        if self.empty:
            return IndustryFetch(observations=())

        fetched_at = trial._now()
        as_of = fetched_at - timedelta(minutes=30)
        observations = []
        for i in range(1, 11):
            observations.append(
                IndustryObservation(
                    code=f"BK00{i:02d}",
                    name=f"板块{i:02d}",
                    as_of=as_of,
                    fetched_at=fetched_at,
                    source=SOURCE_AKSHARE_EASTMONEY_BOARD,
                    return_1d_pct=10.0 - i,
                    return_5d_pct=5.0,
                    return_20d_pct=3.0,
                    turnover_rate=1.5,
                    advancers=20,
                    decliners=5,
                )
            )
        return IndustryFetch(observations=tuple(observations))


def build(stage="morning", *, provider=None, global_provider=None,
          treasury_fallback=None, industry_provider=None, minutes=0, day=MONDAY, after=1,
          mode="scheduled", generated_at=AUTO):
    requested = slot(stage, day) + timedelta(minutes=minutes)
    kwargs = {} if generated_at is AUTO and after is None else {"generated_at": (
        requested + timedelta(minutes=after) if generated_at is AUTO else generated_at)}
    return trial.build_snapshot(
        stage, provider=provider or FakeProvider(),
        global_provider=global_provider or FakeGlobalProvider(),
        treasury_fallback=treasury_fallback or FailingGlobalProvider(),
        industry_provider=FakeIndustryProvider() if industry_provider is None else industry_provider,
        requested_at=requested, execution_mode=mode, **kwargs)


def manual():
    return build("morning", mode="manual_replay", after=1)


def scheduled():
    return build("morning", after=1)


def quotes(payload):
    return {quote["code"]: quote for quote in payload["market"]["quotes"]}


def tamper(payload, *edits):
    for edit in edits:
        if callable(edit):
            edit(payload)
            continue
        if len(edit) == 2 and isinstance(edit[0], (list, tuple)):
            path, value = tuple(edit[0]), edit[1]
        else:
            path, value = tuple(edit[:-1]), edit[-1]
        if not path:
            raise AssertionError(f"empty tamper path: {edit!r}")
        target = payload
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return payload


def requested_after_generated(payload):
    payload["requested_at"] = (datetime.fromisoformat(payload["requested_at"])
                               + timedelta(minutes=5)).isoformat()


def first_symbol():
    return list(trial.PUBLIC_SYMBOLS)[0]


def legacy(payload=None):
    result = json.loads(json.dumps(scheduled() if payload is None else payload))
    result["schema_version"] = "public-trial/v1"
    result["coverage"]["global_markets"] = "NOT_IMPLEMENTED"
    result["coverage"]["industry_ranking"] = "NOT_IMPLEMENTED"
    result.pop("global_market", None)
    result.pop("industries", None)
    return result


def frozen_v2(payload=None):
    """Exact shape of the snapshots already published on the data branch."""
    result = json.loads(json.dumps(scheduled() if payload is None else payload))
    result["schema_version"] = "public-trial/v2"
    result["coverage"]["industry_ranking"] = "NOT_IMPLEMENTED"
    result.pop("industries", None)
    return result


class ContractTests(unittest.TestCase):
    def test_v2_morning_collects_exact_global_instruments_without_enabling_trade(self):
        yahoo = FakeGlobalProvider()
        fallback = FailingGlobalProvider()
        payload = build(global_provider=yahoo, treasury_fallback=fallback)

        self.assertEqual(payload["schema_version"], "public-trial/v3")
        self.assertEqual(
            [item["symbol"] for item in payload["global_market"]["quotes"]],
            list(INSTRUMENTS),
        )
        treasury = next(item for item in payload["global_market"]["quotes"]
                        if item["symbol"] == "^TNX")
        self.assertEqual(treasury["change_unit"], "bp")
        self.assertAlmostEqual(treasury["change"], 1.0)
        self.assertEqual(payload["decision"]["action"], "WAIT")
        self.assertIs(payload["decision"]["auto_trade_enabled"], False)
        self.assertEqual(yahoo.calls, list(INSTRUMENTS))
        self.assertEqual(fallback.calls, [])

    def test_one_global_symbol_failure_is_isolated(self):
        failed = "^SOX"
        yahoo = FakeGlobalProvider({
            failed: lambda _symbol: (_ for _ in ()).throw(RuntimeError(SECRET)),
        })
        payload = build(global_provider=yahoo)
        records = {item["symbol"]: item for item in payload["global_market"]["quotes"]}
        self.assertEqual(payload["global_market"]["status"], "partial")
        self.assertEqual(records[failed]["error_code"], "PROVIDER_UNAVAILABLE")
        self.assertIsNone(records[failed]["value"])
        self.assertIsNone(records["^GSPC"]["error_code"])
        self.assertNotIn(SECRET, json.dumps(payload, ensure_ascii=False))

    def test_treasury_uses_fred_only_after_yahoo_failure(self):
        yahoo = FakeGlobalProvider({
            "^TNX": lambda _symbol: (_ for _ in ()).throw(RuntimeError(SECRET)),
        })
        fred = FakeGlobalProvider({
            "^TNX": SourceObservation(
                symbol="^TNX", value=4.61, previous_value=4.60,
                source_as_of=None, session_date="2026-09-13", source="fred_dgs10"),
        })
        payload = build(global_provider=yahoo, treasury_fallback=fred)
        treasury = next(item for item in payload["global_market"]["quotes"]
                        if item["symbol"] == "^TNX")
        self.assertEqual(payload["global_market"]["status"], "complete")
        self.assertEqual(fred.calls, ["^TNX"])
        self.assertEqual(treasury["source"], "fred_dgs10")
        self.assertIsNone(treasury["source_as_of"])
        self.assertEqual(treasury["freshness"], "RECENT")
        self.assertAlmostEqual(treasury["change"], 1.0)

    def test_treasury_double_failure_is_fixed_and_does_not_leak(self):
        yahoo = FakeGlobalProvider({
            "^TNX": lambda _symbol: (_ for _ in ()).throw(RuntimeError(SECRET)),
        })
        fred = FailingGlobalProvider()
        payload = build(global_provider=yahoo, treasury_fallback=fred)
        treasury = next(item for item in payload["global_market"]["quotes"]
                        if item["symbol"] == "^TNX")
        self.assertEqual(payload["global_market"]["status"], "partial")
        self.assertEqual(treasury["error_code"], "TREASURY_SOURCES_UNAVAILABLE")
        self.assertEqual(fred.calls, ["^TNX"])
        self.assertNotIn(SECRET, json.dumps(payload, ensure_ascii=False))

    def test_all_global_sources_failure_is_unavailable_but_still_wait(self):
        yahoo, fred = FailingGlobalProvider(), FailingGlobalProvider()
        payload = build(global_provider=yahoo, treasury_fallback=fred)
        self.assertEqual(payload["global_market"]["status"], "unavailable")
        self.assertTrue(all(item["error_code"] is not None
                            for item in payload["global_market"]["quotes"]))
        self.assertEqual(payload["decision"], DECISION)
        self.assertNotIn(SECRET, json.dumps(payload, ensure_ascii=False))

    def test_global_freshness_handles_delayed_stale_and_future(self):
        cases = {
            "delayed": ("2026-09-11T23:00:00+00:00", "DELAYED_OR_HOLIDAY", None),
            "stale": ("2026-09-08T00:00:00+00:00", "STALE", None),
            "future": ("2026-09-14T01:07:00+00:00", None, "INVALID_SOURCE_TIME"),
        }
        for name, (as_of, freshness, error) in cases.items():
            with self.subTest(name=name):
                observation = SourceObservation(
                    symbol="^GSPC", value=102, previous_value=100,
                    source_as_of=as_of, session_date="2026-09-13", source="yahoo_chart")
                payload = build(global_provider=FakeGlobalProvider({"^GSPC": observation}), after=1)
                record = payload["global_market"]["quotes"][0]
                self.assertEqual(record["freshness"], freshness)
                self.assertEqual(record["error_code"], error)

    def test_global_freshness_boundaries_are_inclusive(self):
        generated = slot("morning")
        generated_utc = generated.astimezone(ZoneInfo("UTC"))
        cases = (
            (generated_utc - timedelta(hours=36), "RECENT"),
            (generated_utc - timedelta(hours=36, seconds=1), "DELAYED_OR_HOLIDAY"),
            (generated_utc - timedelta(hours=120), "DELAYED_OR_HOLIDAY"),
            (generated_utc - timedelta(hours=120, seconds=1), "STALE"),
            (generated_utc + timedelta(minutes=5), "RECENT"),
        )
        for source_time, expected in cases:
            with self.subTest(source_time=source_time):
                observation = SourceObservation(
                    "^GSPC", 102, 100, source_time.isoformat(),
                    source_time.date().isoformat(), "yahoo_chart")
                payload = build(
                    global_provider=FakeGlobalProvider({"^GSPC": observation}),
                    generated_at=generated,
                )
                self.assertEqual(payload["global_market"]["quotes"][0]["freshness"], expected)

    def test_fred_date_only_freshness_does_not_invent_a_timestamp(self):
        yahoo = FakeGlobalProvider({
            "^TNX": lambda _symbol: (_ for _ in ()).throw(RuntimeError(SECRET)),
        })
        cases = (
            ("2026-09-08", "STALE", None),
            ("2026-09-15", None, "INVALID_SOURCE_TIME"),
        )
        for session_date, freshness, error in cases:
            with self.subTest(session_date=session_date):
                fred = FakeGlobalProvider({
                    "^TNX": SourceObservation(
                        "^TNX", 4.61, 4.60, None, session_date, "fred_dgs10"),
                })
                payload = build(global_provider=yahoo, treasury_fallback=fred)
                record = payload["global_market"]["quotes"][4]
                self.assertIsNone(record["source_as_of"])
                self.assertEqual(record["freshness"], freshness)
                self.assertEqual(record["error_code"], error)

    def test_yahoo_transport_cannot_spoof_a_fred_observation(self):
        spoofed = SourceObservation(
            "^TNX", 4.61, 4.60, None, "2026-09-13", "fred_dgs10")
        payload = build(global_provider=FakeGlobalProvider({"^TNX": spoofed}))
        record = payload["global_market"]["quotes"][4]
        self.assertEqual(record["error_code"], "INVALID_OBSERVATION")
        self.assertIsNone(record["source"])

    def test_invalid_observations_fail_one_record_closed(self):
        invalid = (
            object(),
            SourceObservation("^DJI", 102, 100, "2026-09-14T00:00:00+00:00", "2026-09-13", "yahoo_chart"),
            SourceObservation("^GSPC", True, 100, "2026-09-14T00:00:00+00:00", "2026-09-13", "yahoo_chart"),
            SourceObservation("^GSPC", 102, 0, "2026-09-14T00:00:00+00:00", "2026-09-13", "yahoo_chart"),
            SourceObservation("^GSPC", 102, 100, None, "2026-09-13", "yahoo_chart"),
            SourceObservation("^GSPC", 102, 100, "2026-09-14T00:00:00Z", "2026-09-13", "yahoo_chart"),
            SourceObservation("^GSPC", 102, 100, "2026-09-14T00:00:00+00:00", "2026-02-30", "yahoo_chart"),
            SourceObservation("^GSPC", 102, 100, "2026-09-14T00:00:00+00:00", "2026-09-13", SECRET),
        )
        for observation in invalid:
            with self.subTest(observation=observation):
                payload = build(global_provider=FakeGlobalProvider({"^GSPC": observation}))
                record = payload["global_market"]["quotes"][0]
                self.assertEqual(record["error_code"], "INVALID_OBSERVATION")
                self.assertIsNone(record["source"])
                self.assertNotIn(SECRET, json.dumps(payload, ensure_ascii=False))

    def test_non_morning_stages_never_call_global_providers(self):
        for stage in ("midday", "decision", "closing"):
            with self.subTest(stage=stage):
                yahoo, fred = FakeGlobalProvider(), FakeGlobalProvider()
                payload = build(stage, global_provider=yahoo, treasury_fallback=fred)
                self.assertEqual(yahoo.calls, [])
                self.assertEqual(fred.calls, [])
                self.assertEqual(payload["global_market"]["status"], "not_collected")
                self.assertEqual(
                    {item["error_code"] for item in payload["global_market"]["quotes"]},
                    {"NOT_COLLECTED_FOR_STAGE"},
                )

    def test_non_morning_validation_rejects_a_success_record_in_every_slot(self):
        successful_records = build("morning")["global_market"]["quotes"]
        for stage in ("midday", "decision", "closing"):
            for index, successful in enumerate(successful_records):
                with self.subTest(stage=stage, symbol=successful["symbol"]):
                    payload = build(stage)
                    payload["global_market"]["quotes"][index] = dict(successful)
                    self.assertEqual(payload["global_market"]["status"], "not_collected")
                    with self.assertRaises(ValueError):
                        trial.validate_snapshot(payload)

    def test_stages_and_public_symbols_are_fixed(self):
        self.assertEqual(set(trial.STAGES), set(STAGE_CLOCKS))
        for stage, expected in STAGE_CLOCKS.items():
            with self.subTest(stage=stage):
                self.assertEqual(clock(stage), expected)
        self.assertIsInstance(trial.PUBLIC_SYMBOLS, dict)
        self.assertTrue(trial.PUBLIC_SYMBOLS)
        for code, name in trial.PUBLIC_SYMBOLS.items():
            with self.subTest(code=code):
                self.assertIsInstance(code, str)
                self.assertIsInstance(name, str)
                self.assertTrue(code.strip() and name.strip())

    def test_top_level_schema_coverage_account_and_decision_are_exact(self):
        payload = scheduled()
        self.assertEqual(set(payload), TOP_FIELDS)
        self.assertEqual(payload["schema_version"], "public-trial/v3")
        self.assertEqual(payload["data_classification"], "public_market_and_synthetic_test")
        self.assertEqual(payload["purpose"], "delivery_test_not_investment_advice")
        self.assertEqual(payload["stage"], "morning")
        self.assertEqual(payload["execution_mode"], "scheduled")
        self.assertIs(payload["real_account_trial_day"], False)
        self.assertEqual(payload["coverage"], COVERAGE)
        self.assertEqual(payload["synthetic_account"], SYNTHETIC)
        self.assertEqual(payload["decision"], DECISION)
        self.assertEqual(payload["market"]["source"], "sina_public")
        self.assertEqual(payload["global_market"]["status"], "complete")
        self.assertEqual(len(payload["global_market"]["quotes"]), len(INSTRUMENTS))
        for symbol, record in zip(INSTRUMENTS, payload["global_market"]["quotes"]):
            self.assertEqual(set(record), GLOBAL_FIELDS)
            self.assertEqual(record["symbol"], symbol)

    def test_iso_times_use_beijing_offset_and_scheduled_slot(self):
        payload = build("midday", after=1)
        for field in ("requested_at", "generated_at", "scheduled_for"):
            with self.subTest(field=field):
                self.assertTrue(payload[field].endswith("+08:00"), payload[field])
                self.assertIsNotNone(datetime.fromisoformat(payload[field]).tzinfo)
        self.assertEqual(payload["scheduled_for"], slot("midday").isoformat())
        self.assertGreaterEqual(datetime.fromisoformat(payload["generated_at"]),
                                datetime.fromisoformat(payload["requested_at"]))

    def test_build_snapshot_only_requests_fixed_public_codes(self):
        provider = FakeProvider()
        scheduled_result = build("midday", provider=provider, after=1)
        self.assertEqual(set(provider.calls), set(trial.PUBLIC_SYMBOLS))
        self.assertEqual(len(provider.calls), len(trial.PUBLIC_SYMBOLS))
        self.assertEqual(len(scheduled_result["market"]["quotes"]), len(trial.PUBLIC_SYMBOLS))

    def test_quotes_use_fixed_labels_and_exact_fields(self):
        for code, quote in quotes(scheduled()).items():
            with self.subTest(code=code):
                self.assertEqual(set(quote), QUOTE_FIELDS)
                self.assertEqual(quote["name"], trial.PUBLIC_SYMBOLS[code])
                self.assertNotEqual(quote["name"], SECRET_NAME)
                self.assertNotIsInstance(quote["price"], bool)
                self.assertGreater(quote["price"], 0)
                self.assertTrue(math.isfinite(quote["price"]))
                self.assertTrue(math.isfinite(quote["change_pct"]))
                self.assertIsNone(quote["source_as_of"])
                self.assertEqual(quote["freshness"], "UNKNOWN")
                self.assertIsNone(quote["error_code"])
        self.assertEqual(scheduled()["market"]["status"], "available_unverified")

    def test_no_quote_exception_and_invalid_quote_are_distinguished(self):
        code = first_symbol()
        missing = build("morning", provider=FakeProvider({code: None}), after=1)
        self.assertEqual(quotes(missing)[code]["error_code"], "NO_QUOTE")
        self.assertEqual(missing["market"]["status"], "partial"
                         if len(trial.PUBLIC_SYMBOLS) > 1 else "unavailable")
        boom = FakeProvider({code: lambda ignored: (_ for _ in ()).throw(RuntimeError(SECRET))})
        broken = build("morning", provider=boom, after=1)
        self.assertEqual(quotes(broken)[code]["error_code"], "PROVIDER_UNAVAILABLE")
        for payload in (missing, broken):
            encoded = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn(SECRET, encoded)
            self.assertNotIn(SECRET, trial.render_report(payload))

    def test_unavailable_and_partial_market_statuses(self):
        codes = list(trial.PUBLIC_SYMBOLS)
        empty = build("morning", provider=FakeProvider({code: None for code in codes}), after=1)
        self.assertEqual(empty["market"]["status"], "unavailable")
        for quote in empty["market"]["quotes"]:
            self.assertEqual(quote["error_code"], "NO_QUOTE")
            self.assertIsNone(quote["price"])
            self.assertIsNone(quote["change_pct"])
        self.assertIn("公开行情暂不可用", trial.render_report(empty))
        if len(codes) > 1:
            partial = build("morning", provider=FakeProvider({codes[0]: None}), after=1)
            self.assertEqual(partial["market"]["status"], "partial")
            self.assertEqual(quotes(partial)[codes[0]]["error_code"], "NO_QUOTE")
            self.assertEqual(quotes(partial)[codes[1]]["error_code"], None)

    def test_unsafe_quote_values_become_invalid_quote(self):
        code = first_symbol()
        cases = {
            "wrong_code": quote_for("UNKNOWN-000000"),
            "bool_price": quote_for(code, price=True),
            "bool_change": quote_for(code, change=False),
            "nan_price": quote_for(code, price=float("nan")),
            "inf_price": quote_for(code, price=float("inf")),
            "inf_change": quote_for(code, change=float("inf")),
            "zero_price": quote_for(code, price=0.0),
            "negative_price": quote_for(code, price=-1.5),
            "string_price": quote_for(code, price="3210.5"),
            "none_change": quote_for(code, change=None),
            "not_a_quote": object(),
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                payload = build("morning", provider=FakeProvider({code: value}), after=1)
                quote = quotes(payload)[code]
                self.assertEqual(quote["error_code"], "INVALID_QUOTE")
                self.assertEqual(quote["name"], trial.PUBLIC_SYMBOLS[code])
                self.assertNotIn(SECRET, json.dumps(payload, ensure_ascii=False))

    def test_provider_metadata_never_reaches_json_or_report(self):
        payload = scheduled()
        encoded = json.dumps(payload, ensure_ascii=False)
        rendered = trial.render_report(payload)
        self.assertEqual(payload["market"]["source"], "sina_public")
        for sentinel in (SECRET, SECRET_NAME):
            with self.subTest(sentinel=sentinel):
                self.assertNotIn(sentinel, encoded)
                self.assertNotIn(sentinel, rendered)
        for forbidden in ("market_time", '"timestamp"'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, encoded)
        for quote in payload["market"]["quotes"]:
            with self.subTest(code=quote["code"]):
                self.assertNotIn("source", quote)
                self.assertNotIn("timestamp", quote)
                self.assertNotIn("market_time", quote)

    def test_invalid_stage_mode_or_dates_reject_before_any_fetch(self):
        naive = datetime(2026, 9, 14, 9, 0)
        cases = [
            {"stage": "evening"}, {"stage": ""}, {"stage": None}, {"stage": True},
            {"mode": "live"}, {"mode": "scheduled_realtime"}, {"mode": None},
            {"requested_at": naive}, {"requested_at": "2026-09-14T09:00:00+08:00"},
            {"generated_at": naive}, {"generated_at": slot("morning") - timedelta(minutes=1)},
        ]
        for case in cases:
            with self.subTest(case=case):
                provider = FakeProvider()
                global_provider = FakeGlobalProvider()
                fallback = FakeGlobalProvider()
                industry_provider = FakeIndustryProvider()
                with self.assertRaises(ValueError):
                    trial.build_snapshot(
                        case.get("stage", "morning"), provider=provider,
                        global_provider=global_provider, treasury_fallback=fallback,
                        industry_provider=industry_provider,
                        requested_at=case.get("requested_at", slot("morning")),
                        generated_at=case.get("generated_at", slot("morning")),
                        execution_mode=case.get("mode", "manual_replay"))
                self.assertEqual(provider.calls, [])
                self.assertEqual(global_provider.calls, [])
                self.assertEqual(fallback.calls, [])
                self.assertEqual(industry_provider.calls, [])

    def test_default_generated_at_is_measured_after_fetching(self):
        observed = []
        before = slot("morning") - timedelta(seconds=5)
        after_fetch = slot("morning") + timedelta(seconds=3)

        class ObservedProvider(FakeProvider):
            def fetch(self, code):
                result = super().fetch(code)
                observed.append(code)
                return result

        def generated_after_collection():
            self.assertTrue(observed)
            return after_fetch

        with patch.object(trial, "_now", generated_after_collection):
            payload = trial.build_snapshot("morning", provider=ObservedProvider(), requested_at=before)
        generated = datetime.fromisoformat(payload["generated_at"])
        self.assertEqual(set(observed), set(trial.PUBLIC_SYMBOLS))
        self.assertEqual(generated, after_fetch)
        self.assertGreaterEqual(generated, datetime.fromisoformat(payload["requested_at"]))

    def test_explicit_falsy_generated_at_rejects_before_fetch(self):
        for invalid_time in (False, 0, "", [], {}):
            with self.subTest(invalid_time=invalid_time):
                provider = FakeProvider()
                with self.assertRaises(ValueError):
                    trial.build_snapshot(
                        "morning", provider=provider, requested_at=slot("morning"),
                        generated_at=invalid_time,
                    )
                self.assertEqual(provider.calls, [])


class TimingTests(unittest.TestCase):
    def test_manual_replay_is_always_explicitly_manual(self):
        for stage in trial.STAGES:
            with self.subTest(stage=stage):
                payload = build(stage, mode="manual_replay", after=1)
                self.assertEqual(payload["execution_mode"], "manual_replay")
                self.assertEqual(payload["timing_status"], "manual_replay")
                trial.validate_snapshot(payload)

    def test_scheduled_timing_status_boundaries(self):
        cases = (
            ("on_time_at_slot", 0, 0), ("on_time_at_deadline", 0, 15),
            ("late_completion", 0, 16), ("late_start", 20, 25), ("early_start", -10, 5),
        )
        for name, start, finish in cases:
            with self.subTest(name=name):
                requested = slot("morning") + timedelta(minutes=start)
                payload = trial.build_snapshot(
                    "morning", provider=FakeProvider(), requested_at=requested,
                    generated_at=requested + timedelta(minutes=finish),
                    execution_mode="scheduled")
                expected = ("on_time" if start == 0 and finish <= 15 else
                            "early" if start < 0 else "late")
                self.assertEqual(payload["timing_status"], expected)
                trial.validate_snapshot(payload)

    def test_weekend_is_non_weekday_not_a_trading_day(self):
        payload = build("morning", day=SATURDAY, after=1)
        self.assertEqual(payload["timing_status"], "non_weekday")
        self.assertIs(payload["real_account_trial_day"], False)

    def test_cross_midnight_completion_keeps_scenario_date(self):
        payload = trial.build_snapshot(
            "closing", provider=FakeProvider(), requested_at=slot("closing"),
            generated_at=MONDAY + timedelta(hours=24, minutes=5), execution_mode="scheduled")
        self.assertTrue(payload["requested_at"].startswith("2026-09-14"))
        self.assertTrue(payload["scheduled_for"].startswith("2026-09-14"))
        self.assertTrue(payload["generated_at"].startswith("2026-09-15"))
        self.assertEqual(payload["timing_status"], "late")
        trial.validate_snapshot(payload)

    def test_every_stage_publishes_wait_without_acceptance_day(self):
        for stage in trial.STAGES:
            with self.subTest(stage=stage):
                payload = build(stage, after=1)
                self.assertEqual(payload["stage"], stage)
                self.assertEqual(payload["decision"]["action"], "WAIT")
                self.assertEqual(payload["decision"]["position_change_pct"], 0)
                self.assertIsNone(payload["decision"]["score"])
                self.assertIs(payload["real_account_trial_day"], False)
                trial.validate_snapshot(payload)


class ValidationTests(unittest.TestCase):
    def assert_rejected(self, edits, base=None):
        payload = tamper(scheduled() if base is None else base, *edits)
        with self.assertRaises(ValueError):
            trial.validate_snapshot(payload)

    def test_validator_accepts_generated_payloads(self):
        self.assertIsNone(trial.validate_snapshot(manual()))
        self.assertIsNone(trial.validate_snapshot(scheduled()))

    def test_validator_rejects_extra_fields_at_every_level(self):
        paths = {
            "top": ("extra_field",), "market": ("market", "extra_field"),
            "coverage": ("coverage", "extra_field"),
            "account": ("synthetic_account", "extra_field"),
            "position": ("synthetic_account", "positions", 0, "extra_field"),
            "decision": ("decision", "extra_field"), "quote": ("market", "quotes", 0, "extra_field"),
        }
        for name, path in paths.items():
            with self.subTest(name=name):
                self.assert_rejected([(path, 1)])

    def test_validator_rejects_privacy_sneaking_and_wrong_classification(self):
        cases = {
            "account": [("account", "private-account")],
            "api_token": [("api_token", SECRET)],
            "free_text": [("notes", "raw provider body")],
            "private_classification": [("data_classification", "private_account_data")],
            "public_classification": [("data_classification", "public")],
            "missing_classification": [(("data_classification",), None)],
            "purpose": [("purpose", "investment_advice")],
            "schema": [("schema_version", "public-trial/v4")],
            "mode": [("execution_mode", "automatic")],
            "quote_provider": [("market", "quotes", 0, "provider", SECRET)],
        }
        for name, edits in cases.items():
            with self.subTest(name=name):
                if name == "missing_classification":
                    payload = tamper(scheduled())
                    payload.pop("data_classification", None)
                    with self.assertRaises(ValueError):
                        trial.validate_snapshot(payload)
                else:
                    self.assert_rejected(edits)

    def test_validator_rejects_fake_timestamps_and_freshness(self):
        cases = {
            "source_as_of": [("market", "quotes", 0, "source_as_of", "2026-09-14T09:31:00+08:00")],
            "freshness": [("market", "quotes", 0, "freshness", "REALTIME")],
            "naive_requested": [("requested_at", "2026-09-14T09:00:00")],
            "naive_generated": [("generated_at", "2026-09-14T09:01:00")],
            "utc_offset": [("requested_at", "2026-09-14T01:00:00+00:00")],
            "scheduled_date": [("scheduled_for", "2026-09-15T09:00:00+08:00")],
            "generated_before_requested": [requested_after_generated],
        }
        for name, edits in cases.items():
            with self.subTest(name=name):
                self.assert_rejected(edits)

    def test_validator_rejects_inconsistent_timing_status(self):
        for status in ("late", "early", "non_weekday", "manual_replay", "unknown", 1, None):
            with self.subTest(status=status):
                self.assert_rejected([("timing_status", status)])
        self.assert_rejected([("timing_status", "on_time")], base=manual())

    def test_validator_rejects_advice_or_altered_synthetic_account(self):
        cases = {
            "action_buy": [("decision", "action", "BUY")],
            "action_add": [("decision", "action", "ADD")],
            "position_change": [("decision", "position_change_pct", 5)],
            "score": [("decision", "score", 1.0)],
            "auto_trade": [("decision", "auto_trade_enabled", True)],
            "no_confirmation": [("decision", "manual_confirmation_required", False)],
            "real_day": [("real_account_trial_day", True)],
            "position_symbol": [("synthetic_account", "positions", 0, "symbol", "REAL_ACCOUNT")],
            "position_weight": [("synthetic_account", "positions", 0, "weight_pct", 90)],
            "cash": [("synthetic_account", "cash_pct", 10)],
            "account_id": [("synthetic_account", "id", "REAL-ACCOUNT-001")],
            "extra_position": [lambda payload: payload["synthetic_account"]["positions"].append(
                {"symbol": "DEMO_C", "weight_pct": 1})],
            "coverage_upgrade": [("coverage", "global_markets", "READY")],
            "market_source": [("market", "source", "tushare_private")],
            "market_status": [("market", "status", "verified")],
        }
        for name, edits in cases.items():
            with self.subTest(name=name):
                self.assert_rejected(edits)

    def test_validator_accepts_exact_v1_but_rejects_v2_fields_in_v1(self):
        old = legacy()
        self.assertIsNone(trial.validate_snapshot(old))

        polluted = legacy()
        polluted["global_market"] = scheduled()["global_market"]
        with self.assertRaises(ValueError):
            trial.validate_snapshot(polluted)

    def test_validator_rejects_v2_global_shape_order_status_and_trade_pollution(self):
        mutations = {
            "missing_top": lambda item: item.pop("global_market"),
            "extra_top": lambda item: item.__setitem__("global_extra", 1),
            "missing_record_field": lambda item: item["global_market"]["quotes"][0].pop("source"),
            "extra_record_field": lambda item: item["global_market"]["quotes"][0].__setitem__("raw", SECRET),
            "reordered_records": lambda item: item["global_market"]["quotes"].reverse(),
            "false_status": lambda item: item["global_market"].__setitem__("status", "complete-ish"),
            "trade": lambda item: item["decision"].__setitem__("action", "ADD"),
            "auto_trade": lambda item: item["decision"].__setitem__("auto_trade_enabled", True),
            "arbitrary_error": lambda item: item["global_market"]["quotes"][0].update({
                "value": None, "previous_value": None, "change": None, "source": None,
                "source_as_of": None, "session_date": None, "freshness": None,
                "error_code": SECRET,
            }),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                payload = scheduled()
                mutate(payload)
                with self.assertRaises(ValueError):
                    trial.validate_snapshot(payload)

    def test_render_report_validates_before_rendering(self):
        payload = tamper(manual(), ("decision", "action", "BUY"))
        with self.assertRaises(ValueError):
            trial.render_report(payload)

    def test_rendered_report_keeps_required_labels(self):
        rendered = trial.render_report(build("closing", after=1))
        self.assertIsInstance(rendered, str)
        for label in LABELS:
            self.assertIn(label, rendered)

    def test_rendered_report_humanizes_runtime_metadata_for_notifications(self):
        rendered = trial.render_report(manual())

        self.assertIn("北京时间：2026年9月14日 09:01", rendered)
        self.assertIn("本次模式：手动演练", rendered)
        self.assertIn("国内指数状态：公开行情已获取，来源时间待核验", rendered)
        self.assertIn("【当前建议】", rendered)
        self.assertIn("WAIT｜暂不操作", rendered)
        self.assertIn("【风险说明】", rendered)
        self.assertNotIn("#", rendered)
        for internal_value in ("manual_replay", "available_unverified", "sina_public"):
            with self.subTest(internal_value=internal_value):
                self.assertNotIn(internal_value, rendered)

    def test_rendered_report_humanizes_scheduled_timing_status(self):
        rendered = trial.render_report(build("morning", minutes=20, after=1))

        self.assertIn("本次模式：定时演练（延迟完成）", rendered)
        self.assertNotIn("scheduled", rendered)
        self.assertNotIn("late", rendered)


    def test_v2_morning_renders_eight_human_readable_global_observations(self):
        yahoo = FakeGlobalProvider({
            "^GSPC": SourceObservation("^GSPC", 5432.1, 5400.0,
                "2026-09-14T00:00:00+00:00", "2026-09-13", "yahoo_chart"),
            "^TNX": lambda _symbol: (_ for _ in ()).throw(RuntimeError(SECRET)),
            "GC=F": SourceObservation("GC=F", 2612.3, 2600.0,
                "2026-09-14T00:00:00+00:00", "2026-09-13", "yahoo_chart"),
            "CL=F": SourceObservation("CL=F", 68.25, 67.5,
                "2026-09-14T00:00:00+00:00", "2026-09-13", "yahoo_chart"),
        })
        fred = FakeGlobalProvider({
            "^TNX": SourceObservation("^TNX", 4.61, 4.60, None,
                                      "2026-09-13", "fred_dgs10"),
        })
        rendered = trial.render_report(build(global_provider=yahoo, treasury_fallback=fred))
        self.assertIn("🌍 隔夜全球市场", rendered)
        self.assertIn("标普500：5,432.10 点；较前值 +0.59%；截至 09-14 08:00；数据较新", rendered)
        self.assertIn("美国10年期国债收益率：4.61%；较前值 +1.0 bp；数据日 09-13", rendered)
        self.assertIn("COMEX黄金：2,612.30 美元/盎司", rendered)
        self.assertIn("WTI原油：68.25 美元/桶", rendered)
        for entry in INSTRUMENTS.values():
            self.assertEqual(rendered.count(f"- {entry.name}："), 1)
        for forbidden in ("yahoo_chart", "fred_dgs10", "RECENT", "STALE",
                          "DELAYED_OR_HOLIDAY", "error_code", "manual_replay",
                          "available_unverified", "ADD", "BUY"):
            self.assertNotIn(forbidden, rendered)
        self.assertIn("WAIT｜暂不操作", rendered)
        self.assertIn("人工确认", rendered)
        self.assertIn("国内指数行情源未提供可靠时间戳", rendered)
        self.assertNotIn("\n行情源未提供可靠时间戳", rendered)

    def test_global_partial_and_unavailable_are_readable_and_redacted(self):
        partial = build(global_provider=FakeGlobalProvider({
            "^SOX": lambda _symbol: (_ for _ in ()).throw(RuntimeError(SECRET)),
        }))
        text = trial.render_report(partial)
        self.assertIn("完整性：部分全球行情暂不可用，其余项目照常展示。", text)
        self.assertIn("- 费城半导体指数：暂不可用", text)
        self.assertNotIn("PROVIDER_UNAVAILABLE", text)
        unavailable = trial.render_report(build(
            global_provider=FailingGlobalProvider(), treasury_fallback=FailingGlobalProvider()))
        self.assertIn("完整性：全球行情暂不可用。", unavailable)
        self.assertEqual(unavailable.count("：暂不可用"), len(INSTRUMENTS))

    def test_legacy_v1_morning_still_says_global_market_is_not_connected(self):
        rendered = trial.render_report(legacy())
        self.assertIn("隔夜全球市场尚未接入", rendered)
        self.assertNotIn("🌍 隔夜全球市场", rendered)


class IndustryRankingIntegrationTests(unittest.TestCase):
    def test_morning_snapshot_industry_ranking_is_ready_and_valid(self):
        payload = build("morning")
        industries = payload["industries"]
        self.assertEqual(industries["status"], "READY")
        items = industries["items"]
        self.assertGreater(len(items), 0)
        self.assertEqual(set(items[0]), set(trial._INDUSTRY_ITEM_FIELDS))
        self.assertEqual([i["rank"] for i in items], list(range(1, len(items) + 1)))
        self.assertIsInstance(items[0]["score"], float)
        self.assertTrue(industries["as_of"].endswith("+08:00"))

    def test_non_morning_stages_do_not_collect_industry_data(self):
        for stage in ("midday", "decision", "closing"):
            with self.subTest(stage=stage):
                provider = FakeIndustryProvider()
                payload = build(stage, industry_provider=provider)
                self.assertEqual(payload["industries"], trial._industry_not_collected())
                self.assertEqual(provider.calls, [])

    def test_industry_provider_exception_fails_closed(self):
        failing = FakeIndustryProvider(raises=True)
        payload = build("morning", industry_provider=failing)
        self.assertIsNone(trial.validate_snapshot(payload))
        industries = payload["industries"]
        self.assertEqual(industries["status"], "FAILED")
        self.assertEqual(industries["error"], "PROVIDER_UNAVAILABLE")
        self.assertEqual(industries["items"], [])
        self.assertIsNone(industries["as_of"])

    def test_industry_provider_empty_observations_fails_closed_and_renders(self):
        empty_prov = FakeIndustryProvider(empty=True)
        payload = build("morning", industry_provider=empty_prov)
        self.assertIsNone(trial.validate_snapshot(payload))
        industries = payload["industries"]
        self.assertEqual(industries["status"], "FAILED")
        self.assertEqual(industries["error"], "NO_USABLE_DATA")
        self.assertEqual(industries["items"], [])
        rendered = trial.render_report(payload)
        self.assertIn("行业数据不足或已过期", rendered)

    def test_validator_rejects_tampered_industry_payloads(self):
        extra_key = tamper(scheduled(), lambda p: p["industries"]["items"][0].__setitem__("extra", 123))
        with self.assertRaises(ValueError):
            trial.validate_snapshot(extra_key)

        scrambled_rank = tamper(scheduled(), lambda p: p["industries"]["items"].reverse())
        with self.assertRaises(ValueError):
            trial.validate_snapshot(scrambled_rank)

        invalid_err = tamper(scheduled(), lambda p: p["industries"].update({"status": "FAILED", "as_of": None, "error": "UNKNOWN_ERROR", "items": []}))
        with self.assertRaises(ValueError):
            trial.validate_snapshot(invalid_err)

        ready_empty = tamper(scheduled(), lambda p: p["industries"].update({"status": "READY", "items": []}))
        with self.assertRaises(ValueError):
            trial.validate_snapshot(ready_empty)

        failed_with_as_of = tamper(scheduled(), lambda p: p["industries"].update({"status": "FAILED", "as_of": "2026-09-14T08:30:00+08:00", "error": "PROVIDER_UNAVAILABLE", "items": []}))
        with self.assertRaises(ValueError):
            trial.validate_snapshot(failed_with_as_of)

        empty_code = tamper(scheduled(), lambda p: p["industries"]["items"][0].__setitem__("code", ""))
        with self.assertRaises(ValueError):
            trial.validate_snapshot(empty_code)

    def test_render_report_industry_ready_and_failed_branches(self):
        ready_payload = build("morning")
        rendered_ready = trial.render_report(ready_payload)
        top_name = ready_payload["industries"]["items"][0]["name"]
        self.assertIn("🔥 今日重点行业", rendered_ready)
        self.assertIn(top_name, rendered_ready)
        self.assertIn("评分：", rendered_ready)

        failed_payload = build("morning", industry_provider=FakeIndustryProvider(raises=True))
        rendered_failed = trial.render_report(failed_payload)
        self.assertIn(trial._INDUSTRY_CAUSE_LABELS["PROVIDER_UNAVAILABLE"], rendered_failed)
        for item in ready_payload["industries"]["items"]:
            self.assertNotIn(item["name"], rendered_failed)

    def test_legacy_v1_compatibility_with_industry_ranking(self):
        old = legacy()
        self.assertIsNone(trial.validate_snapshot(old))
        self.assertEqual(old["coverage"]["industry_ranking"], "NOT_IMPLEMENTED")
        self.assertNotIn("industries", old)


class WriteTests(unittest.TestCase):
    def batch(self):
        return [build(stage, after=1) for stage in trial.STAGES]

    def test_write_reports_creates_readme_and_latest_snapshots(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            trial.write_reports(self.batch(), output)
            readme = (output / "README.md").read_text(encoding="utf-8")
            for label in LABELS:
                self.assertIn(label, readme)
            self.assertNotIn(SECRET, readme)
            for stage in trial.STAGES:
                path = output / "latest" / f"{stage}.json"
                self.assertTrue(path.is_file())
                payload = json.loads(path.read_text(encoding="utf-8"))
                trial.validate_snapshot(payload)
                self.assertEqual(payload["stage"], stage)

    def test_mixed_v1_v2_reports_are_read_and_rendered_without_mutation(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            old = legacy(build("morning"))
            before = json.dumps(old, ensure_ascii=False, sort_keys=True)
            (output / "latest").mkdir(parents=True)
            (output / "latest" / "morning.json").write_text(
                json.dumps(old, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            trial.write_reports([build("midday")], output)

            loaded = trial._read_reports(output)
            self.assertEqual([item["schema_version"] for item in loaded],
                             ["public-trial/v1", "public-trial/v3"])
            persisted = json.loads((output / "latest" / "morning.json").read_text(encoding="utf-8"))
            self.assertEqual(json.dumps(persisted, ensure_ascii=False, sort_keys=True), before)
            self.assertNotIn("global_market", persisted)
            self.assertIn("尚未接入", (output / "README.md").read_text(encoding="utf-8"))

    def test_write_reports_refuses_legacy_snapshots_as_new_input(self):
        with TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                trial.write_reports([legacy()], Path(directory) / "out")

    def test_write_reports_validates_the_whole_batch_before_writing(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            batch = self.batch()
            tamper(batch[2], ("decision", "action", "BUY"))
            with self.assertRaises(ValueError):
                trial.write_reports(batch, output)
            self.assertFalse((output / "README.md").exists())
            self.assertFalse((output / "latest" / "morning.json").exists())

    def test_write_reports_rejects_symlinks_in_output(self):
        for name in ("latest", "README.md"):
            with self.subTest(name=name), TemporaryDirectory() as directory:
                root = Path(directory)
                output = root / "out"
                output.mkdir()
                outside = root / "outside.txt"
                outside.write_text("preserve", encoding="utf-8")
                try:
                    if name == "latest":
                        os.symlink(root, output / "latest", target_is_directory=True)
                    else:
                        os.symlink(outside, output / "README.md")
                except (OSError, NotImplementedError) as error:
                    self.skipTest(f"symlinks unavailable: {error}")
                with self.assertRaises(ValueError):
                    trial.write_reports(self.batch(), output)
                self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")

    def test_write_reports_rejects_unsafe_output_structure(self):
        with TemporaryDirectory() as directory:
            as_file = Path(directory) / "not-a-directory"
            as_file.write_text("x", encoding="utf-8")
            with self.assertRaises(ValueError):
                trial.write_reports(self.batch(), as_file)
        with TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            output.mkdir()
            (output / "latest").write_text("x", encoding="utf-8")
            with self.assertRaises(ValueError):
                trial.write_reports(self.batch(), output)
        with TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            (output / "README.md").mkdir(parents=True)
            with self.assertRaises(ValueError):
                trial.write_reports(self.batch(), output)

    def test_frozen_v2_published_shape_still_validates(self):
        self.assertIsNone(trial.validate_snapshot(frozen_v2()))
        self.assertNotIn("industries", frozen_v2())

    def test_summary_migrates_a_data_branch_of_frozen_v2_snapshots(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            (output / "latest").mkdir(parents=True)
            for stage in trial.STAGES:
                payload = frozen_v2(build(stage))
                payload["stage"] = stage
                (output / "latest" / f"{stage}.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
            self.assertEqual(trial.main(["summary", "--input", str(output)]), 0)
            readme = (output / "README.md").read_text(encoding="utf-8")
            self.assertIn("尚未接入", readme)
            for token in ("manual_replay", "available_unverified", "public-trial/v2"):
                self.assertNotIn(token, readme)

    def test_frozen_v2_is_refused_as_new_write_input(self):
        with TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                trial.write_reports([frozen_v2()], Path(directory) / "out")


class ImportIsolationTests(unittest.TestCase):
    def test_import_does_not_touch_private_modules_or_config(self):
        probe = textwrap.dedent(f"""
            import sys
            BLOCKED = {BLOCKED_MODULES!r}
            class Blocker:
                def find_spec(self, name, path=None, target=None):
                    if any(name == item or name.startswith(item + ".") for item in BLOCKED):
                        raise RuntimeError("blocked private import: " + name)
                    return None
            sys.meta_path.insert(0, Blocker())
            from app.integration import public_trial
            leaked = sorted(item for item in BLOCKED if item in sys.modules)
            print("leaked", leaked)
            """)
        completed = subprocess.run(
            [sys.executable, "-c", probe], cwd=ROOT,
            env=dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8"),
            capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("leaked []", completed.stdout)


if __name__ == "__main__":
    unittest.main()
