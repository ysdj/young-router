"""Small, dependency-free security helpers for the Core boundary.

These helpers are intentionally conservative.  A snapshot is a public view
of private Core state, and diagnostics must never become an accidental secret
exfiltration channel.  The real domain adapters may keep raw values in
memory, but they use :func:`redact` before returning anything to IPC or logs.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


REDACTED = "configured"
"""Presence marker used for configured secret values."""

SECRET_KEY_MARKERS = (
    "api_key",
    "apikey",
    "auth_token",
    "authorization",
    "credential",
    "password",
    "passwd",
    "private_key",
    "secret",
    "token",
)
PATH_KEY_MARKERS = ("path", "directory", "dirname", "filename", "file", "cwd", "root")
SENSITIVE_QUERY_MARKERS = ("key", "token", "secret", "password", "passwd", "credential", "auth")


def _key_text(key: object) -> str:
    return str(key).strip().lower().replace("-", "_")


def is_secret_key(key: object) -> bool:
    text = _key_text(key)
    # `key_name` / `key_id` are labels, not the credential itself.
    # Presence metadata is deliberately safe to expose as a boolean.  Do not
    # turn fields such as ``token_configured`` into the string marker
    # ``configured``; the shared snapshot contract uses those fields to show
    # whether a credential exists without carrying its value.
    if text in {"key_name", "key_names", "api_key_name", "api_key_names", "key_id", "key_ids", "credential_store"} or text.endswith(("_configured", "_present", "_exists")):
        return False
    return any(marker == text or marker in text for marker in SECRET_KEY_MARKERS)


def is_path_key(key: object) -> bool:
    text = _key_text(key)
    return any(marker == text or text.endswith(f"_{marker}") for marker in PATH_KEY_MARKERS)


def redact(value: object, *, known_secrets: Sequence[str] = (), _key: object = "") -> object:
    """Return a JSON-safe projection with secrets and local paths removed.

    ``known_secrets`` is useful for diagnostics where a secret's key name is
    not known (for example a provider-specific field).  Values are copied, so
    callers can safely mutate the result without changing Core state.
    """

    if is_secret_key(_key):
        if value in (None, "", False):
            return value
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [REDACTED] if value else []
        return REDACTED
    if is_path_key(_key):
        if value in (None, "", False):
            return value
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [REDACTED] if value else []
        return REDACTED

    secret_values = {item for item in known_secrets if isinstance(item, str) and item}
    if isinstance(value, str):
        return REDACT_TEXT(value, secret_values=secret_values)
    if isinstance(value, Mapping):
        return {
            str(key): redact(item, known_secrets=tuple(secret_values), _key=key)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item, known_secrets=tuple(secret_values)) for item in value]
    return copy.deepcopy(value)


def REDACT_TEXT(value: str, *, secret_values: set[str] | None = None) -> str:
    """Redact common credential forms from one piece of diagnostic text."""

    text = str(value)
    for secret in sorted(secret_values or (), key=len, reverse=True):
        if secret:
            text = text.replace(secret, REDACTED)
    # Provider keys frequently use the OpenAI-looking ``sk-`` prefix.  Keep
    # this generic and bounded; never echo the original token in a traceback.
    text = re.sub(r"(?i)\b(?:bearer\s+)?(?:sk|key|token)-[A-Za-z0-9._~-]{8,}\b", REDACTED, text)
    text = re.sub(r"(?i)\b(?:bearer\s+)[A-Za-z0-9._~-]{8,}\b", "Bearer " + REDACTED, text)
    text = _redact_url_text(text)
    text = _redact_key_value_text(text)
    # Absolute paths are private even when no secret is present.  Preserve a
    # useful basename only for paths clearly marked by an error author.
    # A slash immediately following ``:`` or another slash belongs to a URL,
    # not a local absolute path. URLs have already had credentials and
    # sensitive query values removed by ``_redact_url_text`` above.
    text = re.sub(r"(?<![A-Za-z0-9:/])/(?:[^\s/:]+/){1,}[^\s]+", "<private-path>", text)
    text = " ".join(text.split())
    return text[:512]


_TEXT_KEY_VALUE = re.compile(
    r"""(?ix)
    (?P<prefix>
        (?:
            \"(?P<double_quoted_key>[A-Za-z][A-Za-z0-9_-]*)\"
            | '(?P<single_quoted_key>[A-Za-z][A-Za-z0-9_-]*)'
            | (?P<bare_key>[A-Za-z][A-Za-z0-9_-]*)
        )
        \s*(?:=|:)\s*
    )
    (?P<value>
        (?P<bearer>bearer\s+)?
        (?:
            \"(?:\\.|[^\"\\])*\"
            | '(?:\\.|[^'\\])*'
            | [^\s,;}&\]\)]+
        )
    )
    """
)


def _redact_key_value_text(value: str) -> str:
    """Redact sensitive ``key=value`` and ``key: value`` diagnostic forms."""

    def replace(match: re.Match[str]) -> str:
        key = match.group("double_quoted_key") or match.group("single_quoted_key") or match.group("bare_key")
        if not is_secret_key(key):
            return match.group(0)
        bearer = "Bearer " if match.group("bearer") else ""
        return match.group("prefix") + bearer + REDACTED

    return _TEXT_KEY_VALUE.sub(replace, value)


def _redact_url_text(value: str) -> str:
    # URL parsing is best-effort: diagnostic text can contain prose around a
    # URL, so only replace complete http(s) tokens.
    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        try:
            parsed = urlsplit(raw)
        except ValueError:
            return raw
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        query = []
        for key, item in parse_qsl(parsed.query, keep_blank_values=True):
            lowered = key.lower()
            if any(marker in lowered for marker in SENSITIVE_QUERY_MARKERS):
                item = REDACTED
            query.append((key, item))
        return urlunsplit((parsed.scheme, host, parsed.path, urlencode(query), parsed.fragment))

    return re.sub(r"https?://[^\s,;]+", replace, value)


def safe_error_message(message: object, *, known_secrets: Sequence[str] = ()) -> str:
    """Normalize an error to a short, single-line, secret-free message."""

    text = REDACT_TEXT(str(message), secret_values=set(known_secrets))
    return text or "Core operation failed"


def safe_exception_message(error: BaseException, *, known_secrets: Sequence[str] = ()) -> str:
    """Return only a safe message; never include traceback/source context."""

    return safe_error_message(str(error), known_secrets=known_secrets)


__all__ = [
    "PATH_KEY_MARKERS",
    "REDACTED",
    "SECRET_KEY_MARKERS",
    "REDACT_TEXT",
    "is_path_key",
    "is_secret_key",
    "redact",
    "safe_error_message",
    "safe_exception_message",
]
