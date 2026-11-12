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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

from ... import claude_aliases
from ...atomic_io import atomic_write_text
from ..claude_desktop import (
    ClaudeDesktopConfig,
    ClaudeDesktopConfigError,
    ClaudeDeveloperSettings,
)
from ._shared import (
    DomainError,
    _action_name,
    _app_state_root,
    _default_provider_config_path,
)
from . import _shared_api
from ._shared_api import (
    anthropic_base_url,
    desktop_accepts_model_name,
    normalise_route,
    on_local_api,
    one_saved_route,
    read_generated_models,
    read_route_setting,
    saved_route_api_key,
    saved_routes,
    store_generated_models,
    store_route_setting,
)


DOMAIN_NAME = "claude"
_CLAUDE_NONSTREAM_FALLBACK_ENV = "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK"
SETTINGS_FILENAME = "settings.json"
DEFAULT_SETTINGS_DIR = ".claude"

RISK_CONFIRMATION_CODES = (
    "bypass_permissions",
    "sandbox_disabled",
    "filesystem_scope_broadened",
    "network_scope_broadened",
)

#: The two clients this domain owns.  Claude Code reads ``~/.claude/settings.json``
#: and Claude Desktop reads its third-party inference profile; an action that
#: changes one client's backend names which one it means.
_CLAUDE_CLIENTS = ("claudeCode", "claudeDesktop")

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
            raise ClaudeSettingsError("Claude Settings changed on disk; reload and try again")
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except ClaudeSettingsError:
        raise
    except OSError:
        raise ClaudeSettingsError("Claude Settings could not be saved") from None


def _write_error(_reason: str) -> Exception:
    return ClaudeSettingsError("Claude Settings could not be saved")


def _atomic_write(path: pathlib.Path, text: str) -> None:
    atomic_write_text(path, text, error=_write_error)


def default_settings_path() -> pathlib.Path:
    explicit = os.environ.get("CLAUDE_SETTINGS_PATH", "").strip()
    if explicit:
        return pathlib.Path(explicit).expanduser()
    root = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if root:
        root_path = pathlib.Path(root).expanduser()
        return root_path if root_path.suffix.lower() == ".json" else root_path / SETTINGS_FILENAME
    return pathlib.Path.home() / DEFAULT_SETTINGS_DIR / SETTINGS_FILENAME


#: Claude Code's compiled catalog entries this app names through
#: ``modelPicker[].behavesAs``.  A public gateway id is not in that catalog, so
#: without a substitute Claude Code assumes 200k and offers no Effort ladder;
#: ``behavesAs`` lends the row a known model's client-side handling without
#: changing the id it sends.  The 1M entry also carries the full
#: low..max ladder (``xhigh_effort`` and ``max_effort``).
_CODE_BEHAVES_AS_1M = "claude-opus-5-5"
#: The 1M entry with the smaller output claim (64k instead of 128k).  A route
#: between the client's 200k default and a full 1M needs a behavior model at
#: least as wide as its own window -- otherwise the tier, not the route, is the
#: ceiling and the session reports 200k for a 262k route -- while its output
#: claim should stay as small as the tier allows, because ``behavesAs`` is also
#: where the client reads a model's maximum output and nothing in
#: ``modelSettings`` can state that per route.
_CODE_BEHAVES_AS_LARGE = "claude-opus-4-8"
_CODE_BEHAVES_AS_DEFAULT = "claude-sonnet-4-6"


#: The width of the client's own small tier.  A route wider than this cannot be
#: described by ``_CODE_BEHAVES_AS_DEFAULT``.
_CODE_DEFAULT_TIER_WINDOW = 200_000

#: The window each substitute answers for.  A stored ``behavesAs`` whose window
#: is narrower than the route's own cannot express that route -- the tier would
#: be the ceiling and the session would read back the tier's number instead of
#: the route's -- so it is corrected.  A substitute at least as wide as the route
#: is left alone, even when this app happens to write the same name: it still
#: states the route correctly, and a person who picked it meant it.
_CODE_BEHAVES_AS_WINDOWS = {
    _CODE_BEHAVES_AS_1M: 1_000_000,
    _CODE_BEHAVES_AS_LARGE: 1_000_000,
    _CODE_BEHAVES_AS_DEFAULT: _CODE_DEFAULT_TIER_WINDOW,
}

#: Claude Code's per-model ``modelSettings.<model>.autoCompactWindow`` range.
#: The setting is the effective context window; the client caps it at the
#: behavior model's own maximum, so a 500k route needs a 1M ``behavesAs``.
_CODE_AUTO_COMPACT_MIN = 100_000
_CODE_AUTO_COMPACT_MAX = 1_000_000

#: Claude Code always renders a subtitle for a curated picker row: omitting
#: ``description`` produces ``Custom model (<id>)``.  A zero-width space is the
#: one value that survives the app's ``trim() || undefined`` fallback and draws
#: no text, which is what keeps a row reading as just its model name.
_CLAUDE_CODE_BLANK_DESCRIPTION = "\u200b"


def _code_behaves_as(name: str, max_input_tokens: object) -> str | None:
    """The known Claude Code model an unknown public id should behave as.

    A real Claude id is already in the client's catalog, so it needs no
    substitute.

    The substitute has to be at least as wide as the route, because the client
    caps the session at the behavior model's own window: the tier, not the
    route, would otherwise be the ceiling, and a 262k route read back as 200k.
    ``modelSettings.<model>.autoCompactWindow`` then narrows the effective
    window to the route's exact number, which is the number the client displays
    and compacts against.  So a route wider than the 200k tier gets a 1M entry,
    and which 1M entry is a question about output: ``behavesAs`` is also where
    the client reads a model's maximum output, nothing in ``modelSettings`` can
    state that per route, and the smaller claim is the safer one below a full
    1M.

    A resolved window counts here even when the user declared none, because the
    two ways of being wrong do not cost the same.  Too small is a *client-side*
    refusal -- measured: a 213k prompt on a route the client reads as 200k never
    reaches the router, and there is no elision pass to recover it.  Too large
    arrives, and ``proxy.context_window`` shortens the replay for Codex.  So the
    window a session is given follows the published number.
    """

    if claude_aliases.desktop_keeps_model_id(name):
        return None
    if not isinstance(max_input_tokens, int) or isinstance(max_input_tokens, bool):
        return _CODE_BEHAVES_AS_DEFAULT
    if max_input_tokens >= 1_000_000:
        return _CODE_BEHAVES_AS_1M
    if max_input_tokens > _CODE_DEFAULT_TIER_WINDOW:
        return _CODE_BEHAVES_AS_LARGE
    return _CODE_BEHAVES_AS_DEFAULT


def _code_auto_compact_window(max_input_tokens: object) -> int | str:
    """The per-model auto-compact window Claude Code should use.

    ``modelSettings.<model>.autoCompactWindow`` accepts 100k..1M or ``auto``.
    An unknown route width stays ``auto`` so the client's own model choice
    decides; a known one is clamped into the accepted range.
    """

    if not isinstance(max_input_tokens, int) or isinstance(max_input_tokens, bool):
        return "auto"
    return max(_CODE_AUTO_COMPACT_MIN, min(max_input_tokens, _CODE_AUTO_COMPACT_MAX))


