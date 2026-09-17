import logging
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from app.notify.feishu import FeishuNotifier
from app.market.factory import create_fundamental_provider, create_global_market_providers
from app.report.portfolio import format_failure_reminder
from app.report.reminders import format_reminder
from app.utils.private_storage import require_private_execution
from app.workflow.portfolio import GLOBAL_MARKET_STAGES, run_portfolio_report
from app.workflow.live_acceptance import LiveTrialLedger
from app.workflow.live_observation import observe_report
from app.workflow.stage_evidence import validate_stage_envelope
from app.workflow.stage_store import MAX_STAGE_DELAY, STAGE_TIMES, StageStore
from app.workflow.reminders import (
    REMINDER_GRACE_SECONDS,
    REMINDER_STAGES,
    build_reminder_plan,
    get_reminder_stage,
)


scheduler = BlockingScheduler(timezone="Asia/Shanghai")
logger = logging.getLogger(__name__)

DEFAULT_REPORT_SCHEDULES = (
    ("global-market-scan", "全球市场扫描", 6, 30, "global"),
    ("morning-report", "投资晨报", 9, 0, "morning"),
    ("midday-analysis", "午盘分析", 11, 30, "midday"),
    ("trading-assistant", "人工交易复核提醒", 14, 30, "trading"),
    ("closing-review", "A/H股收盘复盘", 16, 10, "closing"),
)
FOUR_STAGE_SCHEDULES = DEFAULT_REPORT_SCHEDULES[1:]


def _run_scheduled_portfolio_report(notifier, report_title, report_kind, private_state_dir=None, clock=None,
                                    global_provider=None, treasury_fallback=None, fundamental_provider=None):
    result = _collect_scheduled_portfolio_report(
        notifier, report_kind, private_state_dir, clock,
        global_provider=global_provider, treasury_fallback=treasury_fallback,
        fundamental_provider=fundamental_provider,
    )
    return _finish_scheduled_result(notifier, report_title, result)


def _collect_scheduled_portfolio_report(notifier, report_kind, private_state_dir, clock, run_id=None, *,
                                        global_provider=None, treasury_fallback=None, fundamental_provider=None):
    options = {}
    if run_id is not None:
        options["run_id"] = run_id
    if private_state_dir is not None:
        options["private_state_dir"] = private_state_dir
    if clock is not None:
        options["clock"] = clock
    if report_kind in GLOBAL_MARKET_STAGES:
        if global_provider is not None:
            options["global_provider"] = global_provider
        if treasury_fallback is not None:
            options["treasury_fallback"] = treasury_fallback
    if fundamental_provider is not None:
        options["fundamental_provider"] = fundamental_provider
    try:
        result = run_portfolio_report(report_kind=report_kind, notifier=notifier, **options)
        if (
            not isinstance(result, dict)
            or result.get("status") not in ("completed", "partial", "failed")
            or not isinstance(result.get("report"), str)
            or not result["report"].strip()
            or not isinstance(result.get("errors", []), list)
        ):
            raise ValueError("invalid workflow result")
        result = {**result, "errors": list(result.get("errors", []))}
    except Exception as exc:
        errors = [f"scheduled workflow: {type(exc).__name__}"]
        result = {
            "status": "failed",
            "report_kind": report_kind,
            "notified": False,
            "errors": errors,
            "report": format_failure_reminder(
                report_kind, datetime.now(ZoneInfo("Asia/Shanghai")), errors
            ),
        }
        if run_id is not None:
            result["run_id"] = run_id

    if run_id is not None and (result.get("run_id") != run_id or result.get("report_kind") != report_kind):
        raise ValueError("workflow result does not match the reserved identity")

    return result


def _four_stage_window(report_kind, clock):
    if not isinstance(report_kind, str) or report_kind not in {item[4] for item in FOUR_STAGE_SCHEDULES}:
        raise ValueError("unsupported four-stage kind")
    current = datetime.now(ZoneInfo("Asia/Shanghai")) if clock is None else clock()
    if not isinstance(current, datetime) or current.utcoffset() is None:
        raise ValueError("schedule clock must be timezone-aware")
    current = current.astimezone(ZoneInfo("Asia/Shanghai"))
    scheduled = datetime.combine(current.date(), STAGE_TIMES[report_kind], current.tzinfo)
    due = current.weekday() < 5 and scheduled <= current <= scheduled + MAX_STAGE_DELAY
    return due, scheduled, current


