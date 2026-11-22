"""Start the browser-managed gateway shipped in the Linux image."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets

from . import config_paths
from .core import host_profile
from .core.domains.runtime import (
    RuntimeSettingsDomain,
    master_key_from_document,
    master_key_in_document,
)
from .core.persistence import atomic_write_text
from .node_runtime import core_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve", "web-password", "api-key"), nargs="?", default="serve")
    command = parser.parse_args(argv).command
    root = config_paths.runtime_root()
    source = config_paths.provider_config_path(root)
    if command == "web-password":
        password = RuntimeSettingsDomain().web_password()
        if not password:
            parser.exit(1, "Core has not initialized the web password yet.\n")
        print(password)
        return 0
    if command == "api-key":
        configured = host_profile.stated_key()
        if os.environ.get(host_profile.PROXY_KEY_ENV) and not configured:
            parser.exit(1, "The configured gateway API key file is missing, unreadable or empty.\n")
        if configured:
            print(configured)
            return 0
        try:
            print(master_key_from_document(source.read_text(encoding="utf-8")))
        except OSError:
            parser.exit(1, f"The provider configuration is unavailable: {source}\n")
        return 0

    os.umask(0o077)
    private_dir = root / config_paths.RUNTIME_DIRECTORY
    staged = Path(os.environ.setdefault("LITELLM_RUNTIME_CONFIG", str(private_dir / "served.yaml")))
    if staged.resolve() == source.resolve():
        parser.error("The gateway needs distinct editor and staged configuration paths.")

    # A configured host key is authoritative, including on the first start.
    # A missing mount must not make a gateway silently adopt a different key.
    host_key = host_profile.stated_key()
    if os.environ.get(host_profile.PROXY_KEY_ENV) and not host_key:
        parser.error("The configured gateway API key file is missing, unreadable or empty.")

    if not source.exists():
        # Core cannot publish its settings page until its proxy can stage a
        # document. Start with an empty provider list and a private client key.
        key = host_key or ("sk-" + secrets.token_urlsafe(32))
        try:
            atomic_write_text(source, master_key_in_document("providers: {}\nmodel_list: []\n", key))
        except ValueError as exc:
            parser.exit(1, f"The gateway API key is invalid: {exc}\n")

    from .core.__main__ import run

    return run([
        "--manage-service", "--web-require-password",
        "--web-host", str(core_root() / "web"),
        "--runtime-root", str(root), "--config", str(source),
        "--metadata", str(private_dir / "core-metadata.json"),
    ])


if __name__ == "__main__":
    raise SystemExit(main())
