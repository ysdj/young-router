"""External desktop-client configuration files staged as raw documents.

Codex and Claude keep their own domains because their settings are also
structured.  The remaining clients only need direct file editing: the pi
coding agent, DeepSeek Harness (the ``dsh`` CLI and DSH Desktop), and
opencode.  This adapter owns exactly those registered files, so Core never
accepts an arbitrary path from the UI and the pane can show where each file
lives without a general filesystem capability.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from ..persistence import PersistenceError, atomic_write_text, read_bytes
from ._shared import DomainError, _action_name, _mapping

# Every document is a fixed, Core-owned path.  ``document`` is the value that
# travels through the versioned editor contract; ``name`` is the file name the
# pane shows next to that path; ``language`` selects the editor mode.
_DOCUMENT_SPECS: tuple[dict[str, Any], ...] = (
    {"id": "pi_settings", "client": "pi", "name": "settings.json", "language": "json", "strict_json": True},
    {"id": "pi_models", "client": "pi", "name": "models.json", "language": "json", "strict_json": True},
    {"id": "pi_auth", "client": "pi", "name": "auth.json", "language": "json", "strict_json": True},
    {"id": "dsh_settings", "client": "dsh", "name": "settings.yaml", "language": "yaml", "strict_json": False},
    {"id": "dsh_desktop_settings", "client": "dshDesktop", "name": "settings.yaml", "language": "yaml", "strict_json": False},
    {"id": "opencode_config", "client": "opencode", "name": "opencode.json", "language": "json", "strict_json": False},
    {"id": "opencode_auth", "client": "opencode", "name": "auth.json", "language": "json", "strict_json": True},
    # Codex keeps more than config.toml and auth.json: its TUI keybindings, the
    # global instruction file, and the command-execpolicy rules are separate
    # user files in the same home, so the pane lists and edits them as raw text.
    {"id": "codex_keybindings", "client": "codex", "name": "keybindings.json", "language": "json", "strict_json": True},
    {"id": "codex_agents", "client": "codex", "name": "AGENTS.md", "language": "text", "strict_json": False},
    {"id": "codex_rules", "client": "codex", "name": "default.rules", "language": "text", "strict_json": False},
)


def _home() -> Path:
    return Path.home()


def pi_config_dir() -> Path:
    """Return the pi coding agent's global configuration directory."""

    explicit = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
    return Path(explicit).expanduser() if explicit else _home() / ".pi" / "agent"


def dsh_home() -> Path:
    """Return the DeepSeek Harness CLI home that owns ``settings.yaml``."""

    explicit = os.environ.get("DSH_HOME", "").strip()
    return Path(explicit).expanduser() if explicit else _home() / ".dsh"


def dsh_desktop_home() -> Path:
    """Return the DSH Desktop data directory that owns ``harness/``.

    DSH Desktop is an Electron application, so its Harness home lives under
    the per-user application data directory instead of ``~/.dsh``.
    """

    explicit = os.environ.get("DSH_DESKTOP_HOME", "").strip()
    if explicit:
        root = Path(explicit).expanduser()
    elif sys.platform == "darwin":
        root = _home() / "Library" / "Application Support"
    elif os.name == "nt":
        app_data = os.environ.get("APPDATA", "").strip()
        root = Path(app_data).expanduser() if app_data else _home() / "AppData" / "Roaming"
    else:
        config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
        root = Path(config_home).expanduser() if config_home else _home() / ".config"
    if explicit:
        return root
    return root / "dsh-desktop"


