"""Offline scheduling contracts, not live five-day acceptance evidence."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from app.workflow import scheduler as scheduling
from tests.test_scheduler import _Scheduler
from tests.test_private_stage_workflow import run_stage


SHANGHAI = ZoneInfo("Asia/Shanghai")
SLOTS = (("morning", 9, 0), ("midday", 11, 30),
         ("trading", 14, 30), ("closing", 16, 10))


class FourStageSchedulerTests(unittest.TestCase):
    def test_registers_exactly_four_private_stage_jobs_without_starting(self):
        target, notifier = _Scheduler(), Mock()
        with tempfile.TemporaryDirectory() as raw:
            result = scheduling.register_four_stage_jobs(target, notifier=notifier, private_state_dir=raw)
            self.assertIs(result, target)
            self.assertFalse(target.started)
            self.assertEqual(len(target.jobs), 4)
            for (_, trigger, options), (kind, hour, minute) in zip(target.jobs, SLOTS):
                self.assertEqual(trigger, "cron")
                self.assertEqual((options["hour"], options["minute"]), (hour, minute))
                self.assertEqual(options["timezone"], "Asia/Shanghai")
                self.assertEqual(options["day_of_week"], "mon-fri")
                self.assertEqual(options["misfire_grace_time"], 600)
                self.assertIs(options["coalesce"], True)
                self.assertEqual(options["max_instances"], 1)
                self.assertEqual(options["kwargs"]["report_kind"], kind)
                self.assertEqual(options["kwargs"]["private_state_dir"], Path(raw).resolve())
            notifier.send.assert_not_called()
            self.assertEqual(list(Path(raw).iterdir()), [])

    def test_real_cron_overrides_host_timezone(self):
        target = BlockingScheduler(timezone="UTC")
        scheduling.register_four_stage_jobs(target, notifier=Mock())
        first = target.get_jobs()[0]
        self.assertEqual(first.trigger.get_next_fire_time(None, datetime(2026, 9, 14, tzinfo=timezone.utc)),
                         datetime(2026, 9, 14, 9, tzinfo=SHANGHAI))
        self.assertFalse(target.running)

    def test_callbacks_only_analyze_in_the_current_stage_window(self):
        for kind, hour, minute in SLOTS:
            scheduled = datetime(2026, 9, 15, hour, minute, tzinfo=SHANGHAI)
            for offset, due in ((-1, False), (0, True), (599, True), (600, True), (601, False)):
                with self.subTest(kind=kind, offset=offset):
                    notifier = Mock()
                    target = _Scheduler()
                    clock = lambda: scheduled + timedelta(seconds=offset)
                    scheduling.register_four_stage_jobs(target, notifier=notifier, clock=clock)
                    func, _, options = next(job for job in target.jobs if job[2]["kwargs"]["report_kind"] == kind)
                    with patch.object(scheduling, "run_portfolio_report", return_value={
                        "status": "completed", "report": "synthetic WAIT", "notified": True,
                    }) as report:
                        result = func(**options["kwargs"])
                    self.assertEqual(report.call_count, int(due))
                    self.assertEqual(result["status"], "completed" if due else "skipped")
                    self.assertEqual(result["calendar_status"], "UNVERIFIED")
                    notifier.send.assert_not_called()

    def test_bad_clock_and_weekend_never_analyze_or_send(self):
        for value in (None, False, "bad", datetime(2026, 9, 15, 9),
                      datetime(2026, 9, 19, 9, tzinfo=SHANGHAI)):
            with self.subTest(value=value):
                target, notifier = _Scheduler(), Mock()
                scheduling.register_four_stage_jobs(target, notifier=notifier, clock=lambda: value)
                func, _, options = target.jobs[0]
                with patch.object(scheduling, "run_portfolio_report") as report:
                    result = func(**options["kwargs"])
                self.assertIn(result["status"], ("failed", "skipped"))
                report.assert_not_called()
                notifier.send.assert_not_called()

    def test_slow_collection_cannot_send_after_the_stage_deadline(self):
        target, notifier = _Scheduler(), Mock()
        start = datetime(2026, 9, 15, 9, tzinfo=SHANGHAI)
        scheduling.register_four_stage_jobs(target, notifier=notifier,
                                             clock=Mock(side_effect=(start, start + timedelta(seconds=601))))
        func, _, options = target.jobs[0]
        with patch.object(scheduling, "run_portfolio_report", return_value={
            "status": "completed", "report": "synthetic WAIT", "notified": False,
        }) as report:
            result = func(**options["kwargs"])
        self.assertIsNone(report.call_args.kwargs["notifier"])
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "expired_after_analysis")
        notifier.send.assert_not_called()

    def test_scheduler_delivery_updates_only_api_acceptance(self):
        target, notifier = _Scheduler(), Mock()
        notifier.send.return_value = True
        scheduling.register_four_stage_jobs(target, notifier=notifier,
                                             clock=lambda: datetime(2026, 9, 15, 9, tzinfo=SHANGHAI))
        func, _, options = target.jobs[0]
        with patch.object(scheduling, "run_portfolio_report", return_value={
            "status": "completed", "report": "synthetic WAIT", "notified": False,
            "delivery": {"api_accepted": False, "api_receipt_id": None,
                         "chatgpt_received": False, "device_received": False},
        }):
            result = func(**options["kwargs"])
        self.assertTrue(result["delivery"]["api_accepted"])
        self.assertFalse(result["delivery"]["chatgpt_received"])
        self.assertFalse(result["delivery"]["device_received"])
        self.assertIsNone(result["delivery"]["api_receipt_id"])
        notifier.send.assert_called_once_with(result["report"])

    def test_clock_rollback_or_date_change_after_analysis_prevents_delivery(self):
        start = datetime(2026, 9, 15, 9, 5, tzinfo=SHANGHAI)
        for finish in (start - timedelta(seconds=1), start + timedelta(days=1)):
            with self.subTest(finish=finish):
                notifier = Mock()
                with patch.object(scheduling, "run_portfolio_report", return_value={
                    "status": "completed", "report": "synthetic WAIT", "notified": False,
                    "notification_allowed": True,
                }):
                    result = scheduling._run_four_stage_report(
                        notifier, "synthetic morning", "morning",
                        clock=Mock(side_effect=(start, finish)))
                self.assertIn(result["status"], ("failed", "skipped"))
                self.assertFalse(result["notification_allowed"])
                notifier.send.assert_not_called()

    def test_invalid_private_setup_precedes_notifier_construction(self):
        for profile, directory in (("four-stage", Path.cwd() / "data"), ("full-day", Path.cwd() / ".private")):
            target = _Scheduler()
            with self.subTest(profile=profile), patch.object(scheduling, "scheduler", target), patch.object(
                scheduling, "FeishuNotifier"
            ) as factory:
                with self.assertRaises(ValueError):
                    scheduling.start_scheduler(profile=profile, private_state_dir=directory)
                factory.assert_not_called()
                self.assertEqual(target.jobs, [])
                self.assertFalse(target.started)

    def test_presend_clock_cannot_precede_generated_evidence_without_a_stage_store(self):
        start = datetime(2026, 9, 15, 9, tzinfo=SHANGHAI)
        report = run_stage("morning", start + timedelta(minutes=3))
        notifier = Mock(send=Mock(return_value=True))
        with patch.object(scheduling, "run_portfolio_report", return_value=report):
            result = scheduling._run_four_stage_report(
                notifier, "morning", "morning",
                clock=Mock(side_effect=(start, start + timedelta(minutes=2))))
        notifier.send.assert_not_called()
        self.assertFalse(result["notification_allowed"])
        self.assertFalse(result["notification_attempted"])

    def test_evidence_cutoff_cannot_precede_this_scheduler_attempt(self):
        start = datetime(2026, 9, 15, 9, 3, tzinfo=SHANGHAI)
        report = run_stage("morning", start - timedelta(minutes=1))
        notifier = Mock(send=Mock(return_value=True))
        with patch.object(scheduling, "run_portfolio_report", return_value=report):
            result = scheduling._run_four_stage_report(
                notifier, "morning", "morning", clock=lambda: start)
        notifier.send.assert_not_called()
        self.assertFalse(result["notification_allowed"])

    def test_start_four_stage_and_private_legacy_preserve_profile(self):
        for profile, count in (("four-stage", 4), ("legacy", 5)):
            target = _Scheduler()
            with tempfile.TemporaryDirectory() as raw, patch.object(scheduling, "scheduler", target), patch.object(
                scheduling, "FeishuNotifier", return_value=Mock()
            ):
                scheduling.start_scheduler(profile=profile, private_state_dir=raw)
                self.assertTrue(target.started)
                self.assertEqual(len(target.jobs), count)
                self.assertTrue(all(job[2]["kwargs"]["private_state_dir"] == Path(raw).resolve() for job in target.jobs))


if __name__ == "__main__":
    unittest.main()
