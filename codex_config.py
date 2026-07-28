#!/usr/bin/env python3
"""Read, validate, and atomically edit Codex's user configuration.

The editor protocol deliberately keeps the source text as the source of truth:
the native UI may render supported settings on the left, but the editable TOML
and JSON on the right always round-trip.  ``sync`` never writes to CODEX_HOME;
``apply-editor`` is the only command that changes either file.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import re
import stat
import sys
import tempfile
import tomllib
from typing import Any

import yaml


DEFAULT_PORT = "4000"
DEFAULT_KEY = "sk-local-litellm"

# These are the simple, stable fields the native editor renders directly.  Raw
# TOML remains available for every other Codex setting and is never discarded.
CORE_SCALAR_KEYS = (
    "model",
    "review_model",
    "model_provider",
    "openai_base_url",
    "cli_auth_credentials_store",
    "forced_login_method",
    "profile",
    "model_reasoning_effort",
    "plan_mode_reasoning_effort",
    "model_reasoning_summary",
    "model_verbosity",
    "service_tier",
    "personality",
    "oss_provider",
    "web_search",
    "model_context_window",
    "model_auto_compact_token_limit",
    "model_auto_compact_token_limit_scope",
    "tool_output_token_limit",
    "file_opener",
    "include_apps_instructions",
    "include_collaboration_mode_instructions",
    "include_environment_context",
    "include_permissions_instructions",
)

# Current Codex exposes more flags than fit comfortably in the first version
# of the UI.  This curated list covers the user-facing switches while still
# surfacing already-present unknown boolean flags below it.
SUPPORTED_FEATURE_KEYS = (
    "fast_mode",
    "goals",
    "apps",
    "plugins",
    "plugin_sharing",
    "hooks",
    "collab",
    "collaboration_modes",
    "computer_use",
    "browser_use",
    "in_app_browser",
    "image_generation",
    "multi_agent",
    "multi_agent_mode",
    "connectors",
    "memories",
    "request_permissions",
    "web_search",
    "network_proxy",
    "prevent_idle_sleep",
    "remote_models",
        "remote_plugin",
        "code_mode",
        "js_repl",
        "experimental_use_unified_exec_tool",
        "shell_snapshot",
    "shell_tool",
    "skill_mcp_dependency_install",
    "personality",
)

# The UI label predates the configuration schema's deliberately verbose name.
# Keep its short UI key at the protocol boundary and write the canonical TOML
# feature flag underneath.
FEATURE_KEY_ALIASES = {"unified_exec": "experimental_use_unified_exec_tool"}

# Codex's built-in provider IDs cannot be overridden through
# ``model_providers``.  The three locally selectable built-ins are called out
# explicitly in the configuration schema/product UI; rejecting only these
# avoids guessing at an ever-changing complete built-in catalog.
RESERVED_PROVIDER_IDS = frozenset({"openai", "ollama", "lmstudio"})


class EditorError(ValueError):
    """An error safe to show in the native editor."""


def default_config_path() -> pathlib.Path:
    configured = os.environ.get("LITELLM_CONFIG_FILE", "").strip()
    if configured:
        return pathlib.Path(configured).expanduser()
    root = os.environ.get("LITELLM_RUNTIME_ROOT", "").strip() or os.environ.get(
        "LITELLM_MENU_HOME", ""
    ).strip()
    return (
        pathlib.Path(root).expanduser() / "config.yaml"
        if root
        else pathlib.Path.home() / ".litellm-menu" / "config.yaml"
    )


def codex_home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()


def local_port() -> str:
    value = os.environ.get("LITELLM_PORT", DEFAULT_PORT).strip()
    return value if value.isdigit() and 0 < int(value) < 65536 else DEFAULT_PORT


def local_base_url() -> str:
    return f"http://127.0.0.1:{local_port()}/v1"


def load_yaml(path: pathlib.Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError:
        raise EditorError("Could not read the LiteLLM config") from None
    except yaml.YAMLError:
        # PyYAML exceptions often include the offending source line.  That
        # source can contain a key, so never send the parser's text onward.
        raise EditorError("LiteLLM config is not valid YAML") from None
    if not isinstance(loaded, dict):
        raise EditorError("LiteLLM config must be a YAML mapping")
    return loaded


def local_api_key(config: dict[str, Any]) -> str:
    settings = config.get("general_settings")
    value = settings.get("master_key") if isinstance(settings, dict) else None
    if isinstance(value, str) and value.startswith("os.environ/"):
        variable = value.removeprefix("os.environ/").strip()
        value = os.environ.get(variable) if variable else None
    value = str(value or os.environ.get("LITELLM_MASTER_KEY") or DEFAULT_KEY).strip()
    if not value or "\n" in value or "\r" in value:
        raise EditorError("LiteLLM master key is unavailable or malformed")
    return value


def _safe_file_state(path: pathlib.Path, label: str) -> tuple[bool, int]:
    """Return whether a regular file exists, rejecting symbolic-link targets."""

    try:
        details = path.lstat()
    except FileNotFoundError:
        return False, 0o600
    except OSError:
        raise EditorError(f"Could not inspect {label}") from None
    if stat.S_ISLNK(details.st_mode):
        raise EditorError(f"{label} must not be a symbolic link")
    if not stat.S_ISREG(details.st_mode):
        raise EditorError(f"{label} must be a regular file")
    return True, stat.S_IMODE(details.st_mode)


def _safe_read_text(path: pathlib.Path, label: str) -> tuple[str, bool, int]:
    exists, mode = _safe_file_state(path, label)
    if not exists:
        return "", False, mode
    try:
        return path.read_text(encoding="utf-8"), True, mode
    except (OSError, UnicodeError):
        raise EditorError(f"Could not read {label}") from None


def _toml_error(error: tomllib.TOMLDecodeError) -> EditorError:
    # tomllib's diagnostic is safe in current CPython, but avoid relying on it
    # because several parser implementations include source excerpts.
    match = re.search(r"line\s+(\d+)(?:,\s*column\s+(\d+))?", str(error), re.I)
    if match:
        where = f" (line {match.group(1)}"
        if match.group(2):
            where += f", column {match.group(2)}"
        where += ")"
    else:
        where = ""
    return EditorError(f"config.toml is not valid TOML{where}")


def parse_toml_text(text: str) -> dict[str, Any]:
    if not isinstance(text, str):
        raise EditorError("config_text must be a string")
    try:
        loaded = tomllib.loads(text) if text.strip() else {}
    except tomllib.TOMLDecodeError as exc:
        raise _toml_error(exc) from None
    if not isinstance(loaded, dict):  # Defensive: tomllib always returns dict.
        raise EditorError("config.toml must be a TOML mapping")
    return loaded


def _json_error(error: json.JSONDecodeError) -> EditorError:
    return EditorError(f"auth.json is not valid JSON (line {error.lineno}, column {error.colno})")


def parse_auth_text(text: str) -> dict[str, Any]:
    if not isinstance(text, str):
        raise EditorError("auth_text must be a string")
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _json_error(exc) from None
    if not isinstance(loaded, dict):
        raise EditorError("auth.json must be a JSON object")
    return loaded


def _toml_key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else json.dumps(value, ensure_ascii=False)


def toml_value(value: object) -> str:
    """Render the TOML values the structured editor may safely set."""

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EditorError("The editor does not support non-finite TOML numbers")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        pairs: list[str] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise EditorError("TOML object keys must be strings")
            pairs.append(f"{_toml_key(key)} = {toml_value(item)}")
        return "{ " + ", ".join(pairs) + " }"
    raise EditorError("The editor received an unsupported TOML value")


def _toml_statement_spans(text: str) -> list[tuple[int, int]]:
    """Return lexical TOML statement spans without interpreting their values."""

    spans: list[tuple[int, int]] = []
    statement_start: int | None = None
    square_depth = 0
    curly_depth = 0
    string_kind = ""
    escaped = False
    comment = False
    index = 0

    while index < len(text):
        character = text[index]
        if comment:
            if character in "\r\n":
                comment = False
            else:
                index += 1
                continue

        if string_kind:
            if string_kind == '"':
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    string_kind = ""
            elif string_kind == "'":
                if character == "'":
                    string_kind = ""
            elif string_kind == '\"\"\"':
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif text.startswith('\"\"\"', index):
                    string_kind = ""
                    index += 2
            elif string_kind == "'''" and text.startswith("'''", index):
                string_kind = ""
                index += 2
            index += 1
            continue

        if character == "#":
            comment = True
            index += 1
            continue
        if character in " \t\r\n" and statement_start is None:
            index += 1
            continue
        if statement_start is None:
            statement_start = index

        if text.startswith('\"\"\"', index):
            string_kind = '\"\"\"'
            index += 3
            continue
        if text.startswith("'''", index):
            string_kind = "'''"
            index += 3
            continue
        if character in {'"', "'"}:
            string_kind = character
        elif character == "[":
            square_depth += 1
        elif character == "]":
            square_depth = max(0, square_depth - 1)
        elif character == "{":
            curly_depth += 1
        elif character == "}":
            curly_depth = max(0, curly_depth - 1)
        elif character in "\r\n" and square_depth == 0 and curly_depth == 0:
            end = index + 1
            if character == "\r" and end < len(text) and text[end] == "\n":
                end += 1
                index += 1
            spans.append((statement_start, end))
            statement_start = None
        index += 1

    if statement_start is not None:
        spans.append((statement_start, len(text)))
    return spans


def _simple_assignment_key(statement: str) -> str | None:
    match = re.match(
        r'''\s*((?:[A-Za-z0-9_-]+)|(?:"(?:\\.|[^"\\])*")|(?:'[^']*'))\s*=''',
        statement,
    )
    if match is None:
        return None
    encoded_key = match.group(1)
    try:
        parsed = tomllib.loads(f"{encoded_key} = 0")
    except tomllib.TOMLDecodeError:
        return None
    if len(parsed) != 1:
        return None
    key, parsed_value = next(iter(parsed.items()))
    return key if parsed_value == 0 and isinstance(key, str) else None


def _header_path(statement: str) -> tuple[str, ...] | None:
    """Decode a regular table header into a tuple without hand-parsing keys."""

    source = statement.lstrip()
    if not source.startswith("[") or source.startswith("[["):
        return None
    in_string = ""
    escaped = False
    end = None
    for index, character in enumerate(source[1:], 1):
        if in_string:
            if in_string == '"':
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = ""
            elif character == "'":
                in_string = ""
            continue
        if character in {'"', "'"}:
            in_string = character
        elif character == "]":
            end = index
            break
    if end is None:
        return None
    try:
        parsed = tomllib.loads(source[: end + 1] + "\n__codex_editor_probe__ = 0\n")
    except tomllib.TOMLDecodeError:
        return None

    def find_probe(value: object, path: tuple[str, ...] = ()) -> tuple[str, ...] | None:
        if not isinstance(value, dict):
            return None
        if value.get("__codex_editor_probe__") == 0:
            return path
        for key, child in value.items():
            if isinstance(key, str):
                found = find_probe(child, path + (key,))
                if found is not None:
                    return found
        return None

    return find_probe(parsed)


def _is_any_table_header(statement: str) -> bool:
    return statement.lstrip().startswith("[")


def _table_sections(text: str) -> list[tuple[tuple[str, ...], int, int, int]]:
    """Return (path, header_start, body_start, body_end) table sections."""

    spans = _toml_statement_spans(text)
    headers: list[tuple[tuple[str, ...] | None, int, int]] = []
    for start, end in spans:
        statement = text[start:end]
        if _is_any_table_header(statement):
            headers.append((_header_path(statement), start, end))
    sections: list[tuple[tuple[str, ...], int, int, int]] = []
    for index, (path, start, end) in enumerate(headers):
        if path is None:
            continue
        # A child table begins a new TOML table too: parent assignments must
        # be inserted before it, not appended into the child's scope.
        body_end = headers[index + 1][1] if index + 1 < len(headers) else len(text)
        sections.append((path, start, end, body_end))
    return sections


def _find_table_section(
    text: str, path: tuple[str, ...]
) -> tuple[tuple[str, ...], int, int, int] | None:
    return next((section for section in _table_sections(text) if section[0] == path), None)


def _replace_ranges(text: str, replacements: list[tuple[int, int, str]]) -> str:
    result = text
    for start, end, replacement in sorted(replacements, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def _newline_for(statement: str) -> str:
    return "\r\n" if statement.endswith("\r\n") else "\n"


def _first_table_start(text: str) -> int:
    for start, end in _toml_statement_spans(text):
        if _is_any_table_header(text[start:end]):
            return start
    return len(text)


def set_top_level_value(text: str, key: str, value: object) -> str:
    limit = _first_table_start(text)
    for start, end in _toml_statement_spans(text):
        if start >= limit:
            break
        statement = text[start:end]
        if _simple_assignment_key(statement) == key:
            return _replace_ranges(
                text,
                [(start, end, f"{_toml_key(key)} = {toml_value(value)}{_newline_for(statement)}")],
            )

    insertion = f"{_toml_key(key)} = {toml_value(value)}\n"
    if limit < len(text):
        if limit > 0 and not text[:limit].endswith(("\n", "\r")):
            insertion = "\n" + insertion
        insertion += "\n"
    elif text and not text.endswith(("\n", "\r")):
        insertion = "\n" + insertion
    return text[:limit] + insertion + text[limit:]


def remove_top_level_value(text: str, key: str) -> str:
    limit = _first_table_start(text)
    replacements: list[tuple[int, int, str]] = []
    for start, end in _toml_statement_spans(text):
        if start >= limit:
            break
        if _simple_assignment_key(text[start:end]) == key:
            replacements.append((start, end, ""))
    return _replace_ranges(text, replacements)


def _table_header(path: tuple[str, ...]) -> str:
    return "[" + ".".join(_toml_key(part) for part in path) + "]"


def _top_level_assignment_exists(text: str, key: str) -> bool:
    limit = _first_table_start(text)
    return any(
        start < limit and _simple_assignment_key(text[start:end]) == key
        for start, end in _toml_statement_spans(text)
    )


def set_table_value(text: str, path: tuple[str, ...], key: str, value: object) -> str:
    section = _find_table_section(text, path)
    if section is None:
        # A parent represented as an inline table cannot safely grow a child
        # table while preserving the user's source text.
        if path and _top_level_assignment_exists(text, path[0]):
            raise EditorError(
                f"Cannot edit {'.'.join(path)} while its parent uses inline TOML; edit the raw TOML instead"
            )
        prefix = ""
        if text and not text.endswith(("\n", "\r")):
            prefix = "\n"
        if text.strip():
            prefix += "\n"
        return text + prefix + f"{_table_header(path)}\n{_toml_key(key)} = {toml_value(value)}\n"

    _, _, body_start, body_end = section
    for start, end in _toml_statement_spans(text):
        if start < body_start or start >= body_end:
            continue
        statement = text[start:end]
        if _simple_assignment_key(statement) == key:
            return _replace_ranges(
                text,
                [(start, end, f"{_toml_key(key)} = {toml_value(value)}{_newline_for(statement)}")],
            )

    prefix = "" if body_end == 0 or text[:body_end].endswith(("\n", "\r")) else "\n"
    return text[:body_end] + prefix + f"{_toml_key(key)} = {toml_value(value)}\n" + text[body_end:]


def remove_table_value(text: str, path: tuple[str, ...], key: str) -> str:
    section = _find_table_section(text, path)
    if section is None:
        return text
    _, _, body_start, body_end = section
    replacements: list[tuple[int, int, str]] = []
    for start, end in _toml_statement_spans(text):
        if body_start <= start < body_end and _simple_assignment_key(text[start:end]) == key:
            replacements.append((start, end, ""))
    return _replace_ranges(text, replacements)


def remove_table(text: str, path: tuple[str, ...]) -> str:
    """Remove a managed table and its nested managed tables, if present."""

    spans = _toml_statement_spans(text)
    headers: list[tuple[tuple[str, ...] | None, int, int]] = []
    for start, end in spans:
        if _is_any_table_header(text[start:end]):
            headers.append((_header_path(text[start:end]), start, end))
    matching = [
        index for index, (header_path, _, _) in enumerate(headers) if header_path == path
    ]
    if not matching:
        return text
    # Duplicate TOML tables are invalid, but source recovery should still not
    # delete an unrelated sibling after the first matching range.
    start_index = matching[0]
    start = headers[start_index][1]
    end = len(text)
    for header_path, header_start, _ in headers[start_index + 1 :]:
        if header_path is None or header_path[: len(path)] != path:
            end = header_start
            break
    return text[:start] + text[end:]


def set_top_levels(text: str, values: dict[str, object]) -> str:
    """Compatibility helper retained for the legacy selected-model command."""

    parse_toml_text(text)
    result = text
    for key, value in values.items():
        result = set_top_level_value(result, key, value)
    parsed = parse_toml_text(result)
    for key, value in values.items():
        if parsed.get(key) != value:
            raise EditorError(f"Codex config update could not set {key}")
    return result if not result or result.endswith("\n") else result + "\n"


def _get_mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _get_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _field_or_none(mapping: dict[str, Any], key: str) -> Any:
    value = mapping.get(key)
    return value if isinstance(value, (str, int, float, bool)) or value is None else None


def _structured_scalar(mapping: dict[str, Any], key: str) -> Any:
    value = _field_or_none(mapping, key)
    # The native text fields decode numbers as strings; retaining that shape
    # lets a raw TOML edit such as ``tool_output_token_limit = 2048`` populate
    # the corresponding control without a Codable type mismatch.
    if key in {
        "model_context_window",
        "model_auto_compact_token_limit",
        "tool_output_token_limit",
    } and isinstance(value, int):
        return str(value)
    return value


def configured_models(config: dict[str, Any]) -> list[dict[str, str]]:
    providers = _get_mapping(config.get("providers"))
    result: list[dict[str, str]] = []
    model_list = config.get("model_list")
    if not isinstance(model_list, list):
        return result
    for entry in model_list:
        if not isinstance(entry, dict):
            continue
        params = _get_mapping(entry.get("litellm_params"))
        info = _get_mapping(entry.get("model_info"))
        provider_name = str(info.get("provider") or "").strip()
        provider = _get_mapping(providers.get(provider_name))
        model = str(entry.get("model_name") or "").strip()
        if not model:
            continue
        result.append(
            {
                "model": model,
                "provider": provider_name,
                "deployment_id": str(info.get("id") or "").strip(),
                "upstream_model": str(params.get("model") or "").strip(),
                "api_base": str(params.get("api_base") or provider.get("api_base") or "").strip(),
            }
        )
    return result


def read_auth(path: pathlib.Path) -> tuple[dict[str, Any], int]:
    text, exists, mode = _safe_read_text(path, "Codex auth file")
    if not exists:
        return {}, 0o600
    return parse_auth_text(text), mode


def _permission_mode(config: dict[str, Any]) -> str:
    uses_legacy = "sandbox_mode" in config or "sandbox_workspace_write" in config
    uses_profiles = "default_permissions" in config
    if uses_legacy and uses_profiles:
        return "mixed"
    if uses_profiles:
        return "profiles"
    if uses_legacy:
        return "legacy"
    return "unset"


def semantic_validation(config: dict[str, Any], auth: dict[str, Any]) -> list[str]:
    """Return user-safe errors for combinations TOML syntax cannot express."""

    errors: list[str] = []
    if "default_permissions" in config and (
        "sandbox_mode" in config or "sandbox_workspace_write" in config
    ):
        errors.append(
            "Permissions conflict: this config contains both Legacy sandbox and Permission profile. Select one in Settings; Apply removes the other."
        )

    sandbox_mode = config.get("sandbox_mode")
    if sandbox_mode is not None and sandbox_mode not in {
        "read-only",
        "workspace-write",
        "danger-full-access",
    }:
        errors.append("sandbox_mode must be read-only, workspace-write, or danger-full-access")
    if "sandbox_workspace_write" in config and not isinstance(
        config.get("sandbox_workspace_write"), dict
    ):
        errors.append("sandbox_workspace_write must be a TOML table or inline object")

    features = config.get("features")
    if features is not None and not isinstance(features, dict):
        errors.append("features must be a TOML table")
    elif isinstance(features, dict):
        for key in SUPPORTED_FEATURE_KEYS:
            if key in features and not isinstance(features[key], bool):
                errors.append(f"features.{key} must be true or false")

    providers = config.get("model_providers")
    if providers is not None and not isinstance(providers, dict):
        errors.append("model_providers must be a TOML table")
    elif isinstance(providers, dict):
        for provider_id, provider in providers.items():
            label = str(provider_id)
            if label.lower() in RESERVED_PROVIDER_IDS:
                errors.append("A custom provider uses a reserved built-in provider id")
            if not isinstance(provider, dict):
                errors.append("Each custom provider must be a TOML table")
                continue
            wire_api = provider.get("wire_api")
            if wire_api is not None and wire_api != "responses":
                errors.append("A custom provider wire_api must be responses")
            has_command_auth = "auth" in provider
            has_env_key = bool(provider.get("env_key"))
            has_bearer = bool(provider.get("experimental_bearer_token"))
            requires_openai_auth = provider.get("requires_openai_auth") is True
            if has_command_auth and (has_env_key or has_bearer or requires_openai_auth):
                errors.append(
                    "A custom provider command auth cannot be combined with env_key, bearer token, or requires_openai_auth"
                )
            if requires_openai_auth and (has_env_key or has_bearer):
                errors.append(
                    "A custom provider requires_openai_auth cannot be combined with env_key or bearer token"
                )
            if has_env_key and has_bearer:
                errors.append(
                    "A custom provider cannot define both env_key and a bearer token"
                )

    servers = config.get("mcp_servers")
    if servers is not None and not isinstance(servers, dict):
        errors.append("mcp_servers must be a TOML table")
    elif isinstance(servers, dict):
        for _, server in servers.items():
            if not isinstance(server, dict):
                errors.append("Each MCP server must be a TOML table")
                continue
            if server.get("command") and server.get("url"):
                errors.append("An MCP server cannot define both command and url")

    if not isinstance(auth, dict):  # parse_auth_text makes this unreachable.
        errors.append("auth.json must be a JSON object")
    return errors


def _provider_auth_mode(provider: dict[str, Any]) -> str:
    if "auth" in provider:
        return "command"
    if provider.get("requires_openai_auth") is True:
        return "openai_auth"
    if provider.get("experimental_bearer_token"):
        return "bearer"
    if provider.get("env_key"):
        return "env_key"
    return "none"


def structured_config(config: dict[str, Any], auth: dict[str, Any]) -> dict[str, Any]:
    structured: dict[str, Any] = {key: _structured_scalar(config, key) for key in CORE_SCALAR_KEYS}
    # ``api_url`` is a UI-friendly alias for Codex's official
    # ``openai_base_url`` key.  Both are returned so clients can migrate
    # without guessing field names.
    structured["api_url"] = structured["openai_base_url"]
    structured["api_key"] = auth.get("OPENAI_API_KEY") if isinstance(auth.get("OPENAI_API_KEY"), str) else ""

    raw_features = _get_mapping(config.get("features"))
    feature_keys = list(SUPPORTED_FEATURE_KEYS)
    feature_keys.extend(FEATURE_KEY_ALIASES)
    feature_keys.extend(
        key for key in raw_features if isinstance(key, str) and key not in feature_keys
    )
    structured["features"] = {
        key: raw_features.get(FEATURE_KEY_ALIASES.get(key, key))
        if isinstance(raw_features.get(FEATURE_KEY_ALIASES.get(key, key)), bool)
        else None
        for key in feature_keys
    }
    structured["supported_features"] = list(SUPPORTED_FEATURE_KEYS) + list(FEATURE_KEY_ALIASES)

    sandbox = _get_mapping(config.get("sandbox_workspace_write"))
    structured["permissions"] = {
        # Swift's segmented control uses singular ``profile``; keep the
        # internal plural spelling only as an accepted patch alias.
        "mode": "profile" if _permission_mode(config) == "profiles" else _permission_mode(config),
        "sandbox_mode": _get_text(config.get("sandbox_mode")),
        "approval_policy": config.get("approval_policy"),
        "approvals_reviewer": _get_text(config.get("approvals_reviewer")),
        "default_permissions": _get_text(config.get("default_permissions")),
        "network_access": sandbox.get("network_access") if isinstance(sandbox.get("network_access"), bool) else None,
        "writable_roots": list(sandbox.get("writable_roots"))
        if isinstance(sandbox.get("writable_roots"), list)
        and all(isinstance(item, str) for item in sandbox.get("writable_roots"))
        else None,
    }
    permission_profiles = _get_mapping(config.get("permissions"))
    structured["permission_profiles"] = sorted(
        str(profile_id) for profile_id in permission_profiles
    )

    providers: list[dict[str, Any]] = []
    for provider_id, provider in sorted(_get_mapping(config.get("model_providers")).items()):
        if not isinstance(provider, dict):
            continue
        providers.append(
            {
                "id": str(provider_id),
                "name": _get_text(provider.get("name")) or "",
                "base_url": _get_text(provider.get("base_url")) or "",
                "wire_api": _get_text(provider.get("wire_api")) or "responses",
                "env_key": _get_text(provider.get("env_key")) or "",
                "requires_openai_auth": provider.get("requires_openai_auth")
                if isinstance(provider.get("requires_openai_auth"), bool)
                else False,
                "auth_mode": _provider_auth_mode(provider),
                # ``null`` distinguishes no command field from an explicitly
                # configured empty string.  It lets a structured list update
                # retain raw-only bearer credentials instead of accidentally
                # interpreting their hidden command field as a request to
                # clear authentication.
                "auth_command": _get_text(_get_mapping(provider.get("auth")).get("command")),
            }
        )
    structured["providers"] = providers

    mcp_servers: list[dict[str, Any]] = []
    for server_id, server in sorted(_get_mapping(config.get("mcp_servers")).items()):
        if not isinstance(server, dict):
            continue
        if server.get("command") and server.get("url"):
            transport = "invalid"
        elif server.get("command"):
            transport = "stdio"
        elif server.get("url"):
            transport = "http"
        else:
            transport = "unknown"
        mcp_servers.append(
            {
                "id": str(server_id),
                "enabled": server.get("enabled") if isinstance(server.get("enabled"), bool) else None,
                "required": server.get("required") if isinstance(server.get("required"), bool) else None,
                "transport": transport,
                "command": _get_text(server.get("command")) or "",
                "url": _get_text(server.get("url")) or "",
            }
        )
    structured["mcp_servers"] = mcp_servers

    def enabled_entries(table_name: str) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for entry_id, entry in sorted(_get_mapping(config.get(table_name)).items()):
            if not isinstance(entry, dict):
                continue
            entries.append(
                {
                    "id": str(entry_id),
                    "enabled": entry.get("enabled") if isinstance(entry.get("enabled"), bool) else None,
                }
            )
        return entries

    structured["plugins"] = enabled_entries("plugins")
    structured["apps"] = enabled_entries("apps")
    structured["agents"] = _get_mapping(config.get("agents"))
    structured["skills"] = sorted(str(key) for key in _get_mapping(config.get("skills")))
    structured["integrations"] = {
        "mcp_servers": structured["mcp_servers"],
        "plugins": structured["plugins"],
        "apps": structured["apps"],
    }
    shell_policy = _get_mapping(config.get("shell_environment_policy"))
    history = _get_mapping(config.get("history"))
    agents = _get_mapping(config.get("agents"))
    structured["advanced"] = {
        "shell_environment_inherit": _get_text(shell_policy.get("inherit")),
        "history_persistence": _get_text(history.get("persistence")),
        "agents_max_threads": str(agents["max_threads"])
        if isinstance(agents.get("max_threads"), int)
        else None,
        "agents_max_depth": str(agents["max_depth"])
        if isinstance(agents.get("max_depth"), int)
        else None,
        "file_opener": _get_text(config.get("file_opener")),
        "mcp_oauth_credentials_store": _get_text(config.get("mcp_oauth_credentials_store")),
    }
    return structured


def _runtime_context(config_path: pathlib.Path) -> tuple[list[dict[str, str]], str, list[str]]:
    warnings: list[str] = []
    try:
        runtime_config = load_yaml(config_path)
        models = configured_models(runtime_config)
        key = local_api_key(runtime_config)
    except EditorError as exc:
        # The Codex editor must remain usable to repair an existing Codex file
        # even if the LiteLLM source is temporarily invalid/unavailable.
        models = []
        key = ""
        warnings.append(str(exc))
    return models, key, warnings


def editor_payload(
    config: dict[str, Any],
    auth: dict[str, Any],
    *,
    config_text: str | None = None,
    auth_text: str | None = None,
    config_exists: bool | None = None,
    auth_exists: bool | None = None,
    runtime_config_path: pathlib.Path | None = None,
    validation_errors: list[str] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    models, local_key, runtime_warnings = _runtime_context(
        runtime_config_path or default_config_path()
    )
    all_warnings = list(warnings or []) + runtime_warnings
    errors = list(validation_errors or [])
    result: dict[str, Any] = {
        "structured": structured_config(config, auth),
        "models": models,
        "local_base_url": local_base_url(),
        "local_api_key": local_key,
        "validation_errors": errors,
        "validation_error": errors[0] if errors else None,
        "warnings": all_warnings,
    }
    if config_text is not None:
        result["config_text"] = config_text
    if auth_text is not None:
        result["auth_text"] = auth_text
    if config_exists is not None:
        result["config_exists"] = config_exists
    if auth_exists is not None:
        result["auth_exists"] = auth_exists
    return result


def load_editor(runtime_config_path: pathlib.Path | None = None) -> dict[str, Any]:
    home = codex_home()
    config_path = home / "config.toml"
    auth_path = home / "auth.json"
    config_text, config_exists, _ = _safe_read_text(config_path, "Codex config file")
    auth_text, auth_exists, _ = _safe_read_text(auth_path, "Codex auth file")
    # A missing auth file is presented as an editable empty object rather than
    # an empty string, because auth_text must always be valid JSON on Apply.
    visible_auth_text = auth_text if auth_exists else "{}\n"
    errors: list[str] = []
    try:
        config = parse_toml_text(config_text)
    except EditorError as exc:
        config = {}
        errors.append(str(exc))
    try:
        auth = parse_auth_text(visible_auth_text)
    except EditorError as exc:
        auth = {}
        errors.append(str(exc))
    if not errors:
        errors.extend(semantic_validation(config, auth))
    return editor_payload(
        config,
        auth,
        config_text=config_text,
        auth_text=visible_auth_text,
        config_exists=config_exists,
        auth_exists=auth_exists,
        runtime_config_path=runtime_config_path,
        validation_errors=errors,
    )


def _require_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise EditorError(f"{key} must be a string")
    return value


def _require_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EditorError(f"{label} must be an object")
    return value


def _require_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise EditorError(f"{label} must be an array")
    return value


def _patch_optional_string(text: str, path: tuple[str, ...] | None, key: str, value: object) -> str:
    if value is not None and not isinstance(value, str):
        raise EditorError(f"{key} must be a string or null")
    if isinstance(value, str) and ("\n" in value or "\r" in value):
        raise EditorError(f"{key} cannot contain a line break")
    if path is None:
        return remove_top_level_value(text, key) if value is None else set_top_level_value(text, key, value)
    return remove_table_value(text, path, key) if value is None else set_table_value(text, path, key, value)


def _patch_optional_bool(text: str, path: tuple[str, ...] | None, key: str, value: object) -> str:
    if value is not None and not isinstance(value, bool):
        raise EditorError(f"{key} must be true, false, or null")
    if path is None:
        return remove_top_level_value(text, key) if value is None else set_top_level_value(text, key, value)
    return remove_table_value(text, path, key) if value is None else set_table_value(text, path, key, value)


def _remove_provider_auth(text: str, path: tuple[str, ...]) -> str:
    return remove_table(text=remove_table_value(text, path, "auth"), path=path + ("auth",))


def _provider_mapping(text: str, provider_id: str) -> dict[str, Any]:
    return _get_mapping(_get_mapping(parse_toml_text(text).get("model_providers")).get(provider_id))


def _apply_provider_auth_mode(
    text: str, path: tuple[str, ...], provider_id: str, mode: object, item: dict[str, Any]
) -> str:
    if not isinstance(mode, str) or mode not in {
        "none",
        "env_key",
        "openai_auth",
        "command",
        "bearer",
    }:
        raise EditorError("provider auth_mode must be none, env_key, openai_auth, command, or bearer")
    if "auth_command" in item:
        auth_command = item["auth_command"]
        if auth_command is None:
            text = _remove_provider_auth(text, path)
        elif isinstance(auth_command, str):
            text = _apply_provider_auth_command(text, path, provider_id, auth_command)
        else:
            command_data = _require_mapping(auth_command, "provider auth_command")
            if not isinstance(command_data.get("command"), str) or not command_data["command"].strip():
                raise EditorError("provider auth_command.command must be a non-empty string")
            text = set_table_value(text, path, "auth", command_data)
    if mode == "none":
        text = _remove_provider_auth(text, path)
        text = remove_table_value(text, path, "env_key")
        text = remove_table_value(text, path, "experimental_bearer_token")
        return remove_table_value(text, path, "requires_openai_auth")
    if mode == "env_key":
        text = _remove_provider_auth(text, path)
        text = remove_table_value(text, path, "experimental_bearer_token")
        return remove_table_value(text, path, "requires_openai_auth")
    if mode == "openai_auth":
        text = _remove_provider_auth(text, path)
        text = remove_table_value(text, path, "env_key")
        text = remove_table_value(text, path, "experimental_bearer_token")
        return set_table_value(text, path, "requires_openai_auth", True)
    if mode == "command":
        text = remove_table_value(text, path, "env_key")
        text = remove_table_value(text, path, "experimental_bearer_token")
        text = remove_table_value(text, path, "requires_openai_auth")
        if "auth" not in _provider_mapping(text, provider_id):
            raise EditorError("provider command auth needs auth_command")
        return text
    # The bearer value intentionally stays raw-TOML-only.  The user can select
    # this existing mode in the UI, but a new secret is never echoed by the
    # structured response.
    text = _remove_provider_auth(text, path)
    text = remove_table_value(text, path, "env_key")
    text = remove_table_value(text, path, "requires_openai_auth")
    if not _provider_mapping(text, provider_id).get("experimental_bearer_token"):
        raise EditorError("provider bearer auth must be entered in raw TOML")
    return text


def _apply_provider_auth_command(
    text: str, path: tuple[str, ...], provider_id: str, value: object
) -> str:
    """Apply the UI's simple command string without exposing token values."""

    if value is None or value == "":
        return _remove_provider_auth(text, path)
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
        raise EditorError("provider auth_command must be a non-empty single-line string or null")
    text = _remove_provider_auth(text, path)
    text = remove_table_value(text, path, "env_key")
    text = remove_table_value(text, path, "experimental_bearer_token")
    text = remove_table_value(text, path, "requires_openai_auth")
    return set_table_value(text, path, "auth", {"command": value.strip()})


