"""Relay-account metadata and New API/Sub2API import adapters.

The public domain snapshot is intentionally only an account index. Browser
sessions and credentials never enter the metadata file or ordinary IPC.
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
import re
import ssl
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit
import urllib.error
import urllib.request
import uuid

from ..persistence import AtomicJSONStore, PersistenceError
from ..security import safe_exception_message


DOMAIN_NAME = "relay_accounts"
ACCOUNT_TYPES = ("newapi", "sub2api")
LOGIN_STATUSES = ("signed_out", "signed_in", "expired", "unknown")
RESOURCE_STATUSES = ("idle", "ready", "unavailable")
PENDING_CLEANUP_KINDS = ("credentials", "password")
MAX_ACCOUNTS = 64
MAX_PENDING_CLEANUPS = MAX_ACCOUNTS
MAX_MODELS = 512
MAX_RESOURCES = 256
MAX_RESOURCE_ID = 128
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
    return {
        "id": resource_id,
        "name": name,
        "api_name": name,
        "api_base": api_base,
        "key_hint": hint,
        "models": models,
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
    return {
        "id": _account_id(raw.get("id")),
        "type": account_type,
        "label": _text(raw.get("label"), "Relay label", limit=160),
        "origin": _origin(raw.get("origin")),
        "username": _text(raw.get("username", ""), "Relay username", limit=320, required=False),
        "login_status": status,
        "remember_password": raw.get("remember_password") is True,
        "resource_status": resource_status,
        "resources": _safe_resources(raw.get("resources", [])),
    }


def _reloaded_account(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Drop process-local login claims after Core restarts.

    The native host can restore the account's Keychain/Credential Manager
    session and verify it again.  Until then, persisted metadata must not
    claim that the remote site is currently authenticated.
    """

    account = _private_account(raw)
    if account["login_status"] == "signed_in":
        account["login_status"] = "unknown"
    return account


