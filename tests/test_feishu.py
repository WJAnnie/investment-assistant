import json
import os
import unittest
from unittest.mock import patch

from app.notify.feishu import FeishuNotifier


class _Response:
    ok = True

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Session:
    def __init__(self):
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if url.endswith("tenant_access_token/internal"):
            return _Response({"code": 0, "tenant_access_token": "tenant-token"})
        return _Response({"code": 0})


class _WebhookSession:
    def __init__(self, payload):
        self.payload = payload
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return _Response(self.payload)


class _BusinessErrorSession(_Session):
    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if url.endswith("tenant_access_token/internal"):
            return _Response({"code": 0, "tenant_access_token": "tenant-token"})
        return _Response({"code": False})


class FeishuNotifierTests(unittest.TestCase):
    def test_app_message_rejects_boolean_false_as_a_success_code(self):
        environment = {
            "FEISHU_APP_ID": "cli_app",
            "FEISHU_APP_SECRET": "app-secret",
            "FEISHU_RECEIVE_ID": "oc_chat",
        }
        with patch.dict(os.environ, environment, clear=True):
            notifier = FeishuNotifier(session=_BusinessErrorSession())

        self.assertFalse(notifier.send("提醒"))

    def test_webhook_requires_feishu_business_success_code(self):
        for payload, expected in (
            ({"code": 0}, True),
            ({"StatusCode": 0}, True),
            ({"code": 19002, "StatusCode": 0}, False),
            ({"code": 0, "StatusCode": 19002}, False),
            ({"code": 19002, "msg": "bad request"}, False),
            ({"code": False}, False),
            ({}, False),
        ):
            with self.subTest(payload=payload):
                session = _WebhookSession(payload)
                with patch.dict(
                    os.environ,
                    {"FEISHU_WEBHOOK": "https://example.invalid/webhook"},
                    clear=True,
                ):
                    notifier = FeishuNotifier(session=session)

                self.assertEqual(notifier.send("提醒"), expected)
                self.assertEqual(len(session.posts), 1)

    def test_sends_directly_to_chat_id_with_app_credentials(self):
        session = _Session()
        environment = {
            "FEISHU_APP_ID": "cli_app",
            "FEISHU_APP_SECRET": "app-secret",
            "FEISHU_RECEIVE_ID": "oc_chat",
            "FEISHU_RECEIVE_ID_TYPE": "chat_id",
        }

        with patch.dict(os.environ, environment, clear=True):
            notifier = FeishuNotifier(session=session)
            self.assertTrue(notifier.send("行情简报"))

        auth_url, auth_kwargs = session.posts[0]
        self.assertTrue(auth_url.endswith("tenant_access_token/internal"))
        self.assertEqual(
            auth_kwargs["json"],
            {"app_id": "cli_app", "app_secret": "app-secret"},
        )
        message_url, message_kwargs = session.posts[1]
        self.assertTrue(message_url.endswith("/im/v1/messages"))
        self.assertEqual(message_kwargs["params"], {"receive_id_type": "chat_id"})
        self.assertEqual(
            message_kwargs["headers"],
            {
                "Authorization": "Bearer tenant-token",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        self.assertEqual(message_kwargs["json"]["receive_id"], "oc_chat")
        self.assertEqual(message_kwargs["json"]["msg_type"], "text")
        self.assertEqual(
            json.loads(message_kwargs["json"]["content"]),
            {"text": "行情简报"},
        )

    def test_missing_webhook_and_app_credentials_skips_safely(self):
        with patch.dict(os.environ, {}, clear=True):
            notifier = FeishuNotifier(session=_Session())

        self.assertFalse(notifier.send("test"))

    def test_refreshes_tenant_token_for_each_send(self):
        session = _Session()
        environment = {
            "FEISHU_APP_ID": "cli_app",
            "FEISHU_APP_SECRET": "app-secret",
            "FEISHU_RECEIVE_ID": "oc_chat",
        }

        with patch.dict(os.environ, environment, clear=True):
            notifier = FeishuNotifier(session=session)
            self.assertTrue(notifier.send("first"))
            self.assertTrue(notifier.send("second"))

        auth_calls = [url for url, _ in session.posts if url.endswith("tenant_access_token/internal")]
        self.assertEqual(len(auth_calls), 2)


if __name__ == "__main__":
    unittest.main()
