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
MAX_ACCOUNTS = 64
MAX_STATIONS = MAX_ACCOUNTS
MAX_PENDING_CLEANUPS = MAX_ACCOUNTS
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
        # Ordinary relay-account changes retain their historical immediate
        # persistence contract.  A configuration-package import is the one
        # exception: it replaces this in-memory view first and is committed
        # only by Core's explicit Apply transaction.
        self._import_staged = False
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

    def _read_storage_bytes(self) -> bytes | None:
        try:
            return read_bytes(self.storage_path)
        except PersistenceError as exc:
            raise RelayAccountsError(safe_exception_message(exc)) from None

    def _persist(self, *, force: bool = False) -> None:
        # A package import must remain reversible until the shared Core Apply
        # transaction crosses the persistence boundary.  Existing account
        # operations still call this method normally and therefore continue
        # to persist immediately outside that staged-import state.
        if self._import_staged and not force:
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
            "import_staged": self._import_staged,
            "baseline_bytes": self._baseline_bytes,
            "revision": self.revision,
        }

    def restore_transaction(self, checkpoint: Mapping[str, Any]) -> None:
        stations = checkpoint.get("stations")
        accounts = checkpoint.get("accounts")
        secrets = checkpoint.get("session_secrets")
        resource_secrets = checkpoint.get("resource_secret_cache")
        pending_cleanups = checkpoint.get("pending_credential_cleanups")
        import_staged = checkpoint.get("import_staged")
        baseline_bytes = checkpoint.get("baseline_bytes")
        revision = checkpoint.get("revision")
        if (
            not isinstance(stations, list)
            or any(not isinstance(item, Mapping) for item in stations)
            or not isinstance(accounts, list)
            or not isinstance(secrets, Mapping)
            or not isinstance(resource_secrets, Mapping)
            or not isinstance(pending_cleanups, list)
            or type(import_staged) is not bool
            or (baseline_bytes is not None and not isinstance(baseline_bytes, bytes))
            or type(revision) is not int
        ):
            raise RelayAccountsError("Relay account rollback failed")
        self._stations = copy.deepcopy([dict(item) for item in stations])
        self._accounts = copy.deepcopy(accounts)
        self._session_secrets = copy.deepcopy(dict(secrets))
        self._resource_secret_cache = copy.deepcopy(dict(resource_secrets))
        self._pending_credential_cleanups = copy.deepcopy(pending_cleanups)
        self._import_staged = import_staged
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

    def snapshot(self) -> dict[str, Any]:
        accounts = [_public_account(account) for account in self._accounts]
        stations = [
            _public_station(station, self._station_account_count(station["id"]))
            for station in self._stations
        ]
        pending_cleanups = [
            _public_pending_cleanup(cleanup)
            for cleanup in self._pending_credential_cleanups
        ]
        return {
            "domain": self.name,
            "revision": self.revision,
            "stations": stations,
            "station_count": len(stations),
            "accounts": accounts,
            "account_count": len(accounts),
            "pending_credential_cleanups": pending_cleanups,
        }

    def draft_state(self) -> object:
        # Relay-account operations persist immediately, except an imported
        # package.  Returning the private durable payload only while that
        # import is staged lets Core track it as a normal dirty draft without
        # ever putting it in the public snapshot.
        return self._stored_payload() if self._import_staged else {}

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
        self._import_staged = False
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
        if name in {"add", "account_add", "relay_add"}:
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
        elif name in {"station_update", "relay_station_update", "update_station"}:
            self._update_station(data)
        elif name in {"api_key_create", "relay_api_key_create", "account_api_key_create"}:
            self.create_api_key(
                str(data.get("account_id", data.get("id", ""))),
                data.get("name"),
            )
        elif name in {"api_key_update", "relay_api_key_update", "account_api_key_update"}:
            self.update_api_key(
                str(data.get("account_id", data.get("id", ""))),
                data.get("resource_id", data.get("key_id")),
                data.get("name"),
            )
        elif name in {"api_key_set_enabled", "relay_api_key_set_enabled", "account_api_key_set_enabled"}:
            self.set_api_key_enabled(
                str(data.get("account_id", data.get("id", ""))),
                data.get("resource_id", data.get("key_id")),
                data.get("enabled"),
            )
        elif name in {"api_key_set_group", "relay_api_key_set_group", "account_api_key_set_group"}:
            self.set_api_key_group(
                str(data.get("account_id", data.get("id", ""))),
                data.get("resource_id", data.get("key_id")),
                data.get("group_id"),
            )
        elif name in {"api_key_delete", "relay_api_key_delete", "account_api_key_delete"}:
            self.delete_api_key(
                str(data.get("account_id", data.get("id", ""))),
                data.get("resource_id", data.get("key_id")),
            )
        elif name in {"delete", "account_delete", "relay_delete"}:
            account = self._accounts[self._index(data.get("id"))]
            # A whole-account erase supersedes an outstanding password-only
            # cleanup for that account. Keeping both would make the persisted
            # state internally inconsistent once the account is removed.
            self._pending_credential_cleanups = [
                cleanup
                for cleanup in self._pending_credential_cleanups
                if cleanup["account_id"] != account["id"]
            ]
            self._retain_pending_cleanup(
                account_id=account["id"],
                label=account["label"],
                kind="credentials",
            )
            station_id = str(account.get("station_id", ""))
            self._accounts.pop(self._index(account["id"]))
            self._session_secrets.pop(account["id"], None)
            if station_id and self._station_account_count(station_id) == 0:
                self._stations = [item for item in self._stations if item["id"] != station_id]
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
        else:
            raise RelayAccountsError("The requested relay action is unavailable")
        self._persist()
        self.revision += 1
        return self.snapshot()

    def validate(self, payload: object | None = None) -> dict[str, Any]:
        del payload
        return {"valid": True, "issues": []}

    def apply(self, payload: object | None = None) -> dict[str, Any]:
        del payload
        if not self._import_staged:
            return self.snapshot()
        if self._read_storage_bytes() != self._baseline_bytes:
            raise RelayAccountsError("Relay accounts changed on disk; reload before applying")
        self._persist(force=True)
        self._import_staged = False
        self.revision += 1
        return self.snapshot()

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

    def refresh_resources(self, account_id: str) -> dict[str, Any]:
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
            self._persist()
            self.revision += 1
            return _public_account(self._accounts[index])
        account["resources"] = resources
        account["groups"] = groups
        account["resource_status"] = "ready"
        account["last_updated_at"] = _utc_now_iso()
        account["resource_error"] = "none"
        self._accounts[index] = _private_account(account)
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

    def create_api_key(self, account_id: str, name: object = None) -> dict[str, Any]:
        index = self._index(account_id)
        account = self._accounts[index]
        if account["login_status"] != "signed_in":
            raise RelayAccountsError("Relay login is unavailable")
        existing = account.get("resources", [])
        count = len(existing) if isinstance(existing, list) else 0
        key_name = self._api_key_name(name, fallback=f"API {count + 1}")
        if account["type"] == "newapi":
            self._api_key_request(
                account,
                method="post",
                path="/api/token/",
                body={"name": key_name, "unlimited_quota": True},
            )
        else:
            self._api_key_request(
                account,
                method="post",
                path="/api/v1/keys",
                body={"name": key_name},
            )
        self._mark_resources_stale(index)
        return _public_account(self._accounts[index])

    def update_api_key(self, account_id: str, resource_id: object, name: object) -> dict[str, Any]:
        index = self._index(account_id)
        account = self._accounts[index]
        if account["login_status"] != "signed_in":
            raise RelayAccountsError("Relay login is unavailable")
        key_name = self._api_key_name(name)
        remote_id = self._api_key_remote_id(account, resource_id)
        if account["type"] == "newapi":
            self._set_newapi_key_metadata(account, remote_id, {"name": key_name})
        else:
            self._api_key_request(
                account,
                method="put",
                path=f"/api/v1/keys/{quote(remote_id, safe='')}",
                body={"name": key_name},
            )
        self._mark_resources_stale(index)
        return _public_account(self._accounts[index])

    def set_api_key_group(self, account_id: str, resource_id: object, group_id: object) -> dict[str, Any]:
        index = self._index(account_id)
        account = self._accounts[index]
        if account["login_status"] != "signed_in":
            raise RelayAccountsError("Relay login is unavailable")
        selected_group = _group_id(group_id, required=account["type"] == "sub2api")
        groups = account.get("groups", [])
        if not isinstance(groups, list) or selected_group not in {
            group.get("id") for group in groups if isinstance(group, Mapping)
        }:
            raise RelayAccountsError("Relay API group is unavailable")
        remote_id = self._api_key_remote_id(account, resource_id)
        if account["type"] == "newapi":
            self._set_newapi_key_metadata(account, remote_id, {"group": selected_group})
        else:
            try:
                upstream_group_id = int(selected_group)
            except ValueError:
                raise RelayAccountsError("Relay API group is invalid") from None
            self._api_key_request(
                account,
                method="put",
                path=f"/api/v1/keys/{quote(remote_id, safe='')}",
                body={"group_id": upstream_group_id},
            )
        self._mark_resources_stale(index)
        return _public_account(self._accounts[index])

    def set_api_key_enabled(self, account_id: str, resource_id: object, enabled: object) -> dict[str, Any]:
        index = self._index(account_id)
        account = self._accounts[index]
        if account["login_status"] != "signed_in":
            raise RelayAccountsError("Relay login is unavailable")
        if not isinstance(enabled, bool):
            raise RelayAccountsError("Relay API key status is invalid")
        remote_id = self._api_key_remote_id(account, resource_id)
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
        selected_id = _resource_id(resource_id)
        updated_account = copy.deepcopy(self._accounts[index])
        for resource in updated_account.get("resources", []):
            if resource.get("id") == selected_id:
                resource["enabled"] = enabled
                break
        self._accounts[index] = _private_account(updated_account)
        self._mark_resources_stale(index)
        return _public_account(self._accounts[index])

    def delete_api_key(self, account_id: str, resource_id: object) -> dict[str, Any]:
        index = self._index(account_id)
        account = self._accounts[index]
        if account["login_status"] != "signed_in":
            raise RelayAccountsError("Relay login is unavailable")
        remote_id = self._api_key_remote_id(account, resource_id)
        if account["type"] == "newapi":
            path = f"/api/token/{int(remote_id)}"
        else:
            path = f"/api/v1/keys/{quote(remote_id, safe='')}"
        self._api_key_request(account, method="delete", path=path)
        self._resource_secret_cache.pop(self._resource_cache_key(account_id, resource_id), None)
        self._mark_resources_stale(index)
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

    def import_resources(self, account_id: str, resource_ids: object, providers_domain: object) -> dict[str, Any]:
        """Stage explicitly selected relay resources without applying config."""

        index = self._index(account_id)
        account = self._accounts[index]
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
        exporter = getattr(providers_domain, "export", None)
        if not callable(dispatch) or not callable(snapshotter) or not callable(exporter):
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
        for resource in selected:
            key = self._read_key(account, resource)
            key_name = _resource_name(resource["name"], "default")
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
            "account_id": account["id"],
            "resource_count": len(selected),
            "provider_id": provider_id,
            "model_count": imported_models,
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
