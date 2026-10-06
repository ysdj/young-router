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
NON_SECRET_TOKEN_COUNTER_KEYS = frozenset(
    {
        "cached_tokens",
        "completion_tokens",
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "reasoning_tokens",
        "total_tokens",
    }
)
PATH_KEY_MARKERS = ("path", "directory", "dirname", "filename", "file", "cwd", "root")
SENSITIVE_QUERY_MARKERS = ("key", "token", "secret", "password", "passwd", "credential", "auth")
# A path value only needs the absolute-path rule when it really carries one.
_ABSOLUTE_PATH_IN_TEXT = re.compile(r"(?<![A-Za-z0-9:/])/(?:[^\s/:]+/)+[^\s]+")
_URL_IN_TEXT = re.compile(r"https?://[^\s,;]+")
# The credential forms REDACT_TEXT masks whatever key they appear under.
_TEXT_CREDENTIAL_PREFIX = re.compile(r"(?i)\b(?:bearer\s+)?(?:sk|key|token)-[A-Za-z0-9._~-]{8,}\b")
_TEXT_BEARER = re.compile(r"(?i)\b(?:bearer\s+)[A-Za-z0-9._~-]{8,}\b")


def _key_text(key: object) -> str:
    return str(key).strip().lower().replace("-", "_")


# Key classification answers the same question for the same key name on every
# snapshot, and a snapshot asks it once per field.  Remembering the answer for
# the names a projection actually uses keeps its per-key tuple scans off the
# repeated path.
_KEY_CLASSIFICATION_LIMIT = 4096
_key_classification_cache: "dict[str, tuple[bool, bool]]" = {}


def _key_classification(text: str) -> tuple[bool, bool]:
    """Whether one normalized key name names a secret and an absolute path."""

    cached = _key_classification_cache.get(text)
    if cached is not None:
        return cached
    secret = (
        False
        if text in {
            "key_name",
            "key_names",
            "api_key_name",
            "api_key_names",
            "key_id",
            "key_ids",
            "credential_store",
            "remember_password",
            "password_saved",
        }
        or text.endswith(("_configured", "_present", "_exists"))
        else any(marker == text or marker in text for marker in SECRET_KEY_MARKERS)
    )
    path = any(marker == text or text.endswith(f"_{marker}") for marker in PATH_KEY_MARKERS)
    if len(_key_classification_cache) >= _KEY_CLASSIFICATION_LIMIT:
        _key_classification_cache.clear()
    _key_classification_cache[text] = (secret, path)
    return secret, path


def is_secret_key(key: object) -> bool:
    # `key_name` / `key_id` are labels, not the credential itself.
    # Presence metadata is deliberately safe to expose as a boolean.  Do not
    # turn fields such as ``token_configured`` into the string marker
    # ``configured``; the shared snapshot contract uses those fields to show
    # whether a credential exists without carrying its value.
    return _key_classification(_key_text(key))[0]


def is_path_key(key: object) -> bool:
    return _key_classification(_key_text(key))[1]


def redact(value: object, *, known_secrets: Sequence[str] = (), _key: object = "") -> object:
    """Return a JSON-safe projection with secrets and local paths removed.

    ``known_secrets`` is useful for diagnostics where a secret's key name is
    not known (for example a provider-specific field).  Values are copied, so
    callers can safely mutate the result without changing Core state.
    """

    normalized_key = _key_text(_key)
    if normalized_key and normalized_key not in NON_SECRET_TOKEN_COUNTER_KEYS:
        secret_key, path_key = _key_classification(normalized_key)
        if secret_key or path_key:
            if value in (None, "", False):
                return value
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                return [REDACTED] if value else []
            return REDACTED

    secret_values = {item for item in known_secrets if isinstance(item, str) and item}
    if isinstance(value, str):
        # A snapshot runs this over every string it exposes.  REDACT_TEXT
        # normally returns such a value untouched, so the cheap test answers
        # that case directly; anything it cannot prove unchanged takes the
        # original path.
        if _plain_string_is_safe(value, secret_values):
            return value
        return REDACT_TEXT(value, secret_values=set(secret_values))
    if isinstance(value, Mapping):
        return {
            str(key): redact(item, known_secrets=known_secrets, _key=key)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item, known_secrets=known_secrets) for item in value]
    return copy.deepcopy(value)


def _plain_string_is_safe(value: str, secret_values: set[str]) -> bool:
    """The bounded proof that REDACT_TEXT leaves this string as it is.

    Every rule REDACT_TEXT applies fires only on a shape one of its own
    patterns matches, so probing with those same patterns is the same decision
    without the rewrite, the per-rule substitution, and the whitespace join.
    Each pattern also needs a literal its own alternatives contain, so the
    probe runs at all only for a string that carries one — which is what keeps
    a snapshot's thousands of plain identifiers off the matcher.
    """

    if not value:
        return True
    if secret_values and any(secret in value for secret in secret_values):
        return False
    for character in value:
        if character.isspace() or not character.isascii():
            # Whitespace is joined; a non-ASCII value still has to prove it.
            return False
    if "-" in value and _TEXT_CREDENTIAL_PREFIX.search(value):
        return False
    # A bearer credential needs a space to separate its two words, and the
    # whitespace scan above already refuses every string that has one.
    if "http" in value and _URL_IN_TEXT.search(value):
        return False
    if "/" in value and _ABSOLUTE_PATH_IN_TEXT.search(value):
        return False
    if ("=" in value or ":" in value) and _TEXT_KEY_VALUE.search(value):
        return False
    return True


def REDACT_TEXT(value: str, *, secret_values: set[str] | None = None) -> str:
    """Redact common credential forms from one piece of diagnostic text."""

    text = str(value)
    for secret in sorted(secret_values or (), key=len, reverse=True):
        if secret:
            text = text.replace(secret, REDACTED)
    # Provider keys frequently use the OpenAI-looking ``sk-`` prefix.  Keep
    # this generic and bounded; never echo the original token in a traceback.
    text = _TEXT_CREDENTIAL_PREFIX.sub(REDACTED, text)
    text = _TEXT_BEARER.sub("Bearer " + REDACTED, text)
    text = _redact_url_text(text)
    text = _redact_key_value_text(text)
    # Absolute paths are private even when no secret is present.  Preserve a
    # useful basename only for paths clearly marked by an error author.
    # A slash immediately following ``:`` or another slash belongs to a URL,
    # not a local absolute path. URLs have already had credentials and
    # sensitive query values removed by ``_redact_url_text`` above.
    #
    # Both rules below are searched first: a snapshot runs this over every
    # string it exposes, provider URLs and API bases included, and the
    # patterns only ever fire on text that actually looks like one.
    if _ABSOLUTE_PATH_IN_TEXT.search(text):
        text = _ABSOLUTE_PATH_IN_TEXT.sub("<private-path>", text)
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

    return re.sub(_URL_IN_TEXT, replace, value)


def safe_error_message(message: object, *, known_secrets: Sequence[str] = ()) -> str:
    """Normalize an error to a short, single-line, secret-free message."""

    text = REDACT_TEXT(str(message), secret_values=set(known_secrets))
    return text or "Core operation failed"


def safe_exception_message(error: BaseException, *, known_secrets: Sequence[str] = ()) -> str:
    """Return only a safe message; never include traceback/source context."""

    return safe_error_message(str(error), known_secrets=known_secrets)


__all__ = [
    "NON_SECRET_TOKEN_COUNTER_KEYS",
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
