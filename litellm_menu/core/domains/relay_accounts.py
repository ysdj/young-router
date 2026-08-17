"""Relay accounts and New API/Sub2API import adapters.

Remembered passwords and browser sessions are stored in the private account
file so they can move with the existing WebDAV configuration bundle. Public
snapshots never expose those values.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from http.cookies import SimpleCookie
import hashlib
import json
import math
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
import re
import ssl
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
import urllib.error
import urllib.request
import uuid

from ..persistence import AtomicJSONStore, PersistenceError, read_bytes
from ..security import safe_exception_message


DOMAIN_NAME = "relay_accounts"
ACCOUNT_TYPES = ("newapi", "sub2api")
LOGIN_STATUSES = ("signed_out", "signed_in", "expired", "unknown")
RESOURCE_STATUSES = ("idle", "ready", "unavailable")
RESOURCE_ERRORS = ("none", "login_expired", "no_api_keys", "no_models", "unavailable")
PENDING_CLEANUP_KINDS = ("credentials",)
PENDING_OPERATION_KINDS = (
    "api_key_create",
    "api_key_update",
    "api_key_set_group",
    "api_key_set_enabled",
    "api_key_delete",
)
PENDING_OPERATION_STATES = (
    "staged",
    "remote_applied",
    "local_pending",
    "completed",
    "failed",
)
DEPENDENCY_POLICIES = ("delete_models", "detach", "detach_disabled", "rebind")
MAX_ACCOUNTS = 64
MAX_STATIONS = MAX_ACCOUNTS
MAX_PENDING_CLEANUPS = MAX_ACCOUNTS
MAX_PENDING_OPERATIONS = 512
MAX_MODELS = 512
MAX_RESOURCES = 256
MAX_RESOURCE_ID = 128
MAX_GROUPS = 256
MAX_GROUP_ID = 160
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 12.0
DETECTION_RESPONSE_BYTES = 64 * 1024
DETECTION_TIMEOUT_SECONDS = 3.0
_SECRET_FIELDS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "cookie",
        "cookies",
        "password",
        "refresh_token",
        "secret",
        "token",
        "tokens",
    }
)


class RelayAccountsError(ValueError):
    """An error safe to return across the Core boundary."""


def _runtime_root(value: Path | str | None) -> Path:
    if value is not None:
        return Path(value).expanduser()
    configured = os.environ.get("LITELLM_RUNTIME_ROOT", "").strip() or os.environ.get(
        "LITELLM_MENU_HOME", ""
    ).strip()
    return Path(configured).expanduser() if configured else Path.home() / ".litellm-menu"


def _text(value: object, label: str, *, limit: int = 240, required: bool = True) -> str:
    result = value.strip() if isinstance(value, str) else ""
    if (required and not result) or len(result.encode("utf-8")) > limit or any(char in result for char in "\x00\r\n"):
        raise RelayAccountsError(f"{label} is invalid")
    return result


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _updated_at(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        return ""
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _origin(value: object) -> str:
    raw = _text(value, "Relay origin", limit=2048)
    if "://" not in raw:
        raw = f"https://{raw.lstrip('/')}"
    try:
        parsed = urlsplit(raw)
    except ValueError:
        raise RelayAccountsError("Relay origin is invalid") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise RelayAccountsError("Relay origin is invalid")
    if parsed.query or parsed.fragment:
        raise RelayAccountsError("Relay origin is invalid")
    hostname = parsed.hostname.rstrip(".").lower()
    loopback = hostname == "localhost" or hostname.endswith(".localhost") or hostname in {
        "127.0.0.1",
        "::1",
        "0:0:0:0:0:0:0:1",
    }
    if parsed.scheme != "https" and not loopback:
        raise RelayAccountsError("Relay origin must use HTTPS unless it is localhost")
    path_parts = [part for part in parsed.path.split("/") if part]
    # Pasted dashboard or API URLs should open the relay's browser site, not a
    # login endpoint or the OpenAI-compatible API route. Preserve an actual
    # deployment base path while trimming only universally known leaf paths.
    while path_parts and path_parts[-1].lower() in {"login", "signin", "sign-in", "dashboard", "v1"}:
        path_parts.pop()
    path = "/" + "/".join(path_parts) if path_parts else ""
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _api_base(value: object) -> str:
    """Normalize a relay API base while preserving its trailing ``/v1``."""

    raw = _text(value, "Relay API base", limit=2048)
    had_v1 = urlsplit(raw if "://" in raw else f"https://{raw.lstrip('/')}").path.rstrip("/").lower().endswith("/v1")
    origin = _origin(raw)
    return f"{origin}/v1" if had_v1 else origin


def _origin_key(value: str) -> tuple[str, str, int | None] | None:
    """Return a normalized origin tuple without preserving a response URL."""

    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        scheme = parsed.scheme.lower()
        port = parsed.port
        if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
            port = None
        return (scheme, parsed.hostname.rstrip(".").lower(), port)
    except ValueError:
        return None


def _relay_endpoint(origin: str, path: str) -> str:
    """Build one fixed relay endpoint without allowing an origin escape."""

    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or "://" in path
        or "\x00" in path
    ):
        raise RelayAccountsError("Relay detection endpoint is invalid")
    normalized = _origin(origin)
    endpoint = urljoin(normalized.rstrip("/") + "/", path.lstrip("/"))
    if _origin_key(endpoint) != _origin_key(normalized):
        raise RelayAccountsError("Relay detection endpoint is invalid")
    return endpoint


def _contains_secret(value: object, *, key: str = "") -> bool:
    normalized = key.strip().lower().replace("-", "_")
    if normalized == "remember_password":
        return False
    if normalized in _SECRET_FIELDS or any(marker in normalized for marker in ("password", "cookie", "token", "secret")):
        return True
    if isinstance(value, Mapping):
        return any(_contains_secret(item, key=str(name)) for name, item in value.items())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_secret(item) for item in value)
    return False


def _account_id(value: object) -> str:
    result = _text(value, "Relay account", limit=96)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", result):
        raise RelayAccountsError("Relay account is invalid")
    return result


def _station_id(value: object) -> str:
    result = _text(value, "Relay station", limit=96)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", result):
        raise RelayAccountsError("Relay station is invalid")
    return result


def _operation_id(value: object) -> str:
    result = _text(value, "Relay operation", limit=96)
    if not re.fullmatch(r"op-[A-Za-z0-9][A-Za-z0-9._-]{0,92}", result):
        raise RelayAccountsError("Relay operation is invalid")
    return result


def _dependency_policy(value: object, *, default: str = "detach") -> str:
    result = str(value or default).strip().lower().replace("-", "_")
    if result not in DEPENDENCY_POLICIES:
        raise RelayAccountsError("Relay dependency policy is invalid")
    return result


def _station_name(value: object, fallback: str = "") -> str:
    result = value.strip() if isinstance(value, str) else ""
    if not result:
        result = fallback.strip()
    if not result or len(result.encode("utf-8")) > 160 or any(char in result for char in "\x00\r\n"):
        raise RelayAccountsError("Relay station name is invalid")
    return result


def _station_origin_key(value: str) -> str:
    """Canonical identity for one relay deployment base URL.

    ``_origin`` keeps the first user's spelling for display.  Group identity
    additionally lower-cases hosts, removes default ports, and trims a
    trailing slash so equivalent base URLs share one station.
    """

    normalized = _origin(value)
    parsed = urlsplit(normalized)
    hostname = (parsed.hostname or "").rstrip(".").lower()
    try:
        port = parsed.port
    except ValueError:
        raise RelayAccountsError("Relay origin is invalid") from None
    if (parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80):
        port = None
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname if port is None else f"{hostname}:{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def _station_display_name(origin: str) -> str:
    parsed = urlsplit(origin)
    hostname = (parsed.hostname or "").strip().rstrip(".")
    if hostname:
        return hostname
    return origin


def _private_station(
    raw: Mapping[str, Any],
    *,
    fallback_name: str = "",
    fallback_type: str = "",
) -> dict[str, str]:
    station_type = str(raw.get("type", raw.get("station_type", fallback_type)))
    if station_type not in ACCOUNT_TYPES:
        raise RelayAccountsError("Relay station type is invalid")
    origin_value = raw.get("origin", raw.get("base_url"))
    origin = _origin(origin_value)
    name = _station_name(raw.get("name", raw.get("label", "")), fallback_name or _station_display_name(origin))
    return {
        "id": _station_id(raw.get("id")),
        "name": name,
        "origin": origin,
        "type": station_type,
    }


def _resource_id(value: object) -> str:
    result = value.strip() if isinstance(value, str) else ""
    if (
        not result
        or len(result.encode("utf-8")) > MAX_RESOURCE_ID
        or any(char in result for char in "\x00\r\n")
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", result)
    ):
        raise RelayAccountsError("Relay API resource is invalid")
    return result


def _resource_name(value: object, fallback: str) -> str:
    result = value.strip() if isinstance(value, str) else ""
    if not result:
        result = fallback
    if len(result.encode("utf-8")) > 160 or any(char in result for char in "\x00\r\n"):
        raise RelayAccountsError("Relay API resource is invalid")
    return result


def _group_id(value: object, *, required: bool = False) -> str:
    """Validate an upstream relay group identifier without normalizing it."""

    if isinstance(value, bool):
        result = ""
    elif isinstance(value, (str, int)):
        result = str(value).strip()
    else:
        result = ""
    if (
        (required and not result)
        or len(result.encode("utf-8")) > MAX_GROUP_ID
        or any(char in result for char in "\x00\r\n")
    ):
        raise RelayAccountsError("Relay API group is invalid")
    return result


def _group_multiplier(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        raw_multiplier = value
    elif isinstance(value, str) and value.strip():
        raw_multiplier = value.strip()
    else:
        return None
    try:
        multiplier = float(raw_multiplier)
    except ValueError:
        return None
    if not math.isfinite(multiplier) or multiplier < 0:
        return None
    return multiplier


def _safe_group(raw: Mapping[str, Any]) -> dict[str, Any]:
    group_id = _group_id(raw.get("id", raw.get("group_id")), required=True)
    name = _resource_name(raw.get("name", raw.get("group_name", group_id)), group_id)
    result: dict[str, Any] = {"id": group_id, "name": name}
    multiplier = _group_multiplier(raw.get("rate_multiplier", raw.get("ratio", raw.get("multiplier"))))
    if multiplier is not None:
        result["multiplier"] = multiplier
    return result


def _safe_groups(raw: object) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)) or len(raw) > MAX_GROUPS:
        raise RelayAccountsError("Relay API groups are invalid")
    groups = [_safe_group(item) for item in raw if isinstance(item, Mapping)]
    if len(groups) != len(raw) or len({item["id"] for item in groups}) != len(groups):
        raise RelayAccountsError("Relay API groups are invalid")
    return groups


def _safe_resource(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only non-secret API resource metadata in snapshots and storage."""

    resource_id = _resource_id(raw.get("id"))
    name = _resource_name(
        raw.get("name", raw.get("api_name", raw.get("key_name"))),
        f"API {resource_id.rsplit('-', 1)[-1]}",
    )
    api_base = _api_base(raw.get("api_base")) if raw.get("api_base") else ""
    models = _model_names(raw.get("models", []))
    hint = ""
    for key in ("key_hint", "key_name"):
        candidate = raw.get(key)
        if isinstance(candidate, str) and candidate.strip():
            hint = candidate.strip()
            break
    if len(hint.encode("utf-8")) > 160 or any(char in hint for char in "\x00\r\n"):
        hint = ""
    group_id = _group_id(raw.get("group_id"))
    group_name = _resource_name(raw.get("group_name"), group_id) if group_id else ""
    return {
        "id": resource_id,
        "name": name,
        "api_name": name,
        "api_base": api_base,
        "key_hint": hint,
        "enabled": raw.get("enabled") is not False,
        "models": models,
        "group_id": group_id,
        "group_name": group_name,
    }


def _safe_resources(raw: object) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)) or len(raw) > MAX_RESOURCES:
        raise RelayAccountsError("Relay API resources are invalid")
    resources = [_safe_resource(item) for item in raw if isinstance(item, Mapping)]
    if len(resources) != len(raw) or len({item["id"] for item in resources}) != len(resources):
        raise RelayAccountsError("Relay API resources are invalid")
    return resources


