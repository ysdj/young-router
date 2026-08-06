"""Codex staged settings domain."""

from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import threading
from collections.abc import Mapping
from typing import Any, Iterator

from ..model_catalog import (
    catalog_is_current,
    managed_catalog_path,
    selected_model_names,
    write_catalog,
)
from ..model_contexts import ModelContextRegistry, default_context_cache_path
from ..security import redact
from ._shared import (
    LegacyDomainError,
    _action_name,
    _default_provider_config_path,
    _mapping,
    _safe_problem,
)

_CODEX_ENVIRONMENT_LOCK = threading.RLock()


@contextmanager
def _codex_environment(runtime_config_path: Path, codex_home: Path | None) -> Iterator[None]:
    """Use codex_config's public functions without leaking env changes.

    The legacy module intentionally discovers CODEX_HOME at call time.  A
    narrow process-global lock makes an explicit Core test/host path safe
    without keeping a divergent implementation of its editor protocol.
    """

    with _CODEX_ENVIRONMENT_LOCK:
        old_home = os.environ.get("CODEX_HOME")
        old_runtime = os.environ.get("LITELLM_CONFIG_FILE")
        try:
            if codex_home is not None:
                os.environ["CODEX_HOME"] = str(codex_home)
            os.environ["LITELLM_CONFIG_FILE"] = str(runtime_config_path)
            yield
        finally:
            if old_home is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = old_home
            if old_runtime is None:
                os.environ.pop("LITELLM_CONFIG_FILE", None)
            else:
                os.environ["LITELLM_CONFIG_FILE"] = old_runtime


