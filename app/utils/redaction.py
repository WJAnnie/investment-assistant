"""Credential redaction for public JSON, without discarding useful diagnostics."""

import json
import os
import re
from urllib.parse import quote, quote_plus


REDACTED = "[REDACTED]"
_SECRET_NAMES = (
    "api_key", "apikey", "secret", "secret_key", "token",
    "authorization", "password", "passwd", "webhook", "webhook_url",
    "private_key", "access_key", "credential", "credentials",
)
_CREDENTIAL_NAME = (
    r"(?:[a-z][\w-]*[_-])?(?:api[_-]?key|(?:access|refresh|auth)[_-]?token|"
    r"(?:client|app)[_-]?secret|token|secret(?:[_-]?key)?|authorization|"
    r"password|passwd|webhook(?:[_-]?url)?|(?:private|access)[_-]?key|credentials?)"
)
_TOKEN = r"(?:\[REDACTED\]|[^\s,;&}\]\)\"'<>]+)"
_ASSIGNMENT = re.compile(
    rf"(?P<prefix>(?<![\w-])[\"']?{_CREDENTIAL_NAME}[\"']?\s*[:=]\s*)"
    rf"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|"
    rf"(?:Bearer|Basic)\s+{_TOKEN}|{_TOKEN})",
    re.IGNORECASE,
)
_AUTHORIZATION = re.compile(rf"(\b(?:Bearer|Basic)\s+){_TOKEN}", re.IGNORECASE)
_WEBHOOK = re.compile(
    r"(https?://open\.(?:feishu\.cn|larksuite\.com)/open-apis/bot/v2/hook/)"
    r"(?:\[REDACTED\]|[^\s?&#\"'<>]+)",
    re.IGNORECASE,
)
_URL_USERINFO = re.compile(r"(https?://)[^\s/@]+@", re.IGNORECASE)


def is_sensitive_key(key):
    """Recognize credential fields, including vendor-prefixed environment keys."""
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key).strip())
    normalized = normalized.lower().replace("-", "_")
    return normalized in _SECRET_NAMES or normalized.endswith(
        tuple("_" + name for name in _SECRET_NAMES)
    )


def _redacted_assignment(match):
    value = match.group("value")
    delimiter = value[0] if value[0] in ("'", '"') else ""
    return match.group("prefix") + delimiter + REDACTED + delimiter


def redact_secrets(value, environ=None):
    """Return a sanitized copy of JSON-compatible values. Never mutate input.

    Known credentials are replaced even in unstructured provider messages;
    common inline assignments, auth headers and webhook URLs are masked when
    their secrets are not available locally. This is a publication boundary,
    not a reason to consider failed data ready.
    """
    environment = os.environ if environ is None else environ
    variants = set()
    for key, secret in environment.items():
        if not is_sensitive_key(key) or not isinstance(secret, str) or not secret.strip():
            continue
        variants.update((
            secret, quote(secret, safe=""), quote_plus(secret, safe=""),
            json.dumps(secret, ensure_ascii=False)[1:-1],
            json.dumps(secret, ensure_ascii=True)[1:-1],
        ))
    variants.update(
        re.sub(r"%[0-9A-F]{2}", lambda match: match[0].lower(), item)
        for item in tuple(variants)
    )
    variants.discard(REDACTED)
    known_secrets = re.compile(
        "|".join(re.escape(item) for item in [REDACTED, *sorted(variants, key=len, reverse=True)])
    ) if variants else None

    def redact(item):
        if isinstance(item, dict):
            return {redact(key): redact(child) for key, child in item.items()}
        if isinstance(item, list):
            return [redact(child) for child in item]
        if not isinstance(item, str):
            return item
        if known_secrets is not None:
            item = known_secrets.sub(REDACTED, item)
        item = _URL_USERINFO.sub(lambda match: match[1] + REDACTED + "@", item)
        item = _WEBHOOK.sub(lambda match: match[1] + REDACTED, item)
        item = _AUTHORIZATION.sub(lambda match: match[1] + REDACTED, item)
        return _ASSIGNMENT.sub(_redacted_assignment, item)

    return redact(value)

