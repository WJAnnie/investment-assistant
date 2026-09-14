import json
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.portfolio.models import (
    AccountConfig,
    AccountSnapshot,
    HoldingConfig,
    HoldingSnapshot,
    PortfolioSnapshot,
    Valuation,
)
from app.workflow.stage_evidence import (
    build_stage_evidence,
    compare_stage_evidence,
    evaluate_forecasts,
    _fingerprint,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
MORNING = datetime(2026, 9, 15, 9, 0, tzinfo=SHANGHAI)
MIDDAY = datetime(2026, 9, 15, 11, 30, tzinfo=SHANGHAI)
CLOSING = datetime(2026, 9, 15, 16, 10, tzinfo=SHANGHAI)
CN_CLOSE = datetime(2026, 9, 15, 15, 0, tzinfo=SHANGHAI)


def _holding_config(
    account_id,
    code,
    *,
    quantity="100",
    cost="8",
    baseline_price="9",
    market="CN",
    mode="exchange",
):
    return HoldingConfig(
        code=code,
        name=f"{account_id}-{code}",
        market=market,
        instrument_type="stock",
        valuation_mode=mode,
        cost_price=Decimal(cost),
        baseline_value=Decimal("900"),
        sector="synthetic",
        theme="fixture",
        quantity=Decimal(quantity) if quantity is not None else None,
        baseline_price=Decimal(baseline_price),
    )


def _holding_snapshot(
    account_id,
    code,
    *,
    price="10",
    quantity="100",
    cost="8",
    source="fixture",
    precision="exact",
    as_of=None,
    freshness="fresh",
):
    holding = _holding_config(account_id, code, quantity=quantity, cost=cost)
    current_price = Decimal(price) if price is not None else None
    valuation = None
    if current_price is not None:
        valuation = Valuation(
            code=code,
            price=current_price,
            change_percent=Decimal("1.2"),
            as_of=as_of or MORNING,
            source=source,
            freshness=freshness,
        )
    market_value = (
        Decimal("900")
        if current_price is None or quantity is None
        else Decimal(quantity) * current_price
    )
    return HoldingSnapshot(
        account_id=account_id,
        holding=holding,
        current_price=current_price,
        market_value=market_value,
        account_weight=Decimal("0.5"),
        return_from_cost=Decimal("0.25") if current_price is not None else None,
        monetary_profit=Decimal("200") if current_price is not None else None,
        value_precision=precision,
        valuation=valuation,
    )


def _account(account_id, holdings):
    holdings = tuple(holdings)
    return AccountSnapshot(
        account=AccountConfig(
            account_id=account_id,
            name=f"{account_id} account",
            strategy="fixture",
            baseline_date=date(2026, 9, 15),
            total_assets=Decimal("10000"),
            cash=Decimal("1000"),
            holdings=tuple(item.holding for item in holdings),
        ),
        holdings=holdings,
        holdings_value=sum((item.market_value for item in holdings), Decimal("0")),
        cash=Decimal("1000"),
        total_assets=Decimal("10000"),
        position_percent=Decimal("0.5"),
    )


def _snapshot(holdings):
    accounts = {}
    for holding in holdings:
        accounts.setdefault(holding.account_id, []).append(holding)
    account_snapshots = tuple(
        _account(account_id, account_holdings)
        for account_id, account_holdings in sorted(accounts.items())
    )
    return PortfolioSnapshot(
        accounts=account_snapshots,
        total_assets=Decimal("20000"),
        holdings_value=sum(
            (item.market_value for account in account_snapshots for item in account.holdings),
            Decimal("0"),
        ),
        cash=Decimal("2000"),
        position_percent=Decimal("0.5"),
    )


def _analysis():
    return {
        "items": (
            {
                "account_id": "A",
                "code": "DUP",
                "technical": {"score": 63},
                "decision": {"score": 99},
            },
            {
                "account_id": "B",
                "code": "DUP",
                "technical": {"score": 71},
            },
        )
    }


def _evidence(
    snapshot,
    cutoff=MORNING,
    rule_version="rules-1",
    analysis=None,
    report_kind="morning",
    generated_at=None,
):
    return build_stage_evidence(
        snapshot,
        analysis if analysis is not None else _analysis(),
        report_kind,
        cutoff,
        generated_at or cutoff,
        rule_version,
    )


class StageEvidenceTests(unittest.TestCase):
    def test_rehashed_malformed_scores_fail_closed_without_crashing(self):
        morning = _evidence(_snapshot((_holding_snapshot("A", "DUP"),)))
        current = _evidence(_snapshot((_holding_snapshot("A", "DUP", as_of=MIDDAY),)),
                            MIDDAY, report_kind="midday")
        for scores in (None, {}, [None], [{"account_id": "A", "code": "DUP", "score": True}]):
            with self.subTest(scores=scores):
                invalid = {**morning, "technical_scores": scores}
                invalid["evidence_fingerprint"] = _fingerprint({
                    key: value for key, value in invalid.items() if key != "evidence_fingerprint"})
                self.assertEqual(compare_stage_evidence(current, invalid)["status"], "uncomparable")

    def test_mainland_symbol_with_hk_exposure_does_not_claim_hk_close(self):
        holding = _holding_snapshot("A", "510999", as_of=CN_CLOSE)
        holding = replace(holding, holding=replace(holding.holding, market="HK", instrument_type="etf"))
        closing = _evidence(_snapshot((holding,)), CLOSING, report_kind="closing")
        self.assertFalse(closing["valid"])
        self.assertIn("listing_venue_unverified", closing["holdings"][0]["input_reasons"])

    def test_malformed_loaded_evidence_degrades_without_crashing(self):
        morning = _evidence(_snapshot((_holding_snapshot("A", "DUP"),)))
        current = _evidence(
            _snapshot((_holding_snapshot("A", "DUP", as_of=MIDDAY),)),
            MIDDAY, report_kind="midday",
        )
        for invalid in (None, [], "bad", {}, {**morning, "holdings": None},
                        {**morning, "holdings": [None]},
                        {**morning, "cutoff": "bad"},
                        {**morning, "report_kind": []},
                        {**morning, "holdings": [{**morning["holdings"][0], "quantity": object()}]}):
            with self.subTest(invalid=type(invalid).__name__):
                self.assertEqual(compare_stage_evidence(current, invalid)["status"], "uncomparable")
                self.assertEqual(compare_stage_evidence(invalid, morning)["status"], "uncomparable")
                self.assertEqual(evaluate_forecasts(({},), invalid)["status"], "unable_to_evaluate")

    def test_loaded_values_and_dates_are_revalidated_even_when_flags_claim_valid(self):
        morning = _evidence(_snapshot((_holding_snapshot("A", "DUP"),)))
        current = _evidence(
            _snapshot((_holding_snapshot("A", "DUP", as_of=MIDDAY),)),
            MIDDAY, report_kind="midday",
        )
        for field, value in (("price", "0"), ("price", "NaN"), ("quantity", "Infinity"),
                             ("valuation_source", ""), ("valuation_as_of", None),
                             ("valuation_as_of", "2026-09-15T12:00:00+08:00"),
                             ("valuation_price", "99")):
            invalid = deepcopy(current)
            invalid["holdings"][0][field] = value
            invalid["holdings"][0]["input_reasons"] = []
            invalid["holdings"][0]["input_status"] = "valid"
            with self.subTest(field=field, value=value):
                self.assertEqual(compare_stage_evidence(invalid, morning)["status"], "uncomparable")
        invalid = {**current, "trading_date": "2026-09-14"}
        self.assertEqual(compare_stage_evidence(invalid, morning)["status"], "uncomparable")

    def test_build_handles_bad_observation_time_quantity_and_price_mismatch(self):
        good = _holding_snapshot("A", "DUP")
        cases = (
            replace(good, valuation=replace(good.valuation, as_of=datetime(2026, 9, 15, 9))),
            replace(good, holding=replace(good.holding, quantity=Decimal("Infinity"))),
            replace(good, valuation=replace(good.valuation, price=Decimal("99"))),
        )
        for holding in cases:
            evidence = _evidence(_snapshot((holding,)))
            self.assertFalse(evidence["valid"])
            self.assertTrue(evidence["holdings"][0]["input_reasons"])

    def test_empty_holdings_and_old_intraday_quote_never_count_as_comparable(self):
        morning = _evidence(_snapshot((_holding_snapshot("A", "DUP"),)))
        current = _evidence(
            _snapshot((_holding_snapshot("A", "DUP", as_of=MORNING),)),
            MIDDAY, report_kind="midday",
        )
        self.assertFalse(current["valid"])
        self.assertEqual(compare_stage_evidence(current, morning)["status"], "uncomparable")
        empty = _evidence(_snapshot(()))
        self.assertFalse(empty["valid"])
        self.assertEqual(compare_stage_evidence(empty, empty)["status"], "uncomparable")

    def test_fingerprint_covers_observation_not_just_identity(self):
        before = _evidence(_snapshot((_holding_snapshot("A", "DUP", price="10"),)))
        after = _evidence(_snapshot((_holding_snapshot("A", "DUP", price="11"),)))
        self.assertEqual(before["holdings_fingerprint"], after["holdings_fingerprint"])
        self.assertNotEqual(before["evidence_fingerprint"], after["evidence_fingerprint"])

    def test_builds_private_primitive_baseline_for_all_holdings_and_explicit_technical_scores(self):
        snapshot = _snapshot((
            _holding_snapshot("A", "DUP", price="10"),
            _holding_snapshot("B", "DUP", price="20"),
            _holding_snapshot("C", "UNIQUE", price="30"),
            _holding_snapshot("D", "FOUR", price="40"),
            _holding_snapshot("E", "FIVE", price="50"),
        ))

        evidence = _evidence(snapshot)

        self.assertEqual(evidence["trading_date"], "2026-09-15")
        self.assertEqual(evidence["report_kind"], "morning")
        self.assertEqual(evidence["cutoff"], MORNING.isoformat())
        self.assertEqual(evidence["generated_at"], MORNING.isoformat())
        self.assertEqual(evidence["rule_version"], "rules-1")
        self.assertEqual(evidence["account_evidence_classification"], "private_account_data")
        self.assertEqual(len(evidence["holdings"]), 5)
        self.assertTrue(evidence["snapshot_fingerprint"])
        self.assertTrue(evidence["holdings_fingerprint"])
        self.assertEqual(
            [(item["account_id"], item["code"], item["price"]) for item in evidence["holdings"]],
            [
                ("A", "DUP", "10"),
                ("B", "DUP", "20"),
                ("C", "UNIQUE", "30"),
                ("D", "FOUR", "40"),
                ("E", "FIVE", "50"),
            ],
        )
        self.assertEqual(
            evidence["technical_scores"],
            [
                {
                    "account_id": "A",
                    "code": "DUP",
                    "label": "technical_score",
                    "score": 63,
                },
                {
                    "account_id": "B",
                    "code": "DUP",
                    "label": "technical_score",
                    "score": 71,
                },
            ],
        )
        self.assertNotIn("composite_score", evidence)
        json.dumps(evidence)

    def test_build_normalizes_trading_date_to_shanghai_and_rejects_bad_envelope_inputs(self):
        utc_cutoff = datetime(2026, 9, 14, 16, 30, tzinfo=timezone.utc)
        evidence = _evidence(
            _snapshot((_holding_snapshot("A", "DUP", price="10", as_of=utc_cutoff),)),
            utc_cutoff,
            generated_at=datetime(2026, 9, 14, 16, 31, tzinfo=timezone.utc),
        )

        self.assertEqual(evidence["trading_date"], "2026-09-15")
        self.assertEqual(evidence["cutoff"], utc_cutoff.astimezone(SHANGHAI).isoformat())
        self.assertEqual(
            evidence["generated_at"],
            datetime(2026, 9, 15, 0, 31, tzinfo=SHANGHAI).isoformat(),
        )

        for rule_version in ("", None):
            with self.subTest(rule_version=rule_version):
                with self.assertRaisesRegex(ValueError, "rule_version"):
                    _evidence(_snapshot((_holding_snapshot("A", "DUP"),)), rule_version=rule_version)

        with self.assertRaisesRegex(ValueError, "generated_at"):
            _evidence(
                _snapshot((_holding_snapshot("A", "DUP"),)),
                generated_at=datetime(2026, 9, 15, 8, 59, tzinfo=SHANGHAI),
            )

    def test_midday_compare_uses_morning_price_not_cumulative_cost_profit(self):
        morning = _evidence(_snapshot((_holding_snapshot("A", "DUP", price="10", cost="1"),)))
        current = _evidence(
            _snapshot((_holding_snapshot("A", "DUP", price="12", cost="1", as_of=MIDDAY),)),
            MIDDAY,
            report_kind="midday",
        )

        comparison = compare_stage_evidence(current, morning)

        self.assertEqual(comparison["status"], "comparable")
        self.assertEqual(comparison["holdings"][0]["price_change"], "2")
        self.assertEqual(comparison["holdings"][0]["change_label"], "since_morning_reference")
        self.assertEqual(comparison["holdings"][0]["value_change"], "200")
        self.assertNotEqual(comparison["holdings"][0]["value_change"], "1100")

    def test_midday_compare_rejects_cross_day_rule_quantity_precision_and_source_changes(self):
        morning = _evidence(_snapshot((_holding_snapshot("A", "DUP", price="10"),)))
        cases = (
            ("cross_day", _evidence(_snapshot((_holding_snapshot("A", "DUP", price="11", as_of=datetime(2026, 9, 16, 9, 0, tzinfo=SHANGHAI)),)), datetime(2026, 9, 16, 11, 30, tzinfo=SHANGHAI), "rules-1", report_kind="midday")),
            ("rule_version_changed", _evidence(_snapshot((_holding_snapshot("A", "DUP", price="11", as_of=MIDDAY),)), MIDDAY, "rules-2", report_kind="midday")),
            ("holding_identity_changed", _evidence(_snapshot((_holding_snapshot("A", "DUP", price="11", quantity="120", as_of=MIDDAY),)), MIDDAY, report_kind="midday")),
            ("precision_changed", _evidence(_snapshot((_holding_snapshot("A", "DUP", price="11", precision="baseline", as_of=MIDDAY),)), MIDDAY, report_kind="midday")),
            ("source_changed", _evidence(_snapshot((_holding_snapshot("A", "DUP", price="11", source="other", as_of=MIDDAY),)), MIDDAY, report_kind="midday")),
        )

        for reason, current in cases:
            with self.subTest(reason=reason):
                comparison = compare_stage_evidence(current, morning)

                self.assertEqual(comparison["status"], "uncomparable")
                reasons = comparison["reasons"] + [
                    item_reason
                    for item in comparison["holdings"]
                    for item_reason in item["reasons"]
                ]
                self.assertIn(reason, reasons)
                self.assertTrue(all(item["status"] == "uncomparable" for item in comparison["holdings"]))

    def test_midday_compare_rejects_missing_late_future_naive_nonfinite_and_stale_inputs(self):
        morning = _evidence(_snapshot((_holding_snapshot("A", "DUP", price="10"),)))
        cases = (
            ("baseline_missing", None, morning),
            (
                "baseline_not_before_cutoff",
                _evidence(_snapshot((_holding_snapshot("A", "DUP", price="11", as_of=MIDDAY),)), MIDDAY, report_kind="midday"),
                _evidence(_snapshot((_holding_snapshot("A", "DUP", price="9", as_of=MIDDAY),)), MIDDAY),
            ),
            (
                "current_has_future_input",
                _evidence(_snapshot((_holding_snapshot("A", "DUP", price="11", as_of=datetime(2026, 9, 15, 12, 0, tzinfo=SHANGHAI)),)), MIDDAY, report_kind="midday"),
                morning,
            ),
            (
                "current_has_nonfinite_price",
                _evidence(_snapshot((_holding_snapshot("A", "DUP", price="NaN", as_of=MIDDAY),)), MIDDAY, report_kind="midday"),
                morning,
            ),
            (
                "current_has_stale_input",
                _evidence(_snapshot((_holding_snapshot("A", "DUP", price="11", as_of=MIDDAY, freshness="stale"),)), MIDDAY, report_kind="midday"),
                morning,
            ),
        )

        for reason, current, baseline in cases:
            with self.subTest(reason=reason):
                if current is None:
                    comparison = compare_stage_evidence(_evidence(_snapshot((_holding_snapshot("A", "DUP", price="11", as_of=MIDDAY),)), MIDDAY, report_kind="midday"), None)
                else:
                    comparison = compare_stage_evidence(current, baseline)

                self.assertEqual(comparison["status"], "uncomparable")
                reasons = comparison["reasons"] + [
                    item_reason
                    for item in comparison["holdings"]
                    for item_reason in item["reasons"]
                ]
                self.assertIn(reason, reasons)

        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            _evidence(
                _snapshot((_holding_snapshot("A", "DUP", price="10"),)),
                datetime(2026, 9, 15, 11, 30),
            )

    def test_compare_validates_loaded_envelope_fingerprints_and_rejects_duplicate_identities(self):
        morning = _evidence(_snapshot((_holding_snapshot("A", "DUP", price="10"),)))
        current = _evidence(
            _snapshot((_holding_snapshot("A", "DUP", price="11", as_of=MIDDAY),)),
            MIDDAY,
            report_kind="midday",
        )

        for key, reason in (
            ("schema", "baseline_schema_invalid"),
            ("account_evidence_classification", "baseline_classification_invalid"),
            ("report_kind", "baseline_report_kind_invalid"),
            ("holdings_fingerprint", "baseline_holdings_fingerprint_invalid"),
        ):
            loaded = dict(morning)
            loaded[key] = "tampered"
            with self.subTest(key=key):
                comparison = compare_stage_evidence(current, loaded)
                self.assertEqual(comparison["status"], "uncomparable")
                self.assertIn(reason, comparison["reasons"])

        duplicate_current = dict(current)
        duplicate_current["holdings"] = [current["holdings"][0], dict(current["holdings"][0])]
        comparison = compare_stage_evidence(duplicate_current, morning)
        self.assertEqual(comparison["status"], "uncomparable")
        self.assertIn("current_duplicate_identity", comparison["reasons"])

    def test_compare_reports_per_holding_incompatibility_and_allows_price_only_delta(self):
        morning = _evidence(_snapshot((
            _holding_snapshot("A", "GOOD", price="10"),
            _holding_snapshot("A", "FUND", price="10", quantity=None),
            _holding_snapshot("A", "BAD", price="10"),
        )))
        current = _evidence(
            _snapshot((
                _holding_snapshot("A", "GOOD", price="12", as_of=MIDDAY),
                _holding_snapshot("A", "FUND", price="11", quantity=None, as_of=MIDDAY),
                _holding_snapshot("A", "BAD", price="9", source="other", as_of=MIDDAY),
            )),
            MIDDAY,
            report_kind="midday",
        )

        comparison = compare_stage_evidence(current, morning)

        self.assertEqual(comparison["status"], "partial")
        by_code = {item["code"]: item for item in comparison["holdings"]}
        self.assertEqual(by_code["GOOD"]["status"], "comparable")
        self.assertEqual(by_code["GOOD"]["value_change"], "200")
        self.assertEqual(by_code["FUND"]["status"], "comparable")
        self.assertEqual(by_code["FUND"]["price_change"], "1")
        self.assertIsNone(by_code["FUND"]["value_change"])
        self.assertEqual(by_code["BAD"]["status"], "uncomparable")
        self.assertIn("source_changed", by_code["BAD"]["reasons"])

    def test_build_rejects_nonpositive_prices_quantities_missing_source_and_bad_freshness(self):
        evidence = _evidence(_snapshot((
            _holding_snapshot("A", "ZERO_PRICE", price="0"),
            _holding_snapshot("A", "ZERO_QTY", price="10", quantity="0"),
            _holding_snapshot("A", "NO_SOURCE", price="10", source=""),
            _holding_snapshot("A", "STALE", price="10", freshness="stale"),
            _holding_snapshot("A", "FAILED", price="10", freshness="failed"),
        )))

        by_code = {item["code"]: item for item in evidence["holdings"]}
        self.assertIn("nonpositive_price", by_code["ZERO_PRICE"]["input_reasons"])
        self.assertIn("nonpositive_quantity", by_code["ZERO_QTY"]["input_reasons"])
        self.assertIn("source_missing", by_code["NO_SOURCE"]["input_reasons"])
        self.assertIn("freshness_stale", by_code["STALE"]["input_reasons"])
        self.assertIn("freshness_failed", by_code["FAILED"]["input_reasons"])

    def test_closing_evaluates_only_prospective_explicit_forecasts(self):
        current = _evidence(
            _snapshot((_holding_snapshot("A", "DUP", price="12", as_of=CN_CLOSE),)),
            CLOSING,
            report_kind="closing",
        )
        target = {
            "account_id": "A",
            "code": "DUP",
            "market": "CN",
            "valuation_mode": "exchange",
            "valuation_source": "fixture",
            "holding_config_fingerprint": current["holdings"][0]["holding_config_fingerprint"],
        }
        forecasts = (
            {
                "target": target,
                "direction": "up",
                "start_price": "10",
                "start": MORNING.isoformat(),
                "cutoff": MORNING.isoformat(),
                "recorded_at": MORNING.isoformat(),
                "deadline": CN_CLOSE.isoformat(),
                "prospective": True,
                "explicit": True,
                "rule_version": "rules-1",
            },
            {
                "target": {**target, "code": "MISSING"},
                "direction": "down",
                "start_price": "10",
                "start": MORNING.isoformat(),
                "cutoff": MORNING.isoformat(),
                "recorded_at": MORNING.isoformat(),
                "deadline": CN_CLOSE.isoformat(),
                "prospective": True,
                "explicit": True,
                "rule_version": "rules-1",
            },
            {
                "target": target,
                "direction": "up",
                "start_price": "10",
                "start": CLOSING.isoformat(),
                "cutoff": CLOSING.isoformat(),
                "recorded_at": CLOSING.isoformat(),
                "deadline": CLOSING.isoformat(),
                "prospective": False,
                "explicit": False,
                "rule_version": "rules-1",
            },
            {
                "target": target,
                "direction": "up",
                "start_price": "10",
                "start": MORNING.isoformat(),
                "cutoff": MORNING.isoformat(),
                "recorded_at": MORNING.isoformat(),
                "deadline": datetime(2026, 9, 15, 14, 30, tzinfo=SHANGHAI).isoformat(),
                "prospective": True,
                "explicit": True,
                "rule_version": "rules-1",
            },
        )

        result = evaluate_forecasts(forecasts, current)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["evaluations"][0]["status"], "hit")
        self.assertEqual(result["evaluations"][0]["observed_change"], "2")
        self.assertEqual(result["evaluations"][1]["status"], "unable_to_evaluate")
        self.assertIn("observed_outcome_missing", result["evaluations"][1]["reasons"])
        self.assertEqual(result["evaluations"][2]["status"], "unable_to_evaluate")
        self.assertIn("not_prospective_explicit", result["evaluations"][2]["reasons"])
        self.assertEqual(result["evaluations"][3]["status"], "unable_to_evaluate")
        self.assertIn("future_observed_outcome", result["evaluations"][3]["reasons"])

    def test_forecast_waits_until_deadline_and_allows_late_review_with_endpoint_outcome(self):
        early = _evidence(
            _snapshot((_holding_snapshot("A", "DUP", price="12", as_of=MIDDAY),)),
            MIDDAY,
            report_kind="midday",
        )
        late = _evidence(
            _snapshot((_holding_snapshot("A", "DUP", price="12", as_of=CN_CLOSE),)),
            datetime(2026, 9, 15, 17, 0, tzinfo=SHANGHAI),
            report_kind="closing",
        )
        forecast = {
            "target": {
                "account_id": "A",
                "code": "DUP",
                "market": "CN",
                "valuation_mode": "exchange",
                "valuation_source": "fixture",
                "holding_config_fingerprint": late["holdings"][0]["holding_config_fingerprint"],
            },
            "direction": "up",
            "start_price": "10",
            "start": MORNING.isoformat(),
            "cutoff": MORNING.isoformat(),
            "recorded_at": MORNING.isoformat(),
            "deadline": CN_CLOSE.isoformat(),
            "prospective": True,
            "explicit": True,
            "rule_version": "rules-1",
        }

        early_result = evaluate_forecasts((forecast,), early)
        late_result = evaluate_forecasts((forecast,), late)

        self.assertEqual(early_result["evaluations"][0]["status"], "unable_to_evaluate")
        self.assertIn("deadline_not_reached", early_result["evaluations"][0]["reasons"])
        self.assertEqual(late_result["evaluations"][0]["status"], "hit")

    def test_forecast_requires_mapping_provenance_compatibility_and_valid_observation(self):
        current = _evidence(
            _snapshot((_holding_snapshot("A", "DUP", price="12", as_of=CN_CLOSE),)),
            CLOSING,
            report_kind="closing",
        )
        target = {
            "account_id": "A",
            "code": "DUP",
            "market": "CN",
            "valuation_mode": "exchange",
            "valuation_source": "fixture",
            "holding_config_fingerprint": current["holdings"][0]["holding_config_fingerprint"],
        }
        base = {
            "target": target,
            "direction": "up",
            "start_price": "10",
            "start": MORNING.isoformat(),
            "cutoff": MORNING.isoformat(),
            "recorded_at": MORNING.isoformat(),
            "deadline": CN_CLOSE.isoformat(),
            "prospective": True,
            "explicit": True,
            "rule_version": "rules-1",
        }

        cases = (
            ("not a mapping", "forecast_not_mapping"),
            ({**base, "start_price": "0"}, "start_price_missing"),
            ({**base, "deadline": CLOSING.isoformat(),
              "allow_observed_as_of_before_deadline": True}, "observed_endpoint_incompatible"),
            ({**base, "recorded_at": MIDDAY.isoformat()}, "recorded_after_start"),
            ({**base, "rule_version": "rules-2"}, "rule_version_changed"),
            ({**base, "target": {**target, "valuation_source": "other"}}, "source_changed"),
            ({**base, "target": {**target, "holding_config_fingerprint": "bad"}}, "holding_fingerprint_changed"),
        )
        for forecast, reason in cases:
            with self.subTest(reason=reason):
                result = evaluate_forecasts((forecast,), current)
                self.assertEqual(result["evaluations"][0]["status"], "unable_to_evaluate")
                self.assertIn(reason, result["evaluations"][0]["reasons"])

        invalid_current = _evidence(
            _snapshot((_holding_snapshot("A", "DUP", price="12", as_of=CN_CLOSE, freshness="stale"),)),
            CLOSING,
            report_kind="closing",
        )
        result = evaluate_forecasts((base,), invalid_current)
        self.assertIn("observed_outcome_invalid", result["evaluations"][0]["reasons"])

    def test_forecast_evaluation_does_not_synthesize_hindsight_from_current_trend(self):
        current = _evidence(
            _snapshot((_holding_snapshot("A", "DUP", price="12"),)),
            CLOSING,
            analysis={"items": ({"account_id": "A", "code": "DUP", "technical": {"trend": {"direction": "UP"}}},)},
        )

        result = evaluate_forecasts((), current)

        self.assertEqual(result["status"], "unable_to_evaluate")
        self.assertEqual(result["evaluations"], [])
        self.assertIn("forecast_missing", result["reasons"])


if __name__ == "__main__":
    unittest.main()