class CodexSettingsDomain:
    """Staged Codex TOML/JSON editor backed by ``codex_config``."""

    name = "codex"

    def __init__(
        self,
        runtime_config_path: Path | str | None = None,
        *,
        codex_home: Path | str | None = None,
        runtime_settings_path: Path | str | None = None,
    ):
        self.runtime_config_path = Path(runtime_config_path).expanduser() if runtime_config_path else _default_provider_config_path()
        self.codex_home = Path(codex_home).expanduser() if codex_home else None
        configured_home = os.environ.get("CODEX_HOME", "").strip()
        resolved_home = self.codex_home or (Path(configured_home).expanduser() if configured_home else Path.home() / ".codex")
        self.model_catalog_path = managed_catalog_path(resolved_home)
        self._context_registry = ModelContextRegistry(
            runtime_config_path=self.runtime_config_path,
            runtime_settings_path=runtime_settings_path,
            cache_path=default_context_cache_path(resolved_home),
            refresh_enabled=runtime_settings_path is not None,
        )
        self._catalog_restart_required = False
        self._catalog_change_reason: str | None = None
        self._catalog_change_event = 0
        self._raw: dict[str, Any] = {}
        self._draft: dict[str, Any] = {}
        self._baseline: tuple[str, str] = ("", "{}\n")
        self.revision = 0
        self.reload()

    def _load_editor(self) -> dict[str, Any]:
        import codex_config

        try:
            with _codex_environment(self.runtime_config_path, self.codex_home):
                payload = codex_config.load_editor(self.runtime_config_path)
        except Exception as exc:
            raise _safe_problem(exc, "Codex settings could not be loaded") from None
        if not isinstance(payload, Mapping):
            raise LegacyDomainError("Codex settings are invalid")
        config_text = payload.get("config_text", "")
        auth_text = payload.get("auth_text", "{}\n")
        if not isinstance(config_text, str) or not isinstance(auth_text, str):
            raise LegacyDomainError("Codex settings are invalid")
        return copy.deepcopy(dict(payload))

    def _is_catalog_enabled(self, payload: Mapping[str, Any]) -> bool:
        structured = payload.get("structured", {})
        value = structured.get("model_catalog_json") if isinstance(structured, Mapping) else None
        return isinstance(value, str) and Path(value).expanduser() == self.model_catalog_path

    @staticmethod
    def _catalog_model_names(payload: Mapping[str, Any]) -> list[str]:
        structured = payload.get("structured", {})
        return selected_model_names(structured)

    def _require_catalog_model_names(self, payload: Mapping[str, Any]) -> list[str]:
        names = self._catalog_model_names(payload)
        if not names:
            raise LegacyDomainError("Select a Codex model before enabling the model catalog")
        return names

    def _queue_catalog_restart(self, reason: str) -> None:
        self._catalog_restart_required = True
        self._catalog_change_reason = reason
        self._catalog_change_event += 1

    def _ensure_model_catalog_current(self, *, notify: bool) -> bool:
        if not self._is_catalog_enabled(self._raw):
            return False
        names = self._catalog_model_names(self._raw)
        self._context_registry.refresh_if_due()
        if catalog_is_current(self.model_catalog_path, names, registry=self._context_registry):
            return False
        write_catalog(self.model_catalog_path, names, registry=self._context_registry)
        if notify:
            self._queue_catalog_restart("catalog_repaired")
        self.revision += 1
        return True

    def _safe_snapshot(self, payload: Mapping[str, Any], revision: int) -> dict[str, Any]:
        errors = payload.get("validation_errors", [])
        warnings = payload.get("warnings", [])
        public_models = self._catalog_model_names(payload)
        return {
            "domain": "codex",
            "revision": revision,
            "config_exists": bool(payload.get("config_exists")),
            "auth_file_exists": bool(payload.get("auth_exists")),
            "structured": redact(payload.get("structured", {})),
            "models": redact(payload.get("models", [])),
            "validation_errors": redact(errors if isinstance(errors, list) else []),
            "warnings": redact(warnings if isinstance(warnings, list) else []),
            "raw_editor_available": True,
            "model_catalog": {
                "enabled": self._is_catalog_enabled(payload),
                "public_models": public_models or [],
                "restart_required": self._catalog_restart_required,
                "change_reason": self._catalog_change_reason,
                "change_event": self._catalog_change_event,
            },
        }

    def snapshot(self) -> dict[str, Any]:
        self._ensure_model_catalog_current(notify=True)
        return self._safe_snapshot(self._draft, self.revision)

    def draft_state(self) -> object:
        return copy.deepcopy(self._draft)

    def _sync(self, config_text: str, auth_text: str, patch: object | None = None) -> dict[str, Any]:
        import codex_config

        payload: dict[str, Any] = {"config_text": config_text, "auth_text": auth_text}
        if patch is not None:
            payload["patch"] = copy.deepcopy(patch)
        try:
            with _codex_environment(self.runtime_config_path, self.codex_home):
                result = codex_config.sync_editor(payload, self.runtime_config_path)
        except Exception as exc:
            raise _safe_problem(exc, "Codex settings are invalid") from None
        if not isinstance(result, Mapping) or not isinstance(result.get("config_text"), str) or not isinstance(result.get("auth_text"), str):
            raise LegacyDomainError("Codex settings are invalid")
        return copy.deepcopy(dict(result))

    def dispatch(self, action: str, payload: object | None = None) -> dict[str, Any]:
        name = _action_name(action)
        data = _mapping(payload or {})
        config_text = self._draft.get("config_text", "")
        auth_text = self._draft.get("auth_text", "{}\n")
        if not isinstance(config_text, str) or not isinstance(auth_text, str):
            raise LegacyDomainError("Codex settings are invalid")
        if name in {"acknowledge_model_catalog_restart", "acknowledgemodelcatalogrestart"}:
            self._catalog_restart_required = False
            self._catalog_change_reason = None
        elif name in {"set_raw", "setraw"}:
            document = data.get("document")
            text = data.get("text")
            next_config = data.get("config_text", data.get("raw_toml", data.get("toml", config_text)))
            next_auth = data.get("auth_text", data.get("raw_json", data.get("auth", auth_text)))
            if document in {"config", "config.toml", "toml"} and isinstance(text, str):
                next_config = text
            if document in {"auth", "auth.json", "json"} and isinstance(text, str):
                next_auth = text
            if not isinstance(next_config, str) or not isinstance(next_auth, str):
                raise LegacyDomainError("Codex editor text must be text")
            self._draft = self._sync(next_config, next_auth)
        elif name in {"set", "patch", "edit", "select_model", "selectmodel"}:
            if "config_text" in data or "auth_text" in data or "raw_toml" in data or "raw_json" in data:
                next_config = data.get("config_text", data.get("raw_toml", config_text))
                next_auth = data.get("auth_text", data.get("raw_json", auth_text))
                if not isinstance(next_config, str) or not isinstance(next_auth, str):
                    raise LegacyDomainError("Codex editor text must be text")
                self._draft = self._sync(next_config, next_auth)
            else:
                patch = data.get("patch", data.get("structured", data))
                if name in {"select_model", "selectmodel"} and "litellm_model" not in _mapping(patch):
                    patch = {"litellm_model": data.get("model", data.get("selection"))}
                self._draft = self._sync(config_text, auth_text, patch)
        elif name in {"reset", "cancel", "reload", "restore_defaults"}:
            self._draft = copy.deepcopy(self._raw)
        else:
            raise LegacyDomainError("The requested Codex action is unavailable")
        self.revision += 1
        return self.snapshot()

    def secret_present(self, field: str, target: str | None = None) -> bool:
        if field != "api_key" or target is not None:
            raise LegacyDomainError("The requested secret field is unavailable")
        structured = self._draft.get("structured", {})
        return isinstance(structured, Mapping) and bool(structured.get("api_key"))

    def stage_secret(self, field: str, target: str | None, value: str) -> None:
        if field != "api_key" or target is not None:
            raise LegacyDomainError("The requested secret field is unavailable")
        config_text = self._draft.get("config_text", "")
        auth_text = self._draft.get("auth_text", "{}\n")
        if not isinstance(config_text, str) or not isinstance(auth_text, str):
            raise LegacyDomainError("Codex settings are invalid")
        self._draft = self._sync(config_text, auth_text, {"api_key": value or None})
        self.revision += 1

    def validate(self, payload: object | None = None) -> dict[str, Any]:
        draft = self._draft
        if payload is not None:
            data = _mapping(payload)
            config_text = data.get("config_text", draft.get("config_text", ""))
            auth_text = data.get("auth_text", draft.get("auth_text", "{}\n"))
            if not isinstance(config_text, str) or not isinstance(auth_text, str):
                return {"valid": False, "errors": ["Codex editor text must be text"]}
            try:
                draft = self._sync(config_text, auth_text, data.get("patch", data.get("structured")))
            except LegacyDomainError:
                return {"valid": False, "errors": ["Codex settings are invalid"]}
        errors = draft.get("validation_errors", [])
        return {"valid": not bool(errors), "errors": list(errors) if isinstance(errors, list) else ["Codex settings are invalid"]}

    def apply(self, payload: object | None = None) -> dict[str, Any]:
        import codex_config

        if payload is not None:
            data = _mapping(payload)
            if "config_text" in data or "auth_text" in data:
                self.dispatch("set_raw", data)
        validation = self.validate()
        if not validation["valid"]:
            raise LegacyDomainError("Codex settings are invalid")
        current = self._load_editor()
        if (current.get("config_text"), current.get("auth_text")) != self._baseline:
            raise LegacyDomainError("Codex settings changed on disk; reload before applying")
        was_enabled = self._is_catalog_enabled(self._raw)
        will_be_enabled = self._is_catalog_enabled(self._draft)
        catalog_models = self._require_catalog_model_names(self._draft) if will_be_enabled else None
        self._context_registry.refresh_if_due()
        catalog_changed = catalog_models is not None and not catalog_is_current(
            self.model_catalog_path,
            catalog_models,
            registry=self._context_registry,
        )
        if catalog_changed and catalog_models is not None:
            write_catalog(self.model_catalog_path, catalog_models, registry=self._context_registry)
        try:
            with _codex_environment(self.runtime_config_path, self.codex_home):
                codex_config.apply_editor(
                    {
                        "config_text": self._draft["config_text"],
                        "auth_text": self._draft["auth_text"],
                    },
                    self.runtime_config_path,
                )
        except Exception as exc:
            raise _safe_problem(exc, "Codex settings could not be saved") from None
        self.reload()
        if will_be_enabled and (not was_enabled or catalog_changed):
            self._queue_catalog_restart("enabled" if not was_enabled else "catalog_repaired")
            self.revision += 1
        elif was_enabled and not will_be_enabled:
            self._queue_catalog_restart("disabled")
            self.revision += 1
        return {"applied": True, **self.snapshot()}

    def catalog_baseline_state(self) -> object:
        """Return the applied Codex editor state for an immediate menu action."""

        return copy.deepcopy(self._raw)

    def set_model_catalog_enabled_immediately(self, enabled: bool) -> dict[str, Any]:
        """Toggle only the managed catalog, preserving unrelated staged edits."""

        if not isinstance(enabled, bool):
            raise LegacyDomainError("The Codex model catalog switch is invalid")
        import codex_config

        current = self._load_editor()
        if (current.get("config_text"), current.get("auth_text")) != self._baseline:
            raise LegacyDomainError("Codex settings changed on disk; reload before applying")
        was_enabled = self._is_catalog_enabled(self._raw)
        catalog_models = self._require_catalog_model_names(self._raw) if enabled else None
        self._context_registry.refresh_if_due()
        catalog_changed = catalog_models is not None and not catalog_is_current(
            self.model_catalog_path,
            catalog_models,
            registry=self._context_registry,
        )
        draft_before = copy.deepcopy(self._draft)
        draft_next = draft_before
        draft_config = draft_before.get("config_text", "")
        draft_auth = draft_before.get("auth_text", "{}\n")
        if isinstance(draft_config, str) and isinstance(draft_auth, str):
            try:
                draft_next = self._sync(
                    draft_config,
                    draft_auth,
                    {"model_catalog_json": str(self.model_catalog_path) if enabled else None},
                )
            except LegacyDomainError:
                # A raw editor may intentionally contain an invalid draft. Keep
                # that draft intact while the menu action updates the applied
                # document below.
                draft_next = draft_before
        if catalog_changed and catalog_models is not None:
            write_catalog(self.model_catalog_path, catalog_models, registry=self._context_registry)
        disk_patch = {"model_catalog_json": str(self.model_catalog_path) if enabled else None}
        next_disk = self._sync(
            str(current.get("config_text", "")),
            str(current.get("auth_text", "{}\n")),
            disk_patch,
        )
        try:
            with _codex_environment(self.runtime_config_path, self.codex_home):
                codex_config.apply_editor(
                    {
                        "config_text": next_disk["config_text"],
                        "auth_text": next_disk["auth_text"],
                    },
                    self.runtime_config_path,
                )
        except Exception as exc:
            raise _safe_problem(exc, "Codex settings could not be saved") from None
        self.reload()
        if draft_next != draft_before:
            self._draft = draft_next
        if enabled and (not was_enabled or catalog_changed):
            self._queue_catalog_restart("enabled" if not was_enabled else "catalog_repaired")
            self.revision += 1
        elif was_enabled and not enabled:
            self._queue_catalog_restart("disabled")
            self.revision += 1
        return self.snapshot()

    def external_disk_state(self) -> dict[str, bool]:
        """Report only whether either private Codex document changed."""

        self._ensure_model_catalog_current(notify=True)
        current = self._load_editor()
        identity = (str(current.get("config_text", "")), str(current.get("auth_text", "{}\n")))
        return {
            "changed": identity != self._baseline,
            "exists": bool(current.get("config_exists") or current.get("auth_exists")),
        }

    def external_disk_identity(self) -> str:
        """Return an opaque identity without exposing either private document."""

        current = self._load_editor()
        config = str(current.get("config_text", "")).encode("utf-8")
        auth = str(current.get("auth_text", "{}\n")).encode("utf-8")
        digest = hashlib.sha256()
        digest.update(len(config).to_bytes(8, "big"))
        digest.update(config)
        digest.update(len(auth).to_bytes(8, "big"))
        digest.update(auth)
        return digest.hexdigest()

    def rebase_external_disk(self) -> dict[str, Any]:
        """Accept the current disk identity while retaining the staged draft."""

        current = self._load_editor()
        self._baseline = (str(current.get("config_text", "")), str(current.get("auth_text", "{}\n")))
        self.revision += 1
        return self.snapshot()

    def reload(self) -> dict[str, Any]:
        payload = self._load_editor()
        self._raw = copy.deepcopy(payload)
        self._draft = copy.deepcopy(payload)
        self._baseline = (str(payload.get("config_text", "")), str(payload.get("auth_text", "{}\n")))
        self.revision += 1
        return self.snapshot()

    def export(self, *, include_sensitive: bool = False) -> dict[str, Any]:
        if include_sensitive:
            return {
                "domain": self.name,
                "config_text": self._draft.get("config_text", ""),
                "auth_text": self._draft.get("auth_text", "{}\n"),
            }
        return self.snapshot()

    def import_package(self, payload: object) -> None:
        data = _mapping(payload, "Codex package")
        self.dispatch("set_raw", data)

    def probe(self, _payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        result = self.validate()
        return {"ok": bool(result["valid"]), "protocols": [], "detail": "Codex validation completed" if result["valid"] else "Codex validation failed"}

__all__ = ["CodexSettingsDomain"]
