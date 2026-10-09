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
# Fields whose names merely *count* a model's tokens.  The one marker cannot
# tell a credential from a budget, so the bare substring test read every
# token-shaped number as a secret and replaced it with the presence marker: a
# route's ``max_input_tokens`` — the window this app hands Codex — arrived at
# the pane as the string ``configured``.  The pane reads that string as "no
# custom window", so it painted the field empty beside the registry default's
# placeholder and a value the user typed there replaced whatever was set.  A
# name that states a quantity is therefore classified by shape, and never by
# the marker alone.
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
# What the number beside a token word counts.  ``input_tokens`` and
# ``codex_compaction_max_output_tokens`` both say how many tokens, never which
# one, and the word immediately before the plural noun is what proves it.  A
# credential's own qualifier (``access``, ``refresh``, ``bootstrap``, ``id``)
# is deliberately absent from this set, so those names stay secrets.
NON_SECRET_TOKEN_COUNT_KINDS = frozenset(
    {
        "actual",
        "cache_creation",
        "cache_read",
        "cached",
        "completion",
        "creation",
        "image",
        "input",
        "long",
        "output",
        "prompt",
        "read",
        "reasoning",
        "short",
        "stream",
        "target",
        "text",
        "thought",
        "thoughts",
        "total",
    }
)
# Segments that state a budget rather than a credential, wherever they sit in
# the name: ``model_auto_compact_token_limit``, ``token_budget``,
# ``token_count``, and the runtime setting spelled
# ``..._VISION_ROUTER_MAX_TOKENS`` are all configuration this app reads back
# and edits.
NON_SECRET_TOKEN_QUANTITY_SEGMENTS = frozenset(
    {"budget", "count", "limit", "max", "min"}
)
# ``_plain_string_is_safe`` has to answer "does REDACT_TEXT leave this string
# alone?" for every string a snapshot exposes, and the answer for the great
# majority of them is yes.  Two rules made a snapshot pay for text that never
# changes:
#
# * A Python character loop testing ``isspace() or not isascii()`` cost two
#   interpreter-level method calls per character.  One C-level scan of the
#   exact whitespace class replaces it (the class below is
#   ``chr(c).isspace()`` for the whole code space, verified exhaustively).
# * Treating every non-ASCII value as unproven sent each CJK provider, group,
#   and model name through the full rule set.  None of the five patterns can
#   match text that has no whitespace *and* no ``=``/``:``/``http``/``-``/``/``
#   shape, and a CJK name has none of them, so the probe now decides on the
#   literal each pattern needs rather than on the string's encoding.
#
# Whitelisting is only ever widened where the rules provably cannot fire;
# the probes below stay exactly as conservative as the rewrite they stand in
# for.
_TEXT_WHITESPACE = re.compile(
    "[\\x09-\\x0d\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028-\\u2029\\u202f\\u205f\\u3000]"
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
    """Normalize one key name the way every projection compares it.

    A snapshot asks this once per field of every object it exposes, and the
    same few hundred names repeat across all of them, so the normalized form
    is remembered for each distinct string key.  Only a real ``str`` is cached,
    so a non-string key can never collide with the string that spells it.
    """

    if type(key) is str:
        cached = _key_text_cache.get(key)
        if cached is not None:
            return cached
        text = key.strip().lower().replace("-", "_")
        if len(_key_text_cache) >= _KEY_TEXT_CACHE_LIMIT:
            _key_text_cache.clear()
        _key_text_cache[key] = text
        return text
    return str(key).strip().lower().replace("-", "_")


# Key classification answers the same question for the same key name on every
# snapshot, and a snapshot asks it once per field.  Remembering the answer for
# the names a projection actually uses keeps its per-key tuple scans off the
# repeated path.
_KEY_CLASSIFICATION_LIMIT = 4096
_key_classification_cache: "dict[str, tuple[bool, bool]]" = {}
# The same budget for the name normalization itself: a snapshot's key names are
# a few hundred distinct strings repeated across every object it projects.
_KEY_TEXT_CACHE_LIMIT = 8192
_key_text_cache: "dict[str, str]" = {}


def _key_classification(text: str) -> tuple[bool, bool]:
    """Whether one normalized key name names a secret and an absolute path."""

    cached = _key_classification_cache.get(text)
    if cached is not None:
        return cached
    secret = (
        False
        if text in NON_SECRET_TOKEN_COUNTER_KEYS
        or text in {
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
        or _token_quantity_key(text)
        else any(marker == text or marker in text for marker in SECRET_KEY_MARKERS)
    )
    path = any(marker == text or text.endswith(f"_{marker}") for marker in PATH_KEY_MARKERS)
    if len(_key_classification_cache) >= _KEY_CLASSIFICATION_LIMIT:
        _key_classification_cache.clear()
    _key_classification_cache[text] = (secret, path)
    return secret, path


def _token_quantity_key(text: str) -> bool:
    """Whether one normalized name states how many tokens, not which token.

    Only a name that mentions ``token`` is considered, so a credential spelled
    any other way is untouched.  Such a name is a quantity when it carries a
    budget segment (``max_input_tokens``, ``model_auto_compact_token_limit``,
    ``token_budget``, ``token_count``) or when it ends in a plural count of a
    named token kind (``input_tokens``, ``cache_read_tokens``).  Every other
    token name — ``access_token``, ``refresh_token``, ``bootstrap_token``,
    ``id_token``, ``provider_auth_token``, ``bootstrap_token_ttl_seconds``, or
    a bare ``tokens`` — names the credential itself and stays classified as
    one.  A name this test cannot prove is therefore a secret, which is the
    safe direction.
    """

    if "token" not in text:
        return False
    parts = text.split("_")
    if any(part in NON_SECRET_TOKEN_QUANTITY_SEGMENTS for part in parts):
        return True
    return (
        len(parts) >= 2
        and parts[-1] == "tokens"
        and parts[-2] in NON_SECRET_TOKEN_COUNT_KINDS
    )


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
        # A plain loop: this runs once per field of every object a snapshot
        # exposes, and binding the recursive call's arguments here is cheaper
        # than a comprehension that has to close over the enclosing frame.
        result: dict[Any, Any] = {}
        for key, item in value.items():
            result[str(key)] = redact(item, known_secrets=known_secrets, _key=key)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item, known_secrets=known_secrets) for item in value]
    # An immutable leaf this function hands back unchanged: copying it only paid
    # for a call, and every caller of a snapshot treats the result as read-only.
    if value is None or value is True or value is False or type(value) is int or type(value) is float:
        return value
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
    # Whitespace is joined; that join is the one rewrite that can happen to a
    # string none of the patterns below needs to have matched.
    if _TEXT_WHITESPACE.search(value) is not None:
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
    #
    # Every rule below is guarded by a cheap substring test for a literal its
    # own pattern cannot match without: the credential rules need the ``-``
    # that separates their prefix from the token, the URL rule needs ``http``,
    # the path rule needs ``/``, and the key/value rule needs ``=`` or ``:``.
    # A snapshot runs this over every string a projection exposes, and almost
    # all of them are plain names, ids, and slugs that carry none of those —
    # so those strings now cost a handful of C-level ``in`` tests instead of
    # four full regex scans.  The guards only ever skip a rule that provably
    # cannot match, so the rewrite is unchanged wherever it can apply.
    if "-" in text:
        text = _TEXT_CREDENTIAL_PREFIX.sub(REDACTED, text)
        text = _TEXT_BEARER.sub("Bearer " + REDACTED, text)
    if "http" in text:
        text = _redact_url_text(text)
    if "=" in text or ":" in text:
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
