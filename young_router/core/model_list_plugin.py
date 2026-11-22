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
OPERATIONS = {"status", "install", "uninstall", "enable", "disable", "sync"}
_LOCK = threading.RLock()


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
    try:
        config = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(config.get("enabled") and config.get("targets", {}).get(host))


def target_initialized(host: str) -> bool:
    """Keep the legacy generator retired after the plugin takes ownership."""
    try:
        config = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if "initializedTargets" not in config:
        # Configurations from the previous plugin version owned both targets.
        return True
    initialized = config.get("initializedTargets") or {}
    return bool(initialized.get(host) or (
        config.get("enabled") and config.get("targets", {}).get(host)
    ))


def status(host: str) -> dict:
    executable = _executable(host)
    if not executable:
        return {"installed": False, "available": False, "enabled": False}
    row = _row(host, executable)
    if not row:
        return {"installed": False, "available": True, "enabled": False}
    plugin = _json(str(_binary(host, row)), ["status", "--host", host])
    return {
        "installed": True, "available": True,
        "enabled": bool(row.get("enabled", True) and plugin.get("enabled") and plugin.get("targets", {}).get(host)),
        "host_enabled": bool(row.get("enabled", True)),
        "version": row.get("version", ""),
        "last_sync": (plugin.get("lastSyncAtByTarget") or {}).get(host, ""),
        "last_error": plugin.get("lastError", ""),
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
                _run(str(_binary(host, row)), ["disable", "--host", host])
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