def _apply_provider_patch(text: str, value: object) -> str:
    for index, raw_item in enumerate(_require_list(value, "providers")):
        item = _require_mapping(raw_item, f"providers[{index}]")
        provider_id = item.get("id")
        if not isinstance(provider_id, str) or not provider_id.strip() or "\n" in provider_id or "\r" in provider_id:
            raise EditorError(f"providers[{index}].id must be a non-empty single-line string")
        provider_id = provider_id.strip()
        if provider_id.lower() in RESERVED_PROVIDER_IDS:
            raise EditorError(f"providers[{index}].id is a reserved built-in provider id")
        path = ("model_providers", provider_id)
        if item.get("delete") is True:
            text = remove_table(text, path)
            continue
        existing = _provider_mapping(text, provider_id)
        field_aliases = {"name": "name", "base_url": "base_url", "api_base": "base_url", "wire_api": "wire_api", "env_key": "env_key", "requires_openai_auth": "requires_openai_auth"}
        for source_key, target_key in field_aliases.items():
            if source_key not in item:
                continue
            patch_value = item[source_key]
            if target_key == "requires_openai_auth":
                text = _patch_optional_bool(text, path, target_key, patch_value)
            else:
                text = _patch_optional_string(text, path, target_key, patch_value)
        # A raw-only bearer provider has no visible command.  Its null is a
        # sentinel meaning “retain the raw token”, whereas a command/other
        # mode may intentionally clear a visible command field.
        retain_bearer = item.get("auth_mode") == "bearer" and item.get("auth_command") is None
        # The native UI uses a plain auth_command string rather than a nested
        # provider auth object.  It is mutually exclusive with the visible
        # env/OpenAI fields, so resolve it after those fields.
        if "auth_command" in item and not retain_bearer:
            text = _apply_provider_auth_command(text, path, provider_id, item["auth_command"])
        elif retain_bearer:
            # Bearer tokens are intentionally raw-TOML-only.  A round-tripped
            # row contains no token and must therefore leave the existing raw
            # value untouched when another visible provider field is edited.
            if not existing.get("experimental_bearer_token"):
                raise EditorError("provider bearer auth must be entered in raw TOML")
        # Apply the selector last.  A full UI row naturally contains the
        # currently rendered ``env_key``/``requires_openai_auth`` fields; the
        # selector must win so changing modes cannot accidentally recreate an
        # invalid combination after its cleanup pass.
        if "auth_mode" in item and not retain_bearer:
            mode_item = dict(item)
            # A string command has already been normalized above. Passing it
            # through the older nested-auth branch a second time rejects the
            # exact row returned by structured_config.
            if isinstance(mode_item.get("auth_command"), str):
                mode_item.pop("auth_command", None)
            text = _apply_provider_auth_mode(text, path, provider_id, item["auth_mode"], mode_item)
        elif "auth_command" not in item and "auth_command_detail" in item:
            text = _apply_provider_auth_mode(text, path, provider_id, "command", item)
    return text


