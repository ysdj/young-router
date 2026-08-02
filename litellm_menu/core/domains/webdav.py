"""WebDAV staged settings domain."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import stat
from collections.abc import Mapping
from typing import Any

from ..persistence import PersistenceError, atomic_write_json, atomic_write_text
from ..security import REDACTED, safe_exception_message
from ._shared import (
    LegacyDomainError,
    _action_name,
    _default_webdav_enabled_path,
    _default_webdav_status_path,
    _file_bytes,
    _mapping,
    _safe_problem,
    _same_file,
)

class WebDAVSettingsDomain:
    """Staged WebDAV settings using the existing WebDAV core client."""

    name = "webdav"

    def __init__(
        self,
        settings_path: Path | str | None = None,
        *,
        enabled_path: Path | str | None = None,
        status_path: Path | str | None = None,
    ):
        from webdav import core as webdav_core

        self.settings_path = Path(settings_path).expanduser() if settings_path else webdav_core.default_settings_file()
        self.enabled_path = Path(enabled_path).expanduser() if enabled_path else _default_webdav_enabled_path(self.settings_path)
        self.status_path = Path(status_path).expanduser() if status_path else _default_webdav_status_path(self.settings_path)
        self._raw_settings: dict[str, Any] = {}
        self._draft_settings: dict[str, Any] = {}
        self._raw_enabled = False
        self._draft_enabled = False
        self._baseline_settings: bytes | None = None
        self._baseline_enabled: bool = False
        self._last_probe = "unknown"
        self.revision = 0
        self.reload()

    @staticmethod
    def _settings_raw(settings: object) -> dict[str, Any]:
        return {
            "url": str(getattr(settings, "url", "")),
            "username": str(getattr(settings, "username", "")),
            "password": str(getattr(settings, "password", "")),
            "remote_name": str(getattr(settings, "remote_name", "")),
            "sync_interval_minutes": getattr(settings, "sync_interval_minutes", 30),
            "timeout_seconds": getattr(settings, "timeout_seconds", 30),
        }

    def _enabled(self) -> bool:
        try:
            details = self.enabled_path.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            raise LegacyDomainError("WebDAV enablement state is unavailable") from None
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise LegacyDomainError("WebDAV enablement state is unavailable")
        return True

    def _load(self) -> tuple[dict[str, Any], bool, bytes | None]:
        from webdav import core as webdav_core

        try:
            baseline = _file_bytes(self.settings_path)
            settings = webdav_core.load_settings(self.settings_path)
            enabled = self._enabled()
        except LegacyDomainError:
            raise
        except Exception as exc:
            raise _safe_problem(exc, "WebDAV settings could not be loaded") from None
        return self._settings_raw(settings), enabled, baseline

    def _settings(self, raw: Mapping[str, Any] | None = None) -> Any:
        from webdav import core as webdav_core

        try:
            return webdav_core._settings_from_raw(dict(raw or self._draft_settings))
        except Exception as exc:
            raise _safe_problem(exc, "WebDAV settings are invalid") from None

    def snapshot(self) -> dict[str, Any]:
        settings = self._settings()
        sanitized = settings.sanitized()
        password_present = bool(self._draft_settings.get("password"))
        return {
            "domain": self.name,
            "revision": self.revision,
            "enabled": self._draft_enabled,
            "configured": bool(settings.configured),
            "url": sanitized["url"],
            "username": sanitized["username"],
            "remote_name": sanitized["remote_name"],
            "sync_interval_minutes": sanitized["sync_interval_minutes"],
            "sync_interval": sanitized["sync_interval_minutes"],
            "timeout_seconds": sanitized["timeout_seconds"],
            "timeout": sanitized["timeout_seconds"],
            # The Core projection redacts this field again; its only purpose
            # is to give the summary a presence bit without carrying a value.
            "password": REDACTED if password_present else "",
            "password_configured": password_present,
            "last_probe": self._last_probe,
        }

    def _patch(self, data: Mapping[str, Any]) -> None:
        allowed = {"url", "username", "password", "remote_name", "sync_interval_minutes", "timeout_seconds"}
        source = data.get("settings", data.get("values", data))
        updates = _mapping(source, "WebDAV settings")
        aliases = {
            "sync_interval": "sync_interval_minutes",
            "timeout": "timeout_seconds",
        }
        updates = {aliases.get(key, key): value for key, value in updates.items()}
        unknown = set(updates).difference(allowed | {"enabled", "keep_password", "clear_password"})
        if unknown:
            raise LegacyDomainError("WebDAV settings contain an unsupported field")
        draft = copy.deepcopy(self._draft_settings)
        for key in allowed:
            if key not in updates:
                continue
            value = updates[key]
            if key == "password" and value == "__LITELLM_MENU_RETAIN_EXISTING__":
                continue
            draft[key] = value
        if updates.get("clear_password") is True:
            draft["password"] = ""
        if "enabled" in updates:
            self._draft_enabled = bool(updates["enabled"])
        self._settings(draft)
        self._draft_settings = draft

    def dispatch(self, action: str, payload: object | None = None) -> dict[str, Any]:
        name = _action_name(action)
        data = _mapping(payload or {})
        if name in {"set", "patch", "replace", "set_raw", "setraw"}:
            self._patch(data)
        elif name in {"clear_password", "clearpassword"}:
            self._draft_settings["password"] = ""
        elif name in {"enable", "webdav_enable"}:
            self._draft_enabled = True
        elif name in {"disable", "webdav_disable"}:
            self._draft_enabled = False
        elif name in {"restore_defaults", "defaults"}:
            self._draft_settings = {
                "url": "",
                "username": "",
                "password": "",
                "remote_name": "litellm-menu-config.json",
                "sync_interval_minutes": 30,
                "timeout_seconds": 30,
            }
            self._draft_enabled = False
        elif name in {"reset", "cancel", "reload"}:
            self._draft_settings = copy.deepcopy(self._raw_settings)
            self._draft_enabled = self._raw_enabled
        else:
            raise LegacyDomainError("The requested WebDAV action is unavailable")
        self.revision += 1
        return self.snapshot()

    def secret_present(self, field: str, target: str | None = None) -> bool:
        if field != "password" or target is not None:
            raise LegacyDomainError("The requested secret field is unavailable")
        return bool(self._draft_settings.get("password"))

    def stage_secret(self, field: str, target: str | None, value: str) -> None:
        if field != "password" or target is not None:
            raise LegacyDomainError("The requested secret field is unavailable")
        draft = copy.deepcopy(self._draft_settings)
        draft["password"] = value
        self._settings(draft)
        self._draft_settings = draft
        self.revision += 1

    def validate(self, payload: object | None = None) -> dict[str, Any]:
        try:
            raw = self._draft_settings
            enabled = self._draft_enabled
            if payload is not None:
                data = _mapping(payload)
                raw = copy.deepcopy(raw)
                updates = _mapping(data.get("settings", data.get("values", data)), "WebDAV settings")
                raw.update({key: value for key, value in updates.items() if key in raw})
                enabled = bool(updates.get("enabled", enabled))
            settings = self._settings(raw)
            if enabled and not settings.configured:
                raise LegacyDomainError("WebDAV URL is required before enabling sync")
        except LegacyDomainError:
            return {"valid": False, "errors": ["WebDAV settings are invalid"]}
        return {"valid": True, "errors": []}

    def _write_enabled(self, enabled: bool) -> None:
        if enabled:
            try:
                atomic_write_text(self.enabled_path, "1\n")
            except PersistenceError as exc:
                raise LegacyDomainError(safe_exception_message(exc)) from None
            return
        try:
            details = self.enabled_path.lstat()
        except FileNotFoundError:
            return
        except OSError:
            raise LegacyDomainError("WebDAV enablement state could not be saved") from None
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise LegacyDomainError("WebDAV enablement state could not be saved")
        try:
            self.enabled_path.unlink()
        except OSError:
            raise LegacyDomainError("WebDAV enablement state could not be saved") from None

    def apply(self, payload: object | None = None) -> dict[str, Any]:
        from webdav import core as webdav_core

        if payload is not None:
            data = _mapping(payload)
            if data:
                self._patch(data)
        validation = self.validate()
        if not validation["valid"]:
            raise LegacyDomainError("WebDAV settings are invalid")
        if not _same_file(self.settings_path, self._baseline_settings) or self._enabled() != self._baseline_enabled:
            raise LegacyDomainError("WebDAV settings changed on disk; reload before applying")
        settings = self._settings()
        try:
            # Save through the existing module so URL normalization and its
            # on-disk compatibility remain exactly the same as the old app.
            webdav_core.save_settings(self.settings_path, settings)
            self._write_enabled(self._draft_enabled)
        except LegacyDomainError:
            raise
        except Exception as exc:
            raise _safe_problem(exc, "WebDAV settings could not be saved") from None
        self.reload()
        return {"applied": True, **self.snapshot()}

    def _disk_baseline(self) -> tuple[bytes | None, bool]:
        return _file_bytes(self.settings_path), self._enabled()

    @staticmethod
    def _disk_identity(settings: bytes | None, enabled: bool) -> str:
        digest = hashlib.sha256()
        digest.update(b"present\\0" if settings is not None else b"missing\\0")
        if settings is not None:
            digest.update(settings)
        digest.update(b"enabled\\1" if enabled else b"enabled\\0")
        return digest.hexdigest()

    def external_disk_state(self) -> dict[str, bool]:
        """Compare both settings and enablement files with their baseline."""

        settings, enabled = self._disk_baseline()
        return {
            "changed": settings != self._baseline_settings or enabled != self._baseline_enabled,
            "exists": settings is not None,
        }

    def external_disk_identity(self) -> str:
        settings, enabled = self._disk_baseline()
        return self._disk_identity(settings, enabled)

    def rebase_external_disk(self) -> dict[str, Any]:
        """Accept external files as the Apply baseline while retaining the draft."""

        self._baseline_settings, self._baseline_enabled = self._disk_baseline()
        self.revision += 1
        return self.snapshot()

    def reload(self) -> dict[str, Any]:
        settings, enabled, baseline = self._load()
        self._raw_settings = copy.deepcopy(settings)
        self._draft_settings = copy.deepcopy(settings)
        self._raw_enabled = enabled
        self._draft_enabled = enabled
        self._baseline_settings = baseline
        self._baseline_enabled = enabled
        self.revision += 1
        return self.snapshot()

    def export(self, *, include_sensitive: bool = False) -> dict[str, Any]:
        if include_sensitive:
            return {"domain": self.name, "settings": copy.deepcopy(self._draft_settings), "enabled": self._draft_enabled}
        return self.snapshot()

    def import_package(self, payload: object) -> None:
        data = _mapping(payload, "WebDAV package")
        self._patch(data)
        if "enabled" in data:
            self._draft_enabled = bool(data["enabled"])
        self.revision += 1

    def probe(self, _payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        from webdav import core as webdav_core

        try:
            settings = self._settings()
            if not settings.configured:
                raise LegacyDomainError("WebDAV is not configured")
            client = webdav_core.WebDAVClient(settings)
            client.try_mkcol(webdav_core.collection_url(settings))
            try:
                client.head(webdav_core.bundle_url(settings))
            except webdav_core.WebDAVHTTPError as exc:
                if exc.code != 404:
                    raise
            self._last_probe = "ok"
            atomic_write_json(self.status_path, {"action": "probe", "checked_at": webdav_core._utc_now(), "ok": True})
            return {"ok": True, "protocols": ["webdav"], "detail": "WebDAV probe succeeded"}
        except Exception:
            self._last_probe = "failed"
            try:
                atomic_write_json(self.status_path, {"action": "probe", "checked_at": webdav_core._utc_now(), "ok": False})
            except PersistenceError:
                pass
            return {"ok": False, "protocols": ["webdav"], "detail": "WebDAV probe failed"}

__all__ = ["WebDAVSettingsDomain"]
