from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
REMINDER_GRACE_SECONDS = 300


@dataclass(frozen=True)
class ReminderStage:
    task_id: str
    title: str
    hour: int
    minute: int
    checklist: tuple[str, ...]
    wait_conditions: tuple[str, ...]


REMINDER_STAGES = (
    ReminderStage(
        "GLOBAL_0630",
        "隔夜市场与数据日期",
        6,
        30,
        (
            "人工核对隔夜美股、港股 ADR、汇率、利率与大宗商品变化。",
            "分别确认 A 股、港股市场开休市状态和所用数据日期。",
            "记录仅待复核事项，不把工作日提醒视为交易日历证据。",
        ),
        (
            "WAIT：未人工确认市场状态、数据日期和来源前不形成结论。",
            "WAIT：本提醒未读取行情、新闻或交易日历。",
        ),
    ),
    ReminderStage(
        "NEWS_0810",
        "政策、公告、业绩及消息来源",
        8,
        10,
        (
            "人工检查政策、监管公告、公司公告、业绩更新及可信消息源。",
            "分别标注消息发布时间、来源和影响对象。",
            "核对 A/H 市场开休市状态及相关数据日期。",
        ),
        (
            "WAIT：消息来源和时间未核实前不执行后续动作。",
            "WAIT：不把未经确认的新闻摘要作为研究结论。",
        ),
    ),
    ReminderStage(
        "PLAN_0845",
        "盘前观察清单、风险预算、失效条件",
        8,
        45,
        (
            "人工整理盘前观察清单、风险预算和失效条件。",
            "确认每个观察项只引用已闭合、已到齐且日期正确的数据。",
            "再次核对 A/H 市场开休市状态，不猜测节假日。",
        ),
        (
            "WAIT：风险预算和失效条件未人工确认前不执行。",
            "WAIT：数据未闭合或未到齐时只保留待检查清单。",
        ),
    ),
    ReminderStage(
        "OPEN_VERIFY",
        "开盘验证，只参考已闭合且到齐的数据",
        10,
        0,
        (
            "人工检查开盘后已闭合且到齐的数据，排除形成中的分钟线。",
            "核对 A/H 市场是否实际开市及当前数据日期。",
            "只记录验证结果和待补证据，不自动下单。",
        ),
        (
            "WAIT：开盘数据未闭合、未到齐或日期未核实时不形成结论。",
            "WAIT：本阶段提醒时点不承诺数据已经可用。",
        ),
    ),
    ReminderStage(
        "NOON_1130",
        "午盘数据、风险与上午变化",
        11,
        30,
        (
            "人工核对午盘数据、上午变化、风险暴露和异常波动。",
            "确认引用的数据均已闭合且与当日市场状态匹配。",
            "记录下午需继续复核的证据缺口。",
        ),
        (
            "WAIT：午盘数据和风险变化未人工核实前不执行。",
            "WAIT：过期提醒不代表已完成复核或已发送通知。",
        ),
    ),
    ReminderStage(
        "AFTERNOON_CHECK",
        "午后变化与重新形成的周期",
        13,
        15,
        (
            "人工检查午后变化和重新形成的周期结构。",
            "确认上午与午后数据衔接、闭合状态和数据日期。",
            "若为 A 股常规交易日，可把后续闭合时点列为待复核事项。",
            "港股、半日交易和节假日需独立核对，不套用 A 股时点。",
        ),
        (
            "WAIT：午后周期仍在形成或数据未齐时不执行。",
            "WAIT：13:15 是清单提醒时点，不承诺形成完整信号。",
        ),
    ),
    ReminderStage(
        "TRADE_1430",
        "人工条件复核、风险预算与证据缺口",
        14,
        30,
        (
            "人工复核计划条件、风险预算、失效条件和证据缺口。",
            "若确认为 A 股常规交易日，14:30 的下午 120m 尚未闭合；15:00 后且数据齐全再人工检查。",
            "港股、半日交易和节假日需独立核对开休市状态、数据日期和可用范围。",
        ),
        (
            "WAIT：对应市场状态、数据日期、闭合数据和人工条件未核实前不执行。",
            "WAIT：本提醒不下单、不发送交易指令、不承诺信号成立。",
        ),
    ),
    ReminderStage(
        "CLOSE_1610",
        "A/H收盘、基金净值日期与归因",
        16,
        10,
        (
            "人工核对 A/H 收盘状态、收盘数据和基金净值日期。",
            "记录当日归因、偏差来源和待补证据。",
            "确认所有数据日期与市场实际开休市状态一致。",
        ),
        (
            "WAIT：收盘和净值日期未人工确认前不形成归因结论。",
            "WAIT：日历未验证时只保留人工核对事项。",
        ),
    ),
    ReminderStage(
        "RESEARCH_2030",
        "公告、财报、行业深研及后续待核实事项",
        20,
        30,
        (
            "人工检查公告、财报、行业深研和盘后新增信息。",
            "整理后续待核实事项、来源、时间和影响范围。",
            "确认不把晚间研究清单当作已完成研究结论。",
        ),
        (
            "WAIT：公告、财报和研究来源未核实前不执行。",
            "WAIT：本阶段不自动安排周末、月度、季度或实时事件任务。",
        ),
    ),
)