def _mcp_mapping(text: str, server_id: str) -> dict[str, Any]:
    return _get_mapping(_get_mapping(parse_toml_text(text).get("mcp_servers")).get(server_id))


def _apply_mcp_patch(text: str, value: object) -> str:
    for index, raw_item in enumerate(_require_list(value, "mcp_servers")):
        item = _require_mapping(raw_item, f"mcp_servers[{index}]")
        server_id = item.get("id")
        if not isinstance(server_id, str) or not server_id.strip() or "\n" in server_id or "\r" in server_id:
            raise EditorError(f"mcp_servers[{index}].id must be a non-empty single-line string")
        server_id = server_id.strip()
        path = ("mcp_servers", server_id)
        if item.get("delete") is True:
            text = remove_table(text, path)
            continue
        for key in ("enabled", "required"):
            if key in item:
                text = _patch_optional_bool(text, path, key, item[key])
        for key in ("command", "url", "bearer_token_env_var"):
            if key in item:
                text = _patch_optional_string(text, path, key, item[key])
        if "args" in item:
            args = item["args"]
            if args is None:
                text = remove_table_value(text, path, "args")
            elif isinstance(args, list) and all(isinstance(argument, str) for argument in args):
                text = set_table_value(text, path, "args", args)
            else:
                raise EditorError("mcp server args must be an array of strings or null")
        if "transport" not in item:
            continue
        transport = item["transport"]
        if transport not in {"stdio", "http"}:
            raise EditorError("mcp server transport must be stdio or http")
        server = _mcp_mapping(text, server_id)
        if transport == "stdio":
            if not isinstance(server.get("command"), str) or not server["command"].strip():
                raise EditorError("stdio MCP server requires command")
            text = remove_table_value(text, path, "url")
        else:
            if not isinstance(server.get("url"), str) or not server["url"].strip():
                raise EditorError("HTTP MCP server requires url")
            text = remove_table_value(text, path, "command")
            text = remove_table_value(text, path, "args")
    return text


