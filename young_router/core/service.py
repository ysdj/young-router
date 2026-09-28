"""The Python Core state source shared by both native shells.

``CoreStore`` owns the lifecycle around domain adapters: drafts are staged in
the adapter, validation and confirmation happen here, and only ``apply`` is
allowed to cross a persistence boundary.  A domain adapter may wrap one of
the existing Python modules (providers, Codex, WebDAV, runtime settings) or a
new domain such as Claude/language.  It must not be duplicated in React.

The store intentionally has no UI or shell-script dependency.  It is usable
in unit tests and from the local IPC server without importing LiteLLM itself.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from .log_tabs import LOG_TABS
from .persistence import AtomicJSONStore, PersistenceError, atomic_write_bytes, atomic_write_json, read_bytes, read_json
from .protocol import MAX_EDITOR_DOCUMENT_BYTES, PROTOCOL_VERSION, ProtocolError, make_event
from .security import REDACTED, redact, safe_exception_message, safe_error_message


CORE_METADATA_VERSION = 1
STATE_FILE_NAME = "core-state.json"
PACKAGE_FORMAT = "young-router-core-package"
PACKAGE_VERSION = 1
DOMAIN_FILE_FORMAT = "young-router-domain-settings"
DOMAIN_FILE_VERSION = 1
SUBSCRIPTION_QUEUE_LIMIT = 32

_SECRET_FIELDS: dict[tuple[str, str], bool] = {
    ("providers_models", "api_key"): True,
    ("providers_models", "provider_auth_token"): True,
    ("codex", "api_key"): False,
    ("claude", "deployment_token"): False,
    ("claude", "desktop_gateway_api_key"): False,
    ("claude", "auto_memory_directory"): False,
    ("runtime", "setting"): True,
    ("webdav", "password"): False,
    ("relay_accounts", "api_key"): True,
}

_PLAINTEXT_SECRET_FIELDS = {
    ("providers_models", "api_key"),
    ("providers_models", "provider_auth_token"),
    ("codex", "api_key"),
    ("claude", "deployment_token"),
    ("claude", "desktop_gateway_api_key"),
    ("relay_accounts", "api_key"),
    ("runtime", "setting"),
}

_MULTILINE_SECRET_TARGETS = frozenset({
    "YOUNG_ROUTER_PI_WEB_ACCESS_CONFIG_JSON",
    "YOUNG_ROUTER_DSH_VISION_ROUTER_CONFIG_JSON",
})
_DSH_VISION_ROUTER_CONFIG_MAX_BYTES = 256 * 1024


def _allows_multiline_secret(domain: str, field: str, target: str | None) -> bool:
    return domain == "runtime" and field == "setting" and target in _MULTILINE_SECRET_TARGETS


def _secret_value_limit(domain: str, field: str, target: str | None) -> int:
    if domain == "runtime" and field == "setting" and target == "YOUNG_ROUTER_DSH_VISION_ROUTER_CONFIG_JSON":
        return _DSH_VISION_ROUTER_CONFIG_MAX_BYTES
    return 16_384

# Keep the Core's direct and legacy IPC callers tolerant of route-shaped
# domain names.  The unified UI emits canonical names, but older native
# clients may still send the settings route ids.
_DOMAIN_ALIASES = {
    "providers-models": "providers_models",
    "providers_models": "providers_models",
    "codex-settings": "codex",
    "codex_settings": "codex",
    "claude-settings": "claude",
    "claude_settings": "claude",
    "runtime-settings": "runtime",
    "runtime_settings": "runtime",
    "webdav-settings": "webdav",
    "webdav_settings": "webdav",
    "relay-accounts": "relay_accounts",
    "relay_accounts": "relay_accounts",
    "language": "language",
    "logs": "logs",
}

SERVICE_STATES = frozenset({"starting", "running", "unhealthy", "stopped", "unknown"})

# Import previews identify only settings that have a Core-owned adapter. Logs
# are a read-only projection and must never become a staged configuration
# section just because a package contains a similarly named key.
IMPORTABLE_DOMAINS = (
    "providers_models",
    "codex",
    "claude",
    "runtime",
    "webdav",
    "language",
    "relay_accounts",
)


def _default_domain_state(name: str) -> dict[str, Any]:
    """Return a neutral, secret-free initial state for every shared route."""

    if name == "providers_models":
        return {"providers": [], "revision": 0}
    if name == "runtime":
        return {"categories": [], "values": {}, "revision": 0}
    if name == "webdav":
        return {"enabled": False, "configured": False, "last_probe": "unknown", "revision": 0}
    if name == "logs":
        return {"tabs": list(LOG_TABS), "revision": 0}
    if name == "codex":
        return {"config_exists": False, "auth_file_exists": False, "revision": 0}
    return {"revision": 0}


class CoreError(ProtocolError):
    """A safe, stable domain/store error."""


class RevisionConflict(CoreError):
    def __init__(self, expected: int, actual: int):
        self.expected = expected
        self.actual = actual
        super().__init__("revision_conflict", "The Core state changed; reload and try again")


class DomainNotFound(CoreError):
    def __init__(self, domain: str):
        self.domain = domain
        super().__init__("domain_not_found", "The requested settings domain is unavailable")


class ConfirmationNeeded(CoreError):
    def __init__(self, codes: Iterable[str]):
        self.codes = tuple(dict.fromkeys(str(code) for code in codes if str(code)))
        super().__init__("confirmation_required", "Explicit confirmation is required for this change")


@runtime_checkable
class DomainAdapter(Protocol):
    """Minimal adapter contract implemented by Core-owned domains."""

    name: str

    def draft_state(self) -> object: ...

    def snapshot(self) -> Mapping[str, Any]: ...

    def dispatch(self, action: str, payload: object | None = None) -> Mapping[str, Any] | None: ...

    def validate(self, payload: object | None = None) -> Mapping[str, Any]: ...

    def apply(self, payload: object | None = None) -> Mapping[str, Any] | None: ...

    def reload(self) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True)
class FileCapability:
    token: str
    path: Path
    purpose: str


@dataclass(frozen=True, repr=False)
class PreparedImport:
    """Parsed package held by an authenticated IPC import-preview lease.

    The package is intentionally not part of the wire result.  ``CoreIPCServer``
    stores this object behind a session-bound opaque token until the user
    submits a subset of ``detected_sections`` for staging.
    """

    package: Mapping[str, Any] = field(repr=False)
    detected_sections: tuple[str, ...]
    preview: Mapping[str, Mapping[str, bool]]
    revision: int


class FileCapabilityRegistry:
    """Resolve native file-picker choices without exposing paths over IPC."""

    def __init__(self) -> None:
        self._items: dict[str, FileCapability] = {}
        self._lock = threading.RLock()

    def register(self, path: Path | str, purpose: str) -> str:
        target = Path(path).expanduser()
        purpose_text = str(purpose).strip()
        if purpose_text not in {"import", "export", "claude-profile"}:
            raise CoreError("invalid_file_capability", "Unsupported file capability")
        try:
            details = target.lstat()
        except FileNotFoundError:
            details = None
        except OSError:
            raise CoreError("invalid_file_capability", "The selected file is unavailable") from None
        if details is not None and (stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode)):
            # An export target may not exist yet, but it still cannot be a
            # symlink or directory.
            raise CoreError("invalid_file_capability", "The selected file is unavailable")
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._items[token] = FileCapability(token=token, path=target, purpose=purpose_text)
        return token

    def resolve(self, token: object, purpose: str, *, consume: bool = True) -> Path:
        if not isinstance(token, str) or not token or len(token.encode("utf-8")) > 256:
            raise CoreError("invalid_file_capability", "The selected file is unavailable")
        with self._lock:
            item = self._items.get(token)
            if item is None or item.purpose != purpose:
                raise CoreError("invalid_file_capability", "The selected file is unavailable")
            if consume:
                self._items.pop(token, None)
        try:
            details = item.path.lstat()
        except FileNotFoundError:
            if purpose == "export":
                return item.path
            raise CoreError("invalid_file_capability", "The selected file is unavailable") from None
        except OSError:
            raise CoreError("invalid_file_capability", "The selected file is unavailable") from None
        if stat.S_ISLNK(details.st_mode) or (purpose in {"import", "claude-profile"} and not stat.S_ISREG(details.st_mode)):
            raise CoreError("invalid_file_capability", "The selected file is unavailable")
        return item.path


def _canonical_domain(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CoreError("invalid_domain", "A settings domain is required")
    clean = value.strip()
    return _DOMAIN_ALIASES.get(clean, clean)


def _safe_public(value: object) -> object:
    """Redact secrets, paths, and raw config-like fields in a snapshot."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key).strip().lower().replace("-", "_")
            # Raw editor text and complete documents are available only through
            # an explicit trusted editor operation, never in ``snapshot``.
            if key_text in {
                "raw",
                "raw_text",
                "raw_json",
                "config_text",
                "auth_text",
                "settings_text",
                "source_text",
                "document",
                "config_document",
                "private_path",
                "file_path",
            }:
                if item in (None, "", False, [], {}):
                    result[str(key)] = item
                else:
                    result[str(key)] = REDACTED
                continue
            result[str(key)] = _safe_public(redact(item, _key=key))
        return result
    if isinstance(value, list):
        return [_safe_public(item) for item in value]
    if isinstance(value, tuple):
        return [_safe_public(item) for item in value]
    return copy.deepcopy(value)


def _relay_binding_projection(state: object) -> list[tuple[str, ...]]:
    """The relay-bound part of a providers draft, for change detection.

    Two things in a provider draft bind material that only the relay domain can
    resolve: a key that resolves through a station, and a model that points at
    such a key.  Comparing this projection against the applied baseline tells an
    Apply whether it has any relay work to do at all — a draft that only adds an
    independent model, renames a provider, or edits an order has none, and it
    must not be dragged into a relay transaction whose backlog (a pending key
    create, an expired session) would otherwise fail an edit that has nothing to
    do with the relay.
    """

    providers = state.get("providers") if isinstance(state, Mapping) else None
    projection: list[tuple[str, ...]] = []
    if not isinstance(providers, Sequence) or isinstance(providers, (str, bytes, bytearray)):
        return projection
    for provider in providers:
        if not isinstance(provider, Mapping):
            continue
        provider_name = str(provider.get("name", "")).strip()
        relay_key_names: set[str] = set()
        relay_key_ids: set[str] = set()
        api_keys = provider.get("api_keys")
        if isinstance(api_keys, Sequence) and not isinstance(api_keys, (str, bytes, bytearray)):
            for key in api_keys:
                if not isinstance(key, Mapping):
                    continue
                source = key.get("source") if isinstance(key.get("source"), Mapping) else {}
                # Only a key that resolves through a station is relay material;
                # an independent key, and the provider name it happens to live
                # under, is this domain's own business.
                if str(source.get("kind", "")) != "relay":
                    continue
                projection.append(
                    (
                        "key",
                        provider_name,
                        str(key.get("name", "")),
                        str(source.get("station_id", "")),
                        str(source.get("account_id", "")),
                        str(source.get("resource_id", "")),
                    )
                )
                relay_key_names.add(str(key.get("name", "")))
                if key.get("id"):
                    relay_key_ids.add(str(key.get("id")))
        models = provider.get("models")
        if isinstance(models, Sequence) and not isinstance(models, (str, bytes, bytearray)):
            for model in models:
                if not isinstance(model, Mapping):
                    continue
                key_name = str(model.get("api_key_name", ""))
                key_id = str(model.get("provider_key_id", ""))
                if key_name not in relay_key_names and key_id not in relay_key_ids:
                    continue
                projection.append(
                    (
                        "model",
                        provider_name,
                        str(model.get("model_name", model.get("name", ""))),
                        key_id,
                        key_name,
                    )
                )
    return projection


def _as_mapping(value: object, label: str = "payload") -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CoreError("invalid_payload", f"{label} must be an object")
    return dict(value)


def _validation_summary(value: object) -> dict[str, Any]:
    """Normalize domain validation into the shared TypeScript shape."""

    if not isinstance(value, Mapping):
        return {
            "valid": False,
            "issues": [{"path": "", "code": "invalid", "message": "Validation failed", "severity": "error"}],
        }
    valid = value.get("valid") is True
    raw_issues = value.get("issues", value.get("errors", []))
    if isinstance(raw_issues, str):
        raw_issues = [raw_issues]
    issues: list[dict[str, Any]] = []
    if isinstance(raw_issues, Sequence) and not isinstance(raw_issues, (str, bytes, bytearray)):
        for issue in raw_issues:
            if isinstance(issue, Mapping):
                message = safe_error_message(issue.get("message", "Validation failed"))
                path = _safe_issue_path(issue.get("path", ""))
                code = str(issue.get("code", "invalid"))[:64]
                severity = str(issue.get("severity", "error"))
            else:
                message = safe_error_message(issue)
                path, code, severity = "", "invalid", "error"
            if severity not in {"error", "warning"}:
                severity = "error"
            issues.append({"path": path, "code": code, "message": message, "severity": severity})
    if any(issue["severity"] == "error" for issue in issues):
        valid = False
    return {"valid": valid, "issues": issues}


def _safe_issue_path(value: object) -> str:
    """Keep validation locations useful without returning a local path."""

    text = str(value).strip()
    if not text:
        return ""
    if len(text) > 160 or text.startswith(("/", "~", "\\")) or re.match(r"^[A-Za-z]:[\\/]", text):
        return "configuration"
    if "/" in text or "\\" in text or ".." in text.split("."):
        return "configuration"
    return text if re.fullmatch(r"[A-Za-z0-9_.\[\]-]+", text) else "configuration"


def _display_path(path: str) -> str:
    """Spell one user file path with the home directory written as ``~``.

    The external-settings pane shows where each registered client file lives;
    the account's own home directory is noise there and should not be spelled
    out.  Only the spelling is shortened: the row keeps its real ``path`` for
    the operations that must address the file itself.
    """

    home = str(Path.home())
    if path == home:
        return "~"
    prefix = home + os.sep
    if not home or not path.startswith(prefix):
        return path
    return "~/" + path[len(prefix):].replace(os.sep, "/")


def _secret_presence(value: object) -> bool:
    if value in (None, "", False, [], {}, REDACTED):
        return False if value != REDACTED else True
    if isinstance(value, Mapping):
        return any(_secret_presence(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_secret_presence(item) for item in value)
    return True


def _mapping_contains_key(value: object, keys: set[str]) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key).strip()).lower().replace("-", "_")
            if text in keys:
                return True
            if _mapping_contains_key(item, keys):
                return True
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_mapping_contains_key(item, keys) for item in value)
    return False


