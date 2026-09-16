import unittest

from app.report.portfolio import _format_decision_evidence

FORBIDDEN_TOKENS = (
    "87/A",
    "二买确认",
    "建议：+5%",
    "立即买入",
    "立即卖出",
    "自动执行交易",
)

CYCLE_LABELS = ("周线", "日线", "120分钟", "30分钟", "15分钟", "5分钟")
CYCLE_KEYS = ("weekly", "daily", "120m", "30m", "15m", "5m")


def _sample_structure(
    outcome="WAIT",
    ready=False,
    limit="dated_cycles_only",
    reason_code="STRUCTURE_DATA_MISSING",
    blocked_by=None,
    per_cycle_status=None,
    stale_cycles=(),
    insufficient_cycles=(),
    missing_cycles=(),
):
    if per_cycle_status is None:
        per_cycle_status = {c: "missing" for c in CYCLE_KEYS}
    if blocked_by is None:
        blocked_by = ["120m", "30m", "15m"]
    return {
        "outcome": outcome,
        "ready": ready,
        "limit": limit,
        "reason_code": reason_code,
        "blocked_by": list(blocked_by),
        "per_cycle_status": dict(per_cycle_status),
        "stale_cycles": list(stale_cycles),
        "insufficient_cycles": list(insufficient_cycles),
        "missing_cycles": list(missing_cycles),
    }


