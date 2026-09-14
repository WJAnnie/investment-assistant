"""Leader-owned adversarial checks, separate from implementation tests."""

import json
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.report.reminders import format_reminder
from app.workflow.reminders import build_reminder_plan, get_reminder_stage


SHANGHAI = ZoneInfo("Asia/Shanghai")


class ReminderBoundaryReviewTests(unittest.TestCase):
    def test_message_includes_full_date_offset_identity_and_expiration(self):
        plan = build_reminder_plan(
            now=datetime(2026, 9, 14, 14, 30, tzinfo=SHANGHAI)
        )
        reminder = plan["next_reminder"]
        message = format_reminder(reminder)
        self.assertIn(reminder["scheduled_at"], message)
        self.assertIn(reminder["expires_at"], message)
        self.assertIn(reminder["reminder_id"], message)
        self.assertNotIn("已送达", message)

    def test_1430_guidance_is_conditional_on_cn_regular_session_not_all_markets(self):
        stage = get_reminder_stage("TRADE_1430")
        text = "\n".join((stage.title, *stage.checklist, *stage.wait_conditions))
        self.assertIn("A 股常规交易日", text)
        self.assertIn("港股", text)
        self.assertIn("半日", text)
        self.assertIn("120m", text)
        self.assertIn("15:00", text)
        self.assertNotIn("下午120m形成中", stage.title)

    def test_weekday_holiday_is_only_unverified_reminder_cadence(self):
        plan = build_reminder_plan(
            now=datetime(2026, 10, 1, 14, 30, tzinfo=SHANGHAI)
        )
        self.assertEqual(len(plan["items"]), 9)
        self.assertEqual(plan["calendar_status"], "UNVERIFIED")
        self.assertEqual(plan["analysis_state"], "NOT_READY")
        self.assertEqual(plan["action"], "WAIT")
        self.assertIs(plan["scheduled"], False)
        self.assertIs(plan["notified"], False)
        self.assertIs(plan["auto_execute"], False)

    def test_non_string_id_never_uses_object_equality_as_phase_authority(self):
        class EqualsEverything:
            def __eq__(self, other):
                return True

        class BrokenEquality:
            def __eq__(self, other):
                raise RuntimeError("fixture-private-comparison-detail")

        for task_id in (EqualsEverything(), BrokenEquality()):
            with self.subTest(kind=type(task_id).__name__):
                with self.assertRaises(ValueError):
                    get_reminder_stage(task_id)

    def test_string_subclasses_cannot_forge_stage_identity(self):
        class EqualsEverything(str):
            def __eq__(self, other):
                return True

        class BrokenEquality(str):
            def __eq__(self, other):
                raise RuntimeError("fixture-private-comparison-detail")

        for task_id in (EqualsEverything("bogus"), BrokenEquality("bogus")):
            with self.subTest(kind=type(task_id).__name__):
                with self.assertRaises(ValueError):
                    get_reminder_stage(task_id)

    def test_formatter_parses_iso_time_instead_of_slicing_character_positions(self):
        item = build_reminder_plan(
            now=datetime(2026, 9, 14, 14, 30, tzinfo=SHANGHAI)
        )["next_reminder"]
        item["scheduled_at"] = "20260914T143000+0800"
        self.assertIn("阶段：14:30 TRADE_1430", format_reminder(item))

        item["scheduled_at"] = "not-a-timestamp"
        with self.assertRaises(ValueError):
            format_reminder(item)

    def test_historical_preview_uses_shanghai_timezone_rules_not_fixed_offset(self):
        plan = build_reminder_plan(
            now=datetime(1988, 6, 1, 6, 30, tzinfo=timezone.utc)
        )
        self.assertEqual(plan["generated_at"], "1988-06-01T15:30:00+09:00")
        self.assertEqual(plan["items"][0]["scheduled_at"], "1988-06-01T06:30:00+09:00")
        self.assertEqual(plan["calendar_status"], "UNVERIFIED")
        self.assertIs(plan["scheduled"], False)

    def test_valid_iso_extremes_that_overflow_local_date_are_clean_validation_errors(self):
        extremes = (
            datetime(9999, 12, 31, 23, 59, tzinfo=timezone(timedelta(hours=-12))),
            datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=14))),
        )
        for now in extremes:
            with self.subTest(now=now):
                with self.assertRaises(ValueError):
                    build_reminder_plan(now=now)

    def test_expired_means_no_catchup_not_completed_or_delivered(self):
        plan = build_reminder_plan(
            now=datetime(2026, 9, 14, 23, 59, tzinfo=SHANGHAI)
        )
        self.assertIsNone(plan["next_reminder"])
        self.assertTrue(all(item["timing_status"] == "expired" for item in plan["items"]))
        self.assertIs(plan["notified"], False)
        self.assertIs(plan["scheduled"], False)
        self.assertTrue(all(item["action"] == "WAIT" for item in plan["items"]))


