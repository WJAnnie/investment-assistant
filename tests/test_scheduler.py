import unittest
from unittest.mock import Mock, patch

from app.workflow import scheduler as scheduler_module


class _Scheduler:
    def __init__(self):
        self.jobs = []
        self.started = False

    def add_job(self, func, trigger, **kwargs):
        self.jobs.append((func, trigger, kwargs))

    def start(self):
        self.started = True


class SchedulerTests(unittest.TestCase):
    def test_falsey_scheduler_is_preserved_in_legacy_registration(self):
        class FalseyScheduler(_Scheduler):
            def __bool__(self):
                return False

        target = FalseyScheduler()
        with patch.object(scheduler_module, "scheduler") as default_scheduler:
            result = scheduler_module.register_default_jobs(target, notifier=object())

        self.assertIs(result, target)
        self.assertEqual(len(target.jobs), 5)
        default_scheduler.add_job.assert_not_called()

    def test_falsey_notifier_never_initializes_live_notifier_in_legacy_registration(self):
        class FalseyNotifier:
            def __bool__(self):
                return False

        target = _Scheduler()
        notifier = FalseyNotifier()
        with patch.object(scheduler_module, "FeishuNotifier") as notifier_factory:
            scheduler_module.register_default_jobs(target, notifier=notifier)

        notifier_factory.assert_not_called()
        self.assertEqual(len(target.jobs), 5)
        for _, _, options in target.jobs:
            self.assertIs(options["kwargs"]["notifier"], notifier)

    def test_registers_five_stage_specific_portfolio_report_times(self):
        target = _Scheduler()
        notifier = object()

        result = scheduler_module.register_default_jobs(
            target,
            notifier=notifier,
        )

        self.assertIs(result, target)
        self.assertEqual(
            [job[2]["id"] for job in target.jobs],
            [
                "global-market-scan",
                "morning-report",
                "midday-analysis",
                "trading-assistant",
                "closing-review",
            ],
        )
        self.assertEqual(
            [
                (
                    job[2]["kwargs"]["report_title"],
                    job[2]["kwargs"]["report_kind"],
                    job[2]["hour"],
                    job[2]["minute"],
                )
                for job in target.jobs
            ],
            [
                ("全球市场扫描", "global", 6, 30),
                ("投资晨报", "morning", 9, 0),
                ("午盘分析", "midday", 11, 30),
                ("人工交易复核提醒", "trading", 14, 30),
                ("A/H股收盘复盘", "closing", 16, 10),
            ],
        )
        for _, trigger, kwargs in target.jobs:
            self.assertEqual(trigger, "cron")
            self.assertEqual(kwargs["day_of_week"], "mon-fri")
            self.assertEqual(kwargs["misfire_grace_time"], 1800)
            self.assertTrue(kwargs["coalesce"])
            self.assertEqual(kwargs["max_instances"], 1)
            self.assertIs(kwargs["kwargs"]["notifier"], notifier)

    def test_each_scheduled_job_invokes_shared_portfolio_workflow(self):
        target = _Scheduler()
        notifier = object()
        calls = []

        def fake_run_portfolio_report(*, report_kind, notifier):
            calls.append((report_kind, notifier))
            return {
                "status": "completed", "report": "safe reminder",
                "notified": True, "report_kind": report_kind,
            }

        with patch.object(scheduler_module, "run_portfolio_report", fake_run_portfolio_report):
            scheduler_module.register_default_jobs(target, notifier=notifier)
            results = [
                func(**kwargs["kwargs"])
                for func, _, kwargs in target.jobs
            ]

        self.assertEqual(
            calls,
            [
                ("global", notifier),
                ("morning", notifier),
                ("midday", notifier),
                ("trading", notifier),
                ("closing", notifier),
            ],
        )
        self.assertEqual([result["report_kind"] for result in results], [
            "global", "morning", "midday", "trading", "closing"
        ])

    def test_failed_scheduled_report_is_logged_even_when_wait_reminder_was_sent(self):
        with patch.object(
            scheduler_module,
            "run_portfolio_report",
            return_value={
                "status": "failed", "notified": True,
                "report": "safe WAIT reminder",
            },
        ), self.assertLogs(scheduler_module.logger, level="ERROR") as logs:
            result = scheduler_module._run_scheduled_portfolio_report(
                object(), "人工交易复核提醒", "trading"
            )

        self.assertEqual(result["status"], "failed")
        self.assertTrue(any("failed" in message for message in logs.output))

    def test_unsent_scheduled_reminder_is_retried_once(self):
        class RetryNotifier:
            def __init__(self):
                self.messages = []

            def send(self, message):
                self.messages.append(message)
                return True

        notifier = RetryNotifier()
        initial = {
            "status": "partial",
            "notified": False,
            "report": "safe WAIT reminder",
            "errors": ["notification: message was not sent"],
        }
        with patch.object(
            scheduler_module, "run_portfolio_report", return_value=initial
        ):
            result = scheduler_module._run_scheduled_portfolio_report(
                notifier, "人工交易复核提醒", "trading"
            )

        self.assertTrue(result["notified"])
        self.assertEqual(notifier.messages, ["safe WAIT reminder"])

    def test_unhandled_workflow_exception_sends_safe_wait_reminder(self):
        notifier = Mock()
        notifier.send.return_value = True
        with patch.object(
            scheduler_module, "run_portfolio_report",
            side_effect=RuntimeError("fixture-private-provider-detail"),
        ):
            result = scheduler_module._run_scheduled_portfolio_report(
                notifier, "人工交易复核提醒", "trading"
            )

        self.assertEqual(result["status"], "failed")
        self.assertIs(result["notified"], True)
        self.assertIn("WAIT", result["report"])
        self.assertIn("不自动下单", result["report"])
        self.assertIn("+0800", result["report"])
        self.assertNotIn("fixture-private-provider-detail", str(result))
        notifier.send.assert_called_once_with(result["report"])

    def test_invalid_workflow_result_falls_back_to_safe_reminder(self):
        for invalid in (
            None, [], {}, {"status": "completed", "report": ""},
            {"status": "completed", "report": "   "},
            {"status": "completed", "report": 42},
            {"status": ["completed"], "report": "unsafe"},
            {"status": "completed", "report": "unsafe", "errors": "invalid"},
        ):
            with self.subTest(invalid=invalid):
                notifier = Mock()
                notifier.send.return_value = True
                with patch.object(scheduler_module, "run_portfolio_report", return_value=invalid):
                    result = scheduler_module._run_scheduled_portfolio_report(
                        notifier, "人工交易复核提醒", "trading"
                    )
                self.assertEqual(result["status"], "failed")
                self.assertIn("WAIT", result["report"])
                notifier.send.assert_called_once_with(result["report"])

    def test_failed_retry_is_bounded_and_never_claims_delivery(self):
        for response in (False, "false", 1, {"ok": False}):
            with self.subTest(response=response):
                notifier = Mock()
                notifier.send.return_value = response
                initial = {
                    "status": "partial", "notified": False,
                    "report": "safe WAIT reminder", "errors": [],
                }
                with patch.object(scheduler_module, "run_portfolio_report", return_value=initial):
                    result = scheduler_module._run_scheduled_portfolio_report(
                        notifier, "人工交易复核提醒", "trading"
                    )
                self.assertIs(result["notified"], False)
                self.assertEqual(result["status"], "partial")
                notifier.send.assert_called_once_with("safe WAIT reminder")

    def test_fallback_notification_exception_is_recorded_without_leaking_details(self):
        notifier = Mock()
        notifier.send.side_effect = RuntimeError("fixture-webhook-secret")
        with patch.object(scheduler_module, "run_portfolio_report", side_effect=ValueError("broken workflow")):
            result = scheduler_module._run_scheduled_portfolio_report(
                notifier, "人工交易复核提醒", "trading"
            )
        self.assertEqual(result["status"], "failed")
        self.assertIs(result["notified"], False)
        self.assertNotIn("fixture-webhook-secret", str(result))
        notifier.send.assert_called_once()

    def test_truthy_initial_delivery_flag_requires_explicit_confirmation(self):
        notifier = Mock()
        notifier.send.return_value = False
        with patch.object(scheduler_module, "run_portfolio_report", return_value={
            "status": "completed", "report": "safe WAIT reminder",
            "notified": "false",
        }):
            result = scheduler_module._run_scheduled_portfolio_report(
                notifier, "人工交易复核提醒", "trading"
            )
        self.assertIs(result["notified"], False)
        self.assertEqual(result["status"], "partial")
        notifier.send.assert_called_once()

    def test_real_failure_workflow_and_scheduler_make_only_one_attempt(self):
        from app.workflow.portfolio import run_portfolio_report

        notifier = Mock()
        notifier.send.side_effect = [False, True]

        def failing_workflow(**kwargs):
            return run_portfolio_report(
                config_loader=Mock(side_effect=ValueError("fixture-private-detail")),
                **kwargs,
            )

        with patch.object(scheduler_module, "run_portfolio_report", side_effect=failing_workflow):
            result = scheduler_module._run_scheduled_portfolio_report(
                notifier, "人工交易复核提醒", "trading"
            )
        self.assertEqual(notifier.send.call_count, 1)
        self.assertEqual(result["status"], "failed")
        self.assertIs(result["notified"], False)
        self.assertIn("WAIT", result["report"])
        self.assertNotIn("fixture-private-detail", result["report"])

    def test_start_scheduler_registers_jobs_before_blocking(self):
        target = _Scheduler()

        with patch.object(scheduler_module, "scheduler", target):
            scheduler_module.start_scheduler(codes=("SH.000001",))

        self.assertTrue(target.started)
        self.assertEqual(len(target.jobs), 5)


if __name__ == "__main__":
    unittest.main()
