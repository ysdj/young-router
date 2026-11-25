"""Codex staged settings domain."""

from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time
import tomllib
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any, Iterator

from .. import model_catalog
from ..model_catalog import (
    MAX_SERVED_CATALOG_BYTES,
    PLUGIN_CATALOG_FILE_NAME,
    RETIRED_SERVED_CATALOG_ROUTES,
    SERVED_CATALOG_ROUTE,
    catalog_carries_native_profile,
    catalog_is_current,
    catalog_model_names,
    catalog_names_from_editor,
    catalog_is_usable,
    catalog_payload,
    legacy_managed_catalog_path,
    managed_catalog_path,
    previous_managed_catalog_path,
    profile_has_instructions,
    select_inherited_profile,
    served_catalog_body,
    write_catalog,
)
from ..model_contexts import (
    ModelContextRegistry,
    default_context_cache_path,
    legacy_context_cache_paths,
)
from ..persistence import PersistenceError, read_json
from ..security import redact
from ..codex_config import RESERVED_PROVIDER_IDS
from ._shared import (
    DomainError,
    _action_name,
    _app_state_root,
    _default_provider_config_path,
    _default_runtime_settings_path,
    _mapping,
    _safe_problem,
)
from ._shared_api import (
    public_direct_api_url,
    read_direct_connection,
    store_direct_connection,
)

_CODEX_ENVIRONMENT_LOCK = threading.RLock()
_CATALOG_SOURCE_REFRESH_SECONDS = 0.5
#: How long one confirmed answer from the served catalog address is trusted.
#: Resolution is a loopback round trip inside this process's own child; this
#: only keeps a burst of refresh passes from repeating it.
_CATALOG_URL_PROBE_SECONDS = 20.0
#: The client bounds its own fetch of an explicit catalog; so does this probe.
_CATALOG_URL_PROBE_TIMEOUT_SECONDS = 1.0
#: A catalog body larger than Codex's own 1 MiB bound would be refused there too,
#: so this never reads more than that from its own listener.
_CATALOG_URL_PROBE_MAX_BYTES = 1024 * 1024
#: The plugin id carries its name as a prefix (``<name>@<marketplace>``).
PLUGIN_NAME_TOKEN = "model-list-plugin"
_CODEX_APP_RESOURCES = (
    Path("/Applications/ChatGPT.app/Contents/Resources"),
    Path("/Applications/Codex.app/Contents/Resources"),
    Path.home() / "Applications" / "ChatGPT.app" / "Contents" / "Resources",
    Path.home() / "Applications" / "Codex.app" / "Contents" / "Resources",
)
_BUNDLED_MARKETPLACE = Path("plugins") / "openai-bundled"

# App-private bookkeeping for the managed catalog.  Codex only ever reads the
# catalog itself (through ``model_catalog_json``): the pinned inheritance
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
    """Pick the app-state directory for a Codex domain."""

    return _app_state_root(runtime_root, runtime_settings_path, runtime_config_path)

# Keep the form's direct connection separate from the managed proxy provider.
LOCAL_API_PROVIDER_ID = "custom"
DIRECT_API_PROVIDER_ID = "young-router-direct"

def _installed_codex_cli(codex_home: Path) -> Path | None:
    """Find the Codex executable belonging to this home, then the installed app."""

    candidates = [
        codex_home / "plugins" / "cache" / "openai-bundled" / "codex-cli"
        / "CodexCLI.app" / "Contents" / "MacOS" / "codex",
    ]
    candidates.extend(
        resources / "codex-cli" / "CodexCLI.app" / "Contents" / "MacOS" / "codex"
        for resources in _CODEX_APP_RESOURCES
    )
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def _codex_cli(codex_home: Path) -> Path:
    """Return the installed Codex CLI or explain why its plugins cannot be installed."""

    executable = _installed_codex_cli(codex_home)
    if executable is None:
        raise DomainError("Codex is not installed, so its Browser and Computer Use plugins cannot be enabled")
    return executable


