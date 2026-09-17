"""Offline wiring tests: the overnight providers must reach the workflow from
every real composition root, and only from the stages that display them.

These tests use sentinel objects and never construct a live transport, so a
passing suite is evidence of wiring, not of connectivity.
"""

from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from app import main as cli
from app.integration import github_export as exporter
from app.workflow import scheduler as scheduler_module


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 17, 9, 0, tzinfo=SHANGHAI)
GLOBAL = object()
TREASURY = object()
PRIVATE_ENV = {
    "GITHUB_REPOSITORY": "fixture-owner/private-data",
    "GITHUB_WORKFLOW": "Private fixture export",
    "GITHUB_RUN_ID": "fixture-run",
    "GITHUB_SHA": "fixture-sha",
    "GITHUB_ACTIONS": "true",
    "GITHUB_REPOSITORY_VISIBILITY": "private",
}


def _sentinel_providers():
    return (GLOBAL, TREASURY)


def _export_result(report_kind):
    return {
        "status": "completed",
        "report_kind": report_kind,
        "snapshot": {"account_id": "fixture-account", "total_assets": "1.00"},
        "valuations": {},
        "analysis": {"coverage": {"ready": 1, "total": 1, "unavailable": 0},
                     "items": [{"status": "ready", "bar_status": "complete"}]},
        "report": "Synthetic report; WAIT; no automatic orders.",
        "errors": [],
        "notified": False,
    }


class _Scheduler:
    def __init__(self):
        self.jobs = []
        self.started = False

    def add_job(self, func, trigger, **kwargs):
        self.jobs.append((func, trigger, kwargs))

    def start(self):
        self.started = True


class ExportWiringTests(unittest.TestCase):
    def test_default_runner_call_stays_three_keywords(self):
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            return _export_result(kwargs["report_kind"])

        with TemporaryDirectory() as directory:
            exporter.collect_snapshot(
                "closing", Path(directory) / "current.json",
                runner=runner, now=NOW, environ=PRIVATE_ENV,
            )

        self.assertEqual(
            calls, [{"report_kind": "closing", "notifier": None, "now": NOW}]
        )

    def test_injected_providers_are_forwarded_verbatim_to_the_runner(self):
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            return _export_result(kwargs["report_kind"])

        with TemporaryDirectory() as directory:
            exporter.collect_snapshot(
                "morning", Path(directory) / "current.json",
                runner=runner, now=NOW, environ=PRIVATE_ENV,
                global_provider=GLOBAL,
                treasury_fallback=TREASURY,
            )

        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0]["global_provider"], GLOBAL)
        self.assertIs(calls[0]["treasury_fallback"], TREASURY)
        self.assertEqual(calls[0]["report_kind"], "morning")
        self.assertIsNone(calls[0]["notifier"])

    def test_default_signature_maintains_no_global_state(self):
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            return _export_result(kwargs["report_kind"])

        with TemporaryDirectory() as directory:
            exporter.collect_snapshot(
                "morning", Path(directory) / "1.json",
                runner=runner, now=NOW, environ=PRIVATE_ENV,
                global_provider=GLOBAL,
                treasury_fallback=TREASURY,
            )
            exporter.collect_snapshot(
                "morning", Path(directory) / "2.json",
                runner=runner, now=NOW, environ=PRIVATE_ENV,
            )

        self.assertEqual(len(calls), 2)
        self.assertIn("global_provider", calls[0])
        self.assertEqual(
            calls[1], {"report_kind": "morning", "notifier": None, "now": NOW}
        )

    def test_provider_factory_is_not_invoked_before_the_private_boundary(self):
        with TemporaryDirectory() as directory, patch(
            "app.integration.github_export.create_global_market_providers",
            return_value=_sentinel_providers(),
        ) as factory:
            with self.assertRaises(ValueError):
                exporter.main(
                    ["collect", "--report-kind", "morning", "--output", "ignored.json"]
                )

        factory.assert_not_called()

    def test_collect_cli_wires_providers_only_for_overnight_stages(self):
        for report_kind, expected in (("morning", True), ("global", True),
                                      ("midday", False), ("trading", False),
                                      ("closing", False)):
            with self.subTest(report_kind=report_kind), TemporaryDirectory() as directory:
                captured = {}
                out_path = str(Path(directory) / "current.json")

                def fake_collect(kind, output, **kwargs):
                    captured["kind"] = kind
                    captured["global_provider"] = kwargs.get("global_provider")
                    captured["treasury_fallback"] = kwargs.get("treasury_fallback")
                    return {"status": "completed"}

                with patch.object(exporter, "collect_snapshot", fake_collect), patch(
                    "app.integration.github_export.create_global_market_providers",
                    return_value=_sentinel_providers(),
                ) as factory:
                    exit_code = exporter.main(
                        ["collect", "--report-kind", report_kind, "--output", out_path]
                    )

                self.assertEqual(exit_code, 0)
                self.assertEqual(captured["kind"], report_kind)
                if expected:
                    self.assertEqual(factory.call_count, 1)
                    self.assertIs(captured["global_provider"], GLOBAL)
                    self.assertIs(captured["treasury_fallback"], TREASURY)
                else:
                    factory.assert_not_called()
                    self.assertIsNone(captured["global_provider"])
                    self.assertIsNone(captured["treasury_fallback"])