def _checked_stage_clock(report_kind, source):
    """Share sampled-time order across collection and persistence failures."""
    previous, failed = None, False

    def sample():
        nonlocal previous, failed
        if failed:
            raise ValueError("stage clock already failed during this attempt")
        try:
            _, _, current = _four_stage_window(report_kind, source)
            if previous is not None and current < previous:
                raise ValueError("stage clock moved backwards")
        except Exception:
            failed = True
            raise ValueError("invalid or backwards stage clock") from None
        previous = current
        return current

    return sample


def _run_four_stage_report(notifier, report_title, report_kind, private_state_dir=None, clock=None, trial_ledger=None,
                           global_provider=None, treasury_fallback=None, fundamental_provider=None):
    """Serialize a trial attempt, including its checks, send and observation."""
    if trial_ledger is None:
        return _execute_four_stage_report(notifier, report_title, report_kind, private_state_dir, clock,
                                          global_provider=global_provider,
                                          treasury_fallback=treasury_fallback,
                                          fundamental_provider=fundamental_provider)
    result = _new_four_stage_result(report_kind)
    try:
        require_private_execution()
        if not isinstance(trial_ledger, LiveTrialLedger) or private_state_dir is None:
            raise ValueError("a frozen private ledger and stage store are required")
        with trial_ledger.execution_lock():
            result = _execute_four_stage_report(
                notifier, report_title, report_kind, private_state_dir, clock, trial_ledger,
                global_provider=global_provider, treasury_fallback=treasury_fallback,
                fundamental_provider=fundamental_provider)
    except Exception as exc:
        # Preserve the actual delivery outcome even if releasing a lock fails.
        result["errors"].append(f"four-stage execution: {type(exc).__name__}")
        result.update(status="failed", notification_allowed=False)
        logger.error("Four-stage execution rejected: %s", type(exc).__name__)
    return result


def _new_four_stage_result(report_kind):
    return {"run_id": str(uuid4()), "status": "failed", "report_kind": report_kind, "report": "",
              "notified": False, "notification_attempted": False, "errors": [],
              "delivery": {"api_accepted": False, "api_receipt_id": None,
                           "chatgpt_received": False, "device_received": False},
              "calendar_status": "UNVERIFIED", "auto_execute": False}


