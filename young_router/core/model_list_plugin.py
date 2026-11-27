"""Manage the model-list-plugin using the agent's plugin manager."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

NAME = "model-list-plugin"
PLUGIN_ID = f"{NAME}@{NAME}"
HOSTS = {"codex", "claude"}
OPERATIONS = {"status", "install", "uninstall", "enable", "disable", "sync", "takeover"}
_LOCK = threading.RLock()


def _desktop_claude_roots() -> list[Path]:
    """Where Claude Desktop installs the copy of Claude Code its Code tab runs."""

    if sys.platform == "darwin":
        return [Path.home() / "Library/Application Support" / "Claude-3p" / "claude-code"]
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return [Path(local) / "Claude-3p" / "claude-code"]
    return []


def _desktop_claude_cli(roots: list[Path] | None = None) -> str | None:
    """The Claude Code CLI bundled with Claude Desktop, newest install first.

    Claude Desktop's Code tab runs its own copy of Claude Code and puts no
    ``claude`` command on PATH, so an app-only machine has no command for the
    plugin manager even though the client is installed.  The copy sits under a
    versioned directory whose names change with every app update, hence the
    newest-first walk rather than a pinned build.  A CLI on PATH still wins:
    that is the installation the user chose to put there.
    """

    suffix = (
        Path("claude.app/Contents/MacOS/claude")
        if sys.platform == "darwin"
        else Path("claude.exe")
    )
    for root in _desktop_claude_roots() if roots is None else roots:
        versions: list[tuple[float, Path]] = []
        try:
            for entry in root.iterdir():
                if not entry.is_dir():
                    continue
                try:
                    versions.append((entry.stat().st_mtime, entry))
                except OSError:
                    continue
        except OSError:
            continue
        for _, version in sorted(versions, reverse=True):
            try:
                bundles = [entry for entry in version.iterdir() if entry.is_dir()]
            except OSError:
                continue
            for bundle in bundles:
                candidate = bundle / suffix
                try:
                    if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                        return str(candidate)
                except OSError:
                    continue
    return None


def _executable(host: str) -> str | None:
    executable = shutil.which(host)
    if executable:
        return executable
    home = Path.home()
    directories = [home / ".local/bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    if os.name == "nt":
        directories = [home / ".local/bin", Path(os.environ.get("APPDATA", "")) / "npm"]
    for directory in directories:
        for name in ([host + ".exe", host + ".cmd"] if os.name == "nt" else [host]):
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)
    if host == "codex" and sys.platform == "darwin":
        from .domains.codex import _installed_codex_cli
        candidate = _installed_codex_cli(Path(os.environ.get("CODEX_HOME") or home / ".codex"))
        if candidate:
            return str(candidate)
    if host == "claude":
        # An app-only machine reaches its plugin manager through the copy of
        # Claude Code that Claude Desktop installed, not a command on PATH.
        bundled = _desktop_claude_cli()
        if bundled:
            return bundled
    return None


def _run(executable: str, args: list[str]) -> str:
    command = [executable, *args]
    if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
        command = ["cmd.exe", "/d", "/c", *command]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, cwd=Path.home(), check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "Plugin command failed").strip())
    return result.stdout


def _json(executable: str, args: list[str]):
    return json.loads(_run(executable, args))


def _row(host: str, executable: str) -> dict | None:
    data = _json(executable, ["plugin", "list", "--json"])
    entries = data.get("installed", []) if host == "codex" else data
    for row in entries:
        if (row.get("pluginId") or row.get("id")) == PLUGIN_ID and (host != "claude" or row.get("scope") == "user"):
            return row
    return None


def _binary(host: str, row: dict) -> Path:
    if host == "claude":
        root = Path(row["installPath"])
    else:
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        root = home / "plugins/cache" / NAME / NAME / row["version"]
    return root / "bin" / (NAME + (".exe" if os.name == "nt" else ""))


def _config_path() -> Path:
    data = os.environ.get("MODEL_LIST_PLUGIN_DATA")
    if not data:
        if sys.platform == "darwin":
            data = str(Path.home() / "Library/Application Support" / NAME)
        elif os.name == "nt":
            data = str(Path(os.environ.get("APPDATA") or Path.home()) / NAME)
        else:
            data = str(Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / NAME)
    return Path(os.environ.get("MODEL_LIST_PLUGIN_CONFIG") or Path(data) / "config.json")


def target_enabled(host: str) -> bool:
    """Whether the plugin's configuration has this host's model list turned on.

    Half of the ownership answer: the plugin writes this host's list only while
    its configuration says so.  The other half is the client's own document
    (``enabledPlugins``), because the client runs the plugin's hooks only while
    it enables that plugin; the Claude domain joins the two.
    """
    try:
        config = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(config.get("enabled") and config.get("targets", {}).get(host))


#: The first plugin release whose Claude projection writes one row per model,
#: uses the provider's ``claude_alias``/``behaves_as`` metadata, and drops
#: generators.  A takeover on an older plugin would replace the app's picker
#: with the four-rows-per-model list the handoff exists to avoid.
MIN_CLAUDE_METADATA_VERSION = (0, 3, 0)


def version_tuple(value: object) -> tuple[int, ...]:
    """A plugin version as comparable numbers; a pre-release suffix is ignored."""

    parts: list[int] = []
    for part in str(value or "").strip().split("."):
        digits = ""
        for char in part:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _supports_claude_metadata(version: object) -> bool:
    return version_tuple(version) >= MIN_CLAUDE_METADATA_VERSION


def set_alias_mode(mode: str) -> None:
    """Set the Claude projection mode without dropping the other plugin keys."""

    path = _config_path()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        config = {}
    except (OSError, ValueError) as exc:
        raise RuntimeError("The model-list-plugin configuration is unreadable") from exc
    if not isinstance(config, dict):
        raise RuntimeError("The model-list-plugin configuration must be a JSON object")
    config["claudeAliasMode"] = mode
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def ensure_claude_owner() -> dict:
    """Install/enable the plugin and make its Claude projection the YR one.

    The plugin must be the one writer of Claude Code's model list; this is the
    operation the "Use Young Router API" switch calls after the gateway
    connection is saved.  It is idempotent: an installed, enabled plugin only
    gets the metadata mode and a fresh sync.
    """

    with _LOCK:
        executable = _executable("claude")
        if not executable:
            raise RuntimeError("Claude Code is unavailable")
        row = _row("claude", executable)
        if row is None:
            managed = manage("claude", "install")
            if not managed.get("installed"):
                raise RuntimeError("The model-list plugin did not install")
        row = _row("claude", executable)
        if row is None:
            raise RuntimeError("The model-list plugin is not installed")
        if not _supports_claude_metadata(row.get("version")):
            raise RuntimeError(
                "Update model-list-plugin before handing over Claude's model list"
            )
        # Installation's own sync used the default mode; set the provider-
        # published mode before the write that matters, then sync once.
        set_alias_mode("metadata")
        current = status("claude")
        if not current.get("host_enabled") or not current.get("enabled"):
            manage("claude", "enable")
        manage("claude", "sync")
        return status("claude")


def status(host: str) -> dict:
    executable = _executable(host)
    if not executable:
        return {"installed": False, "available": False, "enabled": False}
    row = _row(host, executable)
    if not row:
        return {"installed": False, "available": True, "enabled": False}
    plugin = _json(str(_binary(host, row)), ["status", "--host", host])
    # A plugin that reports per-host errors must never hand one host's failure
    # to the other's card.  Older plugin versions only carry the global field;
    # for them the fallback is the best available attribution.
    by_host = plugin.get("lastErrorByHost")
    last_error = (
        str(by_host.get(host) or "")
        if isinstance(by_host, dict)
        else str(plugin.get("lastError") or "")
    )
    return {
        "installed": True, "available": True,
        "enabled": bool(row.get("enabled", True) and plugin.get("enabled") and plugin.get("targets", {}).get(host)),
        "host_enabled": bool(row.get("enabled", True)),
        "version": row.get("version", ""),
        "last_sync": (plugin.get("lastSyncAtByTarget") or {}).get(host, ""),
        "last_error": last_error,
        "last_update_error": (plugin.get("lastUpdateErrorByHost") or {}).get(host, ""),
    }


def sync_if_enabled(host: str) -> dict:
    """Refresh an installed plugin after the client settings commit."""
    if not target_enabled(host):
        return {}
    with _LOCK:
        executable = _executable(host)
        row = _row(host, executable) if executable else None
        if not row or not row.get("enabled", True):
            return {}
        try:
            _run(str(_binary(host, row)), ["sync", "--host", host])
        except RuntimeError:
            # Sync records its fetch error separately from the connection save.
            pass
        return status(host)


def manage(host: str, action: str) -> dict:
    if host not in HOSTS or action not in OPERATIONS:
        raise ValueError("Invalid model-list-plugin request")
    with _LOCK:
        if action == "status":
            return status(host)
        executable = _executable(host)
        if not executable:
            raise RuntimeError(f"{host} is unavailable")
        row = _row(host, executable)
        if action == "takeover":
            if host != "claude":
                raise RuntimeError("Takeover is only available for Claude Code")
            return ensure_claude_owner()
        if action == "install":
            if row is None:
                source = os.environ.get("MODEL_LIST_PLUGIN_MARKETPLACE") or "ysdj/model-list-plugin"
                if host == "codex":
                    marketplaces = _json(executable, ["plugin", "marketplace", "list", "--json"])
                    if not any(item.get("name") == NAME for item in marketplaces.get("marketplaces", [])):
                        _run(executable, ["plugin", "marketplace", "add", source])
                    _run(executable, ["plugin", "add", PLUGIN_ID])
                else:
                    _run(executable, ["plugin", "install", NAME, "--marketplace", source, "--scope", "user"])
                row = _row(host, executable)
            if row is None:
                raise RuntimeError("The agent did not report the installed plugin")
            if not row.get("enabled", True):
                _run(executable, ["plugin", "add", PLUGIN_ID] if host == "codex" else ["plugin", "enable", PLUGIN_ID, "--scope", "user"])
            binary = str(_binary(host, row))
            _run(binary, ["install", "--host", host])
            _run(binary, ["enable", "--host", host, "--defer"])
            try:
                _run(binary, ["sync", "--host", host])
            except RuntimeError:
                # The installation is complete even when a provider needs setup.
                # The plugin records the synchronization error for the form.
                pass
        elif action == "uninstall":
            if row:
                # The plugin is being removed for every host it serves, so its
                # own uninstall (both targets restored, local config and state
                # deleted) has to run before the host forgets it.  Disabling
                # only this host would leave the other host's catalog pointer
                # aimed at a file nothing updates any more.
                _run(str(_binary(host, row)), ["uninstall"])
                _run(executable, ["plugin", "remove" if host == "codex" else "uninstall", PLUGIN_ID])
        else:
            if row is None:
                raise RuntimeError("Install model-list-plugin first")
            binary = str(_binary(host, row))
            if action in {"enable", "disable"}:
                if action == "enable" and not row.get("enabled", True):
                    _run(executable, ["plugin", "add", PLUGIN_ID] if host == "codex" else ["plugin", "enable", PLUGIN_ID, "--scope", "user"])
                _run(binary, [action, "--host", host])
            else:
                _run(binary, ["sync", "--host", host])
        return status(host)