def _relay_transient_dispatch_result(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap a relay refresh/import projection in the dispatch result envelope.

    The dispatch contract is a revision plus an optional action-scoped
    summary, while those helpers report their own projection (resource status
    and counts).  Returning that projection as the whole result adds top-level
    fields the IPC contract rejects, so it travels as the action summary and
    the caller reads the status from there.
    """

    revision = summary.get("revision")
    return {
        "revision": revision if type(revision) is int else 0,
        "action_summary": {key: value for key, value in summary.items() if key != "revision"},
    }


def _checkpoint_adapter(adapter: DomainAdapter, *, error_code: str = "import_failed") -> dict[str, Any]:
    """Capture one adapter before a transaction mutates it."""

    checkpoint = getattr(adapter, "transaction_checkpoint", None)
    restore = getattr(adapter, "restore_transaction", None)
    if callable(checkpoint) and callable(restore):
        try:
            return {"kind": "transaction", "value": copy.deepcopy(checkpoint())}
        except Exception:
            raise CoreError(error_code, "Settings could not be prepared for a transaction") from None
    state = getattr(adapter, "__dict__", None)
    if not isinstance(state, dict):
        raise CoreError(error_code, "Settings could not be prepared for a transaction")
    try:
        return {"kind": "state", "value": copy.deepcopy(state)}
    except Exception:
        raise CoreError(error_code, "Settings could not be prepared for a transaction") from None


def _restore_adapter(adapter: DomainAdapter, checkpoint: Mapping[str, Any]) -> None:
    """Restore the same adapter instance so no caller observes partial state."""

    kind = checkpoint.get("kind")
    value = checkpoint.get("value")
    if kind == "transaction":
        restore = getattr(adapter, "restore_transaction", None)
        if not callable(restore) or not isinstance(value, Mapping):
            raise RuntimeError("Configuration package adapter cannot be restored")
        restore(copy.deepcopy(value))
        return
    if kind != "state" or not isinstance(value, Mapping):
        raise RuntimeError("Configuration package adapter cannot be restored")
    state = getattr(adapter, "__dict__", None)
    if not isinstance(state, dict):
        raise RuntimeError("Configuration package adapter cannot be restored")
    state.clear()
    state.update(value)


@dataclass(frozen=True)
class _FileCheckpoint:
    path: Path
    data: bytes | None
    mode: int
    backups: frozenset[Path]


def _adapter_persistence_paths(adapter: DomainAdapter) -> tuple[Path, ...]:
    """Resolve only files explicitly owned by a domain adapter."""

    delegate = getattr(adapter, "_delegate", None)
    if delegate is not None:
        adapter = delegate
    paths: list[Path] = []
    owned_paths = getattr(adapter, "persistence_paths", None)
    if callable(owned_paths):
        for value in owned_paths():
            if isinstance(value, (str, Path)):
                paths.append(Path(value).expanduser())
    for attribute in ("config_path", "runtime_config_path", "settings_path", "preference_path", "enabled_path", "status_path", "model_catalog_path"):
        value = getattr(adapter, attribute, None)
        if isinstance(value, (str, Path)):
            paths.append(Path(value).expanduser())
    name = str(getattr(adapter, "name", ""))
    if name == "providers_models":
        config_path = getattr(adapter, "config_path", None)
        if isinstance(config_path, (str, Path)):
            target = Path(config_path).expanduser()
            paths.append(target.with_name(f"{target.stem}.disabled-models.yaml"))
    elif name == "codex":
        home_value = getattr(adapter, "codex_home", None)
        home = Path(home_value).expanduser() if isinstance(home_value, (str, Path)) else Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
        paths.extend((home / "config.toml", home / "auth.json"))
    return tuple(dict.fromkeys(paths))


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except OSError:
        return False


def _checkpoint_files(paths: Iterable[Path]) -> tuple[_FileCheckpoint, ...]:
    checkpoints: list[_FileCheckpoint] = []
    try:
        for path in dict.fromkeys(paths):
            data = read_bytes(path)
            mode = stat.S_IMODE(path.stat().st_mode) if data is not None else 0o600
            backups = frozenset(candidate for candidate in path.parent.glob(f"{path.name}.bak-*") if candidate.is_file() and not candidate.is_symlink())
            checkpoints.append(_FileCheckpoint(path=path, data=data, mode=mode, backups=backups))
    except (OSError, PersistenceError):
        raise CoreError("apply_failed", "Settings could not be prepared for an atomic apply") from None
    return tuple(checkpoints)


def _restore_files(checkpoints: Iterable[_FileCheckpoint]) -> None:
    for checkpoint in checkpoints:
        if checkpoint.data is not None:
            atomic_write_bytes(checkpoint.path, checkpoint.data, mode=checkpoint.mode)
        else:
            try:
                details = checkpoint.path.lstat()
            except FileNotFoundError:
                details = None
            if details is not None:
                if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                    raise PersistenceError("Settings rollback target is unsafe")
                checkpoint.path.unlink()
        for candidate in checkpoint.path.parent.glob(f"{checkpoint.path.name}.bak-*"):
            if candidate in checkpoint.backups:
                continue
            details = candidate.lstat()
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                raise PersistenceError("Settings rollback backup is unsafe")
            candidate.unlink()


class RecoverableDomain:
    """Keep a malformed route registered and retry its real factory on Reload."""

    def __init__(self, name: str, factory: Callable[[], DomainAdapter]):
        self.name = _canonical_domain(name)
        self._factory = factory
        self._delegate: DomainAdapter | None = None
        self._recovery_candidate: DomainAdapter | None = None
        self.revision = 0

    def snapshot(self) -> Mapping[str, Any]:
        if self._delegate is not None:
            return self._delegate.snapshot()
        neutral = _default_domain_state(self.name)
        if self.name == "claude":
            neutral["settings"] = {}
        if self.name == "language":
            neutral["choice"] = "system"
        return {"domain": self.name, "available": False, "error_code": "settings_unavailable", **neutral}

    def draft_state(self) -> object:
        if self._delegate is not None:
            return self._delegate.draft_state()
        return {"available": False}

    def _require_delegate(self) -> DomainAdapter:
        if self._delegate is None:
            raise CoreError("domain_unavailable", "Settings could not be loaded")
        return self._delegate

    def dispatch(self, action: str, payload: object | None = None) -> Mapping[str, Any] | None:
        if action == "reload":
            return self.reload()
        return self._require_delegate().dispatch(action, payload)

    def validate(self, payload: object | None = None) -> Mapping[str, Any]:
        if self._delegate is None:
            return {"valid": False, "errors": ["Settings could not be loaded"]}
        return self._delegate.validate(payload)

    def apply(self, payload: object | None = None) -> Mapping[str, Any] | None:
        return self._require_delegate().apply(payload)

    def reload(self) -> Mapping[str, Any] | None:
        if self._delegate is not None:
            return self._delegate.reload()
        try:
            delegate = self._recovery_candidate or self._factory()
        except Exception:
            raise CoreError("domain_unavailable", "Settings could not be loaded") from None
        self._recovery_candidate = None
        self._delegate = delegate
        self.revision += 1
        return delegate.snapshot()

    def external_disk_state(self) -> Mapping[str, bool]:
        """Probe a failed source so a valid external repair recovers itself."""

        if self._delegate is not None:
            detector = getattr(self._delegate, "external_disk_state", None)
            return detector() if callable(detector) else {"changed": False, "exists": True}
        if self._recovery_candidate is None:
            try:
                self._recovery_candidate = self._factory()
            except Exception:
                return {"changed": False, "exists": True}
        return {"changed": True, "exists": True}

    def external_disk_identity(self) -> str:
        if self._delegate is not None:
            getter = getattr(self._delegate, "external_disk_identity", None)
            if callable(getter):
                return str(getter())
            return "recoverable-loaded"
        if self._recovery_candidate is not None:
            getter = getattr(self._recovery_candidate, "external_disk_identity", None)
            if callable(getter):
                return str(getter())
            return "recoverable-ready"
        return "recoverable-unavailable"

    def __getattr__(self, name: str) -> Any:
        """Expose trusted delegate capabilities after automatic recovery."""

        if name.startswith("_") or self._delegate is None:
            raise AttributeError(name)
        return getattr(self._delegate, name)


class MemoryDomain:
    """A tiny adapter useful for tests and for generic Core-owned metadata.

    Production domains should wrap their existing Python modules; this class
    exists so the transport/store can be tested without creating user files.
    """

    def __init__(self, name: str, initial: Mapping[str, Any] | None = None):
        self.name = _canonical_domain(name)
        self._state = copy.deepcopy(dict(initial or {}))
        self._draft = copy.deepcopy(self._state)
        self._raw = copy.deepcopy(self._state)
        self.revision = 0

    def snapshot(self) -> Mapping[str, Any]:
        return {"domain": self.name, "revision": self.revision, "state": copy.deepcopy(self._draft)}

    def draft_state(self) -> object:
        return copy.deepcopy(self._draft)

    def dispatch(self, action: str, payload: object | None = None) -> Mapping[str, Any]:
        data = _as_mapping(payload)
        if action in {"set", "patch"}:
            self._draft.update(copy.deepcopy(data))
        elif action in {"replace", "set_raw"}:
            self._draft = copy.deepcopy(data.get("state", data))
        elif action in {"reset", "cancel", "reload"}:
            self._draft = copy.deepcopy(self._raw)
        else:
            raise CoreError("unknown_action", "The requested settings action is unavailable")
        self.revision += 1
        return self.snapshot()

    def validate(self, payload: object | None = None) -> Mapping[str, Any]:
        candidate = self._draft if payload is None else payload
        return {"valid": isinstance(candidate, Mapping), "errors": [] if isinstance(candidate, Mapping) else ["Settings must be an object"]}

    def apply(self, payload: object | None = None) -> Mapping[str, Any]:
        self._raw = copy.deepcopy(self._draft)
        self._state = copy.deepcopy(self._raw)
        self.revision += 1
        return {"applied": True, **self.snapshot()}

    def reload(self) -> Mapping[str, Any]:
        self._draft = copy.deepcopy(self._raw)
        self.revision += 1
        return self.snapshot()

    def export(self) -> Mapping[str, Any]:
        return {"domain": self.name, "state": copy.deepcopy(self._draft)}


class UnavailableDomain:
    """Fail closed when a production settings source cannot be parsed."""

    def __init__(self, name: str):
        self.name = _canonical_domain(name)

    def snapshot(self) -> Mapping[str, Any]:
        return {
            "domain": self.name,
            "revision": 0,
            "available": False,
            "error": "Settings could not be loaded",
        }

    def draft_state(self) -> object:
        return {"available": False}

    def dispatch(self, action: str, payload: object | None = None) -> Mapping[str, Any]:
        raise CoreError("domain_unavailable", "Settings could not be loaded")

    def validate(self, payload: object | None = None) -> Mapping[str, Any]:
        return {"valid": False, "errors": ["Settings could not be loaded"]}

    def apply(self, payload: object | None = None) -> Mapping[str, Any]:
        raise CoreError("domain_unavailable", "Settings could not be loaded")

    def reload(self) -> Mapping[str, Any]:
        raise CoreError("domain_unavailable", "Settings could not be loaded")


class CoreStore:
    """One thread-safe, versioned state source for native shells."""

    # A quit must not wait for a full proxy restart: the worker stops whatever
    # it started as soon as it observes the stopping flag.
    _SERVICE_RELOAD_SHUTDOWN_JOIN_SECONDS = 1.0

    def __init__(
        self,
        *,
        metadata_path: Path | str | None = None,
        domains: Iterable[DomainAdapter] = (),
        file_capabilities: FileCapabilityRegistry | None = None,
        service_handlers: Mapping[str, Callable[[str], object]] | None = None,
        workbuddy_runtime: object | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._metadata_store = AtomicJSONStore(metadata_path) if metadata_path else None
        self.file_capabilities = file_capabilities or FileCapabilityRegistry()
        self._service_handlers = dict(service_handlers or {})
        self._shutdown_handler = self._service_handlers.get("stop")
        # The WorkBuddy worker is Core-owned: it must not outlive the process
        # that keeps its credential copy and its loopback bearer.
        self._workbuddy_runtime = workbuddy_runtime
        self._domains: dict[str, DomainAdapter] = {}
        self._baselines: dict[str, object] = {}
        self._drafts: dict[str, dict[str, Any]] = {}
        self._subscribers: dict[str, Callable[[dict[str, Any]], None]] = {}
        self._revision = 0
        self._service: dict[str, Any] = {"state": "unknown"}
        # Post-apply proxy restarts take several seconds; they run on their own
        # thread so the committed edit and every later IPC request stay fast.
        self._service_reload_guard = threading.Lock()
        self._service_reload_thread: threading.Thread | None = None
        self._service_reload_pending = False
        self._service_reload_stopping = False
        # Manual lifecycle operations and the background post-apply restart
        # replace the same proxy, so they take turns on one transition guard.
        self._service_transition_guard = threading.Lock()
        self._last_actions: dict[str, dict[str, Any]] = {}
        # The WebDAV interval loop is created on request, started by the Core
        # entry point, and stopped with the store.
        self._webdav_scheduler: Any | None = None
        # When a user action last reached this store.  An automatic WebDAV sync
        # holds the store for its whole round trip, so it waits for quiet.
        self._last_activity = time.monotonic()
        self._disk: dict[str, dict[str, Any]] = {}
        self._disk_identities: dict[str, str | None] = {}
        self._logs: dict[str, dict[str, Any]] = {
            tab: {
                "tab": tab,
                "available": False,
                "paused": False,
                "line_count": 0,
                "filter": "",
                "limit": 0,
            }
            for tab in LOG_TABS
        }
        if self._metadata_store is not None:
            self._load_metadata()
        for adapter in domains:
            self.register_domain(adapter)

    @classmethod
    def with_default_domains(
        cls,
        *,
        metadata_path: Path | str | None = None,
        config_path: Path | str | None = None,
        codex_home: Path | str | None = None,
        runtime_settings_path: Path | str | None = None,
        webdav_settings_path: Path | str | None = None,
        webdav_enabled_path: Path | str | None = None,
        claude_settings_path: Path | str | None = None,
        language_path: Path | str | None = None,
        runtime_root: Path | str | None = None,
        reset_transient_routing_state: bool = False,
    ) -> "CoreStore":
        """Construct production domains without importing the LiteLLM proxy.

        Existing Python modules remain the only parser and persistence layer
        for providers, Codex, runtime settings, and WebDAV.  A malformed
        optional user file degrades only that route during bootstrap so the
        host can still open the remaining settings windows.
        """

        from .domains._shared import _default_runtime_root, _default_runtime_settings_path
        from .domains.claude import ClaudeSettingsDomain
        from .domains.clients import ClientSettingsDomain
        from .domains.codex import CodexSettingsDomain
        from .domains.language import LanguageSettingsDomain
        from .domains.logs import LogsDomain
        from .domains.providers_models import ProvidersModelsDomain
        from .domains.relay_accounts import RelayAccountsDomain
        from .domains.runtime import RuntimeSettingsDomain
        from .domains.webdav import WebDAVSettingsDomain
        from .operations import CoreServiceController
        from .provider_auth import ProviderAuthManager
        from ..workbuddy import WorkBuddyRuntime

        controller = CoreServiceController(runtime_root)
        provider_auth = ProviderAuthManager(runtime_root)
        if reset_transient_routing_state:
            controller.reset_transient_routing_state()

        resolved_runtime_settings_path = (
            Path(runtime_settings_path).expanduser()
            if runtime_settings_path is not None
            else _default_runtime_settings_path()
        )
        resolved_runtime_root = (
            Path(runtime_root).expanduser() if runtime_root is not None else _default_runtime_root()
        )
        # One worker per Core process serves both WorkBuddy variants; the
        # service controller and the provider domain must share that instance
        # so the proxy child receives the very base URLs it is serving.
        workbuddy_runtime = WorkBuddyRuntime(resolved_runtime_root)
        controller.attach_workbuddy(workbuddy_runtime)
        adapters: list[DomainAdapter] = []
        settings_factories: tuple[tuple[str, Callable[[], DomainAdapter]], ...] = (
            (
                "providers_models",
                lambda: ProvidersModelsDomain(
                    config_path,
                    auth_manager=provider_auth,
                    workbuddy=workbuddy_runtime,
                ),
            ),
            (
                "codex",
                lambda: CodexSettingsDomain(
                    config_path,
                    codex_home=codex_home,
                    runtime_settings_path=resolved_runtime_settings_path,
                    runtime_root=resolved_runtime_root,
                ),
            ),
            ("runtime", lambda: RuntimeSettingsDomain(resolved_runtime_settings_path)),
            (
                "webdav",
                lambda: WebDAVSettingsDomain(
                    webdav_settings_path,
                    enabled_path=webdav_enabled_path,
                    config_path=config_path,
                ),
            ),
        )
        for name, factory in settings_factories:
            try:
                adapters.append(factory())
            except Exception:
                # Never claim Apply succeeded against a placeholder when the
                # user's real source is malformed.
                adapters.append(UnavailableDomain(name))
        claude_factory: Callable[[], DomainAdapter] = lambda: ClaudeSettingsDomain(claude_settings_path)
        try:
            adapters.append(claude_factory())
        except Exception:
            adapters.append(RecoverableDomain("claude", claude_factory))
        clients_factory: Callable[[], DomainAdapter] = lambda: ClientSettingsDomain()
        try:
            adapters.append(clients_factory())
        except Exception:
            adapters.append(RecoverableDomain("clients", clients_factory))
        adapters.append(
            LogsDomain(
                runtime_root,
                config_path=config_path,
                runtime_settings_path=runtime_settings_path,
            )
        )
        language_factory: Callable[[], DomainAdapter] = lambda: LanguageSettingsDomain(language_path)
        try:
            adapters.append(language_factory())
        except Exception:
            adapters.append(RecoverableDomain("language", language_factory))
        try:
            relay_path = None
            if config_path is not None:
                from webdav import core as webdav_core

                relay_path = webdav_core.relay_accounts_path(Path(config_path).expanduser())
            adapters.append(RelayAccountsDomain(runtime_root, storage_path=relay_path))
        except Exception:
            adapters.append(UnavailableDomain("relay_accounts"))
        operations = (
            "start",
            "stop",
            "restart",
            "reload",
            "health",
            "status",
            "autostart_enable",
            "autostart_disable",
            "autostart_status",
        )
        service_handlers = {operation: controller.dispatch for operation in operations}
        initial_service = controller.status()
        store = cls(
            metadata_path=metadata_path,
            domains=adapters,
            service_handlers=service_handlers,
            workbuddy_runtime=workbuddy_runtime,
        )
        if isinstance(initial_service, Mapping):
            store._set_service_from_result(initial_service, increment=False)
        return store

    def shutdown(self) -> dict[str, Any]:
        """Stop only the proxy owned by this Core before the native host exits."""

        self._stop_webdav_scheduler()
        with self._service_reload_guard:
            self._service_reload_stopping = True
            reload_thread = self._service_reload_thread
        if reload_thread is not None and reload_thread is not threading.current_thread():
            # A restart may already be replacing the proxy. Give it a bounded
            # moment to settle; the worker stops whatever it started once it
            # observes the stopping flag, so no proxy outlives this Core.
            reload_thread.join(timeout=self._SERVICE_RELOAD_SHUTDOWN_JOIN_SECONDS)
        with self._lock:
            handler = self._shutdown_handler
            if handler is None:
                self._service = {"state": "stopped"}
                self._stop_workbuddy_runtime()
                return dict(self._service)
            try:
                result = handler("stop")
            except Exception as exc:
                raise CoreError("service_error", safe_exception_message(exc)) from None
            if not isinstance(result, Mapping):
                raise CoreError("service_error", "LiteLLM service returned invalid status")
            self._set_service_from_result(result, increment=False)
            self._persist_metadata()
            self._stop_workbuddy_runtime()
            return dict(self._service)

    def _stop_workbuddy_runtime(self) -> None:
        """Stop the Core-owned WorkBuddy worker; never fail Core shutdown."""

        runtime = self._workbuddy_runtime
        if runtime is None:
            return
        try:
            runtime.stop()
        except Exception:
            pass

    def _load_metadata(self) -> None:
        assert self._metadata_store is not None
        try:
            payload = self._metadata_store.read(default={})
        except PersistenceError:
            # The host passes this private file so the shared revision and
            # service projection survive a host-driven Core replacement. It is
            # optional local state: an unreadable or malformed file must never
            # prevent Core from starting, because the app would then have no
            # working Core at all. Starting fresh only risks one missed
            # revision comparison, never a wrong mutation.
            return
        if not payload:
            return
        if payload.get("version") != CORE_METADATA_VERSION:
            return
        revision = payload.get("revision", 0)
        if type(revision) is not int or revision < 0:
            return
        self._revision = revision
        service = payload.get("service")
        if isinstance(service, Mapping) and service.get("state") in SERVICE_STATES:
            self._service = _safe_public(service)

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    @property
    def domains(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._domains)

    def register_domain(self, adapter: DomainAdapter) -> str:
        name_value = getattr(adapter, "name", None)
        name = _canonical_domain(name_value)
        if (
            not callable(getattr(adapter, "draft_state", None))
            or not callable(getattr(adapter, "snapshot", None))
            or not callable(getattr(adapter, "dispatch", None))
        ):
            raise CoreError("invalid_domain", "The settings domain is unavailable")
        with self._lock:
            self._domains[name] = adapter
            current = self._adapter_draft_state(name)
            self._baselines[name] = copy.deepcopy(current)
            self._drafts[name] = {
                "dirty": False,
                "base_revision": self._revision,
                "validation": {"valid": True, "issues": []},
            }
            self._disk[name] = {"changed": False, "generation": 0, "keep_draft": False}
            self._disk_identities[name] = self._external_disk_identity(adapter)
        return name

    def unregister_domain(self, domain: str) -> None:
        name = _canonical_domain(domain)
        with self._lock:
            self._domains.pop(name, None)
            self._baselines.pop(name, None)
            self._drafts.pop(name, None)
            self._disk.pop(name, None)
            self._disk_identities.pop(name, None)

    def _adapter_snapshot(self, name: str) -> object:
        adapter = self._domains.get(name)
        if adapter is None:
            raise DomainNotFound(name)
        try:
            value = adapter.snapshot()
        except CoreError:
            raise
        except Exception as exc:
            raise CoreError("domain_unavailable", safe_exception_message(exc)) from None
        if not isinstance(value, Mapping):
            raise CoreError("domain_unavailable", "The settings domain returned invalid state")
        return _safe_public(value)

    def _adapter_draft_state(self, name: str) -> object:
        adapter = self._domains.get(name)
        if adapter is None:
            raise DomainNotFound(name)
        try:
            return copy.deepcopy(adapter.draft_state())
        except CoreError:
            raise
        except Exception as exc:
            raise CoreError("domain_unavailable", safe_exception_message(exc)) from None

    def _record_relay_transient_update(
        self,
        *,
        was_dirty: bool,
        prior_baseline: object,
        prior_base_revision: object,
    ) -> None:
        """Keep an existing relay draft dirty across login/refresh reads.

        Login restoration and resource discovery update private session/cache
        state, but are not an implicit Apply of staged station, account, or
        remote-key CRUD. A clean relay state can adopt that update as its new
        baseline; a dirty one must retain the baseline from before the read.
        """

        if was_dirty:
            self._baselines["relay_accounts"] = copy.deepcopy(prior_baseline)
            base_revision = prior_base_revision if type(prior_base_revision) is int else self._revision
            self._mark_domain(
                "relay_accounts",
                dirty=True,
                base_revision=base_revision,
            )
            return
        self._baselines["relay_accounts"] = self._adapter_draft_state("relay_accounts")
        self._mark_domain(
            "relay_accounts",
            dirty=False,
            validation={"valid": True, "issues": []},
            base_revision=self._revision,
        )

    def _persist_metadata(self) -> None:
        if self._metadata_store is None:
            return
        try:
            self._metadata_store.write(
                {
                    "version": CORE_METADATA_VERSION,
                    "revision": self._revision,
                    "service": _safe_public(self._service),
                }
            )
        except PersistenceError as exc:
            raise CoreError("state_unavailable", safe_exception_message(exc)) from None

    def _emit(self) -> None:
        snapshot = self.snapshot()
        event = make_event("snapshot", snapshot["revision"], snapshot)
        callbacks = tuple(self._subscribers.values())
        for callback in callbacks:
            try:
                callback(copy.deepcopy(event))
            except Exception:
                # A disconnected UI must not break a state transition.
                continue

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        if not callable(callback):
            raise CoreError("invalid_subscription", "A subscription callback is required")
        token = uuid.uuid4().hex
        with self._lock:
            self._subscribers[token] = callback

        def unsubscribe() -> None:
            with self._lock:
                self._subscribers.pop(token, None)

        return unsubscribe

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            # The managed proxy can outlive a replaced Core briefly.  Project
            # the controller's current ownership/health result into every
            # snapshot so a persisted transitional state cannot leave the
            # native menu displaying "Starting" after the service is ready.
            # This read-only projection deliberately neither persists nor
            # emits: snapshot callers need the live status without creating a
            # synthetic Core state transition.
            reload_thread = self._service_reload_thread
            planned_restart = reload_thread is not None and reload_thread.is_alive()
            if planned_restart and self._service.get("state") == "starting":
                # A background post-apply restart owns the transitional state:
                # the controller reports stopped/unhealthy while its proxy is
                # replaced, and a cached status must not hide the planned
                # restart from the menu or the settings panes.
                pass
            else:
                status_handler = self._service_handlers.get("status")
                if status_handler is not None:
                    result = status_handler("status")
                    if not isinstance(result, Mapping):
                        raise CoreError("service_error", "LiteLLM service returned invalid status")
                    self._set_service_from_result(result, increment=False)
            self._refresh_external_disk_state()
            # Relay snapshots display reverse dependency counts, while the
            # provider/model domain remains the sole source of binding truth.
            # Project only stable IDs and counts across this in-process seam;
            # no API base or credential is copied into the relay snapshot.
            relay = self._domains.get("relay_accounts")
            providers_domain = self._domains.get("providers_models")
            dependency_summary = getattr(providers_domain, "dependency_summary", None)
            set_binding_summary = getattr(relay, "set_binding_summary", None)
            if callable(dependency_summary) and callable(set_binding_summary):
                try:
                    summary = dependency_summary()
                    provider_keys = summary.get("provider_keys", []) if isinstance(summary, Mapping) else []
                    resources: list[dict[str, Any]] = []
                    if isinstance(provider_keys, Sequence) and not isinstance(provider_keys, (str, bytes, bytearray)):
                        for item in provider_keys:
                            if not isinstance(item, Mapping):
                                continue
                            source = item.get("source")
                            if not isinstance(source, Mapping) or source.get("kind") != "relay":
                                continue
                            count = item.get("model_count", 0)
                            if type(count) is not int or count < 0:
                                continue
                            resources.append(
                                {
                                    "station_id": source.get("station_id"),
                                    "account_id": source.get("account_id"),
                                    "resource_id": source.get("resource_id"),
                                    "linked_model_count": count,
                                    "binding_status": "linked" if count else "independent",
                                }
                            )
                    set_binding_summary({"resources": resources})
                except Exception:
                    # A reverse-count projection is informational. Domain
                    # validation during Apply still enforces binding health.
                    pass
            domain_states = {name: self._adapter_snapshot(name) for name in self._domains}
            providers = self._providers_summary(domain_states)
            webdav = self._webdav_summary(domain_states)
            logs = self._logs_summary(domain_states)
            language = self._language_summary(domain_states)
            return {
                "protocol_version": PROTOCOL_VERSION,
                "revision": self._revision,
                "service": copy.deepcopy(_safe_public(self._service)),
                "providers_models": providers,
                "drafts": copy.deepcopy(self._drafts),
                "disk": copy.deepcopy(self._disk),
                "webdav": webdav,
                "logs": logs,
                "language": language,
                "action_summaries": copy.deepcopy(self._last_actions),
                # The typed RN snapshot consumes the named fields above.  The
                # namespaced map keeps the contract extensible for Codex,
                # Claude, runtime, and future domains without another reducer.
                "domains": copy.deepcopy(domain_states),
            }

    def disk_state(self, domains: Sequence[str]) -> dict[str, Any]:
        """Refresh only the external-file markers needed by the settings UI."""

        if isinstance(domains, (str, bytes, bytearray)) or not isinstance(domains, Sequence):
            raise CoreError("invalid_domain", "The disk-state domains are invalid")
        try:
            requested = tuple(dict.fromkeys(_canonical_domain(domain) for domain in domains))
        except CoreError:
            raise
        if not requested:
            raise CoreError("invalid_domain", "At least one disk-state domain is required")
        with self._lock:
            for name in requested:
                if name not in self._domains:
                    raise DomainNotFound(name)
            self._refresh_external_disk_state(requested)
            return {
                "revision": self._revision,
                "disk": {name: copy.deepcopy(self._disk[name]) for name in requested},
            }

    def log_view(self, tab: str, known_revision: int | None = None) -> dict[str, Any]:
        """Load one requested log tab without inflating global snapshots."""

        with self._lock:
            adapter = self._domains.get("logs")
            view = getattr(adapter, "view", None) if adapter is not None else None
            if not callable(view):
                raise CoreError("logs_unavailable", "Logs are unavailable")
            try:
                result = view(tab, known_revision)
            except CoreError:
                raise
            except Exception as exc:
                raise CoreError("logs_unavailable", safe_exception_message(exc)) from None
            safe_result = _safe_public(result)
            if not isinstance(safe_result, Mapping):
                raise CoreError("logs_unavailable", "Logs are unavailable")
            return dict(safe_result)

    def _refresh_external_disk_state(self, domains: Iterable[str] | None = None) -> None:
        """Auto-reload clean drafts and expose only sanitized conflict state."""

        selected = None if domains is None else set(domains)
        for name, adapter in self._domains.items():
            if selected is not None and name not in selected:
                continue
            detector = getattr(adapter, "external_disk_state", None)
            if not callable(detector):
                continue
            record = self._disk.setdefault(name, {"changed": False, "generation": 0, "keep_draft": False})
            try:
                state = detector()
            except Exception:
                continue
            changed = bool(state.get("changed")) if isinstance(state, Mapping) else False
            identity = self._external_disk_identity(adapter)
            identity_changed = identity is not None and identity != self._disk_identities.get(name)
            if changed and (not record.get("changed") or identity_changed):
                record["generation"] = int(record.get("generation", 0)) + 1
                record["keep_draft"] = False
            if identity is not None:
                self._disk_identities[name] = identity
            if changed and not self._drafts.get(name, {}).get("dirty"):
                try:
                    adapter.reload()
                    self._baselines[name] = self._adapter_draft_state(name)
                    self._mark_domain(name, dirty=False, validation={"valid": True, "issues": []}, base_revision=self._revision + 1)
                    self._revision += 1
                    changed = False
                    # This reload advances the shared revision without an event,
                    # so windows learn it from their own responses. Record it for
                    # the same reason every other bump does: a replacement Core
                    # must resume at or above every revision a window has seen.
                    try:
                        self._persist_metadata()
                    except PersistenceError:
                        # The reload itself succeeded; only the ordering record is
                        # optional. Never fail a disk probe over it.
                        pass
                except Exception:
                    pass
            record["changed"] = changed
            if not changed:
                record["keep_draft"] = False

    @staticmethod
    def _external_disk_identity(adapter: DomainAdapter) -> str | None:
        """Read a bounded opaque identity that is never included in snapshots."""

        getter = getattr(adapter, "external_disk_identity", None)
        if not callable(getter):
            return None
        try:
            identity = getter()
        except Exception:
            return None
        if not isinstance(identity, str) or not identity or len(identity) > 256:
            return None
        return identity

    def _editor_adapter(self, domain: str, document: str) -> tuple[str, DomainAdapter]:
        if not isinstance(document, str) or not document:
            raise CoreError("invalid_editor", "The requested editor is unavailable")
        name = _canonical_domain(domain)
        adapter = self._domains.get(name)
        if name == "codex":
            if document not in {"config", "auth"}:
                raise CoreError("invalid_editor", "The requested editor is unavailable")
        elif name == "claude":
            if document not in {"settings", "desktop", "developer"}:
                raise CoreError("invalid_editor", "The requested editor is unavailable")
        elif name == "clients":
            # Every external-client file is a registered document; the UI can
            # never name a path, only one of these Core-owned ids.
            documents = getattr(adapter, "documents", ())
            if document not in documents:
                raise CoreError("invalid_editor", "The requested editor is unavailable")
        else:
            raise CoreError("invalid_editor", "The requested editor is unavailable")
        if adapter is None:
            raise DomainNotFound(name)
        return name, adapter

    def client_files(self) -> dict[str, Any]:
        """List the external client configuration files the UI may edit.

        This read-only operation is the single Core surface that returns a
        local path, and it only ever names the registered files owned by the
        Codex, Claude, and external-client adapters.  File contents never
        travel here; they stay behind the authenticated editor lease.  Every
        row carries the file's real ``path`` for the pane's own actions next to
        the home-shortened ``display_path`` it shows.
        """

        with self._lock:
            files: list[dict[str, Any]] = []
            for name in ("codex", "claude", "clients"):
                adapter = self._domains.get(name)
                provider = getattr(adapter, "client_files", None)
                if not callable(provider):
                    continue
                try:
                    rows = provider()
                except Exception:
                    # One unavailable client must not hide the files of the
                    # others; the pane simply lists what Core can manage.
                    continue
                if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
                    continue
                for row in rows:
                    if not isinstance(row, Mapping):
                        continue
                    entry = dict(row)
                    path = entry.get("path")
                    if isinstance(path, str) and path:
                        entry["display_path"] = _display_path(path)
                    files.append(entry)
            return {"revision": self._revision, "files": files}

    def editor_document(self, domain: str, document: str) -> dict[str, Any]:
        """Read one versioned editor document as a single Core operation.

        The editor lease is derived from both the document text and its Core
        revision.  Reading those separately leaves a small race where an
        unrelated state update can advance the revision after the descriptor
        is created but before its text is read, producing a false conflict.
        Keep them under the same lock so a newly opened editor always receives
        a self-consistent capability baseline.
        """

        with self._lock:
            name, _adapter = self._editor_adapter(domain, document)
            return {
                "domain": name,
                "document": document,
                "revision": self._revision,
                "text": self._trusted_editor_text_unlocked(domain, document),
                "baseline": self._trusted_editor_baseline_text_unlocked(domain, document),
            }

    def _trusted_editor_text_unlocked(self, domain: str, document: str) -> str:
        """Read one editor document while the caller holds ``self._lock``."""

        name, adapter = self._editor_adapter(domain, document)
        try:
            if name == "codex":
                exporter = getattr(adapter, "export", None)
                if not callable(exporter):
                    raise CoreError("invalid_editor", "The requested editor is unavailable")
                raw = exporter(include_sensitive=True)
                key = "config_text" if document == "config" else "auth_text"
                text = raw.get(key) if isinstance(raw, Mapping) else None
            elif name == "clients":
                reader = getattr(adapter, "raw_text", None)
                text = reader(document) if callable(reader) else None
            else:
                raw_text = getattr(adapter, "raw_text", None)
                text = raw_text(include_sensitive=True, document=document) if callable(raw_text) else None
        except CoreError:
            raise
        except Exception as exc:
            raise CoreError("editor_unavailable", safe_exception_message(exc)) from None
        if not isinstance(text, str):
            raise CoreError("editor_unavailable", "The requested editor is unavailable")
        if len(text.encode("utf-8")) > MAX_EDITOR_DOCUMENT_BYTES:
            raise CoreError("editor_too_large", "The editor document exceeds the size limit")
        return text

    def _trusted_editor_baseline_text_unlocked(self, domain: str, document: str) -> str:
        """Render one editor document from Core's last applied draft state.

        Raw editors are allowed to read sensitive text only through this
        authenticated path.  The normal document text is the staged draft;
        the matching baseline must instead remain the last applied/reloaded
        state so structured UI edits are included in the diff.
        """

        name, _adapter = self._editor_adapter(domain, document)
        state = self._baselines.get(name)
        try:
            if name == "codex":
                if not isinstance(state, Mapping):
                    raise ValueError
                key = "config_text" if document == "config" else "auth_text"
                text = state.get(key)
            elif name == "clients":
                reader = getattr(_adapter, "baseline_text", None)
                text = reader(document) if callable(reader) else None
            else:
                if not isinstance(state, Mapping):
                    raise ValueError
                if document == "settings":
                    # A standalone Claude Code domain stores its draft object
                    # directly. When a Claude Desktop source is enabled,
                    # ``draft_state`` wraps it under ``settings``.
                    value = state.get("settings") if "desktop" in state or "developer" in state else state
                elif document == "desktop":
                    desktop = state.get("desktop")
                    value = desktop.get("config") if isinstance(desktop, Mapping) else None
                else:
                    value = state.get("developer")
                if not isinstance(value, Mapping):
                    raise ValueError
                text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        except (TypeError, ValueError, UnicodeError):
            raise CoreError("editor_unavailable", "The requested editor is unavailable") from None
        if not isinstance(text, str):
            raise CoreError("editor_unavailable", "The requested editor is unavailable")
        if len(text.encode("utf-8")) > MAX_EDITOR_DOCUMENT_BYTES:
            raise CoreError("editor_too_large", "The editor document exceeds the size limit")
        return text

    @staticmethod
    def _editor_text_digest(text: str) -> str:
        """Return an opaque editor-content identity for capability rebasing."""

        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def trusted_editor_text(self, domain: str, document: str, *, revision: int) -> str:
        """Read raw text for the authenticated, versioned editor path."""

        with self._lock:
            self._check_revision(revision)
            return self._trusted_editor_text_unlocked(domain, document)

    def stage_editor_text(
        self,
        domain: str,
        document: str,
        text: str,
        *,
        revision: int,
        expected_text_digest: str | None = None,
    ) -> dict[str, Any]:
        """Stage one versioned code-editor document."""

        if not isinstance(text, str):
            raise CoreError("invalid_editor", "The editor document is invalid")
        if len(text.encode("utf-8")) > MAX_EDITOR_DOCUMENT_BYTES:
            raise CoreError("editor_too_large", "The editor document exceeds the size limit")
        with self._lock:
            name, _adapter = self._editor_adapter(domain, document)
            # Global Core revisions also advance for unrelated operations such
            # as status/menu telemetry. A raw editor must not turn one of
            # those into a false "changed outside this window" conflict. When
            # the document the capability originally read is still identical,
            # safely rebase only that trusted capability onto the current Core
            # revision. A real change to this document continues to reject.
            self._refresh_external_disk_state()
            if expected_text_digest is not None and revision != self._revision:
                if self._editor_text_digest(self._trusted_editor_text_unlocked(domain, document)) != expected_text_digest:
                    raise RevisionConflict(revision, self._revision)
                revision = self._revision
            self._check_revision(revision)
            payload: dict[str, Any]
            if name == "codex" or name == "clients":
                payload = {"document": document, "text": text}
            else:
                payload = {"document": document, "raw_json": text}
            result = self.dispatch(
                {"domain": name, "type": "set_raw", "payload": payload},
                expected_revision=revision,
                _trusted_native_capability=True,
            )
            return {
                **result,
                "baseline": self._trusted_editor_baseline_text_unlocked(domain, document),
            }

    def secret_descriptor(self, domain: str, field: str, target: object | None = None) -> dict[str, Any]:
        """Validate one native-only secret slot without returning its value."""

        with self._lock:
            name = _canonical_domain(domain)
            field_name = field.strip() if isinstance(field, str) else ""
            target_required = _SECRET_FIELDS.get((name, field_name))
            if target_required is None:
                raise CoreError("invalid_secret", "The requested secret field is unavailable")
            if target_required:
                if not isinstance(target, str) or not target or len(target.encode("utf-8")) > 256:
                    raise CoreError("invalid_secret", "The requested secret field is unavailable")
                target_value: str | None = target
            else:
                if target not in (None, ""):
                    raise CoreError("invalid_secret", "The requested secret field is unavailable")
                target_value = None
            adapter = self._domains.get(name)
            presence = getattr(adapter, "secret_present", None) if adapter is not None else None
            staging = getattr(adapter, "stage_secret", None) if adapter is not None else None
            if not callable(presence) or not callable(staging):
                raise CoreError("invalid_secret", "The requested secret field is unavailable")
            try:
                present = bool(presence(field_name, target_value))
            except Exception:
                raise CoreError("invalid_secret", "The requested secret field is unavailable") from None
            return {
                "domain": name,
                "field": field_name,
                "target": target_value,
                "revision": self._revision,
                "present": present,
            }

    def trusted_secret_descriptor(self, domain: str, field: str, target: object | None = None) -> dict[str, Any]:
        """Validate one explicitly plaintext-native credential field.

        These values are read only through a short-lived native capability.
        They are never included in snapshots or the React request protocol.
        """

        with self._lock:
            name = _canonical_domain(domain)
            field_name = field.strip() if isinstance(field, str) else ""
            descriptor_key = (name, field_name)
            if descriptor_key not in _PLAINTEXT_SECRET_FIELDS:
                raise CoreError("invalid_secret", "The requested secret field is unavailable")
            if descriptor_key == ("runtime", "setting") and target not in _MULTILINE_SECRET_TARGETS:
                raise CoreError("invalid_secret", "The requested secret field is unavailable")
            target_required = _SECRET_FIELDS[descriptor_key]
            if target_required and (
                not isinstance(target, str)
                or not target
                or len(target.encode("utf-8")) > 256
            ):
                raise CoreError("invalid_secret", "The requested secret field is unavailable")
            if not target_required and target not in (None, ""):
                raise CoreError("invalid_secret", "The requested secret field is unavailable")
            target_value = target if target_required else None
            adapter = self._domains.get(name)
            reader = getattr(adapter, "trusted_secret_value", None) if adapter is not None else None
            if not callable(reader):
                raise CoreError("invalid_secret", "The requested secret field is unavailable")
            try:
                value = reader(field_name, target_value)
            except Exception:
                raise CoreError("invalid_secret", "The requested secret field is unavailable") from None
            if (
                not isinstance(value, str)
                or len(value.encode("utf-8")) > _secret_value_limit(name, field_name, target_value)
                or "\x00" in value
                or (not _allows_multiline_secret(name, field_name, target_value) and any(character in value for character in "\r\n"))
            ):
                raise CoreError("invalid_secret", "The requested secret field is unavailable")
            return {
                "domain": name,
                "field": field_name,
                "target": target_value,
                "revision": self._revision,
                "present": bool(value.strip()),
            }

    def trusted_secret_value(
        self,
        domain: str,
        field: str,
        target: object | None,
        *,
        revision: int,
    ) -> str:
        """Read one plaintext field via an already-authorized native lease."""

        with self._lock:
            self._check_revision(revision)
            descriptor = self.trusted_secret_descriptor(domain, field, target)
            adapter = self._domains.get(str(descriptor["domain"]))
            reader = getattr(adapter, "trusted_secret_value", None) if adapter is not None else None
            if not callable(reader):
                raise CoreError("invalid_secret", "The requested secret field is unavailable")
            try:
                value = reader(str(descriptor["field"]), descriptor["target"])
            except Exception:
                raise CoreError("invalid_secret", "The requested secret field is unavailable") from None
            if (
                not isinstance(value, str)
                or len(value.encode("utf-8")) > _secret_value_limit(str(descriptor["domain"]), str(descriptor["field"]), descriptor["target"])
                or "\x00" in value
                or (not _allows_multiline_secret(str(descriptor["domain"]), str(descriptor["field"]), descriptor["target"]) and any(character in value for character in "\r\n"))
            ):
                raise CoreError("invalid_secret", "The requested secret field is unavailable")
            return value

    def stage_secret(
        self,
        domain: str,
        field: str,
        target: str | None,
        value: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        """Stage a secret received only from an authenticated native control."""

        if not isinstance(value, str) or len(value.encode("utf-8")) > _secret_value_limit(_canonical_domain(domain), field, target) or "\x00" in value or (not _allows_multiline_secret(_canonical_domain(domain), field, target) and any(char in value for char in "\r\n")):
            raise CoreError("invalid_secret", "The secret value is invalid")
        with self._lock:
            self._check_revision(revision)
            descriptor = self.secret_descriptor(domain, field, target)
            name = str(descriptor["domain"])
            adapter = self._domains[name]
            before = copy.deepcopy(self._baselines.get(name))
            try:
                adapter.stage_secret(str(descriptor["field"]), descriptor["target"], value)  # type: ignore[attr-defined]
                after = self._adapter_draft_state(name)
                present = bool(adapter.secret_present(str(descriptor["field"]), descriptor["target"]))  # type: ignore[attr-defined]
            except Exception as exc:
                raise CoreError(
                    "invalid_secret",
                    safe_exception_message(exc, known_secrets=(value,)),
                ) from None
            dirty = after != before
            self._revision += 1
            self._mark_domain(
                name,
                dirty=dirty,
                base_revision=(
                    self._revision
                    if not dirty
                    else self._drafts.get(name, {}).get("base_revision", self._revision)
                ),
            )
            self._persist_metadata()
            self._emit()
            return {"revision": self._revision, "present": present}

    def import_relay_resources(
        self,
        account_id: object,
        resource_ids: object,
        *,
        revision: int,
        mode: object = "linked",
    ) -> dict[str, Any]:
        """Stage only the API resources explicitly selected by the user."""

        with self._lock:
            self._check_revision(revision)
            relay = self._domains.get("relay_accounts")
            providers = self._domains.get("providers_models")
            importer = getattr(relay, "import_resources", None) if relay is not None else None
            if not isinstance(account_id, str) or not account_id or not callable(importer) or providers is None:
                raise CoreError("relay_import_failed", "Relay account is unavailable")
            import_mode = str(mode).strip().lower()
            if import_mode not in {"linked", "independent"}:
                raise CoreError("relay_import_failed", "Relay import mode is invalid")
            provider_checkpoint = _checkpoint_adapter(providers, error_code="relay_import_failed")
            try:
                result = importer(account_id, resource_ids, providers, mode=import_mode)
            except Exception as exc:
                try:
                    _restore_adapter(providers, provider_checkpoint)
                except Exception:
                    raise CoreError("relay_import_failed", "Provider/model import could not be rolled back") from None
                raise CoreError("relay_import_failed", safe_exception_message(exc)) from None
            self._revision += 1
            provider_dirty = self._adapter_draft_state("providers_models") != self._baselines.get("providers_models")
            self._mark_domain(
                "providers_models",
                dirty=provider_dirty,
                base_revision=(
                    self._drafts.get("providers_models", {}).get("base_revision", self._revision)
                    if provider_dirty
                    else self._revision
                ),
            )
            self._last_actions["relay_accounts"] = {
                "resources_ready": True,
                "account_id": account_id,
                "import_mode": import_mode,
                "resource_count": int(result.get("resource_count", 0)) if isinstance(result, Mapping) else 0,
                "model_count": int(result.get("model_count", 0)) if isinstance(result, Mapping) else 0,
            }
            self._persist_metadata()
            self._emit()
            return {
                "revision": self._revision,
                "imported": True,
                "import_mode": import_mode,
                "resource_count": int(result.get("resource_count", 0)) if isinstance(result, Mapping) else 0,
                "model_count": int(result.get("model_count", 0)) if isinstance(result, Mapping) else 0,
            }

    def accept_relay_login(
        self,
        *,
        account_id: object,
        account_type: object,
        label: object,
        origin: object,
        username: object,
        cookie: object = "",
        access_token: object = "",
        refresh_token: object = "",
        password: object = "",
        station_id: object = None,
        station_name: object = None,
        station_type: object = None,
        station_origin: object = None,
        remember_password: object = None,
        pending_account: object = False,
    ) -> dict[str, Any]:
        """Accept one native browser session and expose only public account state.

        ``remember_password`` is the host's post-login prompt decision. An
        absent value keeps the account's stored preference; a boolean applies
        the choice, so a previously saved password can be retired from the
        same prompt.
        """

        remember_choice = remember_password if isinstance(remember_password, bool) else None
        known_secrets = tuple(
            value for value in (cookie, access_token, refresh_token, password) if isinstance(value, str) and value
        )
        with self._lock:
            relay = self._domains.get("relay_accounts")
            accepter = getattr(relay, "accept_login_result", None) if relay is not None else None
            relay_checkpoint = getattr(relay, "transaction_checkpoint", None) if relay is not None else None
            relay_restore = getattr(relay, "restore_transaction", None) if relay is not None else None
            if (
                not callable(accepter)
                or not callable(relay_checkpoint)
                or not callable(relay_restore)
            ):
                raise CoreError("relay_login_failed", "Relay account is unavailable")
            relay_was_dirty = bool(self._drafts.get("relay_accounts", {}).get("dirty"))
            relay_prior_baseline = copy.deepcopy(self._baselines.get("relay_accounts"))
            relay_prior_base_revision = self._drafts.get("relay_accounts", {}).get("base_revision")
            relay_state = relay_checkpoint()
            core_checkpoint = {
                "revision": self._revision,
                "drafts": copy.deepcopy(self._drafts),
                "last_actions": copy.deepcopy(self._last_actions),
                "baselines": copy.deepcopy(self._baselines),
            }
            persistence_paths = list(_adapter_persistence_paths(relay))
            storage_path = getattr(relay, "storage_path", None)
            if isinstance(storage_path, (str, Path)):
                persistence_paths.append(Path(storage_path).expanduser())
            if self._metadata_store is not None:
                persistence_paths.append(self._metadata_store.path)
            try:
                file_checkpoints = _checkpoint_files(persistence_paths)
            except CoreError:
                raise CoreError("relay_login_failed", "Relay login could not be prepared") from None
            try:
                current = self._adapter_snapshot("relay_accounts")
                accounts = current.get("accounts", []) if isinstance(current, Mapping) else []
                matching = next(
                    (item for item in accounts if isinstance(item, Mapping) and item.get("id") == account_id),
                    None,
                )
                if not isinstance(matching, Mapping):
                    # Pending login: the host started the webview before any
                    # account existed, so a successful sign-in creates the
                    # shell now. A cancelled flow never reaches this branch
                    # and leaves no reserved slot behind. Stale accepts for
                    # unknown accounts without the pending flag still fail.
                    creator = getattr(relay, "add_account_for_login", None)
                    if (
                        not callable(creator)
                        or pending_account is not True
                        or not isinstance(account_id, str)
                        or not account_id
                    ):
                        raise CoreError("relay_login_failed", "Relay account is unavailable")
                    creator(
                        account_id=account_id,
                        account_type=str(account_type) if isinstance(account_type, str) else "",
                        label=str(label) if isinstance(label, str) else "",
                        origin=str(origin) if isinstance(origin, str) else "",
                        station_id=str(station_id) if isinstance(station_id, str) and station_id else None,
                        station_name=str(station_name) if isinstance(station_name, str) and station_name else None,
                        station_type=str(station_type) if isinstance(station_type, str) and station_type else None,
                        station_origin=str(station_origin) if isinstance(station_origin, str) and station_origin else None,
                        remember_password=remember_choice is True,
                    )
                    current = self._adapter_snapshot("relay_accounts")
                    accounts = current.get("accounts", []) if isinstance(current, Mapping) else []
                    matching = next(
                        (item for item in accounts if isinstance(item, Mapping) and item.get("id") == account_id),
                        None,
                    )
                    if not isinstance(matching, Mapping):
                        raise CoreError("relay_login_failed", "Relay account is unavailable")
                if (
                    matching.get("type") != account_type
                    or matching.get("label") != label
                    or matching.get("origin") != origin
                ):
                    raise CoreError("relay_login_failed", "Relay account does not match the saved site")
                public = accepter(
                    str(account_id),
                    username=str(username),
                    cookie=str(cookie) if isinstance(cookie, str) else "",
                    access_token=str(access_token) if isinstance(access_token, str) else "",
                    refresh_token=str(refresh_token) if isinstance(refresh_token, str) else "",
                    password=str(password) if isinstance(password, str) else "",
                    remember_password=remember_choice,
                )
                # Login is the durable browser-session boundary. Resource
                # discovery is a separate explicit action so a station API
                # outage cannot change the result of a successful sign-in.
                self._revision += 1
                self._record_relay_transient_update(
                    was_dirty=relay_was_dirty,
                    prior_baseline=relay_prior_baseline,
                    prior_base_revision=relay_prior_base_revision,
                )
                self._last_actions["relay_accounts"] = {
                    "logged_in": True,
                    "account_id": str(account_id),
                    "resources_ready": False,
                    "resource_count": 0,
                }
                self._persist_metadata()
            except CoreError:
                try:
                    _restore_files(file_checkpoints)
                    relay_restore(relay_state)
                finally:
                    self._revision = int(core_checkpoint["revision"])
                    self._drafts = core_checkpoint["drafts"]
                    self._last_actions = core_checkpoint["last_actions"]
                    self._baselines = core_checkpoint["baselines"]
                raise
            except Exception as exc:
                rollback_failed = False
                try:
                    _restore_files(file_checkpoints)
                    relay_restore(relay_state)
                except Exception:
                    rollback_failed = True
                self._revision = int(core_checkpoint["revision"])
                self._drafts = core_checkpoint["drafts"]
                self._last_actions = core_checkpoint["last_actions"]
                self._baselines = core_checkpoint["baselines"]
                if rollback_failed:
                    raise CoreError("relay_login_failed", "Relay login could not be rolled back") from None
                raise CoreError(
                    "relay_login_failed",
                    safe_exception_message(exc, known_secrets=known_secrets),
                ) from None
            self._emit()
            return {
                "revision": self._revision,
                "login_status": "signed_in",
                "username": str(public.get("username", username)),
            }

    def refresh_relay_resources(
        self,
        account_id: object,
        *,
        revision: int,
    ) -> dict[str, Any]:
        """Refresh selectable relay API metadata without staging providers."""

        with self._lock:
            self._check_revision(revision)
            relay = self._domains.get("relay_accounts")
            refresher = getattr(relay, "refresh_resources", None) if relay is not None else None
            if not isinstance(account_id, str) or not account_id or not callable(refresher):
                raise CoreError("relay_resources_failed", "Relay account is unavailable")
            relay_was_dirty = bool(self._drafts.get("relay_accounts", {}).get("dirty"))
            relay_prior_baseline = copy.deepcopy(self._baselines.get("relay_accounts"))
            relay_prior_base_revision = self._drafts.get("relay_accounts", {}).get("base_revision")
            try:
                result = refresher(account_id)
            except Exception as exc:
                raise CoreError("relay_resources_failed", safe_exception_message(exc)) from None
            resources = result.get("resources", []) if isinstance(result, Mapping) else []
            resource_status = result.get("resource_status", "unavailable") if isinstance(result, Mapping) else "unavailable"
            resource_count = len(resources) if isinstance(resources, list) else 0
            self._revision += 1
            self._record_relay_transient_update(
                was_dirty=relay_was_dirty,
                prior_baseline=relay_prior_baseline,
                prior_base_revision=relay_prior_base_revision,
            )
            self._last_actions["relay_accounts"] = {
                "resources_ready": resource_status == "ready",
                "account_id": account_id,
                "resource_status": resource_status,
                "resource_count": resource_count,
            }
            self._persist_metadata()
            self._emit()
            return {
                "revision": self._revision,
                "account_id": account_id,
                "resource_status": resource_status,
                "resource_count": resource_count,
            }

    def restore_relay_session(
        self,
        *,
        account_id: object,
        account_type: object,
        label: object,
        origin: object,
        login_status: object,
        username: object = "",
        cookie: object = "",
        access_token: object = "",
        refresh_token: object = "",
    ) -> dict[str, Any]:
        """Restore or update one locally validated native relay session.

        Unlike :meth:`accept_relay_login`, this never fetches API keys or
        modifies the provider/model draft. It is used after a Core restart to
        repopulate the process-local relay session from the per-account native
        credential store, or to persist an explicit expired/signed-out result.
        """

        known_secrets = tuple(
            value for value in (cookie, access_token, refresh_token) if isinstance(value, str) and value
        )
        with self._lock:
            relay = self._domains.get("relay_accounts")
            accepter = getattr(relay, "accept_login_result", None) if relay is not None else None
            status_setter = getattr(relay, "set_login_status", None) if relay is not None else None
            session_restorer = getattr(relay, "restore_saved_session", None) if relay is not None else None
            password_restorer = getattr(relay, "restore_saved_password", None) if relay is not None else None
            if (
                not callable(accepter)
                or not callable(status_setter)
                or not callable(session_restorer)
                or not callable(password_restorer)
            ):
                raise CoreError("relay_restore_failed", "Relay account is unavailable")
            relay_was_dirty = bool(self._drafts.get("relay_accounts", {}).get("dirty"))
            relay_prior_baseline = copy.deepcopy(self._baselines.get("relay_accounts"))
            relay_prior_base_revision = self._drafts.get("relay_accounts", {}).get("base_revision")
            current = self._adapter_snapshot("relay_accounts")
            accounts = current.get("accounts", []) if isinstance(current, Mapping) else []
            matching = next(
                (item for item in accounts if isinstance(item, Mapping) and item.get("id") == account_id),
                None,
            )
            if not isinstance(matching, Mapping):
                raise CoreError("relay_restore_failed", "Relay account is unavailable")
            if (
                matching.get("type") != account_type
                or matching.get("label") != label
                or matching.get("origin") != origin
            ):
                raise CoreError("relay_restore_failed", "Relay account does not match the saved site")
            try:
                if login_status == "signed_in":
                    public = accepter(
                        str(account_id),
                        username=str(username),
                        cookie=str(cookie) if isinstance(cookie, str) else "",
                        access_token=str(access_token) if isinstance(access_token, str) else "",
                        refresh_token=str(refresh_token) if isinstance(refresh_token, str) else "",
                        preserve_resources=True,
                    )
                elif login_status in {"signed_out", "expired"} and matching.get("remember_password") is True:
                    public = None
                    if login_status == "signed_out":
                        try:
                            public = session_restorer(str(account_id))
                        except Exception:
                            public = None
                    if public is None:
                        try:
                            public = password_restorer(str(account_id))
                        except Exception:
                            public = status_setter(str(account_id), str(login_status))
                elif login_status in {"signed_out", "expired"}:
                    public = status_setter(str(account_id), str(login_status))
                else:
                    raise CoreError("relay_restore_failed", "Relay login status is invalid")
                self._revision += 1
                self._record_relay_transient_update(
                    was_dirty=relay_was_dirty,
                    prior_baseline=relay_prior_baseline,
                    prior_base_revision=relay_prior_base_revision,
                )
                self._last_actions["relay_accounts"] = {
                    "session_restored": public.get("login_status") == "signed_in",
                    "account_id": str(account_id),
                    "login_status": str(public.get("login_status", login_status)),
                }
                self._persist_metadata()
            except CoreError:
                raise
            except Exception as exc:
                raise CoreError(
                    "relay_restore_failed",
                    safe_exception_message(exc, known_secrets=known_secrets),
                ) from None
            self._emit()
            return {
                "revision": self._revision,
                "login_status": str(public.get("login_status", login_status)),
                "username": str(public.get("username", "")),
            }

    @staticmethod
    def _providers_summary(states: Mapping[str, object]) -> dict[str, Any]:
        value = states.get("providers_models", {})
        if not isinstance(value, Mapping):
            return {"providers": [], "revision": 0}
        raw = value.get("providers", [])
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
            raw = []
        providers: list[dict[str, Any]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                continue
            provider_id = item.get("editor_id", item.get("id", item.get("name", f"provider-{index + 1}")))
            display_name = item.get("display_name", item.get("name", provider_id))
            raw_models = item.get("models", [])
            models: list[dict[str, Any]] = []
            if isinstance(raw_models, Sequence) and not isinstance(raw_models, (str, bytes, bytearray)):
                for model_index, model in enumerate(raw_models):
                    if not isinstance(model, Mapping):
                        continue
                    model_id = model.get(
                        "editor_id",
                        model.get("id", model.get("deployment_id", model.get("model_name", f"model-{model_index + 1}"))),
                    )
                    public_model = model.get(
                        "public_model",
                        model.get("model_name", model.get("name", model.get("display_name", model_id))),
                    )
                    model_display_name = model.get("display_name", model.get("name", public_model))
                    upstream_model = model.get("upstream_model", model.get("litellm_model", ""))
                    if isinstance(upstream_model, str) and "/" in upstream_model:
                        upstream_model = upstream_model.split("/", 1)[1]
                    order_value = model.get("order", 0)
                    try:
                        order = int(order_value) if not isinstance(order_value, bool) else 0
                    except (TypeError, ValueError, OverflowError):
                        order = 0
                    summary = {
                        "id": str(model_id),
                        "display_name": str(model_display_name),
                        "public_model": str(public_model),
                        "upstream_model": str(upstream_model),
                        "enabled": model.get("model_enabled", model.get("enabled", True)) is not False,
                        "order": order,
                    }
                    models.append(summary)
            model_count = item.get("model_count", len(models))
            try:
                model_count = max(0, int(model_count))
            except (TypeError, ValueError):
                model_count = 0
            providers.append(
                {
                    "id": str(provider_id),
                    "display_name": str(display_name),
                    "enabled": item.get("enabled") is not False,
                    "model_count": model_count,
                    "api_key": {
                        "present": bool(item.get("api_key_configured"))
                        or _secret_presence(item.get("api_key", item.get("api_keys")))
                    },
                    "endpoint": str(item.get("endpoint", item.get("api_base", "")) or ""),
                    "models": models,
                }
            )
        revision = value.get("revision", 0)
        return {"providers": providers, "revision": revision if type(revision) is int else 0}

    @staticmethod
    def _webdav_summary(states: Mapping[str, object]) -> dict[str, Any]:
        value = states.get("webdav", {})
        if not isinstance(value, Mapping):
            value = {}
        return {
            "enabled": bool(value.get("enabled", value.get("configured", False))),
            "configured": bool(value.get("configured", value.get("url"))),
            "last_probe": value.get("last_probe", "unknown") if value.get("last_probe", "unknown") in {"unknown", "ok", "failed"} else "unknown",
            "password": {"present": _secret_presence(value.get("password"))},
        }

    def _logs_summary(self, states: Mapping[str, object]) -> dict[str, dict[str, Any]]:
        """Expose only lightweight log state in global snapshots."""

        result = copy.deepcopy(self._logs)
        value = states.get("logs", {})
        if not isinstance(value, Mapping):
            return result
        tabs = value.get("tabs", value)
        if not isinstance(tabs, Mapping):
            return result
        for tab in LOG_TABS:
            raw = tabs.get(tab)
            if not isinstance(raw, Mapping):
                continue
            limit = raw.get("limit", SUBSCRIPTION_QUEUE_LIMIT)
            if type(limit) is not int or limit < 1:
                limit = SUBSCRIPTION_QUEUE_LIMIT
            result[tab] = {
                "tab": tab,
                "available": bool(raw.get("available")),
                "paused": bool(raw.get("paused")),
                "line_count": max(0, raw.get("line_count", 0))
                if type(raw.get("line_count", 0)) is int
                else 0,
                "filter": str(raw.get("filter", ""))[:256],
                "limit": limit,
            }
        return result

    @staticmethod
    def _language_summary(states: Mapping[str, object]) -> str:
        value = states.get("language", {})
        if isinstance(value, Mapping):
            choice = value.get("choice", value.get("language", "system"))
            if choice in {"system", "en", "zh-Hans"}:
                return str(choice)
        return "system"

    def _check_revision(self, expected_revision: object | None) -> None:
        if expected_revision is None:
            return
        if type(expected_revision) is not int or expected_revision < 0:
            raise CoreError("invalid_revision", "Revision is invalid")
        if expected_revision != self._revision:
            raise RevisionConflict(expected_revision, self._revision)

    def _mark_domain(self, name: str, *, dirty: bool | None = None, validation: Mapping[str, Any] | None = None, base_revision: int | None = None) -> None:
        record = self._drafts.setdefault(name, {"dirty": False, "base_revision": self._revision, "validation": {"valid": True, "issues": []}})
        if dirty is not None:
            record["dirty"] = bool(dirty)
        if validation is not None:
            record["validation"] = _validation_summary(validation)
        if base_revision is not None:
            record["base_revision"] = base_revision

    def _dispatch_codex_model_catalog_toggle(self, enabled: bool, expected_revision: object | None) -> dict[str, Any]:
        name = "codex"
        with self._lock:
            self._refresh_external_disk_state()
            self._check_revision(expected_revision)
            adapter = self._domains.get(name)
            if adapter is None:
                raise DomainNotFound(name)
            toggle = getattr(adapter, "set_model_catalog_enabled_immediately", None)
            baseline = getattr(adapter, "catalog_baseline_state", None)
            if not callable(toggle) or not callable(baseline):
                raise CoreError("domain_error", "Codex model catalog is unavailable")
            adapter_checkpoint = _checkpoint_adapter(adapter, error_code="apply_failed")
            persistence_paths = list(_adapter_persistence_paths(adapter))
            if self._metadata_store is not None:
                persistence_paths.append(self._metadata_store.path)
            file_checkpoints = _checkpoint_files(persistence_paths)
            core_checkpoint = {
                "revision": self._revision,
                "drafts": copy.deepcopy(self._drafts),
                "last_actions": copy.deepcopy(self._last_actions),
                "baselines": copy.deepcopy(self._baselines),
                "disk": copy.deepcopy(self._disk),
                "disk_identities": copy.deepcopy(self._disk_identities),
            }
            try:
                result = toggle(enabled)
                self._baselines[name] = baseline()
                dirty = self._adapter_draft_state(name) != self._baselines[name]
                self._disk[name] = {
                    "changed": False,
                    "generation": int(self._disk.get(name, {}).get("generation", 0)),
                    "keep_draft": False,
                }
                self._disk_identities[name] = self._external_disk_identity(adapter)
                self._revision += 1
                self._mark_domain(
                    name,
                    dirty=dirty,
                    validation={"valid": True, "issues": []},
                    base_revision=self._revision,
                )
                self._persist_metadata()
            except Exception as exc:
                try:
                    _restore_files(file_checkpoints)
                    _restore_adapter(adapter, adapter_checkpoint)
                except Exception:
                    raise CoreError("apply_failed", "Settings could not be rolled back") from None
                self._revision = int(core_checkpoint["revision"])
                self._drafts = core_checkpoint["drafts"]
                self._last_actions = core_checkpoint["last_actions"]
                self._baselines = core_checkpoint["baselines"]
                self._disk = core_checkpoint["disk"]
                self._disk_identities = core_checkpoint["disk_identities"]
                if isinstance(exc, CoreError):
                    raise
                raise CoreError("apply_failed", safe_exception_message(exc)) from None
            self._emit()
            # The dispatch envelope is a revision plus an optional action-scoped
            # summary.  Returning the catalog projection as the whole result
            # adds a top-level field the IPC contract rejects, which fails the
            # call before the switch is reported as applied.
            summary = _safe_public(result)
            return {
                "revision": self._revision,
                "action_summary": dict(summary) if isinstance(summary, Mapping) else {},
            }

    # -- WebDAV interval sync ---------------------------------------------
    def quiet_seconds(self) -> float:
        """Seconds since a user action last reached this store."""

        return max(0.0, time.monotonic() - self._last_activity)

    def webdav_auto_sync_state(self) -> dict[str, Any]:
        """What the interval loop reads: the saved switch, interval, and stamp.

        Only committed state counts here.  A half-typed URL in an open window
        must not start background traffic, and the loop re-reads on every pass,
        so nothing has to be pushed at it when the settings change.
        """

        from webdav import core as webdav_core

        idle = {"enabled": False, "configured": False, "sync_interval_minutes": 0, "last_sync_at": None}
        webdav = self._domains.get("webdav")
        settings_path = getattr(webdav, "settings_path", None)
        enabled_path = getattr(webdav, "enabled_path", None)
        state_path = getattr(webdav, "state_path", None)
        if not all(isinstance(value, (str, Path)) for value in (settings_path, enabled_path, state_path)):
            return idle
        try:
            settings = webdav_core.load_settings(Path(settings_path).expanduser())
            enabled = Path(enabled_path).expanduser().is_file()
            state = webdav_core.load_sync_state(Path(state_path).expanduser())
        except Exception:
            return idle
        last_sync_at = state.get("updated_at")
        return {
            "enabled": enabled,
            "configured": settings.configured,
            "sync_interval_minutes": settings.sync_interval_minutes,
            "sync_direction": settings.sync_direction,
            "last_sync_at": last_sync_at if isinstance(last_sync_at, str) else None,
        }

    def run_automatic_webdav_sync(self) -> None:
        """Run one sync for the interval loop, in the saved direction.

        This goes straight to the sync rather than through ``dispatch``: only
        the Core decides to run it, so no IPC action can ask for the unattended
        behaviour.
        """

        state = self.webdav_auto_sync_state()
        direction = state.get("sync_direction")
        # "smart" compares both sides; the IPC action for it is "sync".
        operation = direction if direction in {"push", "pull"} else "sync"
        self._dispatch_webdav_sync(
            operation,
            {"sections": ["providers_models", "relay_accounts"]},
            None,
            automatic=True,
        )

    def webdav_scheduler(self) -> Any:
        """The interval loop, created once and owned by this store."""

        if self._webdav_scheduler is None:
            from .webdav_scheduler import WebDAVSyncScheduler

            self._webdav_scheduler = WebDAVSyncScheduler(
                snapshot=self.webdav_auto_sync_state,
                sync=self.run_automatic_webdav_sync,
                settle_seconds=self.quiet_seconds,
            )
        return self._webdav_scheduler

    def _kick_webdav_scheduler(self) -> None:
        scheduler = self._webdav_scheduler
        if scheduler is not None:
            scheduler.kick()

    def _stop_webdav_scheduler(self) -> None:
        scheduler = self._webdav_scheduler
        if scheduler is not None:
            scheduler.stop()

    @staticmethod
    def _webdav_sync_sections(payload: object) -> tuple[str, str]:
        data = _as_mapping(payload)
        if set(data).difference({"sections"}):
            raise CoreError("invalid_sections", "WebDAV sync options are invalid")
        sections = data.get("sections")
        if sections is None:
            return ("providers_models", "relay_accounts")
        if isinstance(sections, (str, bytes, bytearray)) or not isinstance(sections, Sequence):
            raise CoreError("invalid_sections", "WebDAV sync sections are invalid")
        try:
            selected = tuple(dict.fromkeys(_canonical_domain(value) for value in sections))
        except CoreError:
            raise CoreError("invalid_sections", "WebDAV sync sections are invalid") from None
        required = {"providers_models", "relay_accounts"}
        if set(selected) != required:
            raise CoreError(
                "invalid_sections",
                "WebDAV sync requires Providers & Models and Relay Accounts together",
            )
        return ("providers_models", "relay_accounts")

    def _dispatch_webdav_sync(
        self,
        operation: str,
        payload: object,
        expected_revision: object | None,
        *,
        automatic: bool = False,
    ) -> dict[str, Any]:
        """Run one legacy-bundle WebDAV operation transactionally.

        ``automatic`` marks the interval loop's own run.  A window's unsaved
        draft stops a *reader's* sync, because that sync would race the edit
        they are making; an unattended one must not be blocked by it, or the
        interval would quietly do nothing whenever any pane held a draft.  It
        syncs the files on disk and lets the pane's disk-changed prompt
        reconcile whatever the reader had open.
        """

        sections = self._webdav_sync_sections(payload)
        from webdav import core as webdav_core
        from webdav import operations as webdav_operations

        with self._lock:
            self._refresh_external_disk_state()
            self._check_revision(expected_revision)
            webdav = self._domains.get("webdav")
            providers = self._domains.get("providers_models")
            relay = self._domains.get("relay_accounts")
            if webdav is None or providers is None or relay is None:
                raise CoreError("webdav_sync_failed", "WebDAV sync sources are unavailable")
            if not automatic:
                if any(self._drafts.get(name, {}).get("dirty") for name in ("webdav", *sections)):
                    raise CoreError("webdav_sync_conflict", "Save or discard pending changes before WebDAV sync")
                if any(self._disk.get(name, {}).get("changed") for name in ("webdav", *sections)):
                    raise CoreError("webdav_sync_conflict", "Reload changed local files before WebDAV sync")

            settings_getter = getattr(webdav, "sync_settings", None)
            if not callable(settings_getter):
                raise CoreError("webdav_sync_failed", "WebDAV sync is unavailable")
            try:
                settings = settings_getter()
            except Exception:
                raise CoreError("webdav_sync_failed", "WebDAV is not configured") from None
            config_value = getattr(providers, "config_path", None)
            relay_value = getattr(relay, "storage_path", None)
            state_value = getattr(webdav, "state_path", None)
            status_value = getattr(webdav, "status_path", None)
            if not all(isinstance(value, (str, Path)) for value in (config_value, relay_value, state_value, status_value)):
                raise CoreError("webdav_sync_failed", "WebDAV sync sources are unavailable")
            config_path = Path(config_value).expanduser()
            relay_path = Path(relay_value).expanduser()
            state_path = Path(state_value).expanduser()
            status_path = Path(status_value).expanduser()
            if not _same_path(relay_path, webdav_core.relay_accounts_path(config_path)):
                raise CoreError("webdav_sync_failed", "WebDAV sync sources are unavailable")

            adapter_checkpoints = {
                name: _checkpoint_adapter(adapter, error_code="webdav_sync_failed")
                for name, adapter in (("providers_models", providers), ("relay_accounts", relay))
            }
            core_checkpoint = {
                "revision": self._revision,
                "drafts": copy.deepcopy(self._drafts),
                "last_actions": copy.deepcopy(self._last_actions),
                "baselines": copy.deepcopy(self._baselines),
                "disk": copy.deepcopy(self._disk),
                "disk_identities": copy.deepcopy(self._disk_identities),
                "service": copy.deepcopy(self._service),
            }
            persistence_paths = [
                config_path,
                webdav_core.disabled_models_path(config_path),
                relay_path,
                state_path,
            ]
            if self._metadata_store is not None:
                persistence_paths.append(self._metadata_store.path)
            try:
                file_checkpoints = _checkpoint_files(persistence_paths)
            except CoreError:
                raise CoreError("webdav_sync_failed", "WebDAV sync could not prepare local files") from None

            pulled = False
            outcome = operation
            action_summary: dict[str, Any] = {}
            archived_remote: str | None = None
            adopted_remote: dict[str, Any] | None = None
            try:
                client = webdav_core.WebDAVClient(settings)
                # A setting that still carried an earlier default name is read
                # as the current one; move the file it pointed at so the
                # collection keeps one live bundle under this app's name.
                if settings.remote_name not in webdav_core.LEGACY_REMOTE_NAMES:
                    try:
                        adopted_remote = webdav_operations.adopt_renamed_remote_bundle(client, settings, webdav_core.LEGACY_REMOTE_NAMES)
                    except Exception:
                        adopted_remote = None
                if operation == "push":
                    webdav_operations.push_bundle(client, settings, config_path, state_path, "push")
                elif operation == "pull":
                    webdav_operations.pull_bundle(client, settings, config_path, state_path, "pull")
                    pulled = True
                else:
                    local_manifest = webdav_core.build_manifest(config_path)
                    try:
                        remote_manifest = webdav_operations.read_remote_manifest(client, settings)
                    except webdav_core.SyncError as exc:
                        if exc.code != webdav_core.INCOMPATIBLE_BUNDLE_CODE:
                            raise
                        # Smart sync decides by comparing, and a bundle from
                        # another app identity or archive version cannot be
                        # compared.  Rather than asking the reader to switch to
                        # 推送到远端, the sync keeps that file under a dated
                        # sibling name and pushes this Mac's state over it: the
                        # file the guard refuses to read is never lost, and the
                        # user asked for a sync, not for a setting to change.
                        archived_remote = webdav_operations.archive_remote_bundle(client, settings)
                        remote_manifest = None
                    base_manifest = webdav_core.baseline_manifest(state_path)
                    if remote_manifest is None:
                        webdav_operations.push_bundle(client, settings, config_path, state_path, "sync-push")
                        outcome = "push"
                    elif webdav_core.manifests_match(local_manifest, remote_manifest):
                        webdav_core.save_sync_state(state_path, settings, local_manifest, "sync")
                        outcome = "unchanged"
                    elif base_manifest is None:
                        raise webdav_core.SyncError("WebDAV sync conflict", "webdav_sync_conflict")
                    else:
                        local_changed = not webdav_core.manifests_match(local_manifest, base_manifest)
                        remote_changed = not webdav_core.manifests_match(remote_manifest, base_manifest)
                        if local_changed and not remote_changed:
                            webdav_operations.push_bundle(client, settings, config_path, state_path, "sync-push")
                            outcome = "push"
                        elif remote_changed and not local_changed:
                            webdav_operations.pull_bundle(client, settings, config_path, state_path, "sync-pull")
                            pulled = True
                            outcome = "pull"
                        elif local_changed and remote_changed:
                            raise webdav_core.SyncError("WebDAV sync conflict", "webdav_sync_conflict")
                        else:
                            webdav_core.save_sync_state(state_path, settings, local_manifest, "sync")
                            outcome = "unchanged"

                if pulled:
                    for name, adapter in (("providers_models", providers), ("relay_accounts", relay)):
                        adapter.reload()
                        self._baselines[name] = self._adapter_draft_state(name)
                        self._disk[name] = {
                            "changed": False,
                            "generation": int(self._disk.get(name, {}).get("generation", 0)),
                            "keep_draft": False,
                        }
                        self._disk_identities[name] = self._external_disk_identity(adapter)
                        self._mark_domain(
                            name,
                            dirty=False,
                            validation={"valid": True, "issues": []},
                            base_revision=self._revision + 1,
                        )
                webdav_core.save_sync_status(status_path, operation, True)
                self._last_actions["webdav"] = {
                    "action": operation,
                    "ok": True,
                    "outcome": outcome,
                    "sections": list(sections),
                }
                if adopted_remote is not None:
                    # A setting renamed from an earlier default moved its file;
                    # the pane states it when the server would not let the old
                    # one go.
                    action_summary["adopted_remote"] = adopted_remote
                if archived_remote is not None:
                    # What the pane states after the sync: this Mac's state
                    # replaced a remote file that is still on the server under
                    # the name below.
                    action_summary = {
                        "outcome": outcome,
                        "archived_remote": archived_remote,
                        "sections": list(sections),
                    }
                self._revision += 1
                self._persist_metadata()
                status_handler = self._service_handlers.get("status")
                if status_handler is not None:
                    try:
                        service_result = status_handler("health")
                    except Exception:
                        service_result = None
                    if isinstance(service_result, Mapping):
                        self._set_service_from_result(service_result, increment=False)
            except Exception as exc:
                rollback_failed = False
                try:
                    _restore_files(file_checkpoints)
                    for name, adapter in (("providers_models", providers), ("relay_accounts", relay)):
                        _restore_adapter(adapter, adapter_checkpoints[name])
                except Exception:
                    rollback_failed = True
                self._revision = int(core_checkpoint["revision"])
                self._drafts = core_checkpoint["drafts"]
                self._last_actions = core_checkpoint["last_actions"]
                self._baselines = core_checkpoint["baselines"]
                self._disk = core_checkpoint["disk"]
                self._disk_identities = core_checkpoint["disk_identities"]
                self._service = core_checkpoint["service"]
                try:
                    webdav_core.save_sync_status(status_path, operation, False)
                except Exception:
                    pass
                # A sync error names its own cause (an incompatible remote
                # bundle, a conflict); anything else is a plain failure.  The
                # cause keeps its name even when the rollback itself failed:
                # the pane has to state why the sync stopped, and "the rollback
                # failed too" is not something the reader can act on.
                cause_code = exc.code if isinstance(exc, webdav_core.SyncError) else "webdav_sync_failed"
                if rollback_failed:
                    raise CoreError(cause_code, "WebDAV sync could not roll back local files") from None
                if isinstance(exc, CoreError):
                    raise
                if isinstance(exc, webdav_core.SyncError):
                    raise CoreError(exc.code, str(exc)) from None
                raise CoreError(cause_code, "WebDAV sync failed") from None
            self._emit()
            result: dict[str, Any] = {"revision": self._revision}
            if action_summary:
                result["action_summary"] = action_summary
            return result

    def dispatch(
        self,
        action: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
        _trusted_native_capability: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(action, Mapping):
            raise CoreError("invalid_action", "A Core action is required")
        data = dict(action)
        self._last_activity = time.monotonic()
        if not _trusted_native_capability:
            self.reject_plaintext_secret_action(data)
        action_type = data.get("type", data.get("action"))
        if not isinstance(action_type, str) or not action_type.strip():
            raise CoreError("invalid_action", "A Core action is required")
        domain_value = data.get("domain")
        normalized_action = action_type.replace("-", "_").replace(".", "_").lower()
        if domain_value is not None and _canonical_domain(domain_value) == "webdav":
            # The switch, the interval, and the direction all reshape the
            # interval loop, so let it re-read instead of waiting out its sleep.
            self._kick_webdav_scheduler()
        if (
            domain_value is not None
            and _canonical_domain(domain_value) == "codex"
            and normalized_action == "codex_model_catalog_set"
        ):
            payload = _as_mapping(data.get("payload"))
            enabled = payload.get("enabled")
            if not isinstance(enabled, bool):
                raise CoreError("invalid_action", "The Codex model catalog switch is invalid")
            return self._dispatch_codex_model_catalog_toggle(
                enabled,
                expected_revision if expected_revision is not None else data.get("expected_revision"),
            )
        if domain_value is not None and _canonical_domain(domain_value) == "webdav":
            webdav_operation = {
                "push": "push",
                "webdav_push": "push",
                "pull": "pull",
                "webdav_pull": "pull",
                "sync": "sync",
                "webdav_sync": "sync",
            }.get(normalized_action)
            if webdav_operation is not None:
                return self._dispatch_webdav_sync(
                    webdav_operation,
                    data.get("payload"),
                    expected_revision if expected_revision is not None else data.get("expected_revision"),
                )
        with self._lock:
            self._check_revision(expected_revision if expected_revision is not None else data.get("expected_revision"))
            previous_revision = self._revision
            action_summary: dict[str, Any] | None = None
            if domain_value is None and action_type.startswith("service."):
                result = self._dispatch_service(action_type, _as_mapping(data.get("payload")))
            else:
                name = _canonical_domain(domain_value)
                adapter = self._domains.get(name)
                if adapter is None:
                    raise DomainNotFound(name)
                before = copy.deepcopy(self._baselines.get(name))
                action_draft_before = (
                    self._adapter_draft_state(name)
                    if name == "providers_models"
                    and normalized_action in {
                        "provider_auth_status",
                        "service_provider_auth_status",
                    }
                    else None
                )
                payload = data.get("payload")
                if name == "providers_models" and action_type.replace("-", "_").replace(".", "_") in {
                    "providers_import_selected",
                    "provider_import_selected",
                    "import_selected",
                } and isinstance(payload, Mapping):
                    file_token = payload.get("file_token", payload.get("fileToken"))
                    if file_token is not None:
                        selected_path = self.file_capabilities.resolve(file_token, "import")
                        payload = {"path": str(selected_path)}
                if name == "relay_accounts" and normalized_action in {
                    "resources_import",
                    "relay_resources_import",
                    "account_resources_import",
                }:
                    resource_data = _as_mapping(payload)
                    return _relay_transient_dispatch_result(
                        self.import_relay_resources(
                            resource_data.get("id", resource_data.get("account_id")),
                            resource_data.get("resource_ids"),
                            revision=self._revision,
                            mode=resource_data.get("import_mode", resource_data.get("mode", "linked")),
                        )
                    )
                if name == "relay_accounts" and normalized_action in {
                    "resources_refresh",
                    "relay_resources_refresh",
                    "account_resources_refresh",
                }:
                    resource_data = _as_mapping(payload)
                    return _relay_transient_dispatch_result(
                        self.refresh_relay_resources(
                            resource_data.get("id", resource_data.get("account_id")),
                            revision=self._revision,
                        )
                    )
                if name == "providers_models" and normalized_action == "provider_select_relay_station":
                    station_data = _as_mapping(payload)
                    relay = self._domains.get("relay_accounts")
                    resolver = getattr(relay, "provider_station_source", None)
                    if not callable(resolver):
                        raise CoreError("domain_error", "Relay stations are unavailable")
                    try:
                        source = resolver(station_data)
                    except Exception as exc:
                        raise CoreError("domain_error", safe_exception_message(exc)) from None
                    if not isinstance(source, Mapping):
                        raise CoreError("domain_error", "Relay station is unavailable")
                    payload = {
                        "provider_id": station_data.get("provider_id"),
                        "source": dict(source),
                    }
                relay_dependency_action = name == "relay_accounts" and normalized_action in {
                    "station_remove",
                    "relay_station_remove",
                    "remove_station",
                    "account_remove",
                    "relay_account_remove",
                    "remove_account",
                    "api_key_delete",
                    "relay_api_key_delete",
                    "account_api_key_delete",
                    "api_key_detach",
                    "relay_api_key_detach",
                    "account_api_key_detach",
                }
                providers = self._domains.get("providers_models") if relay_dependency_action else None
                provider_before = self._adapter_draft_state("providers_models") if providers is not None else None
                relay_checkpoint = _checkpoint_adapter(adapter, error_code="domain_error") if relay_dependency_action else None
                provider_checkpoint = _checkpoint_adapter(providers, error_code="domain_error") if providers is not None else None
                try:
                    if name == "providers_models" and normalized_action in {
                        "provider_import_relay_key",
                        "model_select_relay_resource",
                        "provider_fetch_relay_resource_models",
                    }:
                        resource_data = _as_mapping(payload)
                        relay = self._domains.get("relay_accounts")
                        resolver = getattr(relay, "binding_source", None)
                        if not callable(resolver):
                            raise CoreError(
                                "domain_error",
                                "Relay API resources are unavailable",
                            )
                        resolved = resolver(resource_data)
                        if not isinstance(resolved, Mapping):
                            raise CoreError(
                                "domain_error",
                                "Relay API resource is unavailable",
                            )
                        source = dict(resolved)
                        if normalized_action == "provider_fetch_relay_resource_models":
                            materializer = getattr(relay, "binding_materials", None)
                            if not callable(materializer):
                                raise CoreError(
                                    "domain_error",
                                    "Relay API key material is unavailable",
                                )
                            materials = materializer({"resources": [source]})
                            rows = materials.get("resources") if isinstance(materials, Mapping) else None
                            material = next(
                                (
                                    item
                                    for item in rows
                                    if isinstance(item, Mapping)
                                    and all(item.get(key) == source.get(key) for key in ("station_id", "account_id", "resource_id"))
                                ),
                                None,
                            ) if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, bytearray)) else None
                            if not isinstance(material, Mapping) or not isinstance(material.get("api_key"), str):
                                raise CoreError(
                                    "domain_error",
                                    "Relay API key material is unavailable",
                                )
                            source = dict(material)
                        payload = {
                            "provider_id": resource_data.get("provider_id"),
                            **(
                                {"model_id": resource_data.get("model_id")}
                                if normalized_action == "model_select_relay_resource"
                                else {}
                            ),
                            "source": source,
                            "api_key_name": resolved.get("api_key_name"),
                        }
                    result = adapter.dispatch(action_type, payload)
                    if relay_dependency_action and providers is not None:
                        relay_snapshot = adapter.snapshot()
                        details = relay_snapshot.get("last_action") if isinstance(relay_snapshot, Mapping) else None
                        resources = details.get("resources") if isinstance(details, Mapping) else None
                        policy = details.get("dependency_policy") if isinstance(details, Mapping) else None
                        if isinstance(resources, Sequence) and not isinstance(resources, (str, bytes, bytearray)):
                            normalized_policy = {
                                "delete_models": "delete",
                                "delete": "delete",
                                "detach": "detach",
                                "release": "detach",
                                "detach_disabled": "detach_disabled",
                                "rebind": "rebind",
                            }.get(str(policy or "detach"), "")
                            handler = getattr(providers, "apply_relay_dependency_policy", None)
                            if not normalized_policy or not callable(handler):
                                raise CoreError("domain_error", "Linked model dependency handling is unavailable")
                            rebind = _as_mapping(payload).get("rebind") if isinstance(payload, Mapping) else None
                            dependency_result = handler(resources, normalized_policy, rebind=rebind)
                            if isinstance(dependency_result, Mapping):
                                dependency_issues = dependency_result.get("issues")
                                if (
                                    isinstance(dependency_issues, Sequence)
                                    and not isinstance(dependency_issues, (str, bytes, bytearray))
                                    and dependency_issues
                                ):
                                    raise CoreError(
                                        "domain_error",
                                        "Linked model dependency handling could not be completed",
                                    )
                                self._last_actions["providers_models"] = dict(_safe_public(dependency_result))
                except ConfirmationNeeded:
                    if relay_checkpoint is not None:
                        _restore_adapter(adapter, relay_checkpoint)
                    if providers is not None and provider_checkpoint is not None:
                        _restore_adapter(providers, provider_checkpoint)
                    raise
                except Exception as exc:
                    if relay_checkpoint is not None:
                        try:
                            _restore_adapter(adapter, relay_checkpoint)
                            if providers is not None and provider_checkpoint is not None:
                                _restore_adapter(providers, provider_checkpoint)
                        except Exception:
                            raise CoreError("domain_error", "Linked model dependency handling could not be rolled back") from None
                    # Domain exceptions are authored to be safe; normalize
                    # them into the stable Core protocol error shape.
                    if isinstance(exc, CoreError):
                        raise
                    raise CoreError("domain_error", safe_exception_message(exc)) from None
                after = self._adapter_draft_state(name)
                if isinstance(result, Mapping):
                    safe_result = _safe_public(result)
                    if isinstance(safe_result, Mapping):
                        self._last_actions[name] = dict(safe_result)
                        summary = safe_result.get("operation_summary")
                        expected_operation = {
                            "fetch_models": "fetch_models",
                            "providers_fetch_models": "fetch_models",
                            "provider_fetch_models": "fetch_models",
                            "provider_fetch_relay_resource_models": "fetch_models",
                            "service_provider_add": "service_provider_add",
                            "service_add_provider": "service_provider_add",
                            "provider_auth_start": "provider_auth_start",
                            "provider_auth_cancel": "provider_auth_cancel",
                            "provider_auth_logout": "provider_auth_logout",
                            "provider_auth_status": "provider_auth_status",
                            "service_provider_auth_start": "service_provider_auth_start",
                            "service_provider_auth_cancel": "service_provider_auth_cancel",
                            "service_provider_auth_logout": "service_provider_auth_logout",
                            "service_provider_auth_status": "service_provider_auth_status",
                        }.get(normalized_action)
                        if (
                            name == "providers_models"
                            and expected_operation is not None
                            and isinstance(summary, Mapping)
                            and summary.get("operation") == expected_operation
                        ):
                            # Return the summary produced by this dispatch as
                            # an action-scoped value.  The snapshot keeps the
                            # latest summary for rendering, but another
                            # window's status poll can replace that slot before
                            # the caller refreshes it.
                            action_summary = dict(summary)
                cosmetic_binding = bool(
                    name == "providers_models"
                    and normalized_action == "provider_select_relay_station"
                    and getattr(adapter, "_cosmetic_binding", False)
                )
                if cosmetic_binding:
                    # The station binding changed only the hidden source
                    # metadata; the visible name and URL are untouched.  Keep
                    # the draft clean so closing the window never asks to
                    # discard a change that altered nothing visible.
                    self._baselines[name] = copy.deepcopy(after)
                dirty = False if cosmetic_binding else after != before
                action_mutated = action_draft_before is None or after != action_draft_before
                if action_mutated:
                    self._revision += 1
                    self._mark_domain(
                        name,
                        dirty=dirty,
                        base_revision=(
                            self._drafts.get(name, {}).get("base_revision", self._revision)
                            if dirty
                            else self._revision
                        ),
                    )
                    if providers is not None and provider_before is not None:
                        provider_after = self._adapter_draft_state("providers_models")
                        provider_dirty = provider_after != self._baselines.get("providers_models")
                        self._mark_domain(
                            "providers_models",
                            dirty=provider_dirty,
                            base_revision=(
                                self._drafts.get("providers_models", {}).get("base_revision", self._revision)
                                if provider_dirty
                                else self._revision
                            ),
                        )
                    self._persist_metadata()
            if self._revision != previous_revision:
                self._emit()
            response: dict[str, Any] = {"revision": self._revision}
            if action_summary is not None:
                response["action_summary"] = action_summary
            return response

    def reject_plaintext_secret_action(self, action: Mapping[str, Any]) -> None:
        """Reject secret-bearing actions received through ordinary IPC."""

        if not isinstance(action, Mapping):
            raise CoreError("invalid_action", "A Core action is required")
        action_type = action.get("type", action.get("action"))
        if not isinstance(action_type, str):
            return
        if action.get("domain") is None and action_type.startswith("service."):
            return
        name = _canonical_domain(action.get("domain"))
        normalized = action_type.strip().lower().replace("-", "_").replace(".", "_")
        payload = action.get("payload")
        forbidden: set[str] = set()
        if name == "providers_models":
            forbidden = {"api_key", "api_keys", "provider_auth_token"}
            if normalized in {"provider_clear_key", "clear_provider_key", "set_raw", "setraw"}:
                raise CoreError("secret_requires_native", "Use the native secure input for this secret field")
        elif name == "codex":
            forbidden = {"api_key", "auth_text", "raw_json"}
            if normalized in {"set_raw", "setraw"}:
                raise CoreError("secret_requires_native", "Use the versioned code editor for this document")
        elif name == "claude":
            forbidden = {
                "env",
                "desktop_profile",
                "auto_memory_directory",
                "autoMemoryDirectory",
                "token",
                "auth_token",
                "anthropic_auth_token",
                "anthropic_api_key",
                "raw_json",
            }
            if normalized in {"set_raw", "setraw"}:
                raise CoreError("secret_requires_native", "Use the versioned code editor for this document")
        elif name == "webdav":
            forbidden = {"password"}
            if normalized in {"clear_password", "clearpassword"}:
                raise CoreError("secret_requires_native", "Use the native secure input for this secret field")
        elif name == "runtime":
            adapter = self._domains.get(name)
            is_secret = getattr(adapter, "_is_secret_setting", None)
            data = payload if isinstance(payload, Mapping) else {}
            target = data.get("key")
            if isinstance(target, str) and callable(is_secret) and is_secret(target):
                raise CoreError("secret_requires_native", "Use the native secure input for this secret field")
            values = data.get("values", data)
            if isinstance(values, Mapping) and callable(is_secret) and any(
                isinstance(key, str) and is_secret(key) for key in values
            ):
                raise CoreError("secret_requires_native", "Use the native secure input for this secret field")
            if normalized in {"set_raw", "setraw"}:
                raise CoreError("secret_requires_native", "Use the versioned code editor for this document")
        if forbidden and _mapping_contains_key(payload, forbidden):
            raise CoreError("secret_requires_native", "Use the native secure input for this secret field")

    def _dispatch_service(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        operation = action.removeprefix("service.")
        previous_service = copy.deepcopy(self._service)
        handler = self._service_handlers.get(operation)
        if handler is not None:
            try:
                # A manual restart and a background post-apply restart must
                # never replace the proxy at the same time.
                with self._service_transition_guard:
                    result = handler(operation)
            except Exception as exc:
                raise CoreError("service_error", safe_exception_message(exc)) from None
            if isinstance(result, Mapping):
                self._set_service_from_result(result, increment=False)
                if str(result.get("state")) == "running" and operation in {"start", "start_async", "restart", "reload"}:
                    # The proxy's port or key may have just changed; a Codex
                    # client that already uses the proxy has to follow it.
                    self._follow_codex_local_api()
            else:
                raise CoreError("service_error", "LiteLLM service returned invalid status")
        else:
            # Test-only stores can use an injected handler. Never use this
            # optimistic fallback in production construction.
            state = {"start": "starting", "start_async": "starting", "running": "running", "stop": "stopped", "stopped": "stopped", "restart": "starting", "reload": self._service.get("state", "unknown"), "health": self._service.get("state", "unknown")}.get(operation)
            if state in SERVICE_STATES:
                self._service["state"] = state
        if isinstance(payload.get("detail"), str):
            self._service["detail"] = safe_error_message(payload["detail"])
        # A manual health check which projects the same public status is not a
        # state transition, so it does not redraw every settings surface.
        if self._service != previous_service:
            self._revision += 1
            self._persist_metadata()
        return dict(self._service)

    def _set_service_from_result(self, result: Mapping[str, Any], *, increment: bool) -> None:
        """Project a real controller status into the public Core snapshot."""

        state = result.get("state")
        if state not in SERVICE_STATES:
            state = "unknown"
        service: dict[str, Any] = {"state": state}
        for key in ("detail", "auto_start_state", "route_recovery", "webdav"):
            value = result.get(key)
            if value is not None:
                service[key] = _safe_public(value)
        pid = result.get("pid")
        if type(pid) is int and pid > 0:
            service["pid"] = pid
        port = result.get("port")
        if state == "running" and type(port) is int and 1 <= port <= 65535:
            service["port"] = port
        self._service = service
        if increment:
            self._revision += 1

    def _schedule_service_reload_after_apply(self) -> None:
        """Queue the post-apply LiteLLM restart without blocking the commit.

        Replacing the proxy takes seconds, so the restart runs on its own
        thread: the committed edit answers immediately and every later IPC
        request stays fast.  A burst of edits coalesces into one final restart
        against the newest configuration.
        """

        if self._service_handlers.get("reload") is None:
            return
        with self._service_reload_guard:
            if self._service_reload_stopping:
                return
            thread = self._service_reload_thread
            if thread is not None and thread.is_alive():
                # A restart is already replacing the proxy; it will pick up
                # this commit as its follow-up pass.
                self._service_reload_pending = True
                return
            if self._service.get("state") not in {"running", "unhealthy"}:
                return
            thread = threading.Thread(
                target=self._service_reload_worker,
                name="litellm-core-service-reload",
                daemon=True,
            )
            self._service_reload_thread = thread
        thread.start()

    def wait_for_service_reload(self, timeout: float = 10.0) -> bool:
        """Wait for a background post-apply restart to finish.

        The native hosts and focused tests use this to observe the restart
        that Apply now performs off the request path.
        """

        thread = self._service_reload_thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _service_reload_worker(self) -> None:
        while True:
            try:
                self._reload_service_after_apply()
            except Exception:
                # A failed background restart must not kill the worker before
                # the controller's real state is projected.
                self._project_service_reload_failure()
            with self._service_reload_guard:
                if self._service_reload_stopping or not self._service_reload_pending:
                    self._service_reload_thread = None
                    return
                self._service_reload_pending = False

    def _reload_service_after_apply(self) -> None:
        """Reload LiteLLM after settings that shape the proxy are committed.

        Runs only on the reload worker: the controller restart takes seconds,
        and holding the Core lock across it would freeze every other request.
        """

        with self._lock:
            if self._service_reload_stopping:
                return
            if self._service.get("state") not in {"running", "unhealthy"}:
                return
            reloader = self._service_handlers.get("reload")
            if reloader is None:
                return
            # Project the restart so every open window sees the service leave
            # the running state while the proxy is replaced.
            self._set_service_from_result({"state": "starting"}, increment=False)
            self._emit()
        try:
            with self._service_transition_guard:
                if self._service_reload_stopping:
                    return
                service_result = reloader("reload")
        except Exception:
            self._project_service_reload_failure()
            return
        if not isinstance(service_result, Mapping):
            self._project_service_reload_failure()
            return
        with self._lock:
            self._set_service_from_result(service_result, increment=False)
            if service_result.get("state") == "running":
                codex = self._domains.get("codex")
                refresh_catalog = getattr(codex, "refresh_model_catalog", None)
                if callable(refresh_catalog):
                    try:
                        refresh_catalog()
                    except Exception:
                        pass
                self._follow_codex_local_api(codex)
            stopping = self._service_reload_stopping
            self._emit()
        if stopping:
            # The host quit while this restart was in flight; shutdown() may
            # have stopped the previous proxy before this one started.
            self._stop_service_after_reload_shutdown()

    def _follow_codex_local_api(self, codex: object | None = None) -> None:
        """Keep a Codex client that uses this app's proxy on its current endpoint.

        A restart is where the proxy's port and master key can change, so a
        client that already points at the proxy is rewritten there instead of
        being left on a closed port or a retired key.  The caller holds
        ``self._lock``; the write happens before the revision is published so
        every open window sees the new file and the new state together.
        """

        adapter = codex if codex is not None else self._domains.get("codex")
        follow = getattr(adapter, "follow_local_api_endpoint", None)
        if not callable(follow):
            return
        try:
            changed = bool(follow())
        except Exception:
            # A client file this app cannot rewrite (locked, unreadable, hand
            # edited) must not fail the proxy restart that just succeeded.
            return
        if not changed or adapter is None:
            return
        name = "codex"
        # The applied state is the adapter's draft projection, which is what a
        # normal apply records as the baseline for this domain; recording the
        # written documents here clears the dirty flag the reconcile is not
        # responsible for.
        self._baselines[name] = copy.deepcopy(self._adapter_draft_state(name))
        self._disk[name] = {
            "changed": False,
            "generation": int(self._disk.get(name, {}).get("generation", 0)),
            "keep_draft": False,
        }
        self._disk_identities[name] = self._external_disk_identity(adapter)
        self._revision += 1
        self._mark_domain(
            name,
            dirty=False,
            validation={"valid": True, "issues": []},
            base_revision=self._revision,
        )
        self._persist_metadata()

    def _project_service_reload_failure(self) -> None:
        """Project the controller's real state after a failed background restart."""

        with self._lock:
            result: Mapping[str, Any] | None = None
            for operation in ("health", "status"):
                handler = self._service_handlers.get(operation)
                if handler is None:
                    continue
                try:
                    candidate = handler(operation)
                except Exception:
                    continue
                if isinstance(candidate, Mapping):
                    result = candidate
                    break
            self._set_service_from_result(
                result if result is not None else {"state": "unhealthy"},
                increment=False,
            )
            self._emit()

    def _stop_service_after_reload_shutdown(self) -> None:
        stopper = self._shutdown_handler
        if stopper is not None:
            try:
                with self._service_transition_guard:
                    result = stopper("stop")
                if isinstance(result, Mapping):
                    with self._lock:
                        self._set_service_from_result(result, increment=False)
            except Exception:
                pass
        with self._lock:
            self._emit()

    def validate(self, domain: str | None = None, *, revision: int | None = None, payload: object | None = None) -> dict[str, Any]:
        with self._lock:
            self._check_revision(revision)
            names = [_canonical_domain(domain)] if domain is not None else list(self._domains)
            summaries: dict[str, Any] = {}
            for name in names:
                adapter = self._domains.get(name)
                if adapter is None:
                    raise DomainNotFound(name)
                try:
                    value = adapter.validate(payload if len(names) == 1 else None)
                except Exception as exc:
                    value = {"valid": False, "errors": [safe_exception_message(exc)]}
                summary = _validation_summary(value)
                self._mark_domain(name, validation=summary)
                summaries[name] = summary
            if domain is not None:
                return summaries[names[0]]
            issues: list[dict[str, Any]] = []
            for name, summary in summaries.items():
                for issue in summary["issues"]:
                    issue = dict(issue)
                    issue["path"] = f"{name}.{issue.get('path', '')}".rstrip(".")
                    issues.append(issue)
            return {"valid": not any(issue["severity"] == "error" for issue in issues), "issues": issues}

    @staticmethod
    def _relay_operation_count(relay: DomainAdapter) -> int:
        """Return the public count of remote relay work that remains.

        The relay adapter deliberately owns the journal and its retry state.
        Core only needs the count for the Apply result, and must never copy a
        credential-bearing resource payload into an IPC response.
        """

        try:
            state = relay.snapshot()
        except Exception:
            return 0
        if not isinstance(state, Mapping):
            return 0
        count = state.get("pending_operation_count")
        if type(count) is int and count >= 0:
            return count
        entries = state.get("pending_operations")
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes, bytearray)):
            return 0
        return sum(
            1
            for entry in entries
            if isinstance(entry, Mapping)
            and str(entry.get("status", entry.get("state", "staged"))) != "completed"
        )

    @staticmethod
    def _relay_apply_issues(count: int, *, phase: str = "relay_apply") -> list[dict[str, str]]:
        """Return a stable secret-free issue summary for an Apply result."""

        if count <= 0:
            return []
        return [
            {
                "code": phase,
                "message": "Relay synchronization has work that still needs attention.",
            }
        ]

    @staticmethod
    def _relay_public_issue_count(value: object) -> int:
        """Count relay issues without echoing remote/server text to IPC."""

        if not isinstance(value, Mapping):
            return 0
        issues = value.get("issues")
        if not isinstance(issues, Sequence) or isinstance(issues, (str, bytes, bytearray)):
            return 0
        return sum(1 for item in issues if isinstance(item, (str, Mapping)))

    def _mark_relay_coordinated_applied(self, name: str) -> None:
        """Advance Core's local baseline after one coordinated local commit."""

        self._baselines[name] = self._adapter_draft_state(name)
        self._disk[name] = {
            "changed": False,
            "generation": int(self._disk.get(name, {}).get("generation", 0)),
            "keep_draft": False,
        }
        self._mark_domain(name, dirty=False, validation={"valid": True, "issues": []}, base_revision=self._revision + 1)

    def _apply_relay_coordinated(
        self,
        names: Sequence[str],
        adapters: Mapping[str, DomainAdapter],
        confirm_codes: Sequence[str],
        overwrite_codes: set[str],
        *,
        relay_dependency_only: bool = False,
    ) -> dict[str, Any]:
        """Apply relay and linked model changes around irreversible remote work.

        The normal multi-domain Apply path can restore local files and adapter
        objects. It cannot undo a remote key create, rotation, or deletion.
        This coordinator therefore commits in the one safe ordering:

        * validate and journal first;
        * perform non-destructive remote mutations;
        * reconcile stable resource IDs and materialize linked deployments;
        * save local files and reload LiteLLM;
        * only then delete remote keys.

        Once an operation may have reached the remote service, a later local
        failure becomes a truthful ``partial`` result. The relay journal is
        intentionally retained for a retry instead of attempting a fictional
        rollback.
        """

        relay = adapters.get("relay_accounts")
        if relay is None:
            raise CoreError("relay_apply_unavailable", "Relay synchronization is unavailable")
        prepare = getattr(relay, "prepare_apply", None)
        execute = getattr(relay, "execute_pending_operations", None)
        reconcile = getattr(relay, "reconcile_apply", None)
        commit = getattr(relay, "commit_apply", None)
        finalize = getattr(relay, "finalize_apply", None)
        binding_materials = getattr(relay, "binding_materials", None)
        if not all(callable(method) for method in (prepare, execute, reconcile, commit, finalize, binding_materials)):
            raise CoreError("relay_apply_unavailable", "Relay synchronization is unavailable")

        transaction_adapters = dict(adapters)
        adapter_checkpoints = {
            name: _checkpoint_adapter(adapter, error_code="apply_failed")
            for name, adapter in transaction_adapters.items()
        }
        core_checkpoint = {
            "revision": self._revision,
            "drafts": copy.deepcopy(self._drafts),
            "last_actions": copy.deepcopy(self._last_actions),
            "baselines": copy.deepcopy(self._baselines),
            "disk": copy.deepcopy(self._disk),
            "disk_identities": copy.deepcopy(self._disk_identities),
            "service": copy.deepcopy(self._service),
        }
        persistence_paths = [
            path
            for adapter in transaction_adapters.values()
            for path in _adapter_persistence_paths(adapter)
        ]
        if self._metadata_store is not None:
            persistence_paths.append(self._metadata_store.path)
        file_checkpoints = _checkpoint_files(persistence_paths)
        remote_boundary_crossed = False
        provider_locally_applied = False
        relay_locally_applied = False
        applied: list[str] = []
        operation_total = 0

        def restore_local_state() -> None:
            _restore_files(file_checkpoints)
            for adapter_name, adapter in transaction_adapters.items():
                _restore_adapter(adapter, adapter_checkpoints[adapter_name])
            self._revision = int(core_checkpoint["revision"])
            self._drafts = copy.deepcopy(core_checkpoint["drafts"])
            self._last_actions = copy.deepcopy(core_checkpoint["last_actions"])
            self._baselines = copy.deepcopy(core_checkpoint["baselines"])
            self._disk = copy.deepcopy(core_checkpoint["disk"])
            self._disk_identities = copy.deepcopy(core_checkpoint["disk_identities"])
            self._service = copy.deepcopy(core_checkpoint["service"])

        try:
            for name in names:
                if f"overwrite_external_{name}" not in overwrite_codes:
                    continue
                rebase = getattr(adapters[name], "rebase_external_disk", None)
                if not callable(rebase):
                    raise CoreError("apply_failed", "Settings changed on disk; choose the disk version")
                rebase()
                self._disk[name]["changed"] = False
                self._disk[name]["keep_draft"] = True

            # Validate every participating local domain before a remote API
            # call. Relay's preflight separately validates login/session,
            # operation targets, and deletion policies.
            for name in names:
                if name == "providers_models":
                    # Linked provider keys intentionally have no materialized
                    # value until the relay resolution step below. Validate
                    # the rest of the candidate with a domain-owned placeholder
                    # contract, then run the strict validator after private
                    # material is injected and before any local write.
                    preflight = getattr(adapters[name], "validate_relay_preflight", None)
                    if not callable(preflight) or preflight().get("valid") is not True:
                        raise CoreError("provider_model_invalid", "Fix the provider/model issues")
                    continue
                if relay_dependency_only and name == "relay_accounts":
                    # This Apply uses a linked key; it is not the user's relay
                    # work, so the station's session state and its outstanding
                    # operations are not this edit's validation.
                    continue
                if not self.validate(name)["valid"]:
                    raise CoreError("relay_preflight_failed", "Fix the validation errors")
            prepared = {"ready": True, "operations": ()} if relay_dependency_only else prepare()
            if not isinstance(prepared, Mapping) or prepared.get("ready") is not True:
                # The relay is what is not ready here: pending operations, a
                # session that needs a sign-in, or a target that moved.  Say so
                # with its own code instead of reporting the user's edit as a
                # generic validation failure.
                raise CoreError("relay_not_ready", "Fix the relay connection or binding issues")
            operations = prepared.get("operations", ())
            if isinstance(operations, Sequence) and not isinstance(operations, (str, bytes, bytearray)):
                operation_total = len(operations)
            else:
                operations = ()
            # The journal itself is the phase source of truth. It is kept
            # deliberately small and secret-free, so Core can classify work
            # without inspecting an API request body.
            non_destructive = [] if relay_dependency_only else [
                operation
                for operation in operations
                if isinstance(operation, Mapping)
                and operation.get("kind") != "api_key_delete"
                and operation.get("state", operation.get("status", "staged")) == "staged"
            ]
            non_destructive_result: object = {"issues": []}
            if non_destructive:
                # We cross the remote boundary immediately before invoking the
                # helper because a timeout is ambiguous: it may have applied
                # the remote mutation even when no response was received.
                remote_boundary_crossed = True
                non_destructive_result = execute(prepared, phase="non_destructive")

            if not relay_dependency_only:
                reconciled = reconcile(prepared, phase="non_destructive")
                # Transport failures are provisional until the immediate factual
                # refresh. A lost response may still have applied remotely; only
                # unresolved reconciliation issues stop the coordinated Apply —
                # and that is the relay's own outstanding work, which the relay
                # message names, never the edit the user just made.
                if self._relay_public_issue_count(reconciled):
                    raise CoreError("relay_not_ready", "Fix the relay connection or binding issues")

            providers = adapters.get("providers_models")
            if providers is not None:
                materialize = getattr(providers, "materialize_relay_bindings", None)
                dependency_summary = getattr(providers, "dependency_summary", None)
                if not callable(materialize) or not callable(dependency_summary):
                    raise CoreError("relay_apply_unavailable", "Linked model synchronization is unavailable")
                dependencies = dependency_summary()
                provider_keys = dependencies.get("provider_keys", []) if isinstance(dependencies, Mapping) else []
                sources = [
                    item.get("source")
                    for item in provider_keys
                    if isinstance(item, Mapping)
                    and isinstance(item.get("source"), Mapping)
                ]
                if sources:
                    # A relay ProviderKey is itself a dynamic binding, even
                    # before a model selects it.  Resolve every selected key
                    # here so a provider-side key import is fully usable
                    # after Apply; private material remains inside Core.
                    # Resolving a linked key may still read the station — a
                    # key that has never been materialized has to come from
                    # somewhere — but this Apply's *readiness* never depends on
                    # it: the resolution falls back to the credential already
                    # persisted on the slot and to the catalog the last
                    # successful read recorded, so signing in is not a
                    # precondition for managing a model.
                    # The durable half of "a key this Core already holds":
                    # the provider document persists each linked slot's value,
                    # and the relay domain's own cache does not survive a
                    # restart.  Passing it here is what makes a model edit work
                    # on a fresh Core while the station is unreachable.
                    slot_reader = getattr(providers, "relay_slot_credentials", None)
                    slot_credentials = slot_reader() if callable(slot_reader) else None
                    materials = binding_materials(
                        {"resources": sources},
                        refresh=True,
                        slot_credentials=slot_credentials,
                    )
                    materialized = materialize(materials)
                    # Whatever this resolution said becomes the pane's own map
                    # of the linked rows: a successful one clears it, a failed
                    # one marks the key or route that refused the write.
                    recorder = getattr(providers, "record_binding_issues", None)
                    if callable(recorder):
                        recorder(materialized.get("issues") if isinstance(materialized, Mapping) else ())
                    if self._relay_public_issue_count(materialized):
                        # A linked key that the relay could not resolve — a moved
                        # or disabled resource, a base-URL conflict, an upstream
                        # model the group does not serve — refused the write, so
                        # the strip names that cause instead of a bare rollback:
                        # the internal step name stays out of the pane.
                        raise CoreError(
                            "relay_binding_failed",
                            "A linked relay binding could not be resolved",
                        )
                if not self.validate("providers_models")["valid"]:
                    raise CoreError("provider_model_invalid", "Fix the linked model issues")
                providers.apply()
                provider_locally_applied = True
                self._mark_relay_coordinated_applied("providers_models")
                applied.append("providers_models")

            # Other local domains can participate in one explicit Apply. They
            # commit after non-destructive relay work but before any remote
            # deletion. A failure is therefore either fully rollbackable
            # (there were no remote mutations) or truthfully partial.
            for name in names:
                if name in {"providers_models", "relay_accounts"}:
                    continue
                adapter = adapters[name]
                domain_confirmations = list(confirm_codes)
                if name == "claude" and any(
                    item in {"accepted", "claude-risk-confirmed"} for item in domain_confirmations
                ):
                    try:
                        from .domains.claude import RISK_CONFIRMATION_CODES
                    except Exception:
                        RISK_CONFIRMATION_CODES = ()
                    domain_confirmations = [str(item) for item in RISK_CONFIRMATION_CODES]
                payload: object | None = None
                if domain_confirmations:
                    payload = {"confirm_risks": domain_confirmations, "confirmation": domain_confirmations}
                if payload is None:
                    adapter.apply()
                else:
                    adapter.apply(payload)
                self._mark_relay_coordinated_applied(name)
                applied.append(name)

            # The relay's durable draft includes operation-journal state. It
            # is committed only after model materialization has reached disk,
            # so a remote create cannot be lost if the app exits now.
            if relay_dependency_only:
                # The relay domain took part — it resolved the keys this edit
                # binds — but it committed no journal work, so its own pending
                # operations stay exactly as they were, retryable from the
                # relay's own surfaces.
                applied.append("relay_accounts")
            else:
                commit()
                relay_locally_applied = True
                self._mark_relay_coordinated_applied("relay_accounts")
                applied.append("relay_accounts")

            if provider_locally_applied:
                self._schedule_service_reload_after_apply()

            destructive = [] if relay_dependency_only else [
                operation
                for operation in operations
                if isinstance(operation, Mapping)
                and operation.get("kind") == "api_key_delete"
                and operation.get("state", operation.get("status", "staged")) == "staged"
            ]
            destructive_result: object = {"issues": []}
            if destructive:
                # Local configuration is now independent of every key marked
                # for removal; a failed deletion remains journaled for retry.
                remote_boundary_crossed = True
                destructive_result = execute(prepared, phase="destructive")
            if relay_dependency_only:
                # This edit carries no relay work of its own, so the journal's
                # remaining phases are not part of its result: every pending
                # operation stays exactly as the user left it, retryable from
                # the relay's own surfaces.
                issue_count = 0
                pending = 0
            else:
                reconciled_after_delete = reconcile(prepared, phase="destructive")
                finalize_result = finalize()
                issue_count = (
                    self._relay_public_issue_count(reconciled_after_delete)
                    + self._relay_public_issue_count(finalize_result)
                )
                pending = self._relay_operation_count(relay)
            if issue_count or pending:
                # The local relay document is authoritative for completed
                # work, while outstanding journal entries keep this domain
                # dirty and retryable.  A dependency-only Apply owns none of
                # that work, so it must not re-dirty the relay either: doing so
                # would put relay_accounts back into the next edit's domain
                # list and drag a model edit straight back into the station's
                # backlog.
                if not relay_dependency_only:
                    self._drafts["relay_accounts"]["dirty"] = True
                self._drafts["relay_accounts"]["validation"] = {"valid": True, "issues": []}
                self._revision += 1
                self._persist_metadata()
                self._emit()
                return {
                    "revision": self._revision,
                    "applied": False,
                    "status": "partial",
                    "domains": applied,
                    "completed_operations": max(operation_total - pending, 0),
                    "pending_operations": pending,
                    "issues": self._relay_apply_issues(issue_count),
                }

            if not relay_dependency_only:
                self._mark_relay_coordinated_applied("relay_accounts")
            self._revision += 1
            self._persist_metadata()
        except Exception as exc:
            if not remote_boundary_crossed:
                rollback_failed = False
                try:
                    restore_local_state()
                except Exception:
                    rollback_failed = True
                if rollback_failed:
                    raise CoreError("apply_failed", "Settings could not be rolled back") from None
                if isinstance(exc, CoreError):
                    raise
                raise CoreError("apply_failed", safe_exception_message(exc)) from None

            # Remote work may have succeeded even if the network connection
            # timed out. Do not restore its operation journal or local files
            # that have already committed; leave a visible, retryable partial
            # state instead.
            if provider_locally_applied:
                self._mark_relay_coordinated_applied("providers_models")
            if relay_locally_applied:
                self._mark_relay_coordinated_applied("relay_accounts")
            else:
                self._drafts["relay_accounts"]["dirty"] = True
                self._drafts["relay_accounts"]["validation"] = {"valid": True, "issues": []}
            pending = self._relay_operation_count(relay)
            self._revision += 1
            self._persist_metadata()
            self._emit()
            return {
                "revision": self._revision,
                "applied": False,
                "status": "partial",
                "domains": applied,
                "completed_operations": max(operation_total - pending, 0),
                "pending_operations": pending,
                "issues": self._relay_apply_issues(1),
            }

        self._emit()
        return {
            "revision": self._revision,
            "applied": True,
            "status": "applied",
            "domains": applied,
            "completed_operations": operation_total,
            "pending_operations": 0,
            "issues": [],
        }

    def apply(
        self,
        domain: str | None = None,
        *,
        domains: Sequence[str] | None = None,
        revision: int | None = None,
        confirmation: str | Sequence[str] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            # Refresh before accepting the caller revision or taking rollback
            # snapshots. A clean draft may reload here and advance revision;
            # a dirty draft records a conflict without touching the file.
            self._refresh_external_disk_state()
            self._check_revision(revision)
            if domain is not None and domains is not None:
                raise CoreError("invalid_domain", "Choose either one domain or a domain list")
            if domains is not None:
                if not isinstance(domains, Sequence) or isinstance(domains, (str, bytes, bytearray)) or not domains:
                    raise CoreError("invalid_domain", "Choose at least one settings domain")
                names = list(dict.fromkeys(_canonical_domain(name) for name in domains))
            else:
                names = [_canonical_domain(domain)] if domain is not None else [name for name, meta in self._drafts.items() if meta.get("dirty")]
            if not names:
                return {
                    "revision": self._revision,
                    "applied": True,
                    "status": "applied",
                    "domains": [],
                    "completed_operations": 0,
                    "pending_operations": 0,
                    "issues": [],
                }

            # A provider draft containing relay-sourced ProviderKeys must
            # resolve them against the relay domain in the same Apply.
            # Conversely, a relay draft that can affect those keys must bring
            # the provider domain into the coordinated transaction.
            # Independent providers keep the ordinary local-only path.
            relay_candidate = self._domains.get("relay_accounts")
            provider_candidate = self._domains.get("providers_models")
            relay_coordinator_available = callable(getattr(relay_candidate, "prepare_apply", None))
            relay_dependency_only = False
            dependency_summary = getattr(provider_candidate, "dependency_summary", None)
            linked_provider_key_count = 0
            if callable(dependency_summary):
                try:
                    dependency_state = dependency_summary()
                except Exception:
                    dependency_state = None
                if isinstance(dependency_state, Mapping):
                    count = dependency_state.get("provider_key_count")
                    if type(count) is int and count > 0:
                        linked_provider_key_count = count
            if relay_coordinator_available and linked_provider_key_count:
                if "providers_models" in names and "relay_accounts" not in names:
                    # Only a draft that actually binds relay material has relay
                    # work to do.  A plain local edit — a new model on an
                    # independent provider, a rename, an order — applies on its
                    # own: dragging it into the relay transaction would report
                    # the relay's own backlog (a pending key create, an expired
                    # session) as a failure of an edit that has nothing to do
                    # with the relay.
                    baseline = self._baselines.get("providers_models")
                    if baseline is None or _relay_binding_projection(
                        self._adapter_draft_state("providers_models")
                    ) != _relay_binding_projection(baseline):
                        names.append("relay_accounts")
                        # The relay is a *dependency* of this edit, not the
                        # user's own relay work: the edit needs the key it
                        # binds resolved and nothing more.  The station's
                        # session and its unfinished operations belong to the
                        # relay domain's own Apply, so this one never validates
                        # them, never runs the journal, and never reports the
                        # backlog against an edit that did not ask about it.
                        relay_dependency_only = True
                    else:
                        relay_coordinator_available = False
                elif "relay_accounts" in names and "providers_models" not in names:
                    names.append("providers_models")
            adapters: dict[str, DomainAdapter] = {}
            for name in names:
                adapter = self._domains.get(name)
                if adapter is None:
                    raise DomainNotFound(name)
                adapters[name] = adapter

            confirm_codes: list[str] = []
            if isinstance(confirmation, str):
                confirm_codes = [confirmation]
            elif isinstance(confirmation, Sequence) and not isinstance(confirmation, (bytes, bytearray, str)):
                confirm_codes = [str(item) for item in confirmation]
            overwrite_codes = {
                f"overwrite_external_{name}"
                for name in names
                if self._disk.get(name, {}).get("changed")
            }
            missing_overwrite = sorted(overwrite_codes.difference(confirm_codes))
            if missing_overwrite:
                # No persistence checkpoint or rollback is needed: nothing in
                # this Apply attempt has written to disk. In particular, never
                # restore an older checkpoint over the newly detected file.
                raise ConfirmationNeeded(missing_overwrite)

            if "relay_accounts" in adapters and relay_coordinator_available:
                return self._apply_relay_coordinated(
                    names,
                    adapters,
                    confirm_codes,
                    overwrite_codes,
                    relay_dependency_only=relay_dependency_only,
                )

            transaction_adapters = dict(adapters)
            adapter_checkpoints = {
                name: _checkpoint_adapter(adapter, error_code="apply_failed")
                for name, adapter in transaction_adapters.items()
            }
            core_checkpoint = {
                "revision": self._revision,
                "drafts": copy.deepcopy(self._drafts),
                "last_actions": copy.deepcopy(self._last_actions),
                "baselines": copy.deepcopy(self._baselines),
                "disk": copy.deepcopy(self._disk),
                "disk_identities": copy.deepcopy(self._disk_identities),
            }
            persistence_paths = [
                path
                for adapter in transaction_adapters.values()
                for path in _adapter_persistence_paths(adapter)
            ]
            if self._metadata_store is not None:
                persistence_paths.append(self._metadata_store.path)
            file_checkpoints = _checkpoint_files(persistence_paths)
            applied: list[str] = []
            try:
                for name in names:
                    if f"overwrite_external_{name}" not in overwrite_codes:
                        continue
                    rebase = getattr(adapters[name], "rebase_external_disk", None)
                    if not callable(rebase):
                        raise CoreError("apply_failed", "Settings changed on disk; choose the disk version")
                    rebase()
                    self._disk[name]["changed"] = False
                    self._disk[name]["keep_draft"] = True
                # Validation is complete before any persistence boundary is
                # crossed, so a bad later section cannot partially apply an
                # earlier one.
                for name in names:
                    if not self.validate(name)["valid"]:
                        raise CoreError("validation_failed", "Fix the validation errors")
                for name in names:
                    adapter = adapters[name]
                    domain_confirmations = list(confirm_codes)
                    if name == "claude" and any(
                        item in {"accepted", "claude-risk-confirmed"} for item in domain_confirmations
                    ):
                        # The UI confirmation acknowledges the complete risk
                        # summary. The Claude adapter still computes and checks
                        # the exact current codes.
                        try:
                            from .domains.claude import RISK_CONFIRMATION_CODES
                        except Exception:
                            RISK_CONFIRMATION_CODES = ()
                        domain_confirmations = [str(item) for item in RISK_CONFIRMATION_CODES]
                    payload: object | None = None
                    if domain_confirmations:
                        payload = {"confirm_risks": domain_confirmations, "confirmation": domain_confirmations}
                    if payload is None:
                        adapter.apply()
                    else:
                        adapter.apply(payload)
                    self._baselines[name] = self._adapter_draft_state(name)
                    self._disk[name] = {
                        "changed": False,
                        "generation": int(self._disk.get(name, {}).get("generation", 0)),
                        "keep_draft": False,
                    }
                    self._mark_domain(name, dirty=False, validation={"valid": True, "issues": []}, base_revision=self._revision + 1)
                    applied.append(name)
                if "providers_models" in applied or "runtime" in applied:
                    # The restart takes seconds; hand it to the background so
                    # the committed edit stays fast and the next request is
                    # not stuck behind the Core lock.
                    self._schedule_service_reload_after_apply()
                self._revision += 1
                self._persist_metadata()
            except Exception as exc:
                rollback_failed = False
                try:
                    _restore_files(file_checkpoints)
                    for name, adapter in transaction_adapters.items():
                        _restore_adapter(adapter, adapter_checkpoints[name])
                except Exception:
                    rollback_failed = True
                self._revision = int(core_checkpoint["revision"])
                self._drafts = core_checkpoint["drafts"]
                self._last_actions = core_checkpoint["last_actions"]
                self._baselines = core_checkpoint["baselines"]
                self._disk = core_checkpoint["disk"]
                self._disk_identities = core_checkpoint["disk_identities"]
                if rollback_failed:
                    raise CoreError("apply_failed", "Settings could not be rolled back") from None
                codes = getattr(exc, "codes", None)
                if isinstance(codes, Sequence) and not isinstance(codes, (str, bytes, bytearray)):
                    raise ConfirmationNeeded(str(item) for item in codes) from None
                if isinstance(exc, CoreError):
                    raise
                raise CoreError("apply_failed", safe_exception_message(exc)) from None
            self._emit()
            return {
                "revision": self._revision,
                "applied": True,
                "status": "applied",
                "domains": applied,
                "completed_operations": 0,
                "pending_operations": 0,
                "issues": [],
            }

    def reload(self, domain: str | None = None, *, revision: int | None = None) -> dict[str, Any]:
        with self._lock:
            self._check_revision(revision)
            names = [_canonical_domain(domain)] if domain is not None else list(self._domains)
            for name in names:
                adapter = self._domains.get(name)
                if adapter is None:
                    raise DomainNotFound(name)
                try:
                    adapter.reload()
                except Exception as exc:
                    raise CoreError("reload_failed", safe_exception_message(exc)) from None
                self._baselines[name] = self._adapter_draft_state(name)
                self._mark_domain(name, dirty=False, validation={"valid": True, "issues": []}, base_revision=self._revision + 1)
                self._disk[name] = {
                    "changed": False,
                    "generation": int(self._disk.get(name, {}).get("generation", 0)),
                    "keep_draft": False,
                }
            self._revision += 1
            self._persist_metadata()
            self._emit()
            return {"revision": self._revision}

    def probe(self, payload: Mapping[str, Any] | None = None, *, domain: str | None = None) -> dict[str, Any]:
        name = _canonical_domain(domain or "providers_models")
        data = dict(payload or {})
        with self._lock:
            adapter = self._domains.get(name)
            if adapter is None:
                return {"ok": False, "protocols": [], "detail": "Probe is unavailable"}
            prepare = getattr(adapter, "prepare_probe", None)
            perform = getattr(adapter, "perform_probe", None)
            commit = getattr(adapter, "commit_probe", None)
            if name == "providers_models" and all(callable(method) for method in (prepare, perform, commit)):
                try:
                    prepared = prepare(data)
                except Exception as exc:
                    return {"ok": False, "protocols": [], "detail": safe_exception_message(exc)}
            else:
                prepared = None

        if prepared is not None:
            try:
                value = perform(prepared)
            except Exception as exc:
                return {"ok": False, "protocols": [], "detail": safe_exception_message(exc)}
            with self._lock:
                if self._domains.get(name) is not adapter:
                    return {"ok": False, "protocols": [], "detail": "Probe is unavailable"}
                try:
                    value, changed = commit(prepared, value)
                except Exception as exc:
                    return {"ok": False, "protocols": [], "detail": safe_exception_message(exc)}
                if changed:
                    self._revision += 1
                    self._persist_metadata()
                    self._emit()
                result = _safe_public(value if isinstance(value, Mapping) else {})
                if not isinstance(result, dict):
                    result = {}
                result.setdefault("ok", False)
                result.setdefault("protocols", [])
                return result

        with self._lock:
            method = getattr(adapter, "probe", None)
            if not callable(method):
                return {"ok": False, "protocols": [], "detail": "Probe is unavailable"}
            before = self._adapter_draft_state(name)
            try:
                value = method(data)
            except Exception as exc:
                return {"ok": False, "protocols": [], "detail": safe_exception_message(exc)}
            after = self._adapter_draft_state(name)
            if after != before:
                dirty = after != self._baselines.get(name)
                self._revision += 1
                self._mark_domain(
                    name,
                    dirty=dirty,
                    base_revision=(
                        self._drafts.get(name, {}).get("base_revision", self._revision)
                        if dirty
                        else self._revision
                    ),
                )
                self._persist_metadata()
                self._emit()
            result = _safe_public(value if isinstance(value, Mapping) else {})
            if not isinstance(result, dict):
                result = {}
            result.setdefault("ok", False)
            result.setdefault("protocols", [])
            if name == "webdav":
                handler = self._service_handlers.get("status")
                if handler is not None:
                    try:
                        service_result = handler("status")
                    except Exception:
                        service_result = None
                    if isinstance(service_result, Mapping):
                        self._set_service_from_result(service_result, increment=False)
            return result

    def export(self, sections: Sequence[str], *, destination_token: str | None = None) -> dict[str, Any]:
        if isinstance(sections, (str, bytes, bytearray)) or not isinstance(sections, Sequence) or not sections:
            raise CoreError("invalid_sections", "Choose at least one configuration section")
        names = [_canonical_domain(section) for section in sections]
        if destination_token is not None and len(names) == 1 and names[0] in {"providers_models", "runtime"}:
            path = self.file_capabilities.resolve(destination_token, "export")
            name = names[0]
            try:
                adapter = self._domains.get(name)
                if adapter is None:
                    raise DomainNotFound(name)
                if any(_same_path(path, source) for source in _adapter_persistence_paths(adapter)):
                    raise CoreError("export_failed", "Choose a file outside the active settings files")
                method = getattr(adapter, "export", None)
                if not callable(method):
                    raise CoreError("export_failed", "The selected settings cannot be exported")
                parameters = inspect.signature(method).parameters
                exported = method(include_sensitive=True) if "include_sensitive" in parameters else method()
                if not isinstance(exported, Mapping):
                    raise CoreError("export_failed", "The selected settings cannot be exported")
                settings = copy.deepcopy(dict(exported))
                settings.pop("domain", None)
                atomic_write_json(
                    path,
                    {
                        "format": DOMAIN_FILE_FORMAT,
                        "version": DOMAIN_FILE_VERSION,
                        "domain": name,
                        "settings": settings,
                    },
                )
            except CoreError:
                raise
            except Exception as exc:
                raise CoreError("export_failed", safe_exception_message(exc)) from None
            return {
                "revision": self._revision,
                "section_count": 1,
                "sections": names,
            }
        if destination_token is not None and set(names).issubset({"providers_models", "runtime"}):
            path = self.file_capabilities.resolve(destination_token, "export")
            try:
                from .domains.providers_models import ProvidersModelsDomain
                from .domains.runtime import RuntimeSettingsDomain
                from .operations import ConfigurationPackageAdapter

                provider = self._domains.get("providers_models")
                runtime = self._domains.get("runtime")
                config_path = getattr(provider, "config_path", None)
                settings_path = getattr(runtime, "settings_path", None)
                if not isinstance(provider, ProvidersModelsDomain) or not isinstance(runtime, RuntimeSettingsDomain):
                    raise CoreError("export_failed", "Configuration package sources are unavailable")
                adapter = ConfigurationPackageAdapter(
                    config_path=Path(config_path),
                    settings_path=Path(settings_path),
                )
                written = adapter.export(sections=names, destination=path)
            except CoreError:
                raise
            except Exception as exc:
                raise CoreError("export_failed", safe_exception_message(exc)) from None
            return {
                "revision": self._revision,
                "section_count": len(written),
                "sections": names,
            }
        package_sections: dict[str, Any] = {}
        safe_package_sections: dict[str, Any] = {}
        with self._lock:
            for name in names:
                adapter = self._domains.get(name)
                if adapter is None:
                    raise DomainNotFound(name)
                method = getattr(adapter, "export", None)
                try:
                    if callable(method):
                        # A trusted, explicitly selected destination may carry
                        # credentials, but the response sent back to RN must
                        # remain redacted.  Domain adapters that support this
                        # opt in with ``include_sensitive=True``; other
                        # adapters keep their safe no-argument export.
                        try:
                            parameters = inspect.signature(method).parameters
                        except (TypeError, ValueError):
                            parameters = {}
                        value = method(include_sensitive=True) if "include_sensitive" in parameters else method()
                    else:
                        value = adapter.snapshot()
                except Exception as exc:
                    raise CoreError("export_failed", safe_exception_message(exc)) from None
                package_sections[name] = copy.deepcopy(value)
                safe_package_sections[name] = _safe_public(value)
            package = {"format": PACKAGE_FORMAT, "version": PACKAGE_VERSION, "sections": package_sections}
            safe_package = {"format": PACKAGE_FORMAT, "version": PACKAGE_VERSION, "sections": safe_package_sections}
            result: dict[str, Any] = {"revision": self._revision, "section_count": len(package_sections), "sections": list(package_sections)}
            if destination_token is not None:
                path = self.file_capabilities.resolve(destination_token, "export")
                try:
                    AtomicJSONStore(path).write(package)
                except PersistenceError as exc:
                    raise CoreError("export_failed", safe_exception_message(exc)) from None
            # ``package`` is retained for previews/tests, but never contains
            # the raw package values that may have been written to disk.
            result["package"] = safe_package
            return result

    def _read_import_package(self, path: Path) -> Mapping[str, Any]:
        """Parse one selected file without using any preselected UI sections."""

        try:
            from .operations import ConfigurationPackageAdapter

            try:
                selected_json: dict[str, Any] | None = read_json(path)
            except PersistenceError:
                selected_json = None

            if selected_json is not None and selected_json.get("format") == DOMAIN_FILE_FORMAT:
                if set(selected_json) != {"format", "version", "domain", "settings"}:
                    raise CoreError("invalid_package", "Settings file has an unsupported shape")
                name = _canonical_domain(selected_json.get("domain"))
                if (
                    selected_json.get("version") != DOMAIN_FILE_VERSION
                    or name not in {"providers_models", "runtime"}
                    or not isinstance(selected_json.get("settings"), Mapping)
                ):
                    raise CoreError("invalid_package", "Settings file version is unsupported")
                return {
                    "format": PACKAGE_FORMAT,
                    "version": PACKAGE_VERSION,
                    "sections": {name: copy.deepcopy(dict(selected_json["settings"]))},
                }

            if selected_json is not None and selected_json.get("format") == PACKAGE_FORMAT:
                return selected_json

            if (
                selected_json is not None
                and selected_json.get("format") == "young-router-configuration-package"
            ):
                from .domains.providers_models import ProvidersModelsDomain
                from .domains.runtime import RuntimeSettingsDomain

                provider = self._domains.get("providers_models")
                runtime = self._domains.get("runtime")
                if not isinstance(provider, ProvidersModelsDomain) or not isinstance(runtime, RuntimeSettingsDomain):
                    raise CoreError("invalid_package", "Configuration package sources are unavailable")
                adapter = ConfigurationPackageAdapter(
                    config_path=Path(provider.config_path),
                    settings_path=Path(runtime.settings_path),
                )
                loaded = adapter.load(path)
                return {
                    "format": PACKAGE_FORMAT,
                    "version": PACKAGE_VERSION,
                    "sections": ConfigurationPackageAdapter.core_sections(loaded, None),
                }

            # All other supported JSON/TOML/YAML/SQL shapes are existing
            # provider/model imports. Detection happens only after the native
            # picker has returned the file; no checkbox hint influences it.
            import external_provider_import

            imported = external_provider_import.import_explicit(path)
            providers = imported.get("providers") if isinstance(imported, Mapping) else None
            if not isinstance(providers, list):
                raise CoreError("invalid_package", "Provider configuration could not be imported")
            return {
                "format": PACKAGE_FORMAT,
                "version": PACKAGE_VERSION,
                "sections": {"providers_models": {"providers": copy.deepcopy(providers)}},
            }
        except CoreError:
            raise
        except (PersistenceError, ValueError) as exc:
            raise CoreError("invalid_package", safe_exception_message(exc)) from None
        except Exception as exc:
            raise CoreError("invalid_package", safe_exception_message(exc)) from None

    def _validated_import_package(
        self, package: Mapping[str, Any]
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        if not isinstance(package, Mapping) or set(package) != {"format", "version", "sections"}:
            raise CoreError("invalid_package", "Configuration package has an unsupported shape")
        if (
            package.get("format") != PACKAGE_FORMAT
            or package.get("version") != PACKAGE_VERSION
            or not isinstance(package.get("sections"), Mapping)
        ):
            raise CoreError("invalid_package", "Configuration package version is unsupported")
        raw_sections = package["sections"]
        if not raw_sections:
            raise CoreError("invalid_package", "Configuration package does not contain settings")
        normalized: dict[str, Any] = {}
        for raw_name, payload in raw_sections.items():
            name = _canonical_domain(raw_name)
            if name != raw_name or name not in IMPORTABLE_DOMAINS:
                raise CoreError("invalid_package", "Configuration package contains an unsupported section")
            if name in normalized:
                raise CoreError("invalid_package", "Configuration package contains a repeated section")
            if self._domains.get(name) is None:
                raise DomainNotFound(name)
            normalized[name] = copy.deepcopy(payload)
        return (
            {"format": PACKAGE_FORMAT, "version": PACKAGE_VERSION, "sections": normalized},
            tuple(normalized),
        )

    def prepare_import(
        self,
        *,
        source_token: str,
        revision: int | None = None,
    ) -> PreparedImport:
        """Consume a picker capability and prepare a non-mutating import plan."""

        with self._lock:
            self._check_revision(revision)
        path = self.file_capabilities.resolve(source_token, "import")
        package, detected = self._validated_import_package(self._read_import_package(path))
        with self._lock:
            # Parsing may be expensive. Bind the plan to the exact state whose
            # existing drafts are described by this preview.
            self._check_revision(revision)
            preview = {
                name: {
                    "available": True,
                    "will_replace_draft": bool(self._drafts.get(name, {}).get("dirty")),
                }
                for name in detected
            }
            return PreparedImport(
                package=package,
                detected_sections=detected,
                preview=preview,
                revision=self._revision,
            )

    def import_package(
        self,
        *,
        package: Mapping[str, Any],
        sections: Sequence[str] | None = None,
        revision: int | None = None,
    ) -> dict[str, Any]:
        """Stage a package previously parsed by :meth:`prepare_import`.

        File capabilities are accepted only by ``prepare_import``.  Import
        execution receives the parsed package held by the session-bound IPC
        plan, so a caller cannot bypass preview detection or submit another
        path at staging time.
        """

        package, detected = self._validated_import_package(package)
        if sections is None:
            selected = list(detected)
        else:
            if isinstance(sections, (str, bytes, bytearray)) or not isinstance(sections, Sequence) or not sections:
                raise CoreError("invalid_sections", "Choose at least one detected configuration section")
            requested = list(dict.fromkeys(_canonical_domain(section) for section in sections))
            if len(requested) != len(sections):
                raise CoreError("invalid_sections", "Choose only sections detected in the selected file")
            if not set(requested).issubset(detected):
                raise CoreError("invalid_sections", "Choose only sections detected in the selected file")
            selected = requested
        raw_sections = package["sections"]
        with self._lock:
            self._check_revision(revision)
            pending: list[tuple[str, DomainAdapter, object]] = []
            preview: dict[str, dict[str, bool]] = {}
            for name in selected:
                adapter = self._domains.get(name)
                if adapter is None:
                    raise DomainNotFound(name)
                pending.append((name, adapter, raw_sections[name]))
                preview[name] = {
                    "available": True,
                    "will_replace_draft": bool(self._drafts.get(name, {}).get("dirty")),
                }

            adapter_checkpoints = {
                name: _checkpoint_adapter(adapter)
                for name, adapter, _payload in pending
            }
            core_checkpoint = {
                "revision": self._revision,
                "drafts": copy.deepcopy(self._drafts),
                "last_actions": copy.deepcopy(self._last_actions),
                "baselines": copy.deepcopy(self._baselines),
            }
            staged: list[str] = []
            importing = True
            try:
                for name, adapter, payload in pending:
                    importer = getattr(adapter, "import_package", None)
                    if callable(importer):
                        importer(payload)
                    elif name == "claude" and isinstance(payload, Mapping) and isinstance(payload.get("settings"), Mapping):
                        adapter.dispatch("patch", payload["settings"])
                    elif name == "language" and isinstance(payload, Mapping):
                        adapter.dispatch("set", {"language": payload.get("choice", payload.get("language"))})
                    else:
                        adapter.dispatch("replace", payload)
                    staged.append(name)
                importing = False
                for name in staged:
                    dirty = self._adapter_draft_state(name) != self._baselines.get(name)
                    self._mark_domain(
                        name,
                        dirty=dirty,
                        base_revision=(
                            self._drafts.get(name, {}).get("base_revision", self._revision)
                            if dirty
                            else self._revision
                        ),
                    )
                self._revision += 1
                self._persist_metadata()
            except Exception as exc:
                for name, adapter, _payload in pending:
                    _restore_adapter(adapter, adapter_checkpoints[name])
                self._revision = int(core_checkpoint["revision"])
                self._drafts = core_checkpoint["drafts"]
                self._last_actions = core_checkpoint["last_actions"]
                self._baselines = core_checkpoint["baselines"]
                if importing:
                    raise CoreError("import_failed", safe_exception_message(exc)) from None
                raise

            self._emit()
            return {"revision": self._revision, "draft_domains": staged, "preview": preview}

    def set_service_status(
        self,
        state: str,
        *,
        detail: str | None = None,
        pid: int | None = None,
        port: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if state not in SERVICE_STATES:
                raise CoreError("invalid_service_state", "Service state is invalid")
            self._service = {"state": state}
            if detail:
                self._service["detail"] = safe_error_message(detail)
            if type(pid) is int and pid > 0:
                self._service["pid"] = pid
            if state == "running" and type(port) is int and 1 <= port <= 65535:
                self._service["port"] = port
            self._revision += 1
            self._persist_metadata()
            self._emit()
            return copy.deepcopy(self._service)

    def set_log_summary(self, tab: str, *, available: bool, paused: bool = False, line_count: int = 0) -> None:
        if tab not in LOG_TABS:
            raise CoreError("invalid_log_tab", "Log tab is invalid")
        with self._lock:
            self._logs[tab] = {
                "tab": tab,
                "available": bool(available),
                "paused": bool(paused),
                "line_count": max(0, int(line_count)),
            }
            self._revision += 1
            self._emit()

__all__ = [
    "CORE_METADATA_VERSION",
    "CoreError",
    "CoreStore",
    "ConfirmationNeeded",
    "DomainAdapter",
    "DomainNotFound",
    "FileCapability",
    "FileCapabilityRegistry",
    "LOG_TABS",
    "MemoryDomain",
    "PACKAGE_FORMAT",
    "PACKAGE_VERSION",
    "RevisionConflict",
    "SERVICE_STATES",
]
