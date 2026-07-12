#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
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
LOCAL_HOST = "127.0.0.1"
DEFAULT_LOCAL_PORT = "4000"
LOCAL_CONFIG_STATE_FILE = ".litellm-menu-codex-local-config-state.json"
LOCAL_CONFIG_STATE_SCHEMA_VERSION = 3
PROVIDER_MANAGED_VALUES = {
    "name": "OpenAI",
    "base_url": None,
    "wire_api": "responses",
    "requires_openai_auth": True,
}


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


def strip_inline_toml_comment(value: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    for index, char in enumerate(value):
        if in_double:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_double = False
            continue
        if in_single:
            if char == "'":
                in_single = False
            continue
        if char == '"':
            in_double = True
        elif char == "'":
            in_single = True
        elif char == "#":
            return value[:index].rstrip()
    return value.strip()


def parse_toml_scalar(value: str) -> object:
    raw = strip_inline_toml_comment(value).strip()
    if raw.startswith('"'):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"unsupported TOML string: {raw}") from exc
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1]
    if raw == "true":
        return True
    if raw == "false":
        return False
    if re.fullmatch(r"[+-]?\d+", raw):
        return int(raw)
    if re.fullmatch(r"[+-]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][+-]?\d+)?", raw):
        return float(raw)
    raise ValueError(f"unsupported TOML scalar: {raw}")


def key_value_in_range(lines: list[str], start: int, end: int, key: str) -> tuple[bool, object | None]:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*)$")
    for index in range(start, end):
        if lines[index].lstrip().startswith("#"):
            continue
        match = pattern.match(lines[index])
        if match:
            return True, parse_toml_scalar(match.group(1))
    return False, None


def top_level_value(text: str, key: str) -> tuple[bool, object | None]:
    lines = split_lines(text)
    return key_value_in_range(lines, 0, first_table_index(lines), key)


def table_value(text: str, table: str, key: str) -> tuple[bool, object | None]:
    lines = split_lines(text)
    bounds = table_bounds(lines, table)
    if bounds is None:
        return False, None
    start, end = bounds
    return key_value_in_range(lines, start + 1, end, key)


def remove_top_level_key(text: str, key: str) -> str:
    lines = split_lines(text)
    limit = first_table_index(lines)
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    lines = [line for index, line in enumerate(lines) if index >= limit or not pattern.match(line)]
    return "\n".join(lines).rstrip() + "\n" if lines else ""


def remove_table_key(text: str, table: str, key: str, *, remove_empty_table: bool = False) -> str:
    lines = split_lines(text)
    bounds = table_bounds(lines, table)
    if bounds is None:
        return "\n".join(lines).rstrip() + "\n" if lines else ""

    start, end = bounds
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    index = start + 1
    while index < end:
        if pattern.match(lines[index]):
            del lines[index]
            end -= 1
            continue
        index += 1

    if remove_empty_table and all(not line.strip() for line in lines[start + 1 : end]):
        del lines[start:end]
    return "\n".join(lines).rstrip() + "\n" if lines else ""


def remove_empty_table(text: str, table: str) -> str:
    lines = split_lines(text)
    bounds = table_bounds(lines, table)
    if bounds is None:
        return "\n".join(lines).rstrip() + "\n" if lines else ""
    start, end = bounds
    if all(not line.strip() for line in lines[start + 1 : end]):
        del lines[start:end]
    return "\n".join(lines).rstrip() + "\n" if lines else ""


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


def local_config_state_active(state: dict) -> bool:
    return (
        state.get("schema_version") == LOCAL_CONFIG_STATE_SCHEMA_VERSION
        and state.get("active") is True
        and isinstance(state.get("config"), dict)
        and isinstance(state.get("auth"), dict)
    )


def write_local_config_state(home: Path, state: dict) -> None:
    write_atomic(local_config_state_path(home), json.dumps(state, indent=2) + "\n", 0o600)


def clear_local_config_state(home: Path) -> None:
    try:
        local_config_state_path(home).unlink()
    except FileNotFoundError:
        pass


def load_auth(path: Path) -> tuple[dict, int]:
    mode = None
    if path.exists():
        mode = path.stat().st_mode & 0o777
        raw = path.read_text(encoding="utf-8").strip()
        auth = json.loads(raw) if raw else {}
        if not isinstance(auth, dict):
            raise ValueError(f"{path} must contain a JSON object")
    else:
        auth = {}
    if mode is None:
        mode = 0o600
    return auth, mode


def field_snapshot(present: bool, value: object | None) -> dict:
    snapshot = {"present": present}
    if present:
        snapshot["value"] = value
    return snapshot


def capture_initial_state(
    *,
    config_path: Path,
    auth_path: Path,
    old_config: str,
    old_auth: dict,
    provider: str,
) -> dict:
    table = f"model_providers.{provider}"
    top_present, top_value = top_level_value(old_config, "model_provider")
    provider_fields = {
        key: field_snapshot(*table_value(old_config, table, key))
        for key in PROVIDER_MANAGED_VALUES
    }
    return {
        "schema_version": LOCAL_CONFIG_STATE_SCHEMA_VERSION,
        "active": True,
        "target_base_url": LOCAL_BASE_URL,
        "target_model_provider": provider,
        "config": {
            "file_present": config_path.exists(),
            "top_level": {
                "model_provider": field_snapshot(top_present, top_value),
            },
            "providers": {
                provider: {
                    "table_present": table_bounds(split_lines(old_config), table) is not None,
                    "fields": provider_fields,
                }
            },
        },
        "auth": {
            "file_present": auth_path.exists(),
            "OPENAI_API_KEY": field_snapshot(
                "OPENAI_API_KEY" in old_auth,
                old_auth.get("OPENAI_API_KEY"),
            ),
        },
    }