class ReminderCliBoundaryReviewTests(unittest.TestCase):
    def setUp(self):
        from app import main as main_module

        self.main_module = main_module
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.effects = [
            self.stack.enter_context(patch.object(main_module, name))
            for name in (
                "load_dotenv", "FeishuNotifier", "start_scheduler",
                "create_default_collector", "MarketCollector", "SinaProvider",
                "run_daily_reports", "run_portfolio_report", "run_market_snapshot",
            )
        ]

    def assert_no_side_effects(self):
        for effect in self.effects:
            effect.assert_not_called()

    def test_real_preview_uses_shanghai_date_and_due_state_across_utc_midnight(self):
        output = StringIO()
        with redirect_stdout(output):
            code = self.main_module.main(
                ["--reminder-plan", "--at", "2026-09-13T22:30:00Z"]
            )
        self.assertEqual(code, 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["plan_date"], "2026-09-14")
        self.assertEqual(plan["next_reminder"]["reminder_id"], "2026-09-14:GLOBAL_0630")
        self.assertEqual(plan["next_reminder"]["timing_status"], "due")
        self.assertIs(plan["notified"], False)
        self.assert_no_side_effects()

    def test_invalid_or_out_of_range_cli_times_fail_cleanly_before_initialization(self):
        for value in (
            "", "2026-09-14T00:00:00+24:00",
            "9999-12-31T23:59:00-12:00", "0001-01-01T00:00:00+14:00",
        ):
            with self.subTest(value=value):
                output, error = StringIO(), StringIO()
                with redirect_stdout(output), redirect_stderr(error):
                    with self.assertRaises(SystemExit) as raised:
                        self.main_module.main(["--reminder-plan", "--at", value])
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(output.getvalue(), "")
                self.assertNotIn("Traceback", error.getvalue())
                self.assert_no_side_effects()

    def test_preview_rejects_explicit_defaults_equals_and_abbreviated_options(self):
        for options in (
            ["--report-kind=closing"], ["--period=daily"], ["--adjust="],
            ["--start="], ["--end="], ["--rep", "closing"],
            ["--per=daily"], ["--adj="], ["--code="],
        ):
            with self.subTest(options=options), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    self.main_module.main(["--reminder-plan", *options])
                self.assertEqual(raised.exception.code, 2)
                self.assert_no_side_effects()

    def test_invalid_legacy_mode_combinations_are_rejected_before_initialization(self):
        for options in (
            [], ["--snapshot", "--portfolio"], ["--schedule", "--code", "000001"],
            ["--portfolio", "--code", "000001"], ["--schedule", "--no-notify"],
            ["--snapshot", "--at", "2026-09-14T14:30:00+08:00"],
            ["--portfolio", "--schedule-profile", "legacy"],
            ["--schedule", "--schedule-profile=full-day", "--no-notify"],
            ["--schedule", "--no-n"], ["--schedule", "--report-kind=morning"],
        ):
            with self.subTest(options=options), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    self.main_module.main(options)
                self.assertEqual(raised.exception.code, 2)
                self.assert_no_side_effects()

    def test_parser_keeps_legacy_default_values(self):
        args = self.main_module.build_parser().parse_args([])
        self.assertEqual(args.report_kind, "closing")
        self.assertEqual(args.period, "daily")
        self.assertEqual(args.adjust, "")
        self.assert_no_side_effects()

    def test_explicit_options_do_not_leak_between_parser_invocations(self):
        parser = self.main_module.build_parser()
        parser.parse_args(["--period=daily", "--report-kind=closing"])
        args = parser.parse_args([])
        self.assertFalse(getattr(args, "_explicit_options", ()))
        self.assertEqual(args.report_kind, "closing")
        self.assert_no_side_effects()

    def test_preview_reads_sys_argv_and_accepts_equals_timestamp(self):
        output = StringIO()
        with patch.object(
            self.main_module.sys, "argv",
            ["investment-assistant", "--reminder-plan", "--at=2026-09-14T14:30:00+08:00"],
        ), redirect_stdout(output):
            code = self.main_module.main()
        self.assertEqual(code, 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["next_reminder"]["task_id"], "TRADE_1430")
        self.assertEqual(plan["next_reminder"]["timing_status"], "due")
        self.assert_no_side_effects()


if __name__ == "__main__":
    unittest.main()
