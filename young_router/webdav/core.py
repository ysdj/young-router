#!/usr/bin/env python3
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import http.client
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree

from ..atomic_io import atomic_write_bytes
from ..browser_identity import browser_request_headers
from ..json_input import duplicate_key_hook
from dataclasses import dataclass
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - surfaced by validate_config()
    yaml = None


APP_NAME = "young-router"
ARCHIVE_VERSION = 1
DEFAULT_REMOTE_NAME = "young-router-config.json"
#: Names earlier versions gave the remote bundle.  A stored setting that still
#: carries one is read as the current default, and the sync moves the file it
#: pointed at, so the collection keeps one live bundle under this app's name
#: instead of a copy named after the proxy underneath it.
LEGACY_REMOTE_NAMES = ("litellm-config.json",)
CONFIG_BUNDLE_FORMAT = "json"
CONFIG_BUNDLE_MAX_BYTES = 16 * 1024 * 1024
MANIFEST_MAX_BYTES = 64 * 1024
DIGESTS_MAX_BYTES = 64 * 1024
HTTP_ERROR_BODY_MAX_BYTES = 4 * 1024
DEFAULT_RESPONSE_MAX_BYTES = HTTP_ERROR_BODY_MAX_BYTES
RESPONSE_HEADER_MAX_BYTES = 64 * 1024
RESPONSE_READ_CHUNK_BYTES = 64 * 1024
SETTINGS_ENV = "LITELLM_WEBDAV_SYNC_SETTINGS"
URL_ENV = "LITELLM_WEBDAV_URL"
USERNAME_ENV = "LITELLM_WEBDAV_USERNAME"
PASSWORD_ENV = "LITELLM_WEBDAV_PASSWORD"
REMOTE_NAME_ENV = "LITELLM_WEBDAV_REMOTE_NAME"
SYNC_INTERVAL_MINUTES_ENV = "LITELLM_WEBDAV_SYNC_INTERVAL_MINUTES"
REMOTE_FILE_SUFFIXES = (".json",)
#: "The remote file is not there", as servers actually answer it: 404 for a
#: file, 409 when its parent collection is missing as well (measured on 坚果云),
#: and the statuses a server gives when it will not answer HEAD at all.  A probe
#: reads this as "nothing to show yet", not as a broken connection -- creating
#: the collection to make HEAD answer 404 is what left folders behind.
BUNDLE_MISSING_STATUS = frozenset({403, 404, 405, 409, 410, 501})

RELAY_ACCOUNTS_FILE = "relay-accounts.json"
"""The one synced target whose content exists nowhere else."""
SENSITIVE_QUERY_KEYS = {"x-vercel-protection-bypass"}
SYNC_STATE_VERSION = 1
SYNC_DIGESTS_VERSION = 1
DEFAULT_SYNC_INTERVAL_MINUTES = 30
DEFAULT_TIMEOUT_SECONDS = 30.0
WEBDAV_REQUEST_RETRY_ATTEMPTS = 3
WEBDAV_REQUEST_RETRY_DELAY_SECONDS = 1.0


#: The remote file was written by a different app identity, archive version, or
#: bundle format.  It travels to Core's error envelope so a pane can name the
#: cause (and the way out) instead of repeating a generic failure.
INCOMPATIBLE_BUNDLE_CODE = "webdav_sync_incompatible"


class SyncError(RuntimeError):
    #: A cause a pane can name.  ``WebDAVHTTPError`` subclasses this and keeps
    #: its own ``code`` (the HTTP status), so the class default is only used
    #: when a raise asks for something more specific.
    code = "webdav_sync_failed"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class WebDAVHTTPError(SyncError):
    def __init__(self, method: str, url: str, code: int, reason: str, body: bytes = b"") -> None:
        self.method = method
        self.url = url
        self.code = code
        self.reason = reason
        self.body = body
        message = f"{method} {_redact_url(url)} failed: HTTP {code} {reason}"
        if code == 403 and _looks_like_vercel_security_checkpoint(body):
            message = (
                f"{message}\n"
                "Vercel protection rejected the request before it reached WebDAV. "
                "Check that the URL query uses x-vercel-protection-bypass=<secret> "
                "with '=' and that the secret matches this Vercel project."
            )
        super().__init__(message)


def _webdav_request_retry_attempts() -> int:
    return WEBDAV_REQUEST_RETRY_ATTEMPTS


def _webdav_request_retry_delay(attempt: int) -> float:
    return WEBDAV_REQUEST_RETRY_DELAY_SECONDS * attempt


def _is_retryable_webdav_http_error(error: WebDAVHTTPError) -> bool:
    return error.code == 403 and _looks_like_vercel_security_checkpoint(error.body)


def _is_retryable_url_error(error: urllib.error.URLError) -> bool:
    reason = error.reason
    text = str(reason).lower()
    retryable_markers = (
        "unexpected_eof",
        "eof occurred in violation of protocol",
        "connection reset",
        "connection aborted",
        "remote end closed",
        "temporarily unavailable",
        "timed out",
        "timeout",
    )
    return any(marker in text for marker in retryable_markers)


def _curl_binary() -> str:
    configured = os.environ.get("LITELLM_WEBDAV_CURL", "").strip()
    if configured:
        return configured if os.path.exists(configured) else ""
    found = shutil.which("curl")
    if found:
        return found
    return "/usr/bin/curl" if os.path.exists("/usr/bin/curl") else ""


def _curl_config_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "") + '"'


def _parse_curl_header_blocks(data: bytes) -> tuple[str, dict[str, str]]:
    text = data.decode("iso-8859-1", errors="replace").replace("\r\n", "\n")
    blocks = [block for block in text.split("\n\n") if block.strip()]
    if not blocks:
        return "", {}
    lines = blocks[-1].split("\n")
    status_line = lines[0].strip() if lines else ""
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip()] = value.strip()
    return status_line, headers


def _status_reason(status: int, status_line: str = "") -> str:
    parts = status_line.split(None, 2)
    if len(parts) >= 3:
        return parts[2]
    return http.client.responses.get(status, "HTTP Error")


