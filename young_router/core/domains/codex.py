"""Codex staged settings domain."""

from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import stat
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any, Iterator

from .. import model_catalog
from ..model_catalog import (
    catalog_carries_native_profile,
    catalog_is_current,
    catalog_model_names,
    catalog_names_from_editor,
    legacy_managed_catalog_path,
    managed_catalog_path,
    previous_managed_catalog_path,
    profile_has_instructions,
    select_inherited_profile,
    write_catalog,
)
from ..model_contexts import (
    ModelContextRegistry,
    default_context_cache_path,
    legacy_context_cache_paths,
)
from ..persistence import PersistenceError, read_json
from ..security import redact
from ._shared import (
    DomainError,
    _action_name,
    _default_provider_config_path,
    _default_runtime_root,
    _mapping,
    _safe_problem,
)

_CODEX_ENVIRONMENT_LOCK = threading.RLock()
_CATALOG_SOURCE_REFRESH_SECONDS = 0.5

# App-private bookkeeping for the managed catalog.  Codex only ever reads the
# catalog itself (through ``model_catalog_json``): the deferred-restart
# acknowledgement stays in this process's memory and the pinned inheritance
# profile lives in the runtime settings file, so no catalog state file is
# written anywhere.
CATALOG_BASE_PROFILE_SETTING = "YOUNG_ROUTER_CATALOG_BASE_PROFILE"
# Sidecar spellings older releases wrote into the app root or the Codex home.
# They are imported once and removed so no app state stays behind.
CATALOG_STATE_FILE_NAME = "model-catalog-state.json"
PREVIOUS_CATALOG_STATE_FILE_NAMES = (
    "young-router-model-catalog-state.json",
    "litellm-menu-model-catalog-state.json",
)


def _resolve_runtime_root(
    runtime_root: Path | str | None,
    runtime_settings_path: Path | str | None,
    runtime_config_path: Path | str | None,
) -> Path:
    """Pick the app-state directory for a Codex domain.

    Production passes the host's runtime root explicitly.  A directly
    constructed domain falls back to the directory holding its runtime
    documents, so tests and portable callers never touch the user's real app
    directory.
    """

    if runtime_root is not None:
        return Path(runtime_root).expanduser()
    if runtime_settings_path is not None:
        return Path(runtime_settings_path).expanduser().parent
    if runtime_config_path is not None:
        return Path(runtime_config_path).expanduser().parent
    return _default_runtime_root()

# The provider row this app owns in the Codex configuration.  Adopting the
# local proxy through a dedicated row keeps the client's own backends intact
# (``使用特定API`` can always go back to them) and gives the row an identity
# that survives a port or key change, so it can follow the runtime settings.
LOCAL_API_PROVIDER_ID = "custom"

# LiteLLM writes an adapter prefix in front of a route's own model id.  A
# direct client connection talks to the provider itself, so that prefix is
# dropped; any other leading segment belongs to the provider's own model name.
_LITELLM_ADAPTER_PREFIXES = ("openai/", "anthropic/")


def _direct_model_id(value: str) -> str:
    """Return the provider's own model id for a direct connection."""

    model = value.strip()
    for prefix in _LITELLM_ADAPTER_PREFIXES:
        if model.startswith(prefix) and model[len(prefix) :].strip():
            return model[len(prefix) :].strip()
    return model


