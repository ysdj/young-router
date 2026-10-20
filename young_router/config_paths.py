"""The one answer to "which file holds this installation's configuration".

Two answers used to exist.  The provider editor defaulted to
``<root>/.litellm-runtime/config.yaml`` while every other reader — the WebDAV
bundle, the usage-log reader, the Codex editor's own default, the JSON config
API — defaulted to ``<root>/config.yaml``.  An installation that never set
``LITELLM_CONFIG_FILE`` therefore held two configuration documents: the one the
editor wrote and the one everything else read, and neither was the other's
source.  A provider renamed in the editor kept its old spelling in the file
every other reader trusted, so the same installation described itself two ways.

``provider_config_path`` is now the only place that default is decided.  The
layout is:

- ``<root>/.litellm-runtime/config.yaml`` — the document the provider editor
  writes and the managed proxy loads.  This is *the* configuration.
- ``<root>/config.yaml`` — never written, never read.  A file that exists there
  belongs to a layout this module no longer produces; ``retire_stale_document``
  refuses to serve it and reports it so the caller can move it aside.  Refusing
  is the point: silently preferring one of two documents is how an edit landed
  in a file nothing read.

An explicit ``LITELLM_CONFIG_FILE`` still wins, because a packaged preview
profile names its own document.  That setting is one path for the whole process,
so it cannot create a second one.
"""

from __future__ import annotations

import os
from pathlib import Path

#: The one subdirectory that owns runtime state under an installation root.
RUNTIME_DIRECTORY = ".litellm-runtime"
CONFIG_FILENAME = "config.yaml"


def runtime_root(configured: Path | str | None = None) -> Path:
    """This installation's root directory.

    A caller that already knows its root passes it; otherwise the process-wide
    environment names it, defaulting to ``~/.young-router``.  Every other
    helper here derives from this, so an installation is one root.
    """

    if configured is not None:
        return Path(configured).expanduser()
    for name in ("LITELLM_RUNTIME_ROOT", "YOUNG_ROUTER_HOME"):
        value = os.environ.get(name, "").strip()
        if value:
            return Path(value).expanduser()
    return Path.home() / ".young-router"


def provider_config_path(root: Path | str | None = None) -> Path:
    """The one configuration document this installation has."""

    configured = os.environ.get("LITELLM_CONFIG_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return runtime_root(root) / RUNTIME_DIRECTORY / CONFIG_FILENAME


def legacy_provider_config_path(root: Path | str | None = None) -> Path:
    """Where the other default used to point, so a stale copy can be found."""

    return runtime_root(root) / CONFIG_FILENAME


def stale_document(root: Path | str | None = None) -> Path | None:
    """The retired root-level document, when one is still sitting there.

    Absent when the installation has always used one document, and absent for a
    profile that named its own path — that profile's chosen file may legitimately
    be the root-level name.
    """

    if os.environ.get("LITELLM_CONFIG_FILE", "").strip():
        return None
    legacy = legacy_provider_config_path(root)
    return legacy if legacy.is_file() else None


def retired_document_path(root: Path | str | None = None) -> Path:
    """Where ``retire_stale_document`` moves a retired copy.

    Beside the live document rather than beside the dead one: one directory
    holds this installation's configuration, so the record of what it used to
    contain is not a second candidate document in the root.
    """

    live = provider_config_path(root)
    return live.with_name(f"{live.stem}.retired{live.suffix}")
