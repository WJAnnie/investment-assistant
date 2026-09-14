"""Account exports are private even when credentials have been redacted."""

from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import yaml

from app.integration import github_export as exporter


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 14, 16, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
CLASSIFICATION = "private_account_data"
LOCAL_ENV = {
    "GITHUB_REPOSITORY": "fixture-owner/private-data",
    "GITHUB_WORKFLOW": "Private fixture export",
    "GITHUB_RUN_ID": "fixture-run",
    "GITHUB_SHA": "fixture-sha",
}
PRIVATE_CI_ENV = {
    **LOCAL_ENV,
    "GITHUB_ACTIONS": "true",
    "GITHUB_REPOSITORY_VISIBILITY": "private",
}


def fixture_result(status="completed"):
    return {
        "status": status,
        "report_kind": "closing",
        "snapshot": {"account_id": "fixture-account", "total_assets": "123.45"},
        "valuations": {},
        "analysis": {
            "coverage": {"ready": 1, "total": 1, "unavailable": 0},
            "items": [{"status": "ready", "bar_status": "complete"}],
        },
        "report": "Synthetic private account report; WAIT; no automatic orders.",
        "errors": [],
        "notified": False,
    }


class PrivateExportTests(unittest.TestCase):
    def test_complete_partial_and_failed_snapshots_are_always_private(self):
        for status in ("completed", "partial", "failed"):
            with self.subTest(status=status):
                result = fixture_result(status)
                original = deepcopy(result)
                snapshot = exporter.build_snapshot(
                    "closing", result, now=NOW, environ=LOCAL_ENV
                )
                self.assertEqual(snapshot.get("data_classification"), CLASSIFICATION)
                self.assertEqual(result, original)
                self.assertEqual(snapshot["result"]["snapshot"]["account_id"], "fixture-account")

    def test_validator_rejects_unclassified_or_publicly_labelled_account_data(self):
        for classification in (None, "public", "market_data", True, {}, []):
            with self.subTest(classification=classification):
                snapshot = exporter.build_snapshot(
                    "closing", fixture_result(), now=NOW, environ=LOCAL_ENV
                )
                if classification is None:
                    snapshot.pop("data_classification", None)
                else:
                    snapshot["data_classification"] = classification
                with self.assertRaisesRegex(ValueError, "data_classification"):
                    exporter.validate_snapshot(snapshot, environ=LOCAL_ENV)

    def test_public_unknown_or_internal_ci_rejects_before_collection_or_overwrite(self):
        for visibility in (None, "", "public", "internal", "PRIVATE", True):
            with self.subTest(visibility=visibility), TemporaryDirectory() as directory:
                environment = {**PRIVATE_CI_ENV}
                if visibility is None:
                    environment.pop("GITHUB_REPOSITORY_VISIBILITY")
                else:
                    environment["GITHUB_REPOSITORY_VISIBILITY"] = visibility
                output = Path(directory) / "current.json"
                output.write_text("preserve existing private data", encoding="utf-8")
                runner = Mock(return_value=fixture_result())
                with self.assertRaisesRegex(ValueError, "private.*repository"):
                    exporter.collect_snapshot(
                        "closing", output, runner=runner, now=NOW, environ=environment
                    )
                runner.assert_not_called()
                self.assertEqual(output.read_text(encoding="utf-8"), "preserve existing private data")

    def test_malformed_ci_marker_cannot_fall_back_to_local_mode(self):
        for marker in ("false", "", True):
            with self.subTest(marker=marker), TemporaryDirectory() as directory:
                runner = Mock(return_value=fixture_result())
                with self.assertRaisesRegex(ValueError, "private.*repository"):
                    exporter.collect_snapshot(
                        "closing", Path(directory) / "current.json",
                        runner=runner, now=NOW,
                        environ={**PRIVATE_CI_ENV, "GITHUB_ACTIONS": marker},
                    )
                runner.assert_not_called()

    def test_verified_private_ci_can_collect_without_notifying(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "current.json"
            runner = Mock(return_value=fixture_result())
            snapshot = exporter.collect_snapshot(
                "closing", output, runner=runner, now=NOW, environ=PRIVATE_CI_ENV
            )
            runner.assert_called_once_with(report_kind="closing", notifier=None, now=NOW)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), snapshot)
            self.assertEqual(snapshot.get("data_classification"), CLASSIFICATION)

    def test_local_exports_reject_source_paths_and_parent_traversal_before_collection(self):
        for output in (
            ROOT / "data" / "chatgpt" / "privacy-probe.json",
            ROOT / ".private" / ".." / "privacy-probe.json",
        ):
            with self.subTest(output=output), patch.object(exporter, "write_json_atomic") as writer:
                runner = Mock(return_value=fixture_result())
                with self.assertRaisesRegex(ValueError, "private.*output"):
                    exporter.collect_snapshot(
                        "closing", output, runner=runner, now=NOW, environ=LOCAL_ENV
                    )
                runner.assert_not_called()
                writer.assert_not_called()

    def test_local_exports_allow_ignored_private_directory(self):
        output = ROOT / ".private" / "chatgpt" / "current.json"
        with patch.object(exporter, "write_json_atomic") as writer:
            runner = Mock(return_value=fixture_result())
            exporter.collect_snapshot(
                "closing", output, runner=runner, now=NOW, environ=LOCAL_ENV
            )
            writer.assert_called_once()
            runner.assert_called_once()

    def test_local_exports_reject_other_git_worktrees(self):
        with TemporaryDirectory() as directory:
            checkout = Path(directory)
            (checkout / ".git").write_text("gitdir: fixture", encoding="utf-8")
            runner = Mock(return_value=fixture_result())
            with self.assertRaisesRegex(ValueError, "private.*output"):
                exporter.collect_snapshot(
                    "closing", checkout / "data.json", runner=runner,
                    now=NOW, environ=LOCAL_ENV,
                )
            runner.assert_not_called()
            self.assertFalse((checkout / "data.json").exists())

    def test_manifest_rejects_public_ci_before_reading_or_creating_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {**PRIVATE_CI_ENV, "GITHUB_REPOSITORY_VISIBILITY": "public"}, clear=True):
                with self.assertRaisesRegex(ValueError, "private.*repository"):
                    exporter.build_manifest(
                        root / "latest", root / "nonexistent.json",
                        root / "manifest.json", now=NOW,
                    )
            self.assertFalse((root / "manifest.json").exists())

    def test_manifest_and_entries_are_private_including_failed_diagnostics(self):
        for status in ("completed", "failed"):
            with self.subTest(status=status), TemporaryDirectory() as directory:
                root = Path(directory)
                latest = root / "latest"
                latest.mkdir()
                snapshot = exporter.build_snapshot(
                    "closing", fixture_result(status), now=NOW, environ=LOCAL_ENV
                )
                payload = json.dumps(snapshot)
                (root / "current.json").write_text(payload, encoding="utf-8")
                (latest / "closing.json").write_text(payload, encoding="utf-8")
                manifest = exporter.build_manifest(
                    latest, root / "current.json", root / "manifest.json", now=NOW
                )
                for entry in (manifest, manifest["current"], manifest["latest"]["closing"]):
                    self.assertEqual(entry.get("data_classification"), CLASSIFICATION)

    def test_gate_rejects_unclassified_manifest_even_if_publishable(self):
        for classification in (None, "public"):
            with self.subTest(classification=classification), TemporaryDirectory() as directory:
                path = Path(directory) / "manifest.json"
                path.write_text(json.dumps({
                    "publishable": True, "data_classification": classification,
                }), encoding="utf-8")
                self.assertEqual(exporter.main(["gate", "--manifest", str(path)]), 4)


