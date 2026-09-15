"""Offline delivery checks; never read the user's account or credentials."""
import contextlib
import copy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.integration import public_trial
from app.market.global_markets import INSTRUMENTS, SourceObservation
from app.market.models import Quote


class PublicTrialDeliveryTests(unittest.TestCase):
    def snapshot(self, stage='morning'):
        class Provider:
            def fetch(self, code):
                return Quote(code, 'untrusted', 100.0, 1.0, 'untrusted')

        now = datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc)
        return public_trial.build_snapshot(
            stage, provider=Provider(), requested_at=now, generated_at=now,
        )

    def test_notify_missing_credentials_is_not_a_success(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(
                public_trial.send_test_notification([self.snapshot()]),
                'not_configured',
            )

    def test_notify_validates_before_calling_notifier(self):
        payload = self.snapshot()
        payload['account'] = {'secret': 'PRIVATE_SENTINEL'}
        with patch('app.notify.feishu.FeishuNotifier') as notifier:
            with self.assertRaises(ValueError):
                public_trial.send_test_notification([payload], notifier=notifier)
            notifier.send.assert_not_called()

    def test_only_boolean_true_proves_notification_acceptance(self):
        class Notifier:
            result = True

            def send(self, content):
                self.content = content
                return self.result

        notifier = Notifier()
        for result, status in [(True, 'accepted'), (False, 'failed'), (1, 'failed'),
                               ('success', 'failed'), (None, 'failed')]:
            with self.subTest(result=result):
                notifier.result = result
                self.assertEqual(public_trial.send_test_notification(
                    [self.snapshot()], notifier=notifier), status)
        self.assertIn('非交易信号', notifier.content)
        self.assertIn('合成', notifier.content)

    def test_notification_exception_is_redacted(self):
        class Notifier:
            def send(self, _content):
                raise RuntimeError('SECRET_ENDPOINT_TOKEN')

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            result = public_trial.send_test_notification([self.snapshot()], notifier=Notifier())
        self.assertEqual(result, 'failed')
        self.assertNotIn('SECRET_ENDPOINT_TOKEN', captured.getvalue())

    def test_cli_all_writes_only_four_public_snapshots_and_readme(self):
        class Provider:
            def __init__(self, **_kwargs):
                pass

            def fetch(self, code):
                return Quote(code, 'SECRET_NAME', 100.0, 1.0, 'SECRET_TIMESTAMP')

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'public'
            with patch.object(public_trial, 'SinaProvider', Provider):
                self.assertEqual(public_trial.main([
                    'collect', '--stage', 'all', '--output', str(output),
                ]), 0)
            files = {p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()}
            self.assertEqual(files, {'README.md'} | {
                f'latest/{stage}.json' for stage in public_trial.STAGES
            })
            for path in (output / 'latest').glob('*.json'):
                payload = json.loads(path.read_text(encoding='utf-8'))
                public_trial.validate_snapshot(payload)
                self.assertEqual(payload['execution_mode'], 'manual_replay')
                self.assertNotIn('SECRET_', path.read_text(encoding='utf-8'))
            self.assertEqual(public_trial.main(['validate', '--input', str(output)]), 0)


    def test_cli_collect_wires_three_providers_to_one_isolated_session(self):
        sessions = []
        domestic_instances, yahoo_instances, fred_instances = [], [], []

        class Session:
            def __init__(self):
                sessions.append(self)
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return None

        class Domestic:
            def __init__(self, session):
                self.session = session
                self.calls = []
                domestic_instances.append(self)
            def fetch(self, code):
                self.calls.append(code)
                return Quote(code, 'fixed', 100.0, 1.0, 'fixed')

        class Yahoo:
            def __init__(self, session):
                self.session = session
                self.calls = []
                yahoo_instances.append(self)
            def fetch(self, symbol):
                self.calls.append(symbol)
                return SourceObservation(symbol, 4.61 if symbol == '^TNX' else 102.0,
                    4.60 if symbol == '^TNX' else 100.0,
                    '2026-09-14T00:00:00+00:00', '2026-09-13', 'yahoo_chart')

        class Fred:
            def __init__(self, session):
                self.session = session
                self.calls = []
                fred_instances.append(self)
            def fetch(self, symbol):
                self.calls.append(symbol)
                raise AssertionError('fallback should not run')

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(public_trial, '_IsolatedSession', Session), \
                patch.object(public_trial, 'SinaProvider', Domestic), \
                patch.object(public_trial, 'YahooGlobalMarketProvider', Yahoo), \
                patch.object(public_trial, 'FredTreasuryProvider', Fred), \
                patch.object(public_trial, '_now', return_value=datetime(
                    2026, 9, 14, 1, 0, tzinfo=timezone.utc)):
            self.assertEqual(public_trial.main([
                'collect', '--stage', 'morning', '--output', directory]), 0)
        self.assertEqual(len(sessions), 1)
        self.assertIs(domestic_instances[0].session, sessions[0])
        self.assertIs(yahoo_instances[0].session, sessions[0])
        self.assertIs(fred_instances[0].session, sessions[0])
        self.assertEqual(yahoo_instances[0].calls, list(INSTRUMENTS))
        self.assertEqual(fred_instances[0].calls, [])

    def test_cli_non_morning_makes_zero_global_fetches(self):
        class Domestic:
            def __init__(self, **_kwargs): pass
            def fetch(self, code): return Quote(code, 'fixed', 100.0, 1.0, 'fixed')
        class Global:
            instances = []
            def __init__(self, **_kwargs):
                self.calls = []
                self.instances.append(self)
            def fetch(self, symbol):
                self.calls.append(symbol)
                raise AssertionError('non-morning global request')
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(public_trial, 'SinaProvider', Domestic), \
                patch.object(public_trial, 'YahooGlobalMarketProvider', Global), \
                patch.object(public_trial, 'FredTreasuryProvider', Global):
            self.assertEqual(public_trial.main([
                'collect', '--stage', 'midday', '--output', directory]), 0)
        self.assertEqual([item.calls for item in Global.instances], [[], []])

    def test_notification_bundle_contains_human_global_section_without_internals(self):
        payload = self.snapshot()
        content = public_trial._bundle([payload])
        self.assertIn('🌍 隔夜全球市场', content)
        self.assertEqual(sum(content.count(item.name) for item in INSTRUMENTS.values()),
                         len(INSTRUMENTS))
        for forbidden in ('yahoo_chart', 'fred_dgs10', 'RECENT', 'error_code'):
            self.assertNotIn(forbidden, content)


    def test_invalid_batch_does_not_modify_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'public'
            good = self.snapshot()
            public_trial.write_reports([good], output)
            original = (output / 'README.md').read_bytes()
            bad = copy.deepcopy(good)
            bad['decision']['action'] = 'ADD'
            with self.assertRaises(ValueError):
                public_trial.write_reports([self.snapshot('midday'), bad], output)
            self.assertEqual((output / 'README.md').read_bytes(), original)
            self.assertFalse((output / 'latest' / 'midday.json').exists())

    def test_duplicate_json_keys_and_unknown_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'public'
            public_trial.write_reports([self.snapshot()], output)
            target = output / 'latest' / 'morning.json'
            original = target.read_text(encoding='utf-8')
            target.write_text(original.replace('{', '{"account":0,"account":1,', 1), encoding='utf-8')
            self.assertEqual(public_trial.main(['validate', '--input', str(output)]), 1)
            target.write_text(original, encoding='utf-8')
            (output / 'account.json').write_text('{}', encoding='utf-8')
            self.assertEqual(public_trial.main(['validate', '--input', str(output)]), 1)

    def test_cli_notify_does_not_claim_device_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            public_trial.write_reports([self.snapshot()], directory)
            for status, code in [('accepted', 0), ('failed', 1), ('not_configured', 2)]:
                with self.subTest(status=status):
                    captured = io.StringIO()
                    with patch.object(public_trial, 'send_test_notification', return_value=status):
                        with contextlib.redirect_stdout(captured):
                            result = public_trial.main(['notify', '--input', directory])
                    self.assertEqual(result, code)
                    self.assertIn(f'notification_status={status}', captured.getvalue())
                    self.assertNotIn('delivered', captured.getvalue())

    def test_unsafe_webhook_is_not_sent(self):
        for endpoint in ['http://open.feishu.cn/hook/x', 'https://example.com/token',
                         'https://open.feishu.cn.evil.test/hook/token']:
            with self.subTest(endpoint=endpoint):
                with patch.dict('os.environ', {'FEISHU_WEBHOOK': endpoint}, clear=True):
                    with patch('app.notify.feishu.FeishuNotifier.send') as send:
                        self.assertEqual(public_trial.send_test_notification([self.snapshot()]), 'failed')
                        send.assert_not_called()

    def test_receive_id_type_is_allowlisted_without_silent_fallback(self):
        credentials = {'FEISHU_APP_ID': 'SYNTHETIC_APP', 'FEISHU_APP_SECRET': 'SYNTHETIC_SECRET',
                       'FEISHU_RECEIVE_ID': 'SYNTHETIC_RECEIVER'}
        for receive_type in ('open_id', 'user_id', 'union_id', 'email', 'chat_id',
                             '', 'CHAT_ID', 'chat_id ', 'chat_id&override=true', 'unknown'):
            allowed = receive_type in {'open_id', 'user_id', 'union_id', 'email', 'chat_id'}
            with self.subTest(receive_type=receive_type):
                with patch.dict('os.environ', {**credentials, 'FEISHU_RECEIVE_ID_TYPE': receive_type}, clear=True):
                    with patch('app.notify.feishu.FeishuNotifier.send', return_value=True) as send:
                        result = public_trial.send_test_notification([self.snapshot()])
                        self.assertEqual(result, 'accepted' if allowed else 'failed')
                        self.assertEqual(send.call_count, int(allowed))


if __name__ == '__main__':
    unittest.main()