def opencode_config_dir() -> Path:
    """Return the opencode configuration directory (XDG on every platform)."""

    explicit = os.environ.get("OPENCODE_CONFIG_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        app_data = os.environ.get("APPDATA", "").strip()
        root = Path(app_data).expanduser() if app_data else _home() / "AppData" / "Roaming"
    else:
        config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
        root = Path(config_home).expanduser() if config_home else _home() / ".config"
    return root / "opencode"


def opencode_data_dir() -> Path:
    """Return the opencode data directory that owns ``auth.json``."""

    explicit = os.environ.get("OPENCODE_DATA_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        root = Path(local_app_data).expanduser() if local_app_data else _home() / "AppData" / "Local"
    else:
        data_home = os.environ.get("XDG_DATA_HOME", "").strip()
        root = Path(data_home).expanduser() if data_home else _home() / ".local" / "share"
    return root / "opencode"


def _first_existing(candidates: tuple[Path, ...]) -> Path:
    for candidate in candidates:
        try:
            details = candidate.lstat()
        except OSError:
            continue
        if stat.S_ISREG(details.st_mode):
            return candidate
    return candidates[0]


def codex_home() -> Path:
    """Return the Codex home that owns its extra configuration files."""

    explicit = os.environ.get("CODEX_HOME", "").strip()
    return Path(explicit).expanduser() if explicit else _home() / ".codex"


def _client_paths() -> dict[str, Path]:
    codex_dir = codex_home()
    pi_dir = pi_config_dir()
    dsh_dir = dsh_home()
    desktop_dir = dsh_desktop_home()
    opencode_config = opencode_config_dir()
    opencode_data = opencode_data_dir()
    # opencode accepts both JSON and JSONC spellings for its global config and
    # keeps its credentials beside the data directory, not the config one.
    return {
        "pi_settings": pi_dir / "settings.json",
        "pi_models": pi_dir / "models.json",
        "pi_auth": pi_dir / "auth.json",
        "dsh_settings": dsh_dir / "settings.yaml",
        "dsh_desktop_settings": desktop_dir / "harness" / "settings.yaml",
        "opencode_config": _first_existing((opencode_config / "opencode.json", opencode_config / "opencode.jsonc")),
        "opencode_auth": opencode_data / "auth.json",
        "codex_keybindings": codex_dir / "keybindings.json",
        "codex_agents": codex_dir / "AGENTS.md",
        "codex_rules": codex_dir / "rules" / "default.rules",
    }


class ClientSettingsDomain:
    """Raw, staged documents for the remaining external client configs."""

    name = "clients"

    def __init__(self) -> None:
        self._paths = _client_paths()
        self.documents = frozenset(spec["id"] for spec in _DOCUMENT_SPECS)
        self._draft: dict[str, str] = {}
        self._baseline: dict[str, str] = {}
        self._baseline_bytes: dict[str, bytes | None] = {}
        self._baseline_signatures: dict[str, tuple[int, int, int] | None] = {}
        self.revision = 0
        self.reload()

    # -- registry -----------------------------------------------------------

    def _spec(self, document: object) -> dict[str, Any]:
        for spec in _DOCUMENT_SPECS:
            if spec["id"] == document:
                return spec
        raise DomainError("The requested client configuration is unavailable")

    def _read(self, document: str) -> tuple[str, bool]:
        path = self._paths[document]
        try:
            data = read_bytes(path)
        except PersistenceError:
            raise DomainError("The client configuration could not be read") from None
        if data is None:
            return "", False
        try:
            return data.decode("utf-8"), True
        except UnicodeDecodeError:
            raise DomainError("The client configuration must be UTF-8") from None

    def _file_bytes(self, document: str) -> bytes | None:
        try:
            return read_bytes(self._paths[document])
        except PersistenceError:
            raise DomainError("The client configuration could not be read") from None

    def _signature(self, document: str) -> tuple[int, int, int] | None:
        """Return a cheap file identity so snapshots never read file bytes."""

        try:
            details = self._paths[document].lstat()
        except OSError:
            return None
        if not stat.S_ISREG(details.st_mode):
            return None
        return (details.st_mtime_ns, details.st_size, details.st_ino)

    def client_files(self) -> list[dict[str, Any]]:
        """Return the display listing for this adapter's registered files.

        The versioned ``files`` IPC result is the only Core surface that
        carries a local path; it exists so the settings pane can show which
        file it edits.  Nothing here may expose file contents.
        """

        rows: list[dict[str, Any]] = []
        for spec in _DOCUMENT_SPECS:
            path = self._paths[spec["id"]]
            try:
                details = path.lstat()
                exists = stat.S_ISREG(details.st_mode)
            except OSError:
                exists = False
            rows.append(
                {
                    "id": spec["id"],
                    "client": spec["client"],
                    "domain": self.name,
                    "document": spec["id"],
                    "name": path.name,
                    "path": str(path),
                    "language": spec["language"],
                    "exists": exists,
                }
            )
        return rows

    # -- staging ------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        # The pane reads the file listing through the explicit ``files``
        # operation.  This snapshot stays file-free so refreshing every
        # window never re-probes seven unrelated client files.
        return {"domain": self.name, "revision": self.revision}

    def draft_state(self) -> object:
        return {"documents": copy.deepcopy(self._draft)}

    def raw_text(self, document: str) -> str:
        if document not in self.documents:
            raise DomainError("The requested client configuration is unavailable")
        return self._draft.get(document, "")

    def baseline_text(self, document: str) -> str:
        if document not in self.documents:
            raise DomainError("The requested client configuration is unavailable")
        return self._baseline.get(document, "")

    def dispatch(self, action: str, payload: object | None = None) -> dict[str, Any]:
        name = _action_name(action)
        data = _mapping(payload or {})
        if name in {"set_raw", "setraw"}:
            document = data.get("document")
            text = data.get("text")
            if not isinstance(text, str):
                raise DomainError("The client configuration must be text")
            self.stage(document, text)
        elif name in {"reset", "cancel", "reload"}:
            self._draft = copy.deepcopy(self._baseline)
        else:
            raise DomainError("The requested client configuration action is unavailable")
        self.revision += 1
        return self.snapshot()

    def stage(self, document: object, text: str) -> None:
        if document not in self.documents:
            raise DomainError("The requested client configuration is unavailable")
        if not isinstance(text, str) or len(text.encode("utf-8")) > 2 * 1024 * 1024:
            raise DomainError("The client configuration is invalid")
        self._draft[str(document)] = text

    # -- validation ---------------------------------------------------------

    def validate(self, payload: object | None = None) -> dict[str, Any]:
        documents = dict(self._draft)
        if payload is not None:
            data = _mapping(payload)
            document = data.get("document")
            text = data.get("text")
            if document in self.documents and isinstance(text, str):
                documents[str(document)] = text
        issues: list[dict[str, Any]] = []
        for spec in _DOCUMENT_SPECS:
            text = documents.get(spec["id"], "")
            if not text.strip():
                continue
            try:
                if spec["language"] == "yaml":
                    import yaml

                    yaml.safe_load(text)
                elif spec["strict_json"]:
                    json.loads(text)
            except Exception:
                issues.append(
                    {
                        "path": spec["id"],
                        "code": "invalid_syntax",
                        "message": f"{spec['name']} is not valid {spec['language'].upper()}",
                        "severity": "error",
                    }
                )
        return {"valid": not issues, "issues": issues}

    # -- persistence --------------------------------------------------------

    def persistence_paths(self) -> tuple[Path, ...]:
        return tuple(dict.fromkeys(self._paths.values()))

    def external_disk_state(self) -> dict[str, bool]:
        changed = any(
            self._signature(document) != self._baseline_signatures.get(document)
            for document in self.documents
        )
        return {"changed": changed}

    def external_disk_identity(self) -> str:
        digest = hashlib.sha256()
        for document in sorted(self.documents):
            signature = self._signature(document)
            digest.update(repr(signature).encode("ascii"))
        return digest.hexdigest()

    def rebase_external_disk(self) -> dict[str, Any]:
        self._baseline_signatures = {document: self._signature(document) for document in self.documents}
        self._baseline_bytes = {document: self._file_bytes(document) for document in self.documents}
        self.revision += 1
        return self.snapshot()

    def apply(self, payload: object | None = None) -> dict[str, Any]:
        if payload is not None:
            data = _mapping(payload)
            document = data.get("document")
            text = data.get("text")
            if document in self.documents and isinstance(text, str):
                self.stage(document, text)
        validation = self.validate()
        if not validation["valid"]:
            raise DomainError("The client configuration is invalid")
        # The staged draft is only trustworthy while every file still matches
        # the bytes this adapter last read; otherwise Apply would overwrite an
        # edit made outside the window.
        changed = [
            document
            for document in sorted(self.documents)
            if self._file_bytes(document) != self._baseline_bytes.get(document)
        ]
        if changed:
            raise DomainError("The client configuration changed on disk; reload before applying")
        for document in sorted(self.documents):
            text = self._draft.get(document, "")
            if text == self._baseline.get(document, ""):
                continue
            try:
                atomic_write_text(self._paths[document], text)
            except PersistenceError:
                raise DomainError("The client configuration could not be saved") from None
        self.reload()
        return {"applied": True, **self.snapshot()}

    def reload(self) -> dict[str, Any]:
        documents: dict[str, str] = {}
        baseline: dict[str, bytes | None] = {}
        signatures: dict[str, tuple[int, int, int] | None] = {}
        for spec in _DOCUMENT_SPECS:
            text, _exists = self._read(spec["id"])
            documents[spec["id"]] = text
            baseline[spec["id"]] = self._file_bytes(spec["id"])
            signatures[spec["id"]] = self._signature(spec["id"])
        self._draft = documents
        self._baseline = copy.deepcopy(documents)
        self._baseline_bytes = baseline
        self._baseline_signatures = signatures
        self.revision += 1
        return self.snapshot()

    def export(self, *, include_sensitive: bool = False) -> dict[str, Any]:
        return self.snapshot()


__all__ = [
    "ClientSettingsDomain",
    "codex_home",
    "dsh_desktop_home",
    "dsh_home",
    "opencode_config_dir",
    "opencode_data_dir",
    "pi_config_dir",
]
