"""CLI boundary tests: synthetic paths only; no accounts, network or delivery."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import main as cli
from tests.test_live_observation import make_ledger


class PrivateStageCliTests(unittest.TestCase):
    def test_portfolio_passes_explicit_private_directory(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(cli, "load_dotenv"), patch.object(
            cli, "run_portfolio_report", return_value={"status": "completed"}
        ) as report, redirect_stdout(StringIO()):
            self.assertEqual(cli.main(["--portfolio", "--report-kind", "morning",
                                       "--no-notify", "--private-state-dir", raw]), 0)
            report.assert_called_once_with(report_kind="morning", notifier=None,
                                           private_state_dir=Path(raw).resolve())
            self.assertEqual(list(Path(raw).iterdir()), [])

    def test_schedule_forwards_four_stage_profile_and_private_directory(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(cli, "load_dotenv"), patch.object(
            cli, "start_scheduler"
        ) as start:
            self.assertEqual(cli.main(["--schedule", "--schedule-profile", "four-stage",
                                       "--private-state-dir", raw]), 0)
            start.assert_called_once_with(profile="four-stage", private_state_dir=Path(raw).resolve())

    def test_private_directory_does_not_change_legacy_default(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(cli, "load_dotenv"), patch.object(
            cli, "start_scheduler"
        ) as start:
            cli.main(["--schedule", "--private-state-dir", raw])
            start.assert_called_once_with(private_state_dir=Path(raw).resolve())

    def test_invalid_combinations_and_public_execution_have_no_side_effects(self):
        with tempfile.TemporaryDirectory() as raw:
            cases = [
                (["--snapshot"], {}),
                (["--code", "000001"], {}),
                (["--reminder-plan"], {}),
                (["--schedule", "--schedule-profile", "full-day"], {}),
                (["--portfolio"], {"GITHUB_ACTIONS": "true"}),
                (["--portfolio"], {"GITHUB_ACTIONS": "false", "GITHUB_REPOSITORY_VISIBILITY": "private"}),
            ]
            for options, environment in cases:
                with self.subTest(options=options, environment=environment), patch.dict(
                    os.environ, environment, clear=True
                ), patch.object(cli, "load_dotenv") as dotenv, patch.object(
                    cli, "FeishuNotifier"
                ) as notifier, patch.object(cli, "run_portfolio_report") as report, patch.object(
                    cli, "start_scheduler"
                ) as scheduler, redirect_stderr(StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        cli.main([*options, "--private-state-dir", raw])
                    self.assertEqual(raised.exception.code, 2)
                    for forbidden in (dotenv, notifier, report, scheduler):
                        forbidden.assert_not_called()

    def test_source_state_path_is_rejected_before_dotenv(self):
        with patch.object(cli, "load_dotenv") as dotenv, patch.object(
            cli, "FeishuNotifier"
        ) as notifier, redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as raised:
                cli.main(["--portfolio", "--private-state-dir", str(Path.cwd() / "data")])
            self.assertEqual(raised.exception.code, 2)
            dotenv.assert_not_called()
            notifier.assert_not_called()

    def test_existing_live_ledger_is_forwarded_only_with_private_four_stage_schedule(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(cli, "load_dotenv"), patch.object(
            cli, "start_scheduler"
        ) as start:
            ledger = make_ledger(raw)
            stages = Path(raw) / "stages"
            self.assertEqual(cli.main(["--schedule", "--schedule-profile", "four-stage",
                "--private-state-dir", str(stages), "--live-ledger", str(ledger.path)]), 0)
            start.assert_called_once_with(profile="four-stage", private_state_dir=stages.resolve(),
                                           live_ledger=ledger.path)

    def test_live_ledger_conflicts_and_missing_file_fail_before_private_io_setup(self):
        options = (["--portfolio"], ["--snapshot"], ["--reminder-plan"], ["--schedule"],
                   ["--schedule", "--schedule-profile", "full-day"],
                   ["--schedule", "--schedule-profile", "four-stage"])
        with tempfile.TemporaryDirectory() as raw:
            for base in options:
                with self.subTest(options=base), patch.object(cli, "load_dotenv") as dotenv, patch.object(
                    cli, "FeishuNotifier"
                ) as notifier, patch.object(cli, "start_scheduler") as scheduler, redirect_stderr(StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        cli.main([*base, "--live-ledger", str(Path(raw) / "missing.jsonl"),
                                  "--private-state-dir", str(Path(raw) / "stages")])
                    self.assertEqual(raised.exception.code, 2)
                    for forbidden in (dotenv, notifier, scheduler):
                        forbidden.assert_not_called()

    def test_live_ledger_requires_explicit_private_stage_directory(self):
        with patch.object(cli, "load_dotenv") as dotenv, redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(["--schedule", "--schedule-profile", "four-stage",
                          "--live-ledger", ".private/trial.jsonl"])
            dotenv.assert_not_called()


if __name__ == "__main__":
    unittest.main()
