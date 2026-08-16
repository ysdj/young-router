"""Claude Code ``settings.json`` domain.

The domain owns one staged copy of Claude Code's user settings document.  It
only gives the structured UI canonical Claude Code fields; unknown keys remain
in the raw document and survive structured edits unchanged.  Secrets never
leave this adapter through the ordinary Core snapshot.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import pathlib
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

from ..claude_desktop import (
    ClaudeDesktopConfig,
    ClaudeDesktopConfigError,
    ClaudeDeveloperSettings,
)


DOMAIN_NAME = "claude"
SETTINGS_FILENAME = "settings.json"
DEFAULT_SETTINGS_DIR = ".claude"

RISK_CONFIRMATION_CODES = (
    "bypass_permissions",
    "sandbox_disabled",
    "filesystem_scope_broadened",
    "network_scope_broadened",
)

_SECRET_KEY_MARKERS = (
    "token",
    "key",
    "secret",
    "password",
    "credential",
    "authorization",
)
_SENSITIVE_GATEWAY_QUERY_MARKERS = ("key", "token", "secret", "password", "passwd", "credential", "auth")
_SAFE_ERROR = "Claude Settings could not be loaded"
_REDACTED = "configured"

# These are the public controls this application writes through structured
# actions.  Other official fields can remain in the raw JSON editor until the
# UI has a dedicated, documented control for them.
_TOP_LEVEL_STRING_FIELDS = (
    "model",
    "advisorModel",
    "agent",
    "effortLevel",
    "editorMode",
    "theme",
    "viewMode",
    "tui",
    "teammateMode",
    "preferredNotifChannel",
    "askUserQuestionTimeout",
    "language",
    "outputStyle",
    "defaultShell",
    "diffTool",
    "teammateDefaultModel",
    "workflowSizeGuideline",
    "autoUpdatesChannel",
    "minimumVersion",
)
_TOP_LEVEL_BOOLEAN_FIELDS = (
    "agentPushNotifEnabled",
    "alwaysThinkingEnabled",
    "showThinkingSummaries",
    "fastMode",
    "fastModePerSessionOptIn",
    "autoCompactEnabled",
    "autoMemoryEnabled",
    "fileCheckpointingEnabled",
    "verbose",
    "respectGitignore",
    "includeGitInstructions",
    "enableAllProjectMcpServers",
    "spinnerTipsEnabled",
    "terminalProgressBarEnabled",
    "prefersReducedMotion",
    "axScreenReader",
    "syntaxHighlightingDisabled",
    "autoScrollEnabled",
    "wheelScrollAccelerationEnabled",
    "showTurnDuration",
    "enableArtifact",
    "disableWorkflows",
    "workflowKeywordTriggerEnabled",
    "emojiCompletionEnabled",
    "respondToBashCommands",
    "showClearContextOnPlanAccept",
    "switchModelsOnFlag",
    "useAutoModeDuringPlan",
    "inputNeededNotifEnabled",
    "remoteControlAtStartup",
    "awaySummaryEnabled",
    "autoConnectIde",
    "autoInstallIdeExtension",
    "externalEditorContext",
    "permissionExplainerEnabled",
    "disableAgentView",
    "disableArtifact",
    "skipWebFetchPreflight",
    "disableBundledSkills",
    "disableClaudeAiConnectors",
    "disableRemoteControl",
    "disableSkillShellExecution",
    "disableAllHooks",
)
_TOP_LEVEL_LIST_FIELDS = (
    "availableModels",
    "enabledMcpjsonServers",
    "disabledMcpjsonServers",
    "companyAnnouncements",
)
_TOP_LEVEL_DISABLE_LITERAL_FIELDS = ("disableAutoMode", "disableDeepLinkRegistration")
_TOP_LEVEL_UNIT_INTERVAL_FIELDS = ("feedbackSurveyRate",)
_TOP_LEVEL_UNIT_FRACTION_FIELDS = ("skillListingBudgetFraction",)
_TOP_LEVEL_POSITIVE_INTEGER_FIELDS = ("skillListingMaxDescChars",)
_STRUCTURED_PATCH_FIELDS = frozenset(
    {
        "attribution",
        "autoMode",
        "fallbackModel",
        "permissions",
        "sandbox",
        "env",
        "cleanupPeriodDays",
        "vimInsertModeRemaps",
        "voice",
        "skillOverrides",
        "spinnerTipsOverride",
        "spinnerVerbs",
        "worktree",
        *_TOP_LEVEL_STRING_FIELDS,
        *_TOP_LEVEL_BOOLEAN_FIELDS,
        *_TOP_LEVEL_LIST_FIELDS,
        *_TOP_LEVEL_DISABLE_LITERAL_FIELDS,
        *_TOP_LEVEL_UNIT_INTERVAL_FIELDS,
        *_TOP_LEVEL_UNIT_FRACTION_FIELDS,
        *_TOP_LEVEL_POSITIVE_INTEGER_FIELDS,
    }
)

_PERMISSION_LIST_FIELDS = ("allow", "ask", "deny", "additionalDirectories")
_PERMISSION_DISABLE_LITERAL_FIELDS = ("disableBypassPermissionsMode",)
_SANDBOX_BOOLEAN_FIELDS = (
    "enabled",
    "failIfUnavailable",
    "autoAllowBashIfSandboxed",
    "allowUnsandboxedCommands",
    "enableWeakerNestedSandbox",
    "enableWeakerNetworkIsolation",
    "allowAppleEvents",
)
_SANDBOX_LIST_FIELDS = ("excludedCommands",)
_FILESYSTEM_BOOLEAN_FIELDS = ("disabled",)
_FILESYSTEM_LIST_FIELDS = ("allowWrite", "denyWrite", "allowRead", "denyRead")
_NETWORK_BOOLEAN_FIELDS = (
    "allowAllUnixSockets",
    "allowLocalBinding",
    "strictAllowlist",
)
_NETWORK_LIST_FIELDS = ("allowedDomains", "deniedDomains", "allowUnixSockets", "allowMachLookup")
_NETWORK_PORT_FIELDS = ("httpProxyPort", "socksProxyPort")
_ATTRIBUTION_STRING_FIELDS = ("commit", "pr")
_ATTRIBUTION_BOOLEAN_FIELDS = ("sessionUrl",)
_ATTRIBUTION_FIELDS = (*_ATTRIBUTION_STRING_FIELDS, *_ATTRIBUTION_BOOLEAN_FIELDS)
_AUTO_MODE_BOOLEAN_FIELDS = ("classifyAllShell",)
_AUTO_MODE_LIST_FIELDS = ("environment", "allow", "soft_deny", "hard_deny")
_AUTO_MODE_FIELDS = (*_AUTO_MODE_BOOLEAN_FIELDS, *_AUTO_MODE_LIST_FIELDS)
_VOICE_BOOLEAN_FIELDS = ("enabled", "autoSubmit")
_VOICE_MODES = frozenset({"hold", "tap"})
_VIM_INSERT_ESCAPE = "<Esc>"
_AUTO_UPDATE_CHANNELS = frozenset({"latest", "stable"})
_SKILL_OVERRIDE_VALUES = frozenset({"on", "name-only", "user-invocable-only", "off"})
_SPINNER_VERB_MODES = frozenset({"append", "replace"})
_WORKTREE_BASE_REFS = frozenset({"fresh", "head"})
_WORKTREE_BACKGROUND_ISOLATION_MODES = frozenset({"worktree", "none"})


class ClaudeSettingsError(ValueError):
    """An error safe to send through the local IPC boundary."""


class ConfirmationRequired(ClaudeSettingsError):
    """Apply is denied until the caller acknowledges every active risk."""

    def __init__(self, codes: Sequence[str]):
        self.codes = tuple(dict.fromkeys(str(code) for code in codes if code in RISK_CONFIRMATION_CODES))
        super().__init__("Explicit confirmation is required for the selected Claude permissions")


@dataclass(frozen=True)
class ClaudeDeployment:
    """The public deployment fields permitted in Claude's environment."""

    model: str
    base_url: str
    # Native hosts stage this through the authenticated secure-input endpoint.
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