def ensure_provider_snapshot(state: dict, old_config: str, provider: str) -> None:
    config_state = state["config"]
    providers = config_state.setdefault("providers", {})
    if provider in providers:
        return
    table = f"model_providers.{provider}"
    providers[provider] = {
        "table_present": table_bounds(split_lines(old_config), table) is not None,
        "fields": {
            key: field_snapshot(*table_value(old_config, table, key))
            for key in PROVIDER_MANAGED_VALUES
        },
    }


def restore_top_level_field(text: str, key: str, snapshot: dict) -> str:
    if snapshot.get("present") is True:
        return set_top_level_key(text, key, snapshot.get("value"))
    return remove_top_level_key(text, key)


def restore_table_field(text: str, table: str, key: str, snapshot: dict) -> str:
    if snapshot.get("present") is True:
        return set_table_values(text, table, {key: snapshot.get("value")})
    return remove_table_key(text, table, key)


def update_codex_files(
    *,
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
    old_auth, auth_mode = load_auth(auth_path)
    state = load_local_config_state(home)
    if not local_config_state_active(state):
        state = capture_initial_state(
            config_path=config_path,
            auth_path=auth_path,
            old_config=old_config,
            old_auth=old_auth,
            provider=provider,
        )
    else:
        ensure_provider_snapshot(state, old_config, provider)
        state["target_base_url"] = api_base
        state["target_model_provider"] = provider

    new_config = set_top_level_key(old_config, "model_provider", provider)
    new_config = set_table_values(
        new_config,
        f"model_providers.{provider}",
        {
            "name": "OpenAI",
            "base_url": api_base,
            "wire_api": "responses",
            "requires_openai_auth": True,
        },
    )
    new_auth_object = dict(old_auth)
    new_auth_object["OPENAI_API_KEY"] = api_key
    new_auth = json.dumps(new_auth_object, indent=2, ensure_ascii=False) + "\n"

    write_local_config_state(home, state)
    write_atomic(config_path, new_config, config_mode)
    write_atomic(auth_path, new_auth, auth_mode)

    print(summary)
    print(f"model_provider: {provider}")
    print(f"base_url: {api_base}")
    print(f"config: {config_path}")
    print(f"auth: {auth_path}")
    print(f"saved pre-switch managed fields: {local_config_state_path(home)}")
    print("Restart Codex to apply.")


def apply_local() -> None:
    data = load_litellm_config()
    master_key = (
        data.get("general_settings", {}).get("master_key")
        if isinstance(data.get("general_settings"), dict)
        else None
    )
    api_key = str(master_key or "sk-local-litellm")
    update_codex_files(
        api_base=LOCAL_BASE_URL,
        api_key=api_key,
        summary="Codex config updated to use the local LiteLLM service.",
    )


def reapply_pre_switch() -> None:
    home = codex_home()
    state = load_local_config_state(home)
    if not local_config_state_active(state):
        raise FileNotFoundError("No active pre-switch Codex config state found. Nothing to reapply.")

    config_path = home / "config.toml"
    auth_path = home / "auth.json"
    config_mode = (config_path.stat().st_mode & 0o777) if config_path.exists() else 0o600
    auth, auth_mode = load_auth(auth_path)
    config = config_path.read_text(encoding="utf-8") if config_path.exists() else ""

    config_state = state["config"]
    top_level = config_state.get("top_level", {})
    config = restore_top_level_field(config, "model_provider", top_level["model_provider"])
    for provider, provider_state in config_state.get("providers", {}).items():
        table = f"model_providers.{provider}"
        for key, snapshot in provider_state.get("fields", {}).items():
            config = restore_table_field(config, table, key, snapshot)
        if provider_state.get("table_present") is not True:
            config = remove_empty_table(config, table)

    auth_state = state["auth"]
    key_state = auth_state["OPENAI_API_KEY"]
    if key_state.get("present") is True:
        auth["OPENAI_API_KEY"] = key_state.get("value")
    else:
        auth.pop("OPENAI_API_KEY", None)

    if config_state.get("file_present") is not True and not config.strip():
        try:
            config_path.unlink()
        except FileNotFoundError:
            pass
    else:
        write_atomic(config_path, config, config_mode)
    if auth_state.get("file_present") is not True and not auth:
        try:
            auth_path.unlink()
        except FileNotFoundError:
            pass
    else:
        write_atomic(auth_path, json.dumps(auth, indent=2, ensure_ascii=False) + "\n", auth_mode)
    clear_local_config_state(home)
    print("Codex pre-switch managed fields reapplied.")
    print(f"config: {config_path}")
    print(f"auth: {auth_path}")
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
