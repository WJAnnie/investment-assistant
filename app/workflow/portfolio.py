"""Orchestration for multi-account research reminders and reports."""

from datetime import datetime
import hashlib
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import yaml

from app.market.factory import create_portfolio_valuation_router
from app.portfolio.calculator import build_portfolio_snapshot
from app.portfolio.analysis import analyze_portfolio
from app.portfolio.global_market import build_global_market, gather_global_observations
from app.portfolio.loader import load_portfolio
from app.portfolio.risk import apply_portfolio_risk
from app.report.portfolio import format_failure_reminder, format_portfolio_report
from app.utils.private_storage import canonical_json, require_private_execution
from app.utils.serialization import to_jsonable
from app.workflow.stage_evidence import (
    REPORT_KINDS, build_stage_evidence, compare_stage_evidence, evaluate_forecasts,
)
from app.workflow.stage_store import StageStore

GLOBAL_MARKET_STAGES = ("global", "morning")


def _global_market_payload(global_provider, treasury_fallback, cutoff):
    try:
        if global_provider is None and treasury_fallback is None:
            return build_global_market((), cutoff, collected=False)
        observations = gather_global_observations(global_provider, treasury_fallback)
        return build_global_market(observations, cutoff)
    except Exception:
        return build_global_market((), cutoff, collected=False)


def _analysis_gaps(analysis, global_market, report_kind):
    gaps = []
    # Only the overnight stages ever collect the global block; an intraday or
    # closing stage must not report a gap for evidence it never gathers, or the
    # missing-source list overstates what is actually unavailable.
    if report_kind in GLOBAL_MARKET_STAGES:
        if (global_market or {}).get("status") != "complete":
            gaps.append("global_market_source")
    limits = (analysis or {}).get("data_limits") or {}
    if limits.get("industry") != "available":
        gaps.append("industry_ranking")
    if limits.get("fundamental") != "available":
        gaps.append("fundamentals")
    items = (analysis or {}).get("items") or ()
    if not items or any((item.get("structure") or {}).get("ready") is not True for item in items):
        gaps.append("weekly_structure")
        gaps.append("minute_structure_confirmation")
    return gaps


def load_strategy_config(path=None):
    """Load risk thresholds without performing any network activity."""
    config_path = (
        Path(path)
        if path is not None
        else Path(__file__).resolve().parents[2] / "config" / "strategy.yaml"
    )
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("strategy config must be a mapping")
    return data