class SchedulerWiringTests(unittest.TestCase):
    def test_overnight_jobs_receive_the_providers(self):
        target = _Scheduler()
        with patch.object(scheduler_module, "FeishuNotifier", return_value=Mock()):
            scheduler_module.register_default_jobs(
                target, global_provider=GLOBAL, treasury_fallback=TREASURY,
            )

        by_kind = {job[2]["kwargs"]["report_kind"]: job[2]["kwargs"] for job in target.jobs}
        for kind in ("global", "morning"):
            with self.subTest(kind=kind):
                self.assertIs(by_kind[kind]["global_provider"], GLOBAL)
                self.assertIs(by_kind[kind]["treasury_fallback"], TREASURY)

    def test_intraday_and_closing_jobs_stay_free_of_provider_keywords(self):
        target = _Scheduler()
        with patch.object(scheduler_module, "FeishuNotifier", return_value=Mock()):
            scheduler_module.register_default_jobs(
                target, global_provider=GLOBAL, treasury_fallback=TREASURY,
            )

        by_kind = {job[2]["kwargs"]["report_kind"]: job[2]["kwargs"] for job in target.jobs}
        for kind in ("midday", "trading", "closing"):
            with self.subTest(kind=kind):
                self.assertNotIn("global_provider", by_kind[kind])
                self.assertNotIn("treasury_fallback", by_kind[kind])

    def test_scheduled_workflow_forwards_providers_only_when_supplied(self):
        for kind, supplied in (("morning", True), ("closing", False)):
            with self.subTest(kind=kind):
                kwargs = {"global_provider": GLOBAL, "treasury_fallback": TREASURY} \
                    if supplied else {}
                with patch.object(
                    scheduler_module, "run_portfolio_report",
                    return_value={"status": "completed", "report": "safe",
                                  "notified": False, "report_kind": kind},
                ) as report:
                    scheduler_module._run_scheduled_portfolio_report(
                        None, "title", kind, **kwargs
                    )

                passed = report.call_args.kwargs
                if supplied:
                    self.assertIs(passed["global_provider"], GLOBAL)
                    self.assertIs(passed["treasury_fallback"], TREASURY)
                else:
                    self.assertNotIn("global_provider", passed)
                    self.assertNotIn("treasury_fallback", passed)

    def test_start_scheduler_builds_providers_once_for_report_profiles(self):
        for profile in ("legacy", "four-stage"):
            with self.subTest(profile=profile), TemporaryDirectory() as raw:
                target = _Scheduler()
                with patch.object(scheduler_module, "scheduler", target), patch.object(
                    scheduler_module, "FeishuNotifier", return_value=Mock()
                ), patch(
                    "app.workflow.scheduler.create_global_market_providers",
                    return_value=_sentinel_providers(),
                ) as factory:
                    scheduler_module.start_scheduler(
                        profile=profile, private_state_dir=raw,
                    )

                self.assertEqual(factory.call_count, 1)
                overnight = [
                    job[2]["kwargs"] for job in target.jobs
                    if job[2]["kwargs"]["report_kind"] in ("global", "morning")
                ]
                self.assertTrue(overnight)
                for job_kwargs in overnight:
                    self.assertIs(job_kwargs["global_provider"], GLOBAL)

    def test_checklist_profile_never_builds_market_providers(self):
        target = _Scheduler()
        with patch.object(scheduler_module, "scheduler", target), patch.object(
            scheduler_module, "FeishuNotifier", return_value=Mock()
        ), patch(
            "app.workflow.scheduler.create_global_market_providers",
            return_value=_sentinel_providers(),
        ) as factory:
            scheduler_module.start_scheduler(profile="full-day")

        factory.assert_not_called()
        self.assertTrue(all(job[2]["id"].startswith("reminder-") for job in target.jobs))

    def test_invalid_profile_is_rejected_before_provider_construction(self):
        target = _Scheduler()
        with patch.object(scheduler_module, "scheduler", target), patch(
            "app.workflow.scheduler.create_global_market_providers",
            return_value=_sentinel_providers(),
        ) as factory:
            with self.assertRaises(ValueError):
                scheduler_module.start_scheduler(profile="half-day")

        factory.assert_not_called()
        self.assertEqual(target.jobs, [])
        self.assertFalse(target.started)