def _read_stream_prefix(stream: Any, max_bytes: int) -> bytes:
    data = bytearray()
    while len(data) < max_bytes:
        chunk = stream.read(min(RESPONSE_READ_CHUNK_BYTES, max_bytes - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def _read_stream_limited(stream: Any, max_bytes: int) -> tuple[bytes, bool]:
    data = bytearray()
    read_limit = max_bytes + 1
    while len(data) < read_limit:
        chunk = stream.read(min(RESPONSE_READ_CHUNK_BYTES, read_limit - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data[:max_bytes]), len(data) > max_bytes


def _read_file_limited(path: str, max_bytes: int) -> tuple[bytes, bool]:
    if not path or not os.path.exists(path):
        return b"", False
    try:
        size_exceeds_limit = os.path.getsize(path) > max_bytes
        with open(path, "rb") as handle:
            data, stream_exceeds_limit = _read_stream_limited(handle, max_bytes)
    except OSError as exc:
        raise SyncError(f"Could not read WebDAV response: {exc}") from exc
    return data, size_exceeds_limit or stream_exceeds_limit


def _response_limit_error(method: str, url: str, max_bytes: int, *, headers: bool = False) -> SyncError:
    kind = "response headers" if headers else "response"
    return SyncError(f"{method} {_redact_url(url)} failed: {kind} exceeds the {max_bytes}-byte limit")


@dataclass(frozen=True)
class Settings:
    url: str
    username: str = ""
    password: str = ""
    remote_name: str = DEFAULT_REMOTE_NAME
    sync_interval_minutes: int = DEFAULT_SYNC_INTERVAL_MINUTES
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @property
    def configured(self) -> bool:
        return bool(self.url.strip())

    def sanitized(self) -> dict[str, Any]:
        return {
            "url": _redact_url(self.url),
            "username": self.username,
            "remote_name": self.remote_name,
            "sync_interval_minutes": self.sync_interval_minutes,
            "timeout_seconds": self.timeout_seconds,
            "has_password": bool(self.password),
        }


def default_root(root: pathlib.Path | str | None = None) -> pathlib.Path:
    """Where this installation keeps the WebDAV identity, unless told.

    A caller that already knows its root passes it.  Otherwise the same two
    variables ``config_paths.runtime_root`` reads name it, defaulting to
    ``~/.young-router``.  Passing the root matters for a second Core started
    with ``--runtime-root``: reading the home directory's settings gave a
    probe instance this machine's WebDAV account, its bundle baseline and its
    enabled flag, so its interval loop pushed the probe document over the live
    remote and every route on the gateway that pulled it disappeared.
    """

    if root is not None:
        return pathlib.Path(root).expanduser()
    configured = os.environ.get("LITELLM_RUNTIME_ROOT", "").strip()
    if not configured:
        configured = os.environ.get("YOUNG_ROUTER_HOME", "").strip()
    if configured:
        return pathlib.Path(configured).expanduser()
    return pathlib.Path.home() / ".young-router"


def default_config_yaml(root: pathlib.Path | str | None = None) -> pathlib.Path:
    config_file = os.environ.get("LITELLM_CONFIG_FILE", "").strip()
    if config_file:
        return pathlib.Path(config_file).expanduser()
    return default_root(root) / "config.yaml"


def default_settings_file(root: pathlib.Path | str | None = None) -> pathlib.Path:
    settings = os.environ.get(SETTINGS_ENV, "").strip()
    if settings:
        return pathlib.Path(settings).expanduser()
    return default_root(root) / "webdav-sync.json"


def default_state_file(root: pathlib.Path | str | None = None) -> pathlib.Path:
    state = os.environ.get("LITELLM_WEBDAV_SYNC_STATE", "").strip()
    if state:
        return pathlib.Path(state).expanduser()
    return default_root(root) / ".litellm-runtime" / "webdav-sync-state.json"


def default_status_file(root: pathlib.Path | str | None = None) -> pathlib.Path:
    status = os.environ.get("LITELLM_WEBDAV_SYNC_STATUS_FILE", "").strip()
    if status:
        return pathlib.Path(status).expanduser()
    return default_root(root) / ".litellm-runtime" / "webdav-sync-status.json"


def disabled_models_path(config_path: pathlib.Path) -> pathlib.Path:
    return config_path.with_name(f"{config_path.stem}.disabled-models.yaml")


def relay_accounts_path(config_path: pathlib.Path) -> pathlib.Path:
    """The relay account document that belongs beside this configuration.

    The configuration lives in the runtime directory itself
    (``<root>/.litellm-runtime/config.yaml``), so joining another
    ``.litellm-runtime`` under its parent names a second document one level
    too deep.  That path is exactly where an old layout left an empty file, so
    appending the directory unconditionally silently pointed every reader at a
    relay list nobody writes: the panes showed no accounts while the real
    document sat beside the configuration.  Append the directory only when the
    configuration is not already inside it.
    """

    parent = config_path.parent
    if parent.name == ".litellm-runtime":
        return parent / "relay-accounts.json"
    return parent / ".litellm-runtime" / "relay-accounts.json"


def _timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def timestamp_epoch(value: Any) -> float | None:
    """Read one of this module's UTC stamps as epoch seconds.

    Callers that schedule work off ``updated_at``/``checked_at`` need a clock,
    not a string; an unreadable stamp is "no record" rather than an error, so a
    hand-edited state file degrades to a fresh sync instead of a crash.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _redact_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        return url
    query = _redact_query(_normalize_sensitive_query_delimiters(parsed.query))
    if not parsed.username and not parsed.password:
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, f"***@{host}", parsed.path, query, parsed.fragment))


def _redact_query(query: str) -> str:
    if not query:
        return query
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    redacted: list[tuple[str, str]] = []
    for key, value in pairs:
        redacted.append((key, "***" if _query_key_is_sensitive(key) else value))
    return urllib.parse.urlencode(redacted, doseq=True, safe="*")


def _query_key_is_sensitive(key: str) -> bool:
    clean = key.lower()
    if clean in SENSITIVE_QUERY_KEYS:
        return True
    return any(marker in clean for marker in ("secret", "token", "password", "passwd", "apikey", "api_key"))


def _normalize_url_query(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = _normalize_sensitive_query_delimiters(parsed.query)
    if query == parsed.query:
        return url
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))


def _normalize_sensitive_query_delimiters(query: str) -> str:
    if not query:
        return query
    parts = query.split("&")
    normalized_parts = [_normalize_sensitive_query_part(part) for part in parts]
    if normalized_parts == parts:
        return query
    return "&".join(normalized_parts)


def _normalize_sensitive_query_part(part: str) -> str:
    lower = part.lower()
    for key in SENSITIVE_QUERY_KEYS:
        prefix = f"{key}:"
        if lower.startswith(prefix):
            return f"{part[:len(key)]}={part[len(key) + 1:]}"
    return part


def _looks_like_vercel_security_checkpoint(body: bytes) -> bool:
    if not body:
        return False
    text = body[:4096].decode("utf-8", errors="ignore").lower()
    return "vercel security checkpoint" in text


def _strip_url_userinfo(url: str, username: str, password: str) -> tuple[str, str, str]:
    parsed = urllib.parse.urlsplit(url)
    next_username = username
    next_password = password
    if parsed.username and not next_username:
        next_username = urllib.parse.unquote(parsed.username)
    if parsed.password and not next_password:
        next_password = urllib.parse.unquote(parsed.password)
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        url = urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))
    return url, next_username, next_password


def _normalize_sync_interval_minutes(value: Any) -> int:
    if value is None or value == "":
        return DEFAULT_SYNC_INTERVAL_MINUTES
    try:
        minutes = int(value)
    except (TypeError, ValueError) as exc:
        raise SyncError("sync_interval_minutes must be a whole number of minutes") from exc
    if minutes < 0:
        raise SyncError("sync_interval_minutes must be 0 or greater")
    return min(minutes, 24 * 60)


def _normalize_timeout_seconds(value: Any) -> float:
    if value is None or value == "":
        return DEFAULT_TIMEOUT_SECONDS
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise SyncError("timeout_seconds must be a number of seconds") from exc
    if not 1 <= seconds <= 600:
        raise SyncError("timeout_seconds must be between 1 and 600")
    return seconds


def _settings_from_raw(raw: dict[str, Any]) -> Settings:
    url = str(raw.get("url", "") or "").strip()
    username = str(raw.get("username", "") or "").strip()
    password = str(raw.get("password", "") or "")
    remote_name = str(raw.get("remote_name", "") or DEFAULT_REMOTE_NAME).strip() or DEFAULT_REMOTE_NAME
    if remote_name in LEGACY_REMOTE_NAMES:
        remote_name = DEFAULT_REMOTE_NAME
    sync_interval_minutes = _normalize_sync_interval_minutes(
        raw.get("sync_interval_minutes", DEFAULT_SYNC_INTERVAL_MINUTES)
    )
    timeout_seconds = _normalize_timeout_seconds(
        raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    )
    if "/" in remote_name or remote_name in {".", ".."}:
        raise SyncError("remote_name must be a file name, not a path")
    if not remote_name.lower().endswith(".json"):
        raise SyncError("remote_name must end with .json")
    if url:
        url = _normalize_url_query(url)
        url, username, password = _strip_url_userinfo(url, username, password)
    return Settings(
        url=url,
        username=username,
        password=password,
        remote_name=remote_name,
        sync_interval_minutes=sync_interval_minutes,
        timeout_seconds=timeout_seconds,
    )


def load_settings(path: pathlib.Path) -> Settings:
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SyncError(f"Invalid WebDAV sync settings JSON: {path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise SyncError(f"WebDAV sync settings must be a JSON object: {path}")
        raw.update(loaded)

    env_overrides = {
        "url": os.environ.get(URL_ENV),
        "username": os.environ.get(USERNAME_ENV),
        "password": os.environ.get(PASSWORD_ENV),
        "remote_name": os.environ.get(REMOTE_NAME_ENV),
        "sync_interval_minutes": os.environ.get(SYNC_INTERVAL_MINUTES_ENV),
    }
    for key, value in env_overrides.items():
        if value is not None:
            raw[key] = value
    return _settings_from_raw(raw)


def save_settings(path: pathlib.Path, settings: Settings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(
        {
            "url": settings.url,
            "username": settings.username,
            "password": settings.password,
            "remote_name": settings.remote_name,
            "sync_interval_minutes": settings.sync_interval_minutes,
            "timeout_seconds": settings.timeout_seconds,
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    _atomic_write(path, data)


def _atomic_write(path: pathlib.Path, data: bytes) -> None:
    atomic_write_bytes(path, data)


def _relay_session(account: object) -> dict[str, str]:
    """The remembered browser session one account holds, if any.

    A cookie or access token is what lets a client read the station without
    signing in again, so it is exactly the material a sync must carry between
    clients.  Anything else in the session field is ignored: the merge only
    ever copies what it can prove is a usable session.
    """

    if not isinstance(account, dict):
        return {}
    session = account.get("session")
    if not isinstance(session, dict):
        return {}
    return {
        key: value
        for key in ("cookie", "access_token", "refresh_token")
        if isinstance((value := session.get(key)), str) and value
    }


def _relay_has_session(account: object) -> bool:
    session = _relay_session(account)
    return bool(session.get("cookie") or session.get("access_token"))


def _relay_rows_by_id(document: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    """One document's rows of ``key``, keyed by their id; unreadable rows drop."""

    rows = document.get(key)
    if not isinstance(rows, list):
        return {}
    return {
        row["id"]: row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]
    }


def _relay_tombstones(document: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    """One document's removal tombstones, keyed by the id they name."""

    rows = document.get(key)
    if not isinstance(rows, list):
        return {}
    return {
        row["id"]: row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]
    }


def _relay_row_is_removed(
    row: dict[str, Any],
    removed_stations: dict[str, dict[str, Any]],
    removed_accounts: dict[str, dict[str, Any]],
) -> bool:
    """Whether a removal tombstone already names this account or its station."""

    account_id = row.get("id")
    if isinstance(account_id, str) and account_id in removed_accounts:
        return True
    station_id = row.get("station_id")
    return isinstance(station_id, str) and station_id in removed_stations


def _merge_relay_accounts(local: bytes, incoming: bytes) -> bytes | None:
    """Keep this Mac's accounts, its freshest sessions, and every removal.

    The relay store is the only synced file whose content exists nowhere else:
    it carries the stations, their saved sessions, and the plaintext passwords
    that make every bound key readable again.  Three kinds of local state the
    incoming file would otherwise erase are therefore preserved:

    * An account that exists only in the local file is kept — the incoming
      bundle is usually simply older, and an outbound run on another machine
      has no way to know about a station added here.
    * For an account both sides hold, the side with the more recent login
      observation keeps its session.  A client that just signed in stamps
      ``login_observed_at``; the other client's older bundle must not overwrite
      that session, or the freshly logged-in client is forced to sign in again
      on its next read — which is exactly the repeated post-login that fills a
      station's concurrent-session cap.  When neither side has a usable
      session, or the observations cannot be ordered, the incoming copy wins as
      before.
    * A deletion is content, not the absence of content.  The store names the
      station and account ids it removed (``removed_stations`` /
      ``removed_accounts``), because comparing documents cannot tell "this Mac
      never had it" from "this Mac deleted it" — which is how a deleted station
      came back from another machine's older bundle, twice over.  A tombstone
      here drops the incoming rows it names, and a tombstone the incoming copy
      carries drops the local rows it names, so a removal made on either side
      lands on the other.  The two lists are kept, not spent once the copies
      agree: a stale peer is exactly the copy that agrees today and offers the
      row back tomorrow.

    Returns ``None`` when the merged document says exactly what the incoming
    one says, so the caller installs it verbatim and this stays a no-op in the
    ordinary case.

    Nothing is invented: every kept record and every kept session is copied
    from the local file, and the document keeps the local ``version``.  A
    remote copy that adds accounts or carries a newer session still lands, so
    this can only ever preserve local state, never invent it.
    """

    try:
        local_document = json.loads(local.decode("utf-8"))
        incoming_document = json.loads(incoming.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(local_document, dict) or not isinstance(incoming_document, dict):
        return None

    local_accounts = _relay_rows_by_id(local_document, "accounts")
    incoming_accounts = _relay_rows_by_id(incoming_document, "accounts")
    local_stations = _relay_rows_by_id(local_document, "stations")
    incoming_stations = _relay_rows_by_id(incoming_document, "stations")
    local_removed_stations = _relay_tombstones(local_document, "removed_stations")
    local_removed_accounts = _relay_tombstones(local_document, "removed_accounts")
    incoming_removed_stations = _relay_tombstones(incoming_document, "removed_stations")
    incoming_removed_accounts = _relay_tombstones(incoming_document, "removed_accounts")
    if not local_accounts and not local_stations and not (local_removed_stations or local_removed_accounts):
        # Nothing local to preserve and nothing local to defend: the incoming
        # copy is the answer.
        return None

    # A removal recorded on one side suppresses that id on the other.  The two
    # directions are deliberately symmetric: the other machine's deletion is
    # as final as this one's.
    kept_incoming_accounts = {
        account_id: account
        for account_id, account in incoming_accounts.items()
        if not _relay_row_is_removed(account, local_removed_stations, local_removed_accounts)
    }
    kept_local_accounts = {
        account_id: account
        for account_id, account in local_accounts.items()
        if not _relay_row_is_removed(account, incoming_removed_stations, incoming_removed_accounts)
    }
    kept_incoming_stations = {
        station_id: station
        for station_id, station in incoming_stations.items()
        if station_id not in local_removed_stations
    }
    kept_local_stations = {
        station_id: station
        for station_id, station in local_stations.items()
        if station_id not in incoming_removed_stations
    }

    # Accounts this Mac holds that the incoming copy would drop are always kept.
    locally_only = set(kept_local_accounts) - set(kept_incoming_accounts)
    # An account both sides hold whose local session is the newer one is kept
    # too: replacing it with the incoming copy would throw away a login this
    # client just proved, and force a sign-in the station may refuse.
    fresher_local: set[str] = set()
    for account_id, local_account in kept_local_accounts.items():
        incoming_account = kept_incoming_accounts.get(account_id)
        if incoming_account is None:
            continue
        if not _relay_has_session(local_account):
            continue
        local_observed = local_account.get("login_observed_at")
        incoming_observed = incoming_account.get("login_observed_at")
        if not (isinstance(local_observed, str) and local_observed and isinstance(incoming_observed, str) and incoming_observed):
            # Without two comparable observations there is no basis to prefer
            # the local session over the incoming one.
            continue
        if local_observed <= incoming_observed:
            continue
        fresher_local.add(account_id)

    keep_ids = locally_only | fresher_local
    kept_account_rows = [
        account
        for account in local_document.get("accounts", [])
        if isinstance(account, dict)
        and account.get("id") in keep_ids
        and account.get("id") in kept_local_accounts
    ]
    incoming_account_rows = [
        account
        for account in incoming_document.get("accounts", [])
        if isinstance(account, dict)
        and account.get("id") in kept_incoming_accounts
        and account.get("id") not in fresher_local
    ]
    kept_station_ids = {
        account.get("station_id") for account in kept_account_rows if isinstance(account.get("station_id"), str)
    }
    kept_station_rows = [
        station
        for station in local_document.get("stations", [])
        if isinstance(station, dict)
        and station.get("id") in kept_local_stations
        and station.get("id") not in kept_incoming_stations
        and station.get("id") in kept_station_ids
    ]
    incoming_station_rows = [
        station
        for station in incoming_document.get("stations", [])
        if isinstance(station, dict) and station.get("id") in kept_incoming_stations
    ]

    merged_accounts = [*incoming_account_rows, *kept_account_rows]
    merged_account_station_ids = {
        account.get("station_id") for account in merged_accounts if isinstance(account.get("station_id"), str)
    }
    # A station the merge emptied is as gone as the account that emptied it:
    # the peer removed that account, and keeping an account-less station would
    # leave a group the pane shows as a station nothing reaches.  A station
    # that was already empty in its own copy is the other side's business and
    # is left alone.
    emptied = {
        account.get("station_id")
        for account in [*local_accounts.values(), *incoming_accounts.values()]
        if isinstance(account.get("station_id"), str)
    }

    def _station_survives(station: dict[str, Any]) -> bool:
        station_id = station.get("id")
        return station_id not in emptied or station_id in merged_account_station_ids

    kept_station_rows = [station for station in kept_station_rows if _station_survives(station)]
    incoming_station_rows = [station for station in incoming_station_rows if _station_survives(station)]

    # Both sides' removals are carried, and neither is ever dropped for the
    # other copy agreeing: pruning a tombstone the moment both copies stopped
    # listing the id is exactly what let a stale peer hand it back on the next
    # run, and a stale peer can appear at any time.  The list is bounded by the
    # store's own cap, so keeping it costs a row per id ever removed.
    merged_tombstones: dict[str, list[dict[str, Any]]] = {}
    for key, tombstones in (
        ("removed_stations", {**incoming_removed_stations, **local_removed_stations}),
        ("removed_accounts", {**incoming_removed_accounts, **local_removed_accounts}),
    ):
        merged_tombstones[key] = [tombstones[identifier] for identifier in sorted(tombstones)]

    # A cleanup tombstone belongs to the account it names, and the two kinds
    # sit on opposite sides of it: a ``password`` tombstone is about an account
    # that is still here, while a ``credentials`` tombstone is only valid once
    # the account is gone -- it is the instruction that erases the keychain
    # entry of an account nobody can see any more.  Dropping the second kind is
    # how a merge left credentials behind for good.
    merged_account_ids = {account.get("id") for account in merged_accounts}
    merged_cleanups: list[dict[str, Any]] = []
    for cleanup in [
        *local_document.get("pending_credential_cleanups", []),
        *incoming_document.get("pending_credential_cleanups", []),
    ]:
        if not isinstance(cleanup, dict):
            continue
        owner = cleanup.get("account_id")
        if not isinstance(owner, str):
            continue
        if cleanup.get("kind") == "credentials":
            if owner in merged_account_ids:
                continue
        elif owner not in merged_account_ids or owner in incoming_accounts:
            continue
        merged_cleanups.append(cleanup)
    cleanup_keys = {
        (cleanup.get("account_id"), cleanup.get("kind")) for cleanup in merged_cleanups
    }
    incoming_cleanup_keys = {
        (cleanup.get("account_id"), cleanup.get("kind"))
        for cleanup in incoming_document.get("pending_credential_cleanups", [])
        if isinstance(cleanup, dict)
    }

    # What the merge changes, decided before the document is built: the
    # comparison the sync uses ignores a session on purpose, so it cannot be
    # asked whether a fresher session was kept.
    changed = bool(
        locally_only
        or fresher_local
        or {account.get("id") for account in incoming_account_rows} != set(incoming_accounts)
        or {station.get("id") for station in incoming_station_rows} != set(incoming_stations)
        or {row["id"] for row in merged_tombstones["removed_stations"]} != set(incoming_removed_stations)
        or {row["id"] for row in merged_tombstones["removed_accounts"]} != set(incoming_removed_accounts)
        or cleanup_keys != incoming_cleanup_keys
    )
    if not changed:
        # The merge changed nothing: install the incoming copy verbatim, so the
        # ordinary case stays a plain whole-file replacement.
        return None

    merged = dict(incoming_document)
    merged["stations"] = [*incoming_station_rows, *kept_station_rows]
    merged["accounts"] = merged_accounts
    merged["removed_stations"] = merged_tombstones["removed_stations"]
    merged["removed_accounts"] = merged_tombstones["removed_accounts"]
    merged["pending_credential_cleanups"] = merged_cleanups
    merged.setdefault("version", local_document.get("version", 3))
    return json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8")


def _backup(path: pathlib.Path) -> str:
    backup_path = path.with_name(f"{path.name}.bak-webdav-{_timestamp()}")
    shutil.copy2(path, backup_path)
    try:
        os.chmod(backup_path, 0o600)
    except OSError:
        pass
    return str(backup_path)


def _require_yaml() -> Any:
    if yaml is None:
        raise SyncError("PyYAML is required to validate synced LiteLLM config files")
    return yaml


def _load_yaml_mapping(path: pathlib.Path, text: str | None = None) -> dict[str, Any]:
    parser = _require_yaml()
    if text is None:
        text = path.read_text(encoding="utf-8")
    data = parser.safe_load(text)
    if not isinstance(data, dict):
        raise SyncError(f"{path.name} must be a YAML mapping")
    return data


def validate_config_bytes(name: str, data: bytes, required_key: str) -> dict[str, Any]:
    if name == "relay-accounts.json":
        try:
            loaded = json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
        except Exception as exc:
            raise SyncError(f"{name} is not valid JSON: {exc}") from exc
        if (
            not isinstance(loaded, dict)
            or loaded.get("version") not in {1, 3}
            or not isinstance(loaded.get("accounts"), list)
            or (
                loaded.get("version") == 3
                and (
                    not isinstance(loaded.get("stations"), list)
                    or not isinstance(loaded.get("pending_credential_cleanups"), list)
                )
            )
        ):
            raise SyncError(f"{name} is not a relay account file")
        return loaded
    try:
        text = data.decode("utf-8")
    except Exception as exc:
        raise SyncError(f"{name} is not valid UTF-8: {exc}") from exc

    try:
        from ..config.schema import load_yaml_text

        return load_yaml_text(text, pathlib.Path(name))
    except Exception as exc:
        raise SyncError(f"{name} does not use the current Young Router schema: {exc}") from exc


def local_summary(config_path: pathlib.Path) -> dict[str, int]:
    config = _load_yaml_mapping(config_path)
    disabled_path = disabled_models_path(config_path)
    disabled: dict[str, Any] = {}
    if disabled_path.exists():
        disabled = _load_yaml_mapping(disabled_path)
    providers = config.get("providers") if isinstance(config.get("providers"), dict) else {}
    model_list = config.get("model_list") if isinstance(config.get("model_list"), list) else []
    disabled_list = disabled.get("disabled_model_list") if isinstance(disabled.get("disabled_model_list"), list) else []
    return {
        "providers": len(providers),
        "active_models": len(model_list),
        "disabled_models": len(disabled_list),
    }


def sync_targets(config_path: pathlib.Path) -> list[tuple[str, pathlib.Path, bool, str]]:
    return [
        ("config.yaml", config_path, True, "model_list"),
        ("config.disabled-models.yaml", disabled_models_path(config_path), False, "disabled_model_list"),
        ("relay-accounts.json", relay_accounts_path(config_path), False, "relay_accounts"),
    ]


#: The relay store's own observations, one per station read.  Each machine
#: rewrites them on its own poll -- a balance, the moment of the last read, the
#: session a login proved, the station's catalog -- so they are not content the
#: sync is meant to carry between machines.  Comparing them made two ordinary
#: background refreshes read as "both ends changed": a reader who edited one
#: route got a conflict dialog whose other side was a balance the other machine
#: had merely observed.  The sync compares what a reader set; a session still
#: travels inside a bundle that a real change produced, and each machine keeps
#: the fresher copy of what it observed (``_merge_relay_accounts``).
#:
#: The list is deliberately short and explicit, and it lives here rather than
#: being derived from the relay domain's own schema.  `resources` and `groups`
#: must stay excluded even though a staged rename or group pick is written
#: inside them: the poll rewrites those same two fields from the station's
#: answer, and the journal that names the reader's work
#: (`relay-apply-operations.json`) is not part of the bundle.  Deriving the list
#: from the account schema would pull both back in and turn every station read
#: into an edit again.  A field joining the store later enters the comparison by
#: default, which is the safe direction: an unknown field that only a poll
#: writes can show up as a conflict, never as an edit silently dropped.
RELAY_OBSERVATION_FIELDS = frozenset(
    {
        "balance",
        "groups",
        "last_updated_at",
        "login_observed_at",
        "login_status",
        "resource_error",
        "resource_status",
        "resources",
        "session",
        "station_resource_names",
        "user_id",
    }
)


def _relay_rows(value: object, order: str) -> list[dict[str, Any]]:
    """The object rows of one relay list, in an order every machine agrees on."""

    rows = [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []
    rows.sort(key=lambda row: str(row.get(order, "")))
    return rows


def _durable_relay_bytes(data: bytes) -> bytes | None:
    """The relay store with the observations removed, in a stable order.

    ``None`` when the file is not a JSON object: the caller then compares the
    bytes it has, so an unreadable document never matches a readable one by
    pretending to hold nothing.

    The rows are keyed and sorted here rather than left as the writer emitted
    them, because two machines produce the same content in different orders: a
    station that lists its keys in its own order, a group catalog that arrives
    re-sorted.  Order is the writer's, content is the reader's.
    """

    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    accounts = [
        {key: value for key, value in account.items() if key not in RELAY_OBSERVATION_FIELDS}
        for account in _relay_rows(document.get("accounts"), "id")
    ]
    cleanups = _relay_rows(document.get("pending_credential_cleanups"), "account_id")
    cleanups.sort(key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True))
    # A removal tombstone is content too: two machines write the same removals
    # in whatever order they made them, so the order here is the id's, never
    # the writer's.
    removals = {
        key: _relay_rows(document.get(key), "id")
        for key in ("removed_stations", "removed_accounts")
    }
    # Every field this version does not know is kept: a future durable field
    # must still be able to tell the two ends apart, and only the observations
    # named above are known to be written without a reader's hand.
    durable = {
        key: value
        for key, value in document.items()
        if key not in {
            "accounts",
            "stations",
            "pending_credential_cleanups",
            "removed_stations",
            "removed_accounts",
        }
    }
    durable.update(
        {
            "stations": _relay_rows(document.get("stations"), "id"),
            "accounts": accounts,
            "pending_credential_cleanups": cleanups,
            **removals,
        }
    )
    return json.dumps(durable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def content_digest(name: str, data: bytes) -> str:
    """The digest a sync compares for one file: the reader's content in it.

    A configuration file is compared byte for byte, because its text is what
    the reader applied.  The relay store is compared without the observations
    its own poller writes (``RELAY_OBSERVATION_FIELDS``), so a balance read on
    one machine cannot stand in for an edit on the other.  The raw ``sha256``
    in a manifest keeps describing the bytes, because that is what a bundle
    verifies before it installs anything.
    """

    if name == RELAY_ACCOUNTS_FILE:
        durable = _durable_relay_bytes(data)
        if durable is not None:
            return _sha256_bytes(durable)
    return _sha256_bytes(data)


def bundle_snapshot(manifest: dict[str, Any], files: dict[str, bytes]) -> SyncSnapshot:
    """The side a bundle describes: its manifest and the digests it compares by.

    A bundle carries the bytes its manifest hashed, so the digests are read from
    the same content the manifest names rather than from whatever is on disk
    after the install.  A file the bundle does not carry has no digest, which is
    the same value an absent file has everywhere else in the comparison.
    """

    return SyncSnapshot(
        manifest,
        {
            name: (content_digest(name, files[name]) if name in files else None)
            for name in _expected_bundle_files()
        },
    )


def _sync_snapshot(
    config_path: pathlib.Path,
) -> tuple[dict[str, Any], dict[str, str | None], dict[str, bytes]]:
    """One read of every synced file: its manifest entry, compared digest, bytes.

    The three travel together because the digest that describes the remote side
    has to describe the content this bundle carries.  The relay store is
    rewritten by its own poller, so a second read could describe a balance the
    upload never included -- and then the other machine would compare against a
    snapshot neither side holds.
    """

    if not config_path.exists():
        raise SyncError(f"Missing config file: {config_path}")

    files: list[dict[str, Any]] = []
    digests: dict[str, str | None] = {}
    contents: dict[str, bytes] = {}
    for archive_name, path, required, required_key in sync_targets(config_path):
        if not path.exists():
            if required:
                raise SyncError(f"Missing required sync file: {path}")
            files.append({"path": archive_name, "present": False})
            digests[archive_name] = None
            continue
        data = path.read_bytes()
        validate_config_bytes(archive_name, data, required_key)
        stat_result = path.stat()
        files.append(
            {
                "path": archive_name,
                "present": True,
                "bytes": len(data),
                "sha256": _sha256_bytes(data),
                "mode": stat_result.st_mode & 0o777,
                "modified_at": dt.datetime.fromtimestamp(stat_result.st_mtime, dt.timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )
        digests[archive_name] = content_digest(archive_name, data)
        contents[archive_name] = data

    manifest = {
        "app": APP_NAME,
        "version": ARCHIVE_VERSION,
        "format": CONFIG_BUNDLE_FORMAT,
        "created_at": _utc_now(),
        "summary": local_summary(config_path),
        "files": files,
    }
    return manifest, digests, contents


def local_snapshot(config_path: pathlib.Path) -> SyncSnapshot:
    """This machine's side of a comparison: its manifest and compared digests."""

    manifest, digests, _contents = _sync_snapshot(config_path)
    return SyncSnapshot(manifest, digests)


def _manifest_fingerprints(manifest: dict[str, Any] | None) -> dict[str, str | None]:
    if not isinstance(manifest, dict):
        return {}
    fingerprints: dict[str, str | None] = {}
    files = manifest.get("files")
    if not isinstance(files, list):
        return fingerprints
    for entry in files:
        if not isinstance(entry, dict):
            continue
        name = entry.get("path")
        if not isinstance(name, str):
            continue
        fingerprints[name] = entry.get("sha256") if entry.get("present") else None
    return fingerprints


@dataclass(frozen=True)
class SyncSnapshot:
    """One side of a three-way comparison, as the very bytes it describes.

    The manifest and the compared digests have to travel together: the bytes in
    the manifest are what a bundle verifies, and the digests are what the plan
    attributes a move by.  Handing them around as two arguments is how a caller
    ends up comparing this side's digests against the other side's bytes -- a
    comparison that answers "conflict" without either machine having moved.

    ``digests`` is ``None`` for a side that has none: a manifest published by an
    earlier version, a state file written before digests existed.  That is a
    different value from an empty record, and it is what makes the comparison
    fall back to bytes for exactly that side instead of for all of them.
    """

    manifest: dict[str, Any] | None
    digests: dict[str, str | None] | None = None

    @property
    def fingerprints(self) -> dict[str, str | None]:
        return _manifest_fingerprints(self.manifest)

    def digest_of(self, name: str) -> str | None:
        value = (self.digests or {}).get(name)
        return value if isinstance(value, str) else None

    def matches(self, other: SyncSnapshot) -> bool:
        """Whether this side and ``other`` hold the same content, file by file."""

        return _manifest_entries_match(
            self.fingerprints, other.fingerprints, self.digests, other.digests
        )


def as_snapshot(value: SyncSnapshot | dict[str, Any] | None) -> SyncSnapshot:
    """One side of a comparison, from a snapshot or from a bare manifest.

    A caller that only has a manifest -- the CLI's own summaries, a test that
    builds two sides by hand -- states the side it has, and the comparison then
    answers by bytes for that side, exactly as the digests' absence says it
    should.  Coercing here keeps that reading available without letting a
    manifest's digests arrive at a different call site than its bytes.
    """

    if isinstance(value, SyncSnapshot):
        return value
    return SyncSnapshot(value)


def _fingerprint_matches(
    left: str | None,
    right: str | None,
    left_digest: str | None,
    right_digest: str | None,
) -> bool:
    """Whether one file's content matches, by digest when both sides have one."""

    if isinstance(left_digest, str) and isinstance(right_digest, str):
        return left_digest == right_digest
    return left == right


def _manifest_entries_match(
    left: dict[str, str | None],
    right: dict[str, str | None],
    left_digests: dict[str, str | None] | None,
    right_digests: dict[str, str | None] | None,
) -> bool:
    if set(left) != set(right):
        return False
    for name, left_fingerprint in left.items():
        if not _fingerprint_matches(
            left_fingerprint,
            right[name],
            (left_digests or {}).get(name),
            (right_digests or {}).get(name),
        ):
            return False
    return True


def manifests_match(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
    left_digests: dict[str, str | None] | None = None,
    right_digests: dict[str, str | None] | None = None,
) -> bool:
    """Whether two snapshots hold the same content, one file at a time.

    A compared digest answers for a file when both sides have one for it: that
    is what keeps an observed balance out of the comparison.  Everywhere else
    -- a manifest from an earlier version, an absent file, a record that does
    not belong to this manifest -- the raw ``sha256`` answers, which is exactly
    what this comparison did before digests existed.
    """

    return SyncSnapshot(left, left_digests).matches(SyncSnapshot(right, right_digests))


def _completed_baseline_digests(
    base: SyncSnapshot,
    local: SyncSnapshot,
    remote: SyncSnapshot,
) -> dict[str, str | None] | None:
    """A baseline from an earlier version, with the digests its bytes prove.

    A side whose bytes equal the baseline's holds exactly the baseline content,
    so that side's compared digest is the baseline's.  Filling those in lets
    the first run after an upgrade attribute a move instead of reading the
    relay store's own observations as edits.  A file whose bytes match neither
    side stays unfilled: that baseline content is unknown, and comparing it as
    anything would be a guess.
    """

    if base.manifest is None:
        return base.digests
    completed = dict(base.digests or {})
    for name, base_fingerprint in base.fingerprints.items():
        if isinstance(completed.get(name), str):
            continue
        if not isinstance(base_fingerprint, str):
            continue
        if local.fingerprints.get(name) == base_fingerprint:
            digest = local.digest_of(name)
        elif remote.fingerprints.get(name) == base_fingerprint:
            digest = remote.digest_of(name)
        else:
            continue
        if isinstance(digest, str):
            completed[name] = digest
    return completed


def _newest_modified_epoch(manifest: dict[str, Any] | None) -> float | None:
    """The moment the newest file a snapshot holds was written."""

    if not isinstance(manifest, dict):
        return None
    files = manifest.get("files")
    if not isinstance(files, list):
        return None
    stamps = [
        stamp
        for entry in files
        if isinstance(entry, dict) and entry.get("present")
        for stamp in (timestamp_epoch(entry.get("modified_at")),)
        if stamp is not None
    ]
    return max(stamps) if stamps else None


def newest_snapshot(
    local_manifest: dict[str, Any] | None,
    remote_manifest: dict[str, Any] | None,
) -> str:
    """Which side of a compared pair was written last: ``local`` or ``remote``.

    A remote manifest carries the moment the outbound side built it, which is
    the upload that produced the file; a local manifest carries each file's own
    mtime.  That compares two machines' clocks, so it decides only where no
    baseline exists -- a first sync between two machines, where nothing says
    either side moved.  An unreadable or equal stamp answers ``remote``:
    installing the remote copy backs up what it replaces, while a push leaves
    the remote's version nowhere.
    """

    local_stamp = _newest_modified_epoch(local_manifest)
    remote_stamp = timestamp_epoch(remote_manifest.get("created_at")) if isinstance(remote_manifest, dict) else None
    if local_stamp is None or remote_stamp is None:
        return "remote"
    return "local" if local_stamp > remote_stamp else "remote"


def smart_sync_plan(
    local: SyncSnapshot | dict[str, Any] | None,
    remote: SyncSnapshot | dict[str, Any] | None,
    base: SyncSnapshot | dict[str, Any] | None,
) -> str:
    """What a smart sync does with these snapshots: push, pull, unchanged, or conflict.

    Every caller -- the interval loop, the pane's button, the CLI -- asks this
    one question, and there is no direction to consult.  A side that moved alone
    is the side to take, because that snapshot is the one change this sync is
    meant to carry.  Both sides moving is a real conflict: nothing here can know
    which of the two edits the reader meant to keep, so the run stops and the
    pane shows both sides.  Without a baseline nothing says either side moved;
    the later snapshot wins there, so a first sync between two machines still
    finishes.

    What counts as a move is the compared digest, not the file's bytes: the
    relay store is rewritten whenever a machine reads a balance or refreshes a
    session, and those observations are nobody's edit.  A side with no digests
    (an earlier version's state file, a manifest without a digest record) is
    compared by its bytes, which is what every version before this one did, and
    a baseline saved without digests borrows them wherever a side's bytes prove
    what the baseline held.

    A file the two sides already agree on is not a question, even when both of
    them moved to that same content since the baseline: taking either side keeps
    the reader's work.  The files they disagree on are what decides the
    direction -- one side's moves are the snapshot to carry, both sides' moves
    are a choice the reader has to make.  That rule is the same whether or not
    the two snapshots name the same files, so there is one loop over the union
    of them rather than a bundle-level rule that restates it.
    """

    local = as_snapshot(local)
    remote = as_snapshot(remote)
    base = as_snapshot(base)
    if not isinstance(remote.manifest, dict):
        return "push"
    names = set(local.fingerprints) | set(remote.fingerprints)
    if all(
        _fingerprint_matches(
            local.fingerprints.get(name),
            remote.fingerprints.get(name),
            local.digest_of(name),
            remote.digest_of(name),
        )
        for name in names
    ):
        return "unchanged"
    if base.manifest is None or not isinstance(base.manifest, dict):
        return "pull" if newest_snapshot(local.manifest, remote.manifest) == "remote" else "push"
    base_digests = _completed_baseline_digests(base, local, remote)
    local_moved = False
    remote_moved = False
    for name in sorted(names):
        if _fingerprint_matches(
            local.fingerprints.get(name),
            remote.fingerprints.get(name),
            local.digest_of(name),
            remote.digest_of(name),
        ):
            continue
        local_unchanged = _fingerprint_matches(
            local.fingerprints.get(name),
            base.fingerprints.get(name),
            local.digest_of(name),
            (base_digests or {}).get(name),
        )
        remote_unchanged = _fingerprint_matches(
            remote.fingerprints.get(name),
            base.fingerprints.get(name),
            remote.digest_of(name),
            (base_digests or {}).get(name),
        )
        if local_unchanged and remote_unchanged:
            # Each side still holds the baseline, yet the two sides differ: the
            # bytes and the digests disagree about the same content, which is a
            # damaged comparison rather than an edit.  Ask the reader.
            return "conflict"
        if local_unchanged:
            remote_moved = True
        elif remote_unchanged:
            local_moved = True
        else:
            return "conflict"
    if local_moved and remote_moved:
        return "conflict"
    if local_moved:
        return "push"
    if remote_moved:
        return "pull"
    # The two sides disagree on files that neither moved alone.  Nothing here
    # can attribute them, so the choice is the reader's.
    return "conflict"


def load_sync_state(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SyncError(f"Invalid WebDAV sync state JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SyncError(f"WebDAV sync state must be a JSON object: {path}")
    return data


def save_sync_state(
    path: pathlib.Path,
    settings: Settings,
    snapshot: SyncSnapshot | dict[str, Any],
    action: str,
    digests: dict[str, str | None] | None = None,
) -> None:
    """Record the content both ends agreed on, as the next run will compare it.

    ``digests`` is only for a caller holding the two separately; a snapshot
    already states both halves of its own side.
    """

    if not isinstance(snapshot, SyncSnapshot):
        snapshot = SyncSnapshot(snapshot, digests)
    data: dict[str, Any] = {
        "version": SYNC_STATE_VERSION,
        "updated_at": _utc_now(),
        "action": action,
        "remote_url": _redact_url(bundle_url(settings)),
        "remote_name": settings.remote_name,
        "manifest": snapshot.manifest,
    }
    # The baseline has to say what was compared, not only which bytes were
    # agreed: the next run attributes a move by comparing against this side,
    # and without the digests the relay store's own observations would read as
    # an edit again.  Optional, so a state file an earlier version wrote still
    # loads and a run that has no digests simply compares bytes.
    if snapshot.digests is not None:
        data["digests"] = {
            name: value
            for name, value in snapshot.digests.items()
            if isinstance(name, str) and (value is None or isinstance(value, str))
        }
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))


def save_sync_status(path: pathlib.Path, action: str, ok: bool, code: str | None = None) -> None:
    data: dict[str, Any] = {
        "action": action,
        "checked_at": _utc_now(),
        "ok": bool(ok),
    }
    # A stopped run keeps the cause that stopped it, so the pane can name the
    # step that clears it instead of only saying the run failed.
    if not ok and code:
        data["code"] = code
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))


def read_status_file(path: pathlib.Path) -> dict[str, Any]:
    """Read the last run's outcome, treating an unreadable file as "no record".

    Both the settings pane and the interval loop read this file, so a file that
    is missing, empty, or half-written by a killed process must not raise: the
    caller wants to know whether a run is recorded, not to fail.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def baseline_snapshot(path: pathlib.Path) -> SyncSnapshot:
    """The last agreed snapshot, with the digests its own run compared by.

    One read of the state file answers the whole side: the manifest it agreed
    on and the compared digests saved beside it.  A state file an earlier
    version wrote has the manifest and no digests, which is a side that
    compares by bytes -- not a side that is missing.
    """

    state = load_sync_state(path)
    return SyncSnapshot(_stored_manifest(state), _stored_digests(state))


def _stored_manifest(state: dict[str, Any]) -> dict[str, Any] | None:
    manifest = state.get("manifest")
    return manifest if isinstance(manifest, dict) else None


def _stored_digests(state: dict[str, Any]) -> dict[str, str | None] | None:
    stored = state.get("digests")
    if not isinstance(stored, dict):
        return None
    digests: dict[str, str | None] = {}
    for name, value in stored.items():
        if not isinstance(name, str):
            continue
        if value is not None and not isinstance(value, str):
            return None
        digests[name] = value
    return digests


def default_conflict_file(root: pathlib.Path | str | None = None) -> pathlib.Path:
    """Where a stopped sync keeps the remote side for the pane to compare."""

    configured = os.environ.get("LITELLM_WEBDAV_CONFLICT_FILE", "").strip()
    if configured:
        return pathlib.Path(configured).expanduser()
    return default_root(root) / ".litellm-runtime" / "webdav-conflict.json"


def save_conflict_record(
    path: pathlib.Path,
    files: dict[str, bytes],
    remote_created_at: str | None,
) -> None:
    """Keep the remote side of a diverged sync until somebody chooses.

    The comparison needs both texts, and only the local one is on disk: the
    remote copy is gone from memory the moment the run ends, and the pane that
    shows the diff usually opens later than the automatic run that stopped.  So
    the remote files are written beside the sync state -- private, one record,
    replaced by the next conflict and removed by the next successful sync.  The
    local side is never copied here; it stays the file the reader can already
    open.
    """

    texts: dict[str, str] = {}
    for name, data in files.items():
        try:
            texts[name] = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
    record = json.dumps(
        {
            "version": SYNC_STATE_VERSION,
            "recorded_at": _utc_now(),
            "remote_created_at": remote_created_at,
            "files": texts,
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    if len(record) > CONFIG_BUNDLE_MAX_BYTES * 2:
        # A comparison too large to keep is not a reason to stop offering the
        # choice: the pane can still say which file differs and which side to
        # keep, so the record carries what it can and no texts at all.
        record = json.dumps(
            {
                "version": SYNC_STATE_VERSION,
                "recorded_at": _utc_now(),
                "remote_created_at": remote_created_at,
                "files": {},
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
    _atomic_write(path, record)


def read_conflict_record(path: pathlib.Path) -> dict[str, Any] | None:
    """The kept-aside remote side, or ``None`` when there is no comparison.

    An unreadable or expired record is "nothing to compare", never an error:
    the pane opens on a window that may have been left open across a repair by
    hand, and a missing record must only mean the diff is unavailable.
    """

    try:
        if path.stat().st_size > CONFIG_BUNDLE_MAX_BYTES * 2 + 4096:
            # A record this app did not write never reaches a reader: the cap is
            # the one ``save_conflict_record`` uses, so a hand-made or damaged
            # file reads as "nothing to compare" instead of being parsed into
            # memory under Core's lock.
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    files = data.get("files")
    texts: dict[str, str] = {}
    if isinstance(files, dict):
        for name, text in files.items():
            if isinstance(name, str) and isinstance(text, str):
                texts[name] = text
    recorded_at = data.get("recorded_at")
    remote_created_at = data.get("remote_created_at")
    return {
        "recorded_at": recorded_at if isinstance(recorded_at, str) else "",
        "remote_created_at": remote_created_at if isinstance(remote_created_at, str) else "",
        "files": texts,
    }


def clear_conflict_record(path: pathlib.Path) -> None:
    """Drop the kept-aside remote side once the two ends agree again."""

    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def create_bundle(config_path: pathlib.Path) -> tuple[bytes, SyncSnapshot]:
    """The outbound bundle and the side it describes.

    The content is decoded from the very bytes the manifest hashed.  Reading
    the file a second time with text newline translation would let a CRLF
    document enter a bundle whose ``sha256`` describes different bytes, which
    the reading side rejects as a tampered file.
    """

    manifest, digests, contents = _sync_snapshot(config_path)
    files: list[dict[str, Any]] = []
    for file_info in manifest["files"]:
        entry = dict(file_info)
        if entry.get("present"):
            entry["content"] = contents[entry["path"]].decode("utf-8")
        files.append(entry)

    bundle = {
        **manifest,
        "format": CONFIG_BUNDLE_FORMAT,
        "files": files,
    }
    return (
        json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
        SyncSnapshot(manifest, digests),
    )


def _expected_bundle_files() -> dict[str, tuple[bool, str]]:
    return {
        "config.yaml": (True, "model_list"),
        "config.disabled-models.yaml": (False, "disabled_model_list"),
        "relay-accounts.json": (False, "relay_accounts"),
    }


def _validate_bundle_header(manifest: dict[str, Any]) -> None:
    expected_fields = {"app", "version", "format", "created_at", "summary", "files"}
    if set(manifest) != expected_fields:
        raise SyncError("WebDAV sync bundle has unexpected top-level fields", INCOMPATIBLE_BUNDLE_CODE)
    if manifest.get("app") != APP_NAME or manifest.get("version") != ARCHIVE_VERSION:
        raise SyncError("WebDAV sync bundle was not created by this Young Router version", INCOMPATIBLE_BUNDLE_CODE)
    if manifest.get("format") != CONFIG_BUNDLE_FORMAT:
        raise SyncError("WebDAV sync bundle must use the current JSON format", INCOMPATIBLE_BUNDLE_CODE)
    if not isinstance(manifest.get("created_at"), str) or not manifest["created_at"].strip():
        raise SyncError("WebDAV sync bundle is missing created_at", INCOMPATIBLE_BUNDLE_CODE)


def _read_bundle_manifest_entry(
    raw_entry: Any,
    expected_name: str,
    required: bool,
    required_key: str,
) -> tuple[dict[str, Any], bytes | None]:
    if not isinstance(raw_entry, dict):
        raise SyncError("WebDAV sync bundle file entries must be JSON objects")

    entry = dict(raw_entry)
    if entry.get("path") != expected_name:
        raise SyncError(f"WebDAV sync bundle has an unexpected file entry: {entry.get('path')!r}")
    if not isinstance(entry.get("present"), bool):
        raise SyncError(f"WebDAV sync bundle file {expected_name} needs a boolean present field")
    present = entry["present"]
    if required and not present:
        raise SyncError(f"WebDAV sync bundle is missing required file {expected_name}")

    content = entry.pop("content", None)
    if not present:
        if content is not None:
            raise SyncError(f"WebDAV sync bundle absent file {expected_name} must not contain content")
        unexpected = set(entry) - {"path", "present"}
        if unexpected:
            raise SyncError(f"WebDAV sync bundle absent file {expected_name} has unexpected metadata")
        return entry, None

    if not isinstance(content, str):
        raise SyncError(f"WebDAV sync bundle file {expected_name} is missing text content")
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SyncError(f"WebDAV sync bundle file {expected_name} is not valid UTF-8 text") from exc
    byte_count = entry.get("bytes")
    if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count != len(encoded):
        raise SyncError(f"WebDAV sync bundle file {expected_name} has an invalid byte count")
    digest = entry.get("sha256")
    if not isinstance(digest, str) or digest != _sha256_bytes(encoded):
        raise SyncError(f"WebDAV sync bundle file {expected_name} has an invalid sha256")
    mode = entry.get("mode")
    if not isinstance(mode, int) or isinstance(mode, bool) or not 0 <= mode <= 0o777:
        raise SyncError(f"WebDAV sync bundle file {expected_name} has an invalid mode")
    if not isinstance(entry.get("modified_at"), str) or not entry["modified_at"].strip():
        raise SyncError(f"WebDAV sync bundle file {expected_name} is missing modified_at")
    unexpected = set(entry) - {"path", "present", "bytes", "sha256", "mode", "modified_at"}
    if unexpected:
        raise SyncError(f"WebDAV sync bundle file {expected_name} has unexpected metadata")

    validate_config_bytes(expected_name, encoded, required_key)
    return entry, encoded


_reject_duplicate_json_keys = duplicate_key_hook(
    lambda key: SyncError(f"WebDAV sync bundle has duplicate JSON key: {key}")
)


def read_config_bundle(bundle_data: bytes) -> tuple[dict[str, Any], dict[str, bytes]]:
    if len(bundle_data) > CONFIG_BUNDLE_MAX_BYTES:
        raise SyncError(
            f"WebDAV sync bundle exceeds the {CONFIG_BUNDLE_MAX_BYTES // (1024 * 1024)} MiB limit"
        )
    if not bundle_data.lstrip().startswith(b"{"):
        raise SyncError("WebDAV sync bundle must be JSON")
    try:
        loaded = json.loads(
            bundle_data.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys
        )
    except Exception as exc:
        raise SyncError(f"WebDAV sync bundle is not valid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise SyncError("WebDAV sync bundle must be a JSON object")
    _validate_bundle_header(loaded)

    file_entries = loaded.get("files")
    if not isinstance(file_entries, list):
        raise SyncError("WebDAV sync bundle files must be a list")
    expected_files = _expected_bundle_files()
    if len(file_entries) != len(expected_files):
        raise SyncError("WebDAV sync bundle must contain exactly the current configuration files")

    files: dict[str, bytes] = {}
    manifest_files: list[dict[str, Any]] = []
    entries_by_name: dict[str, Any] = {}
    for raw_entry in file_entries:
        if not isinstance(raw_entry, dict):
            raise SyncError("WebDAV sync bundle file entries must be JSON objects")
        name = raw_entry.get("path")
        if not isinstance(name, str):
            raise SyncError("WebDAV sync bundle file entry is missing path")
        if name not in expected_files:
            raise SyncError(f"Unsafe or unexpected file in WebDAV sync bundle: {name}")
        if name in entries_by_name:
            raise SyncError(f"WebDAV sync bundle has duplicate file entry: {name}")
        entries_by_name[name] = raw_entry

    for name, (required, required_key) in expected_files.items():
        if name not in entries_by_name:
            raise SyncError(f"WebDAV sync bundle is missing file entry: {name}")
        entry, content = _read_bundle_manifest_entry(
            entries_by_name[name], name, required, required_key
        )
        if content is not None:
            files[name] = content
        manifest_files.append(entry)

    summary = loaded.get("summary")
    if not isinstance(summary, dict):
        raise SyncError("WebDAV sync bundle summary must be an object")
    config = validate_config_bytes("config.yaml", files["config.yaml"], "model_list")
    disabled = (
        validate_config_bytes(
            "config.disabled-models.yaml",
            files["config.disabled-models.yaml"],
            "disabled_model_list",
        )
        if "config.disabled-models.yaml" in files
        else {}
    )
    expected_summary = {
        "providers": len(config.get("providers", {})),
        "active_models": len(config.get("model_list", [])),
        "disabled_models": len(disabled.get("disabled_model_list", [])),
    }
    if summary != expected_summary:
        raise SyncError("WebDAV sync bundle summary does not match its configuration files")

    manifest = dict(loaded)
    manifest["files"] = manifest_files
    return manifest, files


def read_config_bundle_file(path: pathlib.Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    try:
        if not path.is_file():
            raise SyncError(f"Configuration package is not a file: {path}")
        if path.stat().st_size > CONFIG_BUNDLE_MAX_BYTES:
            raise SyncError(
                f"Configuration package exceeds the {CONFIG_BUNDLE_MAX_BYTES // (1024 * 1024)} MiB limit"
            )
        return read_config_bundle(path.read_bytes())
    except OSError as exc:
        raise SyncError(f"Could not read configuration package: {exc}") from exc


def install_bundle(bundle_data: bytes, config_path: pathlib.Path) -> dict[str, Any]:
    manifest, files = read_config_bundle(bundle_data)
    if "config.yaml" not in files:
        raise SyncError("WebDAV sync bundle is missing config.yaml")

    validate_config_bytes("config.yaml", files["config.yaml"], "model_list")
    if "config.disabled-models.yaml" in files:
        validate_config_bytes("config.disabled-models.yaml", files["config.disabled-models.yaml"], "disabled_model_list")

    backups: list[str] = []
    installed: list[str] = []
    removed: list[str] = []

    config_path.parent.mkdir(parents=True, exist_ok=True)
    for archive_name, target_path, required, _required_key in sync_targets(config_path):
        if archive_name in files:
            # A bundle is a *whole-file* replacement, and the relay store is the
            # one target whose content is not reconstructible from anything else
            # in it: it holds the stations, their saved sessions, and the
            # plaintext passwords that make every bound key readable again.
            # Accepting a remote copy of it unconditionally is what let an
            # outbound run on another Mac (or an older bundle that predates a
            # station this one just added) silently drop a station that exists
            # only here, with no tombstone and nothing in the UI naming it.
            # The reader is the only side that can see both copies, so it keeps
            # the local station when the incoming one would remove an account
            # this Mac still holds.  Nothing is lost either way: a local file
            # the sync does replace is backed up first, and the skipped
            # accounts stay exactly as they were.
            if target_path.exists():
                local_document = target_path.read_bytes()
                if archive_name == RELAY_ACCOUNTS_FILE:
                    merged = _merge_relay_accounts(local_document, files[archive_name])
                    if merged is not None:
                        backups.append(_backup(target_path))
                        _atomic_write(target_path, merged)
                        installed.append(str(target_path))
                        continue
                if local_document == files[archive_name]:
                    # A pull that repeats unchanged remote content must write
                    # nothing: an interval pull would otherwise leave a fresh
                    # .bak beside the file on every run (one per tick, forever)
                    # and bump the mtime of a configuration nothing changed.
                    continue
                backups.append(_backup(target_path))
            _atomic_write(target_path, files[archive_name])
            installed.append(str(target_path))
        elif required:
            raise SyncError(f"WebDAV sync bundle is missing required file {archive_name}")
        elif target_path.exists():
            # An absent relay store is not an instruction to delete the local
            # one.  The station inventory is the user's own work, and a bundle
            # built before it existed (or by a machine that never had it) says
            # nothing about whether it should still be here.
            if archive_name == RELAY_ACCOUNTS_FILE:
                continue
            backups.append(_backup(target_path))
            target_path.unlink()
            removed.append(str(target_path))

    snapshot = bundle_snapshot(manifest, files)
    return {
        "manifest": manifest,
        # What the remote side holds, in the terms the next sync compares by.
        # Saved as the baseline, so content this machine kept on top of the
        # bundle (a station only it had) still reads as this side's move and
        # gets pushed to the other one.
        "digests": snapshot.digests,
        "snapshot": snapshot,
        "installed": installed,
        "removed": removed,
        "backups": backups,
    }


def _quote_remote_name(remote_name: str) -> str:
    return urllib.parse.quote(remote_name, safe="")


# Reserved characters keep their meaning in a path; ``%`` stays so an address
# pasted in its already-encoded form is not encoded a second time.
_REMOTE_PATH_SAFE = "/%:@!$&'()*+,;=-._~"


def _quote_remote_path(path: str) -> str:
    """Percent-encode a folder path a reader typed or pasted.

    The transport hands the request target to ``http.client``, which encodes it
    as ASCII, so a collection named 我的坚果云 raised ``UnicodeEncodeError``
    before a single byte left the process -- and a folder with a space did the
    same.  Quoting here, where the address is read, is what makes the folder a
    reader can see in their client usable in the URL field at all.
    """

    return urllib.parse.quote(path, safe=_REMOTE_PATH_SAFE)


def _remote_path_is_file(path: str) -> bool:
    return pathlib.PurePosixPath(path).name.lower().endswith(REMOTE_FILE_SUFFIXES)


def _remote_collection_path(path: str) -> str:
    if _remote_path_is_file(path):
        parent = str(pathlib.PurePosixPath(path).parent)
        if parent == ".":
            parent = "/"
        path = parent
    if not path:
        path = "/"
    if not path.endswith("/"):
        path = f"{path}/"
    return path


def _remote_child_url(base: str, remote_name: str) -> str:
    parsed = urllib.parse.urlsplit(base)
    path = f"{_quote_remote_path(_remote_collection_path(parsed.path))}{_quote_remote_name(remote_name)}"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def _remote_with_path(url: str, path: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def bundle_url(settings: Settings) -> str:
    base = settings.url.strip()
    if not base:
        raise SyncError("WebDAV sync is not configured")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in {"http", "https"}:
        raise SyncError("WebDAV URL must start with http:// or https://")
    if parsed.path.lower().endswith((".tar.gz", ".tgz")):
        raise SyncError("WebDAV tar/tgz bundles are no longer supported; use a .json bundle")
    if _remote_path_is_file(parsed.path):
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, _quote_remote_path(parsed.path), parsed.query, "")
        )
    return _remote_child_url(base, settings.remote_name)


def manifest_name(remote_name: str) -> str:
    """The sidecar manifest's name for a bundle name."""
    return f"{remote_name[:-5]}.manifest.json" if remote_name.endswith(".json") else f"{remote_name}.manifest.json"


def manifest_url(settings: Settings) -> str:
    url = bundle_url(settings)
    parsed = urllib.parse.urlsplit(url)
    return _remote_with_path(url, f"{parsed.path.rsplit('/', 1)[0]}/{urllib.parse.quote(manifest_name(settings.remote_name))}")


def digests_name(remote_name: str) -> str:
    """The sidecar a sync compares by: what changed, not which bytes changed.

    It travels beside the manifest and is bound to it.  A version that does not
    know the name never reads it, and a record whose manifest is gone is
    ignored, so a mixed pair of versions falls back to comparing bytes instead
    of trusting digests of a snapshot that is no longer on the server.
    """

    stem = remote_name[:-5] if remote_name.endswith(".json") else remote_name
    return f"{stem}.digests.json"


def digests_url(settings: Settings) -> str:
    url = bundle_url(settings)
    parsed = urllib.parse.urlsplit(url)
    return _remote_with_path(url, f"{parsed.path.rsplit('/', 1)[0]}/{urllib.parse.quote(digests_name(settings.remote_name))}")


def manifest_identity(manifest: dict[str, Any]) -> str:
    """What a digest record names: this one manifest, and no other."""

    payload = {key: manifest.get(key) for key in ("app", "version", "format", "created_at", "summary", "files")}
    return _sha256_bytes(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def build_digest_record(manifest: dict[str, Any], digests: dict[str, str | None]) -> bytes:
    """The record the next sync compares the remote side by."""

    record = {
        "app": APP_NAME,
        "version": SYNC_DIGESTS_VERSION,
        "manifest": manifest_identity(manifest),
        "files": {name: digests.get(name) for name in _expected_bundle_files()},
    }
    return json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


def read_digest_record(data: bytes, manifest: dict[str, Any]) -> dict[str, str | None] | None:
    """The digests that belong to ``manifest``, or ``None``.

    ``None`` means "compare the bytes": a server that never stored the record,
    a file damaged in transit, or one written for another manifest.  Guessing
    here would attribute a change to a snapshot the caller is not looking at,
    which is the one thing a three-way comparison must not do.
    """

    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    if record.get("app") != APP_NAME or record.get("version") != SYNC_DIGESTS_VERSION:
        return None
    if record.get("manifest") != manifest_identity(manifest):
        return None
    files = record.get("files")
    if not isinstance(files, dict) or set(files) != set(_expected_bundle_files()):
        return None
    digests: dict[str, str | None] = {}
    for name, value in files.items():
        if value is None:
            digests[name] = None
        elif isinstance(value, str):
            digests[name] = value
        else:
            return None
    return digests


def archived_bundle_name(remote_name: str, stamp: str | None = None) -> str:
    """The name a kept-aside remote bundle takes.

    It mirrors the local convention (``config.yaml.bak-20260924-195812``): the
    remote copy keeps its stem and extension and carries the moment it was set
    aside, so several replacements never collide and the name still says what
    the file is.
    """
    name = pathlib.PurePosixPath(remote_name.strip() or "config.json").name
    suffixes = "".join(pathlib.PurePosixPath(name).suffixes)
    stem = name[: len(name) - len(suffixes)] if suffixes else name
    return f"{stem}.bak-{stamp or _timestamp()}{suffixes}"


def remote_sibling_url(settings: Settings, name: str) -> str:
    """A URL for another file in the bundle's own collection."""
    url = bundle_url(settings)
    parsed = urllib.parse.urlsplit(url)
    directory = parsed.path.rsplit("/", 1)[0]
    return _remote_with_path(url, f"{directory}/{urllib.parse.quote(name)}")


def collection_url(settings: Settings) -> str:
    url = settings.url.strip()
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, _quote_remote_path(_remote_collection_path(parsed.path)), parsed.query, "")
    )


_DAV_NAMESPACE = "{DAV:}"
_WRITE_PRIVILEGES = frozenset({"write", "write-content", "bind", "all"})


def _collection_privilege_access(body: bytes) -> str:
    """Read RFC 4918 privileges: writable, read_only, or not reported."""

    try:
        root = xml.etree.ElementTree.fromstring(body)
    except xml.etree.ElementTree.ParseError:
        return "unknown"
    granted = {
        child.tag.rpartition("}")[2]
        for privilege in root.iter(f"{_DAV_NAMESPACE}privilege")
        for child in privilege
        if child.tag.startswith(_DAV_NAMESPACE)
    }
    if not granted:
        return "unknown"
    return "writable" if granted & _WRITE_PRIVILEGES else "read_only"


class WebDAVClient:
    def __init__(self, settings: Settings) -> None:
        if not settings.configured:
            raise SyncError("WebDAV sync is not configured")
        self.settings = settings
        self.timeout = settings.timeout_seconds

    def request(
        self,
        method: str,
        url: str,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        response_max_bytes: int = DEFAULT_RESPONSE_MAX_BYTES,
    ) -> tuple[int, dict[str, str], bytes]:
        if (
            not isinstance(response_max_bytes, int)
            or isinstance(response_max_bytes, bool)
            or response_max_bytes < 0
        ):
            raise ValueError("response_max_bytes must be a non-negative integer")
        attempts = _webdav_request_retry_attempts()
        retryable_error: SyncError | None = None
        for attempt in range(1, attempts + 1):
            request_headers = {
                **browser_request_headers(accept="*/*"),
                **(headers or {}),
            }
            if self.settings.username or self.settings.password:
                token = f"{self.settings.username}:{self.settings.password}".encode("utf-8")
                request_headers["Authorization"] = "Basic " + base64.b64encode(token).decode("ascii")
            if data is not None:
                request_headers["Content-Length"] = str(len(data))

            request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    response_headers = dict(response.headers.items())
                    body, oversized = _read_stream_limited(response, response_max_bytes)
                    if oversized:
                        raise _response_limit_error(method, url, response_max_bytes)
                    return response.status, response_headers, body
            except urllib.error.HTTPError as exc:
                body = _read_stream_prefix(exc, HTTP_ERROR_BODY_MAX_BYTES)
                error = WebDAVHTTPError(method, url, exc.code, exc.reason, body)
                if _is_retryable_webdav_http_error(error):
                    retryable_error = error
                    if attempt < attempts:
                        time.sleep(_webdav_request_retry_delay(attempt))
                        continue
                    break
                raise error from exc
            except urllib.error.URLError as exc:
                if _is_retryable_url_error(exc):
                    retryable_error = SyncError(f"{method} {_redact_url(url)} failed: {exc.reason}")
                    if attempt < attempts:
                        time.sleep(_webdav_request_retry_delay(attempt))
                        continue
                    break
                raise SyncError(f"{method} {_redact_url(url)} failed: {exc.reason}") from exc
        if retryable_error is not None:
            return self._curl_request(
                method,
                url,
                data,
                headers,
                retryable_error,
                response_max_bytes,
            )
        raise SyncError(f"{method} {_redact_url(url)} failed")

    def _curl_request(
        self,
        method: str,
        url: str,
        data: bytes | None,
        headers: dict[str, str] | None,
        original_error: SyncError,
        response_max_bytes: int,
    ) -> tuple[int, dict[str, str], bytes]:
        curl = _curl_binary()
        if not curl:
            raise original_error
        request_headers = {
            **browser_request_headers(accept="*/*"),
            **(headers or {}),
        }
        if self.settings.username or self.settings.password:
            token = f"{self.settings.username}:{self.settings.password}".encode("utf-8")
            request_headers["Authorization"] = "Basic " + base64.b64encode(token).decode("ascii")
        if data is not None:
            request_headers["Content-Length"] = str(len(data))

        header_fd, header_path = tempfile.mkstemp(prefix="litellm-webdav-headers-", suffix=".txt")
        config_fd, config_path = tempfile.mkstemp(prefix="litellm-webdav-curl-", suffix=".conf")
        body_fd, body_path = tempfile.mkstemp(prefix="litellm-webdav-body-", suffix=".bin")
        data_path = ""
        try:
            os.close(header_fd)
            os.close(body_fd)
            config_lines = [
                "silent",
                "show-error",
                "location",
                f"request = {_curl_config_quote(method)}",
                f"url = {_curl_config_quote(url)}",
                f"connect-timeout = {_curl_config_quote(str(max(1, min(self.timeout, 120))))}",
                f"max-time = {_curl_config_quote(str(max(1, min(self.timeout, 600))))}",
                f"max-filesize = {_curl_config_quote(str(response_max_bytes + 1))}",
                f"dump-header = {_curl_config_quote(header_path)}",
                f"output = {_curl_config_quote(body_path)}",
                "write-out = \"%{http_code}\"",
            ]
            for key, value in request_headers.items():
                config_lines.append(f"header = {_curl_config_quote(f'{key}: {value}')}" )
            if data is not None:
                data_fd, data_path = tempfile.mkstemp(prefix="litellm-webdav-data-", suffix=".bin")
                try:
                    with os.fdopen(data_fd, "wb") as handle:
                        handle.write(data)
                    os.chmod(data_path, 0o600)
                except Exception:
                    os.close(data_fd)
                    raise
                # curl reads the quoted form as a literal filename that includes the
                # quotes, so it fails with a read error instead of uploading. mkstemp
                # paths never contain spaces, so the bare form is both safe and correct.
                config_lines.append(f"data-binary = @{data_path}")
            with os.fdopen(config_fd, "w", encoding="utf-8") as handle:
                handle.write("\n".join(config_lines))
                handle.write("\n")
            os.chmod(config_path, 0o600)
            result = subprocess.run([curl, "--config", config_path], capture_output=True, check=False)
            status_text = result.stdout.decode("ascii", errors="ignore").strip()[-3:]
            status = int(status_text) if status_text.isdigit() else 0
            header_data, headers_oversized = _read_file_limited(
                header_path, RESPONSE_HEADER_MAX_BYTES
            )
            if headers_oversized:
                raise _response_limit_error(
                    method, url, RESPONSE_HEADER_MAX_BYTES, headers=True
                )
            status_line, response_headers = _parse_curl_header_blocks(header_data)
            if 200 <= status < 300:
                body, oversized = _read_file_limited(body_path, response_max_bytes)
                if oversized or result.returncode == 63:
                    raise _response_limit_error(method, url, response_max_bytes)
                if result.returncode:
                    detail = result.stderr.decode("utf-8", errors="replace").strip()
                    raise SyncError(
                        f"{method} {_redact_url(url)} failed: "
                        f"{detail or 'curl request failed'}"
                    )
                return status, response_headers, body
            if status:
                body, _oversized = _read_file_limited(
                    body_path, HTTP_ERROR_BODY_MAX_BYTES
                )
                raise WebDAVHTTPError(method, url, status, _status_reason(status, status_line), body)
            if result.returncode == 63:
                raise _response_limit_error(method, url, response_max_bytes)
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise SyncError(f"{method} {_redact_url(url)} failed: {detail or 'curl request failed'}")
        finally:
            for file_path in (header_path, config_path, body_path, data_path):
                if file_path:
                    try:
                        os.unlink(file_path)
                    except FileNotFoundError:
                        pass

    def try_mkcol(self, url: str) -> None:
        try:
            self.request("MKCOL", url)
        except WebDAVHTTPError as exc:
            if exc.code in {301, 302, 405, 409, 403}:
                return
            raise
        except SyncError:
            return

    def collection_access(self) -> str:
        """Report what the configured collection allows, without writing to it.

        A probe must leave nothing behind.  Asking "does this URL work" by
        creating the collection means every test leaves a folder on the server,
        and at a mount point the server treats as read-only that folder cannot
        even be removed afterwards: DELETE needs write on the parent, which is
        the very privilege that is missing (measured on 坚果云: ``MKCOL /dav/x``
        is answered 201, ``DELETE /dav/x/`` 403).  So the answer comes from the
        collection's own privilege set (RFC 4918 ``current-user-privilege-set``)
        and is ``unknown`` when the server reports none -- not a guess.
        """

        try:
            _status, _headers, body = self.request(
                "PROPFIND", collection_url(self.settings), headers={"Depth": "0"}
            )
        except WebDAVHTTPError as exc:
            if exc.code == 404:
                return "missing"
            raise
        return _collection_privilege_access(body)

    def put(self, url: str, data: bytes, content_type: str) -> None:
        self.request("PUT", url, data, {"Content-Type": content_type})

    def get(self, url: str, *, max_bytes: int) -> bytes:
        return self.request("GET", url, response_max_bytes=max_bytes)[2]

    def head(self, url: str) -> tuple[int, dict[str, str]]:
        status, headers, _body = self.request("HEAD", url)
        return status, headers

    def delete(self, url: str) -> None:
        self.request("DELETE", url)