def _positive_integer(value: object, label: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise ClaudeSettingsError(f"{label} must be a positive integer")
    return value


def _unit_interval(value: object, label: str, *, allow_zero: bool) -> float | int | None:
    """Validate a finite scalar ratio without accepting booleans or NaN."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClaudeSettingsError(f"{label} must be a number")
    if not math.isfinite(value) or value > 1 or (value < 0 if allow_zero else value <= 0):
        lower = "between 0 and 1" if allow_zero else "greater than 0 and at most 1"
        raise ClaudeSettingsError(f"{label} must be {lower}")
    return value


def _disable_literal(value: object, label: str) -> str | None:
    value = _string(value, label)
    if value is None:
        return None
    if value != "disable":
        raise ClaudeSettingsError(f"{label} must be disable")
    return value


def _normalise_skill_overrides(value: object, *, allow_unset: bool = False) -> dict[str, str]:
    if value is None:
        return {}
    overrides = _mapping(value, "skillOverrides")
    result: dict[str, str] = {}
    for name, mode in overrides.items():
        skill_name = _string(name, "skillOverrides skill name", required=True)
        if mode is None and allow_unset:
            continue
        if not isinstance(mode, str) or mode not in _SKILL_OVERRIDE_VALUES:
            raise ClaudeSettingsError("skillOverrides values must be on, name-only, user-invocable-only, or off")
        assert skill_name is not None
        result[skill_name] = mode
    return result


def _normalise_spinner_tips_override(settings: Mapping[str, Any], *, allow_unset: bool = False) -> dict[str, Any]:
    value = settings.get("spinnerTipsOverride")
    if value is None:
        return {}
    override = _mapping(value, "spinnerTipsOverride")
    result: dict[str, Any] = {}
    if "tips" not in override and not allow_unset:
        raise ClaudeSettingsError("spinnerTipsOverride.tips must be a list of strings")
    if "tips" in override:
        tips = override["tips"]
        if tips is None and allow_unset:
            pass
        else:
            result["tips"] = _string_list(tips, "spinnerTipsOverride.tips")
    if "excludeDefault" in override:
        exclude_default = override["excludeDefault"]
        if exclude_default is None and allow_unset:
            pass
        elif not isinstance(exclude_default, bool):
            raise ClaudeSettingsError("spinnerTipsOverride.excludeDefault must be true or false")
        else:
            result["excludeDefault"] = exclude_default
    return result


def _normalise_spinner_verbs(settings: Mapping[str, Any], *, allow_unset: bool = False) -> dict[str, Any]:
    value = settings.get("spinnerVerbs")
    if value is None:
        return {}
    verbs = _mapping(value, "spinnerVerbs")
    result: dict[str, Any] = {}
    if "verbs" not in verbs and not allow_unset:
        raise ClaudeSettingsError("spinnerVerbs.verbs must be a list of strings")
    if "verbs" in verbs:
        values = verbs["verbs"]
        if values is None and allow_unset:
            pass
        else:
            result["verbs"] = _string_list(values, "spinnerVerbs.verbs")
    if "mode" in verbs:
        mode = verbs["mode"]
        if mode is None and allow_unset:
            pass
        elif not isinstance(mode, str) or mode not in _SPINNER_VERB_MODES:
            raise ClaudeSettingsError("spinnerVerbs.mode must be append or replace")
        else:
            result["mode"] = mode
    return result


def _normalise_worktree(settings: Mapping[str, Any], *, allow_unset: bool = False) -> dict[str, str]:
    value = settings.get("worktree")
    if value is None:
        return {}
    worktree = _mapping(value, "worktree")
    result: dict[str, str] = {}
    for key, allowed in (
        ("baseRef", _WORKTREE_BASE_REFS),
        ("bgIsolation", _WORKTREE_BACKGROUND_ISOLATION_MODES),
    ):
        if key not in worktree:
            continue
        item = worktree[key]
        if item is None and allow_unset:
            continue
        if not isinstance(item, str) or item not in allowed:
            values = " or ".join(sorted(allowed))
            raise ClaudeSettingsError(f"worktree.{key} must be {values}")
        result[key] = item
    return result


def _normalise_auto_memory_directory(value: object) -> str | None:
    """Validate Claude's memory location without ever publishing it."""

    directory = _string(value, "autoMemoryDirectory")
    if directory is None:
        return None
    if directory.startswith("~/"):
        return directory
    if pathlib.PurePosixPath(directory).is_absolute() or pathlib.PureWindowsPath(directory).is_absolute():
        return directory
    raise ClaudeSettingsError("autoMemoryDirectory must be an absolute path or start with ~/")


def _normalise_attribution(settings: Mapping[str, Any], *, allow_unset: bool = False) -> dict[str, Any]:
    value = settings.get("attribution")
    if value is None:
        return {}
    attribution = _mapping(value, "attribution")
    result: dict[str, Any] = {}
    for key in _ATTRIBUTION_STRING_FIELDS:
        if key not in attribution:
            continue
        item = attribution[key]
        if item is None and allow_unset:
            continue
        # Commit attribution can intentionally contain multiple git trailers,
        # so unlike ordinary settings text it must retain embedded newlines.
        if not isinstance(item, str):
            raise ClaudeSettingsError(f"attribution.{key} must be a string")
        result[key] = item
    for key in _ATTRIBUTION_BOOLEAN_FIELDS:
        if key not in attribution:
            continue
        item = attribution[key]
        if item is None and allow_unset:
            continue
        if not isinstance(item, bool):
            raise ClaudeSettingsError(f"attribution.{key} must be true or false")
        result[key] = item
    return result


def _normalise_auto_mode(settings: Mapping[str, Any], *, allow_unset: bool = False) -> dict[str, Any]:
    value = settings.get("autoMode")
    if value is None:
        return {}
    auto_mode = _mapping(value, "autoMode")
    result: dict[str, Any] = {}
    for key in _AUTO_MODE_BOOLEAN_FIELDS:
        if key not in auto_mode:
            continue
        item = auto_mode[key]
        if item is None and allow_unset:
            continue
        if not isinstance(item, bool):
            raise ClaudeSettingsError(f"autoMode.{key} must be true or false")
        result[key] = item
    for key in _AUTO_MODE_LIST_FIELDS:
        if key not in auto_mode:
            continue
        item = auto_mode[key]
        if item is None and allow_unset:
            continue
        result[key] = _string_list(item, f"autoMode.{key}")
    return result


def _normalise_auto_updates_channel(value: object) -> str | None:
    channel = _string(value, "autoUpdatesChannel")
    if channel is None:
        return None
    if channel not in _AUTO_UPDATE_CHANNELS:
        raise ClaudeSettingsError("autoUpdatesChannel must be latest or stable")
    return channel


def _normalise_vim_insert_mode_remaps(value: object, *, allow_unset: bool = False) -> dict[str, str]:
    if value is None:
        return {}
    remaps = _mapping(value, "vimInsertModeRemaps")
    result: dict[str, str] = {}
    for sequence, target in remaps.items():
        if not isinstance(sequence, str) or len(sequence) != 2 or not sequence.isprintable():
            raise ClaudeSettingsError("vimInsertModeRemaps keys must be exactly two printable characters")
        if target is None and allow_unset:
            continue
        if target != _VIM_INSERT_ESCAPE:
            raise ClaudeSettingsError(f"vimInsertModeRemaps values must be {_VIM_INSERT_ESCAPE}")
        result[sequence] = _VIM_INSERT_ESCAPE
    return result


def _normalise_voice(settings: Mapping[str, Any], *, allow_unset: bool = False) -> dict[str, Any]:
    value = settings.get("voice")
    if value is None:
        return {}
    voice = _mapping(value, "voice")
    result: dict[str, Any] = {}
    for key in _VOICE_BOOLEAN_FIELDS:
        if key not in voice:
            continue
        item = voice[key]
        if item is None and allow_unset:
            continue
        if not isinstance(item, bool):
            raise ClaudeSettingsError(f"voice.{key} must be true or false")
        result[key] = item
    if "mode" in voice:
        mode = voice["mode"]
        if mode is None and allow_unset:
            return result
        if not isinstance(mode, str) or mode not in _VOICE_MODES:
            raise ClaudeSettingsError("voice.mode must be hold or tap")
        result["mode"] = mode
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
    return text in {
        "path",
        "paths",
        "directory",
        "directories",
        "cwd",
        "root",
        "roots",
        "file",
        "files",
        "additionaldirectories",
        "allowwrite",
        "denywrite",
        "allowread",
        "denyread",
        "allowunixsockets",
    } or text.endswith(("path", "paths", "directory", "directories"))


def _network_list_key(key: object) -> bool:
    return str(key).lower() in {
        "alloweddomains",
        "denieddomains",
        "allowed_domains",
        "denied_domains",
        "egress",
        "allowed_egress",
    }


def redact(value: object, *, _key: object = "") -> object:
    """Return a JSON-safe projection with credentials, paths, and domains removed."""

    if _secret_key(_key):
        if value in (None, "", False):
            return value
        return _REDACTED
    if _path_key(_key) or _network_list_key(_key):
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [_REDACTED] if value else []
        return _REDACTED if value else value
    if isinstance(value, Mapping):
        return {str(key): redact(item, _key=key) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item) for item in value]
    return value


