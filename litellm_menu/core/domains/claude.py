"""Claude Code settings domain.

The domain deliberately owns the raw JSON document and exposes only a safe
projection to the UI/core snapshot.  ``dispatch`` changes an in-memory draft;
``apply`` is the only operation that writes the user's file.  The adapter is
usable by the shared Core registry without importing the legacy AppKit shell.
"""

from __future__ import annotations

import copy
import json
import os
import pathlib
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit


DOMAIN_NAME = "claude"
SETTINGS_FILENAME = "settings.json"
DEFAULT_SETTINGS_DIR = ".claude"

RISK_CONFIRMATION_CODES = (
    "bypass_permissions",
    "sandbox_disabled",
    "filesystem_scope_broadened",
    "network_scope_broadened",
    "cowork_egress_all",
)

_SECRET_KEY_MARKERS = (
    "token",
    "key",
    "secret",
    "password",
    "credential",
    "authorization",
)
_PATH_KEY_MARKERS = ("path", "directory", "cwd", "root", "file")
_NETWORK_LIST_KEYS = ("allowed_domains", "alloweddomains", "egress", "allowed_egress")
_PERMISSION_KEYS = ("allow", "ask", "deny")
_SENSITIVE_GATEWAY_QUERY_MARKERS = ("key", "token", "secret", "password", "passwd", "credential", "auth")
_SAFE_ERROR = "Claude Settings could not be loaded"


class ClaudeSettingsError(ValueError):
    """An error safe to send through the local IPC boundary."""


class ConfirmationRequired(ClaudeSettingsError):
    """Apply is denied until the user explicitly acknowledges these risks."""

    def __init__(self, codes: Sequence[str]):
        self.codes = tuple(dict.fromkeys(str(code) for code in codes if code in RISK_CONFIRMATION_CODES))
        super().__init__("Explicit confirmation is required for the selected Claude permissions")


@dataclass(frozen=True)
class ClaudeDeployment:
    """The public deployment fields permitted in Claude's environment."""

    model: str
    base_url: str
    # The token is staged through the authenticated native secret endpoint.
    # Keeping it optional here lets the shared UI stage the public connection
    # fields without ever placing a credential in an ordinary IPC payload.
    token: str | None


def _mapping(value: object, label: str = "settings") -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ClaudeSettingsError(f"{label} must be a JSON object")
    return dict(value)


