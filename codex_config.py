#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

try:
    import yaml
except Exception as exc:  # pragma: no cover - surfaced in CLI output
    print(f"PyYAML is required: {exc}", file=sys.stderr)
    sys.exit(1)


ROOT = Path(__file__).resolve().parent


def default_config_yaml() -> Path:
    config_file = os.environ.get("LITELLM_CONFIG_FILE", "").strip()
    if config_file:
        return Path(config_file).expanduser()

    runtime_root = os.environ.get("LITELLM_RUNTIME_ROOT", "").strip()
    if not runtime_root:
        runtime_root = os.environ.get("LITELLM_MENU_HOME", "").strip()
    if runtime_root:
        return Path(runtime_root).expanduser() / "config.yaml"

    return Path.home() / ".litellm-menu" / "config.yaml"


CONFIG_YAML = default_config_yaml()
DEFAULT_PROVIDER = "newapi"
DEFAULT_WIRE_API = "responses"
DEFAULT_MODEL = "default-chat"
LOCAL_HOST = "127.0.0.1"
DEFAULT_LOCAL_PORT = "4000"
LOCAL_CONFIG_STATE_FILE = ".litellm-menu-codex-local-config-state.json"


def local_port() -> str:
    value = os.environ.get("LITELLM_PORT", "").strip() or DEFAULT_LOCAL_PORT
    if not value.isdigit():
        return DEFAULT_LOCAL_PORT
    port = int(value)
    if port < 1 or port > 65535:
        return DEFAULT_LOCAL_PORT
    return str(port)


LOCAL_BASE_URL = f"http://{LOCAL_HOST}:{local_port()}/v1"


def usage() -> int:
    print("usage: codex_config.py {local|reapply-pre-switch}", file=sys.stderr)
    return 64


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()


def load_litellm_config() -> dict:
    with CONFIG_YAML.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError("config.yaml must be a YAML mapping")
    return data


def toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def split_lines(text: str) -> list[str]:
    return text.splitlines()


def first_table_index(lines: list[str]) -> int:
    for index, line in enumerate(lines):
        if line.lstrip().startswith("["):
            return index
    return len(lines)


def set_top_level_key(text: str, key: str, value: object) -> str:
    lines = split_lines(text)
    limit = first_table_index(lines)
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    replacement = f"{key} = {toml_value(value)}"

    for index in range(limit):
        if pattern.match(lines[index]):
            lines[index] = replacement
            return "\n".join(lines).rstrip() + "\n"

    if limit == len(lines):
        lines.append(replacement)
    else:
        insert_at = limit
        if insert_at > 0 and lines[insert_at - 1].strip():
            lines.insert(insert_at, "")
        lines.insert(insert_at, replacement)
    return "\n".join(lines).rstrip() + "\n"


def table_bounds(lines: list[str], table: str) -> tuple[int, int] | None:
    header = re.compile(rf"^\s*\[{re.escape(table)}\]\s*(?:#.*)?$")
    for start, line in enumerate(lines):
        if not header.match(line):
            continue
        end = start + 1
        while end < len(lines):
            stripped = lines[end].lstrip()
            if stripped.startswith("[") and not stripped.startswith("#"):
                break
            end += 1
        return start, end
    return None


def set_table_values(text: str, table: str, values: dict[str, object]) -> str:
    lines = split_lines(text)
    bounds = table_bounds(lines, table)

    if bounds is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{table}]")
        for key, value in values.items():
            lines.append(f"{key} = {toml_value(value)}")
        return "\n".join(lines).rstrip() + "\n"

    start, end = bounds
    seen: set[str] = set()
    for index in range(start + 1, end):
        for key, value in values.items():
            pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
            if pattern.match(lines[index]):
                lines[index] = f"{key} = {toml_value(value)}"
                seen.add(key)
                break

    insert_at = end
    for key, value in values.items():
        if key not in seen:
            lines.insert(insert_at, f"{key} = {toml_value(value)}")
            insert_at += 1
    return "\n".join(lines).rstrip() + "\n"


def cleanup_legacy_backups(path: Path, keep: Path) -> None:
    for pattern in (f"{path.name}.bak-*", f"{path.name}.pre-restore-bak-*"):
        for backup_path in path.parent.glob(pattern):
            if backup_path == keep or not backup_path.is_file():
                continue
            backup_path.unlink()


def backup(path: Path) -> Path | None:
    backup_path = path.with_name(f"{path.name}.bak")
    cleanup_legacy_backups(path, backup_path)
    if not path.exists():
        return None
    shutil.copy2(path, backup_path)
    return backup_path


def existing_backup(path: Path) -> Path | None:
    backup_path = path.with_name(f"{path.name}.bak")
    cleanup_legacy_backups(path, backup_path)
    return backup_path if backup_path.is_file() else None