def _claude_client(data: Mapping[str, Any]) -> str:
    """Which Claude app one action addresses.

    The two clients share this domain but not a document, and the pane draws
    one target control too.  An action names its client; anything else is the
    CLI, which is the surface this domain owned before Desktop joined it.
    """

    value = data.get("client")
    if value is None:
        return "claudeCode"
    if value not in _CLAUDE_CLIENTS:
        raise ClaudeSettingsError("The requested Claude client is unavailable")
    return str(value)


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
        runtime_config_path: pathlib.Path | str | None = None,
        runtime_settings_path: pathlib.Path | str | None = None,
        runtime_root: pathlib.Path | str | None = None,
    ):
        self.settings_path = pathlib.Path(settings_path).expanduser() if settings_path else default_settings_path()
        self._loader = loader or _safe_read
        # The saved-route actions ("use a specific API") read the same provider
        # document Codex reads, and the remembered route lives in the runtime
        # settings beside the catalog pin.  Both stay optional so a caller that
        # passes an explicit Claude path keeps that path as its only source.
        self.runtime_config_path = (
            pathlib.Path(runtime_config_path).expanduser()
            if runtime_config_path is not None
            else _default_provider_config_path()
        )
        # This app's own bookkeeping about a Claude client -- the route it was
        # last put on, and the model list generated for it -- lives in the
        # app-state root beside the Codex catalog's, never in the client's file.
        self.app_state_root = _app_state_root(
            runtime_root, runtime_settings_path, runtime_config_path
        )
        # The app's own proxy endpoint, resolved once per document load rather
        # than per snapshot: it reads the runtime settings and the large
        # runtime configuration while snapshots are polled with a window open.
        self._local_api_base_url: str | None = None
        self.refresh_local_api_endpoint()
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
        self._retire_generated_desktop_list()
        self._prefer_one_million_context()
        self._revision += 1
        return self.snapshot()

    def persistence_paths(self) -> tuple[pathlib.Path, ...]:
        paths = [self.settings_path]
        if self._desktop is not None:
            paths.extend(self._desktop.persistence_paths())
        if self._developer is not None:
            paths.extend(self._developer.persistence_paths())
        return tuple(dict.fromkeys(paths))

    def client_files(self) -> list[dict[str, Any]]:
        """Describe the Claude documents for the external-settings listing.

        Claude Desktop keeps its active third-party configuration in a
        library file named after the applied id; an empty library has no
        active file yet, so the listing falls back to the library directory
        that owns the next one.
        """

        rows: list[dict[str, Any]] = []

        def describe(
            document: str,
            path: pathlib.Path,
            *,
            app: str,
            name: str | None = None,
        ) -> None:
            try:
                details = path.lstat()
                exists = stat.S_ISREG(details.st_mode)
            except OSError:
                exists = False
            rows.append(
                {
                    "id": f"claude.{document}",
                    # Both apps are one entry in the pane's rail, and ``app``
                    # is what separates their file groups inside it.
                    "client": "claude",
                    "app": app,
                    "domain": self.name,
                    "document": document,
                    "name": name or path.name,
                    "path": str(path),
                    "language": "json",
                    "exists": exists,
                }
            )

        # Claude Code and Claude Desktop are one product with two apps:
        # ``~/.claude/settings.json`` belongs to the CLI, while the Desktop app
        # owns its third-party inference library and developer settings.  The
        # pane draws them under one Claude entry and groups the files by app,
        # so an edit never looks like it belongs to the other one.
        describe("settings", self.settings_path, app="claudeCode")
        if self._desktop is not None:
            active = self._desktop.desktop_config_path
            describe(
                "desktop",
                active if active is not None else self._desktop.config_library_path,
                app="claudeDesktop",
                name=None if active is not None else "configLibrary",
            )
        if self._developer is not None:
            describe("developer", self._developer.path, app="claudeDesktop")
        return rows

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

    # -- the clients' model lists -------------------------------------------

    def _public_model_rows(self) -> list[dict[str, object]]:
        """The routes a picker may offer, with the window each one publishes.

        The names come from the routes document and the numbers from
        ``ModelContextRegistry.record_for`` -- the same resolution the pane
        shows and the discovery view states.  Neither is read from this app's
        own ``/v1/models``: that answer is a second reading assembled by the
        proxy (LiteLLM resolves a window there from its cost map, which is not
        the window this app publishes), and it also means a background pass
        cannot refresh a client document while the proxy is starting, reloading,
        or stopped.
        """

        names = self._public_models()
        if not names:
            return []
        return [{"id": name, "max_input_tokens": self._published_window(name)} for name in names]

    def _public_models(self) -> list[str] | None:
        """The public names this app's own configuration serves, or ``None``.

        ``None`` and an empty list are different answers.  An unreadable or
        empty document is no observation and must not rewrite a client's picker,
        while a document that lists routes is the app's own statement about what
        it serves.  Ordering and the image-generator filter are
        ``codex_config.configured_exposed_models``', so every picker this app
        writes places a route in the same spot and never offers a route that
        cannot hold a conversation.
        """

        from .. import codex_config

        try:
            document = codex_config.load_yaml(self.runtime_config_path)
        except Exception:
            return None
        names = codex_config.configured_exposed_models(document)
        return names or None

    def code_model_list_names(self) -> list[str]:
        """The model IDs Claude Code's own picker currently offers.

        These are the ids the client sends, which for a third-party route is
        the derived alias (``_code_picker_id``), not the public name.
        """

        value = self._draft.get("modelPicker")
        options = value.get("options") if isinstance(value, Mapping) else None
        if not isinstance(options, list):
            return []
        names: list[str] = []
        for item in options:
            model = item.get("model") if isinstance(item, Mapping) else item
            if isinstance(model, str) and model.strip():
                names.append(model.strip())
        return names

    def code_model_list_public_names(self) -> list[str]:
        """The public routes this picker's ids name, in the picker's order.

        The generated list is compared with the router's public names, and a
        row names an alias; resolving one back is what makes that comparison a
        comparison of routes rather than of spellings.  An id this router does
        not serve (a route that was removed) resolves to itself, which reads as
        a difference and regenerates the list.
        """

        public = self._public_models() or []
        resolved: list[str] = []
        for model_id in self.code_model_list_names():
            resolved.append(claude_aliases.resolve_alias(model_id, public) or model_id)
        return resolved

    def code_model_list_enabled(self) -> bool:
        """Whether Claude Code's picker is this app's list, not its built-in one.

        The lineup alone is not the answer: a lineup that only *adds* rows
        leaves Claude's built-in models beside this router's names, and a user
        who came here to route Claude through this app would then be shown
        models it cannot serve.  The switch means "this list, alone", which is
        exactly ``replaceBuiltInOptions``.
        """

        value = self._draft.get("modelPicker")
        return isinstance(value, Mapping) and value.get("replaceBuiltInOptions") is True

    def desktop_model_list_enabled(self) -> bool:
        """Whether Claude Desktop's picker list is the one this app generated.

        Claude Desktop has a single model list, so "the switch is on" is the
        same question as "this file still carries the list this app wrote".
        The record of what was generated answers it: a profile copied in from
        elsewhere holds its own models, and claiming those as this app's would
        let a refresh overwrite a list the user brought with them.

        The test is containment, not equality, and it holds in both
        directions.  Desktop deletes every entry it cannot map onto an Anthropic
        model, so a list this app wrote can come back *shorter* than the record;
        and a write that failed after the record was stored leaves the file
        *longer* than it.  Equality would read either state as "off" and no
        refresh would ever repair the file -- which is exactly where a user is
        left holding a warning this app cannot clear for them.
        """

        if self._desktop is None:
            return False
        generated = set(read_generated_models(self.app_state_root, "claudeDesktop"))
        if not generated:
            return False
        current = set(self._desktop.snapshot().get("model_names") or [])
        return current.issubset(generated) or generated.issubset(current)

    def desktop_model_entries(self) -> list[dict[str, str]]:
        """The ``inferenceModels`` rows for this router's public models.

        Derived, never configured: a public name Claude Desktop keeps is
        written as itself, so the picker renders the model's own display name
        and Effort control; every other public name gets the alias
        ``claude_aliases`` derives for it, with the public name as
        ``labelOverride`` and the proxy resolving that alias back.  A route
        added later is covered without anybody editing a list.
        """

        names = self._public_models() or []
        aliases = claude_aliases.alias_index(names)
        rows: list[dict[str, str]] = []
        for name in names:
            if claude_aliases.desktop_keeps_model_id(name):
                rows.append({"name": name})
                continue
            alias = claude_aliases.claude_desktop_alias(name)
            if aliases.get(alias) != name:
                continue
            rows.append({"name": alias, "labelOverride": name})
        # Rows keep the order of the public names they were derived from, and
        # that order is already the picker's: ``codex_config.local_exposed_models``
        # answers every name this app writes alphabetically, case-insensitively,
        # and a row's ``labelOverride`` is that same public name -- so the label a
        # person reads is what the list is ordered by, without a sort here.
        return rows

    def _published_window(self, name: str) -> int | None:
        """The window this app publishes for one route.

        ``ModelContextRegistry.record_for`` is the single resolution the pane's
        published context and the managed Codex catalog already read, and
        ``proxy.claude_desktop_listing`` asks it for the discovery view.  Asking
        it here is what makes the terminal's number the same number as the one a
        person sees in the app: the two used to differ whenever LiteLLM's
        listing stated nothing for a route the registry had an answer for, and
        the session then reported the client's 200k default on top of it.

        At or above 1M the answer is exactly 1M -- the client has one
        large-window spelling and its per-model setting accepts 100k..1M.
        """

        try:
            from ..model_catalog import _image_filter_registry

            window = int(_image_filter_registry().record_for(name).context_window)
        except Exception:
            return None
        if window <= 0:
            return None
        return min(window, 1_000_000)

    def _code_picker_id(self, name: str, max_input_tokens: object = None) -> str:
        """The id Claude Code's picker names for one public route.

        One id space for both Claude apps.  Claude Desktop's Code tab launches
        this same Claude Code with the id the discovery view published -- the
        derived alias, a public name the client keeps as written, and a 1M
        route's spelling with the client's ``[1m]`` suffix -- so a picker keyed
        by the public name would describe a model the desktop session never
        asks for, and every context setting would have to be written twice
        (once per spelling).  Naming the published id here is what makes one
        row, one ``modelSettings`` entry and one behavior target serve both
        apps; the proxy resolves either spelling back through
        ``claude_aliases``.

        A route this app publishes at 1M carries the suffix too, because that
        is the id the desktop session sends: one route under two spellings would
        split ``modelSettings`` again the moment one of the two apps addressed
        it.  It also states the window by name instead of only by substitute
        (``behavesAs``), and the discovery view suffixes the same set from the
        same number (``desktop_model_rows``), so the two agree by construction.
        A public name the client already keeps is suffixed there too, for the
        same reason: without it the picker answers its own catalogue row with a
        second, standard entry beside the 1M one.

        ``max_input_tokens`` is the number this app publishes for the route --
        the one its own pane shows and its ``/v1/models`` answers.  At or above
        1M that number *is* 1M: the client has one large-window spelling, and
        every surface here clamps to the same ceiling.
        """

        base = (
            name
            if claude_aliases.desktop_keeps_model_id(name)
            else claude_aliases.claude_desktop_alias(name)
        )
        if (
            isinstance(max_input_tokens, int)
            and not isinstance(max_input_tokens, bool)
            and max_input_tokens >= 1_000_000
        ):
            return f"{base}{claude_aliases.CONTEXT_SUFFIX}"
        return base

    def _code_route_window(self, name: str) -> object:
        """The window this app states for one of its own routes.

        ``_published_window`` first, then the ``max_input_tokens`` the app's own
        model rows carry -- the two readings ``_code_picker`` and
        ``_code_model_settings`` already take, kept in one place so a launch id
        cannot be spelled from a third, wider one.
        """

        window = self._published_window(name)
        if window is not None:
            return window
        for row in self._public_model_rows():
            if str(row.get("id") or "") == name:
                return row.get("max_input_tokens")
        return None

    def _code_published_id(self, name: str) -> str:
        """The id this app publishes for a route, or ``name`` unchanged.

        A client pointed at this proxy has to name the route the way this app
        publishes it.  The public name is not that: a gateway id has no
        catalogue entry in the client, so a session launched with it answers
        the client's 200k default however wide the route is, and the row that
        carries the window (``behavesAs`` and the ``[1m]`` name) is looked up
        by id.  A name this router does not serve -- a deployment id, a model
        written by hand -- comes back as it was.
        """

        raw = str(name or "").strip()
        served = self._public_models() or []
        if not raw or not served:
            return name
        stripped = claude_aliases.context_suffix_free(raw)
        if stripped in served:
            route = stripped
        else:
            resolved = claude_aliases.resolve_alias(raw, served)
            if resolved is None:
                return name
            route = resolved
        return self._code_picker_id(route, self._code_route_window(route))

    def _code_launch_model_apply(
        self,
        candidate: dict[str, Any],
        names: Sequence[str],
        options: object,
    ) -> bool:
        """Launch the client with the id this picker names for its route.

        The file names every route by the id the client sends
        (``_code_picker_id``) and ``behavesAs`` rides on that row, so a client
        launched with the public name has nothing to look it up by: its session
        reports the client's 200k default and drops the Effort ladder, however
        wide the route is.  One id space means the launch spelling is moved
        onto the row's own id -- a 1M route is launched as ``<id>[1m]``, which
        is the name stating the window.

        Only while this client's env names this proxy (a client on another
        provider has no row here), and only onto an id this file actually
        names: a deployment id or a model written by hand resolves to no row
        and stays.
        """

        env = candidate.get("env")
        if not isinstance(env, Mapping):
            return False
        if not self._code_uses_local_api(candidate):
            return False
        rows = options if isinstance(options, list) else []
        published = {
            name: row["model"]
            for name, row in zip(names, rows)
            if isinstance(row, Mapping) and isinstance(row.get("model"), str)
        }
        if not published:
            return False
        chosen = env.get("ANTHROPIC_MODEL")
        if not isinstance(chosen, str) or not chosen.strip():
            chosen = candidate.get("model")
        if not isinstance(chosen, str) or not chosen.strip():
            return False
        current = chosen.strip()
        stripped = claude_aliases.context_suffix_free(current)
        route = stripped if stripped in published else claude_aliases.resolve_alias(current, published)
        wanted = published.get(route) if isinstance(route, str) else None
        if not wanted or wanted == current:
            return False
        if isinstance(env.get("ANTHROPIC_MODEL"), str):
            env = dict(env)
            env["ANTHROPIC_MODEL"] = wanted
            candidate["env"] = env
        current_model = candidate.get("model")
        if isinstance(current_model, str) and current_model.strip() == current:
            candidate["model"] = wanted
        return True

    def _code_picker(self, names: Sequence[str], existing: object = None) -> dict[str, Any]:
        """A picker lineup for one public-model list.

        A row this app already wrote keeps every field it carries except the
        model ID: the lineup is this app's list, but a label or a
        ``behavesAs`` the user added is theirs, and regenerating the file must
        not silently drop it.

        The row names the id the client sends (``_code_picker_id``) and shows
        the public name as its label, so the terminal and the desktop Code tab
        address one model the same way.  ``behavesAs`` is what gives the client
        the route's context window and Effort ladder; ``description`` blank
        keeps the row from showing the ``Custom model (...)`` subtitle the
        client would otherwise add.
        """

        preserved: dict[str, dict[str, Any]] = {}
        options = existing.get("options") if isinstance(existing, Mapping) else None
        if isinstance(options, list):
            for item in options:
                if not isinstance(item, Mapping):
                    continue
                model = item.get("model")
                if isinstance(model, str) and model.strip():
                    preserved.setdefault(model.strip(), dict(item))
        known = {str(row.get("id")): row for row in self._public_model_rows()}
        rows: list[dict[str, Any]] = []
        for name in names:
            window = self._published_window(name)
            if window is None:
                window = (known.get(name) or {}).get("max_input_tokens")
            model_id = self._code_picker_id(name, window)
            # A list written before the ids were aliases carried the public
            # name, and a field the user typed there is theirs: adopt it onto
            # the id the row names now.
            row = dict(
                preserved.get(model_id)
                or preserved.get(name)
                or preserved.get(self._code_picker_id(name))
                or {}
            )
            row["model"] = model_id
            route = known.get(name)
            if route is not None:
                row.setdefault("description", _CLAUDE_CODE_BLANK_DESCRIPTION)
                if model_id != name and not claude_aliases.desktop_keeps_model_id(name):
                    # The picker shows this; the client sends the id.  A label
                    # this app used to add to separate the two rows is ours, not
                    # the user's, and now that there is one row it is just a
                    # second name for one model.  A public name the client keeps
                    # is not labelled: its own catalogue already reads the id,
                    # suffix or not, and this app's spelling of it would only
                    # replace the friendly name the client draws.
                    if row.get("label") in (None, "", f"{name} (Desktop)"):
                        row["label"] = name
                behaves_as = _code_behaves_as(name, window)
                if behaves_as is not None:
                    current = row.get("behavesAs")
                    # The substitute is this app's own choice of tier, and the
                    # tier is what caps the window -- so a stored one of ours is
                    # replaced whenever the route's window moved out from under
                    # it (a 500k route left on the 200k tier read back as 200k).
                    # A target none of these three names is a person's and stays.
                    current_window = _CODE_BEHAVES_AS_WINDOWS.get(current)
                    if (
                        current in (None, "")
                        or current_window is None
                        or (
                            isinstance(window, int)
                            and not isinstance(window, bool)
                            and current_window < window
                        )
                    ):
                        row["behavesAs"] = behaves_as
            rows.append(row)
        lineup: dict[str, Any] = {"replaceBuiltInOptions": True, "options": rows}
        if isinstance(existing, Mapping):
            for key, value in existing.items():
                if key not in lineup:
                    lineup[key] = copy.deepcopy(value)
        return lineup

    def _code_model_settings(self, names: Sequence[str], existing: object = None) -> dict[str, Any]:
        """Per-model settings Claude Code needs for the same rows.

        ``modelPicker`` carries the id and the behavior model; the context
        number itself lives in ``modelSettings.<model>``.
        ``autoCompactWindow`` is the only per-model context field in Claude
        Code, and without it a 500k route that behaves as a 1M model would show
        1M.  Every other field of an existing entry (a user's own effort or
        window) is preserved.

        Two ids describe one route, and both are written.  The picker names the
        row ``<id>[1m]``, but a session runs the suffix-free id: measured on the
        desktop Code tab, the session behind a ``claude-mythos-eb1394[1m]`` row
        wrote ``claude-mythos-eb1394`` as its model.  A number stated only under
        the suffixed spelling therefore reached no session -- the base id kept
        the ``200000`` this app wrote while the route was on the client's 200k
        tier, and the session showed 200k for a route published at 1M.  So the
        row's id carries the number and so does the id a session actually runs.

        An entry already written under the public name is kept rather than
        moved: nothing this app generates launches with that spelling any more,
        but a person who types it (``claude --model <name>``) still gets the
        number they had, and an entry nobody wrote is never invented.
        """

        settings: dict[str, Any] = {}
        if isinstance(existing, Mapping):
            for model, value in existing.items():
                if isinstance(model, str) and isinstance(value, Mapping):
                    settings[model] = dict(value)
        known = {str(row.get("id")): row for row in self._public_model_rows()}
        for name in names:
            route = known.get(name)
            if route is None:
                continue
            window = self._published_window(name)
            if window is None:
                window = route.get("max_input_tokens")
            model_id = self._code_picker_id(name, window)
            base_id = claude_aliases.context_suffix_free(model_id)
            desired = _code_auto_compact_window(window)
            seed = settings.get(model_id) or settings.get(base_id) or settings.get(name) or {}
            for launch_id in dict.fromkeys((model_id, base_id)):
                entry = dict(settings.get(launch_id) or seed)
                stored = entry.get("autoCompactWindow", desired)
                # ``auto`` is this app's marker for "nothing is known about this
                # route's window", and it is what a pass wrote while the number
                # came from the listing (which states nothing for a route
                # LiteLLM's cost map does not carry).  Once the route resolves,
                # the number replaces it -- otherwise the session keeps the
                # client's 200k default on top of a route this app publishes at
                # 500k.
                if stored == desired or stored == "auto":
                    entry["autoCompactWindow"] = desired
                elif launch_id == base_id:
                    # The id a session runs states what this app publishes for
                    # the route.  A number this app wrote there while the route
                    # sat on another tier is its own stale claim, not a choice:
                    # the row moved, so the window moves with it.
                    entry["autoCompactWindow"] = desired
                else:
                    # A row's own entry holds the user's number, if they set one.
                    entry.setdefault("autoCompactWindow", desired)
                settings[launch_id] = entry
        # A key this app wrote under a superseded spelling is not the user's
        # writing: one route described twice would grow an entry per model on
        # every change.  Only an entry holding nothing but the number this app
        # writes is retired, and only when it now agrees -- a value that differs
        # is theirs.  The ids a current route is launched by are not stale.
        for name in names:
            window = self._published_window(name)
            if window is None:
                window = (known.get(name) or {}).get("max_input_tokens")
            model_id = self._code_picker_id(name, window)
            launched = {model_id, claude_aliases.context_suffix_free(model_id)}
            current = settings.get(model_id)
            if not isinstance(current, Mapping):
                continue
            for stale_key in {name, model_id} - launched:
                stale = settings.get(stale_key)
                if not isinstance(stale, Mapping):
                    continue
                if set(stale) - {"autoCompactWindow"}:
                    continue
                stale_window = stale.get("autoCompactWindow")
                # ``auto`` is this app's marker for a window it did not know, so
                # an entry holding only that is the app's own writing and not a
                # second opinion about the route.
                if stale_window != current.get("autoCompactWindow") and stale_window != "auto":
                    continue
                settings.pop(stale_key, None)
        return settings

    def _code_model_list_apply(self, enabled: bool, names: Sequence[str] | None = None) -> dict[str, Any]:
        """Set (or drop) Claude Code's picker lineup from this app's list."""

        candidate = copy.deepcopy(self._draft)
        if not enabled:
            candidate.pop("modelPicker", None)
            return candidate
        resolved = list(names) if names is not None else self._public_models()
        if not resolved:
            raise DomainError("Young Router is not running or exposes no models")
        lineup = self._code_picker(resolved, candidate.get("modelPicker"))
        candidate["modelPicker"] = lineup
        settings = self._code_model_settings(resolved, candidate.get("modelSettings"))
        if settings:
            candidate["modelSettings"] = settings
        else:
            candidate.pop("modelSettings", None)
        # The client's own launch model is part of this list: a session started
        # from the public name has no row to read its window from.
        self._code_launch_model_apply(candidate, resolved, lineup.get("options"))
        return candidate

    def _desktop_model_list_apply(self, enabled: bool, names: Sequence[str] | None = None) -> None:
        """Write the picker's list, always with the labels its rows read as.

        ``names`` narrows the write to a caller's list; it never replaces the
        derivation.  Building ``{"name": ...}`` from those ids directly is how a
        background refresh silently dropped every ``labelOverride`` and left the
        picker showing the derived ids instead of the models they stand for.
        """

        if self._desktop is None:
            raise ClaudeSettingsError("Claude Desktop configuration is unavailable")
        if not enabled:
            try:
                self._desktop.set_model_names([])
            except ClaudeDesktopConfigError as exc:
                raise ClaudeSettingsError(str(exc)) from None
            return
        derived = self.desktop_model_entries()
        if names is None:
            entries = derived
        else:
            wanted = list(names)
            entries = [row for row in derived if row["name"] in wanted]
        # Only ids Desktop keeps are written.  Handing it the rest makes it
        # delete them on the next launch -- with a warning the user cannot act
        # on from inside this app -- and leaves the switch claiming a list the
        # file no longer holds.  An alias it derives always qualifies, so this
        # refuses only a router that publishes nothing at all.
        accepted = [row["name"] for row in entries if desktop_accepts_model_name(row["name"])]
        if not accepted:
            raise DomainError(
                "Young Router is not running or exposes no models"
            )
        labels = {row["name"]: row["labelOverride"] for row in entries if row.get("labelOverride")}
        try:
            self._desktop.set_model_names(accepted, labels=labels)
        except ClaudeDesktopConfigError as exc:
            raise ClaudeSettingsError(str(exc)) from None

    def refresh_model_lists(self) -> bool:
        """Regenerate Claude Code's picker when the public list outgrew it.

        The Codex catalog's equivalent, and for the same reason: a client's
        picker was generated from this router's public models, so a route that
        was added, removed, or renamed has to reach that picker without the
        user reopening the pane.

        Only Claude Code still takes a generated list.  Claude Desktop's picker
        now discovers the alias view ``/v1/models`` publishes for its own
        request, so no background pass writes ``inferenceModels``; the list is
        retired at reload instead.  Three gates keep the remaining list from
        fighting the user: it is touched only while its switch is on; an
        observation that is merely empty (the proxy still loading, a worker
        reload between groups) is not a change; and a document holding an edit
        the user has not applied is left alone, because writing the list would
        commit that edit too.  The write is its own step --
        ``_persist_generated_lists`` -- rather than the ordinary Apply: Apply
        asks for confirmation of the *document's* risk settings, and a user who
        has already applied ``bypassPermissions`` must not have every refresh
        refused by a prompt nobody sees.
        """

        names = self._public_models()
        if not names:
            return False
        # A draft the user is still editing belongs to them.  The next pass,
        # after they apply it, finds the file clean and does the work then.
        code_free = self._draft == self._raw
        saved_code = copy.deepcopy(self._draft)
        generated: dict[str, list[str]] = {}

        if code_free and self.code_model_list_enabled():
            recorded = read_generated_models(self.app_state_root, "claudeCode")
            names_changed = set(recorded) != set(names) or self.code_model_list_public_names() != list(names)
            # The document is compared with what regenerating it would produce,
            # not only with the names: a file that differs at all -- a label a
            # previous release added for the other Claude app, one model's
            # window -- is one the client reads wrong until it is rewritten.
            # Rendering alone needs the route rows (the windows) to be readable,
            # so without them only a change of names regenerates: the rows are
            # then kept as they are, and the pass that can read them settles the
            # rest.
            rows = self._public_model_rows() if not names_changed else []
            if names_changed or rows:
                candidate = self._code_model_list_apply(True, names)
                if validate_settings(candidate):
                    raise ClaudeSettingsError("Claude Settings contains invalid values")
                stale = names_changed or any(
                    candidate.get(key) != self._draft.get(key)
                    for key in ("modelPicker", "modelSettings", "model", "env")
                )
                if stale:
                    self._draft = candidate
                    generated["claudeCode"] = list(names)

        if not generated:
            return False
        try:
            wrote = self._persist_generated_lists()
        except Exception:
            # The refresh is background work.  A file that changed underneath it
            # or cannot be written leaves the user's own state exactly as it was,
            # and the next pass tries again.
            self._draft = saved_code
            return False
        if not wrote:
            return False
        for client, models in generated.items():
            store_generated_models(self.app_state_root, client, models)
        self._revision += 1
        return True

    def _persist_generated_lists(self) -> bool:
        """Write only Claude Code's generated list to its document.

        The ordinary ``apply`` is the wrong writer here: it commits every staged
        field and gates on the document's risk confirmations, and a refresh owns
        neither.  Claude Desktop has no generated list any more; its discovery
        reads ``/v1/models``, so this writer never touches that profile.
        """

        wrote = False
        if self._draft != self._raw:
            if _current_bytes(self.settings_path) != self._baseline_bytes:
                raise ClaudeSettingsError("Claude Settings changed on disk; reload and try again")
            try:
                text = json.dumps(self._draft, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            except (TypeError, ValueError):
                raise ClaudeSettingsError("Claude Settings contains invalid values") from None
            _atomic_write(self.settings_path, text)
            self._raw = copy.deepcopy(self._draft)
            self._exists = True
            self._baseline_bytes = text.encode("utf-8")
            wrote = True
        return wrote

    # -- this app's proxy, and the saved route a client leaves for it -------

    def refresh_local_api_endpoint(self) -> None:
        """Resolve this app's proxy endpoint for the "use this API" action."""

        try:
            self._local_api_base_url = _shared_api.endpoint(
                self.runtime_config_path
            )[0]
        except Exception:
            self._local_api_base_url = None

    def _code_env(self) -> dict[str, Any]:
        return _mapping(self._draft.get("env", {}), "env")

    def code_uses_local_api(self) -> bool:
        """True when Claude Code's own ``env`` already names this proxy."""

        return self._code_uses_local_api(self._draft)

    def _code_uses_local_api(self, document: Mapping[str, Any]) -> bool:
        """The same question for a document that is still a candidate."""

        if not self._local_api_base_url:
            return False
        env = document.get("env")
        base_url = env.get("ANTHROPIC_BASE_URL") if isinstance(env, Mapping) else None
        return on_local_api(base_url, versioned=False)

    def code_nonstream_fallback_enabled(self) -> bool:
        """True when Claude Code is told not to re-send a failed stream whole."""

        env = self._code_env()
        return str(env.get(_CLAUDE_NONSTREAM_FALLBACK_ENV) or "").strip() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def set_code_nonstream_fallback(self, enabled: bool) -> None:
        """Write Claude Code's non-streaming fallback switch and persist it.

        The switch is a client setting, not a proxy one: when it is on, a
        stream that fails mid-answer is retried as a stream instead of
        re-sending the whole turn as one body.  The disk document is the last
        one loaded (``_raw``) so toggling does not save unrelated unsaved
        edits; the draft receives the same env key so the pane and the next
        Apply agree.
        """

        if not isinstance(self._raw, dict):
            self._raw = {}
        for document in (self._raw, self._draft):
            if not isinstance(document, dict):
                continue
            env = dict(_mapping(document.get("env", {}), "env"))
            if enabled:
                env[_CLAUDE_NONSTREAM_FALLBACK_ENV] = "1"
            else:
                env.pop(_CLAUDE_NONSTREAM_FALLBACK_ENV, None)
            document["env"] = env
        errors = validate_settings(self._raw)
        if errors:
            raise ClaudeSettingsError(errors[0])
        if _current_bytes(self.settings_path) != self._baseline_bytes:
            raise ClaudeSettingsError(
                "Claude Settings changed on disk; reload and try again"
            )
        text = json.dumps(self._raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        _atomic_write(self.settings_path, text)
        self._exists = True
        self._baseline_bytes = text.encode("utf-8")

    def desktop_uses_local_api(self) -> bool:
        if self._desktop is None or not self._local_api_base_url:
            return False
        return on_local_api(self._desktop.gateway_url(), versioned=False)

    def _route_named(self, model: str) -> dict[str, str] | None:
        """Resolve a client's own model name back to one saved route.

        Only a name exactly one saved route answers resolves.  Two providers
        can expose the same public model behind two keys, and picking the first
        would put the client's off direction on a route it was never on.
        """

        if not model:
            return None
        try:
            _, rows = saved_routes(self.runtime_config_path)
        except DomainError:
            return None
        matches = [row for row in rows if str(row.get("model") or "").strip() == model]
        if len(matches) != 1:
            return None
        row = matches[0]
        return normalise_route(
            {
                "model": row.get("model"),
                "provider": row.get("provider"),
                "deployment_id": row.get("deployment_id"),
            }
        )

    def client_route(self, client: str) -> dict[str, str] | None:
        """The saved route this client is on, or the one it last left for us.

        The remembered route answers while the client points at this app's
        proxy, because the proxy serves public model names and several saved
        routes may share one: the client's own document has nothing left to
        identify the route it came from.  A client that points somewhere else
        answers from its own document instead, so a route the user configured
        by hand is never reported as this app's choice.
        """

        remembered = read_route_setting(self.app_state_root, client)
        if client == "claudeDesktop":
            if self.desktop_uses_local_api():
                return remembered
            model_names = (
                self._desktop.snapshot().get("model_names", []) if self._desktop is not None else []
            )
            model = str(model_names[0]).strip() if model_names else ""
            return self._route_named(model) or remembered
        if self.code_uses_local_api():
            return remembered
        env = self._code_env()
        model = _string(env.get("ANTHROPIC_MODEL"), "env.ANTHROPIC_MODEL") or _string(
            self._draft.get("model"), "model"
        )
        return self._route_named(model or "") or remembered

    def _prefer_one_million_context(self) -> None:
        """Show the 1M option, not a standard 200k row, for a 1M route.

        Discovery states a route's context through ``max_input_tokens``, and
        Claude Desktop answers it with two picker rows unless the profile asks
        for the 1M option alone.  The standard row assumes 200k, which is the
        wrong number for a 1M route.  Only an absent key is set: a user who
        turned the preference off keeps it.

        The preference is a choice about the whole picker rather than about one
        row, so it follows whether this app publishes a 1M route at all -- the
        number its own pane shows.  Every 1M row this app writes is either a
        published alias (whose id already carries the client's ``[1m]``
        spelling, so it has no variant to collapse) or a Claude-shaped id whose
        own catalogue entry exists, which is why the rows it keeps still
        answer.
        """

        if self._desktop is None or self._desktop.is_dirty():
            return
        if not self.desktop_uses_local_api():
            return
        if not self._publishes_one_million_context():
            return
        state = self._desktop.draft_state()
        config = state.get("config") if isinstance(state, Mapping) else None
        if not isinstance(config, Mapping) or "modelPrefer1mContext" in config:
            return
        try:
            self._desktop.patch({"modelPrefer1mContext": True})
            self._desktop.apply()
        except Exception:
            return

    def _publishes_one_million_context(self) -> bool:
        """Whether this app publishes any served route at 1M.

        The number is the one the router's own ``/v1/models`` answers, which is
        the same number the pane shows and the same one ``_code_picker_id``
        suffixes -- so the preference, the picker row and the discovery row all
        follow one statement.  A route at or above 1M counts as 1M: the client
        has a single large-window spelling, and every surface here clamps to the
        same ceiling.
        """

        for row in self._public_model_rows():
            window = row.get("max_input_tokens")
            if isinstance(window, int) and not isinstance(window, bool) and window >= 1_000_000:
                return True
        return False

    def _retire_generated_desktop_list(self) -> None:
        """Drop the list this app used to write into Claude Desktop's profile.

        Claude Desktop now discovers the alias view ``/v1/models`` publishes for
        its own request, so a generated ``inferenceModels`` list would turn that
        discovery off (the app skips it while the list holds no tier alias) and
        keep the picker tied to a file nobody refreshes any more.  Only a list
        whose record proves this app wrote it is cleared; a hand-written profile
        and the single route the designated-route action writes are left alone.
        """

        if self._desktop is None or self._desktop.is_dirty():
            return
        current = list(self._desktop.snapshot().get("model_names") or [])
        if not current:
            if read_generated_models(self.app_state_root, "claudeDesktop"):
                store_generated_models(self.app_state_root, "claudeDesktop", [])
            return
        recorded = read_generated_models(self.app_state_root, "claudeDesktop")
        # The record is the proof; a derived alias is the mark.  A record can
        # be lost by an older release while the profile still holds the list
        # (observed after the 2026-11-05 upgrade), so an id in this app's own
        # alias namespace must still retire it.  A hand-written profile with
        # real Claude names has neither mark and is left alone.
        if not (recorded and self.desktop_model_list_enabled()) and not any(
            claude_aliases.is_derived_alias(name) for name in current
        ):
            return
        try:
            self._desktop.set_model_names([])
            self._desktop.apply()
            store_generated_models(self.app_state_root, "claudeDesktop", [])
        except Exception:
            return

    def snapshot(self) -> dict[str, Any]:
        return {
            "domain": self.name,
            "revision": self._revision,
            "settings": self._safe_projection(self._draft),
            "desktop": self._desktop.snapshot() if self._desktop is not None else {"available": False},
            "developer": self._developer.snapshot() if self._developer is not None else {"available": False},
            # The two clients share one domain but not one backend: each carries
            # its own switch, its own designated route, and its own model list,
            # so the pane never draws one client's state on the other's row.
            "clients": {
                "claudeCode": {
                    "uses_local_api": self.code_uses_local_api(),
                    "designated_route": self.client_route("claudeCode"),
                    "model_list_enabled": self.code_model_list_enabled(),
                    "nonstream_fallback_enabled": self.code_nonstream_fallback_enabled(),
                },
                "claudeDesktop": {
                    "uses_local_api": self.desktop_uses_local_api(),
                    "designated_route": self.client_route("claudeDesktop"),
                    "model_list_enabled": self.desktop_model_list_enabled(),
                },
            },
            # The saved routes the designate control offers.  Only identity and
            # the labels the menu shows: no endpoint, no key.
            "models": self._saved_route_rows(),
        }

    def _saved_route_rows(self) -> list[dict[str, Any]]:
        try:
            _, rows = saved_routes(self.runtime_config_path)
        except DomainError:
            return []
        return [
            {
                "model": str(row.get("model") or ""),
                "provider": str(row.get("provider") or ""),
                "deployment_id": str(row.get("deployment_id") or ""),
                "api_key_name": str(row.get("api_key_name") or ""),
                "upstream_url_surface": str(row.get("upstream_url_surface") or ""),
            }
            for row in rows
            if row.get("model") and row.get("provider") and row.get("deployment_id")
        ]

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
        # One spelling per action: the UI names an action with dots, a direct
        # Core caller may use underscores, and the two must reach the same
        # branch instead of one of them landing on "unknown action".
        name = _action_name(action)
        if name in {"developer_patch", "developerpatch"}:
            if self._developer is None:
                raise ClaudeSettingsError("Claude Desktop developer settings are unavailable")
            try:
                self._developer.patch(data)
            except ClaudeDesktopConfigError as exc:
                raise ClaudeSettingsError(str(exc)) from None
            self._revision += 1
            return self.snapshot()
        if name in {"desktop_patch", "desktoppatch"}:
            if self._desktop is None:
                raise ClaudeSettingsError("Claude Desktop configuration is unavailable")
            try:
                self._desktop.patch(data)
            except ClaudeDesktopConfigError as exc:
                raise ClaudeSettingsError(str(exc)) from None
            self._revision += 1
            return self.snapshot()
        if name in {"use_local_api", "uselocalapi"}:
            # Point one Claude client at this app's own proxy and adopt its
            # master key.  Claude Code names its gateway in ``env``; Claude
            # Desktop names it in its third-party inference profile.  Both
            # store the Messages root, which carries no ``/v1``: each appends
            # ``/v1/messages`` itself.  The route the client leaves behind is
            # remembered, because the proxy serves public model names and the
            # client's own document can no longer state which route it was on.
            client = _claude_client(data)
            base_url, key = _shared_api.endpoint(self.runtime_config_path)
            # The endpoint just adopted *is* this app's proxy, whatever this
            # domain resolved at construction: a checkout with no installed
            # config resolved nothing, and both `uses_local_api` answers would
            # then contradict the document that was just written.
            self._local_api_base_url = base_url
            base_url = anthropic_base_url(base_url)
            store_route_setting(self.app_state_root, client, data)
            if client == "claudeDesktop":
                if self._desktop is None:
                    raise ClaudeSettingsError("Claude Desktop configuration is unavailable")
                try:
                    self._desktop.patch(
                        {"inferenceProvider": "gateway", "inferenceGatewayBaseUrl": base_url}
                    )
                    self._desktop.stage_secret("desktop_gateway_api_key", key)
                except ClaudeDesktopConfigError as exc:
                    raise ClaudeSettingsError(str(exc)) from None
                self._revision += 1
                return self.snapshot()
            candidate = copy.deepcopy(self._draft)
            env = _mapping(candidate.get("env", {}), "env")
            env["ANTHROPIC_BASE_URL"] = base_url
            # ``ANTHROPIC_AUTH_TOKEN`` is the bearer form a gateway states; an
            # ``ANTHROPIC_API_KEY`` left beside it is the old direct route's
            # credential and would be sent as a second identity.
            env.pop("ANTHROPIC_API_KEY", None)
            env["ANTHROPIC_AUTH_TOKEN"] = key
            # The route is named the way this app publishes it, not by its
            # public name: a gateway id has no catalogue entry in the client, so
            # a session launched with the public name reports the client's 200k
            # default and drops the effort ladder (see ``_code_published_id``).
            # Only while the picker is this app's list is there a row to name,
            # so a client on the proxy with its own catalogue is left as it is.
            named = self.code_model_list_enabled()
            model = str(data.get("model") or "").strip()
            if model:
                env["ANTHROPIC_MODEL"] = self._code_published_id(model) if named else model
                candidate["model"] = env["ANTHROPIC_MODEL"]
            elif isinstance(env.get("ANTHROPIC_MODEL"), str):
                if named:
                    env["ANTHROPIC_MODEL"] = self._code_published_id(env["ANTHROPIC_MODEL"])
                candidate["model"] = env["ANTHROPIC_MODEL"]
            candidate["env"] = env
            errors = validate_settings(candidate)
            if errors:
                raise ClaudeSettingsError(errors[0])
            self._draft = candidate
            self._revision += 1
            return self.snapshot()
        if name in {"use_saved_model", "usesavedmodel"}:
            # The counterpart of ``use_local_api``: put one Claude client back
            # on a saved route's own endpoint, key, and public model name,
            # instead of going through this app's proxy.  A client on the proxy
            # has nothing left to read -- the proxy serves public names -- so
            # the pane names the route explicitly.
            #
            # This router's generated picker lineup names models only its proxy
            # serves, so it leaves with the switch that pointed the client at
            # the proxy: Claude Code goes back to its built-in list, and Desktop
            # is left holding the route's own model.  The record of what this
            # app generated is cleared beside it, or a route whose public name
            # is also in that record would let a background refresh put the
            # whole proxy list back onto a client that no longer talks to it.
            client = _claude_client(data)
            runtime, rows = saved_routes(self.runtime_config_path)
            row = one_saved_route(rows, data)
            route_base_url = str(row.get("api_base") or "").strip()
            if not route_base_url:
                raise DomainError("The selected model has no saved provider endpoint")
            route_key = saved_route_api_key(runtime, str(row.get("deployment_id") or ""))
            route_model = str(row.get("model") or "").strip()
            store_route_setting(self.app_state_root, client, data)
            if client == "claudeDesktop":
                if self._desktop is None:
                    raise ClaudeSettingsError("Claude Desktop configuration is unavailable")
                try:
                    self._desktop.patch(
                        {
                            "inferenceProvider": "gateway",
                            "inferenceGatewayBaseUrl": anthropic_base_url(route_base_url),
                        }
                    )
                    self._desktop.stage_secret("desktop_gateway_api_key", route_key)
                    # Desktop deletes a name it cannot map onto an Anthropic
                    # model, so writing one here would only produce a warning
                    # on the next launch and an empty picker either way.
                    self._desktop.set_model_names(
                        [route_model] if desktop_accepts_model_name(route_model) else []
                    )
                except ClaudeDesktopConfigError as exc:
                    raise ClaudeSettingsError(str(exc)) from None
                store_generated_models(self.app_state_root, "claudeDesktop", [])
                self._revision += 1
                return self.snapshot()
            candidate = copy.deepcopy(self._draft)
            candidate.pop("modelPicker", None)
            env = _mapping(candidate.get("env", {}), "env")
            env["ANTHROPIC_BASE_URL"] = anthropic_base_url(route_base_url)
            # The saved route's own key is the credential this route answers,
            # and it is applied only when this app stores one: a route that
            # authenticates elsewhere must not carry a stale token forward.
            if route_key:
                env.pop("ANTHROPIC_API_KEY", None)
                env["ANTHROPIC_AUTH_TOKEN"] = route_key
            if route_model:
                env["ANTHROPIC_MODEL"] = route_model
                candidate["model"] = route_model
            candidate["env"] = env
            errors = validate_settings(candidate)
            if errors:
                raise ClaudeSettingsError(errors[0])
            self._draft = candidate
            store_generated_models(self.app_state_root, "claudeCode", [])
            self._revision += 1
            return self.snapshot()
        if name in {"desktop_models_patch", "desktopmodelspatch"}:
            # The Desktop list as an explicit value rather than a switch.  Older
            # callers only had this app's Desktop, so they named no client; the
            # value is theirs to set, so it is also not recorded as generated.
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
        if name in {"claude_models_set", "claudemodelsset", "claude_models", "claudemodels"}:
            # One switch per client, because the two apps keep different model
            # lists: Claude Code reads ``modelPicker`` from its own settings,
            # Claude Desktop reads ``inferenceModels`` from its third-party
            # profile.  Both are generated from this router's public list.
            client = _claude_client(data)
            enabled = data.get("enabled", data.get("model_list_enabled"))
            if not isinstance(enabled, bool):
                raise DomainError("The Claude model-list switch is invalid")
            if client == "claudeDesktop":
                self._desktop_model_list_apply(enabled)
                store_generated_models(
                    self.app_state_root,
                    "claudeDesktop",
                    list(self._desktop.snapshot().get("model_names") or []) if enabled else [],
                )
                self._revision += 1
                return self.snapshot()
            candidate = self._code_model_list_apply(enabled)
            errors = validate_settings(candidate)
            if errors:
                raise ClaudeSettingsError(errors[0])
            self._draft = candidate
            store_generated_models(
                self.app_state_root, "claudeCode", self.code_model_list_public_names() if enabled else []
            )
            self._revision += 1
            return self.snapshot()
        if name in {
            "claude_nonstream_fallback_set",
            "claudenonstreamfallbackset",
            "nonstream_fallback_set",
            "nonstreamfallbackset",
        }:
            enabled = data.get("enabled")
            if not isinstance(enabled, bool):
                raise DomainError("The Claude non-streaming fallback switch is invalid")
            self.set_code_nonstream_fallback(enabled)
            self._revision += 1
            return self.snapshot()
        candidate = copy.deepcopy(self._draft)
        if name in {"set", "patch", "claude_settings_set"}:
            _validate_structured_patch(data)
            normalized = _normalise_structured_patch(data)
            for key, value in normalized.items():
                if value is None:
                    candidate.pop(key, None)
                elif isinstance(value, Mapping) and isinstance(candidate.get(key), Mapping):
                    candidate[key] = _deep_merge(_mapping(candidate[key], key), value)
                else:
                    candidate[key] = copy.deepcopy(value)
        elif name in {"set_raw", "setraw"}:
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
        elif name in {"select_deployment", "selectdeployment"}:
            candidate = apply_litellm_deployment(candidate, data.get("deployment", data))
        elif name in {"patch_deployment", "patchdeployment"}:
            candidate = _patch_litellm_deployment(candidate, data.get("deployment", data))
        elif name in {"reset", "cancel", "reload"}:
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
            raise ClaudeSettingsError("Claude Settings changed on disk; reload and try again")
        if self._desktop is not None:
            try:
                if self._desktop.external_disk_state()["changed"]:
                    raise ClaudeDesktopConfigError(
                        "Claude Desktop configuration changed on disk; reload and try again"
                    )
            except ClaudeDesktopConfigError as exc:
                raise ClaudeSettingsError(str(exc)) from None
        if self._developer is not None:
            try:
                if self._developer.external_disk_state()["changed"]:
                    raise ClaudeDesktopConfigError(
                        "Claude Desktop developer settings changed on disk; reload and try again"
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