def _string(value: object, label: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ClaudeSettingsError(f"{label} must be a non-empty string")
        return None
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
        raise ClaudeSettingsError(f"{label} must be a non-empty single-line string")
    return value.strip()


def _string_list(value: object, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ClaudeSettingsError(f"{label} must be a list of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or "\n" in item or "\r" in item:
            raise ClaudeSettingsError(f"{label} must be a list of strings")
        result.append(item.strip())
    return result


def _is_http_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not parsed.username and not parsed.password


def _public_gateway_url(value: object) -> str | None:
    """Return a gateway endpoint only when it is safe for the public snapshot."""

    if not isinstance(value, str) or not _is_http_url(value):
        return None
    parsed = urlsplit(value)
    if parsed.fragment:
        return None
    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        if any(marker in key.lower() for marker in _SENSITIVE_GATEWAY_QUERY_MARKERS):
            return None
    return value


def _secret_key(key: object) -> bool:
    text = str(key).lower()
    return any(marker in text for marker in _SECRET_KEY_MARKERS)


def _path_key(key: object) -> bool:
    text = str(key).lower()
    return any(marker in text for marker in _PATH_KEY_MARKERS)


def redact(value: object, *, _key: object = "") -> object:
    """Return a JSON-safe projection with secrets and local paths removed."""

    if _secret_key(_key):
        if value in (None, "", False):
            return value
        return "configured"
    if _path_key(_key):
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return ["configured"] if value else []
        return "configured" if value else value
    if str(_key).lower() in _NETWORK_LIST_KEYS:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return ["configured"] if value else []
        return "configured" if value else value
    if isinstance(value, Mapping):
        return {str(key): redact(item, _key=key) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item) for item in value]
    return value


def _safe_read(path: pathlib.Path) -> tuple[str, bool]:
    try:
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise ClaudeSettingsError(_SAFE_ERROR)
        return path.read_text(encoding="utf-8"), True
    except FileNotFoundError:
        return "{}\n", False
    except (OSError, UnicodeError):
        raise ClaudeSettingsError(_SAFE_ERROR) from None


def _current_bytes(path: pathlib.Path) -> bytes | None:
    """Read the current regular file for an optimistic Apply check."""

    try:
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise ClaudeSettingsError("Claude Settings changed on disk; reload before applying")
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except ClaudeSettingsError:
        raise
    except OSError:
        raise ClaudeSettingsError("Claude Settings could not be saved") from None


def _atomic_write(path: pathlib.Path, text: str) -> None:
    try:
        try:
            details = path.lstat()
        except FileNotFoundError:
            details = None
        if details is not None and (stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode)):
            raise ClaudeSettingsError("Claude Settings could not be saved")
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    except ClaudeSettingsError:
        raise
    except OSError:
        raise ClaudeSettingsError("Claude Settings could not be saved") from None


def default_settings_path() -> pathlib.Path:
    explicit = os.environ.get("CLAUDE_SETTINGS_PATH", "").strip()
    if explicit:
        return pathlib.Path(explicit).expanduser()
    root = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if root:
        root_path = pathlib.Path(root).expanduser()
        return root_path if root_path.suffix.lower() == ".json" else root_path / SETTINGS_FILENAME
    return pathlib.Path.home() / DEFAULT_SETTINGS_DIR / SETTINGS_FILENAME


def _normalise_permissions(settings: Mapping[str, Any]) -> dict[str, Any]:
    permissions = settings.get("permissions", {})
    if permissions is None:
        permissions = {}
    permissions = _mapping(permissions, "permissions")
    result: dict[str, list[str]] = {}
    for key in _PERMISSION_KEYS:
        result[key] = _string_list(permissions.get(key), f"permissions.{key}")
    for key in ("mode", "defaultMode", "default_mode"):
        if key in permissions:
            result[key] = _string(permissions[key], f"permissions.{key}") or ""
    return result


def _normalise_sandbox(settings: Mapping[str, Any]) -> dict[str, Any]:
    value = settings.get("sandbox", {})
    if value is None:
        value = {}
    if isinstance(value, bool):
        return {"enabled": value}
    sandbox = _mapping(value, "sandbox")
    result: dict[str, Any] = {}
    if "enabled" in sandbox and not isinstance(sandbox["enabled"], bool):
        raise ClaudeSettingsError("sandbox.enabled must be true or false")
    if "mode" in sandbox:
        result["mode"] = _string(sandbox["mode"], "sandbox.mode")
    if "enabled" in sandbox:
        result["enabled"] = sandbox["enabled"]
    for key in ("allowed_paths", "writable_paths", "read_only_paths"):
        if key in sandbox:
            result[key] = _string_list(sandbox[key], f"sandbox.{key}")
    for section_name in ("filesystem", "bash"):
        section = sandbox.get(section_name)
        if section is None:
            continue
        section_map = _mapping(section, f"sandbox.{section_name}")
        child: dict[str, Any] = {}
        if "enabled" in section_map and not isinstance(section_map["enabled"], bool):
            raise ClaudeSettingsError(f"sandbox.{section_name}.enabled must be true or false")
        if "enabled" in section_map:
            child["enabled"] = section_map["enabled"]
        for key in ("allowed_paths", "writable_paths", "read_only_paths"):
            if key in section_map:
                child[key] = _string_list(section_map[key], f"sandbox.{section_name}.{key}")
        result[section_name] = child
    return result


def _normalise_network(settings: Mapping[str, Any]) -> dict[str, Any]:
    value = settings.get("network", settings.get("network_access", {}))
    if value is None:
        value = {}
    if isinstance(value, bool):
        return {"enabled": value}
    network = _mapping(value, "network")
    result: dict[str, Any] = {}
    for key in ("enabled", "allow_all"):
        if key in network and not isinstance(network[key], bool):
            raise ClaudeSettingsError(f"network.{key} must be true or false")
        if key in network:
            result[key] = network[key]
    for key in ("allowed_domains", "allowedDomains", "egress", "allowed_egress"):
        if key in network:
            result[key] = _string_list(network[key], f"network.{key}")
    return result


def validate_settings(settings: Mapping[str, Any]) -> list[str]:
    """Validate editable fields without echoing user values."""

    errors: list[str] = []
    try:
        settings = _mapping(settings)
        for key in ("model", "permissions_mode"):
            if key in settings and settings[key] is not None:
                _string(settings[key], key)
        env = settings.get("env", {})
        env = _mapping(env, "env")
        for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
            if key not in env or env[key] in (None, ""):
                continue
            if not isinstance(env[key], str) or "\n" in env[key] or "\r" in env[key]:
                raise ClaudeSettingsError(f"{key} must be a single-line string")
            if key == "ANTHROPIC_BASE_URL" and not _is_http_url(env[key]):
                raise ClaudeSettingsError("ANTHROPIC_BASE_URL must be an http or https URL")
        permissions = _normalise_permissions(settings)
        for mode_key in ("mode", "defaultMode", "default_mode"):
            mode_value = permissions.get(mode_key)
            if mode_value is not None and mode_value not in {
                "default",
                "acceptEdits",
                "plan",
                "bypassPermissions",
                "dontAsk",
                "bypass",
            }:
                raise ClaudeSettingsError("permissions mode is not supported")
        _normalise_sandbox(settings)
        _normalise_network(settings)
        filesystem = settings.get("filesystem")
        if filesystem is not None and not isinstance(filesystem, (Mapping, bool)):
            raise ClaudeSettingsError("filesystem must be a JSON object or boolean")
        cowork = settings.get("cowork")
        if cowork is not None:
            cowork_map = _mapping(cowork, "cowork")
            for key in ("egress", "allowed_egress", "allowed_domains"):
                if key in cowork_map:
                    _string_list(cowork_map[key], f"cowork.{key}")
        profile = settings.get("desktop_profile")
        if profile is not None:
            _mapping(profile, "desktop_profile")
    except ClaudeSettingsError as exc:
        errors.append(str(exc))
    return errors


def _risk_state(settings: Mapping[str, Any]) -> set[str]:
    risks: set[str] = set()
    permissions = _mapping(settings.get("permissions", {}), "permissions")
    mode = str(
        settings.get("permissions_mode")
        or permissions.get("mode")
        or permissions.get("defaultMode")
        or permissions.get("default_mode")
        or settings.get("permissionMode")
        or ""
    )
    sandbox = settings.get("sandbox", {})
    sandbox_map = {"enabled": sandbox} if isinstance(sandbox, bool) else _mapping(sandbox, "sandbox")
    network = _mapping(settings.get("network", settings.get("network_access", {})), "network")
    paths = []
    for key in ("allowed_paths", "writable_paths"):
        paths.extend(_string_list(sandbox_map.get(key), f"sandbox.{key}"))
    for section_name in ("filesystem", "bash"):
        section = sandbox_map.get(section_name)
        if section is False and section_name == "bash":
            risks.add("sandbox_disabled")
        if isinstance(section, Mapping):
            if section_name == "bash" and section.get("enabled") is False:
                risks.add("sandbox_disabled")
            for key in ("allowed_paths", "writable_paths"):
                paths.extend(_string_list(section.get(key), f"sandbox.{section_name}.{key}"))
    filesystem = settings.get("filesystem", {})
    if filesystem is True:
        risks.add("filesystem_scope_broadened")
    if isinstance(filesystem, Mapping):
        for key in ("allowed_paths", "writable_paths", "roots"):
            paths.extend(_string_list(filesystem.get(key), f"filesystem.{key}"))
    domains = []
    for key in ("allowed_domains", "allowedDomains", "egress", "allowed_egress"):
        domains.extend(_string_list(network.get(key), f"network.{key}"))
    for section_name in ("network", "egress"):
        section = settings.get(section_name)
        if isinstance(section, Mapping):
            for key in ("allowed_domains", "allowedDomains", "egress", "allowed_egress"):
                if section is network and key in network:
                    continue
                domains.extend(_string_list(section.get(key), f"{section_name}.{key}"))
    cowork_domains: list[str] = []
    cowork = settings.get("cowork")
    if isinstance(cowork, Mapping):
        for key in ("egress", "allowed_egress", "allowed_domains"):
            values = _string_list(cowork.get(key), f"cowork.{key}")
            domains.extend(values)
            cowork_domains.extend(values)
    if mode.lower() in {"bypasspermissions", "bypass_permissions", "bypass"}:
        risks.add("bypass_permissions")
    if sandbox_map.get("enabled") is False or str(sandbox_map.get("mode", "")).lower() in {"off", "none", "disabled"}:
        risks.add("sandbox_disabled")
    if any(item in {"*", "/", "**", "/*"} for item in paths):
        risks.add("filesystem_scope_broadened")
    if network.get("allow_all") is True or any(item in {"*", "*/*", "0.0.0.0/0"} for item in domains):
        risks.add("network_scope_broadened")
    if any(item == "*" for item in cowork_domains):
        risks.add("cowork_egress_all")
    return risks


def _deployment(value: object) -> ClaudeDeployment:
    selected = _mapping(value, "deployment")
    model = selected.get("model", selected.get("public_model", selected.get("publicModel")))
    base_url = selected.get("base_url", selected.get("baseUrl"))
    token = selected.get("token", selected.get("auth_token", selected.get("authToken")))
    model_text = _string(model, "deployment.model", required=True)
    base_text = _string(base_url, "deployment.base_url", required=True)
    token_text = _string(token, "deployment.token")
    assert model_text is not None and base_text is not None
    if not _is_http_url(base_text):
        raise ClaudeSettingsError("deployment.base_url must be an http or https URL")
    return ClaudeDeployment(model_text, base_text, token_text)


def apply_litellm_deployment(settings: Mapping[str, Any], deployment: object) -> dict[str, Any]:
    """Return a draft with only Claude's public LiteLLM connection fields changed.

    The source mapping is never mutated.  Existing ``env`` entries—including
    unrelated provider flags—are retained byte-for-byte at the JSON value
    level.  The deployment object must carry a public model id; internal
    provider/deployment ids are intentionally ignored.
    """

    selected = _deployment(deployment)
    updated = copy.deepcopy(_mapping(settings, "settings"))
    env = _mapping(updated.get("env", {}), "env")
    env.update(
        {
            "ANTHROPIC_MODEL": selected.model,
            "ANTHROPIC_BASE_URL": selected.base_url,
        }
    )
    # Direct Core callers may still stage a complete deployment in one local
    # transaction. Native hosts intentionally omit this field and use the
    # one-time secret capability immediately after staging the public fields.
    if selected.token is not None:
        env["ANTHROPIC_AUTH_TOKEN"] = selected.token
    updated["model"] = selected.model
    updated["env"] = env
    return updated


def risk_confirmation_codes(settings: Mapping[str, Any]) -> tuple[str, ...]:
    """Return deterministic confirmation codes for a candidate draft."""

    return tuple(sorted(_risk_state(settings)))


class ClaudeSettingsDomain:
    """One Core-owned Claude settings draft and persistence boundary."""

    name = DOMAIN_NAME

    def __init__(self, settings_path: pathlib.Path | str | None = None, *, loader: Callable[[pathlib.Path], tuple[str, bool]] | None = None):
        self.settings_path = pathlib.Path(settings_path).expanduser() if settings_path else default_settings_path()
        self._loader = loader or _safe_read
        self._raw: dict[str, Any] = {}
        self._draft: dict[str, Any] = {}
        self._exists = False
        self._baseline_bytes: bytes | None = None
        self._revision = 0
        self.reload()

    def reload(self) -> dict[str, Any]:
        text, exists = self._loader(self.settings_path)
        try:
            loaded = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            raise ClaudeSettingsError(_SAFE_ERROR) from None
        if not isinstance(loaded, dict):
            raise ClaudeSettingsError(_SAFE_ERROR)
        if validate_settings(loaded):
            raise ClaudeSettingsError("Claude Settings contains invalid values")
        self._raw = copy.deepcopy(loaded)
        self._draft = copy.deepcopy(loaded)
        self._exists = exists
        self._baseline_bytes = text.encode("utf-8") if exists else None
        self._revision += 1
        return self.snapshot()

    def _safe_projection(self, settings: Mapping[str, Any]) -> dict[str, Any]:
        env = _mapping(settings.get("env", {}), "env")
        permissions = _normalise_permissions(settings)
        sandbox = _normalise_sandbox(settings)
        network = _normalise_network(settings)
        model = settings.get("model")
        if not isinstance(model, str) or not model:
            model = env.get("ANTHROPIC_MODEL") if isinstance(env.get("ANTHROPIC_MODEL"), str) else None
        gateway_url = _public_gateway_url(env.get("ANTHROPIC_BASE_URL"))
        return {
            "model": model if isinstance(model, str) else None,
            "token_configured": bool(env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")),
            "gateway_configured": bool(env.get("ANTHROPIC_BASE_URL")),
            # A plain gateway endpoint is a public connection setting. Query
            # credentials and fragments remain available only in the protected
            # raw editor, never in the ordinary Core snapshot.
            "gateway_url": gateway_url,
            "permissions": permissions,
            "permissions_mode": (
                settings.get("permissions_mode")
                if isinstance(settings.get("permissions_mode"), str)
                else permissions.get("defaultMode", permissions.get("default_mode", permissions.get("mode")))
            ),
            "sandbox": redact(sandbox, _key="sandbox"),
            "network": redact(network, _key="network"),
            "desktop_profile_attached": isinstance(settings.get("desktop_profile"), Mapping),
            "risk_confirmations": sorted(_risk_state(settings)),
            "file_exists": self._exists,
            "revision": self._revision,
        }

    def snapshot(self) -> dict[str, Any]:
        return {"domain": self.name, "revision": self._revision, "settings": self._safe_projection(self._draft)}

    def raw_text(self, *, include_sensitive: bool = False) -> str:
        """Return editor text only when explicitly requested by a trusted UI."""

        value = self._draft if include_sensitive else redact(self._draft)
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def validate(self, payload: object | None = None) -> dict[str, Any]:
        candidate = self._draft if payload is None else _mapping(payload, "settings")
        errors = validate_settings(candidate)
        return {"valid": not errors, "errors": errors}

    def dispatch(self, action: str, payload: object | None = None) -> dict[str, Any]:
        data = _mapping(payload or {}, "payload")
        if action in {"set", "patch"}:
            unknown = set(data).difference({"model", "permissions", "permissions_mode", "sandbox", "network", "network_access", "filesystem", "cowork", "env", "desktop_profile"})
            if unknown:
                raise ClaudeSettingsError("Unknown Claude Settings field")
            for key, value in data.items():
                if key == "env":
                    env = _mapping(self._draft.get("env", {}), "env")
                    env.update(_mapping(value, "env"))
                    self._draft["env"] = env
                elif isinstance(value, Mapping) and isinstance(self._draft.get(key), Mapping):
                    merged = dict(self._draft[key])
                    merged.update(value)
                    self._draft[key] = merged
                else:
                    self._draft[key] = copy.deepcopy(value)
        elif action in {"set_raw", "setRaw"}:
            raw_text = data.get("raw_json", data.get("rawJson", data.get("text")))
            if not isinstance(raw_text, str):
                raise ClaudeSettingsError("Claude Settings JSON must be text")
            try:
                loaded = json.loads(raw_text)
            except json.JSONDecodeError:
                raise ClaudeSettingsError("Claude Settings contains invalid JSON") from None
            if not isinstance(loaded, dict):
                raise ClaudeSettingsError("Claude Settings JSON must be an object")
            self._draft = loaded
        elif action in {"select_deployment", "selectDeployment"}:
            self._draft = apply_litellm_deployment(self._draft, data.get("deployment", data))
        elif action in {"attach_profile", "attachProfile"}:
            self._attach_profile(data.get("path"))
        elif action in {"reset", "cancel", "reload"}:
            self._draft = copy.deepcopy(self._raw)
        else:
            raise ClaudeSettingsError("Unknown Claude Settings action")
        validation = self.validate()
        if not validation["valid"]:
            raise ClaudeSettingsError("Claude Settings contains invalid values")
        self._revision += 1
        return self.snapshot()

    def secret_present(self, field: str, target: str | None = None) -> bool:
        if field != "deployment_token" or target is not None:
            raise ClaudeSettingsError("The requested secret field is unavailable")
        env = _mapping(self._draft.get("env", {}), "env")
        return bool(env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY"))

    def stage_secret(self, field: str, target: str | None, value: str) -> None:
        if field != "deployment_token" or target is not None:
            raise ClaudeSettingsError("The requested secret field is unavailable")
        env = _mapping(self._draft.get("env", {}), "env")
        env["ANTHROPIC_AUTH_TOKEN"] = value
        self._draft["env"] = env
        validation = self.validate()
        if not validation["valid"]:
            raise ClaudeSettingsError("Claude Settings contains invalid values")
        self._revision += 1

    def _attach_profile(self, path_value: object) -> None:
        path_text = _string(path_value, "profile path", required=True)
        assert path_text is not None
        path = pathlib.Path(path_text).expanduser()
        try:
            if path.is_symlink() or not path.is_file():
                raise ClaudeSettingsError("The selected Claude profile is unavailable")
            profile_text = path.read_text(encoding="utf-8")
            profile = json.loads(profile_text)
        except ClaudeSettingsError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ClaudeSettingsError("The selected Claude profile is invalid") from None
        if not isinstance(profile, dict):
            raise ClaudeSettingsError("The selected Claude profile is invalid")
        self._draft["desktop_profile"] = profile

    def apply(self, payload: object | None = None) -> dict[str, Any]:
        data = _mapping(payload or {}, "payload")
        if "raw_json" in data or "rawJson" in data:
            self.dispatch("set_raw", data)
        if "deployment" in data:
            self.dispatch("select_deployment", {"deployment": data["deployment"]})
        if "settings" in data:
            self._draft = copy.deepcopy(_mapping(data["settings"], "settings"))
        validation = self.validate()
        if not validation["valid"]:
            raise ClaudeSettingsError("Claude Settings contains invalid values")
        risks = list(risk_confirmation_codes(self._draft))
        confirmed = data.get("confirm_risks", data.get("confirmRisks", []))
        if "confirmation" in data and not confirmed:
            confirmation = data["confirmation"]
            if confirmation == "accepted" or confirmation == "claude-risk-confirmed":
                confirmed = risks
            elif isinstance(confirmation, str) and confirmation in risks:
                confirmed = [confirmation]
        confirmed_codes = set(_string_list(confirmed, "confirm_risks"))
        missing = [code for code in risks if code not in confirmed_codes]
        if missing:
            raise ConfirmationRequired(missing)
        try:
            text = json.dumps(self._draft, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        except (TypeError, ValueError):
            raise ClaudeSettingsError("Claude Settings contains invalid values") from None
        if _current_bytes(self.settings_path) != self._baseline_bytes:
            raise ClaudeSettingsError("Claude Settings changed on disk; reload before applying")
        _atomic_write(self.settings_path, text)
        self._raw = copy.deepcopy(self._draft)
        self._exists = True
        self._baseline_bytes = text.encode("utf-8")
        self._revision += 1
        return {"applied": True, "domain": self.name, "revision": self._revision, "settings": self._safe_projection(self._draft)}

    def export(self, *, include_sensitive: bool = False) -> dict[str, Any]:
        settings = copy.deepcopy(self._draft)
        return {"domain": self.name, "settings": settings if include_sensitive else redact(settings)}


def create_domain(*args: Any, **kwargs: Any) -> ClaudeSettingsDomain:
    return ClaudeSettingsDomain(*args, **kwargs)


__all__ = [
    "ClaudeDeployment",
    "ClaudeSettingsDomain",
    "ClaudeSettingsError",
    "ConfirmationRequired",
    "DOMAIN_NAME",
    "RISK_CONFIRMATION_CODES",
    "create_domain",
    "default_settings_path",
    "apply_litellm_deployment",
    "risk_confirmation_codes",
    "redact",
    "validate_settings",
]