def _apply_enabled_entries_patch(text: str, table: str, value: object) -> str:
    for index, raw_item in enumerate(_require_list(value, table)):
        item = _require_mapping(raw_item, f"{table}[{index}]")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.strip() or "\n" in item_id or "\r" in item_id:
            raise EditorError(f"{table}[{index}].id must be a non-empty single-line string")
        path = (table, item_id.strip())
        if item.get("delete") is True:
            text = remove_table(text, path)
        elif "enabled" in item:
            text = _patch_optional_bool(text, path, "enabled", item["enabled"])
    return text


def _remove_sandbox_workspace_write(text: str) -> str:
    return remove_table(remove_top_level_value(text, "sandbox_workspace_write"), ("sandbox_workspace_write",))


def _set_sandbox_workspace_value(text: str, key: str, value: object) -> str:
    config = parse_toml_text(text)
    sandbox = config.get("sandbox_workspace_write")
    section = _find_table_section(text, ("sandbox_workspace_write",))
    if section is not None:
        if value is None:
            return remove_table_value(text, ("sandbox_workspace_write",), key)
        return set_table_value(text, ("sandbox_workspace_write",), key, value)
    if sandbox is not None:
        # Re-serializing an inline table would retain its parsed fields but
        # lose comments/formatting.  Keep raw text authoritative for that
        # uncommon form instead of silently altering it.
        raise EditorError(
            "sandbox_workspace_write uses inline TOML; edit the raw TOML instead"
        )
    if value is None:
        return text
    return set_table_value(text, ("sandbox_workspace_write",), key, value)


