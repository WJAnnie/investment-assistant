from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-chatgpt-data.yml"

EXPECTED_SCHEDULES = {
    "31 22 * * 0-4": "global",
    "1 1 * * 1-5": "morning",
    "31 3 * * 1-5": "midday",
    "31 6 * * 1-5": "trading",
    "11 8 * * 1-5": "closing",
}


class GitHubWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.content = WORKFLOW.read_text(encoding="utf-8")

    def test_workflow_is_valid_yaml(self):
        loaded = yaml.safe_load(self.content)
        self.assertIsInstance(loaded, dict)
        self.assertEqual(loaded["name"], "Publish ChatGPT market data")

    def test_each_schedule_maps_to_its_distinct_scenario(self):
        for schedule, report_kind in EXPECTED_SCHEDULES.items():
            self.assertIn(f'cron: "{schedule}"', self.content)
            self.assertIn(
                f'"{schedule}") report_kind={report_kind} ;;',
                self.content,
            )
        self.assertNotIn('cron: "30 7 * * 1-5"', self.content)
        self.assertNotIn("report_kind=hk_close", self.content)

    def test_shared_data_branch_writes_are_serialized(self):
        self.assertIn("group: chatgpt-market-data\n", self.content)
        self.assertNotIn("github.event.schedule || inputs.report_kind", self.content)

    def test_workflow_does_not_claim_pr_event_delivery(self):
        self.assertNotIn("pull-requests: write", self.content)
        self.assertNotIn("gh pr create", self.content)
        self.assertNotIn("Keep the ChatGPT trigger pull request open", self.content)

    def test_private_runner_does_not_cache_data_or_persist_checkout_credentials(self):
        loaded = yaml.safe_load(self.content)
        steps = loaded["jobs"]["publish"]["steps"]
        for step in steps:
            if "uses" in step:
                self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")
                self.assertNotIn("cache", step.get("with", {}))
        checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
        self.assertIs(checkout["with"]["persist-credentials"], False)
        self.assertIn("--no-cache-dir -r requirements.txt", self.content)
        self.assertIn("credential.helper=!gh auth git-credential", self.content)


if __name__ == "__main__":
    unittest.main()