def _balance(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RelayAccountsError("Relay account balance is invalid")
    result = float(value)
    if not math.isfinite(result):
        raise RelayAccountsError("Relay account balance is invalid")
    return result


def _private_account(raw: Mapping[str, Any]) -> dict[str, Any]:
    account_type = str(raw.get("type", ""))
    if account_type not in ACCOUNT_TYPES:
        raise RelayAccountsError("Relay account type is invalid")
    status = str(raw.get("login_status", "unknown"))
    if status not in LOGIN_STATUSES:
        status = "unknown"
    resource_status = str(raw.get("resource_status", "idle"))
    if resource_status not in RESOURCE_STATUSES:
        resource_status = "idle"
    resource_error = str(raw.get("resource_error", "none"))
    if resource_error not in RESOURCE_ERRORS:
        resource_error = "none"
    remember_password = raw.get("remember_password") is True
    raw_session = raw.get("session", {})
    if raw_session is None:
        raw_session = {}
    if not isinstance(raw_session, Mapping):
        raise RelayAccountsError("Relay session storage is invalid")
    unknown_session_fields = set(raw_session).difference({"cookie", "access_token", "refresh_token"})
    if unknown_session_fields:
        raise RelayAccountsError("Relay session storage is invalid")
    session = {
        key: _text(raw_session.get(key, ""), "Relay session", limit=32768, required=False)
        for key in ("cookie", "access_token", "refresh_token")
    }
    if not session["cookie"] and not session["access_token"]:
        session = {}
    if not remember_password:
        session = {}
    return {
        "id": _account_id(raw.get("id")),
        "station_id": _station_id(raw.get("station_id")) if raw.get("station_id") else "",
        "type": account_type,
        "label": _text(raw.get("label"), "Relay label", limit=160),
        "origin": _origin(raw.get("origin")),
        "username": _text(raw.get("username", ""), "Relay username", limit=320, required=False),
        "login_status": status,
        "remember_password": remember_password,
        "password": _text(raw.get("password", ""), "Relay password", limit=4096, required=False) if remember_password else "",
        "session": session,
        "balance": _balance(raw.get("balance")),
        "last_updated_at": _updated_at(raw.get("last_updated_at")),
        "resource_status": resource_status,
        "resource_error": resource_error,
        "resources": _safe_resources(raw.get("resources", [])),
        "groups": _safe_groups(raw.get("groups", [])),
    }


def _reloaded_account(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Drop process-local login claims after Core restarts.

    The native host can restore its process session, while Core can attempt an
    explicitly remembered password. Until either succeeds, persisted metadata
    must not claim that the remote site is currently authenticated.
    """

    account = _private_account(raw)
    if account["login_status"] == "signed_in":
        account["login_status"] = "unknown"
    return account


def _public_account(account: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(account.get("id", "")),
        "station_id": str(account.get("station_id", "")),
        "type": str(account.get("type", "")),
        "label": str(account.get("label", "")),
        "origin": str(account.get("origin", "")),
        "username": str(account.get("username", "")),
        "login_status": str(account.get("login_status", "unknown")),
        "remember_password": account.get("remember_password") is True,
        "password_saved": bool(account.get("password")),
        "balance": _balance(account.get("balance")),
        "last_updated_at": str(account.get("last_updated_at", "")),
        "resource_status": str(account.get("resource_status", "idle")),
        "resource_error": str(account.get("resource_error", "none")),
        "resources": copy.deepcopy(account.get("resources", [])),
        "groups": copy.deepcopy(account.get("groups", [])),
    }


def _stored_account(account: Mapping[str, Any]) -> dict[str, Any]:
    stored = _public_account(account)
    stored.pop("password_saved", None)
    stored["password"] = str(account.get("password", ""))
    stored["session"] = copy.deepcopy(account.get("session", {}))
    return stored


def _public_station(station: Mapping[str, Any], account_count: int) -> dict[str, Any]:
    origin = str(station.get("origin", ""))
    return {
        "id": str(station.get("id", "")),
        "name": str(station.get("name", "")),
        "origin": origin,
        # Keep the explicit alias because callers may call this value a base
        # URL while the relay account API historically called it an origin.
        "base_url": origin,
        "url": origin,
        "type": str(station.get("type", "")),
        "account_count": max(0, int(account_count)),
    }


def _pending_cleanup(raw: Mapping[str, Any]) -> dict[str, str]:
    """Validate the durable, secret-free cleanup tombstone for one account."""

    kind = str(raw.get("kind", ""))
    if kind not in PENDING_CLEANUP_KINDS:
        raise RelayAccountsError("Relay cleanup storage is invalid")
    return {
        "account_id": _account_id(raw.get("account_id")),
        "label": _text(raw.get("label"), "Relay label", limit=160),
        "kind": kind,
    }


def _public_pending_cleanup(cleanup: Mapping[str, Any]) -> dict[str, str]:
    """Return only opaque account metadata; credentials stay native-only."""

    return {
        "account_id": str(cleanup.get("account_id", "")),
        "label": str(cleanup.get("label", "")),
        "kind": str(cleanup.get("kind", "")),
    }


def _pending_operation(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one durable, secret-free relay Apply operation."""

    if _contains_secret(raw):
        raise RelayAccountsError("Relay operation storage is invalid")
    kind = str(raw.get("kind", ""))
    state = str(raw.get("state", "staged"))
    if kind not in PENDING_OPERATION_KINDS or state not in PENDING_OPERATION_STATES:
        raise RelayAccountsError("Relay operation storage is invalid")
    raw_changes = raw.get("changes", {})
    if not isinstance(raw_changes, Mapping) or set(raw_changes).difference({"name", "group_id", "enabled"}):
        raise RelayAccountsError("Relay operation storage is invalid")
    changes: dict[str, Any] = {}
    if "name" in raw_changes:
        changes["name"] = _resource_name(raw_changes.get("name"), "API")
    if "group_id" in raw_changes:
        changes["group_id"] = _group_id(raw_changes.get("group_id"))
    if "enabled" in raw_changes:
        if not isinstance(raw_changes.get("enabled"), bool):
            raise RelayAccountsError("Relay operation storage is invalid")
        changes["enabled"] = raw_changes["enabled"]
    raw_known_ids = raw.get("known_resource_ids", [])
    if not isinstance(raw_known_ids, list) or len(raw_known_ids) > MAX_RESOURCES:
        raise RelayAccountsError("Relay operation storage is invalid")
    known_resource_ids = [_resource_id(item) for item in raw_known_ids]
    if len(set(known_resource_ids)) != len(known_resource_ids):
        raise RelayAccountsError("Relay operation storage is invalid")
    result: dict[str, Any] = {
        "id": _operation_id(raw.get("id")),
        "kind": kind,
        "state": state,
        "station_id": _station_id(raw.get("station_id")),
        "account_id": _account_id(raw.get("account_id")),
        "resource_id": _resource_id(raw.get("resource_id")),
        "changes": changes,
        "dependency_policy": _dependency_policy(raw.get("dependency_policy")),
        "known_resource_ids": known_resource_ids,
        "created_at": _updated_at(raw.get("created_at")) or _utc_now_iso(),
        "updated_at": _updated_at(raw.get("updated_at")) or _utc_now_iso(),
    }
    remote_resource_id = raw.get("remote_resource_id")
    if remote_resource_id:
        result["remote_resource_id"] = _resource_id(remote_resource_id)
    error = raw.get("error")
    if isinstance(error, str) and error.strip():
        result["error"] = _text(error, "Relay operation error", limit=240)
    return result


def _public_pending_operation(operation: Mapping[str, Any]) -> dict[str, Any]:
    """Project only identifiers and status; never request bodies or secrets."""

    result: dict[str, Any] = {
        "id": str(operation.get("id", "")),
        "kind": str(operation.get("kind", "")),
        "state": str(operation.get("state", "staged")),
        "status": str(operation.get("state", "staged")),
        "station_id": str(operation.get("station_id", "")),
        "account_id": str(operation.get("account_id", "")),
        "resource_id": str(operation.get("resource_id", "")),
        "dependency_policy": str(operation.get("dependency_policy", "detach")),
    }
    error = operation.get("error")
    if isinstance(error, str) and error:
        result["error"] = error
    return result


def _json_data(payload: object) -> object:
    if not isinstance(payload, Mapping):
        return payload
    current: object = payload
    for _ in range(3):
        if not isinstance(current, Mapping) or "data" not in current:
            break
        current = current["data"]
    return current


def _model_names(payload: object) -> list[str]:
    candidates = _json_data(payload)
    if isinstance(candidates, Mapping):
        candidates = candidates.get("models", candidates.get("items", []))
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes, bytearray)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        value: object = item
        if isinstance(item, Mapping):
            value = item.get("id", item.get("name", item.get("model")))
        if not isinstance(value, str):
            continue
        model = value.strip()
        if not model or len(model.encode("utf-8")) > 256 or any(ord(char) < 32 for char in model):
            continue
        if model not in seen:
            seen.add(model)
            result.append(model)
        if len(result) >= MAX_MODELS:
            break
    return result


def _newapi_token_enabled(value: object) -> bool:
    if type(value) is int:
        return value == 1
    return isinstance(value, str) and value.strip().lower() in {"1", "active", "enabled"}


def _sub2api_channel_models(payload: object) -> list[str]:
    channels = _json_data(payload)
    if not isinstance(channels, Sequence) or isinstance(channels, (str, bytes, bytearray)):
        return []
    flattened: list[object] = []
    for channel in channels:
        if not isinstance(channel, Mapping):
            continue
        platforms = channel.get("platforms", [])
        if not isinstance(platforms, Sequence) or isinstance(platforms, (str, bytes, bytearray)):
            continue
        for platform in platforms:
            if isinstance(platform, Mapping):
                models = platform.get("supported_models", [])
                if isinstance(models, Sequence) and not isinstance(models, (str, bytes, bytearray)):
                    flattened.extend(models)
    return _model_names(flattened)


class RelayHTTPClient:
    """Small redirect-free client for authenticated relay requests."""

    def __init__(self, opener: object | None = None):
        if opener is None:
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req: object, fp: object, code: int, msg: str, headers: object, newurl: str) -> None:
                    return None

            context = ssl.create_default_context()
            opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
        self._opener = opener

    def _request_json(
        self,
        origin: str,
        path: str,
        *,
        headers: Mapping[str, str],
        method: str,
        body: Mapping[str, Any] | None = None,
    ) -> object:
        if method not in {"GET", "POST", "PUT", "DELETE"}:
            raise RelayAccountsError("Relay request method is invalid")
        url = _relay_endpoint(origin, path)
        request_body = None
        request_headers = {"Accept": "application/json", "User-Agent": "LiteLLM-Menu-Core/1", **dict(headers)}
        if body is not None:
            request_body = json.dumps(dict(body), ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(
            url,
            data=request_body,
            headers=request_headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # type: ignore[attr-defined]
                status = getattr(response, "status", response.getcode())
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise RelayAccountsError("Relay login has expired") from None
            raise RelayAccountsError("Relay request was rejected") from None
        except Exception:
            raise RelayAccountsError("Relay is unavailable") from None
        if not isinstance(status, int) or status < 200 or status >= 300 or len(body) > MAX_RESPONSE_BYTES:
            raise RelayAccountsError("Relay returned an invalid response")
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RelayAccountsError("Relay returned an invalid response") from None

    def json(self, origin: str, path: str, *, headers: Mapping[str, str]) -> object:
        return self._request_json(origin, path, headers=headers, method="GET")

    def post(
        self,
        origin: str,
        path: str,
        *,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None = None,
    ) -> object:
        return self._request_json(origin, path, headers=headers, method="POST", body=body)

    def put(
        self,
        origin: str,
        path: str,
        *,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None = None,
    ) -> object:
        return self._request_json(origin, path, headers=headers, method="PUT", body=body)

    def delete(self, origin: str, path: str, *, headers: Mapping[str, str]) -> object:
        return self._request_json(origin, path, headers=headers, method="DELETE")

    def password_login(
        self,
        origin: str,
        account_type: str,
        username: str,
        password: str,
    ) -> dict[str, str]:
        if account_type == "newapi":
            path = "/api/user/login"
            fields = {"username": username, "password": password}
        elif account_type == "sub2api":
            path = "/api/v1/auth/login"
            fields = {"email": username, "password": password}
        else:
            raise RelayAccountsError("Relay account type is invalid")
        request = urllib.request.Request(
            _relay_endpoint(origin, path),
            data=json.dumps(fields, ensure_ascii=False).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "LiteLLM-Menu-Core/1",
            },
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # type: ignore[attr-defined]
                status = getattr(response, "status", response.getcode())
                body = response.read(MAX_RESPONSE_BYTES + 1)
                set_cookie_headers = response.headers.get_all("Set-Cookie") or []
        except urllib.error.HTTPError:
            raise RelayAccountsError("Relay username or password is invalid") from None
        except Exception:
            raise RelayAccountsError("Relay is unavailable") from None
        if not isinstance(status, int) or status < 200 or status >= 300 or len(body) > MAX_RESPONSE_BYTES:
            raise RelayAccountsError("Relay login was rejected")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RelayAccountsError("Relay returned an invalid response") from None
        data = _json_data(payload)
        if not isinstance(data, Mapping):
            raise RelayAccountsError("Relay login was rejected")
        if data.get("require_2fa") is True or data.get("requires_2fa") is True:
            raise RelayAccountsError("Relay login requires verification")
        user = data.get("user") if isinstance(data.get("user"), Mapping) else {}
        accepted_username = next(
            (
                value.strip()
                for value in (user.get("username"), user.get("email"), username)
                if isinstance(value, str) and value.strip()
            ),
            "",
        )
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        cookie_values: dict[str, str] = {}
        for header in set_cookie_headers:
            parsed = SimpleCookie()
            parsed.load(header)
            for name, morsel in parsed.items():
                cookie_values[name] = morsel.value
        cookie = "; ".join(f"{name}={value}" for name, value in sorted(cookie_values.items()))
        result = {
            "username": accepted_username,
            "cookie": cookie,
            "access_token": access_token.strip() if isinstance(access_token, str) else "",
            "refresh_token": refresh_token.strip() if isinstance(refresh_token, str) else "",
        }
        if not result["username"] or not (result["cookie"] or result["access_token"]):
            raise RelayAccountsError("Relay login was rejected")
        return result

    def probe(self, origin: str, path: str) -> tuple[int, Mapping[str, Any] | None]:
        """Read a small, unauthenticated endpoint for station type detection.

        Detection never follows redirects, never sends browser or account
        credentials, and keeps both the response and the result inside Core.
        Callers receive only a classified station type from the domain.
        """

        endpoint = _relay_endpoint(origin, path)
        request = urllib.request.Request(
            endpoint,
            headers={
                "Accept": "application/json",
                "Cache-Control": "no-store",
                "User-Agent": "LiteLLM-Menu-Core/1",
            },
            method="GET",
        )
        response: object | None = None
        try:
            try:
                response = self._opener.open(request, timeout=DETECTION_TIMEOUT_SECONDS)  # type: ignore[attr-defined]
            except urllib.error.HTTPError as exc:
                # A 401 from a known endpoint is a useful, credential-free
                # type signal. Its response body remains local to this method.
                response = exc
            except Exception:
                return 0, None
            status = getattr(response, "status", getattr(response, "code", None))
            geturl = getattr(response, "geturl", None)
            response_url = geturl() if callable(geturl) else endpoint
            if (
                not isinstance(status, int)
                or _origin_key(response_url) != _origin_key(endpoint)
            ):
                return 0, None
            reader = getattr(response, "read", None)
            if not callable(reader):
                return 0, None
            body = reader(DETECTION_RESPONSE_BYTES + 1)
            if not isinstance(body, bytes) or len(body) > DETECTION_RESPONSE_BYTES:
                return status, None
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return status, None
            return status, dict(payload) if isinstance(payload, Mapping) else None
        finally:
            closer = getattr(response, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass


def _is_newapi_status(status: int, payload: Mapping[str, Any] | None) -> bool:
    return 200 <= status < 300 and isinstance(payload, Mapping) and isinstance(payload.get("success"), bool)


def _is_sub2api_keys(status: int, payload: Mapping[str, Any] | None) -> bool:
    if status not in {200, 401, 403} or not isinstance(payload, Mapping):
        return False
    # Sub2API keeps this envelope for both an authenticated key list and its
    # unauthenticated response. Do not inspect or return its message content.
    return "code" in payload and "message" in payload


class RelayAccountsDomain:
    name = DOMAIN_NAME

    def __init__(
        self,
        runtime_root: Path | str | None = None,
        *,
        storage_path: Path | str | None = None,
        http_client: RelayHTTPClient | None = None,
    ) -> None:
        root = _runtime_root(runtime_root)
        self.storage_path = Path(storage_path).expanduser() if storage_path else root / ".litellm-runtime" / "relay-accounts.json"
        self._store = AtomicJSONStore(self.storage_path)
        self.journal_path = self.storage_path.with_name("relay-apply-operations.json")
        self._journal_store = AtomicJSONStore(self.journal_path)
        self._http = http_client or RelayHTTPClient()
        self._stations: list[dict[str, str]] = []
        self._accounts: list[dict[str, Any]] = []
        # The active copy stays process-local. An explicitly remembered login
        # also has a durable private copy beside the plaintext password.
        self._session_secrets: dict[str, dict[str, str]] = {}
        # Raw API keys remain process-local. They are populated from an
        # authenticated refresh/read and never enter snapshots or storage.
        self._resource_secret_cache: dict[str, str] = {}
        # A native browser-session erase can fail after account deletion. Keep
        # only an opaque tombstone so the UI can retry after a restart.
        self._pending_credential_cleanups: list[dict[str, str]] = []
        # Account, station, and relay-key CRUD are one explicit draft.  Login
        # and metadata refresh may update the same in-memory state, but never
        # smuggle a draft change across the persistence boundary.
        self._pending_operations: list[dict[str, Any]] = []
        self._draft_staged = False
        self._import_staged = False
        # Binding counts are derived by the provider/model domain. They are
        # deliberately process-local and contain only stable IDs and counts.
        self._binding_summary: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._last_action: dict[str, Any] = {}
        self._baseline_bytes: bytes | None = None
        self.revision = 0
        self.reload()

    def _stored_payload(self) -> dict[str, Any]:
        """Return the complete durable relay document, including opt-in secrets.

        This value is deliberately private: callers must use ``snapshot`` for
        the IPC-safe view.  It is shared by persistence and trusted package
        export so the package cannot accidentally omit remembered sessions or
        passwords that are required to restore a selected relay account.
        """

        return {
            "version": 3,
            "stations": [copy.deepcopy(station) for station in self._stations],
            "accounts": [_stored_account(account) for account in self._accounts],
            "pending_credential_cleanups": [
                _public_pending_cleanup(cleanup)
                for cleanup in self._pending_credential_cleanups
            ],
        }

    def _journal_payload(self) -> dict[str, Any]:
        """Return the durable no-secret remote-operation journal."""

        return {
            "version": 1,
            "operations": [copy.deepcopy(operation) for operation in self._pending_operations],
        }

    def _read_journal(self) -> list[dict[str, Any]]:
        try:
            payload = self._journal_store.read(default={"version": 1, "operations": []})
        except PersistenceError as exc:
            raise RelayAccountsError(safe_exception_message(exc)) from None
        if not isinstance(payload, Mapping) or payload.get("version", 1) != 1:
            raise RelayAccountsError("Relay operation storage is invalid")
        raw_operations = payload.get("operations", [])
        if not isinstance(raw_operations, list) or len(raw_operations) > MAX_PENDING_OPERATIONS:
            raise RelayAccountsError("Relay operation storage is invalid")
        operations = [_pending_operation(item) for item in raw_operations if isinstance(item, Mapping)]
        if len(operations) != len(raw_operations) or len({item["id"] for item in operations}) != len(operations):
            raise RelayAccountsError("Relay operation storage is invalid")
        return operations

    def _persist_journal(self) -> None:
        try:
            if self._pending_operations:
                self._journal_store.write(self._journal_payload())
            elif self.journal_path.exists():
                # AtomicJSONStore has no delete primitive. An empty journal is
                # enough to make replay state unambiguous after a restart.
                self._journal_store.write({"version": 1, "operations": []})
        except PersistenceError as exc:
            raise RelayAccountsError(safe_exception_message(exc)) from None

    def _has_staged_changes(self) -> bool:
        return self._draft_staged or self._import_staged or bool(self._pending_operations)

    def _read_storage_bytes(self) -> bytes | None:
        try:
            return read_bytes(self.storage_path)
        except PersistenceError as exc:
            raise RelayAccountsError(safe_exception_message(exc)) from None

    def _persist(self, *, force: bool = False) -> None:
        # No ordinary relay CRUD may write the durable account file until the
        # shared Apply coordinator explicitly commits it.
        if self._has_staged_changes() and not force:
            return
        try:
            self._store.write(self._stored_payload())
            self._baseline_bytes = self._read_storage_bytes()
        except PersistenceError as exc:
            raise RelayAccountsError(safe_exception_message(exc)) from None

    def transaction_checkpoint(self) -> dict[str, Any]:
        """Capture only mutable relay state; the HTTP client stays shared."""

        return {
            "stations": copy.deepcopy(self._stations),
            "accounts": copy.deepcopy(self._accounts),
            "session_secrets": copy.deepcopy(self._session_secrets),
            "resource_secret_cache": copy.deepcopy(self._resource_secret_cache),
            "pending_credential_cleanups": copy.deepcopy(self._pending_credential_cleanups),
            "pending_operations": copy.deepcopy(self._pending_operations),
            "draft_staged": self._draft_staged,
            "import_staged": self._import_staged,
            "binding_summary": copy.deepcopy(self._binding_summary),
            "last_action": copy.deepcopy(self._last_action),
            "baseline_bytes": self._baseline_bytes,
            "revision": self.revision,
        }

    def restore_transaction(self, checkpoint: Mapping[str, Any]) -> None:
        stations = checkpoint.get("stations")
        accounts = checkpoint.get("accounts")
        secrets = checkpoint.get("session_secrets")
        resource_secrets = checkpoint.get("resource_secret_cache")
        pending_cleanups = checkpoint.get("pending_credential_cleanups")
        pending_operations = checkpoint.get("pending_operations")
        draft_staged = checkpoint.get("draft_staged")
        import_staged = checkpoint.get("import_staged")
        binding_summary = checkpoint.get("binding_summary")
        last_action = checkpoint.get("last_action")
        baseline_bytes = checkpoint.get("baseline_bytes")
        revision = checkpoint.get("revision")
        if (
            not isinstance(stations, list)
            or any(not isinstance(item, Mapping) for item in stations)
            or not isinstance(accounts, list)
            or not isinstance(secrets, Mapping)
            or not isinstance(resource_secrets, Mapping)
            or not isinstance(pending_cleanups, list)
            or not isinstance(pending_operations, list)
            or type(draft_staged) is not bool
            or type(import_staged) is not bool
            or not isinstance(binding_summary, Mapping)
            or not isinstance(last_action, Mapping)
            or (baseline_bytes is not None and not isinstance(baseline_bytes, bytes))
            or type(revision) is not int
        ):
            raise RelayAccountsError("Relay account rollback failed")
        self._stations = copy.deepcopy([dict(item) for item in stations])
        self._accounts = copy.deepcopy(accounts)
        self._session_secrets = copy.deepcopy(dict(secrets))
        self._resource_secret_cache = copy.deepcopy(dict(resource_secrets))
        self._pending_credential_cleanups = copy.deepcopy(pending_cleanups)
        self._pending_operations = copy.deepcopy(pending_operations)
        self._draft_staged = draft_staged
        self._import_staged = import_staged
        self._binding_summary = copy.deepcopy(dict(binding_summary))
        self._last_action = copy.deepcopy(dict(last_action))
        self._baseline_bytes = baseline_bytes
        self.revision = revision

    def _index(self, value: object) -> int:
        account_id = _account_id(value)
        for index, account in enumerate(self._accounts):
            if account["id"] == account_id:
                return index
        raise RelayAccountsError("Relay account is unavailable")

    def _station_index(self, value: object) -> int:
        station_id = _station_id(value)
        for index, station in enumerate(self._stations):
            if station["id"] == station_id:
                return index
        raise RelayAccountsError("Relay station is unavailable")

    def _station_for_origin(self, origin: str) -> int | None:
        key = _station_origin_key(origin)
        for index, station in enumerate(self._stations):
            if _station_origin_key(station["origin"]) == key:
                return index
        return None

    def _station_account_count(self, station_id: str) -> int:
        return sum(1 for account in self._accounts if account.get("station_id") == station_id)

    def _new_station(self, *, name: object, origin: object, station_type: object, station_id: object | None = None) -> dict[str, str]:
        normalized_origin = _origin(origin)
        parsed_type = str(station_type)
        if parsed_type not in ACCOUNT_TYPES:
            raise RelayAccountsError("Relay station type is invalid")
        station = _private_station(
            {
                "id": station_id or f"station-{uuid.uuid4().hex}",
                "name": name,
                "origin": normalized_origin,
                "type": parsed_type,
            }
        )
        if self._station_for_origin(station["origin"]) is not None:
            raise RelayAccountsError("Relay station already exists")
        if len(self._stations) >= MAX_STATIONS:
            raise RelayAccountsError("Relay station limit reached")
        return station

    @staticmethod
    def _invalidate_account_session(account: Mapping[str, Any], origin: str) -> dict[str, Any]:
        """Move an account to a new host without reusing old credentials."""

        next_account = copy.deepcopy(dict(account))
        next_account["origin"] = origin
        next_account["login_status"] = "signed_out"
        next_account["password"] = ""
        next_account["session"] = {}
        next_account["balance"] = None
        next_account["last_updated_at"] = ""
        next_account["resource_status"] = "idle"
        next_account["resource_error"] = "none"
        next_account["resources"] = []
        next_account["groups"] = []
        return next_account

    def _retain_pending_cleanup(self, *, account_id: str, label: str, kind: str) -> None:
        cleanup = _pending_cleanup(
            {"account_id": account_id, "label": label, "kind": kind}
        )
        retained = [
            item
            for item in self._pending_credential_cleanups
            if not (item["account_id"] == cleanup["account_id"] and item["kind"] == cleanup["kind"])
        ]
        if len(retained) >= MAX_PENDING_CLEANUPS:
            raise RelayAccountsError("Relay credential cleanup limit reached")
        self._pending_credential_cleanups = [*retained, cleanup]

    def set_binding_summary(self, summary: object) -> None:
        """Accept provider-derived link counts without exposing provider state.

        The Core coordinator may call this before taking a snapshot. The
        summary is never persisted because providers/models remain the source
        of truth for bindings.
        """

        raw_items = summary.get("resources", []) if isinstance(summary, Mapping) else summary
        if raw_items is None:
            raw_items = []
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes, bytearray)):
            raise RelayAccountsError("Relay binding summary is invalid")
        parsed: dict[tuple[str, str, str], dict[str, Any]] = {}
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                raise RelayAccountsError("Relay binding summary is invalid")
            key = (
                _station_id(raw.get("station_id")),
                _account_id(raw.get("account_id")),
                _resource_id(raw.get("resource_id")),
            )
            count = raw.get("linked_model_count", raw.get("model_count", 0))
            if type(count) is not int or count < 0:
                raise RelayAccountsError("Relay binding summary is invalid")
            status = str(raw.get("binding_status", "linked" if count else "independent"))
            parsed[key] = {
                "linked_model_count": count,
                "binding_status": status[:64] if status else "independent",
            }
        self._binding_summary = parsed

    def _pending_operation_summary(self) -> dict[str, int]:
        summary = {
            "total": len(self._pending_operations),
            "staged": 0,
            "remote_applied": 0,
            "local_pending": 0,
            "completed": 0,
            "failed": 0,
            "destructive": 0,
        }
        for operation in self._pending_operations:
            state = str(operation.get("state", "staged"))
            if state in summary:
                summary[state] += 1
            if operation.get("kind") == "api_key_delete":
                summary["destructive"] += 1
        return summary

    def _resource_binding_summary(self, station_id: str, account_id: str, resource_id: str) -> dict[str, Any]:
        return self._binding_summary.get(
            (station_id, account_id, resource_id),
            {"linked_model_count": 0, "binding_status": "independent"},
        )

    def snapshot(self) -> dict[str, Any]:
        accounts: list[dict[str, Any]] = []
        for account in self._accounts:
            public = _public_account(account)
            station_id = str(account.get("station_id", ""))
            account_id = str(account.get("id", ""))
            account_operations = [
                operation for operation in self._pending_operations if operation.get("account_id") == account_id
            ]
            linked_model_count = 0
            for resource in public["resources"]:
                resource_id = str(resource.get("id", ""))
                binding = self._resource_binding_summary(station_id, account_id, resource_id)
                resource_operations = [
                    operation
                    for operation in account_operations
                    if resource_id in {operation.get("resource_id"), operation.get("remote_resource_id")}
                ]
                resource["linked_model_count"] = int(binding.get("linked_model_count", 0))
                resource["binding_status"] = str(binding.get("binding_status", "independent"))
                resource["last_synced_at"] = str(account.get("last_updated_at", ""))
                resource["pending_operation_count"] = len(resource_operations)
                resource["pending_operation_kinds"] = [
                    str(operation.get("kind", "")) for operation in resource_operations
                ]
                resource["pending_delete"] = any(
                    operation.get("kind") == "api_key_delete" for operation in resource_operations
                )
                linked_model_count += resource["linked_model_count"]
            public["linked_model_count"] = linked_model_count
            public["pending_operation_count"] = len(account_operations)
            accounts.append(public)
        stations: list[dict[str, Any]] = []
        for station in self._stations:
            public = _public_station(station, self._station_account_count(station["id"]))
            station_accounts = [account for account in accounts if account.get("station_id") == station["id"]]
            public["linked_model_count"] = sum(int(account.get("linked_model_count", 0)) for account in station_accounts)
            public["pending_operation_count"] = sum(
                int(account.get("pending_operation_count", 0)) for account in station_accounts
            )
            stations.append(public)
        pending_cleanups = [
            _public_pending_cleanup(cleanup)
            for cleanup in self._pending_credential_cleanups
        ]
        pending_operations = [
            _public_pending_operation(operation)
            for operation in self._pending_operations
        ]
        return {
            "domain": self.name,
            "revision": self.revision,
            "stations": stations,
            "station_count": len(stations),
            "accounts": accounts,
            "account_count": len(accounts),
            "pending_credential_cleanups": pending_cleanups,
            "pending_operations": pending_operations,
            "pending_operation_count": len(pending_operations),
            "pending_operation_summary": self._pending_operation_summary(),
            "last_action": copy.deepcopy(self._last_action),
        }

    def draft_state(self) -> object:
        if not self._has_staged_changes():
            return {}
        # Core owns this value in-process. It is intentionally richer than the
        # snapshot so remembered credentials can still be committed without
        # ever crossing IPC.
        return {
            "storage": self._stored_payload(),
            "operations": self._journal_payload()["operations"],
        }

    def _migration_station(
        self,
        account: Mapping[str, Any],
        *,
        used_ids: set[str],
        used_origins: set[str],
    ) -> dict[str, str]:
        if len(used_ids) >= MAX_STATIONS:
            raise RelayAccountsError("Relay station limit reached")
        origin = _origin(account.get("origin"))
        key = _station_origin_key(origin)
        if key in used_origins:
            raise RelayAccountsError("Relay station storage is invalid")
        station_id = f"station-{uuid.uuid4().hex}"
        while station_id in used_ids:
            station_id = f"station-{uuid.uuid4().hex}"
        return _private_station(
            {
                "id": station_id,
                "name": account.get("station_name", account.get("label", "")),
                "origin": origin,
                "type": account.get("type"),
            },
            fallback_name=_station_display_name(origin),
        )

    def _decode_storage(
        self,
        loaded: Mapping[str, Any],
    ) -> tuple[list[dict[str, str]], list[dict[str, Any]], list[dict[str, str]], bool]:
        """Validate one durable relay document without mutating disk or state."""

        if not isinstance(loaded, Mapping):
            raise RelayAccountsError("Relay account storage is invalid")
        if loaded.get("version", 1) not in {1, 3}:
            raise RelayAccountsError("Relay account storage version is unsupported")
        raw_accounts = loaded.get("accounts", [])
        if not isinstance(raw_accounts, list) or len(raw_accounts) > MAX_ACCOUNTS:
            raise RelayAccountsError("Relay account storage is invalid")
        accounts = [_reloaded_account(item) for item in raw_accounts if isinstance(item, Mapping)]
        if len(accounts) != len(raw_accounts) or len({item["id"] for item in accounts}) != len(accounts):
            raise RelayAccountsError("Relay account storage is invalid")

        raw_stations = loaded.get("stations", [])
        if not isinstance(raw_stations, list) or len(raw_stations) > MAX_STATIONS:
            raise RelayAccountsError("Relay station storage is invalid")

        # Station identity is the normalized deployment origin, not the
        # display name.  Deduplicate persisted stations before attaching
        # accounts so older or hand-edited files cannot create two groups for
        # one base URL.
        stations: list[dict[str, str]] = []
        station_by_id: dict[str, dict[str, str]] = {}
        station_by_origin: dict[str, dict[str, str]] = {}
        station_aliases: dict[str, str] = {}
        for raw in raw_stations:
            if not isinstance(raw, Mapping):
                raise RelayAccountsError("Relay station storage is invalid")
            raw_origin = raw.get("origin", raw.get("base_url", ""))
            fallback_type = next(
                (
                    str(account.get("type", ""))
                    for account in accounts
                    if isinstance(account, Mapping)
                    and _station_origin_key(str(account.get("origin", ""))) == _station_origin_key(str(raw_origin))
                ),
                "newapi",
            )
            station = _private_station(raw, fallback_type=fallback_type)
            if station["id"] in station_by_id:
                raise RelayAccountsError("Relay station storage is invalid")
            key = _station_origin_key(station["origin"])
            existing = station_by_origin.get(key)
            if existing is not None:
                station_aliases[station["id"]] = existing["id"]
                continue
            stations.append(station)
            station_by_id[station["id"]] = station
            station_by_origin[key] = station

        # Files written before the station model have no ``stations`` array.
        # Create one group per normalized account origin and use the first
        # account label as a stable migration name.
        migrated_missing_station = False
        relinked_accounts = False
        for account in accounts:
            raw_station_id = account.get("station_id", "")
            station: dict[str, str] | None = None
            if isinstance(raw_station_id, str) and raw_station_id:
                station = station_by_id.get(station_aliases.get(raw_station_id, raw_station_id))
            if station is None:
                key = _station_origin_key(account["origin"])
                station = station_by_origin.get(key)
            if station is None:
                station = self._migration_station(
                    account,
                    used_ids=set(station_by_id),
                    used_origins=set(station_by_origin),
                )
                stations.append(station)
                station_by_id[station["id"]] = station
                station_by_origin[_station_origin_key(station["origin"])] = station
                migrated_missing_station = True
            # The station URL is the source of truth for every account in a
            # group.  This repairs old records with an absent or stale
            # ``station_id`` while preserving the account's other metadata.
            if account.get("station_id") != station["id"] or account.get("origin") != station["origin"]:
                relinked_accounts = True
            account["station_id"] = station["id"]
            account["origin"] = station["origin"]

        raw_cleanups = loaded.get("pending_credential_cleanups", [])
        if not isinstance(raw_cleanups, list) or len(raw_cleanups) > MAX_PENDING_CLEANUPS:
            raise RelayAccountsError("Relay cleanup storage is invalid")
        pending_cleanups = [_pending_cleanup(item) for item in raw_cleanups if isinstance(item, Mapping)]
        cleanup_keys = {(item["account_id"], item["kind"]) for item in pending_cleanups}
        account_ids = {account["id"] for account in accounts}
        has_invalid_cleanup_owner = any(
            (cleanup["kind"] == "credentials" and cleanup["account_id"] in account_ids)
            or (cleanup["kind"] == "password" and cleanup["account_id"] not in account_ids)
            for cleanup in pending_cleanups
        )
        if (
            len(pending_cleanups) != len(raw_cleanups)
            or len(cleanup_keys) != len(pending_cleanups)
            or has_invalid_cleanup_owner
        ):
            raise RelayAccountsError("Relay cleanup storage is invalid")
        migrated = (
            loaded.get("version") != 3
            or "stations" not in loaded
            or migrated_missing_station
            or relinked_accounts
            or any(not isinstance(item.get("station_id"), str) or not item.get("station_id") for item in raw_accounts if isinstance(item, Mapping))
            or len(stations) != len(raw_stations)
        )
        return stations, accounts, pending_cleanups, migrated

    def _replace_storage_state(self, loaded: Mapping[str, Any]) -> bool:
        stations, accounts, pending_cleanups, migrated = self._decode_storage(loaded)
        self._stations = stations
        self._accounts = accounts
        self._pending_credential_cleanups = pending_cleanups
        # A reload or package replacement invalidates every process-local
        # credential. Reusing an account id from another file must never reuse
        # the previous file's browser session or API-key cache.
        self._session_secrets = {}
        self._resource_secret_cache = {}
        return migrated

    def persistence_paths(self) -> tuple[Path, ...]:
        return (self.storage_path,)

    def operation_journal_paths(self) -> tuple[Path, ...]:
        """Return the non-rollback journal used after remote side effects."""

        return (self.journal_path,)

    def external_disk_state(self) -> dict[str, bool]:
        current = self._read_storage_bytes()
        return {"changed": current != self._baseline_bytes, "exists": current is not None}

    def external_disk_identity(self) -> str:
        current = self._read_storage_bytes()
        return "missing" if current is None else hashlib.sha256(b"present\0" + current).hexdigest()

    def rebase_external_disk(self) -> dict[str, Any]:
        self._baseline_bytes = self._read_storage_bytes()
        self.revision += 1
        return self.snapshot()

    def reload(self) -> dict[str, Any]:
        try:
            loaded = self._store.read(
                default={"version": 1, "accounts": [], "pending_credential_cleanups": []}
            )
        except PersistenceError as exc:
            raise RelayAccountsError(safe_exception_message(exc)) from None
        if not isinstance(loaded, Mapping):
            raise RelayAccountsError("Relay account storage is invalid")
        migrated = self._replace_storage_state(loaded)
        self._pending_operations = self._read_journal()
        self._draft_staged = bool(self._pending_operations)
        self._import_staged = False
        self._last_action = {}
        self._binding_summary = {}
        self._baseline_bytes = self._read_storage_bytes()
        if migrated:
            self._persist(force=True)
        self.revision += 1
        return self.snapshot()

    def detect_type(self, origin: object) -> dict[str, str | None]:
        """Classify a relay using fixed public endpoints only.

        The two probes have deliberately narrow signatures: New API exposes a
        public ``/api/status`` response with a boolean ``success`` field,
        while Sub2API's key endpoint keeps its ``code``/``message`` envelope
        even when it correctly rejects an unauthenticated request.  No
        response body, HTTP status, URL, or error text leaves this method.
        """

        normalized = _origin(origin)
        probe = getattr(self._http, "probe", None)
        if not callable(probe):
            return {"detected_type": None, "confidence": "unknown"}

        def read(path: str) -> tuple[int, Mapping[str, Any] | None]:
            try:
                result = probe(normalized, path)
            except Exception:
                return 0, None
            if (
                not isinstance(result, tuple)
                or len(result) != 2
                or type(result[0]) is not int
            ):
                return 0, None
            return result[0], result[1] if isinstance(result[1], Mapping) else None

        status, payload = read("/api/status")
        if _is_newapi_status(status, payload):
            return {"detected_type": "newapi", "confidence": "high"}

        status, payload = read("/api/v1/keys?page=1&page_size=1")
        if _is_sub2api_keys(status, payload):
            return {"detected_type": "sub2api", "confidence": "high"}
        return {"detected_type": None, "confidence": "unknown"}

    def _account_station(
        self,
        data: Mapping[str, Any],
        account_type: str,
    ) -> tuple[dict[str, str], str]:
        """Resolve an account's station, creating one for a new base URL."""

        nested = data.get("station")
        station_data = dict(nested) if isinstance(nested, Mapping) else {}
        station_id_value = data.get("station_id", station_data.get("id"))
        station_name_value = data.get("station_name", station_data.get("name", data.get("name")))
        station_type_value = data.get("station_type", station_data.get("type", account_type))
        station_origin_value = data.get(
            "station_origin",
            station_data.get("origin", station_data.get("base_url", data.get("base_url"))),
        )

        if station_id_value:
            station = self._stations[self._station_index(station_id_value)]
            account_origin_value = data.get("origin", station["origin"])
            account_origin = _origin(account_origin_value)
            if _station_origin_key(account_origin) != _station_origin_key(station["origin"]):
                raise RelayAccountsError("Relay account origin does not match the selected station")
            return station, station["origin"]

        account_origin_value = data.get("origin", station_origin_value)
        account_origin = _origin(account_origin_value)
        if station_origin_value:
            candidate_origin = _origin(station_origin_value)
            if _station_origin_key(candidate_origin) != _station_origin_key(account_origin):
                raise RelayAccountsError("Relay station origin does not match the account origin")
        else:
            candidate_origin = account_origin
        existing_index = self._station_for_origin(candidate_origin)
        if existing_index is not None:
            return self._stations[existing_index], self._stations[existing_index]["origin"]

        station_name = station_name_value
        if not station_name:
            station_name = data.get("label") or _station_display_name(candidate_origin)
        station = self._new_station(
            name=station_name,
            origin=candidate_origin,
            station_type=station_type_value,
        )
        self._stations.append(station)
        return station, station["origin"]

    def _update_station(self, data: Mapping[str, Any]) -> None:
        station_id = data.get("id", data.get("station_id"))
        index = self._station_index(station_id)
        current = self._stations[index]
        old_origin = current["origin"]
        origin_value = data.get("origin", data.get("base_url", old_origin))
        new_origin = _origin(origin_value)
        new_name = _station_name(
            data.get(
                "name",
                data.get("station_name", data.get("display_name", data.get("label", current["name"]))),
            ),
            current["name"],
        )
        new_type = str(data.get("type", data.get("station_type", data.get("account_type", current["type"]))))
        if new_type not in ACCOUNT_TYPES:
            raise RelayAccountsError("Relay station type is invalid")

        duplicate_index = self._station_for_origin(new_origin)
        if duplicate_index is not None and duplicate_index != index:
            # An explicit URL edit onto an existing deployment merges the two
            # groups.  The edited group's name/type win because the user just
            # confirmed those values in the station editor.
            target = self._stations[duplicate_index]
            target["name"] = new_name
            target["type"] = new_type
            source_id = current["id"]
            target_id = target["id"]
            for account_index, account in enumerate(self._accounts):
                if account.get("station_id") not in {source_id, target_id}:
                    continue
                origin_changed = _station_origin_key(str(account.get("origin", ""))) != _station_origin_key(target["origin"])
                type_changed = str(account.get("type", "")) != new_type
                if origin_changed or type_changed:
                    self._session_secrets.pop(str(account["id"]), None)
                    moved = self._invalidate_account_session(account, target["origin"])
                else:
                    moved = copy.deepcopy(account)
                    moved["origin"] = target["origin"]
                moved["type"] = new_type
                moved["station_id"] = target_id
                self._accounts[account_index] = _private_account(moved)
            self._stations.pop(index)
            return

        self._stations[index] = {
            "id": current["id"],
            "name": new_name,
            "origin": new_origin,
            "type": new_type,
        }
        origin_changed = _station_origin_key(old_origin) != _station_origin_key(new_origin)
        for account_index, account in enumerate(self._accounts):
            if account.get("station_id") != current["id"]:
                continue
            if origin_changed:
                self._session_secrets.pop(str(account["id"]), None)
                updated = self._invalidate_account_session(account, new_origin)
            else:
                updated = copy.deepcopy(account)
                updated["origin"] = new_origin
            if str(account.get("type", "")) != new_type:
                self._session_secrets.pop(str(account["id"]), None)
                updated = self._invalidate_account_session(account, new_origin)
            updated["type"] = new_type
            self._accounts[account_index] = _private_account(updated)

    def _move_account_to_station(self, index: int, station: Mapping[str, str]) -> None:
        account = self._accounts[index]
        old_station_id = str(account.get("station_id", ""))
        old_origin = str(account.get("origin", ""))
        new_origin = str(station["origin"])
        origin_changed = _station_origin_key(old_origin) != _station_origin_key(new_origin)
        if origin_changed:
            self._session_secrets.pop(str(account["id"]), None)
            updated = self._invalidate_account_session(account, new_origin)
        else:
            updated = copy.deepcopy(account)
            updated["origin"] = new_origin
        updated["station_id"] = station["id"]
        self._accounts[index] = _private_account(updated)
        if old_station_id and old_station_id != station["id"] and self._station_account_count(old_station_id) == 0:
            self._stations = [item for item in self._stations if item["id"] != old_station_id]

    def _new_pending_operation(
        self,
        *,
        kind: str,
        account: Mapping[str, Any],
        resource_id: object,
        changes: Mapping[str, Any] | None = None,
        dependency_policy: object = "detach",
        known_resource_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        if len(self._pending_operations) >= MAX_PENDING_OPERATIONS:
            raise RelayAccountsError("Relay operation limit reached")
        operation = _pending_operation(
            {
                "id": f"op-{uuid.uuid4().hex}",
                "kind": kind,
                "state": "staged",
                "station_id": account.get("station_id"),
                "account_id": account.get("id"),
                "resource_id": resource_id,
                "changes": dict(changes or {}),
                "dependency_policy": dependency_policy,
                "known_resource_ids": list(known_resource_ids),
                "created_at": _utc_now_iso(),
                "updated_at": _utc_now_iso(),
            }
        )
        self._pending_operations.append(operation)
        self._draft_staged = True
        return operation

    def _pending_for_resource(self, account_id: str, resource_id: str) -> list[dict[str, Any]]:
        return [
            operation
            for operation in self._pending_operations
            if operation.get("account_id") == account_id
            and resource_id in {operation.get("resource_id"), operation.get("remote_resource_id")}
            and operation.get("state") != "completed"
        ]

    def _update_pending_operation(self, operation: dict[str, Any], **updates: object) -> None:
        candidate = copy.deepcopy(operation)
        candidate.update(updates)
        candidate["updated_at"] = _utc_now_iso()
        operation.clear()
        operation.update(_pending_operation(candidate))

    def _replace_resource_preview(self, index: int, resource_id: str, changes: Mapping[str, Any]) -> None:
        account = copy.deepcopy(self._accounts[index])
        resources = account.get("resources", [])
        replaced = False
        for item_index, item in enumerate(resources):
            if item.get("id") != resource_id:
                continue
            candidate = dict(item)
            if "name" in changes:
                candidate["name"] = changes["name"]
                candidate["api_name"] = changes["name"]
            if "group_id" in changes:
                candidate["group_id"] = changes["group_id"]
                selected_group = next(
                    (
                        group
                        for group in account.get("groups", [])
                        if isinstance(group, Mapping) and group.get("id") == changes["group_id"]
                    ),
                    None,
                )
                candidate["group_name"] = (
                    str(selected_group.get("name", changes["group_id"]))
                    if isinstance(selected_group, Mapping)
                    else str(changes["group_id"])
                )
            if "enabled" in changes:
                candidate["enabled"] = changes["enabled"]
            resources[item_index] = _safe_resource(candidate)
            replaced = True
            break
        if not replaced:
            raise RelayAccountsError("The selected relay API resource is unavailable")
        account["resources"] = resources
        self._accounts[index] = _private_account(account)

    def _remove_account_local(self, account_id: object, *, dependency_policy: object) -> dict[str, Any]:
        index = self._index(account_id)
        account = self._accounts[index]
        policy = _dependency_policy(dependency_policy)
        resources = [
            {
                "station_id": str(account.get("station_id", "")),
                "account_id": str(account.get("id", "")),
                "resource_id": str(resource.get("id", "")),
            }
            for resource in account.get("resources", [])
            if isinstance(resource, Mapping)
        ]
        self._pending_operations = [
            operation for operation in self._pending_operations if operation.get("account_id") != account["id"]
        ]
        self._pending_credential_cleanups = [
            cleanup
            for cleanup in self._pending_credential_cleanups
            if cleanup["account_id"] != account["id"]
        ]
        self._retain_pending_cleanup(account_id=account["id"], label=account["label"], kind="credentials")
        station_id = str(account.get("station_id", ""))
        self._accounts.pop(index)
        self._session_secrets.pop(account["id"], None)
        self._clear_resource_cache(account["id"])
        if station_id and self._station_account_count(station_id) == 0:
            self._stations = [item for item in self._stations if item["id"] != station_id]
        self._draft_staged = True
        return {
            "kind": "account_remove",
            "account_id": account["id"],
            "station_id": station_id,
            "dependency_policy": policy,
            "resources": resources,
        }

    def _remove_station_local(self, station_id: object, *, dependency_policy: object) -> dict[str, Any]:
        station = self._stations[self._station_index(station_id)]
        policy = _dependency_policy(dependency_policy)
        account_ids = [
            str(account["id"])
            for account in self._accounts
            if account.get("station_id") == station["id"]
        ]
        resources: list[dict[str, str]] = []
        for account_id in account_ids:
            details = self._remove_account_local(account_id, dependency_policy=policy)
            resources.extend(details["resources"])
        self._stations = [item for item in self._stations if item["id"] != station["id"]]
        self._draft_staged = True
        return {
            "kind": "station_remove",
            "station_id": station["id"],
            "account_ids": account_ids,
            "dependency_policy": policy,
            "resources": resources,
        }

    def _removal_preview(self, kind: str, data: Mapping[str, Any]) -> dict[str, Any]:
        if kind == "station":
            station = self._stations[self._station_index(data.get("id", data.get("station_id")))]
            accounts = [account for account in self._accounts if account.get("station_id") == station["id"]]
            policies = ["delete_models", "detach", "rebind"]
        elif kind == "account":
            account = self._accounts[self._index(data.get("id", data.get("account_id")))]
            station = None
            accounts = [account]
            policies = ["delete_models", "detach", "rebind"]
        elif kind == "api_key":
            account = self._accounts[self._index(data.get("account_id", data.get("id")))]
            resource = self._selected_resource(account, data.get("resource_id", data.get("key_id")))
            binding = self._resource_binding_summary(
                str(account.get("station_id", "")),
                str(account.get("id", "")),
                str(resource.get("id", "")),
            )
            return {
                "kind": "api_key_delete_preview",
                "account_id": account["id"],
                "station_id": account["station_id"],
                "resource_id": resource["id"],
                "linked_model_count": int(binding.get("linked_model_count", 0)),
                "dependency_policies": ["delete_models", "rebind", "detach_disabled", "detach"],
            }
        else:
            raise RelayAccountsError("Relay removal preview is invalid")
        resources: list[dict[str, Any]] = []
        linked_model_count = 0
        for account in accounts:
            for resource in account.get("resources", []):
                if not isinstance(resource, Mapping):
                    continue
                binding = self._resource_binding_summary(
                    str(account.get("station_id", "")),
                    str(account.get("id", "")),
                    str(resource.get("id", "")),
                )
                count = int(binding.get("linked_model_count", 0))
                linked_model_count += count
                resources.append(
                    {
                        "station_id": str(account.get("station_id", "")),
                        "account_id": str(account.get("id", "")),
                        "resource_id": str(resource.get("id", "")),
                        "linked_model_count": count,
                    }
                )
        result = {
            "kind": f"{kind}_remove_preview",
            "account_count": len(accounts),
            "resource_count": len(resources),
            "linked_model_count": linked_model_count,
            "dependency_policies": policies,
            "resources": resources,
        }
        if station is not None:
            result["station_id"] = station["id"]
        else:
            result["account_id"] = accounts[0]["id"]
            result["station_id"] = accounts[0]["station_id"]
        return result

    def dispatch(self, action: str, payload: object | None = None) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            data: dict[str, Any] = {}
        else:
            data = dict(payload)
        if _contains_secret(data):
            raise RelayAccountsError("Relay credentials require a trusted native capability")
        name = str(action).strip().lower().replace("-", "_").replace(".", "_")
        if name in {"detect_type", "account_detect_type", "relay_detect_type"}:
            if set(data) != {"origin"}:
                raise RelayAccountsError("Relay detection input is invalid")
            return self.detect_type(data.get("origin"))
        details: dict[str, Any] = {}
        if name in {"station_remove_preview", "relay_station_remove_preview"}:
            details = self._removal_preview("station", data)
        elif name in {"account_remove_preview", "relay_account_remove_preview"}:
            details = self._removal_preview("account", data)
        elif name in {"api_key_delete_preview", "relay_api_key_delete_preview"}:
            details = self._removal_preview("api_key", data)
        elif name in {"station_add", "relay_station_add", "add_station"}:
            station = self._new_station(
                name=data.get("name", data.get("station_name", data.get("label", ""))),
                origin=data.get("origin", data.get("base_url")),
                station_type=data.get("type", data.get("station_type", "newapi")),
            )
            self._stations.append(station)
            self._draft_staged = True
            details = {"kind": "station_add", "station_id": station["id"]}
        elif name in {"add", "account_add", "relay_add"}:
            if len(self._accounts) >= MAX_ACCOUNTS:
                raise RelayAccountsError("Relay account limit reached")
            account_type = str(data.get("type", ""))
            if account_type not in ACCOUNT_TYPES:
                raise RelayAccountsError("Relay account type is invalid")
            station_ids_before = {station["id"] for station in self._stations}
            station, account_origin = self._account_station(data, account_type)
            label = data.get("label", data.get("username", station["name"]))
            try:
                account = _private_account(
                    {
                        "id": uuid.uuid4().hex,
                        "station_id": station["id"],
                        "type": account_type,
                        "label": label,
                        "origin": account_origin,
                        "username": data.get("username", ""),
                        "login_status": "signed_out",
                        "remember_password": data.get("remember_password") is True,
                        "password": "",
                        "session": {},
                        "balance": None,
                        "last_updated_at": "",
                        "resource_status": "idle",
                        "resource_error": "none",
                        "resources": [],
                        "groups": [],
                    }
                )
            except Exception:
                if station["id"] not in station_ids_before:
                    self._stations = [item for item in self._stations if item["id"] != station["id"]]
                raise
            self._accounts.append(account)
            self._draft_staged = True
            details = {"kind": "account_add", "account_id": account["id"], "station_id": station["id"]}
        elif name in {"station_update", "relay_station_update", "update_station"}:
            station_id = _station_id(data.get("id", data.get("station_id")))
            self._update_station(data)
            self._draft_staged = True
            details = {"kind": "station_update", "station_id": station_id}
        elif name in {"station_remove", "relay_station_remove", "remove_station", "station_delete"}:
            details = self._remove_station_local(
                data.get("id", data.get("station_id")),
                dependency_policy=data.get("dependency_policy", data.get("policy", "detach")),
            )
        elif name in {"api_key_create", "relay_api_key_create", "account_api_key_create"}:
            self.create_api_key(
                str(data.get("account_id", data.get("id", ""))),
                data.get("name"),
                group_id=data.get("group_id"),
                enabled=data.get("enabled", True),
            )
            details = {"kind": "api_key_create", "account_id": str(data.get("account_id", data.get("id", "")))}
        elif name in {"api_key_update", "relay_api_key_update", "account_api_key_update"}:
            self.update_api_key(
                str(data.get("account_id", data.get("id", ""))),
                data.get("resource_id", data.get("key_id")),
                data.get("name"),
            )
            details = {"kind": "api_key_update", "account_id": str(data.get("account_id", data.get("id", ""))), "resource_id": str(data.get("resource_id", data.get("key_id", "")))}
        elif name in {"api_key_set_enabled", "relay_api_key_set_enabled", "account_api_key_set_enabled"}:
            self.set_api_key_enabled(
                str(data.get("account_id", data.get("id", ""))),
                data.get("resource_id", data.get("key_id")),
                data.get("enabled"),
            )
            details = {"kind": "api_key_set_enabled", "account_id": str(data.get("account_id", data.get("id", ""))), "resource_id": str(data.get("resource_id", data.get("key_id", "")))}
        elif name in {"api_key_set_group", "relay_api_key_set_group", "account_api_key_set_group"}:
            self.set_api_key_group(
                str(data.get("account_id", data.get("id", ""))),
                data.get("resource_id", data.get("key_id")),
                data.get("group_id"),
            )
            details = {"kind": "api_key_set_group", "account_id": str(data.get("account_id", data.get("id", ""))), "resource_id": str(data.get("resource_id", data.get("key_id", "")))}
        elif name in {"api_key_delete", "relay_api_key_delete", "account_api_key_delete"}:
            account_id = str(data.get("account_id", data.get("id", "")))
            resource_id = _resource_id(data.get("resource_id", data.get("key_id")))
            self.delete_api_key(
                account_id,
                resource_id,
                dependency_policy=data.get("dependency_policy", data.get("policy", "delete_models")),
            )
            account = self._accounts[self._index(account_id)]
            details = {
                "kind": "api_key_delete",
                "account_id": account_id,
                "station_id": account["station_id"],
                "resource_id": resource_id,
                "dependency_policy": _dependency_policy(
                    data.get("dependency_policy", data.get("policy", "delete_models")),
                    default="delete_models",
                ),
                "resources": [
                    {
                        "station_id": account["station_id"],
                        "account_id": account_id,
                        "resource_id": resource_id,
                    }
                ],
            }
        elif name in {"api_key_detach", "relay_api_key_detach", "account_api_key_detach"}:
            account_id = str(data.get("account_id", data.get("id", "")))
            resource_id = _resource_id(data.get("resource_id", data.get("key_id")))
            self.detach_api_key(
                account_id,
                resource_id,
            )
            account = self._accounts[self._index(account_id)]
            details = {
                "kind": "api_key_detach",
                "account_id": account_id,
                "station_id": account["station_id"],
                "resource_id": resource_id,
                "dependency_policy": "detach",
                "resources": [
                    {
                        "station_id": account["station_id"],
                        "account_id": account_id,
                        "resource_id": resource_id,
                    }
                ],
            }
        elif name in {"delete", "account_delete", "account_remove", "relay_delete", "relay_account_remove", "remove_account"}:
            details = self._remove_account_local(
                data.get("id", data.get("account_id")),
                dependency_policy=data.get("dependency_policy", data.get("policy", "detach")),
            )
        elif name in {
            "credential_cleanup_confirm",
            "credential_cleanup_confirmed",
            "relay_credential_cleanup_confirm",
        }:
            account_id = _account_id(data.get("id", data.get("account_id")))
            kind = str(data.get("kind", ""))
            if kind not in PENDING_CLEANUP_KINDS:
                raise RelayAccountsError("Relay cleanup is invalid")
            self._pending_credential_cleanups = [
                cleanup
                for cleanup in self._pending_credential_cleanups
                if not (cleanup["account_id"] == account_id and cleanup["kind"] == kind)
            ]
            details = {"kind": "credential_cleanup_confirm", "account_id": account_id}
        elif name in {"update", "account_update", "relay_update"}:
            index = self._index(data.get("id"))
            current = copy.deepcopy(self._accounts[index])
            for key in ("label", "username"):
                if key in data:
                    current[key] = data[key]
            label_value = current.get("label")
            username_value = current.get("username")
            if "station_id" in data:
                station = self._stations[self._station_index(data.get("station_id"))]
                self._move_account_to_station(index, station)
                current = copy.deepcopy(self._accounts[index])
            elif "origin" in data or "base_url" in data:
                origin = _origin(data.get("origin", data.get("base_url")))
                station_index = self._station_for_origin(origin)
                if station_index is None:
                    station = self._new_station(
                        name=self._stations[self._station_index(current.get("station_id"))]["name"] if current.get("station_id") else current["label"],
                        origin=origin,
                        station_type=current["type"],
                    )
                    self._stations.append(station)
                else:
                    station = self._stations[station_index]
                self._move_account_to_station(index, station)
                current = copy.deepcopy(self._accounts[index])
            if "label" in data:
                current["label"] = label_value
            if "username" in data:
                current["username"] = username_value
            if "remember_password" in data:
                current["remember_password"] = data["remember_password"] is True
                if not current["remember_password"]:
                    current["password"] = ""
                    current["session"] = {}
                    self._session_secrets.pop(current["id"], None)
            self._accounts[index] = _private_account(current)
            self._draft_staged = True
            details = {"kind": "account_update", "account_id": current["id"]}
        else:
            raise RelayAccountsError("The requested relay action is unavailable")
        self._last_action = details
        self._persist()
        self.revision += 1
        return self.snapshot()

    @staticmethod
    def _apply_issue(
        code: str,
        *,
        account_id: str = "",
        resource_id: str = "",
    ) -> dict[str, str]:
        """Create a stable no-secret issue suitable for IPC projection."""

        result = {"code": code, "message": "Relay Apply requires attention"}
        if account_id:
            result["account_id"] = account_id
        if resource_id:
            result["resource_id"] = resource_id
        return result

    def _operation_account(self, operation: Mapping[str, Any]) -> tuple[int, dict[str, Any]] | None:
        account_id = str(operation.get("account_id", ""))
        try:
            index = self._index(account_id)
        except RelayAccountsError:
            return None
        account = self._accounts[index]
        if account.get("station_id") != operation.get("station_id"):
            return None
        return index, account

    def prepare_apply(self, payload: object | None = None) -> dict[str, Any]:
        """Validate the draft without writing disk or issuing HTTP requests."""

        del payload
        issues: list[dict[str, str]] = []
        if self._read_storage_bytes() != self._baseline_bytes:
            issues.append(self._apply_issue("external_disk_changed"))
        for operation in self._pending_operations:
            if operation.get("state") in {"completed"}:
                continue
            account_result = self._operation_account(operation)
            account_id = str(operation.get("account_id", ""))
            resource_id = str(operation.get("resource_id", ""))
            if account_result is None:
                issues.append(self._apply_issue("account_unavailable", account_id=account_id, resource_id=resource_id))
                continue
            _, account = account_result
            if account.get("login_status") != "signed_in":
                issues.append(self._apply_issue("login_required", account_id=account_id, resource_id=resource_id))
                continue
            if operation.get("kind") != "api_key_create":
                try:
                    self._selected_resource(account, resource_id)
                except RelayAccountsError:
                    issues.append(self._apply_issue("resource_unavailable", account_id=account_id, resource_id=resource_id))
            if operation.get("kind") == "api_key_set_group":
                group_id = str(operation.get("changes", {}).get("group_id", ""))
                if group_id not in {
                    str(group.get("id", ""))
                    for group in account.get("groups", [])
                    if isinstance(group, Mapping)
                }:
                    issues.append(self._apply_issue("group_unavailable", account_id=account_id, resource_id=resource_id))
        operations = [_public_pending_operation(operation) for operation in self._pending_operations]
        non_destructive = [operation for operation in operations if operation["kind"] != "api_key_delete"]
        destructive = [operation for operation in operations if operation["kind"] == "api_key_delete"]
        return {
            "ready": not issues,
            "status": "applied" if not issues else "failed",
            "operations": operations,
            "non_destructive": non_destructive,
            "destructive": destructive,
            "completed_operations": 0,
            "pending_operations": len(operations),
            "issues": issues,
        }

    def validate(self, payload: object | None = None) -> dict[str, Any]:
        prepared = self.prepare_apply(payload)
        return {"valid": prepared["ready"], "issues": prepared["issues"]}

    def _mark_operation_uncertain(self, operation: dict[str, Any]) -> None:
        """Retain an ambiguous remote result for read-only reconciliation.

        A transport failure can occur after the relay accepted a mutation.  A
        later Apply must refresh the stable resource before it ever retries a
        create or delete, rather than treating the exception as proof that no
        remote work happened.
        """

        self._update_pending_operation(
            operation,
            state="local_pending",
            error="Relay API key operation requires reconciliation",
        )
        self._persist_journal()

    @staticmethod
    def _response_resource_id(account: Mapping[str, Any], payload: object) -> str | None:
        value = _json_data(payload)
        if not isinstance(value, Mapping):
            return None
        raw_id = value.get("id", value.get("key_id"))
        if account.get("type") == "newapi":
            if type(raw_id) is int:
                return f"newapi-{raw_id}"
            return None
        if isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool):
            candidate = str(raw_id).strip()
            if candidate:
                return f"sub2api-{candidate}"
        return None

    def _execute_create_operation(self, account: Mapping[str, Any], operation: dict[str, Any]) -> None:
        changes = operation.get("changes", {})
        key_name = self._api_key_name(changes.get("name"))
        if account["type"] == "newapi":
            payload = self._api_key_request(
                account,
                method="post",
                path="/api/token/",
                body={"name": key_name, "unlimited_quota": True},
            )
        else:
            payload = self._api_key_request(
                account,
                method="post",
                path="/api/v1/keys",
                body={"name": key_name},
            )
        updates: dict[str, Any] = {"state": "remote_applied"}
        remote_resource_id = self._response_resource_id(account, payload)
        if remote_resource_id:
            updates["remote_resource_id"] = remote_resource_id
        self._update_pending_operation(operation, **updates)
        self._persist_journal()

    def _execute_existing_operation(self, account: Mapping[str, Any], operation: dict[str, Any]) -> None:
        resource_id = str(operation.get("resource_id", ""))
        remote_id = self._api_key_remote_id(account, resource_id)
        changes = operation.get("changes", {})
        kind = operation.get("kind")
        if kind == "api_key_update":
            key_name = self._api_key_name(changes.get("name"))
            if account["type"] == "newapi":
                self._set_newapi_key_metadata(account, remote_id, {"name": key_name})
            else:
                self._api_key_request(
                    account,
                    method="put",
                    path=f"/api/v1/keys/{quote(remote_id, safe='')}",
                    body={"name": key_name},
                )
        elif kind == "api_key_set_group":
            group_id = _group_id(changes.get("group_id"), required=account["type"] == "sub2api")
            if account["type"] == "newapi":
                self._set_newapi_key_metadata(account, remote_id, {"group": group_id})
            else:
                try:
                    upstream_group_id = int(group_id)
                except ValueError:
                    raise RelayAccountsError("Relay API group is invalid") from None
                self._api_key_request(
                    account,
                    method="put",
                    path=f"/api/v1/keys/{quote(remote_id, safe='')}",
                    body={"group_id": upstream_group_id},
                )
        elif kind == "api_key_set_enabled":
            enabled = changes.get("enabled")
            if not isinstance(enabled, bool):
                raise RelayAccountsError("Relay API key status is invalid")
            if account["type"] == "newapi":
                self._api_key_request(
                    account,
                    method="put",
                    path="/api/token/?status_only=true",
                    body={"id": int(remote_id), "status": 1 if enabled else 2},
                )
            else:
                self._api_key_request(
                    account,
                    method="put",
                    path=f"/api/v1/keys/{quote(remote_id, safe='')}",
                    body={"status": "active" if enabled else "inactive"},
                )
        elif kind == "api_key_delete":
            if account["type"] == "newapi":
                path = f"/api/token/{int(remote_id)}"
            else:
                path = f"/api/v1/keys/{quote(remote_id, safe='')}"
            self._api_key_request(account, method="delete", path=path)
            self._resource_secret_cache.pop(self._resource_cache_key(account["id"], resource_id), None)
        else:
            raise RelayAccountsError("The requested relay operation is unavailable")
        self._update_pending_operation(operation, state="remote_applied")
        self._persist_journal()

    def _apply_create_metadata(self, account: Mapping[str, Any], operation: dict[str, Any]) -> None:
        changes = operation.get("changes", {})
        resource_id = str(operation.get("resource_id", ""))
        remote_id = self._api_key_remote_id(account, resource_id)
        resource = self._selected_resource(account, resource_id)
        name = changes.get("name")
        if isinstance(name, str) and resource.get("name") != name:
            if account["type"] == "newapi":
                self._set_newapi_key_metadata(account, remote_id, {"name": name})
            else:
                self._api_key_request(
                    account,
                    method="put",
                    path=f"/api/v1/keys/{quote(remote_id, safe='')}",
                    body={"name": name},
                )
        group_id = changes.get("group_id")
        if group_id:
            if account["type"] == "newapi":
                self._set_newapi_key_metadata(account, remote_id, {"group": _group_id(group_id)})
            else:
                try:
                    upstream_group_id = int(_group_id(group_id, required=True))
                except ValueError:
                    raise RelayAccountsError("Relay API group is invalid") from None
                self._api_key_request(
                    account,
                    method="put",
                    path=f"/api/v1/keys/{quote(remote_id, safe='')}",
                    body={"group_id": upstream_group_id},
                )
        if changes.get("enabled") is False:
            if account["type"] == "newapi":
                self._api_key_request(
                    account,
                    method="put",
                    path="/api/token/?status_only=true",
                    body={"id": int(remote_id), "status": 2},
                )
            else:
                self._api_key_request(
                    account,
                    method="put",
                    path=f"/api/v1/keys/{quote(remote_id, safe='')}",
                    body={"status": "inactive"},
                )

    @staticmethod
    def _resource_matches_changes(resource: Mapping[str, Any], changes: Mapping[str, Any]) -> bool:
        """Return whether a refreshed resource proves an operation applied."""

        if "name" in changes and resource.get("name") != changes.get("name"):
            return False
        if "group_id" in changes and str(resource.get("group_id", "")) != str(changes.get("group_id", "")):
            return False
        if "enabled" in changes and resource.get("enabled") is not changes.get("enabled"):
            return False
        return True

    def execute_pending_operations(
        self,
        prepared: Mapping[str, Any] | None = None,
        *,
        phase: str = "non_destructive",
    ) -> dict[str, Any]:
        """Execute journaled remote writes. ``prepare_apply`` itself has none."""

        if phase not in {"non_destructive", "destructive"}:
            raise RelayAccountsError("Relay Apply phase is invalid")
        plan = dict(prepared) if isinstance(prepared, Mapping) else self.prepare_apply()
        if plan.get("ready") is not True:
            return {
                "status": "failed",
                "completed_operations": 0,
                "pending_operations": len(self._pending_operations),
                "issues": list(plan.get("issues", [])),
            }
        selected_kinds = {"api_key_delete"} if phase == "destructive" else set(PENDING_OPERATION_KINDS).difference({"api_key_delete"})
        candidates = [
            operation
            for operation in self._pending_operations
            if operation.get("kind") in selected_kinds and operation.get("state") == "staged"
        ]
        if not candidates:
            return {
                "status": "applied",
                "completed_operations": 0,
                "pending_operations": len(self._pending_operations),
                "issues": [],
            }
        # Persist stable identifiers and staged state before the first remote
        # side effect. The journal intentionally excludes all credentials.
        self._persist_journal()
        issues: list[dict[str, str]] = []
        completed = 0
        creates = [operation for operation in candidates if operation.get("kind") == "api_key_create"]
        others = [operation for operation in candidates if operation.get("kind") != "api_key_create"]
        for operation in [*creates, *others]:
            account_result = self._operation_account(operation)
            account_id = str(operation.get("account_id", ""))
            resource_id = str(operation.get("resource_id", ""))
            if account_result is None:
                issues.append(self._apply_issue("account_unavailable", account_id=account_id, resource_id=resource_id))
                self._mark_operation_uncertain(operation)
                continue
            _, account = account_result
            try:
                if operation.get("kind") == "api_key_create":
                    # A previously reconciled create has a stable resource
                    # ID.  Retry only its remaining metadata, never POST a
                    # second remote key with the same display name.
                    if operation.get("remote_resource_id") or not str(operation.get("resource_id", "")).startswith("pending-"):
                        self._apply_create_metadata(account, operation)
                        self._update_pending_operation(operation, state="remote_applied", error="")
                        self._persist_journal()
                    else:
                        self._execute_create_operation(account, operation)
                else:
                    self._execute_existing_operation(account, operation)
            except Exception:
                issues.append(self._apply_issue("remote_operation_failed", account_id=account_id, resource_id=resource_id))
                self._mark_operation_uncertain(operation)
                continue
            completed += 1
        if phase == "non_destructive":
            for operation in creates:
                if operation.get("state") != "remote_applied":
                    continue
                account_result = self._operation_account(operation)
                account_id = str(operation.get("account_id", ""))
                if account_result is None:
                    issues.append(self._apply_issue("account_unavailable", account_id=account_id))
                    self._mark_operation_uncertain(operation)
                    continue
                _, account = account_result
                try:
                    self.refresh_resources(account_id, _for_apply=True)
                    account_result = self._operation_account(operation)
                    if account_result is None or not self._reconcile_created_resource(account_result[1], operation):
                        self._update_pending_operation(
                            operation,
                            state="local_pending",
                            error="Created relay API key requires reconciliation",
                        )
                        issues.append(self._apply_issue("created_resource_unresolved", account_id=account_id, resource_id=str(operation.get("resource_id", ""))))
                        continue
                    account = account_result[1]
                    self._apply_create_metadata(account, operation)
                    self._update_pending_operation(operation, state="remote_applied", error="")
                    self._persist_journal()
                except Exception:
                    issues.append(self._apply_issue("remote_operation_failed", account_id=account_id, resource_id=str(operation.get("resource_id", ""))))
                    self._mark_operation_uncertain(operation)
        return {
            "status": "partial" if issues else "applied",
            "completed_operations": completed,
            "pending_operations": len(self._pending_operations),
            "issues": issues,
        }

    def _reconcile_created_resource(self, account: Mapping[str, Any], operation: dict[str, Any]) -> bool:
        resources = account.get("resources", [])
        if not isinstance(resources, list):
            return False
        remote_id = operation.get("remote_resource_id")
        candidate: Mapping[str, Any] | None = None
        if isinstance(remote_id, str):
            candidate = next(
                (resource for resource in resources if isinstance(resource, Mapping) and resource.get("id") == remote_id),
                None,
            )
        if candidate is None:
            known_ids = set(operation.get("known_resource_ids", []))
            desired_name = str(operation.get("changes", {}).get("name", ""))
            candidates = [
                resource
                for resource in resources
                if isinstance(resource, Mapping)
                and resource.get("id") not in known_ids
                and resource.get("name") == desired_name
            ]
            if len(candidates) == 1:
                candidate = candidates[0]
        if candidate is None:
            return False
        resolved_id = _resource_id(candidate.get("id"))
        self._update_pending_operation(
            operation,
            resource_id=resolved_id,
            remote_resource_id=resolved_id,
            state="remote_applied",
        )
        return True

    def reconcile_apply(
        self,
        prepared: Mapping[str, Any] | None = None,
        *,
        phase: str = "non_destructive",
    ) -> dict[str, Any]:
        """Refresh remote facts and reconcile journal IDs without remote writes."""

        del prepared
        if phase not in {"non_destructive", "destructive"}:
            raise RelayAccountsError("Relay Apply phase is invalid")
        wanted_kinds = {"api_key_delete"} if phase == "destructive" else set(PENDING_OPERATION_KINDS).difference({"api_key_delete"})
        affected_account_ids = list(dict.fromkeys(
            str(operation.get("account_id", ""))
            for operation in self._pending_operations
            if operation.get("kind") in wanted_kinds and operation.get("state") in {"remote_applied", "local_pending"}
        ))
        issues: list[dict[str, str]] = []
        for account_id in affected_account_ids:
            try:
                public = self.refresh_resources(account_id, _for_apply=True)
            except Exception:
                issues.append(self._apply_issue("refresh_failed", account_id=account_id))
                continue
            account_result = self._operation_account({
                "account_id": account_id,
                "station_id": next(
                    (operation.get("station_id") for operation in self._pending_operations if operation.get("account_id") == account_id),
                    "",
                ),
            })
            if account_result is None:
                issues.append(self._apply_issue("account_unavailable", account_id=account_id))
                continue
            account_index, account = account_result
            del public
            for operation in self._pending_operations:
                if operation.get("account_id") != account_id or operation.get("kind") not in wanted_kinds:
                    continue
                if operation.get("state") not in {"remote_applied", "local_pending", "failed"}:
                    continue
                if operation.get("kind") == "api_key_create":
                    if not self._reconcile_created_resource(account, operation):
                        issues.append(self._apply_issue("created_resource_unresolved", account_id=account_id, resource_id=str(operation.get("resource_id", ""))))
                        self._update_pending_operation(
                            operation,
                            # A lost create response may have allocated an
                            # unknown key.  Without a stable remote ID (or a
                            # unique refreshed match) replaying POST would
                            # risk creating a duplicate, so keep it pending
                            # for another read instead of guessing.
                            state="local_pending",
                            error="Created relay API key requires reconciliation",
                        )
                        continue
                    resource_id = str(operation.get("resource_id", ""))
                    resource = self._selected_resource(account, resource_id)
                    if not self._resource_matches_changes(resource, operation.get("changes", {})):
                        issues.append(self._apply_issue("resource_change_not_applied", account_id=account_id, resource_id=resource_id))
                        self._update_pending_operation(
                            operation,
                            state="staged",
                            error="Relay API key change requires retry",
                        )
                        continue
                    self._replace_resource_preview(account_index, resource_id, operation.get("changes", {}))
                    account = self._accounts[account_index]
                if operation.get("kind") == "api_key_delete":
                    resource_still_present = any(
                        isinstance(resource, Mapping) and resource.get("id") == operation.get("resource_id")
                        for resource in account.get("resources", [])
                    )
                    if resource_still_present:
                        issues.append(self._apply_issue("deleted_resource_still_present", account_id=account_id, resource_id=str(operation.get("resource_id", ""))))
                        self._update_pending_operation(
                            operation,
                            state="staged",
                            error="Relay API key deletion requires retry",
                        )
                        continue
                elif operation.get("kind") != "api_key_create":
                    try:
                        resource = self._selected_resource(account, str(operation.get("resource_id", "")))
                    except RelayAccountsError:
                        issues.append(self._apply_issue("resource_unavailable", account_id=account_id, resource_id=str(operation.get("resource_id", ""))))
                        self._update_pending_operation(
                            operation,
                            state="staged",
                            error="Relay API key change requires retry",
                        )
                        continue
                    if not self._resource_matches_changes(resource, operation.get("changes", {})):
                        issues.append(self._apply_issue("resource_change_not_applied", account_id=account_id, resource_id=str(operation.get("resource_id", ""))))
                        # Keep the user's staged value visible while the
                        # journal truthfully reports that the remote fact is
                        # still awaiting retry.
                        self._replace_resource_preview(
                            account_index,
                            str(operation.get("resource_id", "")),
                            operation.get("changes", {}),
                        )
                        account = self._accounts[account_index]
                        self._update_pending_operation(
                            operation,
                            state="staged",
                            error="Relay API key change requires retry",
                        )
                        continue
                    self._replace_resource_preview(
                        account_index,
                        str(operation.get("resource_id", "")),
                        operation.get("changes", {}),
                    )
                self._update_pending_operation(operation, state="local_pending", error="")
        self._persist_journal()
        return {
            "status": "partial" if issues else "applied",
            "completed_operations": 0,
            "pending_operations": len(self._pending_operations),
            "issues": issues,
        }

    def commit_apply(self, payload: object | None = None) -> dict[str, Any]:
        """Commit only local relay state after the coordinator materializes keys."""

        del payload
        if self._read_storage_bytes() != self._baseline_bytes:
            raise RelayAccountsError("Relay accounts changed on disk; reload before applying")
        self._persist(force=True)
        self._import_staged = False
        self._draft_staged = any(
            operation.get("state") in {"staged", "failed"}
            for operation in self._pending_operations
        )
        self.revision += 1
        return {
            "status": "partial" if self._pending_operations else "applied",
            "completed_operations": 0,
            "pending_operations": len(self._pending_operations),
            "issues": [],
        }

    def finalize_apply(self, payload: object | None = None) -> dict[str, Any]:
        """Finalize only operations whose remote and local work both completed."""

        del payload
        retained: list[dict[str, Any]] = []
        completed = 0
        has_completed = False
        for operation in self._pending_operations:
            if operation.get("state") == "local_pending" and not operation.get("error"):
                completed += 1
                has_completed = True
                continue
            retained.append(operation)
        if has_completed:
            if self._read_storage_bytes() != self._baseline_bytes:
                raise RelayAccountsError("Relay accounts changed on disk; reload before applying")
            self._persist(force=True)
        self._pending_operations = retained
        self._persist_journal()
        self._draft_staged = bool(retained) or self._import_staged
        self.revision += 1
        return {
            "status": "partial" if retained else "applied",
            "completed_operations": completed,
            "pending_operations": len(retained),
            "issues": [],
        }

    def apply(self, payload: object | None = None) -> dict[str, Any]:
        """Compatibility Apply path when no cross-domain coordinator is present."""

        prepared = self.prepare_apply(payload)
        if not prepared["ready"]:
            raise RelayAccountsError("Relay Apply is not ready")
        first = self.execute_pending_operations(prepared, phase="non_destructive")
        reconciled = self.reconcile_apply(prepared, phase="non_destructive")
        committed = self.commit_apply()
        deleted = self.execute_pending_operations(prepared, phase="destructive")
        delete_reconciled = self.reconcile_apply(prepared, phase="destructive")
        finalized = self.finalize_apply()
        issues = [
            *([] if not reconciled["issues"] else first["issues"]),
            *reconciled["issues"],
            *committed["issues"],
            *([] if not delete_reconciled["issues"] else deleted["issues"]),
            *delete_reconciled["issues"],
            *finalized["issues"],
        ]
        return {
            "status": "partial" if issues or finalized["pending_operations"] else "applied",
            "completed_operations": sum(
                int(result["completed_operations"])
                for result in (first, reconciled, committed, deleted, delete_reconciled, finalized)
            ),
            "pending_operations": int(finalized["pending_operations"]),
            "issues": issues,
        }

    def export(self, *, include_sensitive: bool = False) -> dict[str, Any]:
        """Export a package-ready durable relay document.

        ``include_sensitive`` is used exclusively when Core writes a selected
        local package. The normal result intentionally stays equivalent to the
        public snapshot, so account passwords and browser sessions never cross
        the IPC boundary.
        """

        if include_sensitive:
            return {"domain": self.name, "storage": self._stored_payload()}
        return self.snapshot()

    def import_package(self, payload: object) -> None:
        """Stage a complete relay-store replacement without writing to disk."""

        if not isinstance(payload, Mapping):
            raise RelayAccountsError("Relay account package is invalid")
        data = dict(payload)
        domain = data.get("domain")
        if domain is not None and domain != self.name:
            raise RelayAccountsError("Relay account package is invalid")
        if set(data).difference({"domain", "storage"}) or not isinstance(data.get("storage"), Mapping):
            raise RelayAccountsError("Relay account package is invalid")
        # Validate and replace only after parsing the entire durable document.
        self._replace_storage_state(dict(data["storage"]))
        self._import_staged = True
        self.revision += 1

    def secret_present(self, field: str, target: str | None = None) -> bool:
        if target is None:
            raise RelayAccountsError("The requested relay credential is unavailable")
        if field == "api_key":
            account_id, resource_id = self._secret_target(target)
            account = self._accounts[self._index(account_id)]
            resource = self._selected_resource(account, resource_id)
            return bool(resource.get("key_hint"))
        if field != "session":
            raise RelayAccountsError("The requested relay credential is unavailable")
        self._index(target)
        secrets = self._session_secrets.get(target, {})
        return bool(secrets.get("cookie") or secrets.get("access_token"))

    def stage_secret(self, field: str, target: str | None, value: str) -> None:
        """Accept an opaque native browser credential payload into memory only."""

        if field != "session" or target is None:
            raise RelayAccountsError("The requested relay credential is unavailable")
        index = self._index(target)
        account = copy.deepcopy(self._accounts[index])
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            raise RelayAccountsError("Relay login result is invalid") from None
        if not isinstance(payload, Mapping) or set(payload).difference({"username", "cookie", "access_token", "refresh_token"}):
            raise RelayAccountsError("Relay login result is invalid")
        username = _text(payload.get("username"), "Relay username", limit=320)
        cookie = payload.get("cookie", "")
        token = payload.get("access_token", "")
        if not isinstance(cookie, str) or not isinstance(token, str) or not (cookie or token):
            raise RelayAccountsError("Relay login result is incomplete")
        self._session_secrets[target] = {
            key: _text(payload.get(key), "Relay login result", limit=32768)
            for key in ("cookie", "access_token", "refresh_token")
            if isinstance(payload.get(key), str) and payload.get(key)
        }
        account["username"] = username
        account["login_status"] = "signed_in"
        account["last_updated_at"] = _utc_now_iso()
        account["resource_status"] = "idle"
        account["resource_error"] = "none"
        account["resources"] = []
        account["groups"] = []
        self._accounts[index] = _private_account(account)
        self._persist()
        self.revision += 1

    def trusted_secret_value(self, field: str, target: str | None = None) -> str:
        """Read one API key only for a native, one-time plaintext lease."""

        if field != "api_key" or target is None:
            raise RelayAccountsError("The requested relay credential is unavailable")
        account_id, resource_id = self._secret_target(target)
        account = self._accounts[self._index(account_id)]
        if account["login_status"] != "signed_in":
            raise RelayAccountsError("Relay login is unavailable")
        return self._read_key(account, self._selected_resource(account, resource_id))

    def accept_login_result(
        self,
        account_id: str,
        *,
        username: str,
        cookie: str = "",
        access_token: str = "",
        refresh_token: str = "",
        password: str = "",
        preserve_resources: bool = False,
    ) -> dict[str, Any]:
        """Trusted native/browser boundary for one completed login."""

        index = self._index(account_id)
        account = copy.deepcopy(self._accounts[index])
        username_value = _text(username, "Relay username", limit=320)
        # Both supported relay families can authenticate their dashboard API
        # with the browser session cookie.  Some deployments additionally
        # surface an access token in local storage, but treating that token as
        # mandatory makes an otherwise verified multi-account browser session
        # impossible to restore or import.
        required_secret = access_token or cookie
        if not isinstance(required_secret, str) or not required_secret.strip():
            raise RelayAccountsError("Relay login result is incomplete")
        secrets: dict[str, str] = {}
        if cookie:
            secrets["cookie"] = _text(cookie, "Relay cookie", limit=32768)
        if access_token:
            secrets["access_token"] = _text(access_token, "Relay access token", limit=32768)
        if refresh_token:
            secrets["refresh_token"] = _text(refresh_token, "Relay refresh token", limit=32768)
        account.update(
            {
                "username": username_value,
                "login_status": "signed_in",
                "last_updated_at": _utc_now_iso(),
                "resource_status": "idle",
                "resource_error": "none",
            }
        )
        if not preserve_resources:
            account["resources"] = []
            account["groups"] = []
        if account["remember_password"] and password:
            account["password"] = _text(password, "Relay password", limit=4096)
        if account["remember_password"]:
            account["session"] = copy.deepcopy(secrets)
        else:
            account["password"] = ""
            account["session"] = {}
        self._accounts[index] = _private_account(account)
        self._session_secrets[account_id] = secrets
        self._persist()
        self.revision += 1
        return _public_account(self._accounts[index])

    def restore_saved_session(self, account_id: str) -> dict[str, Any] | None:
        """Verify and activate an explicitly remembered browser session."""

        index = self._index(account_id)
        account = copy.deepcopy(self._accounts[index])
        session = account.get("session", {})
        if not account.get("remember_password") or not isinstance(session, Mapping):
            return None
        secrets = {
            key: value
            for key in ("cookie", "access_token", "refresh_token")
            if isinstance((value := session.get(key)), str) and value
        }
        cookie = secrets.get("cookie", "")
        token = secrets.get("access_token", "")
        if not cookie and not token:
            return None
        headers: dict[str, str] = {}
        if cookie:
            headers["Cookie"] = cookie
        if token:
            headers["Authorization"] = f"Bearer {token}"
        path = "/api/user/self" if account["type"] == "newapi" else "/api/v1/auth/me"
        try:
            payload = self._http.json(account["origin"], path, headers=headers)
        except RelayAccountsError:
            return None
        if not isinstance(_json_data(payload), Mapping):
            return None
        self._session_secrets[account_id] = secrets
        account["login_status"] = "signed_in"
        account["last_updated_at"] = _utc_now_iso()
        account["resource_status"] = "idle"
        account["resource_error"] = "none"
        self._accounts[index] = _private_account(account)
        self._persist()
        self.revision += 1
        return _public_account(self._accounts[index])

    def restore_saved_password(self, account_id: str) -> dict[str, Any]:
        """Attempt a non-UI login from an explicitly remembered password."""

        index = self._index(account_id)
        account = copy.deepcopy(self._accounts[index])
        password = account.get("password", "")
        username = account.get("username", "")
        login = getattr(self._http, "password_login", None)
        if (
            not account.get("remember_password")
            or not isinstance(password, str)
            or not password
            or not isinstance(username, str)
            or not username
            or not callable(login)
        ):
            return self.set_login_status(account_id, "signed_out")
        result = login(account["origin"], account["type"], username, password)
        if not isinstance(result, Mapping):
            raise RelayAccountsError("Relay login was rejected")
        return self.accept_login_result(
            account_id,
            username=str(result.get("username", "")),
            cookie=str(result.get("cookie", "")),
            access_token=str(result.get("access_token", "")),
            refresh_token=str(result.get("refresh_token", "")),
            preserve_resources=True,
        )

    def set_login_status(self, account_id: str, status: str) -> dict[str, Any]:
        """Record a native session check that did not yield usable credentials.

        This deliberately accepts only terminal, secret-free outcomes. A
        ``signed_in`` state must still pass through :meth:`accept_login_result`
        so the Core has the verified session in memory for a later relay
        import.
        """

        if status not in {"signed_out", "expired"}:
            raise RelayAccountsError("Relay login status is invalid")
        index = self._index(account_id)
        account = copy.deepcopy(self._accounts[index])
        account["login_status"] = status
        account["last_updated_at"] = _utc_now_iso()
        account["resource_status"] = "idle"
        account["resource_error"] = "none"
        account["resources"] = []
        account["groups"] = []
        self._accounts[index] = _private_account(account)
        self._session_secrets.pop(account_id, None)
        if status == "expired":
            self._accounts[index]["session"] = {}
        self._persist()
        self.revision += 1
        return _public_account(self._accounts[index])

    def _headers(self, account: Mapping[str, Any]) -> dict[str, str]:
        secrets = self._session_secrets.get(str(account.get("id", "")), {})
        cookie = secrets.get("cookie")
        token = secrets.get("access_token")
        headers: dict[str, str] = {}
        if isinstance(cookie, str) and cookie:
            headers["Cookie"] = cookie
        if isinstance(token, str) and token:
            headers["Authorization"] = f"Bearer {token}"
        if not headers:
            raise RelayAccountsError("Relay login is unavailable")
        return headers

    @staticmethod
    def _resource_cache_key(account_id: object, resource_id: object) -> str:
        return f"{_account_id(account_id)}:{_resource_id(resource_id)}"

    def _clear_resource_cache(self, account_id: object) -> None:
        prefix = f"{_account_id(account_id)}:"
        self._resource_secret_cache = {
            key: value for key, value in self._resource_secret_cache.items() if not key.startswith(prefix)
        }

    @staticmethod
    def _secret_target(target: object) -> tuple[str, str]:
        if not isinstance(target, str) or ":" not in target:
            raise RelayAccountsError("The requested relay credential is unavailable")
        account_id, resource_id = target.split(":", 1)
        return _account_id(account_id), _resource_id(resource_id)

    @staticmethod
    def _resource_label(item: Mapping[str, Any], index: int, *, prefix: str) -> tuple[str, str]:
        raw_id = item.get("id", item.get("key_id", item.get("name", index + 1)))
        item_id = str(raw_id).strip() if isinstance(raw_id, (str, int)) else str(index + 1)
        item_id = re.sub(r"[^A-Za-z0-9._:-]", "-", item_id).strip("-") or str(index + 1)
        resource_id = f"{prefix}-{item_id}"[:MAX_RESOURCE_ID]
        name = _resource_name(item.get("name", item.get("label", item.get("id"))), f"API {index + 1}")
        return resource_id, name

    @staticmethod
    def _key_hint(value: object) -> str:
        # A resource can signal that it has a usable credential, but even a
        # masked-looking prefix/suffix is still credential material and must
        # not cross the ordinary Core snapshot boundary.
        return "configured" if isinstance(value, str) and value.strip() else ""

    def _newapi_resources(self, account: Mapping[str, Any]) -> list[dict[str, Any]]:
        headers = self._headers(account)
        models = _model_names(self._http.json(account["origin"], "/api/user/models", headers=headers))
        tokens = _json_data(self._http.json(account["origin"], "/api/token/?p=1&size=100", headers=headers))
        if isinstance(tokens, Mapping):
            tokens = tokens.get("items", [])
        if not isinstance(tokens, Sequence) or isinstance(tokens, (str, bytes, bytearray)):
            raise RelayAccountsError("Relay API key list is invalid")
        resources: list[dict[str, Any]] = []
        for index, item in enumerate(tokens):
            if not isinstance(item, Mapping):
                continue
            token_id = item.get("id")
            if type(token_id) is not int:
                continue
            resource_id, name = self._resource_label(item, index, prefix="newapi")
            group_id = _group_id(item.get("group"))
            resources.append(
                {
                    "id": resource_id,
                    "name": name,
                    "api_base": f"{account['origin'].rstrip('/')}/v1",
                    "key_hint": self._key_hint(item.get("key")),
                    "enabled": _newapi_token_enabled(item.get("status")),
                    "models": models,
                    "group_id": group_id,
                    "group_name": group_id,
                    "_token_id": token_id,
                }
            )
        return resources

    def _sub2api_resources(self, account: Mapping[str, Any]) -> list[dict[str, Any]]:
        headers = self._headers(account)
        keys = _json_data(self._http.json(account["origin"], "/api/v1/keys?page=1&page_size=100", headers=headers))
        if isinstance(keys, Mapping):
            keys = keys.get("items", [])
        if not isinstance(keys, Sequence) or isinstance(keys, (str, bytes, bytearray)):
            raise RelayAccountsError("Relay API key list is invalid")
        models: list[str] = []
        try:
            channels = self._http.json(account["origin"], "/api/v1/channels/available", headers=headers)
            models = _sub2api_channel_models(channels)
        except RelayAccountsError:
            # The key list above is the dashboard-session authentication
            # check.  A deployment may protect, omit, or disable this
            # dashboard-only channel catalog while its OpenAI-compatible
            # gateway remains usable; discover models from each active key
            # below instead of falsely expiring the account.
            pass
        resources: list[dict[str, Any]] = []
        for index, item in enumerate(keys):
            if not isinstance(item, Mapping):
                continue
            key = item.get("key")
            if not isinstance(key, str) or not key.strip():
                continue
            enabled = str(item.get("status", "")).lower() in {"active", "enabled"}
            resource_id, name = self._resource_label(item, index, prefix="sub2api")
            group_id = _group_id(item.get("group_id"))
            group = item.get("group")
            group_name = _resource_name(group.get("name"), group_id) if isinstance(group, Mapping) and group_id else group_id
            self._resource_secret_cache[self._resource_cache_key(account["id"], resource_id)] = key.strip()
            resource_models = models
            if enabled and not resource_models:
                try:
                    resource_models = _model_names(
                        self._http.json(
                            account["origin"],
                            "/v1/models",
                            headers={"Authorization": f"Bearer {key.strip()}"},
                        )
                    )
                except RelayAccountsError:
                    # The gateway request is authenticated by the selected
                    # API key, not by the dashboard session. A rejected key
                    # must not clear a valid dashboard login.
                    resource_models = []
            resources.append(
                {
                    "id": resource_id,
                    "name": name,
                    "api_base": f"{account['origin'].rstrip('/')}/v1",
                    "key_hint": self._key_hint(key),
                    "enabled": enabled,
                    "models": resource_models,
                    "group_id": group_id,
                    "group_name": group_name,
                    "_key": key.strip(),
                }
            )
        return resources

    def _newapi_groups(self, account: Mapping[str, Any]) -> list[dict[str, Any]]:
        payload = _json_data(
            self._http.json(account["origin"], "/api/user/self/groups", headers=self._headers(account))
        )
        if not isinstance(payload, Mapping):
            raise RelayAccountsError("Relay API groups are invalid")
        return _safe_groups([
            {
                "id": name,
                "name": name,
                "ratio": value.get("ratio", value.get("multiplier")) if isinstance(value, Mapping) else None,
            }
            for name, value in payload.items()
        ])

    def _sub2api_groups(self, account: Mapping[str, Any]) -> list[dict[str, Any]]:
        payload = _json_data(
            self._http.json(account["origin"], "/api/v1/groups/available", headers=self._headers(account))
        )
        if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes, bytearray)):
            raise RelayAccountsError("Relay API groups are invalid")
        groups = _safe_groups(payload)
        try:
            raw_rates = _json_data(
                self._http.json(account["origin"], "/api/v1/groups/rates", headers=self._headers(account))
            )
        except Exception:
            raw_rates = {}
        if isinstance(raw_rates, Mapping):
            rates = {_group_id(group_id): _group_multiplier(rate) for group_id, rate in raw_rates.items()}
            for group in groups:
                user_multiplier = rates.get(group["id"])
                if user_multiplier is not None:
                    group["multiplier"] = user_multiplier
        return groups

    def _account_balance(self, account: Mapping[str, Any]) -> float:
        headers = self._headers(account)
        if account["type"] == "sub2api":
            profile = _json_data(
                self._http.json(account["origin"], "/api/v1/user/profile", headers=headers)
            )
            if not isinstance(profile, Mapping):
                raise RelayAccountsError("Relay account balance is unavailable")
            balance = _balance(profile.get("balance"))
            if balance is None:
                raise RelayAccountsError("Relay account balance is unavailable")
            return balance
        profile = _json_data(
            self._http.json(account["origin"], "/api/user/self", headers=headers)
        )
        status = _json_data(
            self._http.json(account["origin"], "/api/status", headers={})
        )
        if not isinstance(profile, Mapping) or not isinstance(status, Mapping):
            raise RelayAccountsError("Relay account balance is unavailable")
        quota = _balance(profile.get("quota"))
        quota_per_unit = _balance(status.get("quota_per_unit"))
        if quota is None or quota_per_unit is None or quota_per_unit <= 0:
            raise RelayAccountsError("Relay account balance is unavailable")
        return quota / quota_per_unit

    def refresh_resources(self, account_id: str, *, _for_apply: bool = False) -> dict[str, Any]:
        """Load selectable metadata after native login without staging providers."""

        index = self._index(account_id)
        account = self._accounts[index]
        previous_resources = copy.deepcopy(account.get("resources", []))
        previous_groups = copy.deepcopy(account.get("groups", []))
        if account["login_status"] != "signed_in":
            raise RelayAccountsError("Relay login is unavailable")
        try:
            account["balance"] = self._account_balance(account)
        except Exception:
            # Balance is an independent account summary. A station that omits
            # its profile endpoint must not hide usable API keys.
            account["balance"] = None
        try:
            self._clear_resource_cache(account_id)
            try:
                groups = (
                    self._newapi_groups(account)
                    if account["type"] == "newapi"
                    else self._sub2api_groups(account)
                )
            except RelayAccountsError:
                # Group selection is an enhancement to a usable key list.
                # Keep the last verified choices when an older relay omits
                # this optional dashboard endpoint.
                groups = previous_groups
            private_resources = (
                self._newapi_resources(account)
                if account["type"] == "newapi"
                else self._sub2api_resources(account)
            )
            resources = _safe_resources(private_resources)
            if not resources:
                raise RelayAccountsError("Relay has no API keys")
            enabled_resources = [resource for resource in resources if resource["enabled"]]
            if enabled_resources and not any(resource["models"] for resource in enabled_resources):
                raise RelayAccountsError("Relay API keys have no available models")
        except RelayAccountsError as exc:
            message = str(exc).lower()
            if "expired" in message:
                account["login_status"] = "expired"
                account["session"] = {}
                self._session_secrets.pop(account_id, None)
                resource_error = "login_expired"
            elif "no api keys" in message:
                resource_error = "no_api_keys"
            elif "no available models" in message:
                resource_error = "no_models"
            else:
                resource_error = "unavailable"
            account["resource_status"] = "unavailable"
            account["last_updated_at"] = _utc_now_iso()
            account["resource_error"] = resource_error
            if resource_error == "no_api_keys":
                account["resources"] = []
            elif resource_error == "no_models" and "resources" in locals():
                account["resources"] = resources
            else:
                account["resources"] = previous_resources
            account["groups"] = previous_groups
            self._accounts[index] = _private_account(account)
            if not _for_apply:
                self._persist()
                self.revision += 1
            return _public_account(self._accounts[index])
        account["resources"] = resources
        account["groups"] = groups
        account["resource_status"] = "ready"
        account["last_updated_at"] = _utc_now_iso()
        account["resource_error"] = "none"
        self._accounts[index] = _private_account(account)
        if not _for_apply:
            self._persist()
            self.revision += 1
        return _public_account(self._accounts[index])

    @staticmethod
    def _api_key_name(value: object, *, fallback: str = "") -> str:
        return _text(value if value is not None else fallback, "Relay API key name", limit=160)

    @staticmethod
    def _api_key_mutation_succeeded(payload: object) -> None:
        if isinstance(payload, Mapping) and payload.get("success") is False:
            raise RelayAccountsError("Relay API key operation was rejected")

    def _api_key_request(
        self,
        account: Mapping[str, Any],
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
    ) -> object:
        request = getattr(self._http, method, None)
        if not callable(request):
            raise RelayAccountsError("Relay API key management is unavailable")
        try:
            if body is None:
                payload = request(account["origin"], path, headers=self._headers(account))
            else:
                payload = request(
                    account["origin"],
                    path,
                    headers=self._headers(account),
                    body=body,
                )
        except RelayAccountsError:
            raise
        except Exception:
            raise RelayAccountsError("Relay API key operation was rejected") from None
        self._api_key_mutation_succeeded(payload)
        return payload

    def _api_key_remote_id(self, account: Mapping[str, Any], resource_id: object) -> str:
        resource = self._selected_resource(account, resource_id)
        prefix = "newapi-" if account["type"] == "newapi" else "sub2api-"
        raw_id = resource["id"]
        if not raw_id.startswith(prefix):
            raise RelayAccountsError("The selected relay API resource is unavailable")
        remote_id = raw_id.removeprefix(prefix)
        if not remote_id:
            raise RelayAccountsError("The selected relay API resource is unavailable")
        if account["type"] == "newapi":
            try:
                int(remote_id)
            except ValueError:
                raise RelayAccountsError("The selected relay API resource is unavailable") from None
        return remote_id

    def _mark_resources_stale(self, index: int) -> None:
        account = copy.deepcopy(self._accounts[index])
        account["resource_status"] = "idle"
        account["resource_error"] = "none"
        account["last_updated_at"] = _utc_now_iso()
        self._accounts[index] = _private_account(account)

    def _newapi_token_for_update(self, account: Mapping[str, Any], remote_id: str) -> dict[str, Any]:
        try:
            payload = _json_data(
                self._http.json(account["origin"], f"/api/token/{int(remote_id)}", headers=self._headers(account))
            )
        except RelayAccountsError:
            raise
        except Exception:
            raise RelayAccountsError("Relay API key operation was rejected") from None
        if not isinstance(payload, Mapping) or type(payload.get("id")) is not int or payload["id"] != int(remote_id):
            raise RelayAccountsError("The selected relay API resource is unavailable")
        return dict(payload)

    @staticmethod
    def _newapi_update_payload(token: Mapping[str, Any], changes: Mapping[str, Any]) -> dict[str, Any]:
        token_id = token.get("id")
        if type(token_id) is not int:
            raise RelayAccountsError("The selected relay API resource is unavailable")
        name = _resource_name(token.get("name"), f"API {token_id}")
        expired_time = token.get("expired_time")
        remain_quota = token.get("remain_quota")
        unlimited_quota = token.get("unlimited_quota")
        model_limits_enabled = token.get("model_limits_enabled")
        model_limits = token.get("model_limits")
        allow_ips = token.get("allow_ips")
        cross_group_retry = token.get("cross_group_retry")
        if (
            type(expired_time) is not int
            or type(remain_quota) is not int
            or not isinstance(unlimited_quota, bool)
            or not isinstance(model_limits_enabled, bool)
            or not isinstance(model_limits, str)
            or (allow_ips is not None and not isinstance(allow_ips, str))
            or not isinstance(cross_group_retry, bool)
        ):
            raise RelayAccountsError("Relay API key configuration is invalid")
        payload: dict[str, Any] = {
            "id": token_id,
            "name": name,
            "expired_time": expired_time,
            "remain_quota": remain_quota,
            "unlimited_quota": unlimited_quota,
            "model_limits_enabled": model_limits_enabled,
            "model_limits": model_limits,
            "allow_ips": allow_ips,
            "group": _group_id(token.get("group")),
            "cross_group_retry": cross_group_retry,
        }
        payload.update(changes)
        return payload

    def _set_newapi_key_metadata(
        self,
        account: Mapping[str, Any],
        remote_id: str,
        changes: Mapping[str, Any],
    ) -> None:
        token = self._newapi_token_for_update(account, remote_id)
        self._api_key_request(
            account,
            method="put",
            path="/api/token/",
            body=self._newapi_update_payload(token, changes),
        )

    def _stage_resource_change(
        self,
        account_id: str,
        resource_id: object,
        *,
        kind: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        index = self._index(account_id)
        account = self._accounts[index]
        selected_id = _resource_id(resource_id)
        self._selected_resource(account, selected_id)
        pending = self._pending_for_resource(account["id"], selected_id)
        if any(operation.get("kind") == "api_key_delete" for operation in pending):
            raise RelayAccountsError("The selected relay API resource is pending deletion")
        create = next((operation for operation in pending if operation.get("kind") == "api_key_create"), None)
        if create is not None and create.get("state") == "staged":
            merged = dict(create.get("changes", {}))
            merged.update(changes)
            self._update_pending_operation(create, changes=merged)
        else:
            operation = next((operation for operation in pending if operation.get("kind") == kind and operation.get("state") == "staged"), None)
            if operation is None:
                operation = self._new_pending_operation(
                    kind=kind,
                    account=account,
                    resource_id=selected_id,
                    changes=changes,
                )
            else:
                merged = dict(operation.get("changes", {}))
                merged.update(changes)
                self._update_pending_operation(operation, changes=merged)
        self._replace_resource_preview(index, selected_id, changes)
        self._draft_staged = True
        return _public_account(self._accounts[index])

    def create_api_key(
        self,
        account_id: str,
        name: object = None,
        *,
        group_id: object = None,
        enabled: object = True,
    ) -> dict[str, Any]:
        index = self._index(account_id)
        account = self._accounts[index]
        if not isinstance(enabled, bool):
            raise RelayAccountsError("Relay API key status is invalid")
        existing = account.get("resources", [])
        count = len(existing) if isinstance(existing, list) else 0
        key_name = self._api_key_name(name, fallback=f"API {count + 1}")
        selected_group = _group_id(group_id, required=False) if group_id is not None else ""
        if selected_group:
            groups = account.get("groups", [])
            if not isinstance(groups, list) or selected_group not in {
                group.get("id") for group in groups if isinstance(group, Mapping)
            }:
                raise RelayAccountsError("Relay API group is unavailable")
        temporary_id = f"pending-{uuid.uuid4().hex}"
        changes: dict[str, Any] = {"name": key_name, "enabled": enabled}
        if selected_group:
            changes["group_id"] = selected_group
        self._new_pending_operation(
            kind="api_key_create",
            account=account,
            resource_id=temporary_id,
            changes=changes,
            known_resource_ids=[
                str(resource.get("id", ""))
                for resource in existing
                if isinstance(resource, Mapping)
            ],
        )
        group_name = selected_group
        if selected_group:
            group = next(
                (item for item in account.get("groups", []) if isinstance(item, Mapping) and item.get("id") == selected_group),
                None,
            )
            if isinstance(group, Mapping):
                group_name = str(group.get("name", selected_group))
        updated = copy.deepcopy(account)
        updated["resources"] = [
            *[dict(item) for item in existing if isinstance(item, Mapping)],
            _safe_resource(
                {
                    "id": temporary_id,
                    "name": key_name,
                    "api_base": f"{account['origin'].rstrip('/')}/v1",
                    "enabled": enabled,
                    "models": [],
                    "group_id": selected_group,
                    "group_name": group_name,
                }
            ),
        ]
        self._accounts[index] = _private_account(updated)
        self._draft_staged = True
        return _public_account(self._accounts[index])

    def update_api_key(self, account_id: str, resource_id: object, name: object) -> dict[str, Any]:
        return self._stage_resource_change(
            account_id,
            resource_id,
            kind="api_key_update",
            changes={"name": self._api_key_name(name)},
        )

    def set_api_key_group(self, account_id: str, resource_id: object, group_id: object) -> dict[str, Any]:
        index = self._index(account_id)
        account = self._accounts[index]
        selected_group = _group_id(group_id, required=account["type"] == "sub2api")
        groups = account.get("groups", [])
        if not isinstance(groups, list) or selected_group not in {
            group.get("id") for group in groups if isinstance(group, Mapping)
        }:
            raise RelayAccountsError("Relay API group is unavailable")
        return self._stage_resource_change(
            account_id,
            resource_id,
            kind="api_key_set_group",
            changes={"group_id": selected_group},
        )

    def set_api_key_enabled(self, account_id: str, resource_id: object, enabled: object) -> dict[str, Any]:
        if not isinstance(enabled, bool):
            raise RelayAccountsError("Relay API key status is invalid")
        return self._stage_resource_change(
            account_id,
            resource_id,
            kind="api_key_set_enabled",
            changes={"enabled": enabled},
        )

    def delete_api_key(
        self,
        account_id: str,
        resource_id: object,
        *,
        dependency_policy: object = "delete_models",
    ) -> dict[str, Any]:
        index = self._index(account_id)
        account = self._accounts[index]
        selected_id = _resource_id(resource_id)
        self._selected_resource(account, selected_id)
        policy = _dependency_policy(dependency_policy, default="delete_models")
        pending = self._pending_for_resource(account["id"], selected_id)
        create = next((operation for operation in pending if operation.get("kind") == "api_key_create"), None)
        if create is not None and create.get("state") == "staged":
            self._pending_operations = [operation for operation in self._pending_operations if operation is not create]
            updated = copy.deepcopy(account)
            updated["resources"] = [
                item for item in updated.get("resources", []) if item.get("id") != selected_id
            ]
            self._accounts[index] = _private_account(updated)
            self._draft_staged = True
            return _public_account(self._accounts[index])
        deleting = next((operation for operation in pending if operation.get("kind") == "api_key_delete"), None)
        if deleting is not None:
            if deleting.get("state") != "staged":
                raise RelayAccountsError("The selected relay API resource is already being deleted")
            self._update_pending_operation(deleting, dependency_policy=policy)
        else:
            self._pending_operations = [
                operation
                for operation in self._pending_operations
                if not (
                    operation.get("account_id") == account["id"]
                    and operation.get("resource_id") == selected_id
                    and operation.get("state") == "staged"
                )
            ]
            self._new_pending_operation(
                kind="api_key_delete",
                account=account,
                resource_id=selected_id,
                dependency_policy=policy,
            )
        self._draft_staged = True
        return _public_account(self._accounts[index])

    def detach_api_key(self, account_id: str, resource_id: object) -> dict[str, Any]:
        index = self._index(account_id)
        account = self._accounts[index]
        selected_id = _resource_id(resource_id)
        self._selected_resource(account, selected_id)
        pending = self._pending_for_resource(account["id"], selected_id)
        deleting = next((operation for operation in pending if operation.get("kind") == "api_key_delete"), None)
        if deleting is None:
            return _public_account(self._accounts[index])
        if deleting.get("state") != "staged":
            raise RelayAccountsError("The remote relay API key deletion has already started")
        self._pending_operations = [operation for operation in self._pending_operations if operation is not deleting]
        self._draft_staged = True
        return _public_account(self._accounts[index])

    def _selected_resource(self, account: Mapping[str, Any], resource_id: object) -> dict[str, Any]:
        selected = _resource_id(resource_id)
        resources = account.get("resources", [])
        if not isinstance(resources, list):
            raise RelayAccountsError("Relay API resources are unavailable")
        resource = next((item for item in resources if isinstance(item, Mapping) and item.get("id") == selected), None)
        if not isinstance(resource, Mapping):
            raise RelayAccountsError("The selected relay API resource is unavailable")
        return dict(resource)

    def _read_key(self, account: Mapping[str, Any], resource: Mapping[str, Any]) -> str:
        cache_key = self._resource_cache_key(account["id"], resource["id"])
        cached = self._resource_secret_cache.get(cache_key)
        if isinstance(cached, str) and cached:
            return cached
        if account["type"] == "sub2api":
            keys = _json_data(self._http.json(account["origin"], "/api/v1/keys?page=1&page_size=100", headers=self._headers(account)))
            if isinstance(keys, Mapping):
                keys = keys.get("items", [])
            if not isinstance(keys, Sequence) or isinstance(keys, (str, bytes, bytearray)):
                raise RelayAccountsError("Relay API key list is invalid")
            candidates = [item for item in keys if isinstance(item, Mapping)]
            # Resource IDs are derived from the upstream key ID. Prefer that
            # stable identifier over a display name: duplicate key names are
            # legal and must not reveal or import the wrong credential.
            wanted_id = resource["id"].removeprefix("sub2api-")
            candidate = next((item for item in candidates if str(item.get("id", "")).strip() == wanted_id), None)
            if candidate is None:
                wanted_name = resource["name"]
                candidate = next((item for item in candidates if _resource_name(item.get("name", item.get("label", item.get("id"))), "") == wanted_name), None)
            if candidate is None and len(candidates) == 1:
                candidate = candidates[0]
            key = candidate.get("key") if isinstance(candidate, Mapping) else None
            if not isinstance(key, str) or not key.strip():
                raise RelayAccountsError("Relay API key is unavailable")
            value = key.strip()
            self._resource_secret_cache[cache_key] = value
            return value
        token_id_text = resource["id"].removeprefix("newapi-")
        try:
            token_id = int(token_id_text)
        except ValueError:
            raise RelayAccountsError("The selected relay API resource is unavailable")
        payload = _json_data(self._http.post(account["origin"], f"/api/token/{token_id}/key", headers=self._headers(account)))
        key = payload.get("key") if isinstance(payload, Mapping) else None
        if not isinstance(key, str) or not key.strip():
            raise RelayAccountsError("Relay API key is unavailable")
        value = key.strip()
        value = value if value.startswith("sk-") else f"sk-{value}"
        self._resource_secret_cache[cache_key] = value
        return value

    @staticmethod
    def _resource_multiplier(account: Mapping[str, Any], resource: Mapping[str, Any]) -> float | None:
        group_id = str(resource.get("group_id", ""))
        if not group_id:
            return None
        group = next(
            (
                item
                for item in account.get("groups", [])
                if isinstance(item, Mapping) and item.get("id") == group_id
            ),
            None,
        )
        return _group_multiplier(group.get("multiplier")) if isinstance(group, Mapping) else None

    def _relay_source(
        self,
        account: Mapping[str, Any],
        resource: Mapping[str, Any],
        *,
        include_key: bool = False,
    ) -> dict[str, Any]:
        """Build one stable source descriptor; keys stay private by default."""

        source: dict[str, Any] = {
            "station_id": _station_id(account.get("station_id")),
            "account_id": _account_id(account.get("id")),
            "resource_id": _resource_id(resource.get("id")),
            "provider_name": f"relay-{_station_id(account.get('station_id'))}",
            "api_base": _api_base(resource.get("api_base")),
            "enabled": resource.get("enabled") is not False,
            "name": _resource_name(resource.get("name"), "API"),
            "api_key_name": _resource_name(resource.get("name"), "API"),
            "models": _model_names(resource.get("models", [])),
            "source_models": _model_names(resource.get("models", [])),
        }
        multiplier = self._resource_multiplier(account, resource)
        if multiplier is not None:
            source["multiplier"] = multiplier
        if include_key:
            source["api_key"] = self._read_key(account, resource)
        return source

    def binding_source(self, source: object) -> dict[str, Any]:
        """Resolve one existing relay resource to a secret-free stable source.

        Provider-key imports happen before Apply, so this deliberately reads
        only the persisted relay metadata.  Credentials and an up-to-date API
        base are resolved later by :meth:`binding_materials` during Apply.
        """

        if isinstance(source, Mapping) and isinstance(source.get("source"), Mapping):
            source = source["source"]
        if not isinstance(source, Mapping):
            raise RelayAccountsError("Relay binding source is invalid")
        station_id = _station_id(source.get("station_id"))
        account_id = _account_id(source.get("account_id"))
        resource_id = _resource_id(source.get("resource_id"))
        if resource_id.startswith("pending-"):
            raise RelayAccountsError("Apply the relay API key before importing it")
        account_result = self._operation_account(
            {"station_id": station_id, "account_id": account_id}
        )
        if account_result is None:
            raise RelayAccountsError("Relay account is unavailable")
        _, account = account_result
        resource = self._selected_resource(account, resource_id)
        if resource.get("enabled") is not True:
            raise RelayAccountsError("Enable the relay API key before importing it")
        return self._relay_source(account, resource)

    def provider_station_source(self, source: object) -> dict[str, str]:
        """Resolve the one non-secret station identity used by a provider."""

        if not isinstance(source, Mapping):
            raise RelayAccountsError("Relay station is unavailable")
        station_id = _station_id(source.get("station_id", source.get("id")))
        station = self._stations[self._station_index(station_id)]
        return {
            "station_id": station["id"],
            "name": station["name"],
            "api_base": station["origin"],
        }

    @staticmethod
    def _binding_source_rows(sources: object | None) -> object:
        if isinstance(sources, Mapping):
            return sources.get("resources", sources.get("sources", []))
        return sources

    def refresh_binding_sources(self, sources: object | None = None) -> dict[str, Any]:
        """Refresh linked relay accounts before materializing dynamic bindings.

        This is an Apply-time read-only step. It deliberately reuses stable
        account/resource IDs and never guesses a replacement by display name.
        """

        raw_sources = self._binding_source_rows(sources)
        if raw_sources is None:
            raw_sources = [
                {"station_id": account.get("station_id"), "account_id": account.get("id")}
                for account in self._accounts
            ]
        if not isinstance(raw_sources, Sequence) or isinstance(raw_sources, (str, bytes, bytearray)):
            raise RelayAccountsError("Relay binding sources are invalid")
        account_ids: list[str] = []
        issues: list[dict[str, str]] = []
        for raw in raw_sources:
            if not isinstance(raw, Mapping):
                raise RelayAccountsError("Relay binding sources are invalid")
            station_id = _station_id(raw.get("station_id"))
            account_id = _account_id(raw.get("account_id"))
            account_result = self._operation_account(
                {"station_id": station_id, "account_id": account_id}
            )
            if account_result is None:
                issues.append(self._apply_issue("account_unavailable", account_id=account_id))
                continue
            if account_id not in account_ids:
                account_ids.append(account_id)
        refreshed = 0
        for account_id in account_ids:
            try:
                result = self.refresh_resources(account_id, _for_apply=True)
            except Exception:
                issues.append(self._apply_issue("refresh_failed", account_id=account_id))
                continue
            refreshed += 1
            if result.get("resource_status") != "ready":
                issues.append(self._apply_issue("refresh_failed", account_id=account_id))
        return {"refreshed_accounts": refreshed, "issues": issues}

    def binding_materials(self, sources: object | None = None, *, refresh: bool = False) -> dict[str, Any]:
        """Resolve private relay binding material for the Core Apply coordinator.

        This method is intentionally *not* used by ``snapshot`` or generic
        actions. Its return value can contain ``api_key`` and must remain
        inside Core while it is passed straight to provider materialization.
        """

        refresh_result = self.refresh_binding_sources(sources) if refresh else {"issues": []}
        raw_sources = self._binding_source_rows(sources)
        if raw_sources is None:
            raw_sources = [
                {
                    "station_id": account.get("station_id"),
                    "account_id": account.get("id"),
                    "resource_id": resource.get("id"),
                }
                for account in self._accounts
                for resource in account.get("resources", [])
                if isinstance(resource, Mapping)
            ]
        if not isinstance(raw_sources, Sequence) or isinstance(raw_sources, (str, bytes, bytearray)):
            raise RelayAccountsError("Relay binding sources are invalid")
        materials: list[dict[str, Any]] = []
        issues: list[dict[str, str]] = list(refresh_result["issues"])
        for raw in raw_sources:
            if not isinstance(raw, Mapping):
                raise RelayAccountsError("Relay binding sources are invalid")
            station_id = _station_id(raw.get("station_id"))
            account_id = _account_id(raw.get("account_id"))
            resource_id = _resource_id(raw.get("resource_id"))
            try:
                _, account = self._operation_account(
                    {"station_id": station_id, "account_id": account_id}
                ) or (None, None)
                if account is None:
                    raise RelayAccountsError("Relay account is unavailable")
                resource = self._selected_resource(account, resource_id)
            except Exception:
                issues.append(self._apply_issue("resource_unavailable", account_id=account_id, resource_id=resource_id))
                continue
            if resource_id.startswith("pending-"):
                issues.append(self._apply_issue("created_resource_unresolved", account_id=account_id, resource_id=resource_id))
                continue
            try:
                material = self._relay_source(account, resource, include_key=True)
            except Exception:
                issues.append(self._apply_issue("resource_key_unavailable", account_id=account_id, resource_id=resource_id))
                continue
            materials.append(material)
            if material["enabled"] is not True:
                issues.append(self._apply_issue("resource_disabled", account_id=account_id, resource_id=resource_id))
        return {"resources": materials, "issues": issues}

    def resolve_bindings(self, sources: object | None = None) -> dict[str, Any]:
        """Alias retained for a coordinator that models this as resolution."""

        return self.binding_materials(sources)

    def import_resources(
        self,
        account_id: str,
        resource_ids: object,
        providers_domain: object,
        *,
        mode: object = "linked",
        import_mode: object | None = None,
    ) -> dict[str, Any]:
        """Stage selected resources as linked (default) or independent models."""

        index = self._index(account_id)
        account = self._accounts[index]
        selected_mode = str(import_mode if import_mode is not None else mode).strip().lower()
        if selected_mode not in {"linked", "independent"}:
            raise RelayAccountsError("Relay import mode is invalid")
        if not isinstance(resource_ids, Sequence) or isinstance(resource_ids, (str, bytes, bytearray)):
            raise RelayAccountsError("Select at least one relay API resource")
        requested = [_resource_id(value) for value in resource_ids]
        if not requested or len(set(requested)) != len(requested) or len(requested) > MAX_RESOURCES:
            raise RelayAccountsError("Select at least one relay API resource")
        selected = [self._selected_resource(account, value) for value in requested]
        if any(resource.get("enabled") is False for resource in selected):
            raise RelayAccountsError("Enable the selected relay API resource before importing")
        dispatch = getattr(providers_domain, "dispatch", None)
        snapshotter = getattr(providers_domain, "snapshot", None)
        if not callable(dispatch) or not callable(snapshotter):
            raise RelayAccountsError("Provider/model settings are unavailable")
        sources = [self._relay_source(account, resource) for resource in selected]
        if selected_mode == "linked":
            stage_linked = getattr(providers_domain, "stage_relay_import", None)
            try:
                if callable(stage_linked):
                    snapshot = stage_linked(sources, import_mode="linked")
                else:
                    snapshot = dispatch("relay.import", {"sources": sources, "import_mode": "linked"})
            except Exception as exc:
                raise RelayAccountsError("Provider/model import could not be staged") from exc
            imported_models = sum(len(source["models"]) for source in sources)
            self.revision += 1
            return {
                "imported": True,
                "import_mode": "linked",
                "account_id": account["id"],
                "station_id": account["station_id"],
                "resource_count": len(selected),
                "provider_id": sources[0]["provider_name"] if sources else "",
                "model_count": imported_models,
                "sources": sources,
                "providers": snapshot,
            }

        exporter = getattr(providers_domain, "export", None)
        if not callable(exporter):
            raise RelayAccountsError("Provider/model settings are unavailable")
        private = exporter(include_sensitive=True)
        providers = private.get("providers", []) if isinstance(private, Mapping) else []
        if not isinstance(providers, list):
            raise RelayAccountsError("Provider/model settings are unavailable")
        provider_id = f"relay-{account['id'][:12]}"
        existing = next((item for item in providers if isinstance(item, Mapping) and str(item.get("name", "")) == provider_id), None)
        provider = copy.deepcopy(dict(existing)) if isinstance(existing, Mapping) else {"name": provider_id, "api_keys": [], "models": []}
        provider["api_base"] = str(selected[0]["api_base"])
        api_keys = [dict(item) for item in provider.get("api_keys", []) if isinstance(item, Mapping)]
        models = [dict(item) for item in provider.get("models", []) if isinstance(item, Mapping)]
        imported_models = 0
        selected_names = [str(resource.get("name", "")) for resource in selected]
        for resource in selected:
            key = self._read_key(account, resource)
            key_name = _resource_name(resource["name"], "default")
            if selected_names.count(key_name) > 1:
                key_name = _resource_name(
                    f"{key_name} ({str(resource['id']).rsplit('-', 1)[-1]})",
                    key_name,
                )
            api_keys = [item for item in api_keys if str(item.get("name", "")) != key_name]
            api_keys.append({"name": key_name, "value": key})
            models = [item for item in models if str(item.get("api_key_name", "")) != key_name]
            for model in resource["models"]:
                models.append(
                    {
                        "model_name": model,
                        "litellm_model": f"openai/{model}",
                        "provider": provider_id,
                        "api_base": resource["api_base"],
                        "api_key_name": key_name,
                        "api_key": key,
                        "enabled": True,
                        "model_enabled": True,
                        "order": 0,
                        "deployment_id": uuid.uuid4().hex[:8],
                        "upstream_url_surface": "openai/responses",
                    }
                )
                imported_models += 1
        provider["api_keys"] = api_keys
        provider["api_key"] = api_keys[0]["value"] if api_keys else ""
        provider["models"] = models
        try:
            current = snapshotter()
            current_providers = current.get("providers", []) if isinstance(current, Mapping) else []
            exists = any(isinstance(item, Mapping) and provider_id in {str(item.get("id", "")), str(item.get("name", ""))} for item in current_providers)
            snapshot = dispatch("provider.patch" if exists else "provider.add", {"provider_id": provider_id, "changes": provider} if exists else {"provider": provider})
        except Exception as exc:
            raise RelayAccountsError("Provider/model import could not be staged") from exc
        self.revision += 1
        return {
            "imported": True,
            "import_mode": "independent",
            "account_id": account["id"],
            "station_id": account["station_id"],
            "resource_count": len(selected),
            "provider_id": provider_id,
            "model_count": imported_models,
            "sources": sources,
            "providers": snapshot,
        }

__all__ = [
    "ACCOUNT_TYPES",
    "DETECTION_RESPONSE_BYTES",
    "DETECTION_TIMEOUT_SECONDS",
    "DOMAIN_NAME",
    "MAX_STATIONS",
    "RelayAccountsDomain",
    "RelayAccountsError",
    "RelayHTTPClient",
]