@contextmanager
def _codex_environment(runtime_config_path: Path, codex_home: Path | None) -> Iterator[None]:
    """Use codex_config's public functions without leaking env changes.

    The configuration adapter discovers CODEX_HOME at call time.  A
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
        runtime_root: Path | str | None = None,
    ):
        self.runtime_config_path = Path(runtime_config_path).expanduser() if runtime_config_path else _default_provider_config_path()
        self.runtime_settings_path = (
            Path(runtime_settings_path).expanduser() if runtime_settings_path else None
        )
        self.codex_home = Path(codex_home).expanduser() if codex_home else None
        configured_home = os.environ.get("CODEX_HOME", "").strip()
        resolved_home = self.codex_home or (Path(configured_home).expanduser() if configured_home else Path.home() / ".codex")
        self.runtime_root = _resolve_runtime_root(
            runtime_root, runtime_settings_path, runtime_config_path
        )
        self.model_catalog_path = managed_catalog_path(resolved_home)
        # App-private catalog bookkeeping stays out of the Codex directory:
        # the acknowledgement lives in this process's memory (the host's UI
        # already deduplicates re-presentation) and the inheritance pin lives
        # in the app's runtime settings file.  Legacy sidecar files are
        # imported once and removed.
        self._catalog_base_profile_pin: str | None = None
        self._catalog_base_profile_loaded = False
        self._context_registry = ModelContextRegistry(
            runtime_config_path=self.runtime_config_path,
            runtime_settings_path=runtime_settings_path,
            cache_path=default_context_cache_path(self.runtime_root),
            legacy_cache_paths=legacy_context_cache_paths(resolved_home),
            refresh_enabled=runtime_settings_path is not None,
        )
        self._catalog_restart_required = False
        self._catalog_change_reason: str | None = None
        self._catalog_change_event = 0
        # A managed catalog pointer that names a missing file makes Codex refuse
        # every configuration load ("No such file or directory"), so the repair
        # that clears it must never re-enter itself through ``reload()`` and must
        # report a failure instead of leaving the client broken silently.
        self._catalog_pointer_repairing = False
        self._catalog_pointer_error: str | None = None
        # A deferred restart acknowledges the current public-model set. Keep
        # that acknowledgement in memory so a later catalog repair/snapshot
        # cannot manufacture a new prompt for the same model IDs. A genuine
        # model-set (or enabled-state) change still creates a new event, and a
        # recreated Core re-arms the signature instead of writing app state to
        # disk for it.
        self._catalog_pending_signature: tuple[bool, tuple[str, ...]] | None = None
        self._catalog_acknowledged_signature: tuple[bool, tuple[str, ...]] | None = None
        # Endpoint-backed repairs are noisy during proxy reloads: /v1/models
        # can briefly alternate between adjacent worker views. Require two
        # consecutive observations of the same repaired model set before
        # asking Codex to restart. Explicit enable/disable actions bypass this
        # observation gate and remain immediate.
        self._catalog_repair_observed_signature: tuple[bool, tuple[str, ...]] | None = None
        self._catalog_repair_observation_count = 0
        self._catalog_source_checked_at = 0.0
        self._raw: dict[str, Any] = {}
        self._draft: dict[str, Any] = {}
        self._baseline: tuple[str, str] = ("", "{}\n")
        # The app's own proxy endpoint, resolved once per document load rather
        # than per snapshot: it reads the runtime settings and the (large)
        # runtime configuration, while snapshots are polled while a window is
        # open.  `uses_local_api` then only compares strings.
        self._local_api_base_url: str | None = None
        self.revision = 0
        self.reload()

    def _catalog_state_profile_key(self) -> str:
        """Canonical Codex home naming this profile in a legacy sidecar."""

        home = self.codex_home_path()
        try:
            return str(home.resolve(strict=False))
        except OSError:
            return str(home.absolute())

    def _read_settings_catalog_base_profile(self) -> str:
        """Read the pinned inheritance profile from the runtime settings file."""

        if self.runtime_settings_path is None:
            return ""
        from ..runtime_settings_io import load_specs, read_settings_file

        try:
            values = read_settings_file(self.runtime_settings_path, load_specs())
        except Exception:
            return ""
        value = values.get(CATALOG_BASE_PROFILE_SETTING)
        return value.strip() if isinstance(value, str) and value.strip() else ""

    def _store_catalog_base_profile(self, slug: str) -> None:
        """Persist the pin in the runtime settings file (best effort).

        The pin is an optimization: failing to store it must not block the
        refresh that is already using the resolved slug.
        """

        self._catalog_base_profile_pin = slug
        if self.runtime_settings_path is None:
            return
        from ..runtime_settings_io import load_specs, read_settings_file, write_settings_file

        try:
            specs = load_specs()
            values = read_settings_file(self.runtime_settings_path, specs)
            values[CATALOG_BASE_PROFILE_SETTING] = slug
            write_settings_file(self.runtime_settings_path, specs, values)
        except Exception:
            pass

    def _legacy_catalog_pin(self, path: Path) -> str:
        """Read a pin from a retired sidecar (flat or profile-keyed layout)."""

        try:
            payload = read_json(path, default={})
        except PersistenceError:
            return ""
        if not isinstance(payload, Mapping):
            return ""
        profiles = payload.get("profiles")
        if isinstance(profiles, Mapping):
            entry = profiles.get(self._catalog_state_profile_key())
            payload = entry if isinstance(entry, Mapping) else {}
        value = payload.get("base_profile")
        return value.strip() if isinstance(value, str) and value.strip() else ""

    def _import_legacy_catalog_pin(self) -> str:
        """Move a retired sidecar pin into the settings file and clean up.

        The sidecar only ever held the deferred-restart acknowledgement plus
        this pin.  The acknowledgement is process state now, so the files are
        imported once, their pin is stored in the runtime settings, and every
        retired copy is removed from both the app root and the Codex home.
        """

        candidates = [
            self.runtime_root / CATALOG_STATE_FILE_NAME,
            self.codex_home_path() / CATALOG_STATE_FILE_NAME,
            *(self.codex_home_path() / name for name in PREVIOUS_CATALOG_STATE_FILE_NAMES),
        ]
        pin = ""
        for path in candidates:
            found = self._legacy_catalog_pin(path)
            if found:
                pin = found
                break
        if pin:
            self._store_catalog_base_profile(pin)
        for path in candidates:
            try:
                path.unlink()
            except OSError:
                pass
        return pin

    def _load_catalog_base_profile(self) -> str | None:
        """The pinned inheritance source for models without a native slug."""

        if not self._catalog_base_profile_loaded:
            self._catalog_base_profile_loaded = True
            pin = self._read_settings_catalog_base_profile()
            if not pin:
                pin = self._import_legacy_catalog_pin()
            self._catalog_base_profile_pin = pin or None
        return self._catalog_base_profile_pin

    def _catalog_base_profile(self, native_models: Sequence[Mapping[str, Any]]) -> str | None:
        """Resolve (and pin) the profile that alias models inherit.

        The resolved slug is remembered so a client update that promotes a new
        flagship, hides a model, or reorders its catalog cannot silently re-map
        every third-party route's instructions; the pin is replaced only when
        the client no longer offers that profile.
        """

        pinned = self._load_catalog_base_profile()
        if pinned and any(
            isinstance(item, Mapping)
            and str(item.get("slug") or "").strip().casefold() == pinned.casefold()
            and profile_has_instructions(item)
            for item in native_models
        ):
            return pinned
        selected = select_inherited_profile(native_models)
        slug = str(selected.get("slug") or "").strip() if isinstance(selected, Mapping) else ""
        if slug and slug.casefold() != (pinned or "").casefold():
            self._store_catalog_base_profile(slug)
        return slug or None

    def _catalog_native_models(self) -> list[Mapping[str, Any]] | None:
        """Native models for a catalog refresh, or ``None`` to keep the file.

        ``None`` means the installed client's bundled catalog could not be read
        while the managed file already carries that client's own instructions
        (an in-flight update, for example).  Writing this app's minimal
        fallback profile then would downgrade every model's instructions, so
        the caller keeps the file and retries on the next refresh.
        """

        native_models = model_catalog.load_native_catalog()
        if not native_models and catalog_carries_native_profile(self.model_catalog_path):
            return None
        return native_models

    def _load_editor(self) -> dict[str, Any]:
        from .. import codex_config

        try:
            with _codex_environment(self.runtime_config_path, self.codex_home):
                payload = codex_config.load_editor(self.runtime_config_path)
        except Exception as exc:
            raise _safe_problem(exc, "Codex settings could not be loaded") from None
        if not isinstance(payload, Mapping):
            raise DomainError("Codex settings are invalid")
        config_text = payload.get("config_text", "")
        auth_text = payload.get("auth_text", "{}\n")
        if not isinstance(config_text, str) or not isinstance(auth_text, str):
            raise DomainError("Codex settings are invalid")
        return copy.deepcopy(dict(payload))

    def codex_home_path(self) -> Path:
        """Return the home directory that owns Codex's editor documents."""

        configured_home = os.environ.get("CODEX_HOME", "").strip()
        return self.codex_home or (Path(configured_home).expanduser() if configured_home else Path.home() / ".codex")

    def client_files(self) -> list[dict[str, Any]]:
        """Describe the Codex documents for the external-settings listing."""

        home = self.codex_home_path()
        rows: list[dict[str, Any]] = []
        for document, name, language in (("config", "config.toml", "toml"), ("auth", "auth.json", "json")):
            path = home / name
            try:
                details = path.lstat()
                exists = stat.S_ISREG(details.st_mode)
            except OSError:
                exists = False
            rows.append(
                {
                    "id": f"codex.{document}",
                    "client": "codex",
                    "domain": self.name,
                    "document": document,
                    "name": name,
                    "path": str(path),
                    "language": language,
                    "exists": exists,
                }
            )
        return rows

    def _editor_documents_from_disk(self) -> tuple[str, str, bool, bool]:
        """Read the two editor documents without probing the LiteLLM endpoint.

        The settings-page watcher only needs an opaque file comparison.  Going
        through ``codex_config.load_editor`` here also refreshes the exposed
        model catalog, which performs an authenticated local ``/v1/models``
        request.  That work belongs to a full snapshot, after a real file
        change, rather than to a five-second idle probe.
        """

        home = self.codex_home_path()

        def read_document(filename: str, label: str) -> tuple[str, bool]:
            path = home / filename
            try:
                details = path.lstat()
            except FileNotFoundError:
                return "", False
            except OSError:
                raise DomainError(f"{label} could not be read") from None
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                raise DomainError(f"{label} is unavailable")
            try:
                return path.read_text(encoding="utf-8"), True
            except (OSError, UnicodeError):
                raise DomainError(f"{label} could not be read") from None

        config_text, config_exists = read_document("config.toml", "Codex config file")
        auth_text, auth_exists = read_document("auth.json", "Codex auth file")
        return config_text, auth_text if auth_exists else "{}\n", config_exists, auth_exists

    def _catalog_pointer(self, payload: Mapping[str, Any]) -> Path | None:
        """Return the applied ``model_catalog_json`` target when it is a path."""

        structured = payload.get("structured", {})
        value = structured.get("model_catalog_json") if isinstance(structured, Mapping) else None
        if not isinstance(value, str) or not value.strip():
            return None
        return Path(value).expanduser()

    def _managed_catalog_paths(self) -> tuple[Path, ...]:
        """Return the catalog paths this app owns, current name first.

        The two historical spellings stay managed so a Codex config that still
        points at either one counts as enabled and is migrated to the current
        name instead of being mistaken for a foreign catalog.
        """

        home = self.codex_home_path()
        return (
            self.model_catalog_path,
            previous_managed_catalog_path(home),
            legacy_managed_catalog_path(home),
        )

    def _catalog_pointer_is_managed(self, payload: Mapping[str, Any]) -> bool:
        """Whether the applied pointer names a catalog file this app writes."""

        pointer = self._catalog_pointer(payload)
        return pointer is not None and pointer in self._managed_catalog_paths()

    def persistence_paths(self) -> tuple[Path, ...]:
        """Files this domain owns, including the historical catalog names.

        The older spellings are tracked so their deletion is still observed as
        a disk change (the settings timer then repairs the pointer it dangles
        from) on installations that predate the current naming.
        """

        return self._managed_catalog_paths()

    def _is_catalog_enabled(self, payload: Mapping[str, Any]) -> bool:
        # A historical managed path counts as enabled: a config still pointing
        # at an older spelling is this app's catalog, and the next repair or
        # apply migrates the pointer to the current name.
        return self._catalog_pointer_is_managed(payload)

    def _repair_catalog_pointer(self, value: str | None) -> bool:
        """Rewrite only ``model_catalog_json`` on disk, keeping Codex loadable.

        Codex rejects its whole configuration while that key names a missing
        file, so this repair is intentionally independent of the endpoint
        probe.  The two documents are read from disk immediately before the
        write so an external edit made since the last reload is preserved.
        """

        from .. import codex_config

        if self._catalog_pointer_repairing:
            return False
        patch: dict[str, Any] = {"model_catalog_json": value}
        draft_before = copy.deepcopy(self._draft)
        try:
            config_text, auth_text, _, _ = self._editor_documents_from_disk()
            with _codex_environment(self.runtime_config_path, self.codex_home):
                repaired = self._sync(config_text, auth_text, patch)
                codex_config.apply_editor(
                    {
                        "config_text": repaired["config_text"],
                        "auth_text": repaired["auth_text"],
                    },
                    self.runtime_config_path,
                )
        except Exception:
            self._catalog_pointer_error = (
                "Codex catalog pointer could not be repaired; fix model_catalog_json in config.toml"
            )
            return False
        self._catalog_pointer_error = None
        self._catalog_pointer_repairing = True
        try:
            self.reload()
        finally:
            self._catalog_pointer_repairing = False
        # The staged draft is the user's own copy of config.toml.  Move its
        # pointer with the disk one (as the explicit switch does) so applying a
        # staged edit cannot put the broken pointer back.
        draft_config = draft_before.get("config_text", "")
        draft_auth = draft_before.get("auth_text", "{}\n")
        if isinstance(draft_config, str) and isinstance(draft_auth, str):
            try:
                self._draft = self._sync(draft_config, draft_auth, patch)
            except DomainError:
                self._draft = draft_before
        return True

    @staticmethod
    def _catalog_model_names(payload: Mapping[str, Any]) -> list[str]:
        return catalog_names_from_editor(payload)

    @staticmethod
    def _catalog_source_is_available(payload: Mapping[str, Any]) -> bool:
        """Whether the current LiteLLM endpoint probe completed successfully."""

        return payload.get("young_router_enabled") is True

    def _refresh_live_catalog_source(self, *, force: bool = False) -> bool:
        """Refresh the endpoint-backed catalog source without replacing drafts."""

        now = time.monotonic()
        if not force and now - self._catalog_source_checked_at < _CATALOG_SOURCE_REFRESH_SECONDS:
            return False
        self._catalog_source_checked_at = now
        try:
            payload = self._load_editor()
        except DomainError:
            exposed_models: list[str] = []
            source_available = False
            configured_models: list[dict[str, Any]] = []
        else:
            raw_exposed = payload.get("exposed_models", [])
            exposed_models = list(raw_exposed) if isinstance(raw_exposed, list) else []
            source_available = self._catalog_source_is_available(payload)
            raw_models = payload.get("models", [])
            configured_models = list(raw_models) if isinstance(raw_models, list) else []
        for state in (self._raw, self._draft):
            # A failed probe is not an observed empty model list.  Keep the
            # last verified names so a transient startup/reload failure cannot
            # erase the catalog and manufacture a Codex restart prompt.
            if source_available:
                state["exposed_models"] = copy.deepcopy(exposed_models)
                # The configured model list is the catalog allowlist.  Refresh
                # it alongside the exposure so a provider apply that adds or
                # removes models is visible on the next snapshot without a
                # Core restart.
                state["models"] = copy.deepcopy(configured_models)
            state["young_router_enabled"] = source_available
        return True

    def _catalog_model_ids_changed(self, names: list[str]) -> bool:
        """Return whether the Codex-visible model IDs changed, ignoring priority order."""

        existing_names = catalog_model_names(self.model_catalog_path)
        return existing_names is not None and set(existing_names) != set(names)

    @staticmethod
    def _catalog_signature(
        names: list[str] | tuple[str, ...],
        *,
        enabled: bool,
    ) -> tuple[bool, tuple[str, ...]]:
        # Catalog restart decisions are about model IDs, not endpoint order.
        return enabled, tuple(sorted(set(names)))

    def _reset_catalog_repair_observation(self) -> None:
        self._catalog_repair_observed_signature = None
        self._catalog_repair_observation_count = 0

    @staticmethod
    def _configured_model_names(payload: Mapping[str, Any]) -> set[str]:
        """Names of the models configured in the runtime model list."""

        names: set[str] = set()
        models = payload.get("models", [])
        if not isinstance(models, list):
            return names
        for entry in models:
            if not isinstance(entry, Mapping):
                continue
            name = entry.get("model")
            if isinstance(name, str) and name.strip():
                names.add(name.strip())
        return names

    def _queue_catalog_restart(
        self,
        reason: str,
        *,
        names: list[str] | tuple[str, ...] | None = None,
        enabled: bool | None = None,
        force: bool = False,
    ) -> bool:
        current_enabled = self._is_catalog_enabled(self._raw) if enabled is None else enabled
        current_names = names
        if current_names is None:
            current_names = self._catalog_model_names(self._raw) if current_enabled else []
        signature = self._catalog_signature(current_names, enabled=current_enabled)
        # Endpoint-backed repairs run through the shared stability gate in
        # ``_ensure_model_catalog_current``; explicit enable/disable actions
        # queue their restart immediately and bypass that observation gate.
        self._reset_catalog_repair_observation()
        if not force and signature == self._catalog_acknowledged_signature:
            return False
        if not force:
            acknowledged = self._catalog_acknowledged_signature
            if acknowledged is not None and acknowledged[0] == signature[0]:
                added = set(signature[1]) - set(acknowledged[1])
                dropped = set(acknowledged[1]) - set(signature[1])
                configured = self._configured_model_names(self._raw)
                # Worker views can flap a runtime-added route that is not
                # part of the configured public model set.  Repairing the
                # catalog file to match the live exposure is fine, but only
                # a change that involves configured names warrants a Codex
                # restart prompt.
                if not (added & configured) and not (dropped & configured):
                    return False
        if self._catalog_restart_required and signature == self._catalog_pending_signature:
            return False
        self._catalog_restart_required = True
        self._catalog_change_reason = reason
        self._catalog_change_event += 1
        self._catalog_pending_signature = signature
        return True

    def _remove_retired_catalog_copies(self) -> None:
        """Drop app-written catalogs the applied config no longer names.

        Historical spellings stay recognised so a config pointing at one is
        migrated rather than mistaken for a foreign file, but once the applied
        pointer names the current path those copies are dead app data and do
        not belong in the client's directory.
        """

        for path in self._managed_catalog_paths():
            if path == self.model_catalog_path:
                continue
            try:
                path.unlink()
            except OSError:
                pass

    def _ensure_model_catalog_current(
        self,
        *,
        notify: bool,
        force_source_refresh: bool = False,
        require_stable_repair: bool = False,
    ) -> bool:
        pointer = self._catalog_pointer(self._raw)
        if pointer is None or pointer not in self._managed_catalog_paths():
            self._reset_catalog_repair_observation()
            return False
        dangling = not pointer.exists()
        source_refreshed = self._refresh_live_catalog_source(force=force_source_refresh)
        if not self._catalog_source_is_available(self._raw):
            self._reset_catalog_repair_observation()
            if dangling and source_refreshed:
                # Codex cannot load any configuration whose catalog pointer names
                # a missing file.  A freshly observed endpoint failure means the
                # catalog cannot be rebuilt right now, so drop the pointer instead
                # of leaving the client unusable; the next available probe (or an
                # explicit switch) restores the catalog.
                if self._repair_catalog_pointer(None):
                    self._queue_catalog_restart("catalog_missing", names=[], enabled=False, force=True)
                    self.revision += 1
            return False
        names = self._catalog_model_names(self._raw)
        self._context_registry.refresh_if_due()
        model_ids_changed = self._catalog_model_ids_changed(names)
        if require_stable_repair and notify and model_ids_changed:
            if not source_refreshed:
                # Do not count repeated snapshots of the same cached probe as
                # separate endpoint observations.
                return False
            signature = self._catalog_signature(names, enabled=True)
            if signature != self._catalog_repair_observed_signature:
                self._catalog_repair_observed_signature = signature
                self._catalog_repair_observation_count = 1
                # A single endpoint view is not enough to rewrite the managed
                # catalog: worker reloads can briefly expose 9/10 or 10/9.
                return False
            self._catalog_repair_observation_count += 1
            if self._catalog_repair_observation_count < 2:
                return False
        else:
            self._reset_catalog_repair_observation()

        native_models = self._catalog_native_models()
        if native_models is None:
            self._reset_catalog_repair_observation()
            return False
        base_slug = self._catalog_base_profile(native_models)
        catalog_current = catalog_is_current(
            self.model_catalog_path,
            names,
            registry=self._context_registry,
            native_models=native_models,
            base_slug=base_slug,
        )
        migrated = pointer != self.model_catalog_path
        if catalog_current and not migrated:
            self._remove_retired_catalog_copies()
            return False
        if not catalog_current:
            write_catalog(
                self.model_catalog_path,
                names,
                registry=self._context_registry,
                native_models=native_models,
                base_slug=base_slug,
            )
        if migrated:
            # Write the current file before moving the pointer so a failure in
            # between leaves Codex with a loadable config, never a dangling one.
            if self._repair_catalog_pointer(str(self.model_catalog_path)):
                self._remove_retired_catalog_copies()
        else:
            self._remove_retired_catalog_copies()
        if notify and model_ids_changed:
            self._queue_catalog_restart("catalog_repaired", names=names, enabled=True)
        self.revision += 1
        return True

    def refresh_model_catalog(self) -> bool:
        """Refresh the enabled catalog after LiteLLM reloads its routes.

        Worker reloads briefly alternate between adjacent ``/v1/models``
        views.  Use the same two-observation stability gate as the snapshot
        path so a provider apply that did not change the exposed model set
        cannot manufacture a Codex restart prompt from a transient partial
        view.
        """

        return self._ensure_model_catalog_current(
            notify=True,
            force_source_refresh=True,
            require_stable_repair=True,
        )

    def refresh_local_api_endpoint(self) -> None:
        """Resolve this app's proxy endpoint for the "use this API" action."""

        try:
            from ._shared import local_proxy_endpoint

            self._local_api_base_url = local_proxy_endpoint()[0]
        except Exception:
            self._local_api_base_url = None

    def uses_local_api(self) -> bool:
        """True when the selected Codex provider already points at this proxy."""

        base_url = self._local_api_base_url
        if not base_url:
            return False
        expected = base_url.rstrip("/")
        structured = self._draft.get("structured")
        structured = structured if isinstance(structured, Mapping) else {}
        direct = str(structured.get("model_provider") or "").strip()
        if not direct:
            return False
        candidates: list[str] = []
        if direct == "openai":
            candidates.append(str(structured.get("openai_base_url") or ""))
        providers = structured.get("providers")
        if isinstance(providers, list):
            for item in providers:
                if isinstance(item, Mapping) and str(item.get("id") or "").strip() == direct:
                    candidates.append(str(item.get("base_url") or ""))
        return any(candidate.rstrip("/") == expected for candidate in candidates if candidate)

    def _safe_snapshot(self, payload: Mapping[str, Any], revision: int) -> dict[str, Any]:
        errors = payload.get("validation_errors", [])
        warnings = payload.get("warnings", [])
        if self._catalog_pointer_error:
            warnings = [*(warnings if isinstance(warnings, list) else []), self._catalog_pointer_error]
        public_models = self._catalog_model_names(payload) if self._is_catalog_enabled(payload) else []
        return {
            "domain": "codex",
            "revision": revision,
            "config_exists": bool(payload.get("config_exists")),
            "auth_file_exists": bool(payload.get("auth_exists")),
            "uses_local_api": self.uses_local_api(),
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
        self._ensure_model_catalog_current(notify=True, require_stable_repair=True)
        return self._safe_snapshot(self._draft, self.revision)

    def draft_state(self) -> object:
        # Live model-catalog metadata is refreshed from LiteLLM while the
        # window is open. It is a read-only projection, not a Codex document
        # edit, so it must not participate in Core's dirty-state comparison.
        return {
            "config_text": str(self._draft.get("config_text", "")),
            "auth_text": str(self._draft.get("auth_text", "{}\n")),
        }

    def _sync(self, config_text: str, auth_text: str, patch: object | None = None) -> dict[str, Any]:
        from .. import codex_config

        payload: dict[str, Any] = {"config_text": config_text, "auth_text": auth_text}
        if patch is not None:
            payload["patch"] = copy.deepcopy(patch)
        try:
            with _codex_environment(self.runtime_config_path, self.codex_home):
                result = codex_config.sync_editor(payload, self.runtime_config_path)
        except Exception as exc:
            raise _safe_problem(exc, "Codex settings are invalid") from None
        if not isinstance(result, Mapping) or not isinstance(result.get("config_text"), str) or not isinstance(result.get("auth_text"), str):
            raise DomainError("Codex settings are invalid")
        synced = copy.deepcopy(dict(result))
        # ``sync_editor`` validates and rewrites an in-memory draft without
        # touching CODEX_HOME, so its payload intentionally has no filesystem
        # existence fields. Those fields describe the loaded disk baseline,
        # not whether the draft contains text, and must survive every staged
        # structured/raw edit until Apply or Reload establishes a new baseline.
        synced["config_exists"] = bool(self._raw.get("config_exists"))
        synced["auth_exists"] = bool(self._raw.get("auth_exists"))
        return synced

    def _saved_model_patch(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """Build a direct-connection patch for one saved provider route.

        This app already stores every route's endpoint and key.  Restoring a
        client onto one of them writes those exact values under the provider
        ``model_provider`` already names, so the client talks to the provider
        itself instead of this app's proxy — the counterpart of the "use this
        API" action — and the client's own provider identity never changes.
        """

        from .. import codex_config

        model = str(data.get("model") or "").strip()
        provider = str(data.get("provider") or "").strip()
        deployment_id = str(data.get("deployment_id") or "").strip()
        if not model or not provider:
            raise DomainError("Select one saved Young Router model")
        try:
            runtime = codex_config.load_yaml(self.runtime_config_path)
            rows = codex_config.configured_models(runtime)
        except Exception as exc:
            raise _safe_problem(exc, "The saved Young Router models could not be read") from None
        candidates = [
            row
            for row in rows
            if row.get("model") == model
            and row.get("provider") == provider
            and (not deployment_id or row.get("deployment_id") == deployment_id)
        ]
        if len(candidates) != 1:
            raise DomainError("Select one current Young Router model")
        row = candidates[0]
        base_url = str(row.get("api_base") or "").strip()
        if not base_url:
            raise DomainError("The selected model has no saved provider endpoint")
        surface = str(row.get("upstream_url_surface") or "").strip().lower()
        patch = self._active_provider_patch(
            base_url=base_url,
            key=self._saved_model_api_key(runtime, str(row.get("deployment_id") or "")),
            wire_api="responses" if surface.endswith("responses") else "chat",
        )
        if not patch.get("api_key"):
            patch.pop("api_key", None)
        patch["model"] = _direct_model_id(str(row.get("upstream_model") or model))
        return patch

    @staticmethod
    def _saved_model_api_key(runtime: object, deployment_id: str) -> str:
        """The route's own key from the runtime model list, when it has one."""

        if not deployment_id or not isinstance(runtime, Mapping):
            return ""
        entries = runtime.get("model_list")
        if not isinstance(entries, list):
            return ""
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            info = entry.get("model_info")
            if not isinstance(info, Mapping) or str(info.get("id") or "").strip() != deployment_id:
                continue
            params = entry.get("litellm_params")
            value = params.get("api_key") if isinstance(params, Mapping) else None
            return value.strip() if isinstance(value, str) else ""
        return ""

    def dispatch(self, action: str, payload: object | None = None) -> dict[str, Any]:
        name = _action_name(action)
        data = _mapping(payload or {})
        config_text = self._draft.get("config_text", "")
        auth_text = self._draft.get("auth_text", "{}\n")
        if not isinstance(config_text, str) or not isinstance(auth_text, str):
            raise DomainError("Codex settings are invalid")
        if name in {"acknowledge_model_catalog_restart", "acknowledgemodelcatalogrestart"}:
            self._catalog_restart_required = False
            self._catalog_change_reason = None
            acknowledged = self._catalog_pending_signature
            if acknowledged is None:
                enabled = self._is_catalog_enabled(self._raw)
                names = self._catalog_model_names(self._raw) if enabled else []
                acknowledged = self._catalog_signature(names, enabled=enabled)
            self._catalog_acknowledged_signature = acknowledged
            self._catalog_pending_signature = None
            self._reset_catalog_repair_observation()
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
                raise DomainError("Codex editor text must be text")
            self._draft = self._sync(next_config, next_auth)
        elif name in {"patch", "select_model"}:
            if "config_text" in data or "auth_text" in data or "raw_toml" in data or "raw_json" in data:
                next_config = data.get("config_text", data.get("raw_toml", config_text))
                next_auth = data.get("auth_text", data.get("raw_json", auth_text))
                if not isinstance(next_config, str) or not isinstance(next_auth, str):
                    raise DomainError("Codex editor text must be text")
                self._draft = self._sync(next_config, next_auth)
            else:
                patch = data
                if name == "select_model":
                    patch = {"litellm_model": data.get("selection")}
                self._draft = self._sync(config_text, auth_text, patch)
        elif name in {"use_local_api", "uselocalapi"}:
            # Point the external client at this app's own proxy through the
            # provider row this app owns, and adopt its master key.  The pane
            # names the route the client leaves behind: the proxy serves the
            # route's public model name, so keeping the client's own provider
            # model id would leave it asking for a model the proxy cannot
            # resolve.
            patch = self._local_api_patch()
            model = str(data.get("model") or "").strip()
            if model:
                patch["model"] = model
            self._draft = self._sync(config_text, auth_text, patch)
        elif name in {"use_saved_model", "usesavedmodel"}:
            # Restore the client directly onto one saved provider route: the
            # chosen model, that provider's own endpoint, and the route's key.
            self._draft = self._sync(config_text, auth_text, self._saved_model_patch(data))
        elif name in {"reset", "cancel", "reload", "restore_defaults"}:
            self._draft = copy.deepcopy(self._raw)
        else:
            raise DomainError("The requested Codex action is unavailable")
        self.revision += 1
        return self.snapshot()

    def secret_present(self, field: str, target: str | None = None) -> bool:
        if field != "api_key" or target is not None:
            raise DomainError("The requested secret field is unavailable")
        structured = self._draft.get("structured", {})
        return isinstance(structured, Mapping) and bool(structured.get("api_key"))

    def trusted_secret_value(self, field: str, target: str | None = None) -> str:
        if field != "api_key" or target is not None:
            raise DomainError("The requested secret field is unavailable")
        structured = self._draft.get("structured", {})
        value = structured.get("api_key", "") if isinstance(structured, Mapping) else ""
        if not isinstance(value, str):
            raise DomainError("The requested secret field is unavailable")
        return value

    def stage_secret(self, field: str, target: str | None, value: str) -> None:
        if field != "api_key" or target is not None:
            raise DomainError("The requested secret field is unavailable")
        config_text = self._draft.get("config_text", "")
        auth_text = self._draft.get("auth_text", "{}\n")
        if not isinstance(config_text, str) or not isinstance(auth_text, str):
            raise DomainError("Codex settings are invalid")
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
                draft = self._sync(config_text, auth_text, data.get("patch"))
            except DomainError:
                return {"valid": False, "errors": ["Codex settings are invalid"]}
        errors = draft.get("validation_errors", [])
        return {"valid": not bool(errors), "errors": list(errors) if isinstance(errors, list) else ["Codex settings are invalid"]}

    def apply(self, payload: object | None = None) -> dict[str, Any]:
        from .. import codex_config

        if payload is not None:
            data = _mapping(payload)
            if "config_text" in data or "auth_text" in data:
                self.dispatch("set_raw", data)
        validation = self.validate()
        if not validation["valid"]:
            raise DomainError("Codex settings are invalid")
        current = self._load_editor()
        if (current.get("config_text"), current.get("auth_text")) != self._baseline:
            raise DomainError("Codex settings changed on disk; reload and try again")
        was_enabled = self._is_catalog_enabled(self._raw)
        will_be_enabled = self._is_catalog_enabled(self._draft)
        catalog_models: list[str] | None = None
        catalog_changed = False
        catalog_model_ids_changed = False
        native_models: list[Mapping[str, Any]] | None = None
        base_slug: str | None = None
        if will_be_enabled:
            self._refresh_live_catalog_source(force=True)
            source_available = self._catalog_source_is_available(self._draft)
            if not source_available:
                if not was_enabled:
                    raise DomainError("Young Router is not running or exposes no models")
            else:
                catalog_models = self._catalog_model_names(self._draft)
                if not was_enabled and not catalog_models:
                    raise DomainError("Young Router is not running or exposes no models")
                self._context_registry.refresh_if_due()
                native_models = self._catalog_native_models()
                if native_models is not None:
                    base_slug = self._catalog_base_profile(native_models)
                    catalog_changed = not catalog_is_current(
                        self.model_catalog_path,
                        catalog_models,
                        registry=self._context_registry,
                        native_models=native_models,
                        base_slug=base_slug,
                    )
                catalog_model_ids_changed = self._catalog_model_ids_changed(catalog_models)
        if catalog_changed and catalog_models is not None and native_models is not None:
            write_catalog(
                self.model_catalog_path,
                catalog_models,
                registry=self._context_registry,
                native_models=native_models,
                base_slug=base_slug,
            )
        try:
            self._write_documents(self._draft)
        except DomainError:
            raise
        except Exception as exc:
            raise _safe_problem(exc, "Codex settings could not be saved") from None
        self.reload()
        if will_be_enabled and (not was_enabled or catalog_model_ids_changed):
            names = catalog_models if catalog_models is not None else self._catalog_model_names(self._raw)
            self._queue_catalog_restart(
                "enabled" if not was_enabled else "catalog_repaired",
                names=names,
                enabled=True,
                force=not was_enabled,
            )
            self.revision += 1
        elif was_enabled and not will_be_enabled:
            self._queue_catalog_restart("disabled", names=[], enabled=False, force=True)
            self.revision += 1
        return {"applied": True, **self.snapshot()}

    def catalog_baseline_state(self) -> object:
        """Return the applied Codex editor state for an immediate menu action."""

        return copy.deepcopy(self._raw)

    def set_model_catalog_enabled_immediately(self, enabled: bool) -> dict[str, Any]:
        """Toggle only the managed catalog, preserving unrelated staged edits."""

        if not isinstance(enabled, bool):
            raise DomainError("The Codex model catalog switch is invalid")
        from .. import codex_config

        current = self._load_editor()
        if (current.get("config_text"), current.get("auth_text")) != self._baseline:
            raise DomainError("Codex settings changed on disk; reload and try again")
        was_enabled = self._is_catalog_enabled(self._raw)
        catalog_models: list[str] | None = None
        catalog_changed = False
        catalog_model_ids_changed = False
        native_models: list[Mapping[str, Any]] | None = None
        base_slug: str | None = None
        if enabled:
            self._refresh_live_catalog_source(force=True)
            source_available = self._catalog_source_is_available(self._raw)
            if not source_available:
                if not was_enabled:
                    raise DomainError("Young Router is not running or exposes no models")
            else:
                catalog_models = self._catalog_model_names(self._raw)
                if not was_enabled and not catalog_models:
                    raise DomainError("Young Router is not running or exposes no models")
                self._context_registry.refresh_if_due()
                native_models = self._catalog_native_models()
                if native_models is not None:
                    base_slug = self._catalog_base_profile(native_models)
                    catalog_changed = not catalog_is_current(
                        self.model_catalog_path,
                        catalog_models,
                        registry=self._context_registry,
                        native_models=native_models,
                        base_slug=base_slug,
                    )
                catalog_model_ids_changed = self._catalog_model_ids_changed(catalog_models)
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
            except DomainError:
                # A raw editor may intentionally contain an invalid draft. Keep
                # that draft intact while the menu action updates the applied
                # document below.
                draft_next = draft_before
        if catalog_changed and catalog_models is not None and native_models is not None:
            write_catalog(
                self.model_catalog_path,
                catalog_models,
                registry=self._context_registry,
                native_models=native_models,
                base_slug=base_slug,
            )
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
        if enabled and (not was_enabled or catalog_model_ids_changed):
            names = catalog_models if catalog_models is not None else self._catalog_model_names(self._raw)
            self._queue_catalog_restart(
                "enabled" if not was_enabled else "catalog_repaired",
                names=names,
                enabled=True,
                force=not was_enabled,
            )
            self.revision += 1
        elif was_enabled and not enabled:
            self._queue_catalog_restart("disabled", names=[], enabled=False, force=True)
            self.revision += 1
        return self.snapshot()

    def external_disk_state(self) -> dict[str, bool]:
        """Report only whether either private Codex document changed."""

        config_text, auth_text, config_exists, auth_exists = self._editor_documents_from_disk()
        identity = (config_text, auth_text)
        return {
            "changed": identity != self._baseline,
            "exists": config_exists or auth_exists,
        }

    def external_disk_identity(self) -> str:
        """Return an opaque identity without exposing either private document."""

        config_text, auth_text, _, _ = self._editor_documents_from_disk()
        config = config_text.encode("utf-8")
        auth = auth_text.encode("utf-8")
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

    def _active_provider_patch(self, *, base_url: str, key: str, wire_api: str | None = None) -> dict[str, Any]:
        """Point the client's *current* provider at one endpoint.

        Codex names its gateway in ``model_provider``, so both client actions
        follow that field instead of adding a provider of their own: the row it
        names keeps its identity and only its endpoint (plus the API surface a
        direct route needs) changes.  A selection of the built-in OpenAI
        provider has no editable table, so it takes Codex's own
        ``openai_base_url``; a selection with no editable endpoint at all (an
        empty or Bedrock-style built-in) falls back to the one provider row
        this app owns, because there is nothing to follow.
        """

        structured = self._draft.get("structured")
        structured = structured if isinstance(structured, Mapping) else {}
        active = str(structured.get("model_provider") or "").strip()
        if active == "openai":
            return {"api_key": key, "direct_connection": {"provider": "openai", "base_url": base_url}}
        providers = [
            copy.deepcopy(dict(item))
            for item in structured.get("providers") or []
            if isinstance(item, Mapping)
        ]
        entry = (
            next(
                (item for item in providers if str(item.get("id") or "").strip() == active),
                None,
            )
            if active
            else None
        )
        if entry is not None:
            entry["base_url"] = base_url
            if wire_api is not None:
                entry["wire_api"] = wire_api
            entry["auth_mode"] = "openai_auth"
            return {"api_key": key, "providers": providers}
        entry = next(
            (item for item in providers if str(item.get("id") or "").strip() == LOCAL_API_PROVIDER_ID),
            None,
        )
        if entry is None:
            entry = {"id": LOCAL_API_PROVIDER_ID, "name": LOCAL_API_PROVIDER_ID}
            providers.append(entry)
        entry.update(
            {
                "base_url": base_url,
                "wire_api": wire_api or "responses",
                "auth_mode": "openai_auth",
            }
        )
        return {"api_key": key, "model_provider": LOCAL_API_PROVIDER_ID, "providers": providers}

    @staticmethod
    def _active_provider_endpoint(structured: Mapping[str, Any], active: str) -> str:
        """The endpoint the client's selected provider currently points at."""

        if active == "openai":
            return str(structured.get("openai_base_url") or "").strip()
        for item in structured.get("providers") or []:
            if isinstance(item, Mapping) and str(item.get("id") or "").strip() == active:
                return str(item.get("base_url") or "").strip()
        return ""

    def _local_api_patch(self) -> dict[str, Any]:
        """The patch that moves the client's current provider onto this proxy."""

        from ._shared import local_proxy_endpoint

        base_url, key = local_proxy_endpoint()
        self._local_api_base_url = base_url
        return self._active_provider_patch(base_url=base_url, key=key, wire_api="responses")

    def follow_local_api_endpoint(self) -> bool:
        """Rewrite a client that uses this app's proxy onto its current endpoint.

        The runtime settings own the proxy's port and its master key, so a
        client that already talks to the proxy has to follow both: otherwise
        the external tool keeps calling a closed port or presents a retired
        key after the next restart.  Only the provider row the client actually
        uses and the client's own key are rewritten — the model, the client's
        other backends, and every unrelated line stay exactly as they are —
        and a row that is not this app's proxy is never touched.  Returns
        whether the client configuration was written.
        """

        from ._shared import local_proxy_endpoint

        previous_base_url = self._local_api_base_url
        base_url, key = local_proxy_endpoint()
        self._local_api_base_url = base_url
        structured = self._draft.get("structured")
        structured = structured if isinstance(structured, Mapping) else {}
        active = str(structured.get("model_provider") or "").strip()
        if not active:
            return False
        current = self._active_provider_endpoint(structured, active)
        # The client is on this proxy when the provider ``model_provider``
        # names still carries the endpoint this process resolved before the
        # change; any other endpoint belongs to the user's own provider and is
        # left alone.
        if not current or not previous_base_url or current.rstrip("/") != previous_base_url.rstrip("/"):
            return False
        current_key = str(structured.get("api_key") or "").strip()
        if current.rstrip("/") == base_url.rstrip("/") and current_key == key:
            return False
        patch = self._active_provider_patch(base_url=base_url, key=key)
        config_text, auth_text, _, _ = self._editor_documents_from_disk()
        written = self._sync(config_text, auth_text, patch)
        if isinstance(self._draft.get("config_text"), str) and isinstance(self._draft.get("auth_text"), str):
            staged = self._sync(str(self._draft["config_text"]), str(self._draft["auth_text"]), patch)
        else:
            staged = written
        self._write_documents(written)
        self._raw = copy.deepcopy(written)
        self._draft = copy.deepcopy(staged)
        self._baseline = (str(written.get("config_text", "")), str(written.get("auth_text", "{}\n")))
        self.revision += 1
        return True

    def _write_documents(self, documents: Mapping[str, Any]) -> None:
        """Write one pair of editor documents to the client's configuration."""

        from .. import codex_config

        try:
            with _codex_environment(self.runtime_config_path, self.codex_home):
                codex_config.apply_editor(
                    {
                        "config_text": str(documents.get("config_text", "")),
                        "auth_text": str(documents.get("auth_text", "{}\n")),
                    },
                    self.runtime_config_path,
                )
        except Exception as exc:
            raise _safe_problem(exc, "Codex settings could not be saved") from None

    def reload(self) -> dict[str, Any]:
        self.refresh_local_api_endpoint()
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
