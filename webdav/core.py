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
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - surfaced by validate_config()
    yaml = None


APP_NAME = "litellm-menu"
ARCHIVE_VERSION = 1
DEFAULT_REMOTE_NAME = "litellm-menu-config.json"
CONFIG_BUNDLE_FORMAT = "json"
CONFIG_BUNDLE_MAX_BYTES = 16 * 1024 * 1024
MANIFEST_MAX_BYTES = 64 * 1024
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
SENSITIVE_QUERY_KEYS = {"x-vercel-protection-bypass"}
SYNC_STATE_VERSION = 1
DEFAULT_SYNC_INTERVAL_MINUTES = 30
DEFAULT_TIMEOUT_SECONDS = 30.0
WEBDAV_REQUEST_RETRY_ATTEMPTS = 3
WEBDAV_REQUEST_RETRY_DELAY_SECONDS = 1.0


class SyncError(RuntimeError):
    pass


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


def default_root() -> pathlib.Path:
    root = os.environ.get("LITELLM_RUNTIME_ROOT", "").strip()
    if not root:
        root = os.environ.get("LITELLM_MENU_HOME", "").strip()
    if root:
        return pathlib.Path(root).expanduser()
    return pathlib.Path.home() / ".litellm-menu"


def default_config_yaml() -> pathlib.Path:
    config_file = os.environ.get("LITELLM_CONFIG_FILE", "").strip()
    if config_file:
        return pathlib.Path(config_file).expanduser()
    return default_root() / "config.yaml"


def default_settings_file() -> pathlib.Path:
    settings = os.environ.get(SETTINGS_ENV, "").strip()
    if settings:
        return pathlib.Path(settings).expanduser()
    return default_root() / "webdav-sync.json"


def default_state_file() -> pathlib.Path:
    state = os.environ.get("LITELLM_WEBDAV_SYNC_STATE", "").strip()
    if state:
        return pathlib.Path(state).expanduser()
    return default_root() / ".litellm-runtime" / "webdav-sync-state.json"


def disabled_models_path(config_path: pathlib.Path) -> pathlib.Path:
    return config_path.with_name(f"{config_path.stem}.disabled-models.yaml")


def _timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


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
    try:
        text = data.decode("utf-8")
    except Exception as exc:
        raise SyncError(f"{name} is not valid UTF-8: {exc}") from exc

    try:
        from config_editor_core.schema import load_yaml_text

        return load_yaml_text(text, pathlib.Path(name))
    except Exception as exc:
        raise SyncError(f"{name} does not use the current LiteLLM Menu schema: {exc}") from exc


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
    ]


def build_manifest(config_path: pathlib.Path) -> dict[str, Any]:
    if not config_path.exists():
        raise SyncError(f"Missing config file: {config_path}")
    validate_config_bytes("config.yaml", config_path.read_bytes(), "model_list")

    files: list[dict[str, Any]] = []
    for archive_name, path, required, required_key in sync_targets(config_path):
        if not path.exists():
            if required:
                raise SyncError(f"Missing required sync file: {path}")
            files.append({"path": archive_name, "present": False})
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

    return {
        "app": APP_NAME,
        "version": ARCHIVE_VERSION,
        "format": CONFIG_BUNDLE_FORMAT,
        "created_at": _utc_now(),
        "summary": local_summary(config_path),
        "files": files,
    }


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


def manifests_match(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    return _manifest_fingerprints(left) == _manifest_fingerprints(right)


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


def save_sync_state(path: pathlib.Path, settings: Settings, manifest: dict[str, Any], action: str) -> None:
    data = {
        "version": SYNC_STATE_VERSION,
        "updated_at": _utc_now(),
        "action": action,
        "remote_url": _redact_url(bundle_url(settings)),
        "remote_name": settings.remote_name,
        "manifest": manifest,
    }
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))


def baseline_manifest(path: pathlib.Path) -> dict[str, Any] | None:
    state = load_sync_state(path)
    manifest = state.get("manifest")
    return manifest if isinstance(manifest, dict) else None