class CliWiringTests(unittest.TestCase):
    def test_portfolio_overnight_stage_receives_providers(self):
        for report_kind in ("global", "morning"):
            with self.subTest(report_kind=report_kind), patch.object(
                cli, "load_dotenv"
            ), patch.object(
                cli, "run_portfolio_report", return_value={"status": "completed"}
            ) as report, patch(
                "app.main.create_global_market_providers",
                return_value=_sentinel_providers(),
            ) as factory:
                from contextlib import redirect_stdout
                from io import StringIO

                with redirect_stdout(StringIO()):
                    exit_code = cli.main(
                        ["--portfolio", "--report-kind", report_kind, "--no-notify"]
                    )

                self.assertEqual(exit_code, 0)
                self.assertEqual(factory.call_count, 1)
                self.assertIs(report.call_args.kwargs["global_provider"], GLOBAL)
                self.assertIs(report.call_args.kwargs["treasury_fallback"], TREASURY)

    def test_portfolio_non_overnight_stage_keeps_the_exact_call_shape(self):
        from contextlib import redirect_stdout
        from io import StringIO

        sentinel_fundamental = object()
        with patch.object(cli, "load_dotenv"), patch.object(
            cli, "run_portfolio_report", return_value={"status": "completed"}
        ) as report, patch(
            "app.main.create_global_market_providers",
            return_value=_sentinel_providers(),
        ) as factory, patch(
            "app.main.create_fundamental_provider",
            return_value=sentinel_fundamental,
        ) as fundamental_factory, redirect_stdout(StringIO()):
            exit_code = cli.main(["--portfolio", "--report-kind", "closing", "--no-notify"])

        self.assertEqual(exit_code, 0)
        factory.assert_not_called()
        fundamental_factory.assert_called_once_with()
        self.assertEqual(
            report.call_args.kwargs,
            {
                "report_kind": "closing",
                "notifier": None,
                "fundamental_provider": sentinel_fundamental,
            },
        )
        self.assertNotIn("global_provider", report.call_args.kwargs)
        self.assertNotIn("treasury_fallback", report.call_args.kwargs)

    def test_portfolio_result_remains_json_serializable_with_providers(self):
        from contextlib import redirect_stdout
        from io import StringIO

        output = StringIO()
        with patch.object(cli, "load_dotenv"), patch.object(
            cli, "run_portfolio_report", return_value={"status": "completed"}
        ), patch(
            "app.main.create_global_market_providers",
            return_value=_sentinel_providers(),
        ), redirect_stdout(output):
            exit_code = cli.main(["--portfolio", "--report-kind", "morning", "--no-notify"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue()), {"status": "completed"})


if __name__ == "__main__":
    unittest.main()
