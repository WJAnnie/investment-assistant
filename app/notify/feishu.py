import json
import os

import requests


def _is_success_code(value):
    return type(value) is int and value == 0


class FeishuNotifier:
    AUTH_ENDPOINT = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    MESSAGE_ENDPOINT = "https://open.feishu.cn/open-apis/im/v1/messages"

    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.webhook = os.getenv("FEISHU_WEBHOOK")
        self.app_id = os.getenv("FEISHU_APP_ID")
        self.app_secret = os.getenv("FEISHU_APP_SECRET")
        self.receive_id = os.getenv("FEISHU_RECEIVE_ID")
        self.receive_id_type = os.getenv("FEISHU_RECEIVE_ID_TYPE", "chat_id")

    def send(self, content: str):
        if self.webhook:
            return self._send_webhook(content)
        if not all((self.app_id, self.app_secret, self.receive_id)):
            return False

        token = self._get_tenant_access_token()
        if not token:
            return False

        payload = {
            "receive_id": self.receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": content}, ensure_ascii=False),
        }
        try:
            response = self.session.post(
                self.MESSAGE_ENDPOINT,
                params={"receive_id_type": self.receive_id_type},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            result = response.json()
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            return False

        return bool(
            response.ok
            and isinstance(result, dict)
            and _is_success_code(result.get("code"))
        )

    def _send_webhook(self, content: str):
        payload = {
            "msg_type": "text",
            "content": {"text": content},
        }
        try:
            response = self.session.post(
                self.webhook,
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            result = response.json()
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            return False
        if not response.ok or not isinstance(result, dict):
            return False
        codes = [result[key] for key in ("code", "StatusCode") if key in result]
        return bool(codes and all(_is_success_code(code) for code in codes))

    def _get_tenant_access_token(self):
        try:
            response = self.session.post(
                self.AUTH_ENDPOINT,
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=10,
            )
            response.raise_for_status()
            result = response.json()
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            return None

        if not isinstance(result, dict) or not _is_success_code(result.get("code")):
            return None
        token = result.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            return None
        return token