def _redacted_list(values: Sequence[str]) -> list[str]:
    return [_REDACTED] if values else []


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
    value = settings.get("permissions", {})
    if value is None:
        value = {}
    permissions = _mapping(value, "permissions")
    result: dict[str, Any] = {"allow": [], "ask": [], "deny": [], "additionalDirectories": []}
    if "defaultMode" in permissions:
        result["defaultMode"] = _string(permissions["defaultMode"], "permissions.defaultMode")
    for key in _PERMISSION_DISABLE_LITERAL_FIELDS:
        if key in permissions:
            result[key] = _disable_literal(permissions[key], f"permissions.{key}")
    for key in _PERMISSION_LIST_FIELDS:
        if key in permissions:
            result[key] = _string_list(permissions[key], f"permissions.{key}")
    return result


def _normalise_sandbox(settings: Mapping[str, Any]) -> dict[str, Any]:
    value = settings.get("sandbox", {})
    if value is None:
        value = {}
    sandbox = _mapping(value, "sandbox")
    result: dict[str, Any] = {}
    for key in _SANDBOX_BOOLEAN_FIELDS:
        if key in sandbox:
            if not isinstance(sandbox[key], bool):
                raise ClaudeSettingsError(f"sandbox.{key} must be true or false")
            result[key] = sandbox[key]
    for key in _SANDBOX_LIST_FIELDS:
        if key in sandbox:
            result[key] = _string_list(sandbox[key], f"sandbox.{key}")

    filesystem_value = sandbox.get("filesystem")
    if filesystem_value is not None:
        filesystem = _mapping(filesystem_value, "sandbox.filesystem")
        filesystem_result: dict[str, Any] = {}
        for key in _FILESYSTEM_BOOLEAN_FIELDS:
            if key in filesystem:
                if not isinstance(filesystem[key], bool):
                    raise ClaudeSettingsError(f"sandbox.filesystem.{key} must be true or false")
                filesystem_result[key] = filesystem[key]
        for key in _FILESYSTEM_LIST_FIELDS:
            if key in filesystem:
                filesystem_result[key] = _string_list(filesystem[key], f"sandbox.filesystem.{key}")
        result["filesystem"] = filesystem_result

    network_value = sandbox.get("network")
    if network_value is not None:
        network = _mapping(network_value, "sandbox.network")
        network_result: dict[str, Any] = {}
        for key in _NETWORK_BOOLEAN_FIELDS:
            if key in network:
                if not isinstance(network[key], bool):
                    raise ClaudeSettingsError(f"sandbox.network.{key} must be true or false")
                network_result[key] = network[key]
        for key in _NETWORK_LIST_FIELDS:
            if key in network:
                network_result[key] = _string_list(network[key], f"sandbox.network.{key}")
        for key in _NETWORK_PORT_FIELDS:
            if key in network:
                network_result[key] = _positive_integer(network[key], f"sandbox.network.{key}")
        result["network"] = network_result
    return result


def _normalise_fallback_model(value: object) -> str | list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return _string(value, "fallbackModel")
    models = _string_list(value, "fallbackModel")
    if len(models) > 3:
        raise ClaudeSettingsError("Claude supports at most 3 fallback models")
    return models


def validate_settings(settings: Mapping[str, Any]) -> list[str]:
    """Type-check the canonical controls without rejecting future raw fields."""

    errors: list[str] = []
    try:
        settings = _mapping(settings)
        for key in _TOP_LEVEL_STRING_FIELDS:
            if key in settings:
                if key == "autoUpdatesChannel":
                    _normalise_auto_updates_channel(settings[key])
                else:
                    _string(settings[key], key)
        for key in _TOP_LEVEL_DISABLE_LITERAL_FIELDS:
            if key in settings:
                _disable_literal(settings[key], key)
        for key in _TOP_LEVEL_UNIT_INTERVAL_FIELDS:
            if key in settings:
                _unit_interval(settings[key], key, allow_zero=True)
        for key in _TOP_LEVEL_UNIT_FRACTION_FIELDS:
            if key in settings:
                _unit_interval(settings[key], key, allow_zero=False)
        for key in _TOP_LEVEL_POSITIVE_INTEGER_FIELDS:
            if key in settings:
                _positive_integer(settings[key], key)
        if "fallbackModel" in settings:
            _normalise_fallback_model(settings["fallbackModel"])
        for key in _TOP_LEVEL_BOOLEAN_FIELDS:
            if key in settings and settings[key] is not None and not isinstance(settings[key], bool):
                raise ClaudeSettingsError(f"{key} must be true or false")
        for key in _TOP_LEVEL_LIST_FIELDS:
            if key in settings:
                _string_list(settings[key], key)
        if "cleanupPeriodDays" in settings:
            _positive_integer(settings["cleanupPeriodDays"], "cleanupPeriodDays")
        if "autoMemoryDirectory" in settings:
            _normalise_auto_memory_directory(settings["autoMemoryDirectory"])
        if "attribution" in settings:
            _normalise_attribution(settings)
        if "autoMode" in settings:
            _normalise_auto_mode(settings)
        if "vimInsertModeRemaps" in settings:
            _normalise_vim_insert_mode_remaps(settings["vimInsertModeRemaps"])
        if "voice" in settings:
            _normalise_voice(settings)
        if "skillOverrides" in settings:
            _normalise_skill_overrides(settings["skillOverrides"])
        if "spinnerTipsOverride" in settings:
            _normalise_spinner_tips_override(settings)
        if "spinnerVerbs" in settings:
            _normalise_spinner_verbs(settings)
        if "worktree" in settings:
            _normalise_worktree(settings)

        env = settings.get("env", {})
        env = _mapping(env, "env")
        for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
            if key not in env or env[key] in (None, ""):
                continue
            if not isinstance(env[key], str) or "\n" in env[key] or "\r" in env[key]:
                raise ClaudeSettingsError(f"{key} must be a single-line string")
            if key == "ANTHROPIC_BASE_URL" and not _is_http_url(env[key]):
                raise ClaudeSettingsError("ANTHROPIC_BASE_URL must be an http or https URL")
        _normalise_permissions(settings)
        _normalise_sandbox(settings)
    except ClaudeSettingsError as exc:
        errors.append(str(exc))
    return errors