class PrivateWorkflowTests(unittest.TestCase):
    def setUp(self):
        path = ROOT / ".github" / "workflows" / "publish-chatgpt-data.yml"
        self.job = yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]["publish"]
        self.steps = {step["name"]: step for step in self.job["steps"]}

    def run_step(self, name, environment, *, prefix=""):
        bash = shutil.which("bash")
        if os.name == "nt":
            git = shutil.which("git")
            candidate = Path(git).resolve().parents[1] / "bin" / "bash.exe" if git else None
            # Avoid a WSL launcher whose environment/path rules differ from CI.
            bash = str(candidate) if candidate and candidate.is_file() else None
        if not bash:
            self.skipTest("Bash is required for offline workflow execution")
        clean_env = {
            key: os.environ[key]
            for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
            if key in os.environ
        }
        with TemporaryDirectory() as directory:
            output = Path(directory) / "github-output"
            completed = subprocess.run(
                [bash, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c",
                 prefix + self.steps[name]["run"]],
                cwd=directory,
                env={**clean_env, "GITHUB_OUTPUT": output.as_posix(), **environment},
                capture_output=True, text=True, timeout=10,
            )
            evidence = output.read_text(encoding="utf-8") if output.exists() else ""
        return completed, evidence

    def test_live_visibility_check_runs_before_checkout_or_account_access(self):
        first = self.job["steps"][0]
        self.assertEqual(first.get("id"), "privacy")
        self.assertIn('gh api', first["run"])
        self.assertIn('.visibility', first["run"])
        self.assertIn('!= "private"', first["run"])
        self.assertIn('exit 1', first["run"])
        self.assertNotIn("continue-on-error", first)

    def test_visibility_evidence_is_passed_to_both_export_commands(self):
        for name in ("Collect and validate data", "Publish current and latest snapshots"):
            with self.subTest(step=name):
                self.assertEqual(
                    self.steps[name].get("env", {}).get("GITHUB_REPOSITORY_VISIBILITY"),
                    "${{ steps.privacy.outputs.visibility }}",
                )

    def test_market_secret_is_scoped_to_collection_after_privacy_gate(self):
        self.assertNotIn("TUSHARE_TOKEN", self.job.get("env", {}))
        self.assertEqual(
            self.steps["Collect and validate data"]["env"].get("TUSHARE_TOKEN"),
            "${{ secrets.TUSHARE_TOKEN }}",
        )

    def test_visibility_is_checked_again_before_staging_or_pushing(self):
        script = self.steps["Commit data snapshot"]["run"]
        self.assertIn('gh api', script)
        self.assertLess(script.index('gh api'), script.index('git add'))
        self.assertIn('!= "private"', script)
        self.assertIn('exit 1', script)

    def test_local_private_directory_is_ignored(self):
        lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("/.private/", lines)

    def test_private_checks_fail_closed_without_invoking_git(self):
        stubs = (
            'gh() { echo "${TEST_VISIBILITY}"; return "${TEST_GH_EXIT}"; }\n'
            'git() { echo GIT_WAS_CALLED; }\n'
        )
        for step in ("Verify private publication target", "Commit data snapshot"):
            for visibility, api_exit in (("public", "0"), ("internal", "0"), ("", "0"), ("private", "1")):
                with self.subTest(step=step, visibility=visibility, api_exit=api_exit):
                    result, evidence = self.run_step(step, {
                        "GITHUB_REPOSITORY": "fixture-owner/private-data",
                        "TEST_VISIBILITY": visibility, "TEST_GH_EXIT": api_exit,
                    }, prefix=stubs)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotIn("GIT_WAS_CALLED", result.stdout)
                    self.assertEqual(evidence, "")

    def test_private_preflight_emits_verified_visibility(self):
        result, evidence = self.run_step("Verify private publication target", {
            "GITHUB_REPOSITORY": "fixture-owner/private-data",
        }, prefix="gh() { echo private; }\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(evidence, "visibility=private\n")

    def test_final_gate_receives_step_outcomes(self):
        environment = self.steps["Gate publishability"]["env"]
        for name, step in (("PRIVACY_OUTCOME", "privacy"), ("PUBLISH_OUTCOME", "publish"), ("COMMIT_OUTCOME", "commit")):
            with self.subTest(name=name):
                self.assertEqual(environment.get(name), "${{ steps." + step + ".outcome }}")

    def test_final_gate_never_claims_delivery_without_successful_steps(self):
        complete = {
            "COLLECT_EXIT": "0", "GATE_EXIT": "0",
            "PRIVACY_OUTCOME": "success", "PUBLISH_OUTCOME": "success",
            "COMMIT_OUTCOME": "success",
        }
        failures = [
            ("COLLECT_EXIT", ""), ("COLLECT_EXIT", "1"), ("COLLECT_EXIT", "2"),
            ("GATE_EXIT", ""), ("GATE_EXIT", "4"),
            ("PRIVACY_OUTCOME", "failure"), ("PUBLISH_OUTCOME", "failure"),
            ("COMMIT_OUTCOME", "failure"), ("COMMIT_OUTCOME", "skipped"),
            ("COMMIT_OUTCOME", "cancelled"), ("COMMIT_OUTCOME", ""),
        ]
        for name, value in failures:
            with self.subTest(name=name, value=value):
                result, _ = self.run_step("Gate publishability", {**complete, name: value})
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("Published an analyzable", result.stdout + result.stderr)
        result, _ = self.run_step("Gate publishability", complete)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Published an analyzable", result.stdout)

    def test_failed_push_does_not_claim_diagnostic_snapshot_was_published(self):
        result, _ = self.run_step("Gate publishability", {
            "COLLECT_EXIT": "2", "GATE_EXIT": "4",
            "PRIVACY_OUTCOME": "success", "PUBLISH_OUTCOME": "success",
            "COMMIT_OUTCOME": "failure",
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("was published", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