def create_bundle(config_path: pathlib.Path) -> tuple[bytes, dict[str, Any]]:
    manifest = build_manifest(config_path)
    target_paths = {archive_name: path for archive_name, path, _required, _required_key in sync_targets(config_path)}
    files: list[dict[str, Any]] = []
    for file_info in manifest["files"]:
        entry = dict(file_info)
        if entry.get("present"):
            entry["content"] = target_paths[entry["path"]].read_text(encoding="utf-8")
        files.append(entry)

    bundle = {
        **manifest,
        "format": CONFIG_BUNDLE_FORMAT,
        "files": files,
    }
    return json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"), manifest


def write_config_bundle(config_path: pathlib.Path, output_path: pathlib.Path) -> dict[str, Any]:
    bundle_data, manifest = create_bundle(config_path)
    _atomic_write(output_path, bundle_data)
    return manifest


def _expected_bundle_files() -> dict[str, tuple[bool, str]]:
    return {
        "config.yaml": (True, "model_list"),
        "config.disabled-models.yaml": (False, "disabled_model_list"),
    }


def _validate_bundle_header(manifest: dict[str, Any]) -> None:
    expected_fields = {"app", "version", "format", "created_at", "summary", "files"}
    if set(manifest) != expected_fields:
        raise SyncError("WebDAV sync bundle has unexpected top-level fields")
    if manifest.get("app") != APP_NAME or manifest.get("version") != ARCHIVE_VERSION:
        raise SyncError("WebDAV sync bundle was not created by this LiteLLM Menu version")
    if manifest.get("format") != CONFIG_BUNDLE_FORMAT:
        raise SyncError("WebDAV sync bundle must use the current JSON format")
    if not isinstance(manifest.get("created_at"), str) or not manifest["created_at"].strip():
        raise SyncError("WebDAV sync bundle is missing created_at")


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


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SyncError(f"WebDAV sync bundle has duplicate JSON key: {key}")
        result[key] = value
    return result


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
            if target_path.exists():
                backups.append(_backup(target_path))
            _atomic_write(target_path, files[archive_name])
            installed.append(str(target_path))
        elif required:
            raise SyncError(f"WebDAV sync bundle is missing required file {archive_name}")
        elif target_path.exists():
            backups.append(_backup(target_path))
            target_path.unlink()
            removed.append(str(target_path))

    return {
        "manifest": manifest,
        "installed": installed,
        "removed": removed,
        "backups": backups,
    }


def _quote_remote_name(remote_name: str) -> str:
    return urllib.parse.quote(remote_name, safe="")


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
    path = f"{_remote_collection_path(parsed.path)}{_quote_remote_name(remote_name)}"
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
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
    return _remote_child_url(base, settings.remote_name)


def manifest_url(settings: Settings) -> str:
    url = bundle_url(settings)
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path
    if path.endswith(".json"):
        path = f"{path[:-5]}.manifest.json"
    else:
        path = f"{path}.manifest.json"
    return _remote_with_path(url, path)


def collection_url(settings: Settings) -> str:
    url = settings.url.strip()
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, _remote_collection_path(parsed.path), parsed.query, ""))


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
                "User-Agent": "LiteLLM Menu WebDAV Sync",
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
            "User-Agent": "LiteLLM Menu WebDAV Sync",
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
                config_lines.append(f"data-binary = @{_curl_config_quote(data_path)}")
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

    def put(self, url: str, data: bytes, content_type: str) -> None:
        self.request("PUT", url, data, {"Content-Type": content_type})

    def get(self, url: str, *, max_bytes: int) -> bytes:
        return self.request("GET", url, response_max_bytes=max_bytes)[2]

    def head(self, url: str) -> tuple[int, dict[str, str]]:
        status, headers, _body = self.request("HEAD", url)
        return status, headers

    def delete(self, url: str) -> None:
        self.request("DELETE", url)