def run_portfolio_report(
    report_kind,
    config_loader=load_portfolio,
    valuation_router=None,
    notifier=None,
    now=None,
    strategy_loader=load_strategy_config,
    analysis_enabled=True,
    history_start=None,
    history_end=None,
    history_period="daily",
    history_adjust="",
    min_history_bars=30,
    chan_min_span=4,
    minute_snapshot_loader=None,
    private_state_dir=None,
    clock=None,
    run_id=None,
    global_provider=None,
    treasury_fallback=None,
    industry_provider=None,
    fundamental_provider=None,
):
    """Run valuation, risk analysis, rendering, and optional notification once.

    This workflow never executes orders. Holding-level valuation failures are
    expected to be isolated by the router and produce a partial report. Fatal
    setup/calculation failures return a structured failed result so command-line
    callers can emit valid JSON and a non-zero code.
    """
    current_time = None
    clock_valid = False
    result = {
        "run_id": str(uuid4()) if run_id is None else None,
        "status": "failed",
        "report_kind": report_kind if isinstance(report_kind, str) else None,
        "snapshot": None,
        "valuations": {},
        "report": "",
        "analysis": None,
        "notified": False,
        "notification_attempted": False,
        "notification_allowed": False,
        "delivery": {"api_accepted": False, "api_receipt_id": None,
                     "chatgpt_received": False, "device_received": False},
        "full_analysis_ready": False,
        "analysis_state": "NOT_READY",
        "analysis_gaps": _analysis_gaps(None, None, report_kind),
        "stage_context": {"persistence": {"status": "not_configured"}},
        "live_acceptance_status": "not_started",
        "errors": [],
    }

    try:
        require_private_execution()
        if run_id is not None:
            if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 1000:
                raise ValueError("workflow run identity is invalid")
            result["run_id"] = run_id
        if type(report_kind) is not str or report_kind not in REPORT_KINDS:
            raise ValueError("unsupported report kind")
        if clock is not None and not callable(clock):
            raise ValueError("workflow clock must be callable")
        wall_clock = (lambda: datetime.now(ZoneInfo("Asia/Shanghai"))) if clock is None else clock
        current_time = _workflow_time(wall_clock() if now is None else now)
        clock_valid = True
        if private_state_dir is not None and now is not None:
            raise ValueError("offline now override cannot write private live stage records")
        store = StageStore(private_state_dir, clock=wall_clock) if private_state_dir is not None else None
        config = config_loader()
        router = valuation_router or create_portfolio_valuation_router()
        valuations = router.value_portfolio(config, include_hstech=True)
        result["valuations"] = valuations
        if now is None:
            # Freeze after collection so a quote returned during this request
            # is not labelled as a future observation relative to request start.
            clock_valid = False
            current_time = _workflow_time(wall_clock(), previous=current_time)
            clock_valid = True

        snapshot = build_portfolio_snapshot(config, valuations)
        strategy_config = strategy_loader()
        snapshot = apply_portfolio_risk(snapshot, strategy_config)
        analysis = None
        if analysis_enabled:
            holding_loader = getattr(router, "get_klines_for_holding", None)
            benchmark_loader = getattr(router, "get_klines", None)
            if holding_loader is not None:
                def history_loader(target, **kwargs):
                    if isinstance(target, str):
                        if benchmark_loader is None:
                            raise RuntimeError("benchmark history interface unavailable")
                        return benchmark_loader(
                            target,
                            valuation_mode="exchange",
                            market="HK",
                            **kwargs,
                        )
                    return holding_loader(target, **kwargs)
            else:
                history_loader = None
            analysis = analyze_portfolio(
                config,
                snapshot,
                history_loader=history_loader,
                now=current_time,
                history_start=history_start,
                history_end=history_end,
                history_period=history_period,
                history_adjust=history_adjust,
                min_history_bars=min_history_bars,
                chan_min_span=chan_min_span,
                minute_snapshot_loader=minute_snapshot_loader,
                industry_provider=industry_provider,
                fundamental_provider=fundamental_provider,
            )
            result["analysis"] = analysis
            if (analysis.get("coverage") or {}).get("ready", 0) > 0:
                result["analysis_state"] = "DEGRADED"
        rule_version = portfolio_rule_version(
            strategy_config, analysis_enabled=analysis_enabled,
            history_start=history_start, history_end=history_end,
            history_period=history_period, history_adjust=history_adjust,
            min_history_bars=min_history_bars, chan_min_span=chan_min_span,
        )
        generated_at = current_time
        if now is None:
            clock_valid = False
            generated_at = _workflow_time(wall_clock(), previous=current_time)
            clock_valid = True
        evidence = build_stage_evidence(
            snapshot, analysis, report_kind, current_time, generated_at, rule_version,
        )
        result["rule_version"] = rule_version
        context = {"evidence": evidence, "persistence": {"status": "not_configured"}}
        morning, forecasts = None, []
        if store is not None:
            try:
                context["persistence"] = store.record(evidence)
                morning = store.load_morning(evidence["trading_date"])
                if report_kind == "closing":
                    forecasts = store.load_forecasts(evidence["trading_date"])
            except (OSError, ValueError, TypeError, KeyError) as exc:
                context["persistence"] = {"status": "failed"}
                result["errors"].append(f"private stage evidence: {type(exc).__name__}")
        if report_kind in {"midday", "trading", "closing"}:
            context["comparison"] = compare_stage_evidence(evidence, morning)
        if report_kind == "closing":
            context["forecast_evaluation"] = evaluate_forecasts(forecasts, evidence)
        result["stage_context"] = context
        global_market = None
        if report_kind in GLOBAL_MARKET_STAGES:
            global_market = _global_market_payload(global_provider, treasury_fallback, current_time)
            result["global_market"] = global_market
        result["analysis_gaps"] = _analysis_gaps(analysis, global_market, report_kind)
        benchmark = valuations.get("HK.HSTECH")
        report = format_portfolio_report(
            snapshot,
            report_kind,
            current_time,
            benchmark=benchmark,
            analysis=analysis,
            stage_context=context,
            global_market=global_market,
        )
        result["snapshot"] = snapshot
        result["report"] = report

        for code, valuation in sorted(result["valuations"].items()):
            if valuation.freshness == "failed":
                result["errors"].append(
                    f"{code}: {valuation.error or 'valuation failed'}"
                )
            elif valuation.freshness in {"stale", "lagged"}:
                result["errors"].append(
                    f"{code}: valuation freshness is {valuation.freshness}"
                )

        if analysis_enabled and analysis is not None:
            coverages = [("analysis", analysis.get("coverage"))]
            if minute_snapshot_loader is not None:
                coverages.append(("minute analysis", analysis.get("minute_coverage")))
            for label, coverage in coverages:
                coverage = coverage or {}
                total = int(coverage.get("total", 0) or 0)
                ready = int(coverage.get("ready", 0) or 0)
                unavailable = int(coverage.get("unavailable", 0) or 0)
                if total > 0 and ready < total:
                    result["errors"].append(
                        f"{label}: {unavailable}/{total} items unavailable"
                    )
    except Exception as exc:
        result["errors"].append(f"workflow: {type(exc).__name__}")
        result["report"] = format_failure_reminder(
            result["report_kind"], current_time, result["errors"]
        )
    else:
        result["status"] = "partial" if result["errors"] else "completed"

    result["notification_allowed"] = clock_valid
    if notifier is not None and clock_valid:
        result["notification_attempted"] = True
        try:
            result["notified"] = notifier.send(result["report"]) is True
            if not result["notified"]:
                result["errors"].append("notification: message was not sent")
        except Exception:
            result["errors"].append("notification: send failed")
        if not result["notified"] and result["status"] == "completed":
            result["status"] = "partial"
        # A webhook boolean proves only API acceptance, not ChatGPT consumption
        # or a device receipt. No receipt identifier is fabricated here.
        result["delivery"]["api_accepted"] = result["notified"]

    return result