class DecisionEvidenceRenderTests(unittest.TestCase):
    def assertRedLines(self, text: str):
        for token in FORBIDDEN_TOKENS:
            self.assertNotIn(token, text)

    def assertActionLine(self, text: str):
        self.assertIn("完整分析：未就绪", text)
        self.assertIn("动作 WAIT", text)
        self.assertIn("建议仓位变化 +0%", text)

    def test_none_or_empty_item_produces_safe_wait_evidence(self):
        for item in (None, {}, object()):
            with self.subTest(item=item):
                text = _format_decision_evidence(item)
                self.assertIsInstance(text, str)
                self.assertEqual(len(text.split("\n")), 3)
                self.assertIn("多周期证据", text)
                for label in CYCLE_LABELS:
                    self.assertIn(f"{label} 未就绪", text)
                self.assertIn("WAIT", text)
                self.assertIn("+0%", text)
                self.assertIn("未就绪", text)
                self.assertRedLines(text)
                self.assertActionLine(text)

    def test_missing_structure_key_behaves_as_empty(self):
        items = [
            {"code": "600000", "status": "unavailable"},
            {"code": "600000", "status": "ready", "structure": None},
        ]
        for item in items:
            with self.subTest(item=item):
                text = _format_decision_evidence(item)
                self.assertIsInstance(text, str)
                self.assertEqual(len(text.split("\n")), 3)
                self.assertIn("多周期证据", text)
                for label in CYCLE_LABELS:
                    self.assertIn(f"{label} 未就绪", text)
                self.assertIn("WAIT", text)
                self.assertIn("+0%", text)
                self.assertIn("未就绪", text)
                self.assertRedLines(text)
                self.assertActionLine(text)

    def test_non_mapping_structure_produces_safe_fallback(self):
        for bad_structure in ("invalid", [1, 2, 3], 12345, True):
            with self.subTest(structure=bad_structure):
                item = {"code": "600000", "structure": bad_structure}
                text = _format_decision_evidence(item)
                self.assertIsInstance(text, str)
                self.assertNotIn("closed", text)
                self.assertNotIn("已闭合", text)
                for label in CYCLE_LABELS:
                    self.assertIn(f"{label} 未就绪", text)
                self.assertRedLines(text)
                self.assertActionLine(text)

    def test_per_cycle_status_rendering_all_statuses(self):
        status_expectations = {
            "closed": "已闭合",
            "forming": "形成中",
            "missing": "缺失",
            "invalid": "无效",
            "stale": "陈旧",
        }
        for status_value, expected_text in status_expectations.items():
            with self.subTest(status=status_value):
                cycle_status = {c: status_value for c in CYCLE_KEYS}
                struct = _sample_structure(per_cycle_status=cycle_status)
                text = _format_decision_evidence({"structure": struct})
                self.assertIsInstance(text, str)
                for label in CYCLE_LABELS:
                    self.assertIn(label, text)
                self.assertIn(expected_text, text)
                self.assertRedLines(text)
                self.assertActionLine(text)

    def test_unknown_or_missing_cycle_status_defaults_to_not_ready(self):
        cycle_status = {
            "weekly": "closed",
            "daily": "weird_status",
            "120m": "forming",
            "30m": "corrupted",
            "15m": "missing",
            "5m": None,
        }
        struct = _sample_structure(per_cycle_status=cycle_status)
        text = _format_decision_evidence({"structure": struct})
        self.assertIn("周线 已闭合", text)
        self.assertIn("日线 未就绪", text)
        self.assertIn("120分钟 形成中", text)
        self.assertIn("30分钟 未就绪", text)
        self.assertIn("15分钟 缺失", text)
        self.assertIn("5分钟 未就绪", text)
        self.assertNotIn("日线 已闭合", text)
        self.assertNotIn("30分钟 已闭合", text)
        self.assertNotIn("5分钟 已闭合", text)
        self.assertRedLines(text)
        self.assertActionLine(text)

        partial_status = {"weekly": "closed"}
        struct2 = _sample_structure(per_cycle_status=partial_status)
        text2 = _format_decision_evidence({"structure": struct2})
        self.assertIn("周线 已闭合", text2)
        for label in ("日线", "120分钟", "30分钟", "15分钟", "5分钟"):
            self.assertIn(f"{label} 未就绪", text2)
            self.assertNotIn(f"{label} 已闭合", text2)
        self.assertRedLines(text2)
        self.assertActionLine(text2)

    def test_blocked_by_and_reason_code_rendering(self):
        cases = [
            (["weekly"], "周线"),
            (["daily"], "日线"),
            (["120m"], "120分钟"),
            (["30m"], "30分钟"),
            (["15m"], "15分钟"),
            (["5m"], "5分钟"),
            (["core_signal"], "核心信号"),
            (["120m", "core_signal", "30m"], "120分钟、核心信号、30分钟"),
        ]
        for blocked_by, expected_str in cases:
            with self.subTest(blocked_by=blocked_by):
                struct = _sample_structure(outcome="WAIT", blocked_by=blocked_by)
                text = _format_decision_evidence({"structure": struct})
                self.assertIn("结构结论", text)
                self.assertIn(f"待补齐：{expected_str}", text)
                self.assertRedLines(text)
                self.assertActionLine(text)

        reason_cases = [
            ("STRUCTURE_DATA_MISSING", "必需周期数据缺失"),
            ("STRUCTURE_INCOMPLETE", "周期数据不完整"),
            ("UNCONFIRMED_STRUCTURE", "结构信号未确认"),
        ]
        for code, expected_reason in reason_cases:
            with self.subTest(reason_code=code):
                struct = _sample_structure(outcome="WAIT", blocked_by=[], reason_code=code)
                text = _format_decision_evidence({"structure": struct})
                self.assertIn(f"待补齐：{expected_reason}", text)
                self.assertRedLines(text)
                self.assertActionLine(text)

    def test_structure_outcomes_rendering(self):
        struct_confirmed = _sample_structure(
            outcome="CONFIRMED", ready=True, blocked_by=[], reason_code=None
        )
        text_conf = _format_decision_evidence({"structure": struct_confirmed})
        self.assertIn("结构已确认，仍需本人复核后再决定是否执行", text_conf)
        self.assertRedLines(text_conf)
        self.assertActionLine(text_conf)

        struct_pre = _sample_structure(
            outcome="PRECONFIRM", ready=False, blocked_by=["120m"], reason_code=None
        )
        text_pre = _format_decision_evidence({"structure": struct_pre})
        self.assertIn("前置确认中，等闭合周期补齐", text_pre)
        self.assertIn("待补齐：120分钟", text_pre)
        self.assertRedLines(text_pre)
        self.assertActionLine(text_pre)

        struct_wait = _sample_structure(
            outcome="WAIT", ready=False, blocked_by=[], reason_code=None
        )
        text_wait = _format_decision_evidence({"structure": struct_wait})
        self.assertIn("等待，证据不足不生成买入条件", text_wait)
        self.assertIn("结构结论", text_wait)
        self.assertRedLines(text_wait)
        self.assertActionLine(text_wait)

    def test_action_line_remains_fixed_wait_even_on_confirmed(self):
        struct = _sample_structure(
            outcome="CONFIRMED",
            ready=True,
            limit="confirmed",
            reason_code=None,
            blocked_by=[],
            per_cycle_status={c: "closed" for c in CYCLE_KEYS},
        )
        text = _format_decision_evidence({"structure": struct})
        lines = text.split("\n")
        self.assertEqual(len(lines), 3)
        self.assertEqual(
            lines[2],
            "完整分析：未就绪；动作 WAIT；建议仓位变化 +0%。补齐来源与风险证据后人工复核。",
        )
        self.assertRedLines(text)

    def test_all_sample_cases_satisfy_red_lines(self):
        for status in ("closed", "forming", "missing", "invalid", "stale", "unknown"):
            for outcome in ("WAIT", "PRECONFIRM", "CONFIRMED", "OTHER"):
                struct = _sample_structure(
                    outcome=outcome,
                    per_cycle_status={c: status for c in CYCLE_KEYS},
                    blocked_by=["core_signal", "weekly"],
                )
                text = _format_decision_evidence({"structure": struct})
                self.assertRedLines(text)
                self.assertActionLine(text)


if __name__ == "__main__":
    unittest.main()