def _execute_four_stage_report(notifier, report_title, report_kind, private_state_dir=None, clock=None,
                               trial_ledger=None, global_provider=None, treasury_fallback=None,
                               fundamental_provider=None):
    """Check wall time before collection and before sending; never backfill."""
    result = _new_four_stage_result(report_kind)
    observed_slot = False
    last_sample = None
    clock = _checked_stage_clock(report_kind, clock)
    try:
        due, scheduled, started_at = _four_stage_window(report_kind, clock)
        last_sample = started_at
        if not due:
            return {**result, "status": "skipped", "reason": "outside_stage_window",
                    "notification_allowed": False}
        if trial_ledger is not None:
            require_private_execution()
            if not isinstance(trial_ledger, LiveTrialLedger) or private_state_dir is None:
                raise ValueError("a frozen private ledger and stage store are required")
            day, stage = scheduled.date().isoformat(), scheduled.strftime("%H:%M")
            if day not in trial_ledger.trial_dates:
                return {**result, "status": "skipped", "reason": "outside_frozen_trial_dates",
                        "notification_allowed": False}
            if trial_ledger.has_analysis(day, stage):
                return {**result, "status": "skipped", "reason": "slot_already_observed",
                        "notification_allowed": False}
            if trial_ledger.has_attempt(day, stage):
                return {**result, "status": "skipped", "reason": "slot_already_attempted",
                        "notification_allowed": False}
            reservation = trial_ledger.reserve_attempt(result["run_id"], day, stage, clock=clock)
            observed_slot = True
            reserved_at = datetime.fromisoformat(reservation["recorded_at"])
            if reserved_at < started_at:
                raise ValueError("reservation clock moved backwards")
            last_sample = reserved_at
            if StageStore(private_state_dir).load_stage(day, report_kind) is not None:
                # A previous attempt may have sent before crashing. Never
                # guess its outcome or re-send it to rescue an incomplete slot.
                raise ValueError("stage already exists without an observation")
        # Send only after a second window check. The workflow itself must not
        # send a potentially stale report while a slow collector is running.
        result.update(_collect_scheduled_portfolio_report(
            None, report_kind, private_state_dir, clock,
            run_id=result["run_id"] if trial_ledger is not None else None,
            global_provider=global_provider, treasury_fallback=treasury_fallback,
            fundamental_provider=fundamental_provider))
        evidence_time = _presend_evidence_time(result, private_state_dir, scheduled, last_sample)
        # Sample after the last persistence read: even local I/O can outlive
        # the slot, and the send must not use a pre-read clock sample.
        still_due, current_scheduled, finished_at = _four_stage_window(report_kind, clock)
        if finished_at < max(last_sample, evidence_time):
            raise ValueError("schedule clock moved backwards")
        last_sample = finished_at
        if not still_due or current_scheduled != scheduled:
            result.update(status="skipped", reason="expired_after_analysis", notification_allowed=False)
        else:
            if trial_ledger is not None and (
                result.get("rule_version") != trial_ledger.rule_version
                or (result.get("stage_context") or {}).get("persistence") != {"status": "stored"}
                or result.get("status") == "failed"
            ):
                result["errors"].append("live trial: frozen rule or first stage evidence unavailable")
                result["status"] = "failed"
                result["report"] = format_failure_reminder(
                    report_kind, finished_at, result["errors"], allow_retry=False)
            result = _finish_scheduled_result(notifier, report_title, result)
    except Exception as exc:
        result["errors"].append(f"four-stage schedule: {type(exc).__name__}")
        result["status"] = "failed"
        result["notification_allowed"] = False
        logger.error("Four-stage report rejected: %s", type(exc).__name__)
    if observed_slot:
        try:
            _, _, observed_at = _four_stage_window(report_kind, clock)
            if observed_at < last_sample:
                raise ValueError("observation clock moved backwards")
            observe_report(trial_ledger, result, private_state_dir=private_state_dir,
                           scheduled_at=scheduled, observed_at=observed_at, clock=clock)
        except Exception as exc:
            result["live_acceptance_status"] = "record_failed"
            result["errors"].append(f"live observation: {type(exc).__name__}")
            if result["status"] == "completed":
                result["status"] = "partial"
        if result.get("live_acceptance_status") == "record_failed":
            logger.error("Private live observation was not recorded; this slot is not accepted")
    return result


def _presend_evidence_time(result, private_state_dir, scheduled_at, started_at):
    """Read the latest evidence time before sampling the final send clock."""
    context = result.get("stage_context")
    if context is None:
        return scheduled_at
    if not isinstance(context, dict):
        raise ValueError("invalid pre-send stage context")
    evidence = context.get("evidence")
    if evidence is None and context.get("persistence") != {"status": "stored"}:
        return scheduled_at  # A failed collector may only have a safe reminder.
    if validate_stage_envelope(evidence):
        raise ValueError("invalid pre-send evidence")
    day, kind = scheduled_at.date().isoformat(), result["report_kind"]
    cutoff, generated = (datetime.fromisoformat(evidence[key]) for key in ("cutoff", "generated_at"))
    if (evidence["trading_date"] != day or evidence["report_kind"] != kind
            or not scheduled_at <= started_at <= cutoff <= generated):
        raise ValueError("pre-send evidence time or slot mismatch")
    if private_state_dir is not None:
        record = StageStore(private_state_dir).load_stage(day, kind)
        if record is None:
            if context.get("persistence") == {"status": "stored"}:
                raise ValueError("pre-send stage record missing")
        elif record["evidence"] != evidence:
            raise ValueError("pre-send persisted evidence mismatch")
        else:
            return datetime.fromisoformat(record["recorded_at"])
    return generated


