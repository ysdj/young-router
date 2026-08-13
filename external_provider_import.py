#!/usr/bin/env python3
"""Import explicitly selected provider/model configuration into editor JSON.

The command deliberately knows no third-party application locations.  It reads
only an explicit file, or CODEX_HOME/config.toml (and, for the built-in OpenAI
provider only, CODEX_HOME/auth.json).  Its JSON output is intended for the
native editor's in-memory draft; this command never writes configuration.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import stat
import sys
import tomllib
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit

import yaml

from config_editor_core.schema import infer_upstream_fallback_surface, safe_load_yaml_text


MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_SQL_INPUT_BYTES = 64 * 1024 * 1024
MAX_IMPORT_LINK_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_PROVIDERS = 500
MAX_MODELS = 5_000
MAX_API_KEYS = 5_000
MAX_BASE_URL_CHARS = 2_048
MAX_LABEL_CHARS = 512
MAX_SECRET_CHARS = 8_192
SUPPORTED_EXTENSIONS = {".json", ".sql", ".toml", ".yaml", ".yml"}
SUPPORTED_SURFACES = {"openai/responses", "openai/chat", "anthropic"}
CC_SWITCH_SQL_HEADER = "-- CC Switch SQLite 导出"
BASE_URL_KEYS = (
    "api_base",
    "base_url",
    "openai_base_url",
    "url",
    "endpoint",
    "apiBase",
    "baseUrl",
    "baseURL",
    "api-base",
    "base-url",
)
PROVIDER_NAME_KEYS = (
    "name",
    "provider",
    "provider_name",
    "id",
    "label",
    "providerName",
    "provider-name",
)
MODEL_NAME_KEYS = (
    "litellm_model",
    "upstream_model",
    "model",
    "id",
    "model_id",
    "modelName",
    "model-name",
)


class ImportError(ValueError):
    """An error that is safe to present without source values or paths."""


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _label(value: Any, limit: int = MAX_LABEL_CHARS) -> str:
    text = _text(value)
    if (
        not text
        or len(text) > limit
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
    ):
        return ""
    return text


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def _first_text(source: dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = _label(source.get(key))
        if value:
            return value
    return ""


def _usable_secret(value: Any) -> str:
    """Return direct secret text, never an environment-variable reference."""

    text = _text(value)
    if (
        not text
        or len(text) > MAX_SECRET_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
    ):
        return ""
    lowered = text.lower()
    if (
        lowered.startswith("os.environ/")
        or lowered.startswith("env:")
        or text.startswith("${")
        or text.startswith("$")
    ):
        return ""
    return text


def _valid_base_url(value: Any) -> str:
    text = _text(value).rstrip("/")
    if (
        not text
        or len(text) > MAX_BASE_URL_CHARS
        or any(character.isspace() or ord(character) < 32 for character in text)
    ):
        return ""
    parsed = urlsplit(text)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return ""
    return text


def _read_regular_file(path: pathlib.Path, *, max_bytes: int = MAX_INPUT_BYTES) -> str:
    try:
        metadata = path.stat()
    except OSError as exc:
        raise ImportError("The selected configuration could not be read.") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ImportError("The selected configuration must be a regular file.")
    if metadata.st_size > max_bytes:
        raise ImportError("The selected configuration is too large to import.")
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ImportError("The selected configuration must be UTF-8 text.") from exc


def _read_codex_file(path: pathlib.Path) -> str:
    if path.is_symlink():
        raise ImportError("The current Codex configuration must not be a symbolic link.")
    return _read_regular_file(path)


def _parse_input(path: pathlib.Path, *, codex_file: bool = False) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ImportError("Select a .toml, .yaml, .yml, or .json configuration file, or a CC Switch .sql export.")
    if suffix == ".sql":
        raise ImportError("CC Switch SQL exports must be imported through the SQL reader.")
    text = _read_codex_file(path) if codex_file else _read_regular_file(path)
    try:
        if suffix == ".json":
            parsed = json.loads(text)
        elif suffix == ".toml":
            parsed = tomllib.loads(text)
        else:
            parsed = safe_load_yaml_text(text, "The selected configuration")
    except (json.JSONDecodeError, yaml.YAMLError, ValueError) as exc:
        raise ImportError("The selected configuration has an unsupported format.") from exc
    if not isinstance(parsed, dict):
        raise ImportError("The selected configuration must contain an object at its root.")
    return parsed


def _surface_from_values(*values: Any) -> str:
    for value in values:
        text = _text(value).lower()
        if text in SUPPORTED_SURFACES:
            return text
        if text in {"responses", "response", "openai_responses"}:
            return "openai/responses"
        if text in {"chat", "chat_completions", "completions", "openai_chat"}:
            return "openai/chat"
        if text in {"anthropic", "messages", "anthropic_messages"}:
            return "anthropic"
    return "openai/responses"


def _strip_adapter(value: str) -> str:
    for adapter in ("openai/", "anthropic/"):
        if value.startswith(adapter):
            return value[len(adapter) :]
    return value


def _lite_llm_model(value: str, surface: str) -> str:
    model = value.strip()
    if not model:
        return ""
    if model.startswith(("openai/", "anthropic/")):
        return model
    adapter = "anthropic" if surface == "anthropic" else "openai"
    return f"{adapter}/{model}"


def _key_records(source: dict[str, Any], fallback_name: str = "default") -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen_values: set[str] = set()
    seen_names: set[str] = set()

    def append(name: Any, value: Any) -> None:
        secret = _usable_secret(value)
        key_name = _label(name) or fallback_name
        if not secret or key_name in seen_names or secret in seen_values:
            return
        result.append({"name": key_name, "value": secret})
        seen_names.add(key_name)
        seen_values.add(secret)

    raw_keys = source.get("api_keys") or source.get("apiKeys") or source.get("api-keys")
    if isinstance(raw_keys, list):
        for index, item in enumerate(raw_keys, start=1):
            if isinstance(item, dict):
                append(
                    item.get("name") or f"key-{index}",
                    item.get("value")
                    or item.get("api_key")
                    or item.get("apiKey")
                    or item.get("api-key")
                    or item.get("key")
                    or item.get("token"),
                )
            else:
                append(f"key-{index}", item)
    elif isinstance(raw_keys, dict):
        for name, value in raw_keys.items():
            append(name, value)
    append(fallback_name, source.get("api_key"))
    append(fallback_name, source.get("apiKey"))
    append(fallback_name, source.get("api-key"))
    append(fallback_name, source.get("key"))
    append(fallback_name, source.get("token"))
    return result


def _model_records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return [
            {"model": item.strip()}
            for item in value.replace("\n", ",").split(",")
            if item.strip()
        ]
    if isinstance(value, dict):
        if any(key in value for key in (*MODEL_NAME_KEYS, "model_name")):
            return [value]
        result: list[dict[str, Any]] = []
        for name, nested in value.items():
            if isinstance(nested, dict):
                merged = dict(nested)
                merged.setdefault("name", name)
                result.append(merged)
            elif isinstance(nested, str):
                result.append({"name": name, "model": nested})
            elif nested is True:
                result.append({"name": name, "model": name})
        return result
    if isinstance(value, list):
        result: list[dict[str, Any]] = []
        for item in value:
            result.extend(_model_records(item))
        return result
    return []


@dataclass
class _ImportQuota:
    providers: int = 0
    models: int = 0
    api_keys: int = 0

    def add_provider(self) -> None:
        if self.providers >= MAX_PROVIDERS:
            raise ImportError("The selected configuration contains too many providers or models.")
        self.providers += 1

    def add_model(self) -> None:
        if self.models >= MAX_MODELS:
            raise ImportError("The selected configuration contains too many providers or models.")
        self.models += 1

    def add_api_key(self) -> None:
        if self.api_keys >= MAX_API_KEYS:
            raise ImportError("The selected configuration contains too many API keys.")
        self.api_keys += 1


@dataclass
class _ProviderDraft:
    name: str
    api_base: str
    quota: _ImportQuota
    enabled: bool = True
    api_keys: list[dict[str, str]] = field(default_factory=list)
    models: list[dict[str, Any]] = field(default_factory=list)

    def add_key(self, name: Any, value: Any) -> str:
        secret = _usable_secret(value)
        if not secret:
            return ""
        desired_name = _label(name) or "default"
        for key in self.api_keys:
            if key["value"] == secret:
                return key["name"]
        used_names = {key["name"] for key in self.api_keys}
        key_name = desired_name
        suffix = 2
        while key_name in used_names:
            key_name = f"{desired_name}-{suffix}"
            suffix += 1
        self.quota.add_api_key()
        self.api_keys.append({"name": key_name, "value": secret})
        return key_name

    def preferred_key(self) -> dict[str, str] | None:
        return next((key for key in self.api_keys if key["name"] == "default"), None) or (
            self.api_keys[0] if self.api_keys else None
        )

    def as_editor(self) -> dict[str, Any] | None:
        if not self.name or not self.api_base or not self.api_keys:
            return None
        primary_key = self.preferred_key()
        return {
            "name": self.name,
            "enabled": self.enabled,
            "api_base": self.api_base,
            "api_key": primary_key["value"] if primary_key else "",
            "api_keys": self.api_keys,
            "models": self.models,
            "extra": {},
        }


class _Drafts:
    def __init__(self) -> None:
        self._items: list[_ProviderDraft] = []
        self._by_identity: dict[tuple[str, str], _ProviderDraft] = {}
        self._used_names: set[str] = set()
        self._quota = _ImportQuota()
        self.skipped = 0

    def provider(self, name: Any, api_base: Any, enabled: bool = True) -> _ProviderDraft | None:
        normalized_name = _label(name)
        normalized_base = _valid_base_url(api_base)
        if not normalized_name or not normalized_base:
            self.skipped += 1
            return None
        identity = (normalized_name, normalized_base)
        existing = self._by_identity.get(identity)
        if existing is not None:
            existing.enabled = existing.enabled and enabled
            return existing

        final_name = normalized_name
        suffix = 2
        while final_name in self._used_names:
            final_name = f"{normalized_name}-{suffix}"
            suffix += 1
        self._quota.add_provider()
        item = _ProviderDraft(final_name, normalized_base, self._quota, enabled)
        self._items.append(item)
        self._used_names.add(final_name)
        self._by_identity[identity] = item
        return item

    def editor_providers(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in self._items:
            provider = item.as_editor()
            if provider is None:
                self.skipped += 1
                continue
            result.append(provider)
        return result


def _add_editor_model(
    provider: _ProviderDraft,
    source: dict[str, Any],
    *,
    default_model: str = "",
    default_surface: str = "openai/responses",
    provider_enabled: bool | None = None,
) -> None:
    source_model = _first_text(source, MODEL_NAME_KEYS) or default_model
    public_model = _first_text(
        source,
        ("model_name", "modelName", "model-name", "name", "alias", "id", "model", "model_id"),
    ) or _strip_adapter(source_model)
    if not source_model:
        return
    explicit_surface = _surface_from_values(
        source.get("upstream_url_surface"),
        source.get("wire_api"),
        source.get("provider_type"),
    )
    has_explicit_surface = any(
        _text(source.get(key)).lower() in SUPPORTED_SURFACES
        or _text(source.get(key)).lower()
        in {
            "responses",
            "response",
            "openai_responses",
            "chat",
            "chat_completions",
            "completions",
            "openai_chat",
            "anthropic",
            "messages",
            "anthropic_messages",
        }
        for key in ("upstream_url_surface", "wire_api", "provider_type")
    )
    surface = (
        explicit_surface
        if has_explicit_surface
        else infer_upstream_fallback_surface(source_model)
    )
    if source_model.startswith("anthropic/"):
        surface = "anthropic"
    litellm_model = _lite_llm_model(source_model, surface)
    if not public_model or not litellm_model:
        return

    model_key = (
        _usable_secret(source.get("api_key"))
        or _usable_secret(source.get("apiKey"))
        or _usable_secret(source.get("api-key"))
        or _usable_secret(source.get("key"))
        or _usable_secret(source.get("token"))
    )
    key_name = ""
    if model_key:
        key_name = provider.add_key(source.get("api_key_name") or source.get("key_name") or public_model, model_key)
    preferred_key = provider.preferred_key()
    if not key_name and preferred_key is not None:
        key_name = preferred_key["name"]
        model_key = preferred_key["value"]
    if not key_name or not model_key:
        return

    model_enabled = _bool(
        source.get("x-litellm-menu-model-enabled", source.get("model_enabled", source.get("enabled", True))),
        True,
    )
    effective_enabled = (provider_enabled if provider_enabled is not None else provider.enabled) and model_enabled
    model_base = _valid_base_url(_first_text(source, BASE_URL_KEYS))
    if model_base == provider.api_base:
        model_base = ""
    order = source.get("order", 1)
    ssl_present = "ssl_verify" in source
    ssl_value = source.get("ssl_verify")
    provider.quota.add_model()
    provider.models.append(
        {
            "enabled": effective_enabled,
            "model_enabled": model_enabled,
            "provider": provider.name,
            "model_name": public_model,
            "litellm_model": litellm_model,
            "api_base": model_base,
            "api_key": model_key,
            "api_key_name": key_name,
            "order": str(order) if order is not None else "1",
            "ssl_verify": str(ssl_value).lower() if ssl_present else "",
            "ssl_verify_present": ssl_present,
            "deployment_id": "",
            "supports_responses_image_generation_tool": _bool(
                source.get("supports_responses_image_generation_tool"), False
            ),
            "supports_responses_image_generation_tool_present": "supports_responses_image_generation_tool" in source,
            "upstream_url_surface": surface,
            "entry_extra": {},
            "litellm_extra": {},
            "model_info_extra": {},
        }
    )


def _import_litellm(data: dict[str, Any]) -> _Drafts | None:
    raw_providers = data.get("providers")
    raw_models = data.get("model_list")
    has_litellm_models = any(
        isinstance(item, dict)
        and ("litellm_params" in item or "model_info" in item)
        for item in _list(raw_models)
    )
    if not isinstance(raw_providers, dict) and not has_litellm_models:
        return None

    drafts = _Drafts()
    by_source_name: dict[str, _ProviderDraft] = {}
    for name, raw_provider in _mapping(raw_providers).items():
        provider_data = _mapping(raw_provider)
        provider = drafts.provider(
            name,
            _first_text(provider_data, BASE_URL_KEYS),
            _bool(provider_data.get("enabled"), True),
        )
        if provider is None:
            continue
        by_source_name[_label(name)] = provider
        for key in _key_records(provider_data):
            provider.add_key(key["name"], key["value"])
        for raw_model in _model_records(
            provider_data.get("models")
            or provider_data.get("modelList")
            or provider_data.get("available_models")
            or provider_data.get("availableModels")
        ):
            _add_editor_model(
                provider,
                raw_model,
                default_surface=_surface_from_values(
                    provider_data.get("wire_api"), provider_data.get("provider_type")
                ),
                provider_enabled=provider.enabled,
            )

    for raw_entry in _list(raw_models):
        entry = _mapping(raw_entry)
        params = _mapping(entry.get("litellm_params"))
        info = _mapping(entry.get("model_info"))
        provider_name = _first_text(info, ("provider",))
        model_base = _first_text(params, BASE_URL_KEYS)
        provider = by_source_name.get(provider_name)
        if provider is None:
            provider = drafts.provider(provider_name or "imported", model_base, True)
            if provider is None:
                continue
            by_source_name[provider_name or "imported"] = provider
        model_key = _usable_secret(params.get("api_key"))
        if model_key:
            provider.add_key(_first_text(info, ("api_key_name",)) or _first_text(entry, ("model_name",)) or "default", model_key)

        model_source = {
            "model_name": entry.get("model_name"),
            "litellm_model": params.get("model"),
            "api_base": model_base,
            "api_key": model_key,
            "api_key_name": info.get("api_key_name"),
            "order": params.get("order", 1),
            "ssl_verify": params.get("ssl_verify"),
            "upstream_url_surface": info.get("upstream_url_surface"),
            "x-litellm-menu-model-enabled": info.get("x-litellm-menu-model-enabled", True),
            "supports_responses_image_generation_tool": info.get("supports_responses_image_generation_tool"),
        }
        if "ssl_verify" not in params:
            model_source.pop("ssl_verify")
        if "supports_responses_image_generation_tool" not in info:
            model_source.pop("supports_responses_image_generation_tool")
        _add_editor_model(provider, model_source, provider_enabled=provider.enabled)
    return drafts


def _cliproxyapi_models(source: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _list(source.get("models")):
        model = _mapping(item)
        upstream = _first_text(model, ("name",))
        if not upstream:
            continue
        result.append(
            {
                "litellm_model": upstream,
                "model_name": _first_text(model, ("alias", "name")) or upstream,
            }
        )
    return result


def _import_cliproxyapi(data: dict[str, Any]) -> _Drafts | None:
    """Import only documented CLIProxyAPI upstream credential containers."""

    compatibility = data.get("openai-compatibility")
    codex_keys = data.get("codex-api-key")
    if not isinstance(compatibility, list) and not isinstance(codex_keys, list):
        return None

    drafts = _Drafts()
    force_prefix = _bool(data.get("force-model-prefix"), False)
    for index, raw_provider in enumerate(_list(compatibility), start=1):
        source = _mapping(raw_provider)
        provider = drafts.provider(
            _first_text(source, ("name",)) or f"openai-compatible-{index}",
            _first_text(source, ("base-url",)),
            not _bool(source.get("disabled"), False),
        )
        if provider is None:
            continue
        for key_index, raw_key in enumerate(_list(source.get("api-key-entries")), start=1):
            key = _mapping(raw_key)
            provider.add_key(f"key-{key_index}", key.get("api-key"))
        prefix = _label(source.get("prefix"))
        for model in _cliproxyapi_models(source):
            if force_prefix and prefix:
                model["model_name"] = f"{prefix}/{model['model_name']}"
            _add_editor_model(
                provider,
                model,
                default_surface="openai/chat",
                provider_enabled=provider.enabled,
            )

    for index, raw_key in enumerate(_list(codex_keys), start=1):
        source = _mapping(raw_key)
        prefix = _first_text(source, ("prefix",))
        provider = drafts.provider(
            f"cliproxy-codex-{prefix or index}",
            _first_text(source, ("base-url",)),
            not _bool(source.get("disabled"), False),
        )
        if provider is None:
            continue
        provider.add_key("default", source.get("api-key"))
        for model in _cliproxyapi_models(source):
            if force_prefix and prefix:
                model["model_name"] = f"{prefix}/{model['model_name']}"
            _add_editor_model(
                provider,
                model,
                default_surface="openai/responses",
                provider_enabled=provider.enabled,
            )
    return drafts


def _cc_switch_settings(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        return _mapping(json.loads(value))
    except json.JSONDecodeError:
        return {}


def _add_unique_models(
    provider: _ProviderDraft,
    models: Iterable[str],
    *,
    surface: str,
) -> None:
    seen = {
        _label(model.get("model_name"))
        for model in provider.models
        if isinstance(model, dict)
    }
    for value in models:
        model = _label(value)
        if not model or model in seen:
            continue
        _add_editor_model(
            provider,
            {
                "litellm_model": model,
                "model_name": model,
                "upstream_url_surface": surface,
            },
            default_surface=surface,
            provider_enabled=provider.enabled,
        )
        seen.add(model)


def _add_cc_switch_codex_provider(
    drafts: _Drafts,
    name: str,
    settings: dict[str, Any],
    enabled: bool,
) -> None:
    config_text = _text(settings.get("config"))
    try:
        config = tomllib.loads(config_text)
    except ValueError:
        drafts.skipped += 1
        return
    selected_provider = _first_text(config, ("model_provider",))
    provider_config = _mapping(_mapping(config.get("model_providers")).get(selected_provider))
    base = _first_text(provider_config, ("base_url", "api_base")) or _first_text(
        config, ("base_url", "openai_base_url")
    )
    provider = drafts.provider(name, base, enabled)
    if provider is None:
        return
    auth = _mapping(settings.get("auth"))
    provider.add_key("default", auth.get("OPENAI_API_KEY"))
    for key in _key_records(provider_config):
        provider.add_key(key["name"], key["value"])
    surface = _surface_from_values(provider_config.get("wire_api"), "responses")
    models = [
        _label(_mapping(item).get("model"))
        for item in _list(_mapping(settings.get("modelCatalog")).get("models"))
    ]
    selected_model = _first_text(config, ("model",))
    if selected_model:
        models.append(selected_model)
    _add_unique_models(provider, models, surface=surface)


def _add_cc_switch_claude_provider(
    drafts: _Drafts,
    name: str,
    settings: dict[str, Any],
    enabled: bool,
) -> None:
    env = _mapping(settings.get("env"))
    provider = drafts.provider(name, env.get("ANTHROPIC_BASE_URL"), enabled)
    if provider is None:
        return
    provider.add_key(
        "default",
        env.get("ANTHROPIC_AUTH_TOKEN")
        or env.get("ANTHROPIC_API_KEY")
        or env.get("OPENROUTER_API_KEY")
        or env.get("GOOGLE_API_KEY"),
    )
    _add_unique_models(
        provider,
        (
            _text(env.get(key))
            for key in (
                "ANTHROPIC_MODEL",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL",
                "ANTHROPIC_DEFAULT_OPUS_MODEL",
            )
        ),
        surface="anthropic",
    )


def _add_cc_switch_additive_provider(
    drafts: _Drafts,
    app_type: str,
    name: str,
    settings: dict[str, Any],
    enabled: bool,
) -> None:
    if app_type == "openclaw":
        base, key = settings.get("baseUrl"), settings.get("apiKey")
        models, surface = settings.get("models"), "openai/chat"
    elif app_type == "opencode":
        options = _mapping(settings.get("options"))
        base, key = options.get("baseURL"), options.get("apiKey")
        models, surface = settings.get("models"), "openai/chat"
    elif app_type == "hermes":
        base, key = settings.get("base_url"), settings.get("api_key")
        models = settings.get("models")
        surface = _surface_from_values(settings.get("api_mode"), "chat")
    else:
        drafts.skipped += 1
        return
    provider = drafts.provider(name, base, enabled)
    if provider is None:
        return
    provider.add_key("default", key)
    _add_unique_models(
        provider,
        (
            _first_text(model, ("id", "model", "name"))
            for model in _model_records(models)
        ),
        surface=surface,
    )


def _add_cc_switch_provider(
    drafts: _Drafts,
    *,
    app_type: str,
    name: Any,
    settings: Any,
    enabled: bool = True,
) -> None:
    provider_settings = _cc_switch_settings(settings)
    if not provider_settings:
        drafts.skipped += 1
        return
    provider_name = _label(name) or f"cc-switch-{app_type}"
    if app_type == "codex":
        _add_cc_switch_codex_provider(drafts, provider_name, provider_settings, enabled)
    elif app_type == "claude":
        _add_cc_switch_claude_provider(drafts, provider_name, provider_settings, enabled)
    else:
        _add_cc_switch_additive_provider(
            drafts, app_type, provider_name, provider_settings, enabled
        )


def _split_sql_statements(text: str) -> Iterable[str]:
    start = 0
    index = 0
    quoted = False
    while index < len(text):
        character = text[index]
        if character == "'":
            if quoted and index + 1 < len(text) and text[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif character == ";" and not quoted:
            yield text[start:index].strip()
            start = index + 1
        index += 1
    if text[start:].strip():
        yield text[start:].strip()


def _split_sql_csv(text: str) -> list[str]:
    values: list[str] = []
    start = 0
    index = 0
    quoted = False
    while index < len(text):
        character = text[index]
        if character == "'":
            if quoted and index + 1 < len(text) and text[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif character == "," and not quoted:
            values.append(text[start:index].strip())
            start = index + 1
        index += 1
    values.append(text[start:].strip())
    return values


def _sql_literal(value: str) -> str | None:
    stripped = value.strip()
    if stripped.upper() == "NULL":
        return None
    if len(stripped) >= 2 and stripped[0] == "'" and stripped[-1] == "'":
        return stripped[1:-1].replace("''", "'")
    if re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", stripped):
        return stripped
    return None


def _cc_switch_sql_rows(text: str) -> Iterable[tuple[str, dict[str, str | None]]]:
    if not text.lstrip("\ufeff").startswith(CC_SWITCH_SQL_HEADER):
        raise ImportError("The selected SQL file is not a CC Switch export.")
    pattern = re.compile(
        r'^INSERT(?:\s+OR\s+REPLACE)?\s+INTO\s+"?(providers|settings)"?\s*'
        r"\((.*?)\)\s*VALUES\s*\((.*)\)$",
        re.IGNORECASE | re.DOTALL,
    )
    for statement in _split_sql_statements(text):
        match = pattern.match(statement)
        if match is None:
            continue
        columns = [
            column.strip().strip('"`[]')
            for column in _split_sql_csv(match.group(2))
        ]
        values = _split_sql_csv(match.group(3))
        if len(columns) != len(values):
            continue
        yield match.group(1).lower(), {
            column: _sql_literal(value)
            for column, value in zip(columns, values)
        }


def _import_cc_switch_universal(drafts: _Drafts, value: Any) -> None:
    try:
        providers = json.loads(_text(value))
    except (json.JSONDecodeError, TypeError):
        drafts.skipped += 1
        return
    for identifier, raw_provider in _mapping(providers).items():
        source = _mapping(raw_provider)
        provider = drafts.provider(source.get("name") or identifier, source.get("baseUrl"), True)
        if provider is None:
            continue
        provider.add_key("default", source.get("apiKey"))
        apps = _mapping(source.get("apps"))
        models = _mapping(source.get("models"))
        if _bool(apps.get("codex"), False):
            _add_unique_models(
                provider,
                [_text(_mapping(models.get("codex")).get("model"))],
                surface="openai/responses",
            )
        if _bool(apps.get("claude"), False):
            claude = _mapping(models.get("claude"))
            _add_unique_models(
                provider,
                (
                    _text(claude.get(key))
                    for key in ("model", "haikuModel", "sonnetModel", "opusModel")
                ),
                surface="anthropic",
            )


def _import_cc_switch_sql(text: str) -> _Drafts:
    drafts = _Drafts()
    for table, row in _cc_switch_sql_rows(text):
        if table == "providers":
            app_type = _label(row.get("app_type"))
            if app_type not in {"claude", "codex", "openclaw", "opencode", "hermes"}:
                drafts.skipped += 1
                continue
            _add_cc_switch_provider(
                drafts,
                app_type=app_type,
                name=row.get("name") or row.get("id"),
                settings=row.get("settings_config"),
            )
        elif row.get("key") == "universal_providers":
            _import_cc_switch_universal(drafts, row.get("value"))
    return drafts


def _generic_candidates(data: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    candidates: list[tuple[str, dict[str, Any]]] = []
    if _first_text(data, BASE_URL_KEYS):
        candidates.append((_first_text(data, PROVIDER_NAME_KEYS) or "imported", data))

    raw_providers = data.get("providers")
    if isinstance(raw_providers, list):
        candidates.extend(
            (_first_text(_mapping(item), PROVIDER_NAME_KEYS) or f"imported-{index}", _mapping(item))
            for index, item in enumerate(raw_providers, start=1)
            if isinstance(item, dict)
        )
    elif isinstance(raw_providers, dict):
        for name, item in raw_providers.items():
            if isinstance(item, dict) and _first_text(item, BASE_URL_KEYS):
                candidates.append((_label(name) or "imported", item))

    for container_name in ("data", "channels", "accounts", "endpoints"):
        raw_items = data.get(container_name)
        if isinstance(raw_items, dict):
            raw_items = [
                dict(_mapping(value), name=_first_text(_mapping(value), PROVIDER_NAME_KEYS) or str(name))
                for name, value in raw_items.items()
                if isinstance(value, dict)
            ]
        for index, item in enumerate(_list(raw_items), start=1):
            entry = _mapping(item)
            if _first_text(entry, BASE_URL_KEYS):
                name = _first_text(entry, PROVIDER_NAME_KEYS) or f"{container_name}-{index}"
                candidates.append((name, entry))
    return candidates


def _import_generic(data: dict[str, Any]) -> _Drafts:
    drafts = _Drafts()
    for fallback_name, source in _generic_candidates(data):
        base = _first_text(source, BASE_URL_KEYS)
        provider = drafts.provider(fallback_name, base, _bool(source.get("enabled"), True))
        if provider is None:
            continue
        for key in _key_records(source):
            provider.add_key(key["name"], key["value"])
        model_source = source.get("models") or source.get("modelList") or source.get("available_models") or source.get("availableModels")
        if model_source is None:
            model_source = source.get("model_list")
        if model_source is None:
            model_source = source.get("model") or source.get("model_name")
        for model in _model_records(model_source):
            _add_editor_model(
                provider,
                model,
                default_surface=_surface_from_values(source.get("wire_api"), source.get("provider_type")),
                provider_enabled=provider.enabled,
            )
    return drafts


def _read_codex_openai_key(home: pathlib.Path) -> str:
    auth_path = home / "auth.json"
    if not auth_path.exists():
        return ""
    try:
        auth = json.loads(_read_codex_file(auth_path))
    except (ImportError, json.JSONDecodeError):
        return ""
    return _usable_secret(_mapping(auth).get("OPENAI_API_KEY"))


def _import_codex_data(parsed: dict[str, Any], openai_auth_key: str = "") -> _Drafts:
    drafts = _Drafts()
    selected_provider = _first_text(parsed, ("model_provider",))
    selected_model = _first_text(parsed, ("model",))
    openai_base = _first_text(parsed, ("openai_base_url",))

    if selected_provider in {"", "openai"} and openai_base:
        openai_key = openai_auth_key or _usable_secret(parsed.get("openai_api_key"))
        provider = drafts.provider("openai", openai_base, True)
        if provider is not None:
            provider.add_key("default", openai_key)
            if selected_model:
                _add_editor_model(provider, {"model": selected_model}, default_model=selected_model)

    custom_providers = _mapping(parsed.get("model_providers"))
    for identifier, raw_provider in custom_providers.items():
        source = _mapping(raw_provider)
        provider = drafts.provider(
            _first_text(source, ("name",)) or _label(identifier),
            _first_text(source, ("base_url", "api_base")),
            True,
        )
        if provider is None:
            continue
        for key in _key_records(source):
            provider.add_key(key["name"], key["value"])
        if (
            openai_auth_key
            and _label(identifier) == selected_provider
            and _bool(source.get("requires_openai_auth"), False)
        ):
            provider.add_key("default", openai_auth_key)
        models = _model_records(source.get("models"))
        if not models and _first_text(source, ("model",)):
            models = _model_records(source.get("model"))
        if _label(identifier) == selected_provider and selected_model:
            models.append({"model": selected_model})
        for model in models:
            _add_editor_model(
                provider,
                model,
                default_surface=_surface_from_values(source.get("wire_api"), source.get("provider_type")),
            )
    return drafts


def _import_codex(home: pathlib.Path) -> _Drafts:
    config_path = home / "config.toml"
    parsed = _parse_input(config_path, codex_file=True)
    return _import_codex_data(parsed, _read_codex_openai_key(home))


def _result(drafts: _Drafts, source: str) -> dict[str, Any]:
    providers = drafts.editor_providers()
    model_count = sum(len(_list(provider.get("models"))) for provider in providers)
    if len(providers) > MAX_PROVIDERS or model_count > MAX_MODELS:
        raise ImportError("The selected configuration contains too many providers or models.")
    result = {
        "providers": providers,
        "source": source,
        "summary": {
            "providers": len(providers),
            "models": model_count,
            "skipped": drafts.skipped,
        },
    }
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ImportError("The imported provider and model draft is too large.")
    return result


def import_explicit(path: pathlib.Path) -> dict[str, Any]:
    if path.suffix.lower() == ".sql":
        return _result(
            _import_cc_switch_sql(
                _read_regular_file(path, max_bytes=MAX_SQL_INPUT_BYTES)
            ),
            "file",
        )
    parsed = _parse_input(path)
    if "model_provider" in parsed or "model_providers" in parsed:
        drafts = _import_codex_data(parsed)
    else:
        drafts = (
            _import_cliproxyapi(parsed)
            or _import_litellm(parsed)
            or _import_generic(parsed)
        )
    return _result(drafts, "file")


def _single_link_value(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key, [])
    return values[0].strip() if len(values) == 1 else ""


def import_link(link: str) -> dict[str, Any]:
    if (
        not link
        or len(link.encode("utf-8")) > MAX_IMPORT_LINK_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in link)
    ):
        raise ImportError("The CC Switch import link is invalid.")
    parsed = urlsplit(link)
    if (
        parsed.scheme.lower() != "ccswitch"
        or parsed.netloc.lower() != "v1"
        or parsed.path.rstrip("/") != "/import"
        or parsed.fragment
    ):
        raise ImportError("The CC Switch import link is invalid.")
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise ImportError("The CC Switch import link is invalid.") from exc
    if _single_link_value(query, "resource") != "provider":
        raise ImportError("The CC Switch link does not contain a provider.")

    app = _single_link_value(query, "app").lower()
    if app == "codex":
        surface, model_fields = "openai/responses", ("model",)
    elif app == "claude":
        surface = "anthropic"
        model_fields = ("model", "haikuModel", "sonnetModel", "opusModel")
    else:
        raise ImportError("The CC Switch provider uses an unsupported API surface.")

    drafts = _Drafts()
    provider = drafts.provider(
        _single_link_value(query, "name") or f"new-api-{app}",
        _single_link_value(query, "endpoint"),
        _bool(_single_link_value(query, "enabled"), True),
    )
    if provider is None:
        raise ImportError("The CC Switch link has no valid provider endpoint.")
    if not provider.add_key("default", _single_link_value(query, "apiKey")):
        raise ImportError("The CC Switch link has no usable API key.")
    _add_unique_models(
        provider,
        (_single_link_value(query, field) for field in model_fields),
        surface=surface,
    )
    if not provider.models:
        raise ImportError("The CC Switch link has no usable model.")
    return _result(drafts, "import-link")


def import_link_stdin() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_IMPORT_LINK_BYTES + 1)
    if len(raw) > MAX_IMPORT_LINK_BYTES:
        raise ImportError("The CC Switch import link is too large.")
    try:
        return import_link(raw.decode("utf-8").strip())
    except UnicodeDecodeError as exc:
        raise ImportError("The CC Switch import link must be UTF-8 text.") from exc


def import_codex_current() -> dict[str, Any]:
    raw_home = os.environ.get("CODEX_HOME", "~/.codex").strip() or "~/.codex"
    return _result(_import_codex(pathlib.Path(raw_home).expanduser()), "codex-current")


def import_claude_current() -> dict[str, Any]:
    """Import the current Claude Code settings through the same bounded parser."""

    raw_home = os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude").strip() or "~/.claude"
    path = pathlib.Path(raw_home).expanduser() / "settings.json"
    parsed = _parse_input(path)
    drafts = _Drafts()
    _add_cc_switch_claude_provider(drafts, "claude", parsed, True)
    return _result(drafts, "claude-current")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read an explicit external provider/model configuration into editor JSON."
    )
    parser.add_argument("command", nargs="?")
    parser.add_argument("--input")
    parser.add_argument("--link-stdin", action="store_true")
    args = parser.parse_args(argv)
    if sum((bool(args.command), bool(args.input), args.link_stdin)) != 1:
        parser.error("use exactly one of codex-current, claude-current, --input PATH, or --link-stdin")
    if args.command and args.command not in {"codex-current", "claude-current"}:
        parser.error("the supported commands are codex-current and claude-current")

    try:
        result = (
            import_codex_current()
            if args.command == "codex-current"
            else import_claude_current()
            if args.command == "claude-current"
            else import_link_stdin()
            if args.link_stdin
            else import_explicit(pathlib.Path(args.input).expanduser())
        )
    except ImportError as exc:
        print(f"External provider import failed: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("External provider import failed: the configuration could not be imported.", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