_PLAN_NOTES = (
    "工作日仅用于提醒时点，不是交易日历证据；节假日不会自动识别。",
    "每次提醒均需人工分别核对 A/H 市场开休市状态、数据日期和来源。",
    "本预览不读取行情、新闻、持仓、凭据、通知配置或交易日历。",
    "不推算下一交易日，不补发过期提醒，不自动安排周末/月季/实时事件任务。",
    "所有项目均为 WAIT / NOT_READY / UNVERIFIED，且 auto_execute=false。",
)


_STAGE_INDEX = {stage.task_id: stage for stage in REMINDER_STAGES}


def get_reminder_stage(task_id):
    # Subclass equality must not decide which stage is selected.
    if type(task_id) is not str:
        raise ValueError("task_id must be a string")
    try:
        return _STAGE_INDEX[task_id]
    except KeyError:
        raise ValueError("unknown reminder task_id") from None



def build_reminder_plan(*, now):
    current = _normalize_now(now)
    items = []
    if current.weekday() < 5:
        items = [_build_item(stage, current) for stage in REMINDER_STAGES]

    return {
        "mode": "preview",
        "timezone": "Asia/Shanghai",
        "plan_date": current.date().isoformat(),
        "generated_at": current.isoformat(),
        "scheduled": False,
        "notified": False,
        "auto_execute": False,
        "calendar_status": "UNVERIFIED",
        "analysis_state": "NOT_READY",
        "action": "WAIT",
        "items": items,
        "next_reminder": _next_reminder(items),
        "notes": list(_PLAN_NOTES),
    }


def _normalize_now(now):
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be an aware datetime")
    try:
        offset = now.utcoffset()
    except Exception as exc:
        raise ValueError("now must have a valid timezone offset") from exc
    if offset is None:
        raise ValueError("now must be an aware datetime")
    try:
        return now.astimezone(SHANGHAI_TZ)
    except (OverflowError, ValueError, OSError) as exc:
        raise ValueError("now is outside the supported datetime range") from exc


def _build_item(stage, current):
    scheduled_at = datetime.combine(
        current.date(),
        time(stage.hour, stage.minute),
        tzinfo=SHANGHAI_TZ,
    )
    expires_at = scheduled_at + timedelta(seconds=REMINDER_GRACE_SECONDS)
    return {
        "task_id": stage.task_id,
        "reminder_id": f"{current.date().isoformat()}:{stage.task_id}",
        "title": stage.title,
        "scheduled_at": scheduled_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "timing_status": _timing_status(current, scheduled_at, expires_at),
        "checklist": list(stage.checklist),
        "wait_conditions": list(stage.wait_conditions),
        "action": "WAIT",
        "auto_execute": False,
        "calendar_status": "UNVERIFIED",
        "analysis_state": "NOT_READY",
    }


def _timing_status(current, scheduled_at, expires_at):
    if current < scheduled_at:
        return "upcoming"
    if current < expires_at:
        return "due"
    return "expired"


def _next_reminder(items):
    for item in items:
        if item["timing_status"] in ("due", "upcoming"):
            return _copy_item(item)
    return None


def _copy_item(item):
    result = dict(item)
    result["checklist"] = list(item["checklist"])
    result["wait_conditions"] = list(item["wait_conditions"])
    return result
