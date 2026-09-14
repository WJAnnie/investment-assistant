"""Independent, offline scheduler regressions for manual checklists."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from app.workflow import scheduler as scheduler_module


SHANGHAI = ZoneInfo("Asia/Shanghai")
SLOTS = (
    ("GLOBAL_0630", 6, 30),
    ("NEWS_0810", 8, 10),
    ("PLAN_0845", 8, 45),
    ("OPEN_VERIFY", 10, 0),
    ("NOON_1130", 11, 30),
    ("AFTERNOON_CHECK", 13, 15),
    ("TRADE_1430", 14, 30),
    ("CLOSE_1610", 16, 10),
    ("RESEARCH_2030", 20, 30),
)


class _Scheduler:
    def __init__(self):
        self.jobs = []
        self.started = False

    def add_job(self, func, trigger, **kwargs):
        self.jobs.append((func, trigger, kwargs))

    def start(self):
        self.started = True


class ReminderSchedulerTests(unittest.TestCase):
    def test_registration_has_nine_isolated_slots_and_no_delivery_or_start(self):
        target = _Scheduler()
        notifier = Mock()
        clock = Mock()
        with patch.object(scheduler_module, "FeishuNotifier") as factory, patch.object(
            scheduler_module, "run_portfolio_report"
        ) as run_report:
            result = scheduler_module.register_reminder_jobs(
                target, notifier=notifier, clock=clock
            )
        self.assertIs(result, target)
        self.assertFalse(target.started)
        factory.assert_not_called()
        clock.assert_not_called()
        notifier.send.assert_not_called()
        run_report.assert_not_called()
        self.assertEqual(len(target.jobs), len(SLOTS))
        for (func, trigger, kwargs), (task_id, hour, minute) in zip(target.jobs, SLOTS):
            with self.subTest(task_id=task_id):
                self.assertIs(func, scheduler_module._run_scheduled_reminder)
                self.assertEqual(trigger, "cron")
                self.assertEqual(kwargs["id"], f"reminder-{task_id}")
                self.assertEqual((kwargs["hour"], kwargs["minute"]), (hour, minute))
                self.assertEqual(kwargs["timezone"], "Asia/Shanghai")
                self.assertEqual(kwargs["day_of_week"], "mon-fri")
                self.assertEqual(kwargs["misfire_grace_time"], 300)
                self.assertIs(kwargs["coalesce"], True)
                self.assertEqual(kwargs["max_instances"], 1)
                self.assertIs(kwargs["replace_existing"], True)
                self.assertEqual(kwargs["kwargs"]["task_id"], task_id)
                self.assertIs(kwargs["kwargs"]["notifier"], notifier)
                self.assertIs(kwargs["kwargs"]["clock"], clock)

    def test_real_cron_is_shanghai_even_when_scheduler_default_is_utc(self):
        target = BlockingScheduler(timezone="UTC")
        scheduler_module.register_reminder_jobs(target, notifier=Mock())
        first = target.get_jobs()[0]
        monday_start = datetime(2026, 9, 13, 16, tzinfo=timezone.utc)
        self.assertEqual(
            first.trigger.get_next_fire_time(None, monday_start),
            datetime(2026, 9, 14, 6, 30, tzinfo=SHANGHAI),
        )
        self.assertFalse(target.running)

    def test_every_due_callback_is_checklist_only_and_never_collects_market_data(self):
        for task_id, hour, minute in SLOTS:
            with self.subTest(task_id=task_id):
                notifier = Mock()
                notifier.send.return_value = True
                now = datetime(2026, 9, 14, hour, minute, tzinfo=SHANGHAI)
                with patch.object(scheduler_module, "run_portfolio_report") as run_report:
                    result = scheduler_module._run_scheduled_reminder(
                        notifier, task_id, clock=lambda: now
                    )
                run_report.assert_not_called()
                self.assertEqual(result["status"], "completed")
                self.assertIs(result["notified"], True)
                self.assertIs(result["auto_execute"], False)
                self.assertEqual(result["action"], "WAIT")
                self.assertEqual(result["analysis_state"], "NOT_READY")
                self.assertEqual(result["calendar_status"], "UNVERIFIED")
                self.assertEqual(result["reminder_id"], f"2026-09-14:{task_id}")
                self.assertIn("不自动下单", result["report"])
                self.assertIn("WAIT", result["report"])
                self.assertIn("交易日历", result["report"])
                notifier.send.assert_called_once_with(result["report"])

    def test_due_window_opens_at_start_and_closes_at_expiration(self):
        for task_id, hour, minute in SLOTS:
            scheduled = datetime(2026, 9, 14, hour, minute, tzinfo=SHANGHAI)
            for offset, should_send in ((-1, False), (0, True), (1, True), (299, True), (300, False), (301, False)):
                with self.subTest(task_id=task_id, offset=offset):
                    notifier = Mock()
                    notifier.send.return_value = True
                    now = scheduled + timedelta(seconds=offset)
                    result = scheduler_module._run_scheduled_reminder(
                        notifier, task_id, clock=lambda: now
                    )
                    self.assertEqual(result["status"], "completed" if should_send else "skipped")
                    self.assertIs(result["notified"], should_send)
                    self.assertEqual(notifier.send.call_count, int(should_send))

    def test_weekend_callbacks_do_not_send_or_guess_next_trading_day(self):
        for day in (19, 20):
            with self.subTest(day=day):
                notifier = Mock()
                result = scheduler_module._run_scheduled_reminder(
                    notifier, "TRADE_1430",
                    clock=lambda: datetime(2026, 9, day, 14, 30, tzinfo=SHANGHAI),
                )
                self.assertEqual(result["status"], "skipped")
                self.assertIs(result["notified"], False)
                notifier.send.assert_not_called()

    def test_clock_utc_equivalence_uses_local_date_and_stable_reminder_id(self):
        notifier = Mock()
        notifier.send.return_value = True
        result = scheduler_module._run_scheduled_reminder(
            notifier, "GLOBAL_0630",
            clock=lambda: datetime(2026, 9, 13, 22, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(result["reminder_id"], "2026-09-14:GLOBAL_0630")
        self.assertEqual(result["scheduled_at"], "2026-09-14T06:30:00+08:00")
        self.assertIs(result["notified"], True)

    def test_invalid_clock_or_stage_fails_closed_without_message(self):
        valid_now = datetime(2026, 9, 14, 14, 30, tzinfo=SHANGHAI)
        cases = (
            ("TRADE_1430", None),
            ("TRADE_1430", datetime(2026, 9, 14, 14, 30)),
            ("TRADE_1430", "2026-09-14T14:30:00+08:00"),
            ("unknown-fixture-private-detail", valid_now),
            (None, valid_now),
            (["TRADE_1430"], valid_now),
        )
        for task_id, now in cases:
            with self.subTest(task_id=task_id, now=now):
                notifier = Mock()
                result = scheduler_module._run_scheduled_reminder(
                    notifier, task_id, clock=lambda: now
                )
                self.assertEqual(result["status"], "failed")
                self.assertIs(result["notified"], False)
                self.assertIs(result["auto_execute"], False)
                self.assertNotIn("fixture-private-detail", str(result))
                notifier.send.assert_not_called()

    def test_clock_exception_is_safely_reported_without_notification(self):
        notifier = Mock()
        clock = Mock(side_effect=RuntimeError("fixture-private-clock-detail"))
        result = scheduler_module._run_scheduled_reminder(
            notifier, "TRADE_1430", clock=clock
        )
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("fixture-private-clock-detail", str(result))
        notifier.send.assert_not_called()

    def test_delivery_requires_literal_true_and_one_bounded_attempt(self):
        for acknowledgment in (False, None, 0, 1, "true", {"ok": True}):
            with self.subTest(acknowledgment=acknowledgment):
                notifier = Mock()
                notifier.send.return_value = acknowledgment
                result = scheduler_module._run_scheduled_reminder(
                    notifier, "TRADE_1430",
                    clock=lambda: datetime(2026, 9, 14, 14, 30, tzinfo=SHANGHAI),
                )
                self.assertEqual(result["status"], "partial")
                self.assertIs(result["notified"], False)
                notifier.send.assert_called_once()
                self.assertTrue(result["errors"])

    def test_notification_exception_never_exposes_provider_detail(self):
        notifier = Mock()
        notifier.send.side_effect = RuntimeError("fixture-private-delivery-detail")
        with self.assertLogs(scheduler_module.logger, level="WARNING") as logs:
            result = scheduler_module._run_scheduled_reminder(
                notifier, "TRADE_1430",
                clock=lambda: datetime(2026, 9, 14, 14, 30, tzinfo=SHANGHAI),
            )
        self.assertEqual(result["status"], "partial")
        self.assertIs(result["notified"], False)
        self.assertNotIn("fixture-private-delivery-detail", str(result) + str(logs.output))
        notifier.send.assert_called_once()

    def test_full_day_start_registers_only_checklist_jobs(self):
        target = _Scheduler()
        with patch.object(scheduler_module, "scheduler", target), patch.object(
            scheduler_module, "FeishuNotifier", return_value=Mock()
        ):
            scheduler_module.start_scheduler(profile="full-day")
        self.assertTrue(target.started)
        self.assertEqual(len(target.jobs), 9)
        self.assertTrue(all(job[2]["id"].startswith("reminder-") for job in target.jobs))

    def test_invalid_profile_cannot_create_notifier_register_jobs_or_start(self):
        target = _Scheduler()

        class EqualsEverything:
            def __eq__(self, other):
                return True

        class BrokenEquality:
            def __eq__(self, other):
                raise RuntimeError("fixture-private-profile-detail")

        for profile in ("unknown", None, True, [], EqualsEverything(), BrokenEquality()):
            with self.subTest(profile=profile), patch.object(
                scheduler_module, "scheduler", target
            ), patch.object(scheduler_module, "FeishuNotifier") as factory:
                with self.assertRaises(ValueError):
                    scheduler_module.start_scheduler(profile=profile)
                factory.assert_not_called()
                self.assertFalse(target.started)
                self.assertEqual(target.jobs, [])

    def test_string_subclasses_cannot_forge_scheduler_profile(self):
        class EqualsEverything(str):
            def __eq__(self, other):
                return True

        class BrokenEquality(str):
            def __eq__(self, other):
                raise RuntimeError("fixture-private-profile-detail")

        for profile in (EqualsEverything("bogus"), BrokenEquality("bogus")):
            target = _Scheduler()
            with self.subTest(kind=type(profile).__name__), patch.object(
                scheduler_module, "scheduler", target
            ), patch.object(scheduler_module, "FeishuNotifier") as factory:
                with self.assertRaises(ValueError):
                    scheduler_module.start_scheduler(profile=profile)
                factory.assert_not_called()
                self.assertFalse(target.started)
                self.assertEqual(target.jobs, [])

    def test_falsey_injected_dependencies_are_not_replaced(self):
        class FalseyScheduler(_Scheduler):
            def __bool__(self):
                return False

        class FalseyNotifier:
            def __bool__(self):
                return False

        target = FalseyScheduler()
        notifier = FalseyNotifier()
        with patch.object(scheduler_module, "FeishuNotifier") as factory:
            result = scheduler_module.register_reminder_jobs(target, notifier=notifier)
        self.assertIs(result, target)
        self.assertEqual(len(target.jobs), 9)
        self.assertTrue(all(job[2]["kwargs"]["notifier"] is notifier for job in target.jobs))
        factory.assert_not_called()

    def test_invalid_injected_clock_is_rejected_before_registration(self):
        target = _Scheduler()
        with patch.object(scheduler_module, "FeishuNotifier") as factory:
            with self.assertRaises(ValueError):
                scheduler_module.register_reminder_jobs(target, clock="not-callable")
        self.assertEqual(target.jobs, [])
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