def _apply_permissions_patch(text: str, value: object) -> str:
    patch = _require_mapping(value, "permissions")
    mode = patch.get("mode")
    if mode == "profile":
        mode = "profiles"
    if mode is not None and mode not in {"legacy", "profiles", "unset"}:
        raise EditorError("permissions.mode must be legacy, profile, profiles, or unset")
    if mode == "profiles":
        text = remove_top_level_value(text, "sandbox_mode")
        text = _remove_sandbox_workspace_write(text)
    elif mode in {"legacy", "unset"}:
        text = remove_top_level_value(text, "default_permissions")
    if mode == "unset":
        text = remove_top_level_value(text, "sandbox_mode")
        text = _remove_sandbox_workspace_write(text)

    # Shared approval values work in either mode.
    if "approval_policy" in patch:
        policy = patch["approval_policy"]
        if policy is not None and not isinstance(policy, (str, dict)):
            raise EditorError("approval_policy must be a string, object, or null")
        text = remove_top_level_value(text, "approval_policy") if policy is None else set_top_level_value(text, "approval_policy", policy)
    if "approvals_reviewer" in patch:
        text = _patch_optional_string(text, None, "approvals_reviewer", patch["approvals_reviewer"])
    if "default_permissions" in patch and mode not in {"legacy", "unset"}:
        text = _patch_optional_string(text, None, "default_permissions", patch["default_permissions"])
    if "sandbox_mode" in patch and mode not in {"profiles", "unset"}:
        sandbox_mode = patch["sandbox_mode"]
        if sandbox_mode is not None and sandbox_mode not in {
            "read-only",
            "workspace-write",
            "danger-full-access",
        }:
            raise EditorError("sandbox_mode must be read-only, workspace-write, danger-full-access, or null")
        text = remove_top_level_value(text, "sandbox_mode") if sandbox_mode is None else set_top_level_value(text, "sandbox_mode", sandbox_mode)
    if "network_access" in patch and mode not in {"profiles", "unset"}:
        network_access = patch["network_access"]
        if network_access is not None and not isinstance(network_access, bool):
            raise EditorError("network_access must be true, false, or null")
        text = _set_sandbox_workspace_value(text, "network_access", network_access)
    if "writable_roots" in patch and mode not in {"profiles", "unset"}:
        writable_roots = patch["writable_roots"]
        if writable_roots is not None and (
            not isinstance(writable_roots, list)
            or not all(isinstance(root, str) and root for root in writable_roots)
        ):
            raise EditorError("writable_roots must be an array of non-empty strings or null")
        text = _set_sandbox_workspace_value(text, "writable_roots", writable_roots)
    return text