def _broad_filesystem_pattern(value: str) -> bool:
    return value.strip().lower() in {"*", "**", "/", "/*", "~", "~/"}


def _broad_network_pattern(value: str) -> bool:
    return value.strip().lower() in {"*", "*.*", "0.0.0.0/0", "::/0"}


def _risk_state(settings: Mapping[str, Any]) -> set[str]:
    risks: set[str] = set()
    permissions = _normalise_permissions(settings)
    sandbox = _normalise_sandbox(settings)
    default_mode = str(permissions.get("defaultMode") or "")
    filesystem = _mapping(sandbox.get("filesystem", {}), "sandbox.filesystem")
    network = _mapping(sandbox.get("network", {}), "sandbox.network")

    if default_mode.lower() in {"bypasspermissions", "bypass_permissions", "bypass"}:
        risks.add("bypass_permissions")
    if sandbox.get("enabled") is False or filesystem.get("disabled") is True:
        risks.add("sandbox_disabled")
    if any(_broad_filesystem_pattern(item) for item in _string_list(filesystem.get("allowWrite"), "sandbox.filesystem.allowWrite")):
        risks.add("filesystem_scope_broadened")
    if any(_broad_network_pattern(item) for item in _string_list(network.get("allowedDomains"), "sandbox.network.allowedDomains")):
        risks.add("network_scope_broadened")
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
    """Return a draft with only Claude's public LiteLLM connection fields changed."""

    selected = _deployment(deployment)
    updated = copy.deepcopy(_mapping(settings, "settings"))
    env = _mapping(updated.get("env", {}), "env")
    env.update(
        {
            "ANTHROPIC_MODEL": selected.model,
            "ANTHROPIC_BASE_URL": selected.base_url,
        }
    )
    # Direct Core callers may stage a complete deployment in one local
    # transaction. Native hosts stage this with a one-time secure capability.
    if selected.token is not None:
        env["ANTHROPIC_AUTH_TOKEN"] = selected.token
    updated["model"] = selected.model
    updated["env"] = env
    return updated


def _patch_litellm_deployment(settings: Mapping[str, Any], patch: object) -> dict[str, Any]:
    """Stage one public deployment field without requiring a complete pair."""

    data = _mapping(patch, "deployment")
    if not data or set(data).difference({"model", "base_url"}):
        raise ClaudeSettingsError("Unknown Claude deployment field")
    updated = copy.deepcopy(_mapping(settings, "settings"))
    env = _mapping(updated.get("env", {}), "env")
    if "model" in data:
        value = data["model"]
        if not isinstance(value, str) or "\n" in value or "\r" in value:
            raise ClaudeSettingsError("deployment.model must be a single-line string")
        model = value.strip()
        if model:
            updated["model"] = model
            env["ANTHROPIC_MODEL"] = model
        else:
            updated.pop("model", None)
            env.pop("ANTHROPIC_MODEL", None)
    if "base_url" in data:
        value = data["base_url"]
        if not isinstance(value, str) or "\n" in value or "\r" in value:
            raise ClaudeSettingsError("deployment.base_url must be a single-line string")
        base_url = value.strip()
        if base_url:
            if not _is_http_url(base_url):
                raise ClaudeSettingsError("deployment.base_url must be an http or https URL")
            env["ANTHROPIC_BASE_URL"] = base_url
        else:
            env.pop("ANTHROPIC_BASE_URL", None)
    if env:
        updated["env"] = env
    else:
        updated.pop("env", None)
    return updated


def risk_confirmation_codes(settings: Mapping[str, Any]) -> tuple[str, ...]:
    """Return deterministic confirmation codes for a candidate draft."""

    return tuple(sorted(_risk_state(settings)))


