from datetime import datetime


REQUIRED_REMINDER_KEYS = (
    "task_id",
    "title",
    "reminder_id",
    "scheduled_at",
    "expires_at",
    "timing_status",
    "checklist",
    "wait_conditions",
)


def format_reminder(item):
    if not isinstance(item, dict):
        raise ValueError("reminder item must be a dictionary")
    for key in REQUIRED_REMINDER_KEYS:
        if key not in item:
            raise ValueError(f"missing required key in reminder item: {key}")

    try:
        scheduled_time = datetime.fromisoformat(item["scheduled_at"]).strftime("%H:%M")
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid scheduled_at timestamp: {item['scheduled_at']!r}") from exc

    checklist = "\n".join(f"- {entry}" for entry in item["checklist"])
    wait_conditions = "\n".join(f"- {entry}" for entry in item["wait_conditions"])

    return "\n".join(
        (
            "全日程人工复核提醒",
            f"阶段：{scheduled_time} {item['task_id']} - {item['title']}",
            f"提醒ID：{item['reminder_id']}",
            f"计划时间：{item['scheduled_at']}",
            f"过期时间：{item['expires_at']}",
            f"状态：{item['timing_status']} / WAIT / NOT_READY / UNVERIFIED",
            "",
            "人工检查清单",
            "",
            checklist,
            "",
            "WAIT 条件",
            "",
            wait_conditions,
            "",
            "限制：未读取行情、未读取新闻、未读取交易日历；工作日提醒不是交易日历证据。",
            "纪律：仅提醒人工检查，不自动下单，不发送交易指令，不宣称研究已完成。",
        )
    )
