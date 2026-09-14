from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.report.reminders import format_reminder
from app.workflow.reminders import (
    REMINDER_GRACE_SECONDS,
    REMINDER_STAGES,
    ReminderStage,
    build_reminder_plan,
    get_reminder_stage,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


EXPECTED_STAGES = (
    ("GLOBAL_0630", "隔夜市场与数据日期", 6, 30),
    ("NEWS_0810", "政策、公告、业绩及消息来源", 8, 10),
    ("PLAN_0845", "盘前观察清单、风险预算、失效条件", 8, 45),
    ("OPEN_VERIFY", "开盘验证，只参考已闭合且到齐的数据", 10, 0),
    ("NOON_1130", "午盘数据、风险与上午变化", 11, 30),
    ("AFTERNOON_CHECK", "午后变化与重新形成的周期", 13, 15),
    ("TRADE_1430", "人工条件复核、风险预算与证据缺口", 14, 30),
    ("CLOSE_1610", "A/H收盘、基金净值日期与归因", 16, 10),
    ("RESEARCH_2030", "公告、财报、行业深研及后续待核实事项", 20, 30),
)


def shanghai_at(hour, minute, second=0):
    return datetime(2026, 9, 14, hour, minute, second, tzinfo=SHANGHAI)


def test_stage_catalog_is_frozen_ordered_and_manual_only():
    assert REMINDER_GRACE_SECONDS == 300
    assert len(REMINDER_STAGES) == 9
    assert tuple(
        (stage.task_id, stage.title, stage.hour, stage.minute)
        for stage in REMINDER_STAGES
    ) == EXPECTED_STAGES

    first = REMINDER_STAGES[0]
    assert isinstance(first, ReminderStage)
    with pytest.raises(FrozenInstanceError):
        first.title = "changed"
    with pytest.raises(AttributeError):
        first.checklist.append("mutate")
    assert all(stage.checklist for stage in REMINDER_STAGES)
    assert all(stage.wait_conditions for stage in REMINDER_STAGES)


def test_get_reminder_stage_returns_known_stage_and_rejects_unknown_ids():
    assert get_reminder_stage("OPEN_VERIFY").title == "开盘验证，只参考已闭合且到齐的数据"

    for invalid in ("", "missing", None, 42):
        with pytest.raises(ValueError):
            get_reminder_stage(invalid)


def test_get_reminder_stage_rejects_non_string_before_comparison():
    class EqualsEverything:
        def __eq__(self, other):
            return True

    class BrokenEquality:
        def __eq__(self, other):
            raise RuntimeError("fixture-private-comparison-detail")

    class StrEqualsEverything(str):
        def __eq__(self, other):
            return True

    class BrokenStrEquality(str):
        def __eq__(self, other):
            raise RuntimeError("fixture-private-comparison-detail")

    for task_id in (
        EqualsEverything(),
        BrokenEquality(),
        StrEqualsEverything("bogus"),
        BrokenStrEquality("bogus"),
    ):
        with pytest.raises(ValueError):
            get_reminder_stage(task_id)



def test_workday_plan_contains_wait_only_unverified_preview_items():
    plan = build_reminder_plan(now=shanghai_at(8, 0))

    assert plan["mode"] == "preview"
    assert plan["timezone"] == "Asia/Shanghai"
    assert plan["plan_date"] == "2026-09-14"
    assert plan["generated_at"] == "2026-09-14T08:00:00+08:00"
    assert plan["scheduled"] is False
    assert plan["notified"] is False
    assert plan["auto_execute"] is False
    assert plan["calendar_status"] == "UNVERIFIED"
    assert plan["analysis_state"] == "NOT_READY"
    assert plan["action"] == "WAIT"
    assert "不读取行情" in " ".join(plan["notes"])
    assert "交易日历" in " ".join(plan["notes"])
    assert len(plan["items"]) == 9

    assert plan["next_reminder"]["task_id"] == "NEWS_0810"
    assert plan["next_reminder"] == plan["items"][1]

    for item, expected in zip(plan["items"], EXPECTED_STAGES):
        task_id, title, hour, minute = expected
        assert item["task_id"] == task_id
        assert item["reminder_id"] == f"2026-09-14:{task_id}"
        assert item["title"] == title
        assert item["scheduled_at"] == f"2026-09-14T{hour:02d}:{minute:02d}:00+08:00"
        assert item["expires_at"] == (
            shanghai_at(hour, minute) + timedelta(seconds=REMINDER_GRACE_SECONDS)
        ).isoformat()
        assert item["action"] == "WAIT"
        assert item["auto_execute"] is False
        assert item["calendar_status"] == "UNVERIFIED"
        assert item["analysis_state"] == "NOT_READY"
        assert item["checklist"]
        assert item["wait_conditions"]


@pytest.mark.parametrize(
    "task_id,hour,minute",
    [(task_id, hour, minute) for task_id, _title, hour, minute in EXPECTED_STAGES],
)
def test_timing_status_uses_exact_due_window_for_each_stage(task_id, hour, minute):
    scheduled = shanghai_at(hour, minute)
    before = build_reminder_plan(now=scheduled - timedelta(seconds=1))
    at_start = build_reminder_plan(now=scheduled)
    before_expiry = build_reminder_plan(now=scheduled + timedelta(seconds=299))
    at_expiry = build_reminder_plan(now=scheduled + timedelta(seconds=300))

    def status(plan):
        return next(item for item in plan["items"] if item["task_id"] == task_id)["timing_status"]

    assert status(before) == "upcoming"
    assert status(at_start) == "due"
    assert status(before_expiry) == "due"
    assert status(at_expiry) == "expired"


def test_next_reminder_prefers_due_then_upcoming_and_never_backfills_expired():
    at_open_due = build_reminder_plan(now=shanghai_at(10, 4, 59))
    assert at_open_due["next_reminder"]["task_id"] == "OPEN_VERIFY"

    after_open_expired = build_reminder_plan(now=shanghai_at(10, 5))
    assert after_open_expired["next_reminder"]["task_id"] == "NOON_1130"

    after_all = build_reminder_plan(now=shanghai_at(20, 35))
    assert after_all["next_reminder"] is None
    assert after_all["items"][-1]["timing_status"] == "expired"


def test_weekend_plan_has_no_items_and_does_not_guess_next_trading_day():
    plan = build_reminder_plan(now=datetime(2026, 9, 19, 8, 0, tzinfo=SHANGHAI))

    assert plan["plan_date"] == "2026-09-19"
    assert plan["items"] == []
    assert plan["next_reminder"] is None
    assert plan["calendar_status"] == "UNVERIFIED"
    assert any("不推算下一交易日" in note for note in plan["notes"])


def test_aware_utc_input_is_normalized_to_shanghai_date_and_offsets():
    utc_now = datetime(2026, 9, 13, 22, 31, tzinfo=timezone.utc)
    plan = build_reminder_plan(now=utc_now)

    assert plan["plan_date"] == "2026-09-14"
    assert plan["generated_at"] == "2026-09-14T06:31:00+08:00"
    assert plan["items"][0]["timing_status"] == "due"
    assert plan["items"][0]["scheduled_at"].endswith("+08:00")
    assert plan["items"][0]["expires_at"].endswith("+08:00")


@pytest.mark.parametrize(
    "bad_now",
    [
        "2026-09-14T08:00:00+08:00",
        date(2026, 9, 14),
        datetime(2026, 9, 14, 8, 0),
        None,
    ],
)
def test_build_reminder_plan_rejects_bad_now_inputs(bad_now):
    with pytest.raises(ValueError):
        build_reminder_plan(now=bad_now)


def test_build_reminder_plan_wraps_timezone_validation_errors():
    extremes = (
        datetime(9999, 12, 31, 23, 59, tzinfo=timezone(timedelta(hours=-12))),
        datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=14))),
    )

    for now in extremes:
        with pytest.raises(ValueError):
            build_reminder_plan(now=now)