def _deep_merge(existing: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(dict(existing))
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        elif isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(_mapping(merged[key]), value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _reject_unknown_patch_fields(value: object, allowed: Sequence[str], label: str) -> dict[str, Any]:
    patch = _mapping(value, label)
    if set(patch).difference(allowed):
        raise ClaudeSettingsError("Unknown Claude Settings field")
    return patch


def _validate_structured_patch(data: Mapping[str, Any]) -> None:
    """Keep ordinary patch actions on the documented UI-owned surface."""

    unknown = set(data).difference(_STRUCTURED_PATCH_FIELDS)
    if unknown:
        raise ClaudeSettingsError("Unknown Claude Settings field")
    if "fallbackModel" in data:
        _normalise_fallback_model(data["fallbackModel"])
    if "attribution" in data and data["attribution"] is not None:
        attribution = _reject_unknown_patch_fields(
            data["attribution"],
            _ATTRIBUTION_FIELDS,
            "attribution",
        )
        _normalise_attribution({"attribution": attribution}, allow_unset=True)
    if "autoMode" in data and data["autoMode"] is not None:
        auto_mode = _reject_unknown_patch_fields(data["autoMode"], _AUTO_MODE_FIELDS, "autoMode")
        _normalise_auto_mode({"autoMode": auto_mode}, allow_unset=True)
    if "vimInsertModeRemaps" in data and data["vimInsertModeRemaps"] is not None:
        _normalise_vim_insert_mode_remaps(data["vimInsertModeRemaps"], allow_unset=True)
    if "voice" in data and data["voice"] is not None:
        voice = _reject_unknown_patch_fields(
            data["voice"],
            (*_VOICE_BOOLEAN_FIELDS, "mode"),
            "voice",
        )
        _normalise_voice({"voice": voice}, allow_unset=True)
    if "skillOverrides" in data and data["skillOverrides"] is not None:
        _normalise_skill_overrides(data["skillOverrides"], allow_unset=True)
    if "spinnerTipsOverride" in data and data["spinnerTipsOverride"] is not None:
        spinner_tips = _reject_unknown_patch_fields(
            data["spinnerTipsOverride"],
            ("tips", "excludeDefault"),
            "spinnerTipsOverride",
        )
        _normalise_spinner_tips_override({"spinnerTipsOverride": spinner_tips}, allow_unset=True)
    if "spinnerVerbs" in data and data["spinnerVerbs"] is not None:
        spinner_verbs = _reject_unknown_patch_fields(
            data["spinnerVerbs"],
            ("verbs", "mode"),
            "spinnerVerbs",
        )
        _normalise_spinner_verbs({"spinnerVerbs": spinner_verbs}, allow_unset=True)
    if "worktree" in data and data["worktree"] is not None:
        worktree = _reject_unknown_patch_fields(
            data["worktree"],
            ("baseRef", "bgIsolation"),
            "worktree",
        )
        _normalise_worktree({"worktree": worktree}, allow_unset=True)
    if "permissions" in data:
        _reject_unknown_patch_fields(
            data["permissions"],
            ("defaultMode", *_PERMISSION_DISABLE_LITERAL_FIELDS, *_PERMISSION_LIST_FIELDS),
            "permissions",
        )
    if "sandbox" not in data:
        return
    sandbox = _reject_unknown_patch_fields(
        data["sandbox"],
        (*_SANDBOX_BOOLEAN_FIELDS, *_SANDBOX_LIST_FIELDS, "filesystem", "network"),
        "sandbox",
    )
    if "filesystem" in sandbox:
        _reject_unknown_patch_fields(
            sandbox["filesystem"],
            (*_FILESYSTEM_BOOLEAN_FIELDS, *_FILESYSTEM_LIST_FIELDS),
            "sandbox.filesystem",
        )
    if "network" in sandbox:
        _reject_unknown_patch_fields(
            sandbox["network"],
            (*_NETWORK_BOOLEAN_FIELDS, *_NETWORK_LIST_FIELDS, *_NETWORK_PORT_FIELDS),
            "sandbox.network",
        )


def _normalise_structured_patch(data: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalize a UI-owned patch before it reaches the draft.

    Validation helpers deliberately trim user-facing strings and lists.  Apply
    those results here so snapshots and persisted JSON cannot disagree, while
    retaining `None` as the explicit delete marker used by `_deep_merge`.
    """

    def retain_unsets(source: Mapping[str, Any], normalized: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: None if value is None else copy.deepcopy(normalized[key])
            for key, value in source.items()
            if key in normalized or value is None
        }

    result = copy.deepcopy(dict(data))
    for key in _TOP_LEVEL_STRING_FIELDS:
        if key not in result or result[key] is None:
            continue
        result[key] = _normalise_auto_updates_channel(result[key]) if key == "autoUpdatesChannel" else _string(result[key], key)
    for key in _TOP_LEVEL_DISABLE_LITERAL_FIELDS:
        if key in result and result[key] is not None:
            result[key] = _disable_literal(result[key], key)
    for key in _TOP_LEVEL_UNIT_INTERVAL_FIELDS:
        if key in result and result[key] is not None:
            result[key] = _unit_interval(result[key], key, allow_zero=True)
    for key in _TOP_LEVEL_UNIT_FRACTION_FIELDS:
        if key in result and result[key] is not None:
            result[key] = _unit_interval(result[key], key, allow_zero=False)
    for key in _TOP_LEVEL_POSITIVE_INTEGER_FIELDS:
        if key in result and result[key] is not None:
            result[key] = _positive_integer(result[key], key)
    if "cleanupPeriodDays" in result and result["cleanupPeriodDays"] is not None:
        result["cleanupPeriodDays"] = _positive_integer(result["cleanupPeriodDays"], "cleanupPeriodDays")
    for key in _TOP_LEVEL_BOOLEAN_FIELDS:
        if key in result and result[key] is not None and not isinstance(result[key], bool):
            raise ClaudeSettingsError(f"{key} must be true or false")
    for key in _TOP_LEVEL_LIST_FIELDS:
        if key in result and result[key] is not None:
            result[key] = _string_list(result[key], key)
    if "fallbackModel" in result and result["fallbackModel"] is not None:
        result["fallbackModel"] = _normalise_fallback_model(result["fallbackModel"])
    if "attribution" in result and result["attribution"] is not None:
        attribution = _mapping(result["attribution"], "attribution")
        result["attribution"] = retain_unsets(
            attribution,
            _normalise_attribution({"attribution": attribution}, allow_unset=True),
        )
    if "autoMode" in result and result["autoMode"] is not None:
        auto_mode = _mapping(result["autoMode"], "autoMode")
        result["autoMode"] = retain_unsets(
            auto_mode,
            _normalise_auto_mode({"autoMode": auto_mode}, allow_unset=True),
        )
    if "vimInsertModeRemaps" in result and result["vimInsertModeRemaps"] is not None:
        remaps = _mapping(result["vimInsertModeRemaps"], "vimInsertModeRemaps")
        result["vimInsertModeRemaps"] = retain_unsets(
            remaps,
            _normalise_vim_insert_mode_remaps(remaps, allow_unset=True),
        )
    if "voice" in result and result["voice"] is not None:
        voice = _mapping(result["voice"], "voice")
        result["voice"] = retain_unsets(
            voice,
            _normalise_voice({"voice": voice}, allow_unset=True),
        )
    if "skillOverrides" in result and result["skillOverrides"] is not None:
        overrides = _mapping(result["skillOverrides"], "skillOverrides")
        normalized_overrides = _normalise_skill_overrides(overrides, allow_unset=True)
        result["skillOverrides"] = {
            name.strip(): None if mode is None else normalized_overrides[name.strip()]
            for name, mode in overrides.items()
        }
    if "spinnerTipsOverride" in result and result["spinnerTipsOverride"] is not None:
        spinner_tips = _mapping(result["spinnerTipsOverride"], "spinnerTipsOverride")
        result["spinnerTipsOverride"] = retain_unsets(
            spinner_tips,
            _normalise_spinner_tips_override({"spinnerTipsOverride": spinner_tips}, allow_unset=True),
        )
    if "spinnerVerbs" in result and result["spinnerVerbs"] is not None:
        spinner_verbs = _mapping(result["spinnerVerbs"], "spinnerVerbs")
        result["spinnerVerbs"] = retain_unsets(
            spinner_verbs,
            _normalise_spinner_verbs({"spinnerVerbs": spinner_verbs}, allow_unset=True),
        )
    if "worktree" in result and result["worktree"] is not None:
        worktree = _mapping(result["worktree"], "worktree")
        result["worktree"] = retain_unsets(
            worktree,
            _normalise_worktree({"worktree": worktree}, allow_unset=True),
        )
    if "permissions" in result and result["permissions"] is not None:
        permissions = _mapping(result["permissions"], "permissions")
        normalized_permissions: dict[str, Any] = {}
        if "defaultMode" in permissions:
            normalized_permissions["defaultMode"] = None if permissions["defaultMode"] is None else _string(permissions["defaultMode"], "permissions.defaultMode")
        for key in _PERMISSION_DISABLE_LITERAL_FIELDS:
            if key in permissions:
                normalized_permissions[key] = None if permissions[key] is None else _disable_literal(permissions[key], f"permissions.{key}")
        for key in _PERMISSION_LIST_FIELDS:
            if key in permissions:
                normalized_permissions[key] = None if permissions[key] is None else _string_list(permissions[key], f"permissions.{key}")
        result["permissions"] = normalized_permissions
    if "sandbox" in result and result["sandbox"] is not None:
        sandbox = _mapping(result["sandbox"], "sandbox")
        normalized_sandbox: dict[str, Any] = {}
        for key in _SANDBOX_BOOLEAN_FIELDS:
            if key in sandbox:
                if sandbox[key] is None:
                    normalized_sandbox[key] = None
                elif not isinstance(sandbox[key], bool):
                    raise ClaudeSettingsError(f"sandbox.{key} must be true or false")
                else:
                    normalized_sandbox[key] = sandbox[key]
        for key in _SANDBOX_LIST_FIELDS:
            if key in sandbox:
                normalized_sandbox[key] = None if sandbox[key] is None else _string_list(sandbox[key], f"sandbox.{key}")
        if "filesystem" in sandbox and sandbox["filesystem"] is None:
            normalized_sandbox["filesystem"] = None
        elif "filesystem" in sandbox:
            filesystem = _mapping(sandbox["filesystem"], "sandbox.filesystem")
            normalized_filesystem: dict[str, Any] = {}
            for key in _FILESYSTEM_BOOLEAN_FIELDS:
                if key in filesystem:
                    if filesystem[key] is None:
                        normalized_filesystem[key] = None
                    elif not isinstance(filesystem[key], bool):
                        raise ClaudeSettingsError(f"sandbox.filesystem.{key} must be true or false")
                    else:
                        normalized_filesystem[key] = filesystem[key]
            for key in _FILESYSTEM_LIST_FIELDS:
                if key in filesystem:
                    normalized_filesystem[key] = None if filesystem[key] is None else _string_list(filesystem[key], f"sandbox.filesystem.{key}")
            normalized_sandbox["filesystem"] = normalized_filesystem
        if "network" in sandbox and sandbox["network"] is None:
            normalized_sandbox["network"] = None
        elif "network" in sandbox:
            network = _mapping(sandbox["network"], "sandbox.network")
            normalized_network: dict[str, Any] = {}
            for key in _NETWORK_BOOLEAN_FIELDS:
                if key in network:
                    if network[key] is None:
                        normalized_network[key] = None
                    elif not isinstance(network[key], bool):
                        raise ClaudeSettingsError(f"sandbox.network.{key} must be true or false")
                    else:
                        normalized_network[key] = network[key]
            for key in _NETWORK_LIST_FIELDS:
                if key in network:
                    normalized_network[key] = None if network[key] is None else _string_list(network[key], f"sandbox.network.{key}")
            for key in _NETWORK_PORT_FIELDS:
                if key in network:
                    normalized_network[key] = None if network[key] is None else _positive_integer(network[key], f"sandbox.network.{key}")
            normalized_sandbox["network"] = normalized_network
        result["sandbox"] = normalized_sandbox
    return result


class ClaudeSettingsDomain:
    """Core-owned drafts for Claude Code and Claude Desktop 3P settings."""

    name = DOMAIN_NAME

    def __init__(
        self,
        settings_path: pathlib.Path | str | None = None,
        *,
        loader: Callable[[pathlib.Path], tuple[str, bool]] | None = None,
        desktop_config_library_path: pathlib.Path | str | None = None,
        desktop_loader: Callable[[pathlib.Path], tuple[str, bool]] | None = None,
        developer_settings_path: pathlib.Path | str | None = None,
        developer_loader: Callable[[pathlib.Path], tuple[str, bool]] | None = None,
    ):
        self.settings_path = pathlib.Path(settings_path).expanduser() if settings_path else default_settings_path()
        self._loader = loader or _safe_read
        # Production uses both official sources.  Tests and callers that pass
        # an explicit Claude Code path stay isolated unless they also provide
        # an explicit Desktop library.
        desktop_enabled = desktop_config_library_path is not None or settings_path is None
        self._desktop = (
            ClaudeDesktopConfig(desktop_config_library_path, loader=desktop_loader)
            if desktop_enabled
            else None
        )
        developer_enabled = developer_settings_path is not None or settings_path is None
        self._developer = (
            ClaudeDeveloperSettings(developer_settings_path, loader=developer_loader)
            if developer_enabled
            else None
        )
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
        if self._desktop is not None:
            self._desktop.reload()
        if self._developer is not None:
            self._developer.reload()
        self._revision += 1
        return self.snapshot()

    def persistence_paths(self) -> tuple[pathlib.Path, ...]:
        paths = [self.settings_path]
        if self._desktop is not None:
            paths.extend(self._desktop.persistence_paths())
        if self._developer is not None:
            paths.extend(self._developer.persistence_paths())
        return tuple(dict.fromkeys(paths))

    def external_disk_state(self) -> dict[str, bool]:
        """Compare the current settings file with the last reload/apply baseline."""

        current = _current_bytes(self.settings_path)
        changed = current != self._baseline_bytes
        if self._desktop is not None:
            changed = changed or self._desktop.external_disk_state()["changed"]
        if self._developer is not None:
            changed = changed or self._developer.external_disk_state()["changed"]
        return {"changed": changed, "exists": current is not None}

    def external_disk_identity(self) -> str:
        """Return an opaque content identity used only by the Core conflict tracker."""

        current = _current_bytes(self.settings_path)
        if self._desktop is None and self._developer is None:
            if current is None:
                return "missing"
            return hashlib.sha256(b"present\0" + current).hexdigest()
        digest = hashlib.sha256(b"missing\0" if current is None else b"present\0" + current)
        if self._desktop is not None:
            digest.update(self._desktop.external_disk_identity().encode("ascii"))
        if self._developer is not None:
            digest.update(self._developer.external_disk_identity().encode("ascii"))
        return digest.hexdigest()

    def rebase_external_disk(self) -> dict[str, Any]:
        """Accept the current disk bytes as Apply's baseline without touching the draft."""

        current = _current_bytes(self.settings_path)
        self._baseline_bytes = current
        if self._desktop is not None:
            self._desktop.rebase_external_disk()
        if self._developer is not None:
            self._developer.rebase_external_disk()
        self._revision += 1
        return self.snapshot()

    def _safe_projection(self, settings: Mapping[str, Any]) -> dict[str, Any]:
        env = _mapping(settings.get("env", {}), "env")
        permissions = _normalise_permissions(settings)
        sandbox = _normalise_sandbox(settings)
        model = settings.get("model")
        if not isinstance(model, str) or not model:
            model = env.get("ANTHROPIC_MODEL") if isinstance(env.get("ANTHROPIC_MODEL"), str) else None

        safe_permissions: dict[str, Any] = {
            # Permission rules and excluded commands can carry local paths,
            # command arguments, or credentials. The structured snapshot only
            # indicates whether they exist; the native raw editor is the sole
            # route that can view or edit their full text.
            "allow": _redacted_list(permissions["allow"]),
            "ask": _redacted_list(permissions["ask"]),
            "deny": _redacted_list(permissions["deny"]),
            "additionalDirectories": _redacted_list(permissions["additionalDirectories"]),
        }
        if permissions.get("defaultMode") is not None:
            safe_permissions["defaultMode"] = permissions["defaultMode"]
        for key in _PERMISSION_DISABLE_LITERAL_FIELDS:
            if key in permissions:
                safe_permissions[key] = permissions[key]

        safe_sandbox: dict[str, Any] = {}
        for key in _SANDBOX_BOOLEAN_FIELDS:
            if key in sandbox:
                safe_sandbox[key] = sandbox[key]
        if "excludedCommands" in sandbox:
            safe_sandbox["excludedCommands"] = _redacted_list(sandbox["excludedCommands"])
        filesystem = _mapping(sandbox.get("filesystem", {}), "sandbox.filesystem")
        if filesystem:
            safe_filesystem: dict[str, Any] = {}
            for key in _FILESYSTEM_BOOLEAN_FIELDS:
                if key in filesystem:
                    safe_filesystem[key] = filesystem[key]
            for key in _FILESYSTEM_LIST_FIELDS:
                if key in filesystem:
                    safe_filesystem[key] = _redacted_list(filesystem[key])
            safe_sandbox["filesystem"] = safe_filesystem
        network = _mapping(sandbox.get("network", {}), "sandbox.network")
        if network:
            safe_network: dict[str, Any] = {}
            for key in _NETWORK_BOOLEAN_FIELDS:
                if key in network:
                    safe_network[key] = network[key]
            for key in _NETWORK_PORT_FIELDS:
                if key in network:
                    safe_network[key] = network[key]
            for key in _NETWORK_LIST_FIELDS:
                if key in network:
                    if key in {"allowedDomains", "deniedDomains", "allowUnixSockets"}:
                        safe_network[key] = _redacted_list(network[key])
                    else:
                        safe_network[key] = copy.deepcopy(network[key])
            safe_sandbox["network"] = safe_network

        result: dict[str, Any] = {
            "model": model if isinstance(model, str) else None,
            "token_configured": bool(env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")),
            "gateway_configured": bool(env.get("ANTHROPIC_BASE_URL")),
            "gateway_url": _public_gateway_url(env.get("ANTHROPIC_BASE_URL")),
            # A memory directory is a local path.  Its presence is useful to
            # the structured UI, but its value remains available only through
            # the trusted native raw editor.
            "autoMemoryDirectoryConfigured": bool(settings.get("autoMemoryDirectory")),
            "permissions": safe_permissions,
            "sandbox": safe_sandbox,
            "risk_confirmations": sorted(_risk_state(settings)),
            "file_exists": self._exists,
            "revision": self._revision,
        }
        for key in _TOP_LEVEL_STRING_FIELDS:
            if key != "model" and key in settings:
                result[key] = _normalise_auto_updates_channel(settings[key]) if key == "autoUpdatesChannel" else _string(settings[key], key)
        for key in _TOP_LEVEL_DISABLE_LITERAL_FIELDS:
            if key in settings:
                result[key] = _disable_literal(settings[key], key)
        for key in _TOP_LEVEL_UNIT_INTERVAL_FIELDS:
            if key in settings:
                result[key] = _unit_interval(settings[key], key, allow_zero=True)
        for key in _TOP_LEVEL_UNIT_FRACTION_FIELDS:
            if key in settings:
                result[key] = _unit_interval(settings[key], key, allow_zero=False)
        for key in _TOP_LEVEL_POSITIVE_INTEGER_FIELDS:
            if key in settings:
                result[key] = _positive_integer(settings[key], key)
        if "fallbackModel" in settings:
            result["fallbackModel"] = _normalise_fallback_model(settings["fallbackModel"])
        for key in _TOP_LEVEL_BOOLEAN_FIELDS:
            if key in settings:
                result[key] = settings[key]
        for key in _TOP_LEVEL_LIST_FIELDS:
            if key in settings:
                result[key] = copy.deepcopy(_string_list(settings[key], key))
        if "attribution" in settings:
            result["attribution"] = _normalise_attribution(settings)
        if "autoMode" in settings:
            result["autoMode"] = _normalise_auto_mode(settings)
        if "vimInsertModeRemaps" in settings:
            result["vimInsertModeRemaps"] = _normalise_vim_insert_mode_remaps(settings["vimInsertModeRemaps"])
        if "voice" in settings:
            result["voice"] = _normalise_voice(settings)
        if "skillOverrides" in settings:
            result["skillOverrides"] = _normalise_skill_overrides(settings["skillOverrides"])
        if "spinnerTipsOverride" in settings:
            result["spinnerTipsOverride"] = _normalise_spinner_tips_override(settings)
        if "spinnerVerbs" in settings:
            result["spinnerVerbs"] = _normalise_spinner_verbs(settings)
        if "worktree" in settings:
            result["worktree"] = _normalise_worktree(settings)
        if "cleanupPeriodDays" in settings:
            result["cleanupPeriodDays"] = _positive_integer(settings["cleanupPeriodDays"], "cleanupPeriodDays")
        return result

    def snapshot(self) -> dict[str, Any]:
        return {
            "domain": self.name,
            "revision": self._revision,
            "settings": self._safe_projection(self._draft),
            "desktop": self._desktop.snapshot() if self._desktop is not None else {"available": False},
            "developer": self._developer.snapshot() if self._developer is not None else {"available": False},
        }

    def draft_state(self) -> object:
        if self._desktop is None and self._developer is None:
            return copy.deepcopy(self._draft)
        result: dict[str, Any] = {"settings": copy.deepcopy(self._draft)}
        if self._desktop is not None:
            result["desktop"] = self._desktop.draft_state()
        if self._developer is not None:
            result["developer"] = self._developer.draft_state()
        return result

    def raw_text(self, *, include_sensitive: bool = False, document: str = "settings") -> str:
        """Return editor text only when explicitly requested by a trusted UI."""

        if document == "desktop":
            if self._desktop is None:
                raise ClaudeSettingsError("Claude Desktop configuration is unavailable")
            return self._desktop.raw_text(include_sensitive=include_sensitive)
        if document == "developer":
            if self._developer is None:
                raise ClaudeSettingsError("Claude Desktop developer settings are unavailable")
            return self._developer.raw_text()
        if document != "settings":
            raise ClaudeSettingsError("The requested editor is unavailable")
        value = self._draft if include_sensitive else redact(self._draft)
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def validate(self, payload: object | None = None) -> dict[str, Any]:
        candidate = self._draft if payload is None else _mapping(payload, "settings")
        errors = validate_settings(candidate)
        if self._desktop is not None:
            desktop_validation = self._desktop.validate()
            errors.extend(f"Claude Desktop: {error}" for error in desktop_validation["errors"])
        if self._developer is not None:
            developer_validation = self._developer.validate()
            errors.extend(f"Claude Desktop developer settings: {error}" for error in developer_validation["errors"])
        return {"valid": not errors, "errors": errors}

    def dispatch(self, action: str, payload: object | None = None) -> dict[str, Any]:
        data = _mapping(payload or {}, "payload")
        if action in {"developer_patch", "developerPatch"}:
            if self._developer is None:
                raise ClaudeSettingsError("Claude Desktop developer settings are unavailable")
            try:
                self._developer.patch(data)
            except ClaudeDesktopConfigError as exc:
                raise ClaudeSettingsError(str(exc)) from None
            self._revision += 1
            return self.snapshot()
        if action in {"desktop_patch", "desktopPatch"}:
            if self._desktop is None:
                raise ClaudeSettingsError("Claude Desktop configuration is unavailable")
            try:
                self._desktop.patch(data)
            except ClaudeDesktopConfigError as exc:
                raise ClaudeSettingsError(str(exc)) from None
            self._revision += 1
            return self.snapshot()
        if action in {"desktop_models_patch", "desktopModelsPatch"}:
            if self._desktop is None:
                raise ClaudeSettingsError("Claude Desktop configuration is unavailable")
            if set(data) != {"model_names"}:
                raise ClaudeSettingsError("Unknown Claude Desktop model-list field")
            try:
                self._desktop.set_model_names(data.get("model_names"))
            except ClaudeDesktopConfigError as exc:
                raise ClaudeSettingsError(str(exc)) from None
            self._revision += 1
            return self.snapshot()
        candidate = copy.deepcopy(self._draft)
        if action in {"set", "patch"}:
            _validate_structured_patch(data)
            normalized = _normalise_structured_patch(data)
            for key, value in normalized.items():
                if value is None:
                    candidate.pop(key, None)
                elif isinstance(value, Mapping) and isinstance(candidate.get(key), Mapping):
                    candidate[key] = _deep_merge(_mapping(candidate[key], key), value)
                else:
                    candidate[key] = copy.deepcopy(value)
        elif action in {"set_raw", "setRaw"}:
            document = data.get("document", "settings")
            raw_text = data.get("raw_json", data.get("rawJson", data.get("text")))
            if not isinstance(raw_text, str):
                raise ClaudeSettingsError("Claude Settings JSON must be text")
            if document == "desktop":
                if self._desktop is None:
                    raise ClaudeSettingsError("Claude Desktop configuration is unavailable")
                try:
                    self._desktop.set_raw_text(raw_text)
                except ClaudeDesktopConfigError as exc:
                    raise ClaudeSettingsError(str(exc)) from None
                self._revision += 1
                return self.snapshot()
            if document == "developer":
                if self._developer is None:
                    raise ClaudeSettingsError("Claude Desktop developer settings are unavailable")
                try:
                    self._developer.set_raw_text(raw_text)
                except ClaudeDesktopConfigError as exc:
                    raise ClaudeSettingsError(str(exc)) from None
                self._revision += 1
                return self.snapshot()
            if document != "settings":
                raise ClaudeSettingsError("The requested editor is unavailable")
            try:
                loaded = json.loads(raw_text)
            except json.JSONDecodeError:
                raise ClaudeSettingsError("Claude Settings contains invalid JSON") from None
            if not isinstance(loaded, dict):
                raise ClaudeSettingsError("Claude Settings JSON must be an object")
            candidate = loaded
        elif action in {"select_deployment", "selectDeployment"}:
            candidate = apply_litellm_deployment(candidate, data.get("deployment", data))
        elif action in {"patch_deployment", "patchDeployment"}:
            candidate = _patch_litellm_deployment(candidate, data.get("deployment", data))
        elif action in {"reset", "cancel", "reload"}:
            candidate = copy.deepcopy(self._raw)
            if self._desktop is not None:
                self._desktop.reset_draft()
            if self._developer is not None:
                self._developer.reset_draft()
        else:
            raise ClaudeSettingsError("Unknown Claude Settings action")
        errors = validate_settings(candidate)
        if errors:
            raise ClaudeSettingsError("Claude Settings contains invalid values")
        self._draft = candidate
        self._revision += 1
        return self.snapshot()

    def secret_present(self, field: str, target: str | None = None) -> bool:
        if target is not None:
            raise ClaudeSettingsError("The requested secret field is unavailable")
        if field == "desktop_gateway_api_key":
            if self._desktop is None:
                raise ClaudeSettingsError("The requested secret field is unavailable")
            return self._desktop.secret_present(field)
        if field == "auto_memory_directory":
            return bool(self._draft.get("autoMemoryDirectory"))
        if field != "deployment_token":
            raise ClaudeSettingsError("The requested secret field is unavailable")
        env = _mapping(self._draft.get("env", {}), "env")
        return bool(env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY"))

    def trusted_secret_value(self, field: str, target: str | None = None) -> str:
        if target is not None:
            raise ClaudeSettingsError("The requested secret field is unavailable")
        if field == "desktop_gateway_api_key":
            if self._desktop is None:
                raise ClaudeSettingsError("The requested secret field is unavailable")
            try:
                return self._desktop.secret_value(field)
            except ClaudeDesktopConfigError as exc:
                raise ClaudeSettingsError(str(exc)) from None
        if field != "deployment_token":
            raise ClaudeSettingsError("The requested secret field is unavailable")
        env = _mapping(self._draft.get("env", {}), "env")
        value = env.get("ANTHROPIC_AUTH_TOKEN")
        if not isinstance(value, str):
            value = env.get("ANTHROPIC_API_KEY")
        if not isinstance(value, str):
            return ""
        return value

    def stage_secret(self, field: str, target: str | None, value: str) -> None:
        if target is not None:
            raise ClaudeSettingsError("The requested secret field is unavailable")
        if field == "desktop_gateway_api_key":
            if self._desktop is None:
                raise ClaudeSettingsError("The requested secret field is unavailable")
            try:
                self._desktop.stage_secret(field, value)
            except ClaudeDesktopConfigError as exc:
                raise ClaudeSettingsError(str(exc)) from None
            self._revision += 1
            return
        if field == "auto_memory_directory":
            directory = _normalise_auto_memory_directory(value) if value else None
            if directory is None:
                self._draft.pop("autoMemoryDirectory", None)
            else:
                self._draft["autoMemoryDirectory"] = directory
            validation = self.validate()
            if not validation["valid"]:
                raise ClaudeSettingsError("Claude Settings contains invalid values")
            self._revision += 1
            return
        if field != "deployment_token":
            raise ClaudeSettingsError("The requested secret field is unavailable")
        env = _mapping(self._draft.get("env", {}), "env")
        if value:
            token_key = "ANTHROPIC_API_KEY" if "ANTHROPIC_AUTH_TOKEN" not in env and "ANTHROPIC_API_KEY" in env else "ANTHROPIC_AUTH_TOKEN"
            env[token_key] = value
        else:
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
            env.pop("ANTHROPIC_API_KEY", None)
        if env:
            self._draft["env"] = env
        else:
            self._draft.pop("env", None)
        validation = self.validate()
        if not validation["valid"]:
            raise ClaudeSettingsError("Claude Settings contains invalid values")
        self._revision += 1

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
            if confirmation in {"accepted", "claude-risk-confirmed"}:
                confirmed = risks
            elif isinstance(confirmation, str) and confirmation in risks:
                confirmed = [confirmation]
        confirmed_codes = set(_string_list(confirmed, "confirm_risks"))
        missing = [code for code in risks if code not in confirmed_codes]
        if missing:
            raise ConfirmationRequired(missing)
        if _current_bytes(self.settings_path) != self._baseline_bytes:
            raise ClaudeSettingsError("Claude Settings changed on disk; reload before applying")
        if self._desktop is not None:
            try:
                if self._desktop.external_disk_state()["changed"]:
                    raise ClaudeDesktopConfigError(
                        "Claude Desktop configuration changed on disk; reload before applying"
                    )
            except ClaudeDesktopConfigError as exc:
                raise ClaudeSettingsError(str(exc)) from None
        if self._developer is not None:
            try:
                if self._developer.external_disk_state()["changed"]:
                    raise ClaudeDesktopConfigError(
                        "Claude Desktop developer settings changed on disk; reload before applying"
                    )
            except ClaudeDesktopConfigError as exc:
                raise ClaudeSettingsError(str(exc)) from None

        code_dirty = self._draft != self._raw
        # Preserve the existing standalone Claude Code adapter contract for
        # explicit-path callers.  In production, a Desktop-only edit must not
        # create or rewrite ~/.claude/settings.json.
        write_code = code_dirty or self._desktop is None
        if write_code:
            try:
                text = json.dumps(self._draft, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            except (TypeError, ValueError):
                raise ClaudeSettingsError("Claude Settings contains invalid values") from None
            _atomic_write(self.settings_path, text)
            self._raw = copy.deepcopy(self._draft)
            self._exists = True
            self._baseline_bytes = text.encode("utf-8")
        if self._desktop is not None:
            try:
                self._desktop.apply()
            except ClaudeDesktopConfigError as exc:
                raise ClaudeSettingsError(str(exc)) from None
        if self._developer is not None:
            try:
                self._developer.apply()
            except ClaudeDesktopConfigError as exc:
                raise ClaudeSettingsError(str(exc)) from None
        self._revision += 1
        return {"applied": True, **self.snapshot()}

    def export(self, *, include_sensitive: bool = False) -> dict[str, Any]:
        settings = copy.deepcopy(self._draft)
        result: dict[str, Any] = {
            "domain": self.name,
            "settings": settings if include_sensitive else redact(settings),
        }
        if self._desktop is not None:
            result["desktop"] = (
                self._desktop.draft_state()
                if include_sensitive
                else redact(self._desktop.draft_state())
            )
        if self._developer is not None:
            result["developer"] = (
                self._developer.draft_state()
                if include_sensitive
                else redact(self._developer.draft_state())
            )
        return result

    def import_package(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            raise ClaudeSettingsError("Claude Settings package is invalid")
        data = copy.deepcopy(dict(payload))
        if data.get("domain", self.name) != self.name or set(data).difference(
            {"domain", "settings", "desktop", "developer"}
        ):
            raise ClaudeSettingsError("Claude Settings package is invalid")
        settings = data.get("settings")
        if not isinstance(settings, Mapping) or validate_settings(settings):
            raise ClaudeSettingsError("Claude Settings package is invalid")
        desktop = data.get("desktop")
        if desktop is not None:
            if self._desktop is None or not isinstance(desktop, Mapping):
                raise ClaudeSettingsError("Claude Desktop configuration is unavailable")
            config = desktop.get("config")
            if not isinstance(config, Mapping):
                raise ClaudeSettingsError("Claude Settings package is invalid")
            try:
                self._desktop.set_raw_text(
                    json.dumps(dict(config), ensure_ascii=False, sort_keys=True)
                )
            except ClaudeDesktopConfigError as exc:
                raise ClaudeSettingsError(str(exc)) from None
        developer = data.get("developer")
        if developer is not None:
            if self._developer is None or not isinstance(developer, Mapping):
                raise ClaudeSettingsError("Claude Desktop developer settings are unavailable")
            try:
                self._developer.set_raw_text(
                    json.dumps(dict(developer), ensure_ascii=False, sort_keys=True)
                )
            except ClaudeDesktopConfigError as exc:
                raise ClaudeSettingsError(str(exc)) from None
        self._draft = copy.deepcopy(dict(settings))
        self._revision += 1


def create_domain(*args: Any, **kwargs: Any) -> ClaudeSettingsDomain:
    return ClaudeSettingsDomain(*args, **kwargs)


__all__ = [
    "ClaudeDeployment",
    "ClaudeSettingsDomain",
    "ClaudeSettingsError",
    "ConfirmationRequired",
    "DOMAIN_NAME",
    "RISK_CONFIRMATION_CODES",
    "apply_litellm_deployment",
    "create_domain",
    "default_settings_path",
    "redact",
    "risk_confirmation_codes",
    "validate_settings",
]