def _finish_scheduled_result(notifier, report_title, result):
    """Make at most one delivery attempt and preserve data-failure status."""
    result["notified"] = result.get("notified") is True
    if (not result["notified"] and result.get("notification_attempted") is not True
            and result.get("notification_allowed") is not False):
        result["notification_attempted"] = True
        try:
            result["notified"] = notifier.send(result["report"]) is True
        except Exception:
            result["notified"] = False
        if result["notified"]:
            logger.info("Scheduled reminder API accepted: %s", report_title)
        else:
            result["errors"].append("notification: scheduled delivery was not confirmed")
            if result["status"] == "completed":
                result["status"] = "partial"
    delivery = result.get("delivery")
    result["delivery"] = {
        "api_receipt_id": None, "chatgpt_received": False, "device_received": False,
        **(delivery if isinstance(delivery, dict) else {}),
        "api_accepted": result["notified"],
    }

    if result.get("status") == "failed":
        logger.error(
            "Scheduled report failed; safe reminder sent=%s: %s",
            result["notified"],
            report_title,
        )
    elif result.get("status") == "partial":
        logger.warning("Scheduled report completed with partial data: %s", report_title)
    if not result["notified"]:
        logger.warning("Scheduled report was not sent to Feishu: %s", report_title)
    return result


def _run_scheduled_reminder(notifier, task_id, clock=None):
    """Send only a currently due manual checklist, without collecting data."""
    result = {
        "status": "failed",
        "mode": "reminder",
        "report": "",
        "notified": False,
        "errors": [],
        "action": "WAIT",
        "auto_execute": False,
        "analysis_state": "NOT_READY",
        "calendar_status": "UNVERIFIED",
    }
    try:
        stage = get_reminder_stage(task_id)
        now = datetime.now(ZoneInfo("Asia/Shanghai")) if clock is None else clock()
        plan = build_reminder_plan(now=now)
        reminder = next(
            (item for item in plan["items"] if item["task_id"] == stage.task_id), None
        )
        if reminder is None:
            result.update(
                status="skipped", task_id=stage.task_id,
                reason="非工作日提醒日；不推算下一交易日。",
            )
            return result
        result.update(reminder)
        if reminder["timing_status"] != "due":
            result.update(
                status="skipped", reason="本阶段未到提醒时点或已过期；不补发旧清单。"
            )
            return result
        result["report"] = format_reminder(reminder)
    except Exception as exc:
        # A clock/config error is not authority to invent a send time.
        result["errors"].append(f"reminder setup: {type(exc).__name__}")
        logger.error("Scheduled checklist rejected: %s", type(exc).__name__)
        return result

    result["status"] = "completed"
    return _finish_scheduled_result(notifier, stage.title, result)


def register_default_jobs(target_scheduler=None, codes=None, collector=None, notifier=None, private_state_dir=None,
                          global_provider=None, treasury_fallback=None, fundamental_provider=None):
    """Register the five weekday portfolio reports on an APScheduler instance.

    The legacy codes and collector arguments remain accepted for compatibility;
    portfolio jobs load their own account configuration and valuation router.
    """
    return _register_report_jobs(
        target_scheduler, notifier, DEFAULT_REPORT_SCHEDULES,
        _run_scheduled_portfolio_report, 1800, private_state_dir, None,
        None,
        global_provider, treasury_fallback,
        fundamental_provider=fundamental_provider,
    )


def register_four_stage_jobs(target_scheduler=None, notifier=None, *, private_state_dir=None, clock=None, live_ledger=None,
                             global_provider=None, treasury_fallback=None, fundamental_provider=None):
    """Opt-in four-stage cadence; weekdays are not a verified trading calendar."""
    return _register_report_jobs(
        target_scheduler, notifier, FOUR_STAGE_SCHEDULES, _run_four_stage_report,
        int(MAX_STAGE_DELAY.total_seconds()), private_state_dir, clock, live_ledger,
        global_provider, treasury_fallback,
        fundamental_provider=fundamental_provider,
    )