def test_plan_outputs_are_independent_mutable_values():
    plan_a = build_reminder_plan(now=shanghai_at(8, 0))
    plan_b = build_reminder_plan(now=shanghai_at(8, 0))

    plan_a["items"][0]["checklist"].append("local mutation")
    plan_a["items"][0]["wait_conditions"].clear()
    plan_a["notes"].append("changed")
    plan_b["next_reminder"]["checklist"].append("next mutation")

    assert "local mutation" not in plan_b["items"][0]["checklist"]
    assert plan_b["items"][0]["wait_conditions"]
    assert "local mutation" not in REMINDER_STAGES[0].checklist
    assert "changed" not in plan_b["notes"]
    assert "next mutation" not in plan_b["items"][1]["checklist"]


def test_format_reminder_is_manual_wait_message_without_delivery_claims():
    item = build_reminder_plan(now=shanghai_at(14, 30))["next_reminder"]
    message = format_reminder(item)

    assert "14:30" in message
    assert "TRADE_1430" in message
    assert item["reminder_id"] in message
    assert item["scheduled_at"] in message
    assert item["expires_at"] in message
    assert "人工检查清单" in message
    assert "人工检查清单\n\n-" in message
    assert "WAIT 条件" in message
    assert "WAIT 条件\n\n-" in message
    assert "未读取行情" in message
    assert "未读取新闻" in message
    assert "未读取交易日历" in message
    assert "不自动下单" in message
    assert "已发送" not in message
    assert "已送达" not in message
    assert item["checklist"][0] in message
    assert item["wait_conditions"][0] in message


def test_format_reminder_schema_validation():
    # Must reject non-dict input
    for non_dict in (None, "string", [1, 2, 3], 42):
        with pytest.raises(ValueError, match="reminder item must be a dictionary"):
            format_reminder(non_dict)

    # Must reject dict missing any required key
    valid_item = build_reminder_plan(now=shanghai_at(14, 30))["next_reminder"]
    required_keys = (
        "task_id",
        "title",
        "reminder_id",
        "scheduled_at",
        "expires_at",
        "timing_status",
        "checklist",
        "wait_conditions",
    )
    for key in required_keys:
        incomplete = dict(valid_item)
        del incomplete[key]
        with pytest.raises(ValueError, match="missing required key in reminder item"):
            format_reminder(incomplete)

    # Must reject invalid scheduled_at timestamp
    invalid_timestamp_item = dict(valid_item)
    invalid_timestamp_item["scheduled_at"] = "not-a-timestamp"
    with pytest.raises(ValueError, match="invalid scheduled_at timestamp"):
        format_reminder(invalid_timestamp_item)



def test_afternoon_and_trade_guidance_keeps_market_session_assumptions_conditional():
    for task_id in ("AFTERNOON_CHECK", "TRADE_1430"):
        stage = get_reminder_stage(task_id)
        text = "\n".join((stage.title, *stage.checklist, *stage.wait_conditions))
        assert "A 股常规交易日" in text
        assert "港股" in text
        assert "半日" in text
        assert "下午120m形成中" not in stage.title

    trade_text = "\n".join((
        get_reminder_stage("TRADE_1430").title,
        *get_reminder_stage("TRADE_1430").checklist,
        *get_reminder_stage("TRADE_1430").wait_conditions,
    ))
    assert "120m" in trade_text
    assert "15:00" in trade_text