def copy_file_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        shutil.copy2(source, tmp)
        tmp.replace(target)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_atomic(path: Path, text: str, mode: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    if mode is not None:
        os.chmod(tmp, mode)
    tmp.replace(path)


def local_config_state_path(home: Path) -> Path:
    return home / LOCAL_CONFIG_STATE_FILE


def load_local_config_state(home: Path) -> dict:
    path = local_config_state_path(home)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def local_config_state_active(home: Path, old_config: str) -> bool:
    state = load_local_config_state(home)
    if state.get("active") is not True:
        return False
    if state.get("target_base_url") != LOCAL_BASE_URL:
        return False
    if LOCAL_BASE_URL not in old_config:
        return False
    return (home / "config.toml.bak").is_file() or (home / "auth.json.bak").is_file()


def write_local_config_state(
    home: Path,
    *,
    model: str,
    provider: str,
    api_base: str,
    config_backup: Path | None,
    auth_backup: Path | None,
) -> None:
    state = {
        "schema_version": 1,
        "active": True,
        "target_base_url": api_base,
        "target_model": model,
        "target_model_provider": provider,
        "config_backup": str(config_backup) if config_backup else None,
        "auth_backup": str(auth_backup) if auth_backup else None,
    }
    write_atomic(local_config_state_path(home), json.dumps(state, indent=2) + "\n", 0o600)


def clear_local_config_state(home: Path) -> None:
    try:
        local_config_state_path(home).unlink()
    except FileNotFoundError:
        pass


def update_auth(path: Path, api_key: str) -> tuple[dict, int | None]:
    mode = None
    if path.exists():
        mode = path.stat().st_mode & 0o777
        raw = path.read_text(encoding="utf-8").strip()
        auth = json.loads(raw) if raw else {}
        if not isinstance(auth, dict):
            raise ValueError(f"{path} must contain a JSON object")
    else:
        auth = {}
    auth["OPENAI_API_KEY"] = api_key
    if mode is None:
        mode = 0o600
    return auth, mode


def update_codex_files(
    *,
    model: str,
    api_base: str,
    api_key: str,
    summary: str,
) -> None:
    home = codex_home()
    config_path = home / "config.toml"
    auth_path = home / "auth.json"
    provider = os.environ.get("CODEX_MODEL_PROVIDER", DEFAULT_PROVIDER)

    old_config = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    config_mode = (config_path.stat().st_mode & 0o777) if config_path.exists() else 0o600
    old_auth, auth_mode = update_auth(auth_path, api_key)

    new_config = set_top_level_key(old_config, "model_provider", provider)
    new_config = set_top_level_key(new_config, "model", model)
    new_config = set_table_values(
        new_config,
        f"model_providers.{provider}",
        {
            "name": provider,
            "base_url": api_base,
            "wire_api": DEFAULT_WIRE_API,
            "requires_openai_auth": True,
        },
    )
    new_auth = json.dumps(old_auth, indent=2, ensure_ascii=False) + "\n"

    if local_config_state_active(home, old_config):
        config_backup = existing_backup(config_path)
        auth_backup = existing_backup(auth_path)
    else:
        config_backup = backup(config_path)
        auth_backup = backup(auth_path)
    write_atomic(config_path, new_config, config_mode)
    write_atomic(auth_path, new_auth, auth_mode)
    write_local_config_state(
        home,
        model=model,
        provider=provider,
        api_base=api_base,
        config_backup=config_backup,
        auth_backup=auth_backup,
    )

    print(summary)
    print(f"model: {model}")
    print(f"base_url: {api_base}")
    print(f"config: {config_path}")
    print(f"auth: {auth_path}")
    print(f"saved pre-switch config file: {config_backup if config_backup else '(new file)'}")
    print(f"saved pre-switch auth file: {auth_backup if auth_backup else '(new file)'}")
    print("Restart Codex to apply.")


def apply_local() -> None:
    data = load_litellm_config()
    master_key = (
        data.get("general_settings", {}).get("master_key")
        if isinstance(data.get("general_settings"), dict)
        else None
    )
    api_key = str(master_key or "sk-local-litellm")
    model = os.environ.get("CODEX_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    update_codex_files(
        model=model,
        api_base=LOCAL_BASE_URL,
        api_key=api_key,
        summary="Codex config updated to use the local LiteLLM service.",
    )


def reapply_pre_switch() -> None:
    home = codex_home()
    state = load_local_config_state(home)
    if state.get("active") is not True or state.get("target_base_url") != LOCAL_BASE_URL:
        raise FileNotFoundError("No active pre-switch Codex config state found. Nothing to reapply.")

    files = [home / "config.toml", home / "auth.json"]
    restored: list[tuple[Path, Path]] = []
    missing: list[Path] = []

    for path in files:
        backup_path = path.with_name(f"{path.name}.bak")
        cleanup_legacy_backups(path, backup_path)
        if not backup_path.is_file():
            missing.append(backup_path)
            continue
        copy_file_atomic(backup_path, path)
        restored.append((path, backup_path))

    if not restored:
        missing_text = "\n".join(f"missing saved pre-switch file: {path}" for path in missing)
        raise FileNotFoundError(f"No saved pre-switch Codex config files found.\n{missing_text}")

    print("Codex config reapplied from saved pre-switch files.")
    for path, backup_path in restored:
        print(f"reapplied: {path}")
        print(f"from saved file: {backup_path}")
    for path in missing:
        print(f"missing saved pre-switch file: {path}")
    clear_local_config_state(home)
    print("Restart Codex to apply.")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return usage()
    command = argv[1]
    try:
        if command == "local":
            apply_local()
            return 0
        if command == "reapply-pre-switch":
            reapply_pre_switch()
            return 0
    except Exception as exc:
        print(f"Codex config update failed: {exc}", file=sys.stderr)
        return 1
    return usage()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
