"""Offline wiring tests: industry ranking provider wiring.

The industry provider must reach the workflow from every real composition root,
across all analysis stages (unlike overnight-only global market providers).
These tests use sentinel objects and verify construction without live network I/O.
"""

from contextlib import redirect_stdout
from io import StringIO
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from app import main as cli
from app.market.board_http import EastMoneyBoardClient
from app.market.factory import create_industry_provider
from app.market.industries import AkShareIndustryProvider
from app.workflow import scheduler as scheduler_module


INDUSTRY_SENTINEL = object()
FUNDAMENTAL_SENTINEL = object()
GLOBAL_SENTINEL = object()
TREASURY_SENTINEL = object()


class _Scheduler:
    def __init__(self):
        self.jobs = []
        self.started = False

    def add_job(self, func, trigger, **kwargs):
        self.jobs.append((func, trigger, kwargs))

    def start(self):
        self.started = True


class IndustryProviderWiringTests(unittest.TestCase):
    def test_create_industry_provider_returns_instance_without_io(self):
        """1. create_industry_provider() returns an AkShareIndustryProvider with
        EastMoneyBoardClient, client.session.trust_env is False, timeout <= 10,
        and no requests were made.
        """
        with patch.object(EastMoneyBoardClient, "_get", side_effect=AssertionError("No requests allowed during construction")):
            provider = create_industry_provider()

        self.assertIsInstance(provider, AkShareIndustryProvider)
        client = getattr(provider, "client", getattr(provider, "_client", None))
        self.assertIsInstance(client, EastMoneyBoardClient)
        self.assertFalse(client.session.trust_env)
        self.assertLessEqual(client.timeout, 10)

    def test_cli_portfolio_mode_passes_industry_provider_in_all_stages(self):
        """2. app.main --portfolio passes industry_provider in morning and closing stages."""
        for report_kind in ("morning", "closing"):
            with self.subTest(report_kind=report_kind):
                with patch.object(cli, "load_dotenv"), patch.object(
                    cli, "run_portfolio_report", return_value={"status": "completed"}
                ) as report, patch(
                    "app.main.create_industry_provider",
                    return_value=INDUSTRY_SENTINEL,
                ) as industry_factory, patch(
                    "app.main.create_fundamental_provider",
                    return_value=FUNDAMENTAL_SENTINEL,
                ) as fundamental_factory, patch(
                    "app.main.create_global_market_providers",
                    return_value=(GLOBAL_SENTINEL, TREASURY_SENTINEL),
                ) as global_factory, redirect_stdout(StringIO()):
                    exit_code = cli.main(
                        ["--portfolio", "--report-kind", report_kind, "--no-notify"]
                    )

                self.assertEqual(exit_code, 0)
                industry_factory.assert_called_once_with()
                fundamental_factory.assert_called_once_with()
                self.assertIs(
                    report.call_args.kwargs.get("industry_provider"),
                    INDUSTRY_SENTINEL,
                )
                if report_kind == "morning":
                    global_factory.assert_called_once_with()
                    self.assertIs(report.call_args.kwargs.get("global_provider"), GLOBAL_SENTINEL)
                    self.assertIs(report.call_args.kwargs.get("treasury_fallback"), TREASURY_SENTINEL)
                else:
                    global_factory.assert_not_called()
                    self.assertNotIn("global_provider", report.call_args.kwargs)
                    self.assertNotIn("treasury_fallback", report.call_args.kwargs)

    def test_start_scheduler_builds_industry_provider_once_for_report_profiles(self):
        """3. start_scheduler builds industry_provider once for legacy and four-stage profiles,
        and every report job's kwargs receives industry_provider.
        """
        for profile, expected_count in (("legacy", 5), ("four-stage", 4)):
            with self.subTest(profile=profile), TemporaryDirectory() as raw:
                target = _Scheduler()
                with patch.object(scheduler_module, "scheduler", target), patch.object(
                    scheduler_module, "FeishuNotifier", return_value=Mock()
                ), patch(
                    "app.workflow.scheduler.create_industry_provider",
                    return_value=INDUSTRY_SENTINEL,
                ) as industry_factory, patch(
                    "app.workflow.scheduler.create_fundamental_provider",
                    return_value=FUNDAMENTAL_SENTINEL,
                ), patch(
                    "app.workflow.scheduler.create_global_market_providers",
                    return_value=(GLOBAL_SENTINEL, TREASURY_SENTINEL),
                ):
                    scheduler_module.start_scheduler(
                        profile=profile, private_state_dir=raw,
                    )

                self.assertTrue(target.started)
                self.assertEqual(len(target.jobs), expected_count)
                industry_factory.assert_called_once_with()
                for job in target.jobs:
                    job_kwargs = job[2]["kwargs"]
                    self.assertIn(
                        "industry_provider",
                        job_kwargs,
                        f"Job {job[2]['id']} missing industry_provider",
                    )
                    self.assertIs(
                        job_kwargs["industry_provider"],
                        INDUSTRY_SENTINEL,
                    )

    def test_start_scheduler_checklist_profile_never_builds_industry_provider(self):
        """4. start_scheduler(profile="full-day") completely skips industry provider construction."""
        target = _Scheduler()
        with patch.object(scheduler_module, "scheduler", target), patch.object(
            scheduler_module, "FeishuNotifier", return_value=Mock()
        ), patch(
            "app.workflow.scheduler.create_industry_provider",
            return_value=INDUSTRY_SENTINEL,
        ) as industry_factory, patch(
            "app.workflow.scheduler.create_fundamental_provider",
            return_value=FUNDAMENTAL_SENTINEL,
        ) as fundamental_factory, patch(
            "app.workflow.scheduler.create_global_market_providers",
            return_value=(GLOBAL_SENTINEL, TREASURY_SENTINEL),
        ) as global_factory:
            scheduler_module.start_scheduler(profile="full-day")

        industry_factory.assert_not_called()
        fundamental_factory.assert_not_called()
        global_factory.assert_not_called()
        self.assertTrue(target.started)
        self.assertTrue(all(job[2]["id"].startswith("reminder-") for job in target.jobs))

    def test_collect_scheduled_portfolio_report_forwards_industry_across_all_stages(self):
        """5. _collect_scheduled_portfolio_report forwards industry_provider across
        all stages (morning, midday, trading, closing), while global providers are only
        forwarded for morning.
        """
        stages = ("morning", "midday", "trading", "closing")
        for stage in stages:
            with self.subTest(stage=stage):
                with patch.object(
                    scheduler_module,
                    "run_portfolio_report",
                    return_value={"status": "completed", "report": "ok", "notified": False},
                ) as report:
                    scheduler_module._collect_scheduled_portfolio_report(
                        None,
                        stage,
                        None,
                        None,
                        global_provider=GLOBAL_SENTINEL,
                        treasury_fallback=TREASURY_SENTINEL,
                        fundamental_provider=FUNDAMENTAL_SENTINEL,
                        industry_provider=INDUSTRY_SENTINEL,
                    )

                passed = report.call_args.kwargs
                self.assertIs(
                    passed.get("industry_provider"),
                    INDUSTRY_SENTINEL,
                    f"Stage {stage} should forward industry_provider",
                )
                if stage == "morning":
                    self.assertIs(passed.get("global_provider"), GLOBAL_SENTINEL)
                    self.assertIs(passed.get("treasury_fallback"), TREASURY_SENTINEL)
                else:
                    self.assertNotIn(
                        "global_provider",
                        passed,
                        f"Stage {stage} must not receive global_provider",
                    )
                    self.assertNotIn(
                        "treasury_fallback",
                        passed,
                        f"Stage {stage} must not receive treasury_fallback",
                    )

    def test_invalid_profile_is_rejected_before_industry_provider_construction(self):
        """6. start_scheduler rejects invalid profile before calling create_industry_provider."""
        target = _Scheduler()
        with patch.object(scheduler_module, "scheduler", target), patch(
            "app.workflow.scheduler.create_industry_provider",
            return_value=INDUSTRY_SENTINEL,
        ) as industry_factory:
            with self.assertRaises(ValueError):
                scheduler_module.start_scheduler(profile="half-day")

        industry_factory.assert_not_called()
        self.assertEqual(target.jobs, [])


if __name__ == "__main__":
    unittest.main()
