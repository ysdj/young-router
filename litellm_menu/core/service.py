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
from dataclasses import dataclass
import inspect
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from .persistence import AtomicJSONStore, PersistenceError, atomic_write_bytes, atomic_write_json, read_bytes, read_json
from .protocol import PROTOCOL_VERSION, ProtocolError, make_event
from .security import REDACTED, redact, safe_exception_message, safe_error_message


CORE_METADATA_VERSION = 1
STATE_FILE_NAME = "core-state.json"
PACKAGE_FORMAT = "litellm-menu-core-package"
PACKAGE_VERSION = 1
DOMAIN_FILE_FORMAT = "litellm-menu-domain-settings"
DOMAIN_FILE_VERSION = 1
SUBSCRIPTION_QUEUE_LIMIT = 32

_SECRET_FIELDS: dict[tuple[str, str], bool] = {
    ("providers_models", "api_key"): True,
    ("codex", "api_key"): False,
    ("claude", "deployment_token"): False,
    ("claude", "auto_memory_directory"): False,
    ("runtime", "setting"): True,
    ("webdav", "password"): False,
}

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
LOG_TABS = (
    "requests",
    "service",
    "menu",
    "route-trace",
    "recovery",
    "online-usage",
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
        super().__init__("revision_conflict", "The Core state changed; reload before applying")


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


def _as_mapping(value: object, label: str = "payload") -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CoreError("invalid_payload", f"{label} must be an object")
    return dict(value)


def _validation_summary(value: object) -> dict[str, Any]:
    """Normalize legacy adapter validation into the shared TS shape."""

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


def _checkpoint_adapter(adapter: DomainAdapter, *, error_code: str = "import_failed") -> dict[str, Any]:
    """Capture one adapter before a transaction mutates it."""

    state = getattr(adapter, "__dict__", None)
    if not isinstance(state, dict):
        raise CoreError(error_code, "Settings could not be prepared for a transaction")
    try:
        return copy.deepcopy(state)
    except Exception:
        raise CoreError(error_code, "Settings could not be prepared for a transaction") from None


def _restore_adapter(adapter: DomainAdapter, checkpoint: Mapping[str, Any]) -> None:
    """Restore the same adapter instance so no caller observes partial state."""

    state = getattr(adapter, "__dict__", None)
    if not isinstance(state, dict):
        raise RuntimeError("Configuration package adapter cannot be restored")
    state.clear()
    state.update(checkpoint)


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
    for attribute in ("config_path", "runtime_config_path", "settings_path", "preference_path", "enabled_path", "status_path"):
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

    def __init__(
        self,
        *,
        metadata_path: Path | str | None = None,
        domains: Iterable[DomainAdapter] = (),
        file_capabilities: FileCapabilityRegistry | None = None,
        service_handlers: Mapping[str, Callable[[str], object]] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._metadata_store = AtomicJSONStore(metadata_path) if metadata_path else None
        self.file_capabilities = file_capabilities or FileCapabilityRegistry()
        self._service_handlers = dict(service_handlers or {})
        self._shutdown_handler = self._service_handlers.get("stop")
        self._domains: dict[str, DomainAdapter] = {}
        self._baselines: dict[str, object] = {}
        self._drafts: dict[str, dict[str, Any]] = {}
        self._subscribers: dict[str, Callable[[dict[str, Any]], None]] = {}
        self._revision = 0
        self._service: dict[str, Any] = {"state": "unknown"}
        self._last_actions: dict[str, dict[str, Any]] = {}
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
    ) -> "CoreStore":
        """Construct production domains without importing the LiteLLM proxy.

        Existing Python modules remain the only parser and persistence layer
        for providers, Codex, runtime settings, and WebDAV.  A malformed
        optional user file degrades only that route during bootstrap so the
        host can still open the remaining settings windows.
        """

        adapters: list[DomainAdapter] = []
        try:
            from .domains.codex import CodexSettingsDomain
            from .domains.providers_models import ProvidersModelsDomain
            from .domains.runtime import RuntimeSettingsDomain
            from .domains.webdav import WebDAVSettingsDomain

            legacy_factories: tuple[tuple[str, Callable[[], DomainAdapter]], ...] = (
                ("providers_models", lambda: ProvidersModelsDomain(config_path)),
                ("codex", lambda: CodexSettingsDomain(config_path, codex_home=codex_home)),
                ("runtime", lambda: RuntimeSettingsDomain(runtime_settings_path)),
                (
                    "webdav",
                    lambda: WebDAVSettingsDomain(
                        webdav_settings_path,
                        enabled_path=webdav_enabled_path,
                    ),
                ),
            )
        except Exception:
            legacy_factories = ()
        loaded_legacy: set[str] = set()
        for name, factory in legacy_factories:
            try:
                adapters.append(factory())
            except Exception:
                # Never claim Apply succeeded against a placeholder when the
                # user's real source is malformed.
                adapters.append(UnavailableDomain(name))
            loaded_legacy.add(name)
        for name in ("providers_models", "codex", "runtime", "webdav"):
            if name not in loaded_legacy:
                adapters.append(MemoryDomain(name, _default_domain_state(name)))
        adapters.append(MemoryDomain("logs", _default_domain_state("logs")))
        try:
            from .domains.claude import ClaudeSettingsDomain

            claude_factory: Callable[[], DomainAdapter] = lambda: ClaudeSettingsDomain(claude_settings_path)
            try:
                adapters.append(claude_factory())
            except Exception:
                adapters.append(RecoverableDomain("claude", claude_factory))
        except Exception:
            adapters.append(UnavailableDomain("claude"))
        try:
            from .domains.logs import LogsDomain

            adapters = [adapter for adapter in adapters if getattr(adapter, "name", "") != "logs"]
            adapters.append(
                LogsDomain(
                    runtime_root,
                    config_path=config_path,
                    runtime_settings_path=runtime_settings_path,
                )
            )
        except Exception:
            # A log directory can be unavailable during early startup; retain
            # the neutral summary adapter so the rest of the UI still opens.
            pass
        try:
            from .domains.language import LanguageSettingsDomain

            language_factory: Callable[[], DomainAdapter] = lambda: LanguageSettingsDomain(language_path)
            try:
                adapters.append(language_factory())
            except Exception:
                adapters.append(RecoverableDomain("language", language_factory))
        except Exception:
            adapters.append(UnavailableDomain("language"))
        try:
            from .domains.relay_accounts import RelayAccountsDomain

            adapters.append(RelayAccountsDomain(runtime_root))
        except Exception:
            adapters.append(UnavailableDomain("relay_accounts"))
        from .operations import CoreServiceController

        controller = CoreServiceController(runtime_root)
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
        if initial_service.get("state") == "stopped":
            # Core establishes the proxy side of the lifecycle unit; it does
            # not expose IPC while the configured 4000 service is absent.
            initial_service = controller.start()
        if initial_service.get("state") != "running":
            raise RuntimeError("LiteLLM service is unavailable")
        store = cls(metadata_path=metadata_path, domains=adapters, service_handlers=service_handlers)
        if isinstance(initial_service, Mapping):
            store._set_service_from_result(initial_service, increment=False)
        return store

    def shutdown(self) -> dict[str, Any]:
        """Stop only the proxy owned by this Core before the native host exits."""

        with self._lock:
            handler = self._shutdown_handler
            if handler is None:
                self._service = {"state": "stopped"}
                return dict(self._service)
            try:
                result = handler("stop")
            except Exception as exc:
                raise CoreError("service_error", safe_exception_message(exc)) from None
            if not isinstance(result, Mapping):
                raise CoreError("service_error", "LiteLLM service returned invalid status")
            self._set_service_from_result(result, increment=False)
            self._persist_metadata()
            return dict(self._service)

    def _load_metadata(self) -> None:
        assert self._metadata_store is not None
        try:
            payload = self._metadata_store.read(default={})
        except PersistenceError as exc:
            raise CoreError("state_unavailable", safe_exception_message(exc)) from None
        if not payload:
            return
        if payload.get("version") != CORE_METADATA_VERSION:
            raise CoreError("state_unavailable", "Core state version is unsupported")
        revision = payload.get("revision", 0)
        if type(revision) is not int or revision < 0:
            raise CoreError("state_unavailable", "Core state is invalid")
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
            status_handler = self._service_handlers.get("status")
            if status_handler is not None:
                result = status_handler("status")
                if not isinstance(result, Mapping):
                    raise CoreError("service_error", "LiteLLM service returned invalid status")
                self._set_service_from_result(result, increment=False)
            self._refresh_external_disk_state()
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

    def _refresh_external_disk_state(self) -> None:
        """Auto-reload clean drafts and expose only sanitized conflict state."""

        for name, adapter in self._domains.items():
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
        name = _canonical_domain(domain)
        if name not in {"codex", "claude"}:
            raise CoreError("invalid_editor", "The requested editor is unavailable")
        if document not in {"config", "auth", "settings"}:
            raise CoreError("invalid_editor", "The requested editor is unavailable")
        if (name == "codex" and document not in {"config", "auth"}) or (name == "claude" and document != "settings"):
            raise CoreError("invalid_editor", "The requested editor is unavailable")
        adapter = self._domains.get(name)
        if adapter is None:
            raise DomainNotFound(name)
        return name, adapter

    def editor_descriptor(self, domain: str, document: str) -> dict[str, Any]:
        """Describe a native editor without returning its sensitive text."""

        with self._lock:
            name, _adapter = self._editor_adapter(domain, document)
            return {"domain": name, "document": document, "revision": self._revision}

    def trusted_editor_text(self, domain: str, document: str, *, revision: int) -> str:
        """Read raw text only for the authenticated native-host editor path."""

        with self._lock:
            self._check_revision(revision)
            name, adapter = self._editor_adapter(domain, document)
            try:
                if name == "codex":
                    exporter = getattr(adapter, "export", None)
                    if not callable(exporter):
                        raise CoreError("invalid_editor", "The requested editor is unavailable")
                    raw = exporter(include_sensitive=True)
                    key = "config_text" if document == "config" else "auth_text"
                    text = raw.get(key) if isinstance(raw, Mapping) else None
                else:
                    raw_text = getattr(adapter, "raw_text", None)
                    text = raw_text(include_sensitive=True) if callable(raw_text) else None
            except CoreError:
                raise
            except Exception as exc:
                raise CoreError("editor_unavailable", safe_exception_message(exc)) from None
            if not isinstance(text, str) or len(text.encode("utf-8")) > 2 * 1024 * 1024:
                raise CoreError("editor_unavailable", "The requested editor is unavailable")
            return text

    def stage_editor_text(self, domain: str, document: str, text: str, *, revision: int) -> dict[str, Any]:
        """Stage one native-editor result without exposing it to React."""

        if not isinstance(text, str) or len(text.encode("utf-8")) > 2 * 1024 * 1024:
            raise CoreError("invalid_editor", "The editor document is invalid")
        name, _adapter = self._editor_adapter(domain, document)
        payload: dict[str, Any]
        if name == "codex":
            payload = {"document": document, "text": text}
        else:
            payload = {"raw_json": text}
        return self.dispatch(
            {"domain": name, "type": "set_raw", "payload": payload},
            expected_revision=revision,
            _trusted_native_capability=True,
        )

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
        """Validate the sole plaintext-native secret slot.

        Only ``providers_models/api_key`` can be read back, and only through
        the Core IPC server's short-lived native read capability.  It is never
        included in snapshots or the normal React request protocol.
        """

        with self._lock:
            name = _canonical_domain(domain)
            field_name = field.strip() if isinstance(field, str) else ""
            if (name, field_name) != ("providers_models", "api_key"):
                raise CoreError("invalid_secret", "The requested secret field is unavailable")
            if not isinstance(target, str) or not target or len(target.encode("utf-8")) > 256:
                raise CoreError("invalid_secret", "The requested secret field is unavailable")
            adapter = self._domains.get(name)
            reader = getattr(adapter, "trusted_secret_value", None) if adapter is not None else None
            if not callable(reader):
                raise CoreError("invalid_secret", "The requested secret field is unavailable")
            try:
                value = reader(field_name, target)
            except Exception:
                raise CoreError("invalid_secret", "The requested secret field is unavailable") from None
            if (
                not isinstance(value, str)
                or len(value.encode("utf-8")) > 16_384
                or any(character in value for character in "\x00\r\n")
            ):
                raise CoreError("invalid_secret", "The requested secret field is unavailable")
            return {
                "domain": name,
                "field": field_name,
                "target": target,
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
        """Read a provider API key via an already-authorized native lease."""

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
                or len(value.encode("utf-8")) > 16_384
                or any(character in value for character in "\x00\r\n")
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

        if not isinstance(value, str) or len(value.encode("utf-8")) > 16_384 or any(char in value for char in "\x00\r\n"):
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
    ) -> dict[str, Any]:
        """Stage only the API resources explicitly selected by the user."""

        with self._lock:
            self._check_revision(revision)
            relay = self._domains.get("relay_accounts")
            providers = self._domains.get("providers_models")
            importer = getattr(relay, "import_resources", None) if relay is not None else None
            if not isinstance(account_id, str) or not account_id or not callable(importer) or providers is None:
                raise CoreError("relay_import_failed", "Relay account is unavailable")
            provider_checkpoint = _checkpoint_adapter(providers, error_code="relay_import_failed")
            try:
                result = importer(account_id, resource_ids, providers)
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
                "resource_count": int(result.get("resource_count", 0)) if isinstance(result, Mapping) else 0,
                "model_count": int(result.get("model_count", 0)) if isinstance(result, Mapping) else 0,
            }
            self._persist_metadata()
            self._emit()
            return {
                "revision": self._revision,
                "imported": True,
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
    ) -> dict[str, Any]:
        """Accept one native browser session and expose only public account state."""

        known_secrets = tuple(
            value for value in (cookie, access_token, refresh_token) if isinstance(value, str) and value
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
                )
                # Login is the durable browser-session boundary. Resource
                # discovery is a separate explicit action so a station API
                # outage cannot change the result of a successful sign-in.
                self._revision += 1
                self._baselines["relay_accounts"] = self._adapter_draft_state("relay_accounts")
                self._mark_domain(
                    "relay_accounts",
                    dirty=False,
                    validation={"valid": True, "issues": []},
                    base_revision=self._revision,
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
            try:
                result = refresher(account_id)
            except Exception as exc:
                raise CoreError("relay_resources_failed", safe_exception_message(exc)) from None
            resources = result.get("resources", []) if isinstance(result, Mapping) else []
            resource_status = result.get("resource_status", "unavailable") if isinstance(result, Mapping) else "unavailable"
            resource_count = len(resources) if isinstance(resources, list) else 0
            self._revision += 1
            self._baselines["relay_accounts"] = self._adapter_draft_state("relay_accounts")
            self._mark_domain(
                "relay_accounts",
                dirty=False,
                validation={"valid": True, "issues": []},
                base_revision=self._revision,
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
            if not callable(accepter) or not callable(status_setter):
                raise CoreError("relay_restore_failed", "Relay account is unavailable")
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
                    )
                elif login_status in {"signed_out", "expired"}:
                    public = status_setter(str(account_id), str(login_status))
                else:
                    raise CoreError("relay_restore_failed", "Relay login status is invalid")
                self._revision += 1
                self._baselines["relay_accounts"] = self._adapter_draft_state("relay_accounts")
                self._mark_domain(
                    "relay_accounts",
                    dirty=False,
                    validation={"valid": True, "issues": []},
                    base_revision=self._revision,
                )
                self._last_actions["relay_accounts"] = {
                    "session_restored": login_status == "signed_in",
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
        def optional_text(value: object) -> str | None:
            projected = _safe_public(value)
            if projected in (None, "", [], {}):
                return None
            if isinstance(projected, str):
                return safe_error_message(projected)
            if isinstance(projected, (int, float)) and not isinstance(projected, bool):
                return str(projected)
            if isinstance(projected, (Mapping, Sequence)) and not isinstance(projected, (str, bytes, bytearray)):
                try:
                    return safe_error_message(
                        json.dumps(projected, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
                    )
                except (TypeError, ValueError):
                    return None
            return None

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
                    order_value = model.get("order", 1)
                    try:
                        order = int(order_value) if not isinstance(order_value, bool) else 1
                    except (TypeError, ValueError, OverflowError):
                        order = 1
                    summary = {
                        "id": str(model_id),
                        "display_name": str(model_display_name),
                        "public_model": str(public_model),
                        "upstream_model": str(upstream_model),
                        "enabled": model.get("model_enabled", model.get("enabled", True)) is not False,
                        "order": order,
                    }
                    for optional_key in ("billing", "multiplier"):
                        optional_value = optional_text(model.get(optional_key))
                        if optional_value is not None:
                            summary[optional_key] = optional_value
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
        if not _trusted_native_capability:
            self.reject_plaintext_secret_action(data)
        with self._lock:
            self._check_revision(expected_revision if expected_revision is not None else data.get("expected_revision"))
            previous_revision = self._revision
            action_type = data.get("type", data.get("action"))
            if not isinstance(action_type, str) or not action_type.strip():
                raise CoreError("invalid_action", "A Core action is required")
            domain_value = data.get("domain")
            if domain_value is None and action_type.startswith("service."):
                result = self._dispatch_service(action_type, _as_mapping(data.get("payload")))
            else:
                name = _canonical_domain(domain_value)
                adapter = self._domains.get(name)
                if adapter is None:
                    raise DomainNotFound(name)
                before = copy.deepcopy(self._baselines.get(name))
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
                normalized_action = action_type.replace("-", "_").replace(".", "_")
                if name == "relay_accounts" and normalized_action in {
                    "resources_import",
                    "relay_resources_import",
                    "account_resources_import",
                }:
                    resource_data = _as_mapping(payload)
                    return self.import_relay_resources(
                        resource_data.get("id", resource_data.get("account_id")),
                        resource_data.get("resource_ids"),
                        revision=self._revision,
                    )
                if name == "relay_accounts" and normalized_action in {
                    "resources_refresh",
                    "relay_resources_refresh",
                    "account_resources_refresh",
                }:
                    resource_data = _as_mapping(payload)
                    return self.refresh_relay_resources(
                        resource_data.get("id", resource_data.get("account_id")),
                        revision=self._revision,
                    )
                try:
                    result = adapter.dispatch(action_type, payload)
                except ConfirmationNeeded:
                    raise
                except Exception as exc:
                    # Domain exceptions are authored to be safe, but still
                    # normalize a third-party/legacy exception defensively.
                    raise CoreError("domain_error", safe_exception_message(exc)) from None
                after = self._adapter_draft_state(name)
                if isinstance(result, Mapping):
                    safe_result = _safe_public(result)
                    if isinstance(safe_result, Mapping):
                        self._last_actions[name] = dict(safe_result)
                dirty = after != before
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
            if self._revision != previous_revision:
                self._emit()
            return {"revision": self._revision}

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
            forbidden = {"api_key", "api_keys"}
            if normalized in {"provider_clear_key", "clear_provider_key", "set_raw", "setraw"}:
                raise CoreError("secret_requires_native", "Use the native secure input for this secret field")
        elif name == "codex":
            forbidden = {"api_key", "auth_text", "raw_json"}
            if normalized in {"set_raw", "setraw"}:
                raise CoreError("secret_requires_native", "Use the native secure editor for this document")
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
                raise CoreError("secret_requires_native", "Use the native secure editor for this document")
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
                raise CoreError("secret_requires_native", "Use the native secure editor for this document")
        if forbidden and _mapping_contains_key(payload, forbidden):
            raise CoreError("secret_requires_native", "Use the native secure input for this secret field")

    def _dispatch_service(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        operation = action.removeprefix("service.")
        previous_service = copy.deepcopy(self._service)
        handler = self._service_handlers.get(operation)
        if handler is not None:
            try:
                result = handler(operation)
            except Exception as exc:
                raise CoreError("service_error", safe_exception_message(exc)) from None
            if isinstance(result, Mapping):
                self._set_service_from_result(result, increment=False)
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
        # Health polling is deliberately frequent.  A probe which projects the
        # same public status is not a state transition, so do not make every
        # settings surface redraw just to carry an identical service snapshot.
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
                return {"revision": self._revision, "applied": True, "domains": []}
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

            adapter_checkpoints = {name: _checkpoint_adapter(adapter, error_code="apply_failed") for name, adapter in adapters.items()}
            core_checkpoint = {
                "revision": self._revision,
                "drafts": copy.deepcopy(self._drafts),
                "last_actions": copy.deepcopy(self._last_actions),
                "baselines": copy.deepcopy(self._baselines),
                "disk": copy.deepcopy(self._disk),
                "disk_identities": copy.deepcopy(self._disk_identities),
            }
            persistence_paths = [path for adapter in adapters.values() for path in _adapter_persistence_paths(adapter)]
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
                        raise CoreError("validation_failed", "Fix validation errors before applying")
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
                self._revision += 1
                self._persist_metadata()
            except Exception as exc:
                rollback_failed = False
                try:
                    _restore_files(file_checkpoints)
                    for name, adapter in adapters.items():
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
            return {"revision": self._revision, "applied": True, "domains": applied}

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
        name = _canonical_domain(domain or "providers-models")
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
                        # opt in with ``include_sensitive=True``; legacy
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

    def import_package(
        self,
        *,
        source_token: str | None = None,
        package: Mapping[str, Any] | None = None,
        sections: Sequence[str] | None = None,
        revision: int | None = None,
    ) -> dict[str, Any]:
        # Reject an already-stale picker result before consuming its opaque
        # capability.  The check under the staging lock below closes the race
        # while the selected package is read and parsed.
        with self._lock:
            self._check_revision(revision)
        if package is None:
            if source_token is None:
                raise CoreError("invalid_package", "A configuration package is required")
            path = self.file_capabilities.resolve(source_token, "import")
            try:
                from .operations import ConfigurationPackageAdapter

                provider = self._domains.get("providers_models")
                runtime = self._domains.get("runtime")
                config_path = getattr(provider, "config_path", None)
                settings_path = getattr(runtime, "settings_path", None)
                requested = None if sections is None else [_canonical_domain(section) for section in sections]
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
                    if requested is not None and requested != [name]:
                        raise CoreError("invalid_package", "Settings file does not contain the selected section")
                    package = {
                        "format": PACKAGE_FORMAT,
                        "version": PACKAGE_VERSION,
                        "sections": {name: copy.deepcopy(dict(selected_json["settings"]))},
                    }
                elif selected_json is not None and selected_json.get("format") == PACKAGE_FORMAT:
                    package = selected_json
                elif requested == ["providers_models"]:
                    import external_provider_import

                    imported = external_provider_import.import_explicit(path)
                    providers = imported.get("providers") if isinstance(imported, Mapping) else None
                    if not isinstance(providers, list):
                        raise CoreError("invalid_package", "Provider configuration could not be imported")
                    package = {
                        "format": PACKAGE_FORMAT,
                        "version": PACKAGE_VERSION,
                        "sections": {"providers_models": {"providers": copy.deepcopy(providers)}},
                    }
                elif requested == ["runtime"]:
                    raise CoreError("invalid_package", "Runtime settings import requires a Runtime Settings JSON file")
                elif config_path is not None and settings_path is not None:
                    adapter = ConfigurationPackageAdapter(
                        config_path=Path(config_path),
                        settings_path=Path(settings_path),
                    )
                    loaded = adapter.load(path)
                    package_sections = adapter.core_sections(loaded, sections)
                    package = {
                        "format": PACKAGE_FORMAT,
                        "version": PACKAGE_VERSION,
                        "sections": package_sections,
                    }
                else:
                    package = selected_json if selected_json is not None else read_json(path)
            except (PersistenceError, ValueError) as exc:
                raise CoreError("invalid_package", safe_exception_message(exc)) from None
            except Exception as exc:
                raise CoreError("invalid_package", safe_exception_message(exc)) from None
        if not isinstance(package, Mapping) or set(package) != {"format", "version", "sections"}:
            raise CoreError("invalid_package", "Configuration package has an unsupported shape")
        if package.get("format") != PACKAGE_FORMAT or package.get("version") != PACKAGE_VERSION or not isinstance(package.get("sections"), Mapping):
            raise CoreError("invalid_package", "Configuration package version is unsupported")
        raw_sections = package["sections"]
        selected = list(raw_sections) if sections is None else [_canonical_domain(section) for section in sections]
        with self._lock:
            self._check_revision(revision)
            pending: list[tuple[str, DomainAdapter, object]] = []
            preview: dict[str, dict[str, bool]] = {}
            for name in selected:
                if name not in raw_sections:
                    raise CoreError("invalid_package", "Configuration package does not contain the selected section")
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

    # Python cannot use ``import`` as a method name, but keeping this alias
    # makes direct callers mirror the wire method exactly.
    def import_(self, **kwargs: Any) -> dict[str, Any]:
        return self.import_package(**kwargs)

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


CoreService = CoreStore
Core = CoreStore


__all__ = [
    "CORE_METADATA_VERSION",
    "Core",
    "CoreError",
    "CoreService",
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
