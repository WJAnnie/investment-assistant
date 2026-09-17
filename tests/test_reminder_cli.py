import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import patch

from app import main as main_module


FAKE_PLAN = {
    "mode": "preview",
    "timezone": "Asia/Shanghai",
    "plan_date": "2026-09-14",
    "generated_at": "2026-09-14T12:00:00+08:00",
    "scheduled": False,
    "notified": False,
    "auto_execute": False,
    "calendar_status": "UNVERIFIED",
    "analysis_state": "NOT_READY",
    "action": "WAIT",
    "items": [],
    "next_reminder": None,
    "notes": ["preview-only"],
}


def _capture(func, *args, **kwargs):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        result = func(*args, **kwargs)
    return result, out.getvalue(), err.getvalue()


def _assert_rejected_before_dotenv(testcase, argv):
    with patch.object(main_module, "load_dotenv") as dotenv:
        with testcase.assertRaises(SystemExit) as raised, redirect_stderr(StringIO()):
            main_module.main(argv)
    testcase.assertEqual(raised.exception.code, 2)
    dotenv.assert_not_called()


class ReminderPlanPreviewTests(unittest.TestCase):
    def test_reminder_plan_preview_prints_plan_json(self):
        with patch(
            "app.main.build_reminder_plan", create=True, return_value=dict(FAKE_PLAN)
        ) as build_plan, patch.object(
            main_module, "load_dotenv"
        ) as dotenv, patch(
            "app.main.FeishuNotifier", create=True
        ) as notifier_type:
            exit_code, out, err = _capture(main_module.main, ["--reminder-plan"])

        self.assertEqual(exit_code, 0)
        dotenv.assert_not_called()
        notifier_type.assert_not_called()
        self.assertEqual(json.loads(out), FAKE_PLAN)
        self.assertEqual(build_plan.call_args.args, ())
        self.assertEqual(set(build_plan.call_args.kwargs), {"now"})
        now = build_plan.call_args.kwargs["now"]
        self.assertIsNotNone(now.tzinfo)
        self.assertIsNotNone(now.utcoffset())

    def test_reminder_plan_preview_does_not_touch_workflow_side_effects(self):
        with patch("app.main.start_scheduler", create=True) as start, patch(
            "app.main.run_daily_reports", create=True
        ) as reports, patch("app.main.run_portfolio_report", create=True) as portfolio, patch(
            "app.main.run_market_snapshot", create=True
        ) as snapshot, patch(
            "app.main.create_default_collector", create=True
        ) as collector_factory, patch(
            "app.main.build_reminder_plan", create=True, return_value=dict(FAKE_PLAN)
        ):
            exit_code, out, err = _capture(main_module.main, ["--reminder-plan"])

        self.assertEqual(exit_code, 0)
        start.assert_not_called()
        reports.assert_not_called()
        portfolio.assert_not_called()
        snapshot.assert_not_called()
        collector_factory.assert_not_called()

    def test_reminder_plan_preview_accepts_no_notify_as_redundant_safety(self):
        with patch(
            "app.main.build_reminder_plan", create=True, return_value=dict(FAKE_PLAN)
        ), patch("app.main.FeishuNotifier", create=True) as notifier_type:
            exit_code, out, err = _capture(
                main_module.main, ["--reminder-plan", "--no-notify"]
            )

        self.assertEqual(exit_code, 0)
        notifier_type.assert_not_called()
        self.assertEqual(json.loads(out), FAKE_PLAN)

    def test_reminder_plan_with_at_converts_aware_utc_iso_string(self):
        captured = {}

        def fake_build_plan(*, now):
            captured["now"] = now
            return dict(FAKE_PLAN)

        with patch(
            "app.main.build_reminder_plan", create=True, side_effect=fake_build_plan
        ):
            exit_code, out, err = _capture(
                main_module.main,
                ["--reminder-plan", "--at", "2026-09-14T04:00:00+00:00"],
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            captured["now"], datetime(2026, 9, 14, 4, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(json.loads(out), FAKE_PLAN)

    def test_reminder_plan_with_at_accepts_offset_time(self):
        captured = {}

        def fake_build_plan(*, now):
            captured["now"] = now
            return dict(FAKE_PLAN)

        with patch(
            "app.main.build_reminder_plan", create=True, side_effect=fake_build_plan
        ):
            exit_code, out, err = _capture(
                main_module.main,
                ["--reminder-plan", "--at", "2026-09-14T12:30:00+08:00"],
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            captured["now"],
            datetime(2026, 9, 14, 12, 30, tzinfo=timezone(timedelta(hours=8))),
        )

    def test_reminder_plan_without_at_uses_current_shanghai_time(self):
        captured = {}

        def fake_build_plan(*, now):
            captured["now"] = now
            return dict(FAKE_PLAN)

        with patch(
            "app.main.build_reminder_plan", create=True, side_effect=fake_build_plan
        ):
            exit_code, out, err = _capture(main_module.main, ["--reminder-plan"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["now"].tzinfo, main_module.SHANGHAI_TZ)
        self.assertEqual(captured["now"].utcoffset(), timedelta(hours=8))

    def test_reminder_plan_preview_runs_without_dotenv_package(self):
        original = main_module.load_dotenv
        main_module.load_dotenv = None
        try:
            with patch(
                "app.main.build_reminder_plan",
                create=True,
                return_value=dict(FAKE_PLAN),
            ):
                exit_code, out, err = _capture(main_module.main, ["--reminder-plan"])
        finally:
            main_module.load_dotenv = original

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(out), FAKE_PLAN)

    def test_reminder_plan_preview_works_with_real_core_module(self):
        with patch.object(main_module, "load_dotenv") as dotenv:
            exit_code, out, err = _capture(main_module.main, ["--reminder-plan"])

        self.assertEqual(exit_code, 0)
        dotenv.assert_not_called()
        payload = json.loads(out)
        self.assertEqual(payload["mode"], "preview")
        self.assertEqual(payload["scheduled"], False)
        self.assertEqual(payload["auto_execute"], False)
        self.assertEqual(payload["action"], "WAIT")
        self.assertIn(payload["plan_date"], payload["generated_at"])


class ReminderPlanConflictTests(unittest.TestCase):
    def test_reminder_plan_conflicts_with_other_modes(self):
        for argv in (
            ["--reminder-plan", "--snapshot"],
            ["--reminder-plan", "--schedule"],
            ["--reminder-plan", "--portfolio"],
        ):
            with self.subTest(argv=argv):
                _assert_rejected_before_dotenv(self, argv)

    def test_reminder_plan_rejects_code_report_and_history_options(self):
        for argv in (
            ["--reminder-plan", "--code", "000001"],
            ["--reminder-plan", "--report-kind", "closing"],
            ["--reminder-plan", "--start", "20260901"],
            ["--reminder-plan", "--end", "20260911"],
            ["--reminder-plan", "--period", "daily"],
            ["--reminder-plan", "--adjust", "qfq"],
        ):
            with self.subTest(argv=argv):
                _assert_rejected_before_dotenv(self, argv)

    def test_reminder_plan_rejects_schedule_profile(self):
        _assert_rejected_before_dotenv(
            self, ["--reminder-plan", "--schedule-profile", "legacy"]
        )

    def test_at_requires_reminder_plan(self):
        _assert_rejected_before_dotenv(self, ["--at", "2026-09-14T12:00:00+08:00"])

    def test_at_rejects_naive_and_malformed_times(self):
        for bad in ("2026-09-14T12:00:00", "not-a-time", "2026-09-14"):
            with self.subTest(bad=bad):
                with patch.object(
                    main_module, "load_dotenv"
                ) as dotenv, patch(
                    "app.main.build_reminder_plan",
                    create=True,
                    return_value=dict(FAKE_PLAN),
                ):
                    with self.assertRaises(SystemExit) as raised, redirect_stderr(
                        StringIO()
                    ):
                        main_module.main(["--reminder-plan", "--at", bad])
                self.assertEqual(raised.exception.code, 2)
                dotenv.assert_not_called()


class ScheduleProfileTests(unittest.TestCase):
    def setUp(self):
        dotenv = patch.object(main_module, "load_dotenv")
        self.dotenv = dotenv.start()
        self.addCleanup(dotenv.stop)

    def test_schedule_with_full_day_profile_starts_checklist_scheduler(self):
        with patch("app.main.start_scheduler", create=True) as start_scheduler:
            exit_code, out, err = _capture(
                main_module.main, ["--schedule", "--schedule-profile", "full-day"]
            )

        self.assertEqual(exit_code, 0)
        start_scheduler.assert_called_once_with(profile="full-day")
        self.dotenv.assert_called_once_with()

    def test_schedule_with_legacy_profile_keeps_legacy_call(self):
        with patch("app.main.start_scheduler", create=True) as start_scheduler:
            exit_code, out, err = _capture(
                main_module.main, ["--schedule", "--schedule-profile", "legacy"]
            )

        self.assertEqual(exit_code, 0)
        start_scheduler.assert_called_once_with(profile="legacy")

    def test_plain_schedule_keeps_no_argument_call(self):
        with patch("app.main.start_scheduler", create=True) as start_scheduler:
            exit_code, out, err = _capture(main_module.main, ["--schedule"])

        self.assertEqual(exit_code, 0)
        start_scheduler.assert_called_once_with()

    def test_schedule_profile_requires_schedule_mode(self):
        for argv in (
            ["--schedule-profile", "legacy"],
            ["--schedule-profile", "full-day", "--code", "000001"],
        ):
            with self.subTest(argv=argv):
                _assert_rejected_before_dotenv(self, argv)

    def test_schedule_profile_rejects_unknown_choice(self):
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main_module.main(["--schedule", "--schedule-profile", "half-day"])
        self.assertEqual(raised.exception.code, 2)

    def test_schedule_rejects_no_notify(self):
        with patch(
            "app.main.start_scheduler", create=True
        ) as start, patch.object(
            main_module, "load_dotenv"
        ) as dotenv, redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main_module.main(["--schedule", "--no-notify"])
        self.assertEqual(raised.exception.code, 2)
        dotenv.assert_not_called()
        start.assert_not_called()


class LegacyCliRegressionTests(unittest.TestCase):
    def setUp(self):
        dotenv = patch.object(main_module, "load_dotenv")
        dotenv.start()
        self.addCleanup(dotenv.stop)

    def test_batch_mode_still_calls_run_daily_reports(self):
        batch_result = {"status": "completed", "failed": 0, "results": []}
        fake_collector = object()
        with patch(
            "app.main.create_default_collector", create=True, return_value=fake_collector
        ), patch(
            "app.main.run_daily_reports", create=True, return_value=batch_result
        ) as run_reports, redirect_stdout(StringIO()):
            exit_code = main_module.main(
                [
                    "--code",
                    "000001",
                    "--start",
                    "20260901",
                    "--end",
                    "20260911",
                    "--no-notify",
                ]
            )
        self.assertEqual(exit_code, 0)
        run_reports.assert_called_once_with(
            ["000001"],
            collector=fake_collector,
            notifier=None,
            history_start="20260901",
            history_end="20260911",
            history_period="daily",
            history_adjust="",
        )

    def test_schedule_mode_without_profile_calls_start_scheduler_no_args(self):
        with patch("app.main.start_scheduler", create=True) as start:
            exit_code = main_module.main(["--schedule"])
        self.assertEqual(exit_code, 0)
        start.assert_called_once_with()

    def test_snapshot_mode_without_codes_uses_default_market_codes(self):
        snapshot_result = {
            "status": "completed",
            "failed": 0,
            "quotes": [],
            "report": "snap",
        }
        fake_collector = object()
        with patch("app.main.SinaProvider", create=True), patch(
            "app.main.MarketCollector", create=True, return_value=fake_collector
        ), patch(
            "app.main.run_market_snapshot", create=True, return_value=snapshot_result
        ) as run_snapshot, redirect_stdout(StringIO()):
            exit_code = main_module.main(["--snapshot", "--no-notify"])
        self.assertEqual(exit_code, 0)
        run_snapshot.assert_called_once_with(
            main_module.DEFAULT_MARKET_CODES, collector=fake_collector, notifier=None
        )

    def test_portfolio_mode_still_works_with_report_kind(self):
        result = {
            "status": "completed",
            "report": "ok",
            "notified": False,
            "errors": [],
        }
        sentinel_fundamental = object()
        with patch(
            "app.main.run_portfolio_report", create=True, return_value=result
        ) as run_report, patch(
            "app.main.create_fundamental_provider", return_value=sentinel_fundamental
        ) as create_fundamental, patch(
            "app.main.FeishuNotifier", create=True
        ) as notifier_type, redirect_stdout(StringIO()):
            exit_code = main_module.main(
                ["--portfolio", "--report-kind", "closing", "--no-notify"]
            )
        self.assertEqual(exit_code, 0)
        notifier_type.assert_not_called()
        create_fundamental.assert_called_once_with()
        run_report.assert_called_once_with(
            report_kind="closing",
            notifier=None,
            fundamental_provider=sentinel_fundamental,
        )
        self.assertNotIn("global_provider", run_report.call_args.kwargs)
        self.assertNotIn("treasury_fallback", run_report.call_args.kwargs)

    def test_report_kind_requires_portfolio(self):
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main_module.main(["--report-kind", "morning"])
        self.assertEqual(raised.exception.code, 2)

    def test_code_required_without_snapshot(self):
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main_module.main([])
        self.assertEqual(raised.exception.code, 2)

    def test_existing_mode_conflicts_still_rejected(self):
        for argv in (
            ["--portfolio", "--snapshot"],
            ["--portfolio", "--schedule"],
            ["--portfolio", "--reminder-plan"],
        ):
            with self.subTest(argv=argv), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main_module.main(argv)
                self.assertEqual(raised.exception.code, 2)

    def test_portfolio_mode_still_rejects_code(self):
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main_module.main(["--portfolio", "--code", "000001"])
        self.assertEqual(raised.exception.code, 2)

    def test_schedule_still_rejects_code(self):
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main_module.main(["--schedule", "--code", "000001"])
        self.assertEqual(raised.exception.code, 2)


class ReminderScheduleProfileCliTests(unittest.TestCase):
    def test_parser_has_schedule_profile_option_matching_readme(self):
        parser = main_module.build_parser()
        action = next(
            (a for a in parser._actions if "--schedule-profile" in a.option_strings),
            None,
        )
        self.assertIsNotNone(action)
        self.assertEqual(set(action.choices), {"legacy", "four-stage", "full-day"})

    def test_schedule_profile_invokes_start_scheduler_with_profile(self):
        for profile in ("legacy", "full-day"):
            with self.subTest(profile=profile):
                with patch.object(
                    main_module, "start_scheduler"
                ) as mock_start, patch.object(
                    main_module, "load_dotenv"
                ):
                    exit_code = main_module.main(
                        ["--schedule", "--schedule-profile", profile]
                    )
                self.assertEqual(exit_code, 0)
                mock_start.assert_called_once_with(profile=profile)

    def test_schedule_without_profile_calls_start_scheduler_default(self):
        with patch.object(
            main_module, "start_scheduler"
        ) as mock_start, patch.object(
            main_module, "load_dotenv"
        ):
            exit_code = main_module.main(["--schedule"])
        self.assertEqual(exit_code, 0)
        mock_start.assert_called_once_with()

    def test_schedule_profile_requires_schedule_flag(self):
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main_module.main(["--code", "000001", "--schedule-profile", "full-day"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