def _public_account(account: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(account.get("id", "")),
        "type": str(account.get("type", "")),
        "label": str(account.get("label", "")),
        "origin": str(account.get("origin", "")),
        "username": str(account.get("username", "")),
        "login_status": str(account.get("login_status", "unknown")),
        "remember_password": account.get("remember_password") is True,
        "resource_status": str(account.get("resource_status", "idle")),
        "resources": copy.deepcopy(account.get("resources", [])),
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

    def json(self, origin: str, path: str, *, headers: Mapping[str, str]) -> object:
        url = urljoin(origin.rstrip("/") + "/", path.lstrip("/"))
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "LiteLLM-Menu-Core/1", **dict(headers)}, method="GET")
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
        self._accounts: list[dict[str, Any]] = []
        # Browser credentials are deliberately process-local. Native WebView
        # stores own persistent sessions; an optional remembered password is
        # owned by Keychain/Credential Manager, never this domain.
        self._session_secrets: dict[str, dict[str, str]] = {}
        # A native Keychain/Credential Manager erase can fail after the account
        # metadata has been deleted. Keep only an opaque tombstone so the UI
        # can retry after it closes or the Core restarts; never retain a value
        # that could authenticate to the relay here.
        self._pending_credential_cleanups: list[dict[str, str]] = []
        self.revision = 0
        self.reload()

    def _persist(self) -> None:
        try:
            self._store.write(
                {
                    "version": 1,
                    "accounts": [_public_account(account) for account in self._accounts],
                    "pending_credential_cleanups": [
                        _public_pending_cleanup(cleanup)
                        for cleanup in self._pending_credential_cleanups
                    ],
                }
            )
        except PersistenceError as exc:
            raise RelayAccountsError(safe_exception_message(exc)) from None

    def transaction_checkpoint(self) -> dict[str, Any]:
        """Capture only mutable relay state; the HTTP client stays shared."""

        return {
            "accounts": copy.deepcopy(self._accounts),
            "session_secrets": copy.deepcopy(self._session_secrets),
            "pending_credential_cleanups": copy.deepcopy(self._pending_credential_cleanups),
            "revision": self.revision,
        }

    def restore_transaction(self, checkpoint: Mapping[str, Any]) -> None:
        accounts = checkpoint.get("accounts")
        secrets = checkpoint.get("session_secrets")
        pending_cleanups = checkpoint.get("pending_credential_cleanups")
        revision = checkpoint.get("revision")
        if (
            not isinstance(accounts, list)
            or not isinstance(secrets, Mapping)
            or not isinstance(pending_cleanups, list)
            or type(revision) is not int
        ):
            raise RelayAccountsError("Relay account rollback failed")
        self._accounts = copy.deepcopy(accounts)
        self._session_secrets = copy.deepcopy(dict(secrets))
        self._pending_credential_cleanups = copy.deepcopy(pending_cleanups)
        self.revision = revision

    def _index(self, value: object) -> int:
        account_id = _account_id(value)
        for index, account in enumerate(self._accounts):
            if account["id"] == account_id:
                return index
        raise RelayAccountsError("Relay account is unavailable")

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
        pending_cleanups = [
            _public_pending_cleanup(cleanup)
            for cleanup in self._pending_credential_cleanups
        ]
        return {
            "domain": self.name,
            "revision": self.revision,
            "accounts": accounts,
            "account_count": len(accounts),
            "pending_credential_cleanups": pending_cleanups,
        }

    def reload(self) -> dict[str, Any]:
        try:
            loaded = self._store.read(
                default={"version": 1, "accounts": [], "pending_credential_cleanups": []}
            )
        except PersistenceError as exc:
            raise RelayAccountsError(safe_exception_message(exc)) from None
        if not isinstance(loaded, Mapping):
            raise RelayAccountsError("Relay account storage is invalid")
        raw_accounts = loaded.get("accounts", [])
        if not isinstance(raw_accounts, list) or len(raw_accounts) > MAX_ACCOUNTS:
            raise RelayAccountsError("Relay account storage is invalid")
        accounts = [_reloaded_account(item) for item in raw_accounts if isinstance(item, Mapping)]
        if len(accounts) != len(raw_accounts) or len({item["id"] for item in accounts}) != len(accounts):
            raise RelayAccountsError("Relay account storage is invalid")
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
        self._accounts = accounts
        self._pending_credential_cleanups = pending_cleanups
        self.revision += 1
        return self.snapshot()

    def is_read_only_action(self, action: str, payload: object | None = None) -> bool:
        """Tell CoreStore that relay-type detection never stages account data."""

        del payload
        name = str(action).strip().lower().replace("-", "_").replace(".", "_")
        return name in {"detect_type", "account_detect_type", "relay_detect_type"}

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
            account = _private_account(
                {
                    "id": uuid.uuid4().hex,
                    "type": account_type,
                    "label": data.get("label"),
                    "origin": data.get("origin"),
                    "username": data.get("username", ""),
                    "login_status": "signed_out",
                    "remember_password": data.get("remember_password") is True,
                    "resource_status": "idle",
                    "resources": [],
                }
            )
            self._accounts.append(account)
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
            self._accounts.pop(self._index(account["id"]))
            self._session_secrets.pop(account["id"], None)
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
            for key in ("label", "origin", "username"):
                if key in data:
                    current[key] = data[key]
            if "remember_password" in data:
                current["remember_password"] = data["remember_password"] is True
            self._accounts[index] = _private_account(current)
            if "remember_password" in data:
                if current["remember_password"]:
                    self._pending_credential_cleanups = [
                        cleanup
                        for cleanup in self._pending_credential_cleanups
                        if not (cleanup["account_id"] == current["id"] and cleanup["kind"] == "password")
                    ]
                else:
                    self._retain_pending_cleanup(
                        account_id=current["id"],
                        label=current["label"],
                        kind="password",
                    )
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
        return self.snapshot()

    def secret_present(self, field: str, target: str | None = None) -> bool:
        if field != "session" or target is None:
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
        self._accounts[index] = _private_account(account)
        self._persist()
        self.revision += 1

    def accept_login_result(
        self,
        account_id: str,
        *,
        username: str,
        cookie: str = "",
        access_token: str = "",
        refresh_token: str = "",
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
                "resource_status": "idle",
                "resources": [],
            }
        )
        self._accounts[index] = _private_account(account)
        self._session_secrets[account_id] = secrets
        self._persist()
        self.revision += 1
        return _public_account(self._accounts[index])

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
        account["resource_status"] = "idle"
        account["resources"] = []
        self._accounts[index] = _private_account(account)
        self._session_secrets.pop(account_id, None)
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
        tokens = _json_data(self._http.json(account["origin"], "/api/token/?p=0&page_size=100", headers=headers))
        if isinstance(tokens, Mapping):
            tokens = tokens.get("items", [])
        if not isinstance(tokens, Sequence) or isinstance(tokens, (str, bytes, bytearray)):
            raise RelayAccountsError("Relay API key list is invalid")
        resources: list[dict[str, Any]] = []
        for index, item in enumerate(tokens):
            if not isinstance(item, Mapping) or int(item.get("status", 0)) != 1:
                continue
            token_id = item.get("id")
            if type(token_id) is not int:
                continue
            resource_id, name = self._resource_label(item, index, prefix="newapi")
            resources.append(
                {
                    "id": resource_id,
                    "name": name,
                    "api_base": f"{account['origin'].rstrip('/')}/v1",
                    "key_hint": self._key_hint(item.get("key")),
                    "models": models,
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
        channels = self._http.json(account["origin"], "/api/v1/channels/available", headers=headers)
        models = _sub2api_channel_models(channels)
        resources: list[dict[str, Any]] = []
        for index, item in enumerate(keys):
            if not isinstance(item, Mapping) or str(item.get("status", "")).lower() not in {"active", "enabled"}:
                continue
            key = item.get("key")
            if not isinstance(key, str) or not key.strip():
                continue
            resource_id, name = self._resource_label(item, index, prefix="sub2api")
            resources.append(
                {
                    "id": resource_id,
                    "name": name,
                    "api_base": f"{account['origin'].rstrip('/')}/v1",
                    "key_hint": self._key_hint(key),
                    "models": models,
                    "_key": key.strip(),
                }
            )
        return resources

    def refresh_resources(self, account_id: str) -> dict[str, Any]:
        """Load selectable metadata after native login without staging providers."""

        index = self._index(account_id)
        account = self._accounts[index]
        if account["login_status"] != "signed_in":
            raise RelayAccountsError("Relay login is unavailable")
        try:
            private_resources = (
                self._newapi_resources(account)
                if account["type"] == "newapi"
                else self._sub2api_resources(account)
            )
            resources = _safe_resources(private_resources)
            if not resources or not any(resource["models"] for resource in resources):
                raise RelayAccountsError("Relay has no available API resources")
        except RelayAccountsError as exc:
            if "expired" in str(exc).lower():
                account["login_status"] = "expired"
                self._session_secrets.pop(account_id, None)
            account["resource_status"] = "unavailable"
            account["resources"] = []
            self._accounts[index] = _private_account(account)
            self._persist()
            self.revision += 1
            return _public_account(self._accounts[index])
        account["resources"] = resources
        account["resource_status"] = "ready"
        self._accounts[index] = _private_account(account)
        self._persist()
        self.revision += 1
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
        if account["type"] == "sub2api":
            keys = _json_data(self._http.json(account["origin"], "/api/v1/keys?page=1&page_size=100", headers=self._headers(account)))
            if isinstance(keys, Mapping):
                keys = keys.get("items", [])
            if not isinstance(keys, Sequence) or isinstance(keys, (str, bytes, bytearray)):
                raise RelayAccountsError("Relay API key list is invalid")
            wanted_name = resource["name"]
            candidates = [item for item in keys if isinstance(item, Mapping) and str(item.get("status", "")).lower() in {"active", "enabled"}]
            candidate = next((item for item in candidates if _resource_name(item.get("name", item.get("label", item.get("id"))), "") == wanted_name), None)
            if candidate is None and len(candidates) == 1:
                candidate = candidates[0]
            key = candidate.get("key") if isinstance(candidate, Mapping) else None
            if not isinstance(key, str) or not key.strip():
                raise RelayAccountsError("Relay API key is unavailable")
            return key.strip()
        token_id_text = resource["id"].removeprefix("newapi-")
        tokens = _json_data(self._http.json(account["origin"], "/api/token/?p=0&page_size=100", headers=self._headers(account)))
        if isinstance(tokens, Mapping):
            tokens = tokens.get("items", [])
        if not isinstance(tokens, Sequence) or isinstance(tokens, (str, bytes, bytearray)):
            raise RelayAccountsError("Relay API key list is invalid")
        token = next((item for item in tokens if isinstance(item, Mapping) and str(item.get("id")) == token_id_text and int(item.get("status", 0)) == 1), None)
        if not isinstance(token, Mapping) or type(token.get("id")) is not int:
            raise RelayAccountsError("The selected relay API resource is unavailable")
        payload = _json_data(self._http.json(account["origin"], f"/api/token/{token['id']}/key", headers=self._headers(account)))
        key = payload.get("key") if isinstance(payload, Mapping) else None
        if not isinstance(key, str) or not key.strip():
            raise RelayAccountsError("Relay API key is unavailable")
        value = key.strip()
        return value if value.startswith("sk-") else f"sk-{value}"

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
                        "order": 1,
                        "deployment_id": uuid.uuid4().hex[:8],
                        "upstream_url_surface": "openai/responses",
                        "supported_upstream_url_surfaces": ["openai/responses", "openai/chat-completions"],
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

    def import_into(self, account_id: str, providers_domain: object) -> dict[str, Any]:
        """Compatibility wrapper for the one-resource programmatic import path."""

        index = self._index(account_id)
        account = self._accounts[index]
        if account.get("resource_status") != "ready":
            self.refresh_resources(account_id)
            account = self._accounts[index]
        resources = account.get("resources", [])
        if not isinstance(resources, list) or not resources:
            raise RelayAccountsError("Relay has no available API resources")
        return self.import_resources(account_id, [resources[0]["id"]], providers_domain)


__all__ = [
    "ACCOUNT_TYPES",
    "DETECTION_RESPONSE_BYTES",
    "DETECTION_TIMEOUT_SECONDS",
    "DOMAIN_NAME",
    "RelayAccountsDomain",
    "RelayAccountsError",
    "RelayHTTPClient",
]