def _apply_features_patch(text: str, value: object) -> str:
    features = _require_mapping(value, "features")
    for key, flag in features.items():
        canonical_key = FEATURE_KEY_ALIASES.get(key, key)
        if canonical_key not in SUPPORTED_FEATURE_KEYS:
            raise EditorError(f"features.{key} is not editable in the structured UI")
        if flag is None:
            text = remove_table_value(text, ("features",), canonical_key)
        elif isinstance(flag, bool):
            text = set_table_value(text, ("features",), canonical_key, flag)
        else:
            raise EditorError(f"features.{key} must be true, false, or null")
    return text


def _apply_agents_patch(text: str, value: object) -> str:
    agents = _require_mapping(value, "agents")
    allowed = {"max_threads", "max_depth", "job_max_runtime_seconds", "interrupt_message"}
    for key, item in agents.items():
        if key not in allowed:
            raise EditorError(f"agents.{key} is not editable in the structured UI")
        if item is None:
            text = remove_table_value(text, ("agents",), key)
        elif key == "interrupt_message" and isinstance(item, bool):
            text = set_table_value(text, ("agents",), key, item)
        elif key != "interrupt_message" and isinstance(item, int) and item > 0:
            text = set_table_value(text, ("agents",), key, item)
        else:
            raise EditorError(f"agents.{key} has an invalid value")
    return text