def _ensure_browser_plugins(
    codex_home: Path,
    codex_cli: Path,
    plugin_ids: Sequence[str],
) -> list[dict[str, Any]]:
    """Install and enable the requested bundled plugins.

    Reading only the plugin bundle leaves an empty ``skills`` directory in
    Codex's cache after an application restart.  The Codex-owned installer
    materializes those files and its plugin registry together, so every new
    desktop chat can discover Browser and Computer Use.
    """

    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    plugins: list[dict[str, Any]] = []
    for plugin_id in plugin_ids:
        result = subprocess.run(
            [str(codex_cli), "plugin", "add", plugin_id, "--json"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )
        try:
            installed = json.loads(result.stdout)
        except ValueError:
            installed = None
        if result.returncode != 0 or not isinstance(installed, Mapping):
            detail = result.stderr.strip() or result.stdout.strip()
            raise DomainError(
                f"Codex plugin {plugin_id} could not be installed"
                + (f": {detail}" if detail else "")
            )
        plugins.append({"id": plugin_id, "enabled": True})
    return plugins


def _installed_surface_plugins(
    codex_home: Path,
) -> tuple[dict[str, str], str | None]:
    """Return ``feature -> plugin id`` from the installed Desktop runtime."""

    resources = next(
        (
            path
            for path in _CODEX_APP_RESOURCES
            if (path / _BUNDLED_MARKETPLACE / ".agents" / "plugins" / "marketplace.json").is_file()
        ),
        None,
    )
    if resources is None:
        return {}, None

    marketplace_path = (
        resources / _BUNDLED_MARKETPLACE / ".agents" / "plugins"
        / "marketplace.json"
    )
    try:
        marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DomainError("Codex bundled plugin marketplace is malformed") from exc
    entries = marketplace.get("plugins") if isinstance(marketplace, Mapping) else None
    if not isinstance(entries, list):
        raise DomainError("Codex bundled plugin marketplace is malformed")

    plugins: dict[str, tuple[str, Path]] = {}
    for item in entries:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        source = item.get("source")
        relative = source.get("path") if isinstance(source, Mapping) else None
        if not isinstance(name, str) or not isinstance(relative, str):
            continue
        root = (resources / _BUNDLED_MARKETPLACE / relative).resolve()
        if root.is_dir():
            plugins[name] = (relative, root)

    runtime: tuple[str, str, Path] | None = None
    for name, (relative, root) in plugins.items():
        config_path = resources / _BUNDLED_MARKETPLACE / relative / ".mcp.json"
        if not config_path.is_file():
            continue
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        servers = config.get("mcpServers") if isinstance(config, Mapping) else None
        if not isinstance(servers, Mapping) or "cua_repl" not in servers:
            continue
        server = servers.get("cua_repl")
        env = server.get("env") if isinstance(server, Mapping) else None
        surfaces = env.get("CUA_REPL_ENABLED_SURFACES") if isinstance(env, Mapping) else None
        if not isinstance(surfaces, str):
            surfaces = _runtime_surfaces_from_cache(
                # The cache is keyed by the marketplace's *name*; its first
                # path part is "plugins", which named a directory that is never
                # there, so this fallback found nothing.
                codex_home / "plugins" / "cache" / _BUNDLED_MARKETPLACE.name,
                name,
            )
        if isinstance(surfaces, str):
            runtime = (name, surfaces, root)
            break
    if runtime is None:
        return {}, None

    runtime_name, surfaces, _ = runtime
    feature_plugins: dict[str, str] = {}
    for surface in (value.strip() for value in surfaces.split(",")):
        if not surface:
            continue
        feature = f"{surface}_use"
        plugin_name = next(
            (
                name for name, (_, root) in plugins.items()
                if _plugin_serves_surface(root, surface)
            ),
            None,
        )
        if plugin_name is not None:
            feature_plugins[feature] = _bundled_plugin_id(plugin_name)
    return feature_plugins, _bundled_plugin_id(runtime_name)


def _plugin_serves_surface(root: Path, surface: str) -> bool:
    skills = root / "skills"
    if not skills.is_dir():
        return False
    return any(
        path.is_dir() and (path.name == surface or surface in path.name)
        for path in skills.iterdir()
    )


def _bundled_plugin_id(name: str) -> str:
    return name if "@" in name else f"{name}@openai-bundled"


def _runtime_surfaces_from_cache(cache_root: Path, plugin_name: str) -> str | None:
    for path in sorted(cache_root.glob(f"{plugin_name}/*/.mcp.json")):
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        servers = config.get("mcpServers") if isinstance(config, Mapping) else None
        server = servers.get("cua_repl") if isinstance(servers, Mapping) else None
        env = server.get("env") if isinstance(server, Mapping) else None
        surfaces = env.get("CUA_REPL_ENABLED_SURFACES") if isinstance(env, Mapping) else None
        if isinstance(surfaces, str):
            return surfaces
    return None


def _surface_sync_patch(
    codex_home: Path,
    features: Mapping[str, Any],
    plugins: Sequence[Mapping[str, Any]],
    *,
    override: Mapping[str, bool] | None = None,
    sync_plugins: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Synchronize discovered surface plugins with explicit Codex choices.

    An absent feature keeps its absent state. An explicit ``true`` enables the
    matching plugin and runtime; an explicit ``false`` disables them. The
    feature/plugin mapping comes from the installed Desktop release, so new
    surfaces need no Young Router allowlist.
    """

    feature_plugins, runtime_id = _installed_surface_plugins(codex_home)
    if not feature_plugins:
        if override and any(value is True for value in override.values()):
            raise DomainError("Codex did not declare the requested desktop surface")
        return dict(override or {})

    effective = dict(features)
    if override is not None:
        effective.update(override)
    if override and runtime_id is not None:
        missing = set(override) - set(feature_plugins)
        if missing and any(value is True for value in override.values()):
            raise DomainError("Codex did not declare the requested desktop surface")
    selected_features = {
        feature
        for feature in feature_plugins
        if sync_plugins is None or feature in sync_plugins
    }
    selected_override = {
        feature: state
        for feature, state in (override or {}).items()
        if feature in feature_plugins
    }
    if sync_plugins is not None and override is None and not selected_features:
        return {}
    plugin_states = {
        str(item.get("id") or ""): item.get("enabled")
        for item in plugins
    }
    desired_plugins = {
        plugin_id: plugin_states.get(plugin_id) is True
        for feature, plugin_id in feature_plugins.items()
        if feature in selected_features
    }
    for feature in selected_features:
        plugin_id = feature_plugins[feature]
        state = effective.get(feature)
        if state is True:
            desired_plugins[plugin_id] = True
        elif state is False:
            desired_plugins[plugin_id] = False

    explicit = [effective.get(feature) for feature in selected_features]
    desired_runtime = (
        True if any(state is True for state in explicit)
        else False if explicit and all(state is False for state in explicit)
        else plugin_states.get(runtime_id) if runtime_id is not None else None
    )

    patch_plugins = [
        {"id": plugin_id, "enabled": enabled}
        for plugin_id, enabled in sorted(desired_plugins.items())
        if (
            (enabled and plugin_states.get(plugin_id) is not True)
            or (not enabled and plugin_states.get(plugin_id) is True)
        )
    ]
    if runtime_id is not None and desired_runtime is not None:
        if plugin_states.get(runtime_id) is not desired_runtime:
            patch_plugins.append({"id": runtime_id, "enabled": desired_runtime})

    install = sorted(
        plugin_id
        for plugin_id, enabled in desired_plugins.items()
        if enabled and plugin_states.get(plugin_id) is not True
    )
    if desired_runtime is True and runtime_id is not None and plugin_states.get(runtime_id) is not True:
        install.append(runtime_id)
    if install:
        _ensure_browser_plugins(
            codex_home,
            _codex_cli(codex_home),
            sorted(set(install)),
        )

    patch: dict[str, Any] = {}
    if patch_plugins:
        patch["plugins"] = patch_plugins
    if selected_override:
        patch["features"] = selected_override
    return patch


# The MCP server Young Router registers for the model's fetch tool.  It carries
# this app's own name and its argument list, so nothing a user configured for
# another purpose is ever mistaken for it, and only this entry is added or
# removed when the image-context setting changes.
IMAGE_CONTEXT_SERVER_ID = "young_router_image_context"
IMAGE_CONTEXT_SERVER_ARGS = [
    "-m",
    "young_router.proxy.image_context_mcp",
]


def image_context_mcp_registered(config: Mapping[str, Any]) -> bool:
    """Whether Codex already lists this app's image-context fetch server."""

    raw = (config or {}).get("mcp_servers")
    servers = raw if isinstance(raw, Mapping) else {}
    if not servers and isinstance(raw, Sequence):
        # ``structured`` lists servers as ``[{id, ...}]``; the parsed TOML they
        # come from is a table keyed by id.  Accept both so the caller never has
        # to know which projection it is holding.
        servers = {
            str(item.get("id")): item
            for item in raw
            if isinstance(item, Mapping) and item.get("id")
        }
    server = servers.get(IMAGE_CONTEXT_SERVER_ID)
    return isinstance(server, Mapping) and bool(
        str(server.get("command") or "").strip()
    )


def image_context_mcp_patch(
    config: Mapping[str, Any],
    *,
    enabled: bool,
    python: str,
    python_path: str = "",
) -> dict[str, Any]:
    """The patch that adds or removes the fetch server without touching others.

    The server is a stdio entry that runs this installation's interpreter, so a
    bundled desktop app resolves its own runtime instead of a ``python`` the
    user happens to have on ``PATH``.  It is registered with
    ``required = false``: a session whose interpreter is missing still starts,
    and the model simply gets no fetch tool.
    """

    # ``mcp_servers`` in a structured patch is the complete list: any server
    # absent from it is removed.  So the patch always states every server the
    # file already has, and only this app's own entry gains or loses anything.
    retained = [
        {"id": str(item.get("id"))}
        for item in _mcp_server_entries(config)
        if str(item.get("id")) != IMAGE_CONTEXT_SERVER_ID
    ]
    if enabled:
        entry: dict[str, Any] = {
            "id": IMAGE_CONTEXT_SERVER_ID,
            "command": python,
            "args": list(IMAGE_CONTEXT_SERVER_ARGS),
            "transport": "stdio",
            "required": False,
        }
        if python_path:
            # The client starts this server itself, without this app's launcher,
            # so the one thing the launcher would have exported has to travel in
            # the entry: a bare ``-m young_router…`` cannot find the package
            # otherwise, and the tool would fail to start in every session.
            entry["env"] = {"PYTHONPATH": python_path}
        return {"mcp_servers": [entry, *retained]}
    if not image_context_mcp_registered(config):
        return {}
    return {"mcp_servers": retained}


def _mcp_server_entries(config: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every MCP server of a Codex document, as ``[{id, …}]``."""

    raw = (config or {}).get("mcp_servers")
    if isinstance(raw, Mapping):
        return [
            {**item, "id": str(server_id)}
            for server_id, item in raw.items()
            if isinstance(item, Mapping)
        ]
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return [item for item in raw if isinstance(item, Mapping) and item.get("id")]
    return []


def _image_context_python_path() -> str:
    """The import path the client's own process must carry for the tool.

    A bundled Core runs from ``Contents/Resources/Core`` with its dependencies
    in ``runtime/site-packages``; nothing puts either on a child's path unless
    the launcher does, and this child is started by the client instead.
    """

    roots: list[str] = []
    for entry in sys.path:
        if not entry:
            continue
        normalised = str(entry).rstrip("/")
        if normalised.endswith("site-packages") or normalised.endswith("Resources/Core"):
            if normalised not in roots:
                roots.append(normalised)
    return os.pathsep.join(roots)


def image_context_settings(path: Path | None = None) -> dict[str, Any]:
    """Read the runtime settings that decide this pass.

    Read at dispatch time rather than cached: the settings file is the
    operator's, and a value changed there must reach the next write.
    ``path`` is the domain's configured settings file; without it the home
    default is read.
    """

    from .. import runtime_settings_io

    values: Mapping[str, Any] = {}
    try:
        values = runtime_settings_io.read_settings_file(
            path or _default_runtime_settings_path(), runtime_settings_io.load_specs()
        )
    except Exception:
        values = {}
    return {
        "enabled": str(values.get("YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_ENABLED") or "").strip()
        in {"1", "true", "TRUE", "True"},
        "fetch": str(values.get("YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_FETCH") or "auto").strip().lower(),
    }


def _remove_legacy_computer_mcp(text: str) -> str:
    """Drop the pre-unified Computer Use server without touching other MCPs.

    The desktop app supplies ``cua_repl`` through its plugin runtime now, while
    older Young Router settings also wrote a standalone Computer Use server.
    Keeping both makes the Browser and Computer Use skills target a runtime
    that the conversation does not expose.
    """

    lines = text.splitlines(keepends=True)
    in_target = False
    retained: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_target = (
                stripped == "[mcp_servers.computer-use]"
                or stripped.startswith("[mcp_servers.computer-use.")
            )
        if not in_target:
            retained.append(line)
    now = tomllib.loads("".join(retained))
    if isinstance(now.get("mcp_servers"), Mapping):
        servers = now.get("mcp_servers", {})
        if isinstance(servers, Mapping) and not servers:
            retained = [
                line for line in retained
                if line.strip() != "[mcp_servers]"
            ]
    return "".join(retained)


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


def _is_loopback_address(value: str) -> bool:
    from urllib.parse import urlsplit

    try:
        return (urlsplit(value.strip()).hostname or "").lower() in {"127.0.0.1", "::1", "localhost"}
    except ValueError:
        return False


def _same_origin(left: str, right: str) -> bool:
    """Whether two addresses name the same scheme, host and port.

    Used for the catalog address only: ``/v1/models`` is a path every gateway
    serves, so "is this ours" is answered by the row's own inference endpoint
    rather than by the path alone.  A stepped start moves both together, so an
    address left on the previous port still compares equal to nothing and is
    rewritten by the same rule that writes a new row.
    """

    from urllib.parse import urlsplit

    def parts(value: str):
        parsed = urlsplit(value.strip())
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        port = parsed.port or (443 if scheme == "https" else 80)
        return scheme, host, port

    try:
        return parts(left) == parts(right)
    except ValueError:
        return False


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
        # This app's own bookkeeping about a client -- the route it was last put
        # on, and the model list generated for it -- lives under this root, in
        # the one file both domains read.  It is named for what it holds so the
        # two domains' calls read the same.
        self.app_state_root = self.runtime_root
        self.model_catalog_path = managed_catalog_path(resolved_home)
        # App-private catalog bookkeeping stays out of the Codex directory:
        # the inheritance pin lives in the app's runtime settings file.
        # Legacy sidecar files are imported once and removed.
        self._catalog_base_profile_pin: str | None = None
        self._catalog_base_profile_loaded = False
        self._context_registry = ModelContextRegistry(
            runtime_config_path=self.runtime_config_path,
            runtime_settings_path=runtime_settings_path,
            cache_path=default_context_cache_path(self.runtime_root),
            legacy_cache_paths=legacy_context_cache_paths(resolved_home),
            refresh_enabled=runtime_settings_path is not None,
        )
        # A managed catalog pointer that names a missing file makes Codex refuse
        # every configuration load ("No such file or directory"), so the repair
        # that clears it must never re-enter itself through ``reload()`` and must
        # report a failure instead of leaving the client broken silently.
        self._catalog_pointer_repairing = False
        self._catalog_pointer_error: str | None = None
        # Endpoint-backed repairs are noisy during proxy reloads: /v1/models
        # can briefly alternate between adjacent worker views. Require two
        # consecutive observations of the same repaired model set before the
        # catalog is rewritten, so a transient partial view never becomes the
        # client's model list.
        self._catalog_repair_observed_signature: tuple[bool, tuple[str, ...]] | None = None
        self._catalog_repair_observation_count = 0
        self._catalog_source_checked_at = 0.0
        # Inputs of the last catalog the comparison found current.  Rebuilding
        # the expected 1.2 MB payload to compare it again costs hundreds of
        # milliseconds under Core's lock; a stat plus these identities answers
        # "nothing that shapes the file moved" without touching the body.
        self._catalog_current_signature: tuple[Any, ...] | None = None
        self._raw: dict[str, Any] = {}
        self._draft: dict[str, Any] = {}
        self._baseline: tuple[str, str] = ("", "{}\n")
        # The app's own proxy endpoint, resolved once per document load rather
        # than per snapshot: it reads the runtime settings and the (large)
        # runtime configuration, while snapshots are polled while a window is
        # open.  `uses_local_api` then only compares strings.
        self._local_api_base_url: str | None = None
        self._local_api_key = ""
        # The catalog address this app serves on that same port, resolved with
        # the endpoint so one document load answers both.
        self._local_catalog_url: str | None = None
        # When the served address was last confirmed to answer, so a refresh
        # pass does not re-fetch a body it already holds.
        self._catalog_url_checked_at = 0.0
        self._catalog_url_answers = False
        # The catalog this app serves, held as the payload rather than as a file.
        # Building it costs ~0.3 ms once the process has warmed its model-context
        # registry, so the served body needs no on-disk copy to be cheap; the
        # client's own file is written only while that file is what it reads.
        self._served_payload: dict[str, Any] | None = None
        self._served_payload_key: tuple[Any, ...] | None = None
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

        The sidecar only ever held this pin plus an acknowledgement no release
        uses any more.  The files are imported once, their pin is stored in the
        runtime settings, and every retired copy is removed from both the app
        root and the Codex home.
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
        # apply migrates the pointer to the current name.  Delivery by URL is
        # the same switch: the menu entry is on while either carrier names it.
        return self._catalog_delivery(payload) != "off"

    def _repair_catalog_pointer(self, value: str | None) -> bool:
        """Rewrite only ``model_catalog_json`` on disk, keeping Codex loadable.

        Codex rejects its whole configuration while that key names a missing
        file, so this repair is intentionally independent of the endpoint
        probe.  The two documents are read from disk immediately before the
        write so an external edit made since the last reload is preserved.
        """

        return self._write_catalog_delivery(value, url=None)

    def _write_catalog_delivery(self, value: str | None, *, url: str | None) -> bool:
        """Write one catalog decision -- pointer and provider URL -- to both documents.

        ``value`` is the file pointer (``None`` removes it) and ``url`` is the
        provider-level catalog address.  Codex treats a present
        ``model_catalog_json`` as an authoritative static catalog, so the two
        are never written separately: a URL without dropping the pointer would
        never be fetched.
        """

        from .. import codex_config

        if self._catalog_pointer_repairing:
            return False
        patch: dict[str, Any] = {"model_catalog_json": value}
        draft_before = copy.deepcopy(self._draft)
        structured = self._raw.get("structured")
        draft_structured = draft_before.get("structured")
        try:
            config_text, auth_text, _, _ = self._editor_documents_from_disk()
            config_text = self._set_catalog_delivery(
                config_text,
                structured if isinstance(structured, Mapping) else {},
                url=url,
            )
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
                draft_config = self._set_catalog_delivery(
                    draft_config,
                    draft_structured if isinstance(draft_structured, Mapping) else {},
                    url=url,
                )
                self._draft = self._sync(draft_config, draft_auth, patch)
            except DomainError:
                self._draft = draft_before
        # The answer to "does this address serve these models" changed with the
        # document; let the next pass ask again instead of trusting a stale yes.
        self._catalog_url_checked_at = 0.0
        return True

    def _keep_served_catalog_current(self) -> None:
        """Rebuild the served catalog while no local client is switched on.

        ``_catalog_response`` answers whoever fetches the address, and that
        client need not be on this machine: a remote Codex presents this
        proxy's key and reads the same body.  Without this pass the view was
        only refreshed when a local client named the catalog, so a host that
        serves other machines answered nothing until an unrelated reload.

        The source probe has already run; a failed one is not an observed empty
        list, so the last good body is kept.  Nothing is written to a client.
        """

        if not self._catalog_source_is_available(self._raw):
            return
        names = self._catalog_model_names(self._raw)
        if not names:
            return
        native_models = self._catalog_native_models()
        if native_models is None:
            return
        base_slug = self._catalog_base_profile(native_models)
        # ``_catalog_payload_for`` is keyed on the inputs, so a pass that finds
        # nothing new pays a comparison rather than a rebuild.
        self._catalog_payload_for(names, native_models, base_slug)

    def _catalog_payload_for(
        self,
        names: Sequence[str],
        native_models: Sequence[Mapping[str, Any]] | None,
        base_slug: str | None,
    ) -> dict[str, Any]:
        """This app's catalog payload for one model set, built once per input set."""

        from ..native_codex_catalog import native_catalog_identity

        key = (
            tuple(names),
            base_slug,
            native_catalog_identity(),
            self._context_registry.cache_identity(),
        )
        if self._served_payload is not None and self._served_payload_key == key:
            return self._served_payload
        payload = catalog_payload(
            list(names),
            registry=self._context_registry,
            native_models=native_models,
            base_slug=base_slug,
        )
        self._served_payload = payload
        self._served_payload_key = key
        return payload

    def _materialized_names(self) -> list[str] | None:
        """The model names the client is being served right now.

        The generated file answers while that file is the carrier; otherwise the
        payload this app serves by URL does.  Jitter protection compares against
        this one answer, so it survives the carrier changing.
        """

        pointer = self._catalog_pointer(self._raw)
        if pointer is not None and pointer in self._managed_catalog_paths():
            return catalog_model_names(self.model_catalog_path)
        payload = self._served_payload
        if not isinstance(payload, Mapping):
            return None
        models = payload.get("models")
        if not isinstance(models, Sequence) or isinstance(models, (str, bytes, bytearray)):
            return None
        return [
            str(model.get("slug") or "").strip()
            for model in models
            if isinstance(model, Mapping)
        ]

    def _catalog_response(self, *, pretty: bool = False) -> tuple[int, bytes]:
        """Answer one ``GET`` for the served catalog, without touching the store lock.

        The body is the managed catalog filtered to the models that are
        actually exposed and configured *now*: the file may lag an endpoint
        apply by one refresh pass, and a catalog naming a model the router does
        not serve is exactly what a picker must not offer.  A body Codex would
        reject (empty, or over its 1 MiB bound) is answered as a failure rather
        than as a valid empty list, so the client keeps its cached catalog
        instead of dropping every model.
        """

        names = self._catalog_model_names(self._raw)
        unavailable = b'{"error":{"code":"catalog_unavailable","message":"No exposed model is catalogued yet"}}'
        if not names:
            return 503, unavailable
        payload = self._served_payload
        if not isinstance(payload, Mapping):
            # The refresh lane keeps this warm; a fetch before its first pass
            # builds it here rather than answering nothing.
            payload = self._catalog_payload_for(
                names, self._catalog_native_models(), None
            )
        body = served_catalog_body(payload, pretty=pretty)
        return (200, body) if body else (503, unavailable)

    def _catalog_url_serves(self, names: Sequence[str]) -> bool:
        """Whether the proxy's own catalog answers with exactly these models.

        Codex fetches this address itself, with the provider's credentials, so
        the answer has to be checked with the same credential a client sends.
        A failure here is not an error: it sends the client back to the
        generated file, which is the documented fallback.
        """

        served = self._served_catalog_url()
        if not served or not names:
            return False
        now = time.monotonic()
        if now - self._catalog_url_checked_at < _CATALOG_URL_PROBE_SECONDS:
            return self._catalog_url_answers
        # Only a confirmed answer is remembered.  A failure is usually this
        # pass's own doing -- the proxy it asks may still be starting -- so
        # caching it would keep the client on the file for no reason.
        self._catalog_url_answers = False
        try:
            from ...api_base import isolated_http_opener
            from .. import codex_config

            key = codex_config.local_api_key(codex_config.load_yaml(self.runtime_config_path))
        except Exception:
            return False
        request = urllib.request.Request(
            served,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with isolated_http_opener().open(request, timeout=_CATALOG_URL_PROBE_TIMEOUT_SECONDS) as response:
                if getattr(response, "status", 0) != 200:
                    return False
                raw = response.read(_CATALOG_URL_PROBE_MAX_BYTES + 1)
        except Exception:
            return False
        if len(raw) > _CATALOG_URL_PROBE_MAX_BYTES:
            return False
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            return False
        models = payload.get("models") if isinstance(payload, Mapping) else None
        if not isinstance(models, Sequence) or isinstance(models, (str, bytes, bytearray)):
            return False
        served_names = {
            str(model.get("slug") or "").strip()
            for model in models
            if isinstance(model, Mapping)
        }
        self._catalog_url_answers = served_names == set(names)
        self._catalog_url_checked_at = now if self._catalog_url_answers else 0.0
        return self._catalog_url_answers

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
            # erase the catalog.
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

        existing_names = self._materialized_names()
        return existing_names is not None and set(existing_names) != set(names)

    @staticmethod
    def _catalog_signature(
        names: list[str] | tuple[str, ...],
        *,
        enabled: bool,
    ) -> tuple[bool, tuple[str, ...]]:
        # Catalog repair decisions are about model IDs, not endpoint order.
        return enabled, tuple(sorted(set(names)))

    def _reset_catalog_repair_observation(self) -> None:
        self._catalog_repair_observed_signature = None
        self._catalog_repair_observation_count = 0

    def _write_managed_catalog(
        self,
        names: Sequence[str],
        native_models: Sequence[Mapping[str, Any]],
        base_slug: str | None,
    ) -> None:
        """Write the managed catalog, never a model-less one.

        Codex refuses its entire configuration when the catalog it is pointed
        at holds no model, so an empty observed view must not become the file
        the client loads.  Every writer of the managed catalog goes through
        here, which is what makes that a property of the file rather than a
        rule each caller has to remember.
        """

        if not names:
            return
        # A writer other than the comparison below changes the file identity;
        # never reuse an answer about the body that was just replaced.
        self._catalog_current_signature = None
        write_catalog(
            self.model_catalog_path,
            names,
            registry=self._context_registry,
            native_models=native_models,
            base_slug=base_slug,
        )

    def _discard_managed_catalog_file(self) -> None:
        """Remove the generated catalog while the URL carries it.

        The file exists for one reader: a client whose provider row names it.
        While the row names this app's address instead, the body is served from
        memory and the file is stale weight -- and a stale file next to a
        client's documents is what made an earlier install keep loading a
        catalog nobody updated.
        """

        for path in self._managed_catalog_paths():
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                continue

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

    def _catalog_candidate_signature(
        self,
        names: Sequence[str],
        native_models: Sequence[Mapping[str, Any]],
        base_slug: str | None,
    ) -> tuple[Any, ...]:
        """The inputs whose change requires rebuilding the managed catalog."""

        try:
            details = self.model_catalog_path.stat()
            file_key: tuple[int, int] | None = (details.st_mtime_ns, details.st_size)
        except OSError:
            file_key = None
        native_key = tuple(
            (
                str(item.get("slug", "")),
                int(item.get("priority") or 0),
                profile_has_instructions(item),
            )
            for item in native_models
            if isinstance(item, Mapping)
        )
        return (
            file_key,
            tuple(names),
            base_slug or "",
            native_key,
            id(self._context_registry._records),
        )

    def _ensure_model_catalog_current(
        self,
        *,
        force_source_refresh: bool = False,
        require_stable_repair: bool = False,
    ) -> bool:
        delivery = self._catalog_delivery(self._raw)
        pointer = self._catalog_pointer(self._raw)
        owned_pointer = pointer is not None and pointer in self._managed_catalog_paths()
        plugin_pointer = pointer is not None and pointer.name == PLUGIN_CATALOG_FILE_NAME
        dangling = owned_pointer and not pointer.exists()
        source_refreshed = self._refresh_live_catalog_source(force=force_source_refresh)
        if delivery == "off":
            # No client on this machine reads the catalog, but the address this
            # app serves does not belong to one client: another machine points
            # its own Codex at it, with no local app in the loop at all.  So the
            # served body stays current even here -- only the client-side writes
            # below are off, and an untouched client keeps its own list.
            self._keep_served_catalog_current()
            self._reset_catalog_repair_observation()
            return False
        if not self._catalog_source_is_available(self._raw):
            self._reset_catalog_repair_observation()
            if dangling and source_refreshed:
                # Codex cannot load any configuration whose catalog pointer names
                # a missing file.  A freshly observed endpoint failure means the
                # catalog cannot be rebuilt right now, so drop the pointer instead
                # of leaving the client unusable; the next available probe (or an
                # explicit switch) restores the catalog.
                if self._repair_catalog_pointer(None):
                    self.revision += 1
            return False
        names = self._catalog_model_names(self._raw)
        if not names:
            # A live view with no public model is never handed to Codex: it
            # refuses a catalog that lists no model, so writing this view would
            # break the very configuration it serves.  The view is empty for
            # reasons unrelated to the user's model set — the proxy answers
            # ``/v1/models`` while it is still loading its routes, and a worker
            # reload briefly publishes no group — so a usable file stays
            # untouched and the next observation repairs from a truthful view.
            # Where the pointer names a missing or unusable file instead, one
            # load failure is not exchanged for another: the pointer is dropped
            # so Codex loads its own model list until a real one is observed.
            self._reset_catalog_repair_observation()
            if owned_pointer and not catalog_is_usable(pointer):
                if self._repair_catalog_pointer(None):
                    self.revision += 1
            return False
        model_ids_changed = self._catalog_model_ids_changed(names)
        if require_stable_repair and model_ids_changed:
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
        signature = self._catalog_candidate_signature(names, native_models, base_slug)
        # The payload is the catalog: it is what the address answers with, and
        # what the file is written from when the client has to read a file.
        self._catalog_payload_for(names, native_models, base_slug)
        changed = False
        if self._catalog_url_serves(names):
            # URL delivery: no file.  Deleting a leftover one is part of the
            # switch -- a catalog next to the client's documents that nobody
            # reads is how an earlier install kept serving a stale list.
            if self.model_catalog_path.exists():
                self._discard_managed_catalog_file()
                changed = True
            structured = self._raw.get("structured")
            structured = structured if isinstance(structured, Mapping) else {}
            # A row already naming this app's address still needs a write when it
            # names a *retired* spelling: the route moved, and the old path no
            # longer answers, so leaving it would drop the client to Codex's own
            # list at its next fetch.
            active_url = self._active_provider_catalog_url(structured).rstrip("/")
            if active_url != self._served_catalog_url() or pointer is not None:
                if self._write_catalog_delivery(None, url=self._served_catalog_url()):
                    self._catalog_current_signature = self._catalog_candidate_signature(
                        names, native_models, base_slug
                    )
                    self.revision += 1
                    return True
            self._catalog_current_signature = signature
            self._remove_retired_catalog_copies()
            return changed
        # The address did not answer with this list, so this client falls back
        # to a file.  While the plugin is the one maintaining it, its pointer is
        # left exactly as it is -- installing a second writer here is what the
        # retirement flag existed to prevent.  A plugin the *host* has switched
        # off maintains nothing, so it cannot hold the client on a stale file.
        structured = self._raw.get("structured")
        structured = structured if isinstance(structured, Mapping) else {}
        if plugin_pointer and self._plugin_enabled_in_host(structured):
            self._reset_catalog_repair_observation()
            return changed
        catalog_current = catalog_is_current(
            self.model_catalog_path,
            names,
            registry=self._context_registry,
            native_models=native_models,
            base_slug=base_slug,
        )
        if not catalog_current:
            self._write_managed_catalog(names, native_models, base_slug)
            changed = True
        migrated = pointer != self.model_catalog_path
        if not migrated and not changed:
            self._catalog_current_signature = signature
            self._remove_retired_catalog_copies()
            return False
        if migrated:
            # Write the current file before moving the pointer so a failure in
            # between leaves Codex with a loadable config, never a dangling one.
            if not self._repair_catalog_pointer(str(self.model_catalog_path)):
                # The applied config still names the retired file, so that file
                # stays and this view is not settled.  Recording the signature
                # here sent the next refresh down the fast path above, which
                # deleted the very catalog the pointer names.  The move is
                # retried on the next refresh; only a rewritten catalog is a
                # change worth a revision until it succeeds.
                if not changed:
                    return False
                self.revision += 1
                return True
            self._remove_retired_catalog_copies()
        else:
            self._remove_retired_catalog_copies()
        self._catalog_current_signature = self._catalog_candidate_signature(
            names, native_models, base_slug
        )
        self.revision += 1
        return True

    def refresh_model_contexts_if_due(self) -> bool:
        """Refresh public model metadata on the background lane.

        The fetch is a remote round trip and can outlast any window's poll; it
        must run before the caller takes Core's store lock, never inside a
        projection.  A reader keeps using the registry's previous records until
        the new map is swapped in.
        """

        return self._context_registry.refresh_if_due(sync_cache=False)

    def refresh_model_catalog(self, *, force_source_refresh: bool = True) -> bool:
        """Refresh the enabled catalog after LiteLLM reloads its routes.

        Worker reloads briefly alternate between adjacent ``/v1/models``
        views.  Keep the two-observation stability gate so a provider apply
        that did not change the exposed model set cannot rewrite the catalog
        from a transient partial view.

        ``force_source_refresh`` is the caller's statement about the moment:
        a proxy replacement or an explicit client-list refresh asks again,
        while an observation taken by the background lane's own cadence
        respects the endpoint probe's rate limit.
        """

        return self._ensure_model_catalog_current(
            force_source_refresh=force_source_refresh,
            require_stable_repair=True,
        )

    def refresh_local_api_endpoint(self) -> None:
        """Resolve this app's proxy endpoint for the "use this API" action."""

        from ._shared import local_catalog_url, local_proxy_endpoint

        try:
            self._local_api_base_url, self._local_api_key = local_proxy_endpoint(
                self.runtime_config_path
            )
        except Exception:
            self._local_api_base_url = None
            self._local_api_key = ""
        # The catalog address follows the same port, so it is resolved here too:
        # one document load answers both, and a snapshot never reads settings.
        try:
            self._local_catalog_url = local_catalog_url(self.runtime_config_path)
        except Exception:
            self._local_catalog_url = None
        self._catalog_url_checked_at = 0.0
        self._catalog_url_answers = False
        # The catalog this app serves, held as the payload rather than as a file.
        # Building it costs ~0.3 ms once the process has warmed its model-context
        # registry, so the served body needs no on-disk copy to be cheap; the
        # client's own file is written only while that file is what it reads.
        self._served_payload: dict[str, Any] | None = None
        self._served_payload_key: tuple[Any, ...] | None = None

    def _served_catalog_url(self) -> str:
        """The catalog address this app answers on the proxy's own port."""

        return (self._local_catalog_url or "").rstrip("/")

    @staticmethod
    def _active_provider_row(structured: Mapping[str, Any], active: str) -> Mapping[str, Any]:
        for item in structured.get("providers") or []:
            if isinstance(item, Mapping) and str(item.get("id") or "").strip() == active:
                return item
        return {}

    def _active_provider_catalog_url(self, structured: Mapping[str, Any]) -> str:
        active = str(structured.get("model_provider") or "openai").strip() or "openai"
        if active in RESERVED_PROVIDER_IDS:
            # A built-in row carries no catalog URL: Codex reads that key only
            # from a provider table this app can edit.
            return ""
        value = self._active_provider_row(structured, active).get("model_catalog_url")
        return value.strip() if isinstance(value, str) else ""

    @staticmethod
    def _plugin_enabled_in_host(structured: Mapping[str, Any]) -> bool:
        """Whether the model-list-plugin is switched on *in the client's own file*.

        The plugin's own configuration says which targets it manages, not
        whether the client runs it: ``[plugins."model-list-plugin@..."]
        enabled = false`` in Codex's document is the host's answer, and it is
        the only one that decides whether that catalog is still being written.
        """

        for entry in structured.get("plugins") or []:
            if not isinstance(entry, Mapping):
                continue
            if PLUGIN_NAME_TOKEN not in str(entry.get("id") or ""):
                continue
            return entry.get("enabled") is True
        return False

    @staticmethod
    def _is_served_catalog_address(value: str, base_url: str | None = None) -> bool:
        """Whether one address is (or was) the catalog this app points a client at.

        The address is the proxy's own ``/v1/models``, which is a generic path --
        so a row is only ours when it is the *same origin* as that row's
        inference endpoint, i.e. when the row already names this proxy.  A direct
        or relay row keeps its own ``/v1/models`` untouched.

        A retired spelling counts as ours at any port: it was this app's own
        route, and the next pass moves it to the current spelling instead of
        leaving a dead address in the client's document.
        """

        from urllib.parse import urlsplit

        if not isinstance(value, str) or not value.strip():
            return False
        try:
            parts = urlsplit(value.strip())
        except ValueError:
            return False
        if parts.scheme not in {"http", "https"} or (parts.hostname or "") not in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            return False
        path = parts.path.rstrip("/")
        if path == SERVED_CATALOG_ROUTE or path in RETIRED_SERVED_CATALOG_ROUTES:
            return True
        if path != "/v1/models":
            return False
        # One released build pointed at the proxy's ``/v1/models``; that row is
        # ours only when its own endpoint is this machine's, so a direct row on
        # a remote gateway keeps the catalog URL its operator wrote.
        return (
            isinstance(base_url, str)
            and _is_loopback_address(base_url)
            and _same_origin(base_url, value)
        )

    @staticmethod
    def _active_provider_base_url(structured: Mapping[str, Any]) -> str:
        active = str(structured.get("model_provider") or "openai").strip() or "openai"
        if active in RESERVED_PROVIDER_IDS:
            return str(structured.get("openai_base_url") or "").strip()
        value = CodexSettingsDomain._active_provider_row(structured, active).get("base_url")
        return value.strip() if isinstance(value, str) else ""

    def _catalog_delivery(self, payload: Mapping[str, Any]) -> str:
        """How this client receives its catalog: ``url``, ``file``, ``plugin``, ``off``.

        URL delivery is preferred because the client fetches it itself, so a
        refreshed list needs no file rewrite and no client restart.  The file
        this app generates is the fallback, and the model-list-plugin's own
        catalog is the fallback of last resort -- only a pointer naming it is
        recognised, and only while no URL answers.
        """

        structured = payload.get("structured")
        structured = structured if isinstance(structured, Mapping) else {}
        if self._is_served_catalog_address(
            self._active_provider_catalog_url(structured),
            self._active_provider_base_url(structured),
        ):
            return "url"
        pointer = self._catalog_pointer(payload)
        if pointer is None:
            return "off"
        if pointer in self._managed_catalog_paths():
            return "file"
        if pointer.name == PLUGIN_CATALOG_FILE_NAME:
            return "plugin"
        return "off"

    def _set_catalog_delivery(
        self, config_text: str, structured: Mapping[str, Any], *, url: str | None
    ) -> str:
        """Move the provider catalog URL and the file pointer as one decision.

        Codex reads ``model_catalog_json`` before it ever looks at a provider's
        ``model_catalog_url``: a file pointer present anywhere replaces the
        catalog manager outright, so writing the URL without dropping the
        pointer would leave the URL dead.  Both halves are therefore written
        here and nowhere else.

        A built-in provider id (``openai`` and friends) has no table this app
        may edit, so only the pointer half applies there; the caller's probe
        keeps such a client on the file.  Removing the URL is limited to the
        address this app serves -- a direct route's own catalog URL belongs to
        the operator and is never rewritten here.
        """

        from .. import codex_config

        active = str(structured.get("model_provider") or "openai").strip() or "openai"
        if active in RESERVED_PROVIDER_IDS:
            return config_text
        path = ("model_providers", active)
        if url:
            return codex_config.set_table_value(config_text, path, "model_catalog_url", url)
        if self._is_served_catalog_address(
            self._active_provider_catalog_url(structured),
            self._active_provider_base_url(structured),
        ):
            return codex_config.remove_table_value(config_text, path, "model_catalog_url")
        return config_text

    def _names_local_api(self, configured: object) -> bool:
        """Whether one endpoint is this app's own proxy."""

        base_url = self._local_api_base_url
        if not base_url or not isinstance(configured, str) or not configured.strip():
            return False
        return configured.strip().rstrip("/") == base_url.rstrip("/")

    def uses_local_api(self) -> bool:
        """True when the selected Codex provider already points at this proxy."""

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
        return any(self._names_local_api(candidate) for candidate in candidates if candidate)

    def _effective_api_key(self, structured: Mapping[str, Any], active: str) -> str:
        """The credential this client would actually send to its provider."""

        from .. import codex_config

        if active == "openai":
            value = structured.get("api_key")
            return value.strip() if isinstance(value, str) else ""
        raw = codex_config._provider_mapping(self._draft.get("config_text", ""), active)
        auth_mode = codex_config._provider_auth_mode(raw)
        if auth_mode == "bearer":
            value = raw.get("experimental_bearer_token")
            return value.strip() if isinstance(value, str) else ""
        if auth_mode == "openai_auth":
            value = structured.get("api_key")
            return value.strip() if isinstance(value, str) else ""
        if auth_mode == "env_key":
            env_key = raw.get("env_key")
            return os.environ.get(env_key, "").strip() if isinstance(env_key, str) else ""
        return ""

    def _yrapi_catalog_url(self, structured: Mapping[str, Any]) -> str:
        """The catalog address this app's API would put in the active row.

        The catalog switch is a separate decision: a client carrying the file
        pointer (or with the catalog off) has no address of its own, so that
        half of the identity is empty.  Only URL delivery names an address.
        """

        active = str(structured.get("model_provider") or "openai").strip() or "openai"
        if active in RESERVED_PROVIDER_IDS:
            return ""
        if self._catalog_delivery(self._draft) != "url":
            return ""
        return self._served_catalog_url()

    def uses_yrapi_api(self) -> bool:
        """True when the client names this app's whole API, not just its port.

        The switch is an identity, not a reachability test: the base URL, the
        credential, and (for Codex) the catalog address all have to be the ones
        this app writes.  A client pointed at the proxy with someone else's key,
        or with a catalog of its own, is a direct connection whose address
        merely happens to be local.
        """

        base_url = self._local_api_base_url
        key = self._local_api_key
        if not base_url or not key:
            return False
        structured = self._draft.get("structured")
        structured = structured if isinstance(structured, Mapping) else {}
        active = str(structured.get("model_provider") or "openai").strip() or "openai"
        if not self._names_local_api(self._active_provider_endpoint(structured, active)):
            return False
        if self._effective_api_key(structured, active) != key:
            return False
        current_catalog = self._active_provider_catalog_url(structured).rstrip("/")
        expected_catalog = self._yrapi_catalog_url(structured).rstrip("/")
        return current_catalog == expected_catalog

    def standalone_search_enabled(self) -> bool:
        """Whether the selected provider opts into Codex's own web search.

        Provider-level opt-in is the only carrier that matters on this route:
        the Responses Lite lane never declares the hosted ``web_search`` tool,
        so ``supports_standalone_web_search`` is what makes Codex register its
        ``web.run`` executor. A missing field is Codex's default (off), which
        is why this reads the row instead of a top-level switch.
        """

        structured = self._draft.get("structured")
        structured = structured if isinstance(structured, Mapping) else {}
        provider_id = str(structured.get("model_provider") or "").strip()
        providers = structured.get("providers")
        if not provider_id or not isinstance(providers, list):
            return False
        for item in providers:
            if isinstance(item, Mapping) and str(item.get("id") or "").strip() == provider_id:
                return item.get("supports_standalone_web_search") is True
        return False

    def image_context_mcp_enabled(self) -> bool:
        """Whether Codex lists this app's image-context fetch server.

        The switch lives in the runtime settings (that is where the operator
        turns the pass on), and the server entry follows it: a Codex session
        with the pass on but no fetch tool would leave the model holding
        placeholders it can never open, so the two are kept together here.
        """

        structured = self._draft.get("structured")
        return image_context_mcp_registered(
            structured if isinstance(structured, Mapping) else {}
        )

    def _direct_api_settings(self, structured: Mapping[str, Any]) -> dict[str, Any]:
        """The connection the client is on, as the Edit API form prefills it.

        The form edits the live connection.  The off toggle applies the
        remembered direct route before it opens the form, so that path still
        shows the route the switch returns to; a person who opens the form while
        the switch is on sees this app's URL, key, and catalog, and an
        unchanged save keeps the switch on.
        """

        active = str(structured.get("model_provider") or "openai").strip() or "openai"
        provider = next(
            (
                row
                for row in structured.get("providers") or []
                if isinstance(row, Mapping) and str(row.get("id") or "").strip() == active
            ),
            {},
        )
        return {
            "base_url": public_direct_api_url(self._active_provider_endpoint(structured, active)),
            "model_catalog_url": public_direct_api_url(provider.get("model_catalog_url")),
            "model": structured.get("model") or "",
            "key_configured": bool(self._effective_api_key(structured, active)),
            "auth_mode": "bearer",
        }

    def _safe_snapshot(self, payload: Mapping[str, Any], revision: int) -> dict[str, Any]:
        errors = payload.get("validation_errors", [])
        warnings = payload.get("warnings", [])
        if self._catalog_pointer_error:
            warnings = [*(warnings if isinstance(warnings, list) else []), self._catalog_pointer_error]
        delivery = self._catalog_delivery(payload)
        public_models = self._catalog_model_names(payload) if delivery != "off" else []
        structured = payload.get("structured") or {}
        uses_local_api = self.uses_local_api()
        return {
            "domain": "codex",
            "revision": revision,
            "config_exists": bool(payload.get("config_exists")),
            "auth_file_exists": bool(payload.get("auth_exists")),
            "uses_local_api": uses_local_api,
            "uses_yrapi_api": self.uses_yrapi_api(),
            "standalone_search": self.standalone_search_enabled(),
            "image_context_mcp": self.image_context_mcp_enabled(),
            "structured": redact(structured),
            "direct_api": redact(self._direct_api_settings(structured)),
            "models": redact(payload.get("models", [])),
            "validation_errors": redact(errors if isinstance(errors, list) else []),
            "warnings": redact(warnings if isinstance(warnings, list) else []),
            "raw_editor_available": True,
            "model_catalog": {
                "enabled": delivery != "off",
                "public_models": public_models or [],
                # Which carrier the client is on. The generated file exists in
                # every mode -- it is what the served body is read from, and
                # what a fetch failure falls back to -- so a window cannot infer
                # from its presence whether a client still loads it.
                "delivery": delivery,
            },
        }

    def snapshot(self) -> dict[str, Any]:
        """Project the staged document without touching the live proxy.

        A projection is read by every window poll and rebuilt whenever the
        shared revision moves.  The managed catalog's refresh asks the local
        proxy for ``/v1/models``, and while that proxy replaces its workers the
        port is bound but silent: doing it here charged the endpoint timeout to
        the window's own refresh, once per revision, and the native host's
        one-second polls queued behind it.  ``refresh_model_catalog`` owns that
        observation on the background client-model lane, and it publishes what
        it learns like any other mutation.
        """

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
        # ``sync_editor`` treats a rejected edit as non-fatal so the raw text
        # panes can keep the user's work on screen.  A structured *action* is a
        # different caller: it has already decided what to write, so a refusal
        # has to reach the pane instead of becoming this draft's state.  Left
        # as state, the refusal would replace the projection with an empty one
        # and the next action would no longer know the active provider.
        if patch is not None:
            errors = synced.get("validation_errors")
            if isinstance(errors, list) and errors:
                raise DomainError(str(errors[0]))
        # ``sync_editor`` validates and rewrites an in-memory draft without
        # touching CODEX_HOME, so its payload intentionally has no filesystem
        # existence fields. Those fields describe the loaded disk baseline,
        # not whether the draft contains text, and must survive every staged
        # structured/raw edit until Apply or Reload establishes a new baseline.
        synced["config_exists"] = bool(self._raw.get("config_exists"))
        synced["auth_exists"] = bool(self._raw.get("auth_exists"))
        return synced

    def dispatch(self, action: str, payload: object | None = None) -> dict[str, Any]:
        name = _action_name(action)
        data = _mapping(payload or {})
        config_text = self._draft.get("config_text", "")
        auth_text = self._draft.get("auth_text", "{}\n")
        if not isinstance(config_text, str) or not isinstance(auth_text, str):
            raise DomainError("Codex settings are invalid")
        if name in {"set_raw", "setraw"}:
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
            self._draft = self._adopt_local_api(config_text, auth_text, data)
        elif name in {"leave_local_api", "leavelocalapi"}:
            self._draft = self._leave_local_api(config_text, auth_text)
        elif name == "use_direct_api":
            base_url = data["base_url"].rstrip("/")
            form = self._form_direct_connection(data)
            structured = self._draft.get("structured")
            structured = structured if isinstance(structured, Mapping) else {}
            if (
                self._names_local_api(base_url)
                and bool(self._local_api_key)
                and form.get("api_key") == self._local_api_key
                and str(form.get("model_catalog_url") or "").rstrip("/")
                == self._yrapi_catalog_url(structured).rstrip("/")
            ):
                # The form is the off editor, but saving this app's own full
                # triplet is how its other direction is reached: the row keeps
                # its endpoint, master key, catalog address and search opt-in
                # as one connection instead of becoming a look-alike direct one.
                self._draft = self._adopt_local_api(config_text, auth_text, data)
            else:
                self._draft = self._apply_direct_connection(config_text, auth_text, form)
                store_direct_connection(self.app_state_root, "codex", form)
        elif name == "codex_feature_set":
            feature = data.get("feature")
            enabled_value = data.get("enabled")
            if not isinstance(feature, str) or not feature.endswith("_use") or not isinstance(enabled_value, bool):
                raise DomainError("The Codex feature switch is invalid")
            current = self._draft.get("structured")
            current = current if isinstance(current, Mapping) else {}
            current_features = current.get("features")
            current_plugins = current.get("plugins")
            patch = _surface_sync_patch(
                self.codex_home_path(),
                current_features if isinstance(current_features, Mapping) else {},
                [item for item in current_plugins if isinstance(item, Mapping)]
                if isinstance(current_plugins, list) else [],
                override={feature: enabled_value},
            )
            self._draft = self._sync(
                config_text,
                auth_text,
                patch,
            )
        elif name in {"codex_search_set", "codex_search"}:
            # Codex's Responses Lite lane carries no hosted web_search tool, so
            # the provider row's standalone opt-in is the whole switch: with it
            # Codex registers ``web.run`` and the agent stops curling search
            # engines from the shell.  Since the opt-in inherits Codex's default
            # ``cached`` mode -- which registers the executor with live access
            # off -- enabling it also asks for live search.
            enabled_value = data.get("enabled")
            if not isinstance(enabled_value, bool):
                raise DomainError("The Codex search switch is invalid")
            current = self._draft.get("structured")
            current = current if isinstance(current, Mapping) else {}
            provider_id = str(current.get("model_provider") or "").strip()
            providers = current.get("providers")
            if not provider_id or not isinstance(providers, list) or not any(
                isinstance(item, Mapping)
                and str(item.get("id") or "").strip() == provider_id
                for item in providers
            ):
                raise DomainError("The selected Codex provider has no editable row")
            self._draft = self._sync(
                config_text,
                auth_text,
                {
                    "web_search": "live" if enabled_value else None,
                    "providers": [
                        {
                            "id": provider_id,
                            # An explicit ``false``, never an absent key: a
                            # restart reconciles a *missing* opt-in onto a
                            # client that points at this proxy (that is the
                            # healing path for a file written before the
                            # opt-in existed), so recording "off" as absence
                            # would turn this switch back on at the next start.
                            "supports_standalone_web_search": enabled_value,
                        }
                    ],
                },
            )
        elif name in {"codex_image_context_set", "codex_image_context"}:
            # The setting itself belongs to the runtime settings; this action
            # only keeps Codex's own MCP list in step with it, which is the one
            # thing the data plane cannot write by itself.
            enabled_value = data.get("enabled")
            if not isinstance(enabled_value, bool):
                raise DomainError("The Codex image-context switch is invalid")
            structured = self._draft.get("structured")
            structured = structured if isinstance(structured, Mapping) else {}
            patch = image_context_mcp_patch(
                structured,
                enabled=enabled_value,
                python=str(data.get("python") or sys.executable or "python3"),
                python_path=str(data.get("python_path") or ""),
            )
            if patch:
                self._draft = self._sync(config_text, auth_text, patch)
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
        if field == "direct_api_key" and target:
            from .. import codex_config

            structured = self._draft.get("structured") or {}
            # The form edits the connection the client is on.  After the off
            # action has applied the remembered route, that route's own
            # credential is what the field reads; while yrapi is on, it reads
            # the master key the client actually sends.
            provider_id = str(structured.get("model_provider") or "openai").strip() or "openai"
            provider = codex_config._provider_mapping(self._draft.get("config_text", ""), provider_id)
            env_key = provider.get("env_key")
            if isinstance(env_key, str) and env_key:
                return os.environ.get(env_key, "")
            if provider_id == "openai" or provider.get("requires_openai_auth") is True:
                return os.environ.get("OPENAI_API_KEY") or structured.get("api_key", "")
            return str(provider.get("experimental_bearer_token") or "")
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

    def _catalog_carrier(
        self,
        *,
        structured: Mapping[str, Any] | None = None,
        enabled: bool,
        names: Sequence[str],
    ) -> tuple[str | None, Path | None]:
        """The (provider URL, file pointer) pair one catalog decision selects.

        URL first: this app answers that address from the same model list the
        file would hold, so a refreshed list reaches the client without a
        rewrite it has to restart to read.  The generated file is the fallback
        whenever the address does not answer with exactly this list, and it is
        the only carrier for a built-in provider: ``openai`` and friends have
        no provider table, so a URL written anywhere else would never be read.
        """

        if not enabled:
            self._discard_managed_catalog_file()
            return None, None
        selected = structured if isinstance(structured, Mapping) else {}
        active = str(selected.get("model_provider") or "openai").strip() or "openai"
        if active in RESERVED_PROVIDER_IDS:
            return None, self.model_catalog_path
        served = self._served_catalog_url()
        if served and names and self._catalog_url_serves(names):
            # The client fetches the address, so a second copy next to its
            # documents would be a file it never opens -- and exactly the kind
            # of orphan this app already had to clean up once.
            self._discard_managed_catalog_file()
            return served, None
        return None, self.model_catalog_path

    def _with_catalog_carrier(
        self, state: Mapping[str, Any], *, enabled: bool, names: Sequence[str]
    ) -> dict[str, Any]:
        """Return one staged document carrying the selected catalog carrier."""

        config_text = state.get("config_text", "")
        auth_text = state.get("auth_text", "{}\n")
        structured = state.get("structured")
        if not isinstance(config_text, str) or not isinstance(auth_text, str):
            return dict(state)
        url, pointer = self._catalog_carrier(
            structured=structured if isinstance(structured, Mapping) else None,
            enabled=enabled,
            names=names,
        )
        updated = self._set_catalog_delivery(
            config_text,
            structured if isinstance(structured, Mapping) else {},
            url=url,
        )
        try:
            return self._sync(
                updated, auth_text, {"model_catalog_json": str(pointer) if pointer else None}
            )
        except DomainError:
            # A raw editor may hold an intentionally invalid draft.  Leaving it
            # untouched is better than replacing the person's text with a
            # document this action synthesised.
            return dict(state)

    def apply(self, payload: object | None = None) -> dict[str, Any]:
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
                # The public context sources are fetched on Core's background
                # lane before this action takes the store lock; this Apply is
                # not allowed to wait for the internet.
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
        if catalog_changed and catalog_models is not None and native_models is not None:
            self._write_managed_catalog(catalog_models, native_models, base_slug)
        # The carrier is one decision with the catalog switch: applying an
        # unrelated edit must not leave the client on the other one, and a
        # staged draft written before the URL existed would otherwise restore
        # the pointer that makes Codex ignore it.
        if will_be_enabled or self._catalog_delivery(self._draft) == "url":
            self._draft = self._with_catalog_carrier(
                self._draft, enabled=will_be_enabled, names=catalog_models or []
            )
        try:
            self._write_documents(self._draft)
        except DomainError:
            raise
        except Exception as exc:
            raise _safe_problem(exc, "Codex settings could not be saved") from None
        self.reload()
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
                # The public context sources are fetched on Core's background
                # lane before this action takes the store lock; this Apply is
                # not allowed to wait for the internet.
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
        draft_before = copy.deepcopy(self._draft)
        draft_next = draft_before
        draft_config = draft_before.get("config_text", "")
        draft_auth = draft_before.get("auth_text", "{}\n")
        if isinstance(draft_config, str) and isinstance(draft_auth, str):
            try:
                # The staged copy moves with the applied one, or the next Apply
                # would put the previous carrier back.
                draft_next = self._with_catalog_carrier(
                    draft_before, enabled=enabled, names=catalog_models or []
                )
            except DomainError:
                # A raw editor may intentionally contain an invalid draft. Keep
                # that draft intact while the menu action updates the applied
                # document below.
                draft_next = draft_before
        if catalog_changed and catalog_models is not None and native_models is not None:
            self._write_managed_catalog(catalog_models, native_models, base_slug)
        next_disk = self._with_catalog_carrier(
            {
                "config_text": str(current.get("config_text", "")),
                "auth_text": str(current.get("auth_text", "{}\n")),
                "structured": current.get("structured"),
            },
            enabled=enabled,
            names=catalog_models or [],
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

    def _active_provider_patch(
        self,
        *,
        base_url: str,
        key: str,
        wire_api: str | None = None,
        standalone_web_search: bool | None = None,
    ) -> dict[str, Any]:
        """Point the client's *current* provider at one endpoint.

        A user provider row keeps its identity: only its endpoint (plus the API
        surface a direct route needs) changes.  A built-in provider (``openai``
        and friends) has no editable table at all, so adopting this proxy moves
        the client to the row already naming this proxy -- or to this app's own
        row -- because the built-in row cannot carry the catalog URL or the
        gateway's credential shape.  That row is also what keeps Codex's picker
        on the catalog instead of its own model family.

        ``standalone_web_search`` is for the local-proxy route only.  The flag
        makes Codex call ``{this provider's base_url}/alpha/search``, which only
        a gateway this app fronts can answer; a direct route's ``base_url`` is
        the provider itself, so setting it there would aim Codex at an endpoint
        that provider does not serve.
        """

        structured = self._draft.get("structured")
        structured = structured if isinstance(structured, Mapping) else {}
        providers = [
            copy.deepcopy(dict(item))
            for item in structured.get("providers") or []
            if isinstance(item, Mapping)
        ]
        selected = str(structured.get("model_provider") or "").strip()
        if selected == DIRECT_API_PROVIDER_ID:
            active = LOCAL_API_PROVIDER_ID
        elif not selected or selected.lower() in RESERVED_PROVIDER_IDS:
            # A built-in selection cannot hold this app's catalog URL.  Reuse
            # the row a previous adoption already pointed here, so repeated
            # switches do not accumulate provider identities.
            proxy_row = next(
                (
                    item
                    for item in providers
                    if str(item.get("id") or "").strip() != DIRECT_API_PROVIDER_ID
                    and self._names_local_api(str(item.get("base_url") or ""))
                    and (
                        str(item.get("id") or "").strip() == LOCAL_API_PROVIDER_ID
                        or self._is_served_catalog_address(
                            str(item.get("model_catalog_url") or ""),
                            str(item.get("base_url") or ""),
                        )
                    )
                ),
                None,
            )
            active = (
                str(proxy_row.get("id") or "").strip()
                if proxy_row is not None
                else LOCAL_API_PROVIDER_ID
            )
        else:
            active = selected
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
            if standalone_web_search is not None:
                entry["supports_standalone_web_search"] = standalone_web_search
            entry["auth_mode"] = "openai_auth"
            return {"api_key": key, "model_provider": active, "providers": providers}
        entry = next(
            (item for item in providers if str(item.get("id") or "").strip() == LOCAL_API_PROVIDER_ID),
            None,
        )
        # The row this client names is not in the table the projection could
        # read, so one is created here.  It takes the client's own id whenever
        # that id can name a row at all: the provider identity is what this
        # whole action promises not to change, and moving the client onto a
        # second provider leaves the first row for the reconciliation below to
        # delete.  Only an empty or reserved id (a built-in Codex provider, or
        # none) falls back to this app's own row.
        fallback_id = LOCAL_API_PROVIDER_ID
        if active and active.lower() not in RESERVED_PROVIDER_IDS:
            # The row this client names is not in the table (the lookup above
            # found none), so one is created rather than updating the app's row.
            entry = None
            fallback_id = active
        if entry is None:
            entry = {"id": fallback_id, "name": fallback_id}
            providers.append(entry)
        entry.update(
            {
                "base_url": base_url,
                "wire_api": wire_api or "responses",
                "auth_mode": "openai_auth",
            }
        )
        if standalone_web_search is not None:
            entry["supports_standalone_web_search"] = standalone_web_search
        return {"api_key": key, "model_provider": fallback_id, "providers": providers}

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
        """The patch that moves the client's current provider onto this proxy.

        The search opt-in travels with it: Codex's Responses Lite lane declares
        no hosted ``web_search`` tool, so ``supports_standalone_web_search`` on
        this row is the whole reason the client ever calls this proxy's
        ``/alpha/search`` -- which is the route that reaches DeepSeek's own
        ``/tools/search``.  Without it the client registers no web tool at all
        and the agent falls back to driving a browser.

        Adoption states the opt-in unless the operator already did: an explicit
        ``false`` in the row is ``codex_search_set``'s own decision and moving
        the endpoint does not reopen it.
        """

        from ._shared import local_proxy_endpoint

        base_url, key = local_proxy_endpoint(self.runtime_config_path)
        self._local_api_base_url = base_url
        self._local_api_key = key
        structured = self._draft.get("structured")
        structured = structured if isinstance(structured, Mapping) else {}
        active = str(structured.get("model_provider") or "").strip()
        if active == DIRECT_API_PROVIDER_ID:
            active = LOCAL_API_PROVIDER_ID
        already_decided = any(
            isinstance(item, Mapping)
            and str(item.get("id") or "").strip() == active
            and item.get("supports_standalone_web_search") is False
            for item in structured.get("providers") or []
        )
        patch = self._active_provider_patch(
            base_url=base_url,
            key=key,
            wire_api="responses",
            standalone_web_search=None if already_decided else True,
        )
        if not already_decided:
            # The standalone executor inherits Codex's default ``cached`` mode,
            # which registers it with live web access off.  The opt-in and the
            # mode are one decision, exactly as ``codex_search_set`` treats
            # them.
            patch["web_search"] = "live"
        return patch

    def _active_provider(self, structured: Mapping[str, Any], provider_id: str) -> Mapping[str, Any]:
        return next(
            (
                row
                for row in structured.get("providers") or []
                if isinstance(row, Mapping) and str(row.get("id") or "").strip() == provider_id
            ),
            {},
        )

    def _remember_direct_connection(self) -> None:
        """Remember the connection this client leaves for this app's proxy.

        The off position of the pane's switch has to put the client back on the
        endpoint and credential it used before, and adopting the proxy overwrites
        both.  A client already on the proxy (or on the app's own proxy row)
        leaves nothing to remember: storing that row would make the switch's two
        positions the same address.
        """

        if self.uses_yrapi_api():
            return
        structured = self._draft.get("structured")
        structured = structured if isinstance(structured, Mapping) else {}
        active = str(structured.get("model_provider") or "openai").strip() or "openai"
        provider = self._active_provider(structured, active)
        base_url = self._active_provider_endpoint(structured, active)
        auth_mode = str(provider.get("auth_mode") or "none") if active != "openai" else "openai_auth"
        key = ""
        if auth_mode == "bearer":
            # The structured projection deliberately leaves the token out, so
            # the raw row is the only place the credential still exists before
            # adoption replaces it.
            from .. import codex_config

            raw_provider = codex_config._provider_mapping(
                self._draft.get("config_text", ""), active
            )
            key = str(raw_provider.get("experimental_bearer_token") or "")
        elif auth_mode in {"openai_auth", "command"}:
            key = str(structured.get("api_key") or "")
        connection: dict[str, Any] = {
            "provider": active,
            "base_url": base_url.rstrip("/"),
            "model": str(structured.get("model") or ""),
            "model_catalog_url": str(provider.get("model_catalog_url") or "").rstrip("/"),
            "auth_mode": auth_mode,
        }
        if key:
            connection["api_key"] = key
        if auth_mode == "env_key":
            env_key = str(provider.get("env_key") or "")
            if env_key:
                connection["env_key"] = env_key
        supports_search = provider.get("supports_standalone_web_search")
        if isinstance(supports_search, bool):
            connection["supports_standalone_web_search"] = supports_search
        web_search = structured.get("web_search")
        if isinstance(web_search, str) and web_search.strip():
            connection["web_search"] = web_search.strip()
        store_direct_connection(self.app_state_root, "codex", connection)

    def _form_direct_connection(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """The form's connection in the shape the state file remembers."""

        connection: dict[str, Any] = {
            "provider": DIRECT_API_PROVIDER_ID,
            "base_url": str(data.get("base_url") or "").rstrip("/"),
            "model": str(data.get("model") or "").strip(),
            "model_catalog_url": str(data.get("model_catalog_url") or "").rstrip("/"),
            "auth_mode": "bearer",
        }
        key = data.get("api_key")
        if isinstance(key, str) and key.strip() and not data.get("clear_key"):
            connection["api_key"] = key.strip()
        if data.get("clear_key"):
            connection["clear_key"] = True
        return connection

    def _apply_direct_connection(
        self, config_text: str, auth_text: str, connection: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Stage one remembered or saved direct connection and select it."""

        from .. import codex_config

        provider_id = str(connection.get("provider") or DIRECT_API_PROVIDER_ID).strip()
        base_url = str(connection.get("base_url") or "").rstrip("/")
        model = str(connection.get("model") or "").strip()
        auth_mode = str(connection.get("auth_mode") or "bearer").strip()
        key = connection.get("api_key")
        key = key.strip() if isinstance(key, str) else ""
        patch: dict[str, Any] = {"model": model or None, "web_search": connection.get("web_search") or None}
        if provider_id == "openai":
            patch["model_provider"] = "openai"
            patch["openai_base_url"] = base_url or None
            patch["api_key"] = key or None
            if self._catalog_pointer_is_managed(self._draft):
                patch["model_catalog_json"] = None
            return self._sync(config_text, auth_text, patch)
        path = ("model_providers", provider_id)
        current = codex_config._provider_mapping(config_text, provider_id)
        old_url = current.get("base_url")
        if not key and not connection.get("clear_key") and isinstance(old_url, str) and old_url.rstrip("/") == base_url and auth_mode == "bearer":
            key = str(current.get("experimental_bearer_token") or "")
        fields = {
            "name": str(current.get("name") or "")
            if current.get("name") else "Direct API",
            "base_url": base_url,
            "wire_api": "responses",
            "model_catalog_url": str(connection.get("model_catalog_url") or "").rstrip("/"),
        }
        for field, value in fields.items():
            config_text = (codex_config.set_table_value(config_text, path, field, value)
                           if value else codex_config.remove_table_value(config_text, path, field))
        # One auth carrier per row: Codex refuses a provider that states two.
        for field in ("experimental_bearer_token", "requires_openai_auth", "env_key"):
            config_text = codex_config.remove_table_value(config_text, path, field)
        if auth_mode == "bearer" and key:
            config_text = codex_config.set_table_value(config_text, path, "experimental_bearer_token", key)
        elif auth_mode == "openai_auth":
            config_text = codex_config.set_table_value(config_text, path, "requires_openai_auth", True)
        elif auth_mode == "env_key":
            env_key = str(connection.get("env_key") or "").strip()
            if env_key:
                config_text = codex_config.set_table_value(config_text, path, "env_key", env_key)
        supports_search = connection.get("supports_standalone_web_search")
        if isinstance(supports_search, bool):
            config_text = codex_config.set_table_value(
                config_text, path, "supports_standalone_web_search", supports_search
            )
        else:
            config_text = codex_config.remove_table_value(
                config_text, path, "supports_standalone_web_search"
            )
        patch["model_provider"] = provider_id
        # The auth document holds the proxy's master key while this app's API
        # is on.  Only an ``openai_auth`` row reads that document; a bearer
        # token and an ``env_key`` credential belong elsewhere, so the proxy key
        # is removed instead of being copied into a credential this row never
        # sends.
        patch["api_key"] = key or None if auth_mode == "openai_auth" else None
        if self._catalog_pointer_is_managed(self._draft):
            patch["model_catalog_json"] = None
        return self._sync(config_text, auth_text, patch)

    def _leave_local_api(self, config_text: str, auth_text: str) -> dict[str, Any]:
        """Stage the off position: the remembered direct route, if one exists.

        The Edit API form is the off action: it opens beside the pane and is
        what the person edits.  A remembered direct route is restored first so
        the switch really leaves this app's API; without one the dialog starts
        from the current yrapi values and nothing changes until it is saved.
        """

        connection = read_direct_connection(self.app_state_root, "codex")
        if connection is None:
            return self._draft
        return self._apply_direct_connection(config_text, auth_text, connection)

    def _adopt_local_api(
        self, config_text: str, auth_text: str, data: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Stage this app's proxy as the client's backend, keeping its model.

        One staging path for both entries that name it: the pane's switch and
        the Edit API form saved on the proxy's own endpoint.  The route, its
        master key, the model list and the search opt-in are one decision, so
        a second writer would only be able to fall out of step with this one.
        The connection being replaced is remembered first, because the switch's
        off position has to return to it.
        """

        self._remember_direct_connection()
        patch = self._local_api_patch()
        patch.update(self._surface_sync_patch())
        config_text = _remove_legacy_computer_mcp(config_text)
        model = str(data.get("model") or "").strip()
        if model:
            patch["model"] = model
        staged = self._sync(config_text, auth_text, patch)
        # This app's API carries its model catalog too: the picker has to see
        # the same public names the proxy serves, or adopting the API leaves
        # the client on its own two-model list.  Only a source that has
        # actually answered may turn the switch on; when the proxy is down or
        # exposes nothing, the previous catalog position is left alone.
        names = self._catalog_model_names(self._draft)
        if not names and self._draft.get("young_router_enabled") is True:
            self._refresh_live_catalog_source(force=True)
            names = self._catalog_model_names(self._draft)
        if names:
            staged = self._with_catalog_carrier(staged, enabled=True, names=names)
        return self._with_yrapi_catalog(staged)

    def _with_yrapi_catalog(self, staged: Mapping[str, Any]) -> dict[str, Any]:
        """Make the adopted row's catalog half match what the switch compares.

        The yrapi connection is one identity: base URL, credential, and (for
        Codex) the catalog address.  The catalog switch decides which carrier
        answers; the row must not keep a direct catalog URL beside a file
        pointer, because that is a third state the switch cannot name.  A
        built-in provider has no table to hold the address, so its catalog half
        is the empty string and only the managed pointer moves.
        """

        from .. import codex_config

        structured = staged.get("structured")
        structured = structured if isinstance(structured, Mapping) else {}
        active = str(structured.get("model_provider") or "openai").strip() or "openai"
        if active in RESERVED_PROVIDER_IDS:
            return dict(staged)
        config_text = staged.get("config_text", "")
        auth_text = staged.get("auth_text", "{}\n")
        if not isinstance(config_text, str) or not isinstance(auth_text, str):
            return dict(staged)
        path = ("model_providers", active)
        served = self._served_catalog_url()
        if self._catalog_delivery(staged) == "url":
            config_text = codex_config.set_table_value(
                config_text, path, "model_catalog_url", served
            )
        else:
            config_text = codex_config.remove_table_value(
                config_text, path, "model_catalog_url"
            )
        return self._sync(config_text, auth_text)

    def _surface_sync_patch(
        self,
        override: Mapping[str, bool] | None = None,
    ) -> dict[str, Any]:
        """Synchronize the installed desktop surfaces with Codex settings.

        A Codex app restart recreates the plugin cache even when the registry
        says the plugin is installed. Let ``/plugins`` install the missing
        files. The installed marketplace and CUA runtime declare the surfaces,
        so a later Desktop release can add one without a Young Router update.
        """

        structured = self._draft.get("structured")
        structured = structured if isinstance(structured, Mapping) else {}
        features = structured.get("features")
        if not isinstance(features, Mapping):
            raise DomainError("Codex feature settings are unavailable")
        plugins = structured.get("plugins")
        feature_plugins, _runtime_id = _installed_surface_plugins(self.codex_home_path())
        selected_features = tuple(
            feature
            for feature in feature_plugins
            if override is not None or features.get(feature) is not None
        )
        if not selected_features:
            return {}
        patch = _surface_sync_patch(
            self.codex_home_path(),
            features,
            [item for item in plugins if isinstance(item, Mapping)]
            if isinstance(plugins, list) else [],
            override=override,
            sync_plugins=selected_features,
        )
        return patch

    def sync_image_context_mcp(self) -> bool:
        """Keep this app's own MCP entry out of the client's file.

        The pass normally needs nothing from Codex's configuration: the desktop
        client can open a file the pass writes, and a gateway client adds the
        endpoint itself if it wants it.  So this only ever *removes* the entry an
        earlier build added, unless an operator picked the MCP server explicitly
        (``stdio``), in which case the entry is written to match.
        """

        import sys as _sys

        # Nothing in the client's file is written for the ordinary case any
        # more: a desktop client already has an image viewer, so the pass hands
        # it a file path instead of adding a server.  An entry is only kept when
        # an operator explicitly asks for the MCP server, and one written by an
        # earlier build is removed here rather than left behind.
        settings = image_context_settings(self.runtime_settings_path or _default_runtime_settings_path())
        enabled_now = settings["fetch"] == "stdio" and bool(settings["enabled"])
        structured = self._draft.get("structured")
        structured = structured if isinstance(structured, Mapping) else {}
        if enabled_now == image_context_mcp_registered(structured):
            return False
        config_text = self._draft.get("config_text", "")
        auth_text = self._draft.get("auth_text", "{}\n")
        if not isinstance(config_text, str) or not isinstance(auth_text, str):
            return False
        patch = image_context_mcp_patch(
            structured,
            enabled=enabled_now,
            python=str(_sys.executable or "python3"),
            python_path=_image_context_python_path(),
        )
        if not patch:
            return False
        written = self._sync(config_text, auth_text, patch)
        self._write_documents(written)
        self._raw = copy.deepcopy(written)
        self._draft = copy.deepcopy(written)
        self._baseline = (
            str(written.get("config_text", "")),
            str(written.get("auth_text", "{}\n")),
        )
        self.revision += 1
        return True

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
        config_text, auth_text, _, _ = self._editor_documents_from_disk()
        # An unapplied draft is the user's own copy of the document, not the
        # client's file.  This pass runs on the file, and its patch carries
        # every provider row it reads, so it must not publish staged edits.
        if (
            str(self._draft.get("config_text", "")),
            str(self._draft.get("auth_text", "{}\n")),
        ) != (config_text, auth_text):
            return False
        # Resolved against this installation's own records, so a start that had
        # to step past an occupied port rewrites the client onto the port it
        # actually bound instead of the preference the document already names.
        base_url, key = local_proxy_endpoint(self.runtime_config_path)
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
        endpoint_current = current.rstrip("/") == base_url.rstrip("/") and current_key == key
        # The search opt-in is transcribed from the same row, and this route is
        # the only thing that can answer it: Codex calls
        # ``{base_url}/alpha/search`` here, which is how a search reaches
        # DeepSeek's own ``/tools/search``.  A client adopted before the
        # opt-in was written carries none, registers no web tool at all, and
        # the agent drives a browser instead.  Only a *missing* key is healed:
        # an explicit ``false`` is the operator's own decision
        # (``codex_search_set``), and reviving it on every restart would undo
        # that.
        search_missing = not isinstance(
            next(
                (
                    item.get("supports_standalone_web_search")
                    for item in structured.get("providers") or []
                    if isinstance(item, Mapping)
                    and str(item.get("id") or "").strip() == active
                ),
                None,
            ),
            bool,
        )
        # The built-in OpenAI provider has no editable table, so no row here
        # can ever carry the opt-in; treating it as missing would rewrite the
        # client on every restart.
        if active == "openai":
            search_missing = False
        # ``web_search`` is the switch Codex actually reads, so an operator who
        # turned search off at the top level is just as decided as one who
        # wrote ``false`` on the row: leaving the opt-in absent cannot turn that
        # back on, and writing it would register an executor with the mode off.
        if str(structured.get("web_search") or "").strip().lower() == "disabled":
            search_missing = False
        if endpoint_current and not search_missing:
            return False
        patch = self._active_provider_patch(
            base_url=base_url,
            key=key,
            standalone_web_search=True if search_missing else None,
        )
        if search_missing:
            patch["web_search"] = structured.get("web_search") or "live"
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