def _register_report_jobs(target_scheduler, notifier, schedules, callback, grace, private_state_dir, clock,
                          live_ledger=None, global_provider=None, treasury_fallback=None,
                          fundamental_provider=None):
    require_private_execution()
    if clock is not None and not callable(clock):
        raise ValueError("schedule clock must be callable")
    options = {}
    if private_state_dir is not None:
        options["private_state_dir"] = StageStore(private_state_dir).directory
    if live_ledger is not None:
        if private_state_dir is None or callback is not _run_four_stage_report:
            raise ValueError("live ledger requires four-stage private evidence")
        options["trial_ledger"] = LiveTrialLedger.load(live_ledger)
    if clock is not None:
        options["clock"] = clock
    # Only the overnight stages consume these; a reporting job that does not
    # need them must stay free of the keyword so its call shape is unchanged.
    if global_provider is not None or treasury_fallback is not None:
        options["global_provider"] = global_provider
        options["treasury_fallback"] = treasury_fallback
    if fundamental_provider is not None:
        options["fundamental_provider"] = fundamental_provider
    target_scheduler = scheduler if target_scheduler is None else target_scheduler
    notifier = FeishuNotifier() if notifier is None else notifier
    for job_id, report_title, hour, minute, report_kind in schedules:
        job_options = dict(options)
        if report_kind not in GLOBAL_MARKET_STAGES:
            job_options.pop("global_provider", None)
            job_options.pop("treasury_fallback", None)
        target_scheduler.add_job(
            callback,
            "cron",
            timezone="Asia/Shanghai",
            day_of_week="mon-fri",
            hour=hour,
            minute=minute,
            id=job_id,
            replace_existing=True,
            misfire_grace_time=grace,
            coalesce=True,
            max_instances=1,
            kwargs={
                "notifier": notifier,
                "report_title": report_title,
                "report_kind": report_kind,
                **job_options,
            },
        )
    return target_scheduler


def register_reminder_jobs(target_scheduler=None, notifier=None, clock=None):
    """Register nine weekday checklist jobs, without starting or sending.

    Weekdays define notification cadence, not exchange-calendar evidence.
    The callback independently rejects early, expired, and weekend invocations.
    """
    if clock is not None and not callable(clock):
        raise ValueError("reminder clock must be callable")
    target_scheduler = scheduler if target_scheduler is None else target_scheduler
    notifier = FeishuNotifier() if notifier is None else notifier
    for stage in REMINDER_STAGES:
        target_scheduler.add_job(
            _run_scheduled_reminder,
            "cron",
            timezone="Asia/Shanghai",
            day_of_week="mon-fri",
            hour=stage.hour,
            minute=stage.minute,
            id=f"reminder-{stage.task_id}",
            replace_existing=True,
            misfire_grace_time=REMINDER_GRACE_SECONDS,
            coalesce=True,
            max_instances=1,
            kwargs={
                "notifier": notifier,
                "task_id": stage.task_id,
                "clock": clock,
            },
        )
    return target_scheduler


def start_scheduler(codes=None, *, profile="legacy", private_state_dir=None, live_ledger=None,
                    global_provider=None, treasury_fallback=None, fundamental_provider=None):
    """Start an explicit reminder profile; this is not ChatGPT Tasks."""
    if type(profile) is not str or profile not in ("legacy", "four-stage", "full-day"):
        raise ValueError("unsupported scheduler profile")
    if private_state_dir is not None and profile == "full-day":
        raise ValueError("private state requires a portfolio report profile")
    if live_ledger is not None and (profile != "four-stage" or private_state_dir is None):
        raise ValueError("live ledger requires four-stage private evidence")
    options = {"private_state_dir": private_state_dir} if private_state_dir is not None else {}
    if live_ledger is not None:
        options["live_ledger"] = live_ledger
    # Built once per scheduler, after every profile/private-state check above,
    # so an invalid configuration never reaches provider construction. The
    # full-day checklist sends no report and must not acquire market plumbing.
    if profile != "full-day" and (global_provider is None or treasury_fallback is None):
        default_global, default_treasury = create_global_market_providers()
        global_provider = default_global if global_provider is None else global_provider
        treasury_fallback = default_treasury if treasury_fallback is None else treasury_fallback
    if profile != "full-day":
        if fundamental_provider is None:
            fundamental_provider = create_fundamental_provider()
        options["global_provider"] = global_provider
        options["treasury_fallback"] = treasury_fallback
        options["fundamental_provider"] = fundamental_provider
    if profile == "legacy":
        register_default_jobs(codes=codes, **options)
    elif profile == "four-stage":
        register_four_stage_jobs(**options)
    else:
        register_reminder_jobs()
    scheduler.start()
