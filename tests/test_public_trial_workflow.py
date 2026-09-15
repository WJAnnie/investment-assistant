"""Execute the real workflow shell offline with local Git and a fake gh."""
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

from app.integration import public_trial
from app.market.models import Quote


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/public-trial.yml'


class PublicTrialWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding='utf-8')
        cls.workflow = yaml.safe_load(cls.text)
        cls.job = cls.workflow['jobs']['public-trial']
        cls.steps = {step.get('id', step['name']): step for step in cls.job['steps']}
        cls.bash = ('D:/Program Files/Git/bin/bash.exe' if os.name == 'nt'
                    else shutil.which('bash'))

    def shell(self, code, directory, **values):
        if not self.bash or not Path(self.bash).exists():
            self.skipTest('Bash is unavailable')
        environment = os.environ.copy()
        environment.update({
            'GITHUB_REPOSITORY': 'WJAnnie/investment-assistant',
            'GITHUB_OUTPUT': (directory / 'step-output.txt').as_posix(),
            'GITHUB_STEP_SUMMARY': (directory / 'summary.txt').as_posix(),
            'RUNNER_TEMP': directory.as_posix(), 'PYTHONUTF8': '1',
        })
        environment.update(values)
        return subprocess.run([self.bash, '-c', code], cwd=ROOT, env=environment,
                              text=True, encoding='utf-8', capture_output=True, timeout=40)

    def test_schedule_is_four_weekday_slots_and_has_no_untrusted_trigger(self):
        triggers = self.workflow.get('on', self.workflow.get(True))
        self.assertEqual(set(triggers), {'schedule', 'workflow_dispatch'})
        self.assertEqual([entry['cron'] for entry in triggers['schedule']],
                         ['0 1 * * 1-5', '30 3 * * 1-5', '30 6 * * 1-5', '10 8 * * 1-5'])
        self.assertEqual(self.job['runs-on'], 'ubuntu-latest')
        self.assertLessEqual(self.job['timeout-minutes'], 10)
        self.assertIn('github.repository_owner', self.job['if'])
        self.assertIn('refs/heads/main', self.job['if'])
        self.assertIn('github.event.repository.private == false', self.job['if'])

    def test_only_explicit_public_files_are_checked_out(self):
        checkout = next(step for step in self.job['steps'] if step.get('uses', '').startswith('actions/checkout@'))
        options = checkout['with']
        self.assertIs(options['persist-credentials'], False)
        self.assertEqual(options['fetch-depth'], 1)
        self.assertIs(options['sparse-checkout-cone-mode'], False)
        self.assertEqual(set(options['sparse-checkout'].splitlines()), {
            '/.github/workflows/public-trial.yml', '/app/integration/__init__.py',
            '/app/integration/public_trial.py', '/app/market/sina.py', '/app/market/models.py',
            '/app/market/global_markets.py', '/app/notify/feishu.py',
            '/tests/test_global_markets.py', '/tests/test_public_trial.py',
            '/tests/test_public_trial_delivery.py', '/tests/test_public_trial_workflow.py',
            '/tests/test_feishu.py',
        })

    def test_cloud_regression_runs_all_four_required_suites(self):
        script = self.steps['tests']['run']
        for filename in ('test_public_trial.py', 'test_public_trial_delivery.py',
                         'test_public_trial_workflow.py', 'test_global_markets.py'):
            self.assertIn(filename, script)
        self.assertIn('compileall -q app tests', script)

    def test_actions_are_pinned_and_no_artifact_or_cache_is_uploaded(self):
        for step in self.job['steps']:
            if 'uses' in step:
                self.assertRegex(step['uses'], r'@([a-f0-9]{40})$')
                self.assertNotRegex(step['uses'], r'upload-artifact|cache@')
                self.assertNotIn('cache', step.get('with', {}))
        self.assertNotIn('requirements.txt', self.text)
        self.assertNotIn('git add -A', self.text)
        self.assertNotIn('git merge', self.text)
        self.assertNotIn('git push --force', self.text)

    def test_secrets_are_only_in_the_notification_step(self):
        for step in self.job['steps']:
            self.assertNotIn('secrets.', step.get('run', ''))
            if 'secrets.' in str(step):
                self.assertEqual(step.get('id'), 'notify')
        for forbidden in ('TUSHARE', 'PORTFOLIO', 'PRIVATE_TOKEN', 'github_export'):
            self.assertNotIn(forbidden, self.text)
        self.assertNotIn('secrets.', str(self.job.get('env', {})))
        self.assertIn('github.run_attempt == 1', self.steps['notify']['if'])
        self.assertNotIn('secrets.', str(self.steps['collect']))

    def test_workflow_inputs_are_not_interpolated_inside_shell(self):
        for step in self.job['steps']:
            self.assertNotIn('$' + '{{', step.get('run', ''))

    def test_actual_scenario_script_maps_slots_and_rejects_injection(self):
        script = self.steps['scenario']['run']
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            for cron, stage in [('0 1 * * 1-5', 'morning'), ('30 3 * * 1-5', 'midday'),
                                ('30 6 * * 1-5', 'decision'), ('10 8 * * 1-5', 'closing')]:
                with self.subTest(cron=cron):
                    result = self.shell(script, directory, GITHUB_EVENT_NAME='schedule',
                                        SCHEDULE_EXPRESSION=cron, MANUAL_STAGE='')
                    self.assertEqual(result.returncode, 0, result.stderr)
                    content = (directory / 'step-output.txt').read_text()
                    self.assertIn(f'stage={stage}', content)
                    self.assertIn('mode=scheduled', content)
            result = self.shell(script, directory, GITHUB_EVENT_NAME='workflow_dispatch',
                                MANUAL_STAGE='all', SCHEDULE_EXPRESSION='')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('mode=manual_replay', (directory / 'step-output.txt').read_text())
            for stage in ['closing; echo UNSAFE_EXECUTION', '$(echo UNSAFE_EXECUTION)', '', 'global']:
                result = self.shell(script, directory, GITHUB_EVENT_NAME='workflow_dispatch',
                                    MANUAL_STAGE=stage, SCHEDULE_EXPRESSION='')
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('UNSAFE_EXECUTION', result.stdout)

    def test_final_gate_cannot_claim_a_failed_or_unconfirmed_push(self):
        script = self.steps['Record actual delivery outcomes']['run']
        healthy = {'TEST_OUTCOME': 'success', 'COLLECT_OUTCOME': 'success',
                   'PUBLISH_OUTCOME': 'success', 'PUSH_CONFIRMED': 'true',
                   'NOTIFY_OUTCOME': 'success', 'NOTIFY_STATUS': 'accepted'}
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            self.assertEqual(self.shell(script, directory, **healthy).returncode, 0)
            for key, value in [('PUBLISH_OUTCOME', 'failure'), ('PUBLISH_OUTCOME', 'skipped'),
                               ('PUSH_CONFIRMED', ''), ('PUSH_CONFIRMED', 'false'),
                               ('TEST_OUTCOME', 'failure'), ('COLLECT_OUTCOME', 'failure'),
                               ('NOTIFY_OUTCOME', 'failure'), ('NOTIFY_STATUS', ''),
                               ('NOTIFY_STATUS', 'not_configured'), ('NOTIFY_STATUS', 'failed')]:
                with self.subTest(key=key, value=value):
                    self.assertNotEqual(self.shell(script, directory, **{**healthy, key: value}).returncode, 0)
            skipped = self.shell(script, directory, **{**healthy, 'NOTIFY_OUTCOME': 'skipped', 'NOTIFY_STATUS': ''})
            self.assertEqual(skipped.returncode, 0)
            self.assertIn('not_sent', (directory / 'summary.txt').read_text())

    def test_real_publish_script_only_pushes_public_files_and_fails_closed(self):
        # No network: gh is a shell function, and every Git remote is rewritten
        # to this test's new, empty local bare repository.
        stub = r'''
        gh() {
          if [[ "$1" == "api" ]]; then echo public;
          elif [[ "$1 $2" == "auth setup-git" ]]; then return 0;
          else return 1; fi
        }
        git() {
          if [[ "$1" == "-C" && "$3 $4" == "remote add" ]]; then
            command git -C "$2" remote add origin "${TRIAL_TEST_ORIGIN}"
          elif [[ "$1" == "-C" && "$3" == "push" && "${TRIAL_TEST_FAIL_PUSH:-0}" == "1" ]]; then
            return 1
          else command git "$@"; fi
        }
        '''
        class Provider:
            def fetch(self, code):
                return Quote(code, 'not-used', 100.0, 1.0, 'not-used')

        now = datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            origin = directory / 'origin.git'
            subprocess.run(['git', 'init', '--bare', str(origin)], check=True, capture_output=True)
            payload = public_trial.build_snapshot('morning', provider=Provider(), requested_at=now, generated_at=now)
            public_trial.write_reports([payload], directory / 'public-trial-output')
            script = stub + '\n' + self.steps['publish']['run']
            first = self.shell(script, directory, TRIAL_TEST_ORIGIN=origin.as_posix())
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            tree = subprocess.run(['git', '--git-dir', str(origin), 'ls-tree', '-r', '--name-only',
                                   'public-test-data'], check=True, text=True, capture_output=True).stdout
            self.assertEqual(set(tree.splitlines()), {'README.md', 'latest/morning.json'})
            output = directory / 'step-output.txt'
            self.assertIn('confirmed=true', output.read_text())
            second = self.shell(script, directory, TRIAL_TEST_ORIGIN=origin.as_posix())
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            before = output.read_bytes()
            failed = self.shell(script, directory, TRIAL_TEST_ORIGIN=origin.as_posix(), TRIAL_TEST_FAIL_PUSH='1')
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(output.read_bytes(), before)

    def test_publish_migrates_a_valid_previous_readme_before_final_validation(self):
        script = self.steps['publish']['run']
        checkout = script.index('checkout -B public-test-data FETCH_HEAD')
        copy_snapshots = script.index('mkdir -p "${publish_dir}/latest"')
        migrate = script.index(
            'python -m app.integration.public_trial summary --input "${publish_dir}"',
            checkout,
        )

        self.assertLess(migrate, copy_snapshots)


if __name__ == '__main__':
    unittest.main()
