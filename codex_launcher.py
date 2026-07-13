#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

try:
    import yaml
except Exception as exc:  # pragma: no cover - surfaced in CLI output
    print(f"PyYAML is required: {exc}", file=sys.stderr)
    raise SystemExit(1)


DEFAULT_LOCAL_PORT = "4000"
DEFAULT_LOCAL_API_KEY = "sk-local-litellm"
LOCAL_HOST = "127.0.0.1"
PROVIDER_ID = "litellm_menu_local"


def usage() -> int:
    print("usage: codex_launcher.py {exec [codex arguments...]|auth-token}", file=sys.stderr)
    return 64


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


def local_port() -> str:
    value = os.environ.get("LITELLM_PORT", "").strip() or DEFAULT_LOCAL_PORT
    if not value.isdigit():
        return DEFAULT_LOCAL_PORT
    port = int(value)
    if port < 1 or port > 65535:
        return DEFAULT_LOCAL_PORT
    return str(port)


def local_base_url() -> str:
    return f"http://{LOCAL_HOST}:{local_port()}/v1"


def load_local_api_key() -> str:
    config_path = default_config_yaml()
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError("config.yaml must be a YAML mapping")

    general_settings = data.get("general_settings")
    master_key = general_settings.get("master_key") if isinstance(general_settings, dict) else None
    if isinstance(master_key, str) and master_key.startswith("os.environ/"):
        env_name = master_key.removeprefix("os.environ/").strip()
        if not env_name:
            raise ValueError("general_settings.master_key has an empty environment variable name")
        master_key = os.environ.get(env_name)
        if master_key is None or not master_key.strip():
            raise ValueError("the environment-backed LiteLLM master key is unavailable")

    value = str(master_key or os.environ.get("LITELLM_MASTER_KEY") or DEFAULT_LOCAL_API_KEY).strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("the LiteLLM master key is empty or malformed")
    return value


def codex_binary() -> str:
    override = os.environ.get("CODEX_BIN", "").strip()
    if override:
        resolved = shutil.which(override) if "/" not in override else override
        if resolved and os.access(resolved, os.X_OK):
            return str(Path(resolved).expanduser())
        raise FileNotFoundError("CODEX_BIN does not point to an executable")

    resolved = shutil.which("codex")
    if resolved:
        return resolved

    app_binary = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    if app_binary.is_file() and os.access(app_binary, os.X_OK):
        return str(app_binary)
    raise FileNotFoundError("Codex CLI was not found; install Codex or set CODEX_BIN")


def toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


def config_override(key: str, value: object) -> list[str]:
    return ["-c", f"{key}={toml_value(value)}"]


def isolated_codex_arguments(arguments: list[str]) -> list[str]:
    script_path = str(Path(__file__).resolve())
    auth_args = [script_path, "auth-token"]
    overrides: list[str] = []
    for key, value in (
        ("model_provider", PROVIDER_ID),
        (f"model_providers.{PROVIDER_ID}.name", "OpenAI"),
        (f"model_providers.{PROVIDER_ID}.base_url", local_base_url()),
        (f"model_providers.{PROVIDER_ID}.wire_api", "responses"),
        (f"model_providers.{PROVIDER_ID}.auth.command", sys.executable),
        (f"model_providers.{PROVIDER_ID}.auth.args", auth_args),
        (f"model_providers.{PROVIDER_ID}.auth.timeout_ms", 5000),
        (f"model_providers.{PROVIDER_ID}.auth.refresh_interval_ms", 300000),
    ):
        overrides.extend(config_override(key, value))
    return [codex_binary(), *overrides, *arguments]


def launch_codex(arguments: list[str]) -> None:
    argv = isolated_codex_arguments(arguments)
    os.execvpe(argv[0], argv, os.environ.copy())


def print_auth_token() -> None:
    sys.stdout.write(load_local_api_key())


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return usage()
    command = argv[1]
    try:
        if command == "exec":
            launch_codex(argv[2:])
            return 0
        if command == "auth-token" and len(argv) == 2:
            print_auth_token()
            return 0
    except Exception as exc:
        print(f"Isolated Codex launch failed: {exc}", file=sys.stderr)
        return 1
    return usage()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