def _optional_integer_string(value: object, label: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise EditorError(f"{label} must be a positive whole number or null")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        result = int(value.strip())
    else:
        raise EditorError(f"{label} must be a positive whole number or null")
    if result <= 0:
        raise EditorError(f"{label} must be a positive whole number or null")
    return result


def _apply_advanced_patch(text: str, value: object) -> str:
    patch = _require_mapping(value, "advanced")
    allowed = {
        "shell_environment_inherit",
        "history_persistence",
        "agents_max_threads",
        "agents_max_depth",
        "file_opener",
        "mcp_oauth_credentials_store",
    }
    unknown = set(patch).difference(allowed)
    if unknown:
        raise EditorError(f"advanced.{sorted(unknown)[0]} is not editable in the structured UI")
    if "shell_environment_inherit" in patch:
        inherit = patch["shell_environment_inherit"]
        if inherit is not None and inherit not in {"all", "core", "none"}:
            raise EditorError("shell_environment_inherit must be all, core, none, or null")
        text = (
            remove_table_value(text, ("shell_environment_policy",), "inherit")
            if inherit is None
            else set_table_value(text, ("shell_environment_policy",), "inherit", inherit)
        )
    if "history_persistence" in patch:
        persistence = patch["history_persistence"]
        if persistence is not None and persistence not in {"save-all", "none"}:
            raise EditorError("history_persistence must be save-all, none, or null")
        text = (
            remove_table_value(text, ("history",), "persistence")
            if persistence is None
            else set_table_value(text, ("history",), "persistence", persistence)
        )
    for source, target in (("agents_max_threads", "max_threads"), ("agents_max_depth", "max_depth")):
        if source not in patch:
            continue
        integer = _optional_integer_string(patch[source], source)
        text = (
            remove_table_value(text, ("agents",), target)
            if integer is None
            else set_table_value(text, ("agents",), target, integer)
        )
    if "file_opener" in patch:
        file_opener = patch["file_opener"]
        if file_opener is not None and file_opener not in {
            "vscode",
            "vscode-insiders",
            "windsurf",
            "cursor",
            "none",
        }:
            raise EditorError("file_opener has an unsupported value")
        text = (
            remove_top_level_value(text, "file_opener")
            if file_opener is None
            else set_top_level_value(text, "file_opener", file_opener)
        )
    if "mcp_oauth_credentials_store" in patch:
        credentials_store = patch["mcp_oauth_credentials_store"]
        if credentials_store is not None and credentials_store not in {"auto", "file", "keyring"}:
            raise EditorError("mcp_oauth_credentials_store must be auto, file, keyring, or null")
        text = (
            remove_top_level_value(text, "mcp_oauth_credentials_store")
            if credentials_store is None
            else set_top_level_value(text, "mcp_oauth_credentials_store", credentials_store)
        )
    return text


def _selected_litellm_model(
    selection: object, runtime_config_path: pathlib.Path
) -> dict[str, str]:
    selected = _require_mapping(selection, "litellm_model")
    model = selected.get("model")
    provider = selected.get("provider", "")
    deployment_id = selected.get("deployment_id", selected.get("deploymentId", ""))
    if not isinstance(model, str) or not model.strip():
        raise EditorError("litellm_model.model must be a non-empty string")
    if not isinstance(provider, str) or not isinstance(deployment_id, str):
        raise EditorError("litellm_model provider and deployment_id must be strings")
    candidates = [item for item in configured_models(load_yaml(runtime_config_path)) if item["model"] == model.strip()]
    if provider.strip():
        candidates = [item for item in candidates if item["provider"] == provider.strip()]
    if deployment_id.strip():
        candidates = [item for item in candidates if item["deployment_id"] == deployment_id.strip()]
    if len(candidates) != 1:
        raise EditorError("Select one current LiteLLM provider and model")
    return candidates[0]


def _set_auth_api_key(auth: dict[str, Any], value: object) -> tuple[dict[str, Any], bool]:
    if value is not None and not isinstance(value, str):
        raise EditorError("api_key must be a string or null")
    updated = dict(auth)
    if value is None or value == "":
        changed = "OPENAI_API_KEY" in updated
        updated.pop("OPENAI_API_KEY", None)
        return updated, changed
    if "\n" in value or "\r" in value:
        raise EditorError("api_key cannot contain a line break")
    changed = updated.get("OPENAI_API_KEY") != value
    updated["OPENAI_API_KEY"] = value
    return updated, changed


def _apply_direct_connection_patch(text: str, value: object) -> str:
    """Update the endpoint used by the currently selected direct provider.

    ``openai_base_url`` is intentionally only for Codex's built-in OpenAI
    provider.  A custom provider owns its endpoint below
    ``[model_providers.<id>]``.  Keeping this distinction in the editor
    protocol prevents a UI edit from being valid TOML that Codex never uses.
    """

    connection = _require_mapping(value, "direct_connection")
    unknown = set(connection).difference({"provider", "base_url"})
    if unknown:
        raise EditorError(
            f"direct_connection.{sorted(unknown)[0]} is not editable in the structured UI"
        )
    provider_id = connection.get("provider")
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise EditorError("direct_connection.provider must be a non-empty string")
    if "base_url" not in connection:
        raise EditorError("direct_connection.base_url is required")

    provider_id = provider_id.strip()
    base_url = connection["base_url"]
    if provider_id == "openai":
        return _patch_optional_string(text, None, "openai_base_url", base_url)

    if provider_id in {"amazon-bedrock", "ollama", "lmstudio"}:
        raise EditorError(
            "The selected built-in provider does not use an editable endpoint URL"
        )

    path = ("model_providers", provider_id)
    if _find_table_section(text, path) is None:
        raise EditorError(
            "Selected custom provider must be defined before changing its endpoint URL"
        )
    return _patch_optional_string(text, path, "base_url", base_url)


def apply_structured_patch(
    config_text: str,
    auth_text: str,
    patch: object,
    runtime_config_path: pathlib.Path,
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    """Patch known fields while preserving unrelated TOML and JSON fields."""

    parse_toml_text(config_text)
    auth = parse_auth_text(auth_text)
    patch_data = _require_mapping(patch, "patch")
    # The native Swift client namespaces every editable value below
    # ``patch.structured``.  Accept the older flat form too so an in-flight
    # dialog upgrade does not lose a user's staged edit.
    if "structured" in patch_data:
        patch_data = _require_mapping(patch_data["structured"], "patch.structured")
    result = config_text
    updated_auth = dict(auth)
    auth_changed = False

    selection = patch_data.get("litellm_model", patch_data.get("selected_model"))
    connection = patch_data.get("connection")
    if connection is not None:
        connection_data = _require_mapping(connection, "connection")
        if connection_data.get("use_litellm_menu") is True:
            selection = connection_data.get("model_selection", connection_data.get("litellm_model", selection))
    if selection is not None:
        selected = _selected_litellm_model(selection, runtime_config_path)
        runtime_config = load_yaml(runtime_config_path)
        result = set_top_levels(
            result,
            {
                "model_provider": "openai",
                "model": selected["model"],
                "openai_base_url": local_base_url(),
                "cli_auth_credentials_store": "file",
            },
        )
        updated_auth, auth_changed = _set_auth_api_key(updated_auth, local_api_key(runtime_config))

    if "direct_connection" in patch_data:
        result = _apply_direct_connection_patch(result, patch_data["direct_connection"])

    # Core fields are accepted directly (the exact shape returned in
    # ``structured``) or below a ``core`` wrapper for non-native callers.
    core_patch: dict[str, Any] = {}
    if "core" in patch_data:
        core_patch.update(_require_mapping(patch_data["core"], "core"))
    for key in CORE_SCALAR_KEYS:
        if key in patch_data:
            core_patch[key] = patch_data[key]
    if "api_url" in patch_data:
        core_patch["openai_base_url"] = patch_data["api_url"]
    for key, value in core_patch.items():
        if key not in CORE_SCALAR_KEYS:
            raise EditorError(f"{key} is not editable in the structured UI")
        if value is None:
            result = remove_top_level_value(result, key)
        elif key in {
            "model_context_window",
            "model_auto_compact_token_limit",
            "tool_output_token_limit",
        }:
            numeric_value = _optional_integer_string(value, key)
            result = (
                remove_top_level_value(result, key)
                if numeric_value is None
                else set_top_level_value(result, key, numeric_value)
            )
        elif isinstance(value, (str, int, float, bool)):
            if isinstance(value, str) and ("\n" in value or "\r" in value):
                raise EditorError(f"{key} cannot contain a line break")
            result = set_top_level_value(result, key, value)
        else:
            raise EditorError(f"{key} has an invalid value")
    if "api_key" in patch_data:
        updated_auth, changed = _set_auth_api_key(updated_auth, patch_data["api_key"])
        auth_changed = auth_changed or changed

    if "features" in patch_data:
        result = _apply_features_patch(result, patch_data["features"])
    if "permissions" in patch_data:
        result = _apply_permissions_patch(result, patch_data["permissions"])
    if "providers" in patch_data:
        result = _apply_provider_patch(result, patch_data["providers"])
    if "mcp_servers" in patch_data:
        result = _apply_mcp_patch(result, patch_data["mcp_servers"])
    if "plugins" in patch_data:
        result = _apply_enabled_entries_patch(result, "plugins", patch_data["plugins"])
    if "apps" in patch_data:
        result = _apply_enabled_entries_patch(result, "apps", patch_data["apps"])
    if "agents" in patch_data:
        result = _apply_agents_patch(result, patch_data["agents"])
    if "advanced" in patch_data:
        result = _apply_advanced_patch(result, patch_data["advanced"])

    # A structured form represents the complete state of each visible list.
    # Remove only managed entries absent from that form, preserving unknown
    # TOML tables/fields outside the UI rather than replacing whole files.
    if "providers" in patch_data:
        desired_providers = {
            item["id"].strip()
            for item in _require_list(patch_data["providers"], "providers")
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and item.get("delete") is not True
        }
        for provider_id in list(_get_mapping(parse_toml_text(result).get("model_providers"))):
            if provider_id not in desired_providers:
                result = remove_table(result, ("model_providers", str(provider_id)))
    if "mcp_servers" in patch_data:
        desired_servers = {
            item["id"].strip()
            for item in _require_list(patch_data["mcp_servers"], "mcp_servers")
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and item.get("delete") is not True
        }
        for server_id in list(_get_mapping(parse_toml_text(result).get("mcp_servers"))):
            if server_id not in desired_servers:
                result = remove_table(result, ("mcp_servers", str(server_id)))
    if "plugins" in patch_data:
        desired_plugins = {
            item["id"].strip()
            for item in _require_list(patch_data["plugins"], "plugins")
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and item.get("delete") is not True
        }
        for plugin_id in list(_get_mapping(parse_toml_text(result).get("plugins"))):
            if plugin_id not in desired_plugins:
                result = remove_table(result, ("plugins", str(plugin_id)))

    integrations = patch_data.get("integrations")
    if integrations is not None:
        integration_patch = _require_mapping(integrations, "integrations")
        if "mcp_servers" in integration_patch and "mcp_servers" not in patch_data:
            result = _apply_mcp_patch(result, integration_patch["mcp_servers"])
        if "plugins" in integration_patch and "plugins" not in patch_data:
            result = _apply_enabled_entries_patch(result, "plugins", integration_patch["plugins"])
        if "apps" in integration_patch and "apps" not in patch_data:
            result = _apply_enabled_entries_patch(result, "apps", integration_patch["apps"])

    parsed = parse_toml_text(result)
    errors = semantic_validation(parsed, updated_auth)
    if errors:
        raise EditorError("; ".join(errors))
    next_auth_text = (
        json.dumps(updated_auth, ensure_ascii=False, indent=2) + "\n" if auth_changed else auth_text
    )
    # Re-parse the serialized JSON as a final guard against accidental changes
    # to the expected auth-object shape.
    parsed_auth = parse_auth_text(next_auth_text)
    return result, next_auth_text, parsed, parsed_auth


def sync_editor(payload: dict[str, Any], runtime_config_path: pathlib.Path) -> dict[str, Any]:
    config_text = _require_string(payload, "config_text")
    auth_text = _require_string(payload, "auth_text")
    try:
        if "patch" in payload and payload["patch"] is not None:
            config_text, auth_text, config, auth = apply_structured_patch(
                config_text, auth_text, payload["patch"], runtime_config_path
            )
        else:
            config = parse_toml_text(config_text)
            auth = parse_auth_text(auth_text)
            errors = semantic_validation(config, auth)
            if errors:
                raise EditorError("; ".join(errors))
    except EditorError as exc:
        # Text-area edits are intentionally non-fatal: the UI needs the raw
        # draft back so it can preserve the user's work and show a precise,
        # source-safe validation message while Apply stays disabled.  Nothing
        # in this branch reads or writes CODEX_HOME.
        return editor_payload(
            {},
            {},
            config_text=config_text,
            auth_text=auth_text,
            runtime_config_path=runtime_config_path,
            validation_errors=[str(exc)],
        )
    return editor_payload(
        config,
        auth,
        config_text=config_text,
        auth_text=auth_text,
        runtime_config_path=runtime_config_path,
    )


def atomic_write(path: pathlib.Path, data: str, mode: int = 0o600) -> None:
    """Atomically replace one regular file without following a final symlink."""

    _safe_file_state(path, path.name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
    except OSError:
        raise EditorError(f"Could not prepare {path.name} for writing") from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        # Check again immediately before replace so a target swapped for a
        # symlink between the first lstat and this point is refused.
        _safe_file_state(path, path.name)
        os.replace(temporary, path)
        os.chmod(path, mode)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except EditorError:
        raise
    except OSError:
        raise EditorError(f"Could not write {path.name}") from None
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _restore_file(path: pathlib.Path, original: tuple[bool, str, int]) -> None:
    existed, text, mode = original
    if existed:
        atomic_write(path, text, mode)
        return
    _safe_file_state(path, path.name)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        raise EditorError(f"Could not restore {path.name}") from None


def atomic_write_editor_files(config_text: str, auth_text: str) -> tuple[pathlib.Path, pathlib.Path]:
    """Commit config/auth together with rollback if the second replace fails."""

    home = codex_home()
    config_path = home / "config.toml"
    auth_path = home / "auth.json"
    old_config_text, config_existed, config_mode = _safe_read_text(
        config_path, "Codex config file"
    )
    old_auth_text, auth_existed, auth_mode = _safe_read_text(auth_path, "Codex auth file")
    old_config = (config_existed, old_config_text, config_mode)
    old_auth = (auth_existed, old_auth_text, auth_mode)
    auth_written = False
    config_written = False
    try:
        # Auth first means a brief reader sees a usable credential for either
        # the old or new config, never a new config with stale credentials.
        atomic_write(auth_path, auth_text, 0o600)
        auth_written = True
        atomic_write(config_path, config_text, 0o600)
        config_written = True
    except Exception:
        # Best effort restoration is intentionally performed for both paths:
        # an OS error can occur after os.replace but before the caller sees a
        # successful return.  Preserve the original modes on rollback.
        restoration_error: Exception | None = None
        for target, original, changed in (
            (config_path, old_config, config_written or True),
            (auth_path, old_auth, auth_written),
        ):
            if not changed:
                continue
            try:
                _restore_file(target, original)
            except Exception as exc:  # pragma: no cover - catastrophic FS path
                restoration_error = exc
        if restoration_error is not None:
            raise EditorError("Could not apply Codex configuration or restore its previous files") from None
        raise
    return config_path, auth_path


def apply_editor(payload: dict[str, Any], runtime_config_path: pathlib.Path) -> dict[str, Any]:
    config_text = _require_string(payload, "config_text")
    auth_text = _require_string(payload, "auth_text")
    config = parse_toml_text(config_text)
    auth = parse_auth_text(auth_text)
    errors = semantic_validation(config, auth)
    if errors:
        raise EditorError("; ".join(errors))
    config_path, auth_path = atomic_write_editor_files(config_text, auth_text)
    result = editor_payload(
        config,
        auth,
        config_text=config_text,
        auth_text=auth_text,
        runtime_config_path=runtime_config_path,
    )
    # This IPC response goes directly to the password-bearing editor, which
    # needs both exact texts to establish its post-Apply disk baseline.  The
    # command is intentionally called with logCommand:false; status/errors
    # never include raw auth data.
    result.update(
        {
            "applied": True,
            "config_path": str(config_path),
            "auth_path": str(auth_path),
        }
    )
    return result


def apply_selected_model(
    config: dict[str, Any],
    model: str,
    provider: str = "",
    deployment_id: str = "",
) -> dict[str, str]:
    """Compatibility bridge for callers still using the former chooser."""

    candidates = [item for item in configured_models(config) if item["model"] == model]
    if provider:
        candidates = [item for item in candidates if item["provider"] == provider]
    if deployment_id:
        candidates = [item for item in candidates if item["deployment_id"] == deployment_id]
    if len(candidates) != 1:
        raise EditorError("Select one current LiteLLM provider and model")
    selected = candidates[0]
    home = codex_home()
    config_path = home / "config.toml"
    auth_path = home / "auth.json"
    current, _, _ = _safe_read_text(config_path, "Codex config file")
    auth, _ = read_auth(auth_path)
    next_config = set_top_levels(
        current,
        {
            "model_provider": "openai",
            "model": selected["model"],
            "openai_base_url": local_base_url(),
            "cli_auth_credentials_store": "file",
        },
    )
    next_auth = dict(auth)
    next_auth["OPENAI_API_KEY"] = local_api_key(config)
    atomic_write_editor_files(next_config, json.dumps(next_auth, ensure_ascii=False, indent=2) + "\n")
    return {
        "model": selected["model"],
        "provider": selected["provider"],
        "config": str(config_path),
        "auth": str(auth_path),
    }


def status(config: dict[str, Any]) -> dict[str, Any]:
    home = codex_home()
    config_path = home / "config.toml"
    auth_path = home / "auth.json"
    # Do not read the values here: legacy status is deliberately safe to send
    # to a menu log and must never include either key material.
    return {
        "codex_home": str(home),
        "models": configured_models(config),
        "config_exists": config_path.exists() and not config_path.is_symlink(),
        "auth_file_exists": auth_path.exists() and not auth_path.is_symlink(),
    }


def _read_json_stdin() -> dict[str, Any]:
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        raise EditorError(f"Editor request is not valid JSON (line {exc.lineno}, column {exc.colno})") from None
    if not isinstance(payload, dict):
        raise EditorError("Editor request must be a JSON object")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Configure Codex through LiteLLM Menu.")
    parser.add_argument("command", choices=("status", "apply", "load", "sync", "apply-editor"))
    parser.add_argument("--model", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--deployment-id", default="")
    parser.add_argument("--config", default=str(default_config_path()))
    args = parser.parse_args(argv)
    runtime_config_path = pathlib.Path(args.config).expanduser()
    try:
        if args.command == "load":
            print(json.dumps(load_editor(runtime_config_path), ensure_ascii=False))
            return 0
        if args.command == "sync":
            print(json.dumps(sync_editor(_read_json_stdin(), runtime_config_path), ensure_ascii=False))
            return 0
        if args.command == "apply-editor":
            print(json.dumps(apply_editor(_read_json_stdin(), runtime_config_path), ensure_ascii=False))
            return 0

        config = load_yaml(runtime_config_path)
        if args.command == "status":
            print(json.dumps(status(config), ensure_ascii=False))
            return 0
        result = apply_selected_model(
            config,
            args.model.strip(),
            args.provider.strip(),
            args.deployment_id.strip(),
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except EditorError as exc:
        print(f"Codex config editor failed: {exc}", file=sys.stderr)
        return 1
    except Exception:
        # Do not stringify an arbitrary exception: parser/OS libraries may
        # include a source fragment or a value from auth.json.
        print("Codex config editor failed: could not update configuration", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