def _workflow_time(value, *, previous=None):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("workflow time must be timezone-aware")
    try:
        value = value.astimezone(ZoneInfo("Asia/Shanghai"))
    except OverflowError:
        raise ValueError("workflow time is outside the supported range") from None
    if previous is not None and value < previous:
        raise ValueError("workflow clock moved backwards")
    return value


def portfolio_rule_version(strategy_config=None, *, analysis_enabled=True, history_start=None,
                           history_end=None, history_period="daily", history_adjust="",
                           min_history_bars=30, chan_min_span=4):
    """Freeze the same rule inputs before trial start, without reading accounts."""
    strategy_config = load_strategy_config() if strategy_config is None else strategy_config
    return _rule_version(strategy_config, {
        "analysis_enabled": analysis_enabled,
        "history_start": history_start, "history_end": history_end,
        "history_period": history_period, "history_adjust": history_adjust,
        "min_history_bars": min_history_bars, "chan_min_span": chan_min_span,
    })


def _rule_version(strategy_config, analysis_options):
    """Freeze actual source + rule inputs, not a caller-selected version label."""
    digest = hashlib.sha256()
    root = Path(__file__).resolve().parents[1]
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    digest.update(canonical_json(to_jsonable({"strategy": strategy_config,
                                              "analysis_options": analysis_options})).encode("utf-8"))
    return "sha256:" + digest.hexdigest()
