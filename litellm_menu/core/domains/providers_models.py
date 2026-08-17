"""Providers and models staged settings domain."""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from config_editor_core.schema import (
    MENU_PROVIDER_SOURCE_KEY,
    MENU_RELAY_KEYS_KEY,
    MENU_RELAY_KEYS_VERSION,
    MODEL_ORDER_MODES,
    _menu_order,
    _provider_key_id,
    _provider_source,
    _relay_source,
    _stable_provider_key_id,
    infer_upstream_fallback_surface,
)

from ...api_base import isolated_http_opener, service_root
from ..persistence import atomic_write_text
from ..security import REDACT_TEXT, redact
from ._shared import (
    DomainError,
    _action_name,
    _copy_mapping,
    _default_provider_config_path,
    _direction_destination,
    _index,
    _mapping,
    _move,
    _safe_problem,
    _selected_identifier,
)

class ProvidersModelsDomain:
    """Staged providers/models editing through ``config_editor_core``."""

    name = "providers_models"
    _MODEL_LIST_PROTOCOL = "openai-models-v1"
    _MODEL_LIST_TIMEOUT_SECONDS = 5.0
    _MAX_MODEL_LIST_BYTES = 512 * 1024
    _MAX_MODEL_CANDIDATES = 256
    _MODEL_PROBE_TIMEOUT_SECONDS = 6.0
    _MAX_MODEL_PROBE_BYTES = 256 * 1024
    _API_KEY_TARGET_SEPARATOR = "\x1f"

    def __init__(self, config_path: Path | str | None = None):
        self.config_path = Path(config_path).expanduser() if config_path else _default_provider_config_path()
        self._raw: dict[str, Any] = {}
        self._draft: dict[str, Any] = {}
        self._probe_overlay: dict[str, dict[str, dict[str, Any]]] = {}
        self._disk_revision: object = None
        self._exists = False
        self._provider_editor_ids: dict[int, str] = {}
        self._model_editor_ids: dict[int, str] = {}
        self.revision = 0
        self.reload()

    @staticmethod
    def _empty_document() -> dict[str, str | None]:
        # The config parser accepts a document with no providers. Do not create
        # this document on disk; it merely keeps a missing installation
        # renderable until the user explicitly restores/imports a config.
        # Provider anchors must precede model aliases in the emitted YAML.
        # Keep this first-run document in that order before any staged linked
        # deployment causes the dumper to introduce aliases.
        return {"config": "providers: {}\n\nmodel_list: []\n", "disabled": None}

    def _load(self) -> dict[str, Any]:
        from config_editor_core import load as config_load

        try:
            payload = config_load.load_config(self.config_path)
        except FileNotFoundError:
            return {
                "providers": [],
                "document": self._empty_document(),
                "disk_revision": {"config": {"exists": False, "sha256": ""}, "disabled": {"exists": False, "sha256": ""}},
                "exists": False,
            }
        except Exception as exc:
            raise _safe_problem(exc, "Provider/model configuration could not be loaded") from None
        if not isinstance(payload.get("providers"), list) or not isinstance(payload.get("document"), Mapping):
            raise DomainError("Provider/model configuration is invalid")
        return {
            "providers": copy.deepcopy(payload["providers"]),
            "document": copy.deepcopy(dict(payload["document"])),
            "disk_revision": copy.deepcopy(payload.get("revision")),
            "exists": True,
        }

    def _editor_id(self, item: Mapping[str, Any], *, model: bool = False) -> str:
        registry = self._model_editor_ids if model else self._provider_editor_ids
        identity = id(item)
        if identity not in registry:
            registry[identity] = ("model-" if model else "provider-") + uuid.uuid4().hex
        return registry[identity]

    @staticmethod
    def _provider_editor_key(provider: Mapping[str, Any], index: int) -> str:
        name = str(provider.get("name", "")).strip()
        return name if name else f"#{index}"

    @staticmethod
    def _model_editor_key(model: Mapping[str, Any], index: int) -> str:
        deployment_id = str(model.get("deployment_id", "")).strip().lower()
        return deployment_id if deployment_id else f"#{index}"

    def _editor_id_bindings(self) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
        provider_bindings: dict[str, str] = {}
        model_bindings: dict[tuple[str, str], str] = {}
        providers = self._draft.get("providers", [])
        if not isinstance(providers, list):
            return provider_bindings, model_bindings
        for provider_index, provider in enumerate(providers):
            if not isinstance(provider, Mapping):
                continue
            provider_key = self._provider_editor_key(provider, provider_index)
            provider_bindings[provider_key] = self._editor_id(provider)
            models = provider.get("models", [])
            if not isinstance(models, list):
                continue
            for model_index, model in enumerate(models):
                if isinstance(model, Mapping):
                    model_bindings[(provider_key, self._model_editor_key(model, model_index))] = self._editor_id(model, model=True)
        return provider_bindings, model_bindings

    def _restore_editor_id_bindings(
        self,
        provider_bindings: Mapping[str, str],
        model_bindings: Mapping[tuple[str, str], str],
    ) -> None:
        self._provider_editor_ids.clear()
        self._model_editor_ids.clear()
        providers = self._draft.get("providers", [])
        if not isinstance(providers, list):
            return
        for provider_index, provider in enumerate(providers):
            if not isinstance(provider, Mapping):
                continue
            provider_key = self._provider_editor_key(provider, provider_index)
            provider_id = provider_bindings.get(provider_key)
            if provider_id is not None:
                self._provider_editor_ids[id(provider)] = provider_id
            models = provider.get("models", [])
            if not isinstance(models, list):
                continue
            for model_index, model in enumerate(models):
                if not isinstance(model, Mapping):
                    continue
                model_id = model_bindings.get((provider_key, self._model_editor_key(model, model_index)))
                if model_id is not None:
                    self._model_editor_ids[id(model)] = model_id

    def _copy_model_for_edit(self, model: Mapping[str, Any]) -> dict[str, Any]:
        copied = _copy_mapping(model, "model")
        self._model_editor_ids[id(copied)] = self._editor_id(model, model=True)
        return copied

    def _copy_provider_for_edit(self, provider: Mapping[str, Any]) -> dict[str, Any]:
        copied = _copy_mapping(provider, "provider")
        self._provider_editor_ids[id(copied)] = self._editor_id(provider)
        source_models = provider.get("models")
        copied_models = copied.get("models")
        if isinstance(source_models, list) and isinstance(copied_models, list):
            for source_model, copied_model in zip(source_models, copied_models):
                if isinstance(source_model, Mapping) and isinstance(copied_model, Mapping):
                    self._model_editor_ids[id(copied_model)] = self._editor_id(source_model, model=True)
        return copied

    def _register_new_provider(self, provider: Mapping[str, Any]) -> None:
        self._editor_id(provider)
        models = provider.get("models")
        if isinstance(models, list):
            for model in models:
                if isinstance(model, Mapping):
                    self._editor_id(model, model=True)

    def _probe_model_key(self, model: Mapping[str, Any]) -> str:
        """Return the editor identity so one model's result never aliases another's."""

        return self._editor_id(model, model=True)

    @staticmethod
    def _upstream_model_prefix(model: Mapping[str, Any]) -> str:
        surface = str(model.get("upstream_url_surface", "")).strip()
        return "anthropic" if surface == "anthropic" else "openai"

    @staticmethod
    def _default_upstream_surface(
        provider: Mapping[str, Any],
        model: Mapping[str, Any],
    ) -> str:
        del provider
        return infer_upstream_fallback_surface(model.get("litellm_model"))

    @classmethod
    def _canonical_upstream_model(cls, value: object, model: Mapping[str, Any]) -> str:
        name = str(value or "").strip()
        if not name:
            return ""
        if "/" in name:
            existing_prefix, raw_name = name.split("/", 1)
            if existing_prefix in {"openai", "anthropic"}:
                name = raw_name.strip()
        if not name:
            return ""
        prefix = cls._upstream_model_prefix(model)
        return f"{prefix}/{name}"

    @staticmethod
    def _binding_health(
        model: Mapping[str, Any],
        keys_by_id: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, str]:
        provider_key_id = str(model.get("provider_key_id", "")).strip()
        override = model.get("binding_health")

        def preserved_problem() -> dict[str, str] | None:
            if not isinstance(override, Mapping):
                return None
            status = str(override.get("status", "")).strip()
            # Link state belongs to the selected ProviderKey, not to the
            # model.  Keep only concrete Apply-time failures as an override.
            if not status or status in {"linked", "independent"}:
                return None
            detail = str(override.get("detail", "")).strip()
            return {"status": status, **({"detail": detail} if detail else {})}

        if not provider_key_id:
            problem = preserved_problem()
            if problem is not None:
                return problem
            return {"status": "independent"}
        key = keys_by_id.get(provider_key_id)
        if key is None:
            return {
                "status": "missing_provider_key",
                "detail": "The linked provider key is unavailable",
            }
        source = key.get("source", {})
        if not isinstance(source, Mapping) or source.get("kind") != "relay":
            return {"status": "independent"}
        problem = preserved_problem()
        if problem is not None:
            return problem
        return {"status": "linked"}

    def _safe_provider(self, provider: object, index: int) -> dict[str, Any]:
        if not isinstance(provider, Mapping):
            return {
                "id": f"provider-{index + 1}",
                "name": "",
                "enabled": False,
                "provider_type": "custom",
                "relay_station_id": "",
                "api_key_names": [],
                "models": [],
            }
        name = str(provider.get("name", "")).strip()
        provider_source = self._provider_source_state(provider)
        keys = self._provider_api_keys(provider)
        configured_key = bool(provider.get("api_key"))
        api_key_names: list[str] = []
        key_configured: dict[str, bool] = {}
        key_configured_by_id: dict[str, bool] = {}
        keys_by_id: dict[str, Mapping[str, Any]] = {}
        key_states: list[dict[str, Any]] = []
        if isinstance(keys, Sequence) and not isinstance(keys, (str, bytes, bytearray)):
            for item in keys:
                if not isinstance(item, Mapping):
                    continue
                key_name = str(item.get("name", "")).strip()
                if key_name and key_name not in api_key_names:
                    api_key_names.append(key_name)
                key_value = item.get("value", "")
                configured = isinstance(key_value, str) and bool(key_value.strip())
                provider_key_id = str(item.get("id", "")).strip()
                if key_name:
                    key_configured[key_name] = configured
                if provider_key_id:
                    key_configured_by_id[provider_key_id] = configured
                    keys_by_id[provider_key_id] = item
                configured_key = configured_key or configured
        models: list[dict[str, Any]] = []
        raw_models = provider.get("models", [])
        if isinstance(raw_models, Sequence) and not isinstance(raw_models, (str, bytes, bytearray)):
            for model_index, model in enumerate(raw_models):
                if not isinstance(model, Mapping):
                    continue
                model_key_name = str(model.get("api_key_name", "")).strip()
                provider_key_id = str(model.get("provider_key_id", "")).strip()
                model_api_key = model.get("api_key")
                model_key = (
                    key_configured_by_id.get(provider_key_id, False)
                    if provider_key_id
                    else key_configured.get(model_key_name, False)
                    if model_key_name
                    else isinstance(model_api_key, str) and bool(model_api_key.strip())
                )
                litellm_extra = model.get("litellm_extra", {})
                safe_litellm_extra = {
                    key: redact(value, _key=key)
                    for key, value in litellm_extra.items()
                    if isinstance(litellm_extra, Mapping)
                    and key in {"rpm", "tpm", "timeout", "allowed_openai_params", "drop_params", "additional_drop_params"}
                }
                live_probe = self._probe_overlay.get(name, {}).get(self._probe_model_key(model), {})
                model_enabled = model.get("model_enabled", model.get("enabled", True)) is not False
                effective_order = model.get("effective_order", model.get("order", 0))
                manual_order = model.get("manual_order", model.get("order", 0))
                selected_key = keys_by_id.get(provider_key_id)
                relay_selected = (
                    isinstance(selected_key, Mapping)
                    and isinstance(selected_key.get("source"), Mapping)
                    and selected_key["source"].get("kind") == "relay"
                )
                raw_order_mode = str(model.get("order_mode", "manual")).strip() or "manual"
                order_mode = raw_order_mode if relay_selected or raw_order_mode != "relay_multiplier" else "manual"
                models.append(
                    {
                        "id": str(model.get("deployment_id") or model.get("model_name") or self._editor_id(model, model=True)),
                        "editor_id": self._editor_id(model, model=True),
                        "model_name": str(model.get("model_name", "")),
                        "name": str(model.get("model_name", "")),
                        "display_name": str(model.get("model_name", "")),
                        "litellm_model": str(model.get("litellm_model", "")),
                        "upstream_model": str(model.get("litellm_model", "")).split("/", 1)[-1],
                        "provider": str(model.get("provider", name)),
                        "api_base": REDACT_TEXT(str(model.get("api_base", ""))),
                        "api_key_name": model_key_name,
                        "provider_key_id": provider_key_id,
                        # Relation state is derived from the chosen ProviderKey
                        # so a stale legacy model field cannot claim a relay
                        # association after the key was changed.
                        "catalog_mode": "relay_linked" if relay_selected else "independent",
                        "source_model_id": str(model.get("source_model_id", "")) if relay_selected else "",
                        "order_mode": order_mode,
                        "manual_order": manual_order,
                        "effective_order": effective_order,
                        "binding_health": self._binding_health(model, keys_by_id),
                        "enabled": model_enabled,
                        "model_enabled": model_enabled,
                        "order": effective_order,
                        "ssl_verify": str(model.get("ssl_verify", "")),
                        "ssl_verify_present": bool(model.get("ssl_verify_present")),
                        "deployment_id": str(model.get("deployment_id", "")),
                        "upstream_url_surface": str(model.get("upstream_url_surface", "")),
                        "upstream_protocol_mode": str(model.get("upstream_protocol_mode", "fallback")),
                        "supports_responses_image_generation_tool": bool(model.get("supports_responses_image_generation_tool")),
                        "supports_responses_image_generation_tool_present": bool(model.get("supports_responses_image_generation_tool_present")),
                        "litellm_extra": safe_litellm_extra,
                        "api_key_configured": model_key,
                        "probe": redact(live_probe) if live_probe else None,
                    }
                )
        model_counts: dict[str, int] = {}
        for model in models:
            key_id = str(model.get("provider_key_id", "")).strip()
            key_name = str(model.get("api_key_name", "")).strip()
            count_key = key_id or key_name
            if count_key:
                model_counts[count_key] = model_counts.get(count_key, 0) + 1
        if isinstance(keys, Sequence) and not isinstance(keys, (str, bytes, bytearray)):
            for item in keys:
                if not isinstance(item, Mapping):
                    continue
                key_name = str(item.get("name", "")).strip()
                if not key_name or any(state["name"] == key_name for state in key_states):
                    continue
                key_value = item.get("value", "")
                provider_key_id = str(item.get("id", "")).strip()
                key_states.append(
                    {
                        "id": provider_key_id,
                        "name": key_name,
                        "configured": isinstance(key_value, str) and bool(key_value.strip()),
                        "model_count": model_counts.get(provider_key_id or key_name, 0),
                        "source": copy.deepcopy(item.get("source", {"kind": "independent"})),
                    }
                )
        return {
            "id": name or self._editor_id(provider),
            "editor_id": self._editor_id(provider),
            "name": name,
            "enabled": provider.get("enabled") is not False,
            "provider_type": provider_source["kind"],
            "relay_station_id": provider_source.get("station_id", ""),
            "api_base": REDACT_TEXT(str(provider.get("api_base", ""))),
            "api_key_configured": configured_key,
            "api_key_names": api_key_names,
            # Safe presence/count metadata for the editor. Credential values
            # remain inside the Core draft and native secure-input capability.
            "key_states": key_states,
            "models": models,
            # Unknown non-secret provider metadata remains visible only when
            # it is safe; the full object remains in the private draft.
            "extra": redact(provider.get("extra", {})),
        }

    def snapshot(self) -> dict[str, Any]:
        providers = self._draft.get("providers", [])
        safe = [self._safe_provider(provider, index) for index, provider in enumerate(providers) if isinstance(provider, Mapping)]
        return {
            "domain": self.name,
            "revision": self.revision,
            "exists": self._exists,
            "providers": safe,
            "provider_count": len(safe),
            "raw_editor_available": True,
        }

    def draft_state(self) -> object:
        return copy.deepcopy(self._draft)

    def _replace_draft(self, providers: object, document: object | None = None) -> None:
        from config_editor_core.load import load_config_document, normalize_config_document

        if not isinstance(providers, list):
            raise DomainError("Providers must be an array")
        source = self._draft.get("document", self._empty_document()) if document is None else document
        try:
            normalized = normalize_config_document(source)
            # This validates the complete source document before staging it.
            load_config_document(normalized)
        except Exception as exc:
            raise _safe_problem(exc, "Provider/model configuration is invalid") from None
        self._draft = {"providers": copy.deepcopy(providers), "document": copy.deepcopy(normalized)}
        self._provider_editor_ids.clear()
        self._model_editor_ids.clear()
        self._probe_overlay.clear()

    def _set_raw(self, data: Mapping[str, Any]) -> None:
        from config_editor_core.load import load_config_document, normalize_config_document

        document = data.get("document")
        if document is None:
            config_text = data.get("config", data.get("config_text", data.get("raw_yaml", data.get("text"))))
            disabled_text = data.get("disabled", data.get("disabled_text"))
            if not isinstance(config_text, str):
                raise DomainError("Provider/model YAML must be text")
            document = {"config": config_text, "disabled": disabled_text}
        try:
            normalized = normalize_config_document(document)
            loaded = load_config_document(normalized)
        except Exception as exc:
            raise _safe_problem(exc, "Provider/model configuration is invalid") from None
        self._draft = {"providers": copy.deepcopy(loaded["providers"]), "document": copy.deepcopy(loaded["document"])}
        self._provider_editor_ids.clear()
        self._model_editor_ids.clear()
        self._probe_overlay.clear()

    def _import_selected(self, data: Mapping[str, Any]) -> None:
        """Stage an explicitly selected external config through its existing parser."""

        source = data.get("path")
        if not isinstance(source, str) or not source:
            raise DomainError("Select a provider configuration file")
        try:
            import external_provider_import

            self._stage_import_result(external_provider_import.import_explicit(Path(source)))
        except DomainError:
            raise
        except Exception as exc:
            raise _safe_problem(exc, "Provider configuration could not be imported") from None

    def _stage_import_result(self, imported: object) -> None:
        providers = imported.get("providers") if isinstance(imported, Mapping) else None
        if not isinstance(providers, list):
            raise DomainError("Provider configuration could not be imported")
        self._replace_draft(providers)
        summary = imported.get("summary", {}) if isinstance(imported, Mapping) else {}
        self._last_operation = {
            "operation": "import",
            "summary": redact(summary) if isinstance(summary, Mapping) else {},
        }

    def _import_codex_current(self) -> None:
        try:
            import external_provider_import

            self._stage_import_result(external_provider_import.import_codex_current())
        except DomainError:
            raise
        except Exception as exc:
            raise _safe_problem(exc, "Provider configuration could not be imported") from None

    def _import_claude_current(self) -> None:
        try:
            import external_provider_import

            self._stage_import_result(external_provider_import.import_claude_current())
        except DomainError:
            raise
        except Exception as exc:
            raise _safe_problem(exc, "Provider configuration could not be imported") from None

    def _import_link(self, link: str) -> None:
        try:
            import external_provider_import

            self._stage_import_result(external_provider_import.import_link(link))
        except DomainError:
            raise
        except Exception as exc:
            raise _safe_problem(exc, "Provider configuration could not be imported") from None

    @staticmethod
    def _provider_api_base(provider: Mapping[str, Any]) -> str:
        """Get the provider's effective base without projecting it to clients."""

        base = provider.get("api_base")
        if isinstance(base, str) and base.strip():
            return base.strip()
        models = provider.get("models", [])
        if isinstance(models, Sequence) and not isinstance(models, (str, bytes, bytearray)):
            for model in models:
                if not isinstance(model, Mapping):
                    continue
                candidate = model.get("api_base")
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
        return ""

    @classmethod
    def _provider_credential(
        cls,
        provider: Mapping[str, Any],
        api_key_name: str | None = None,
    ) -> tuple[str, str]:
        """Choose one named Bearer credential without returning it to callers."""

        def credential_value(value: object) -> str:
            if not isinstance(value, str):
                return ""
            text = value.strip()
            if not text.startswith("os.environ/"):
                return text
            variable = text.removeprefix("os.environ/").strip()
            return os.environ.get(variable, "").strip()

        keys = cls._provider_api_keys(provider)
        if api_key_name is not None:
            selected_name = cls._api_key_name(api_key_name)
            key_index = cls._api_key_index(keys, selected_name)
            value = credential_value(keys[key_index].get("value"))
            if not value:
                raise DomainError("The selected API key has no value")
            return selected_name, value

        direct = credential_value(provider.get("api_key"))
        if direct:
            credential = direct
            matching_names = [
                str(item.get("name", "")).strip()
                for item in keys
                if credential_value(item.get("value")) == credential
            ]
            selected_name = "default" if "default" in matching_names else (matching_names[0] if matching_names else "")
            return selected_name, credential

        configured: list[tuple[str, str]] = []
        for item in keys:
            value = credential_value(item.get("value"))
            if not value:
                continue
            configured.append((str(item.get("name", "")).strip(), value))
        for name, value in configured:
            if name == "default":
                return name, value
        return configured[0] if configured else ("", "")

    @classmethod
    def _model_endpoint(cls, api_base: str) -> str | None:
        """Normalize an OpenAI-compatible base to its model-list endpoint."""

        root = service_root(api_base)
        return f"{root}/v1/models" if isinstance(root, str) and root else None

    @classmethod
    def _model_candidates(cls, payload: object) -> list[str] | None:
        if not isinstance(payload, Mapping):
            return None
        data = payload.get("data")
        if not isinstance(data, list):
            return None
        candidates: list[str] = []
        seen: set[str] = set()
        for item in data:
            value = item.get("id") if isinstance(item, Mapping) else item
            if not isinstance(value, str):
                continue
            candidate = REDACT_TEXT(value.strip())
            if not candidate or len(candidate) > 256 or any(ord(char) < 32 for char in candidate):
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            candidates.append(candidate)
            if len(candidates) >= cls._MAX_MODEL_CANDIDATES:
                break
        return candidates

    def _fetch_provider_models(
        self,
        provider: Mapping[str, Any],
        provider_id: str,
        api_key_name: str | None = None,
        *,
        credential_override: str | None = None,
    ) -> dict[str, Any]:
        """Fetch one standard `/v1/models` list without exposing remote details."""

        if credential_override is None:
            selected_key_name, credential = self._provider_credential(provider, api_key_name)
        else:
            selected_key_name = self._api_key_name(api_key_name)
            self._api_key_index(self._provider_api_keys(provider), selected_key_name)
            credential = credential_override
        endpoint = self._model_endpoint(self._provider_api_base(provider))
        summary: dict[str, Any] = {
            "operation": "fetch_models",
            "provider_id": provider_id,
            "protocols": [self._MODEL_LIST_PROTOCOL],
        }
        if selected_key_name:
            summary["api_key_name"] = selected_key_name
        if endpoint is None:
            return {
                **summary,
                "available": False,
                "detail": "The provider model endpoint is not configured",
                "models": [],
                "model_count": 0,
            }

        headers = {"Accept": "application/json", "User-Agent": "LiteLLM-Menu-Core/1"}
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        request = urllib.request.Request(endpoint, headers=headers, method="GET")
        try:
            # Keep credential-bearing requests isolated from ambient proxies
            # and reject redirects before an Authorization header can travel.
            with isolated_http_opener().open(request, timeout=self._MODEL_LIST_TIMEOUT_SECONDS) as response:
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                body = response.read(self._MAX_MODEL_LIST_BYTES + 1)
        except urllib.error.HTTPError:
            return {
                **summary,
                "available": False,
                "detail": "The provider model endpoint rejected the request",
                "models": [],
                "model_count": 0,
            }
        except Exception:
            return {
                **summary,
                "available": False,
                "detail": "The provider model endpoint is unavailable",
                "models": [],
                "model_count": 0,
            }

        if not isinstance(status, int) or status < 200 or status >= 300 or len(body) > self._MAX_MODEL_LIST_BYTES:
            return {
                **summary,
                "available": False,
                "detail": "The provider model endpoint returned an invalid response",
                "models": [],
                "model_count": 0,
            }
        try:
            candidates = self._model_candidates(json.loads(body.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            candidates = None
        if candidates is None:
            return {
                **summary,
                "available": False,
                "detail": "The provider model endpoint returned an invalid model list",
                "models": [],
                "model_count": 0,
            }
        return {
            **summary,
            "available": True,
            "detail": "Provider model list fetched",
            "models": candidates,
            "model_count": len(candidates),
        }

    def _fetch_models(self, data: Mapping[str, Any]) -> dict[str, Any]:
        index = self._provider_index(data)
        provider = self._draft["providers"][index]
        if not isinstance(provider, Mapping):
            raise DomainError("The selected provider is unavailable")
        api_key_name = self._api_key_name(data["api_key_name"]) if "api_key_name" in data else None
        summary = self._fetch_provider_models(
            provider,
            self._safe_provider(provider, index)["id"],
            api_key_name,
        )
        self._last_operation = summary
        return summary

    def _fetch_relay_resource_models(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """Fetch models through a dynamically discovered relay API key.

        The Core coordinator supplies the relay credential only for this
        request.  The staged ProviderKey keeps its stable relay source and is
        materialized again at Apply time; no relay secret becomes draft or
        snapshot data.
        """

        raw_source = data.get("source")
        if not isinstance(raw_source, Mapping):
            raise DomainError("Relay API key is unavailable")
        credential = raw_source.get("api_key")
        if not isinstance(credential, str) or not credential.strip():
            raise DomainError("Relay API key is unavailable")
        provider_index = self._provider_index(data)
        provider = self._draft["providers"][provider_index]
        if not isinstance(provider, Mapping):
            raise DomainError("The selected provider is unavailable")
        provider_root = service_root(self._provider_api_base(provider))
        relay_root = service_root(raw_source.get("api_base"))
        if not provider_root or provider_root != relay_root:
            raise DomainError("Relay API key does not match the provider Base URL")

        imported = self._stage_provider_relay_key_import(
            {
                "provider_id": data.get("provider_id"),
                "source": raw_source,
                "api_key_name": raw_source.get("api_key_name", raw_source.get("name")),
            }
        )
        staged_provider = self._draft["providers"][provider_index]
        if not isinstance(staged_provider, Mapping):
            raise DomainError("The selected provider is unavailable")
        summary = self._fetch_provider_models(
            staged_provider,
            imported["provider_id"],
            imported["api_key_name"],
            credential_override=credential.strip(),
        )
        summary["slot_id"] = imported["slot_id"]
        summary["imported"] = imported["imported"]
        summary["reused"] = imported["reused"]
        self._last_operation = summary
        return summary

    @staticmethod
    def _wire_model_name(model: Mapping[str, Any]) -> str:
        value = str(model.get("litellm_model", "")).strip()
        if "/" in value:
            value = value.split("/", 1)[1]
        return value

    @classmethod
    def _model_api_base(
        cls,
        provider: Mapping[str, Any],
        model: Mapping[str, Any],
    ) -> str:
        value = model.get("api_base")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return cls._provider_api_base(provider)

    @classmethod
    def _model_credential(
        cls,
        provider: Mapping[str, Any],
        model: Mapping[str, Any],
    ) -> tuple[str, str]:
        key_name = str(model.get("api_key_name", "")).strip()
        if key_name:
            return cls._provider_credential(provider, key_name)
        return cls._provider_credential(provider)

    @classmethod
    def _surface_probe(
        cls,
        *,
        surface: str,
        api_base: str,
        credential: str,
        model_name: str,
    ) -> dict[str, Any]:
        root = service_root(api_base)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "LiteLLM-Menu-Core/1",
        }
        if surface == "anthropic":
            endpoint = f"{root}/v1/messages" if isinstance(root, str) and root else ""
            headers["x-api-key"] = credential
            headers["anthropic-version"] = "2023-06-01"
            payload: dict[str, Any] = {
                "model": model_name,
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "Reply with OK."}],
            }
        elif surface == "openai/chat":
            endpoint = f"{root}/v1/chat/completions" if isinstance(root, str) and root else ""
            headers["Authorization"] = f"Bearer {credential}"
            payload = {
                "model": model_name,
                "max_tokens": 8,
                "stream": False,
                "messages": [{"role": "user", "content": "Reply with OK."}],
            }
        else:
            endpoint = f"{root}/v1/responses" if isinstance(root, str) and root else ""
            headers["Authorization"] = f"Bearer {credential}"
            payload = {
                "model": model_name,
                "max_output_tokens": 8,
                "stream": False,
                "input": "Reply with OK.",
            }

        original_request = {
            "method": "POST",
            "url": endpoint,
            "headers": {
                "Accept": "application/json",
                "Content-Type": "application/json",
                **({"anthropic-version": "2023-06-01"} if surface == "anthropic" else {}),
            },
            "body": payload,
        }

        def result(available: bool, status: str) -> dict[str, Any]:
            return {
                "surface": surface,
                "available": available,
                "status": status,
                "original_request": original_request,
            }

        if not endpoint or not credential or not model_name:
            return result(False, "invalid_config")

        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with isolated_http_opener().open(
                request,
                timeout=cls._MODEL_PROBE_TIMEOUT_SECONDS,
            ) as response:
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                body = response.read(cls._MAX_MODEL_PROBE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            code = exc.code
            probe_status = (
                "unsupported"
                if code in {404, 405}
                else "auth_error"
                if code in {401, 403}
                else "rate_limited"
                if code == 429
                else "http_error"
            )
            return result(False, probe_status)
        except Exception:
            return result(False, "network_error")

        if not isinstance(status, int) or not 200 <= status < 300 or len(body) > cls._MAX_MODEL_PROBE_BYTES:
            return result(False, "http_error")
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            decoded = None
        if not isinstance(decoded, Mapping) or isinstance(decoded.get("error"), Mapping):
            return result(False, "invalid_response")
        if surface == "openai/chat":
            usable = isinstance(decoded.get("choices"), list)
        elif surface == "anthropic":
            usable = isinstance(decoded.get("content"), list)
        else:
            usable = any(key in decoded for key in ("id", "output", "output_text"))
        return result(usable, "ok" if usable else "invalid_response")

    def _probe_model(
        self,
        provider: Mapping[str, Any],
        model: Mapping[str, Any],
        *,
        provider_id: str,
        model_id: str,
    ) -> dict[str, Any]:
        provider_name = str(provider.get("name", "")).strip()
        probe_key = model_id
        api_base = self._model_api_base(provider, model)
        _key_name, credential = self._model_credential(provider, model)
        model_name = self._wire_model_name(model)
        surfaces = ["openai/responses", "openai/chat", "anthropic"]

        with ThreadPoolExecutor(max_workers=len(surfaces)) as executor:
            surface_futures = {
                surface: executor.submit(
                    self._surface_probe,
                    surface=surface,
                    api_base=api_base,
                    credential=credential,
                    model_name=model_name,
                )
                for surface in surfaces
            }
            surface_results = {
                surface: future.result()
                for surface, future in surface_futures.items()
            }

        configured_fallback = str(model.get("upstream_url_surface", "")).strip()
        inferred_fallback = infer_upstream_fallback_surface(model_name)
        protocol_mode = str(
            model.get("upstream_protocol_mode", "fallback")
        ).strip().lower()
        preferred_surfaces = (
            (configured_fallback, inferred_fallback)
            if protocol_mode == "fixed"
            else (inferred_fallback, configured_fallback)
        )
        priority = [
            surface
            for surface in (*preferred_surfaces, *surfaces)
            if surface in surfaces
        ]
        priority = list(dict.fromkeys(priority))
        available_surfaces = [
            surface
            for surface in priority
            if surface_results.get(surface, {}).get("available") is True
        ]
        recommended = available_surfaces[0] if available_surfaces else None
        unavailable_surfaces = [surface for surface in priority if surface not in available_surfaces]
        summary = {
            "available_surfaces": available_surfaces,
            "unavailable_surfaces": unavailable_surfaces,
            "statuses": {
                surface: str(surface_results.get(surface, {}).get("status", "unavailable"))
                for surface in priority
            },
        }
        checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        probe_overlay = {
            "available": recommended is not None,
            "recommended_surface": recommended,
            "summary": summary,
            "checked_at": checked_at,
            "surfaces": {
                surface: {
                    "available": result.get("available") is True,
                    "status": str(result.get("status", "unavailable")),
                    "original_request": copy.deepcopy(result.get("original_request", {})),
                }
                for surface, result in surface_results.items()
            },
        }
        return {
            "ok": recommended is not None,
            "available": recommended is not None,
            "protocols": [
                surface
                for surface in surfaces
                if surface_results.get(surface, {}).get("available") is True
            ],
            "recommended_surface": recommended,
            "summary": summary,
            "detail": "Model probe completed" if recommended is not None else "No usable model API surface was found",
            "provider_id": provider_id,
            "model_id": model_id,
            "surfaces": [
                {"surface": surface, **value}
                for surface, value in probe_overlay["surfaces"].items()
            ],
            "_overlay": {
                "provider_name": provider_name,
                "probe_key": probe_key,
                "probe": probe_overlay,
            },
        }

    def _provider_index(self, data: Mapping[str, Any]) -> int:
        providers = self._draft["providers"]
        direct = _selected_identifier(data, "provider_id", "provider", "name", "id", "index")
        if type(direct) is int:
            return _index(direct, len(providers), "provider")
        if isinstance(direct, Mapping):
            direct = direct.get("id", direct.get("name"))
        if isinstance(direct, str):
            for index, provider in enumerate(providers):
                if isinstance(provider, Mapping) and direct in {
                    str(provider.get("name", "")),
                    self._editor_id(provider),
                }:
                    return index
        raise DomainError("The selected provider is unavailable")

    @staticmethod
    def _changes(data: Mapping[str, Any], key: str) -> dict[str, Any]:
        value = data.get("changes", data.get("patch"))
        if value is None:
            candidate = data.get(key)
            value = candidate if isinstance(candidate, Mapping) else {}
        return _copy_mapping(value, "changes")

    def _model_index(self, provider: Mapping[str, Any], data: Mapping[str, Any]) -> int:
        models = provider.get("models", [])
        if not isinstance(models, list):
            raise DomainError("The selected model is unavailable")
        direct = _selected_identifier(data, "model_id", "deployment_id", "model_name", "model", "index")
        if type(direct) is int:
            return _index(direct, len(models), "model")
        if isinstance(direct, Mapping):
            direct = direct.get("deployment_id", direct.get("model_name", direct.get("id")))
        if isinstance(direct, str):
            for index, model in enumerate(models):
                if not isinstance(model, Mapping):
                    continue
                if direct in {
                    str(model.get("deployment_id", "")),
                    str(model.get("model_name", "")),
                    self._editor_id(model, model=True),
                }:
                    return index
        raise DomainError("The selected model is unavailable")

    @classmethod
    def _api_key_name(cls, value: object) -> str:
        name = str(value).strip() if isinstance(value, str) else ""
        if (
            not name
            or len(name.encode("utf-8")) > 128
            or cls._API_KEY_TARGET_SEPARATOR in name
            or any(char in name for char in "\x00\r\n")
        ):
            raise DomainError("An API key name is required")
        return name

    @staticmethod
    def _provider_key_source(value: object) -> dict[str, str]:
        try:
            return _relay_source(value)
        except ValueError:
            raise DomainError("Provider API key source is invalid") from None

    @staticmethod
    def _provider_source_state(provider: Mapping[str, Any]) -> dict[str, str]:
        """Read the explicit URL/name source; legacy providers are custom."""

        extra = provider.get("extra")
        raw = extra.get(MENU_PROVIDER_SOURCE_KEY) if isinstance(extra, Mapping) else None
        provider_type = provider.get("provider_type")
        station_id = provider.get("relay_station_id")
        if provider_type is not None or station_id is not None:
            raw = {
                "kind": str(provider_type or "custom"),
                **({"station_id": str(station_id)} if station_id else {}),
            }
        try:
            return _provider_source(raw)
        except ValueError:
            raise DomainError("Provider source is invalid") from None

    @staticmethod
    def _set_provider_source(provider: dict[str, Any], value: object) -> dict[str, str]:
        try:
            source = _provider_source(value, required=True)
        except ValueError:
            raise DomainError("Provider source is invalid") from None
        provider["provider_type"] = source["kind"]
        provider["relay_station_id"] = source.get("station_id", "")
        extra = dict(provider.get("extra", {})) if isinstance(provider.get("extra"), Mapping) else {}
        extra[MENU_PROVIDER_SOURCE_KEY] = copy.deepcopy(source)
        provider["extra"] = extra
        return source

    @staticmethod
    def _sync_provider_identity_to_models(provider: dict[str, Any]) -> None:
        """Keep every staged model on its provider's URL and name."""

        name = str(provider.get("name", "")).strip()
        api_base = str(provider.get("api_base", "")).strip()
        models = provider.get("models")
        if not isinstance(models, list):
            return
        for model in models:
            if not isinstance(model, dict):
                continue
            model["provider"] = name
            model["api_base"] = api_base

    @staticmethod
    def _order_value(value: object, *, label: str = "Route order") -> int | float:
        try:
            return _menu_order(value, label)
        except ValueError:
            raise DomainError(f"{label} is invalid") from None

    @staticmethod
    def _new_provider_key_id() -> str:
        return f"provider-slot-{uuid.uuid4().hex}"

    @classmethod
    def _provider_api_keys(cls, provider: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw_keys = provider.get("api_keys", [])
        if raw_keys is None:
            return []
        if not isinstance(raw_keys, list):
            raise DomainError("Provider API keys are invalid")
        keys: list[dict[str, Any]] = []
        provider_name = str(provider.get("name", "")).strip()
        seen_ids: set[str] = set()
        for item in raw_keys:
            if not isinstance(item, Mapping):
                raise DomainError("Provider API keys are invalid")
            copied = copy.deepcopy(dict(item))
            key_name = cls._api_key_name(copied.get("name"))
            try:
                provider_key_id = _provider_key_id(copied.get("id"))
            except ValueError:
                raise DomainError("Provider API key ID is invalid") from None
            provider_key_id = provider_key_id or _stable_provider_key_id(
                provider_name, key_name
            )
            if provider_key_id in seen_ids:
                raise DomainError("Provider API key IDs must be unique")
            seen_ids.add(provider_key_id)
            copied["id"] = provider_key_id
            copied["name"] = key_name
            copied["source"] = cls._provider_key_source(copied.get("source"))
            keys.append(copied)
        return keys

    @classmethod
    def _sync_primary_api_key(cls, provider: dict[str, Any], keys: list[dict[str, Any]]) -> None:
        normalized = cls._provider_api_keys({**provider, "api_keys": keys})
        extra = dict(provider.get("extra", {})) if isinstance(provider.get("extra"), Mapping) else {}
        extra[MENU_RELAY_KEYS_KEY] = {
            "version": MENU_RELAY_KEYS_VERSION,
            "slots": [
                {
                    "id": item["id"],
                    "api_key_name": item["name"],
                    "source": copy.deepcopy(item["source"]),
                }
                for item in normalized
            ],
        }
        provider["extra"] = extra
        provider["api_keys"] = normalized
        first_value = normalized[0].get("value", "") if normalized else ""
        provider["api_key"] = first_value if isinstance(first_value, str) else ""

    @classmethod
    def _api_key_index(cls, keys: Sequence[Mapping[str, Any]], name: str) -> int:
        for index, item in enumerate(keys):
            if str(item.get("name", "")).strip() == name:
                return index
        raise DomainError("The selected API key is unavailable")

    @staticmethod
    def _provider_key_index(keys: Sequence[Mapping[str, Any]], provider_key_id: str) -> int:
        for index, item in enumerate(keys):
            if str(item.get("id", "")).strip() == provider_key_id:
                return index
        raise DomainError("The selected provider key is unavailable")

    def _provider_key_location(self, provider_key_id: object) -> tuple[int, int]:
        try:
            target = _provider_key_id(provider_key_id, required=True)
        except ValueError:
            raise DomainError("The selected provider key is unavailable") from None
        for provider_index, provider in enumerate(self._draft.get("providers", [])):
            if not isinstance(provider, Mapping):
                continue
            keys = self._provider_api_keys(provider)
            for key_index, item in enumerate(keys):
                if item["id"] == target:
                    return provider_index, key_index
        raise DomainError("The selected provider key is unavailable")

    def _model_provider_key(
        self,
        provider: Mapping[str, Any],
        model: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        keys = self._provider_api_keys(provider)
        provider_key_id = str(model.get("provider_key_id", "")).strip()
        if provider_key_id:
            for item in keys:
                if item["id"] == provider_key_id:
                    return item
            return None
        key_name = str(model.get("api_key_name", "")).strip()
        if key_name:
            for item in keys:
                if item["name"] == key_name:
                    return item
            return None
        return keys[0] if keys else None

    def _normalize_model_binding(
        self,
        provider: Mapping[str, Any],
        model: dict[str, Any],
    ) -> None:
        order_mode = str(model.get("order_mode", "manual")).strip() or "manual"
        if order_mode not in MODEL_ORDER_MODES:
            raise DomainError("Order mode must be manual or relay_multiplier")
        key = self._model_provider_key(provider, model)
        if key is not None:
            model["provider_key_id"] = key["id"]
            model["api_key_name"] = key["name"]
        relay_selected = (
            key is not None
            and isinstance(key.get("source"), Mapping)
            and key["source"].get("kind") == "relay"
        )
        if order_mode == "relay_multiplier" and not relay_selected:
            raise DomainError("Relay multiplier order requires a relay provider key")
        # ProviderKey.source is the single relation source of truth.  These
        # editor fields remain derived for old documents/UI readers, but they
        # are never accepted as an independent model-level binding contract.
        model["catalog_mode"] = "relay_linked" if relay_selected else "independent"
        # Legacy documents persist this compatibility field.  Derive it from
        # the model's ordinary upstream selection instead of accepting a
        # second, independently editable relay-model identity.
        model["source_model_id"] = self._wire_model_name(model) if relay_selected else ""
        model["order_mode"] = order_mode
        raw_manual_order = model.get("manual_order", model.get("order", 0))
        manual_order = self._order_value(raw_manual_order, label="Manual route order")
        model["manual_order"] = manual_order
        if order_mode == "manual":
            model["effective_order"] = manual_order
            model["order"] = manual_order
        else:
            effective = model.get("effective_order")
            if effective is None or str(effective).strip() == "":
                model["effective_order"] = None
            else:
                model["effective_order"] = self._order_value(effective)
                model["order"] = model["effective_order"]
        model["binding_health"] = {
            "status": "linked" if relay_selected else "independent"
        }

    def _normalize_provider_model_bindings(
        self,
        providers: object,
    ) -> None:
        """Derive model relation state from ProviderKeys after loading.

        Legacy documents can carry model-level relay fields.  Reading them
        remains supported, but the selected ProviderKey wins immediately so
        stale metadata cannot survive in the staged editor state.
        """

        if not isinstance(providers, list):
            return
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            models = provider.get("models", [])
            if not isinstance(models, list):
                continue
            for model in models:
                if not isinstance(model, dict):
                    continue
                key = self._model_provider_key(provider, model)
                relay_selected = (
                    key is not None
                    and isinstance(key.get("source"), Mapping)
                    and key["source"].get("kind") == "relay"
                )
                if (
                    not relay_selected
                    and str(model.get("order_mode", "manual")).strip()
                    == "relay_multiplier"
                ):
                    manual_order = self._order_value(
                        model.get("manual_order", model.get("order", 0)),
                        label="Manual route order",
                    )
                    model.update(
                        {
                            "order_mode": "manual",
                            "manual_order": manual_order,
                            "effective_order": manual_order,
                            "order": manual_order,
                        }
                    )
                self._normalize_model_binding(provider, model)

    def _dispatch_provider_key(self, action: str, data: Mapping[str, Any]) -> None:
        providers = self._draft["providers"]
        provider_index = self._provider_index(data)
        provider = self._copy_provider_for_edit(providers[provider_index])
        keys = self._provider_api_keys(provider)

        if action == "provider_key_add":
            name = self._api_key_name(data.get("name"))
            if any(str(item.get("name", "")).strip() == name for item in keys):
                raise DomainError("The API key name is already in use")
            # The value is filled only through the native secret capability.
            # Validation remains false until that secure staging step finishes.
            keys.append(
                {
                    "id": self._new_provider_key_id(),
                    "name": name,
                    "value": "",
                    "source": {"kind": "independent"},
                }
            )
        elif action == "provider_key_patch":
            old_name = self._api_key_name(data.get("old_name"))
            name = self._api_key_name(data.get("name"))
            key_index = self._api_key_index(keys, old_name)
            if self._provider_key_source(keys[key_index].get("source"))["kind"] == "relay":
                raise DomainError("Relay provider key is managed by its source")
            if name != old_name and any(
                str(item.get("name", "")).strip() == name for item in keys
            ):
                raise DomainError("The API key name is already in use")
            provider_key_id = str(keys[key_index].get("id", "")).strip()
            keys[key_index]["name"] = name
            models = provider.get("models", [])
            if isinstance(models, list):
                for model in models:
                    if isinstance(model, dict) and (
                        str(model.get("provider_key_id", "")).strip() == provider_key_id
                        or str(model.get("api_key_name", "")).strip() == old_name
                    ):
                        model["provider_key_id"] = provider_key_id
                        model["api_key_name"] = name
        elif action == "provider_key_delete":
            name = self._api_key_name(data.get("name"))
            key_index = self._api_key_index(keys, name)
            provider_key_id = str(keys[key_index].get("id", "")).strip()
            keys.pop(key_index)
            models = provider.get("models", [])
            if isinstance(models, list):
                provider_name = str(provider.get("name", "")).strip()
                remaining_models: list[Any] = []
                for model in models:
                    if isinstance(model, Mapping) and (
                        str(model.get("provider_key_id", "")).strip() == provider_key_id
                        or str(model.get("api_key_name", "")).strip() == name
                    ):
                        self._probe_overlay.get(provider_name, {}).pop(self._probe_model_key(model), None)
                    else:
                        remaining_models.append(model)
                provider["models"] = remaining_models
        else:
            raise DomainError("The requested provider action is unavailable")

        self._sync_primary_api_key(provider, keys)
        providers[provider_index] = provider

    def _secret_target(self, target: str) -> tuple[int, str | None]:
        if target.count(self._API_KEY_TARGET_SEPARATOR) > 1:
            raise DomainError("The requested secret field is unavailable")
        if self._API_KEY_TARGET_SEPARATOR not in target:
            return self._provider_index({"provider_id": target}), None
        provider_id, key_name = target.split(self._API_KEY_TARGET_SEPARATOR, 1)
        if not provider_id or not key_name:
            raise DomainError("The requested secret field is unavailable")
        index = self._provider_index({"provider_id": provider_id})
        return index, self._api_key_name(key_name)

    def _stage_provider_secret(self, provider_index: int, key_name: str | None, value: str) -> None:
        providers = self._draft["providers"]
        provider = self._copy_provider_for_edit(providers[provider_index])
        keys = self._provider_api_keys(provider)
        if key_name is None:
            if keys:
                key_index = 0
            elif value:
                keys = [{"name": "default", "value": ""}]
                key_index = 0
            else:
                self._sync_primary_api_key(provider, [])
                providers[provider_index] = provider
                return
        else:
            key_index = self._api_key_index(keys, key_name)
        if self._provider_key_source(keys[key_index].get("source"))["kind"] == "relay":
            raise DomainError("Relay provider key is managed by its source")
        keys[key_index]["value"] = value
        self._sync_primary_api_key(provider, keys)
        providers[provider_index] = provider

    def _select_provider_relay_station(self, data: Mapping[str, Any]) -> None:
        """Atomically bind a provider's URL and name to one relay station."""

        raw_source = data.get("source")
        if not isinstance(raw_source, Mapping):
            raise DomainError("Relay station is unavailable")
        station_id = str(raw_source.get("station_id", "")).strip()
        station_name = str(raw_source.get("name", "")).strip()
        station_origin = str(raw_source.get("api_base", raw_source.get("origin", ""))).strip()
        if not station_id or not station_name or not station_origin:
            raise DomainError("Relay station is unavailable")
        providers = self._draft["providers"]
        provider_index = self._provider_index(data)
        provider = self._copy_provider_for_edit(providers[provider_index])
        if any(
            index != provider_index
            and isinstance(candidate, Mapping)
            and str(candidate.get("name", "")).strip() == station_name
            for index, candidate in enumerate(providers)
        ):
            raise DomainError("A provider with this name already exists")
        provider["name"] = station_name
        provider["api_base"] = station_origin
        self._set_provider_source(provider, {"kind": "relay", "station_id": station_id})
        self._sync_provider_identity_to_models(provider)
        providers[provider_index] = provider

    def _dispatch_provider(self, action: str, data: Mapping[str, Any]) -> None:
        providers = self._draft["providers"]
        if action in {"provider_key_add", "provider_key_patch", "provider_key_delete"}:
            self._dispatch_provider_key(action, data)
            return
        if action == "provider_select_relay_station":
            self._select_provider_relay_station(data)
            return
        if action in {"provider_add", "add_provider"}:
            value = data.get("provider", data.get("value", data))
            provider = _copy_mapping(value, "provider")
            create_default_api_key = provider.pop("create_default_api_key", False) is True
            # UI-created providers may request a safe key slot before their
            # first native-secret edit. The intent marker is consumed here;
            # React never constructs or transports a credential value.
            if create_default_api_key and "api_keys" not in provider:
                provider["api_keys"] = [{"name": "default", "value": ""}]
            if "api_keys" in provider:
                self._sync_primary_api_key(provider, self._provider_api_keys(provider))
            source = self._provider_source_state(provider)
            if source["kind"] != "custom":
                raise DomainError("Select a relay station to create a relay provider")
            self._set_provider_source(provider, {"kind": "custom"})
            providers.append(provider)
            self._register_new_provider(provider)
            return
        if action in {"provider_patch", "patch_provider"}:
            index = self._provider_index(data)
            provider = self._copy_provider_for_edit(providers[index])
            previous_name = str(provider.get("name", "")).strip()
            current_source = self._provider_source_state(provider)
            changes = self._changes(data, "provider")
            if "endpoint" in changes:
                changes["api_base"] = changes.pop("endpoint")
            source_requested = "provider_type" in changes or "relay_station_id" in changes
            next_source = current_source
            if source_requested:
                next_type = str(changes.pop("provider_type", current_source["kind"])).strip() or "custom"
                if next_type == "relay":
                    raise DomainError("Select a relay station to set a relay provider")
                next_station_id = str(changes.pop("relay_station_id", "")).strip()
                next_source = self._set_provider_source(
                    provider,
                    {
                        "kind": next_type,
                        **({"station_id": next_station_id} if next_station_id else {}),
                    },
                )
            elif current_source["kind"] == "relay" and any(key in changes for key in ("name", "api_base")):
                raise DomainError("Relay provider URL and name are set by its station")
            if next_source["kind"] == "relay" and any(key in changes for key in ("name", "api_base")):
                raise DomainError("Relay provider URL and name are set by its station")
            if "api_key" in changes:
                api_key = changes.pop("api_key")
                keys = list(provider.get("api_keys", [])) if isinstance(provider.get("api_keys"), list) else []
                if api_key:
                    if keys and isinstance(keys[0], Mapping):
                        first = dict(keys[0])
                        first["value"] = api_key
                        keys[0] = first
                    else:
                        keys = [{"name": "default", "value": api_key}]
                else:
                    keys = []
                provider["api_keys"] = keys
                provider["api_key"] = api_key
            provider.update(changes)
            if {"name", "api_base"}.intersection(changes):
                self._sync_provider_identity_to_models(provider)
            # ``extra`` is user-editable metadata.  Always write the
            # authoritative source state last so a single patch cannot leave
            # its top-level source fields and persisted metadata disagreeing.
            self._set_provider_source(provider, next_source)
            current_name = str(provider.get("name", "")).strip()
            if current_name != previous_name:
                if previous_name in self._probe_overlay:
                    self._probe_overlay[current_name] = self._probe_overlay.pop(previous_name)
            if "api_keys" in changes:
                normalized_keys = self._provider_api_keys(provider)
                self._sync_primary_api_key(provider, normalized_keys)
                keys_by_name = {item["name"]: item for item in normalized_keys}
                models = provider.get("models", [])
                if isinstance(models, list):
                    for model in models:
                        if not isinstance(model, dict):
                            continue
                        key = keys_by_name.get(str(model.get("api_key_name", "")).strip())
                        if key is not None:
                            model["provider_key_id"] = key["id"]
            providers[index] = provider
            return
        if action in {"provider_clear_key", "clear_provider_key"}:
            index = self._provider_index(data)
            provider = self._copy_provider_for_edit(providers[index])
            self._sync_primary_api_key(provider, [])
            providers[index] = provider
            return
        if action in {"provider_delete", "delete_provider"}:
            removed = providers.pop(self._provider_index(data))
            if isinstance(removed, Mapping):
                name = str(removed.get("name", "")).strip()
                self._probe_overlay.pop(name, None)
            return
        if action in {"provider_move", "move_provider"}:
            source = data.get("from", data.get("source"))
            if type(source) is not int:
                source = self._provider_index(data)
            _move(providers, source, _direction_destination(source, len(providers), data), "provider")
            return
        raise DomainError("The requested provider action is unavailable")

    def _new_model(
        self,
        provider: Mapping[str, Any],
        value: object,
        used_deployment_ids: set[str],
    ) -> dict[str, Any]:
        model = _copy_mapping(value, "model")
        if "name" in model and "model_name" not in model:
            model["model_name"] = model.pop("name")
        if "upstream_model" in model and "litellm_model" not in model:
            model["litellm_model"] = model.pop("upstream_model")
        if "model_enabled" in model:
            model["enabled"] = model["model_enabled"]
        elif "enabled" in model:
            model["model_enabled"] = model["enabled"]
        if "order" not in model:
            model["order"] = 0
        if "catalog_mode" not in model:
            model["catalog_mode"] = "independent"
        if "order_mode" not in model:
            model["order_mode"] = "manual"
        if not str(model.get("upstream_url_surface", "")).strip():
            model["upstream_url_surface"] = self._default_upstream_surface(provider, model)
        if not str(model.get("upstream_protocol_mode", "")).strip():
            model["upstream_protocol_mode"] = "fallback"
        if "litellm_model" in model:
            model["litellm_model"] = self._canonical_upstream_model(model["litellm_model"], model)
        self._normalize_model_binding(provider, model)
        deployment_id = str(model.get("deployment_id", "")).strip().lower()
        if not deployment_id:
            deployment_id = uuid.uuid4().hex[:8]
            while deployment_id in used_deployment_ids:
                deployment_id = uuid.uuid4().hex[:8]
            model["deployment_id"] = deployment_id
        used_deployment_ids.add(deployment_id)
        self._editor_id(model, model=True)
        return model

    @staticmethod
    def _unique_provider_key_name(keys: Sequence[Mapping[str, Any]], preferred: str) -> str:
        base = preferred.strip() or "independent"
        used = {str(item.get("name", "")).strip() for item in keys}
        if base not in used:
            return base
        suffix = 2
        while f"{base}-{suffix}" in used:
            suffix += 1
        return f"{base}-{suffix}"

    def _stage_provider_relay_key_import(
        self,
        data: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Create or reuse one relay-sourced ProviderKey in a target provider.

        The staged slot intentionally has no credential or API base.  Core
        resolves both from the stable relay source during coordinated Apply.
        """

        providers = self._draft["providers"]
        provider_index = self._provider_index(data)
        provider = self._copy_provider_for_edit(providers[provider_index])
        relay_source = self._relay_source_filter(data)
        keys = self._provider_api_keys(provider)
        existing = next(
            (
                key
                for key in keys
                if key["source"].get("kind") == "relay"
                and self._same_relay_source(key["source"], relay_source)
            ),
            None,
        )
        imported = existing is None
        if existing is None:
            preferred_name = self._api_key_name(data.get("api_key_name"))
            existing = {
                "id": self._new_provider_key_id(),
                "name": self._unique_provider_key_name(keys, preferred_name),
                "value": "",
                "source": relay_source,
            }
            keys.append(existing)
            self._sync_primary_api_key(provider, keys)
            providers[provider_index] = provider
        provider_id = str(provider.get("name", "")).strip() or self._editor_id(provider)
        return {
            "operation": "provider_relay_key_import",
            "provider_id": provider_id,
            "slot_id": existing["id"],
            "api_key_name": existing["name"],
            "imported": imported,
            "reused": not imported,
            "source": copy.deepcopy(relay_source),
        }

    def _select_model_relay_resource(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """Select a relay key discovered automatically from the provider Base URL."""

        raw_source = data.get("source")
        if not isinstance(raw_source, Mapping):
            raise DomainError("Relay API key is unavailable")
        provider_index = self._provider_index(data)
        provider = self._draft["providers"][provider_index]
        if not isinstance(provider, Mapping):
            raise DomainError("The selected provider is unavailable")
        provider_root = service_root(provider.get("api_base"))
        relay_root = service_root(raw_source.get("api_base"))
        if not provider_root or provider_root != relay_root:
            raise DomainError("Relay API key does not match the provider Base URL")

        imported = self._stage_provider_relay_key_import(
            {
                "provider_id": data.get("provider_id"),
                "source": raw_source,
                "api_key_name": raw_source.get("api_key_name", raw_source.get("name")),
            }
        )
        self._dispatch_model(
            "model_patch",
            {
                "provider_id": data.get("provider_id"),
                "model_id": data.get("model_id"),
                "changes": {
                    "provider_key_id": imported["slot_id"],
                    "api_key_name": imported["api_key_name"],
                },
            },
        )
        return {
            **imported,
            "operation": "model_relay_key_selected",
            "model_id": str(data.get("model_id", "")),
        }

    def _link_or_rebind_model(self, data: Mapping[str, Any]) -> None:
        """Legacy relay action alias for selecting a local relay ProviderKey.

        A model no longer owns a relay source.  It stays in its provider and
        selects one of that provider's stable key slots; the slot owns the
        relay triple and is materialized during Apply.
        """

        providers = self._draft["providers"]
        provider_index = self._provider_index(data)
        provider = self._copy_provider_for_edit(providers[provider_index])
        models = provider.get("models", [])
        if not isinstance(models, list):
            raise DomainError("The selected model is unavailable")
        model_index = self._model_index(provider, data)
        model = self._copy_model_for_edit(models[model_index])
        keys = self._provider_api_keys(provider)
        try:
            target_key_id = _provider_key_id(
                data.get("provider_key_id"), required=True
            )
        except ValueError:
            raise DomainError("The selected provider key is unavailable") from None
        target_key = keys[self._provider_key_index(keys, target_key_id)]
        if target_key["source"].get("kind") != "relay":
            raise DomainError("The selected provider key is not linked to a relay")
        model.update(
            {
                "api_key_name": target_key["name"],
                "provider_key_id": target_key["id"],
            }
        )
        self._normalize_model_binding(provider, model)
        models[model_index] = model
        providers[provider_index] = provider

    def _detach_model_from_relay(self, data: Mapping[str, Any]) -> None:
        providers = self._draft["providers"]
        provider_index = self._provider_index(data)
        provider = self._copy_provider_for_edit(providers[provider_index])
        models = provider.get("models", [])
        if not isinstance(models, list):
            raise DomainError("The selected model is unavailable")
        model_index = self._model_index(provider, data)
        model = self._copy_model_for_edit(models[model_index])
        key = self._model_provider_key(provider, model)
        if key is None:
            raise DomainError("The linked provider key is unavailable")
        keys = self._provider_api_keys(provider)
        if key["source"].get("kind") == "relay":
            key_value = key.get("value")
            if not isinstance(key_value, str) or not key_value.strip():
                raise DomainError(
                    "The linked provider key has no materialized credential"
                )
            preferred_name = (
                str(data.get("api_key_name", "")).strip()
                or f"{key['name']}-independent"
            )
            detached_key = {
                "id": self._new_provider_key_id(),
                "name": self._unique_provider_key_name(keys, preferred_name),
                "value": key_value,
                "source": {"kind": "independent"},
            }
            keys.append(detached_key)
            self._sync_primary_api_key(provider, keys)
        else:
            detached_key = key
        manual_order = self._order_value(
            model.get("effective_order", model.get("order", 0)),
            label="Manual route order",
        )
        model.update(
            {
                "api_key_name": detached_key["name"],
                "provider_key_id": detached_key["id"],
                "catalog_mode": "independent",
                "source_model_id": "",
                "order_mode": "manual",
                "manual_order": manual_order,
                "effective_order": manual_order,
                "order": manual_order,
                "binding_health": {"status": "independent"},
            }
        )
        models[model_index] = model
        providers[provider_index] = provider

    def link_model_to_relay_key(
        self,
        provider_id: str,
        model_id: str,
        provider_key_id: str,
        **options: Any,
    ) -> dict[str, Any]:
        return self.dispatch(
            "model.link_relay_key",
            {
                "provider_id": provider_id,
                "model_id": model_id,
                "provider_key_id": provider_key_id,
                **options,
            },
        )

    def rebind_model_to_relay_key(
        self,
        provider_id: str,
        model_id: str,
        provider_key_id: str,
        **options: Any,
    ) -> dict[str, Any]:
        return self.dispatch(
            "model.rebind_relay_key",
            {
                "provider_id": provider_id,
                "model_id": model_id,
                "provider_key_id": provider_key_id,
                **options,
            },
        )

    def detach_model_from_relay_key(
        self,
        provider_id: str,
        model_id: str,
        *,
        api_key_name: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider_id": provider_id,
            "model_id": model_id,
        }
        if api_key_name is not None:
            payload["api_key_name"] = api_key_name
        return self.dispatch("model.detach_relay_key", payload)

    @staticmethod
    def _relay_import_models(source: Mapping[str, Any]) -> list[str]:
        raw_models = source.get("source_models", source.get("models", []))
        if not isinstance(raw_models, Sequence) or isinstance(
            raw_models, (str, bytes, bytearray)
        ):
            raise DomainError("Relay model catalog is invalid")
        models: list[str] = []
        seen: set[str] = set()
        for raw_model in raw_models:
            value = (
                raw_model.get("id", raw_model.get("name", raw_model.get("model")))
                if isinstance(raw_model, Mapping)
                else raw_model
            )
            model = str(value).strip() if isinstance(value, str) else ""
            if (
                not model
                or len(model.encode("utf-8")) > 256
                or any(char in model for char in "\x00\r\n")
            ):
                raise DomainError("Relay model catalog is invalid")
            if model not in seen:
                seen.add(model)
                models.append(model)
        return models

    def _stage_relay_import(
        self,
        sources: object,
        *,
        import_mode: object = "linked",
    ) -> dict[str, Any]:
        mode = str(import_mode).strip().lower()
        if mode not in {"linked", "independent"}:
            raise DomainError("Relay import mode is invalid")
        if not isinstance(sources, Sequence) or isinstance(
            sources, (str, bytes, bytearray)
        ) or not sources:
            raise DomainError("Select at least one relay API resource")
        providers = self._draft.get("providers")
        if not isinstance(providers, list):
            raise DomainError("Provider/model configuration is invalid")
        imported_models = 0
        imported_keys = 0
        updated_models = 0
        for raw_source in sources:
            if not isinstance(raw_source, Mapping):
                raise DomainError("Relay import source is invalid")
            relay_source = self._relay_source_filter(raw_source)
            provider_name = str(raw_source.get("provider_name", "")).strip()
            if not provider_name:
                provider_name = f"relay-{relay_source['station_id']}"
            if (
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", provider_name)
                or any(char in provider_name for char in "\x00\r\n")
            ):
                raise DomainError("Relay provider name is invalid")
            provider_index = next(
                (
                    index
                    for index, provider in enumerate(providers)
                    if isinstance(provider, Mapping)
                    and str(provider.get("name", "")).strip() == provider_name
                ),
                None,
            )
            if provider_index is None:
                provider: dict[str, Any] = {
                    "name": provider_name,
                    "enabled": True,
                    "api_base": str(raw_source.get("api_base", "")).strip(),
                    "api_key": "",
                    "api_keys": [],
                    "models": [],
                    "extra": {},
                }
                providers.append(provider)
                provider_index = len(providers) - 1
                self._register_new_provider(provider)
            else:
                provider = self._copy_provider_for_edit(providers[provider_index])
            api_base = str(raw_source.get("api_base", "")).strip()
            if api_base:
                provider["api_base"] = api_base
            keys = self._provider_api_keys(provider)
            key = next(
                (
                    item
                    for item in keys
                    if item["source"].get("kind") == "relay"
                    and self._same_relay_source(item["source"], relay_source)
                ),
                None,
            ) if mode == "linked" else None
            preferred_name = str(
                raw_source.get("api_key_name", raw_source.get("name", "relay-key"))
            ).strip() or "relay-key"
            if key is None:
                key_name = self._unique_provider_key_name(keys, preferred_name)
                key = {
                    "id": self._new_provider_key_id(),
                    "name": key_name,
                    "value": str(raw_source.get("api_key", "")),
                    "source": relay_source
                    if mode == "linked"
                    else {"kind": "independent"},
                }
                keys.append(key)
                imported_keys += 1
            elif isinstance(raw_source.get("api_key"), str) and str(
                raw_source.get("api_key", "")
            ):
                key["value"] = str(raw_source["api_key"])
            self._sync_primary_api_key(provider, keys)

            models = provider.get("models")
            if not isinstance(models, list):
                models = []
                provider["models"] = models
            order_mode = str(raw_source.get("order_mode", "manual")).strip() or "manual"
            if order_mode not in MODEL_ORDER_MODES:
                raise DomainError("Order mode must be manual or relay_multiplier")
            manual_order = self._order_value(
                raw_source.get("manual_order", 0), label="Manual route order"
            )
            if order_mode == "relay_multiplier":
                multiplier = raw_source.get("multiplier")
                effective_order = (
                    self._order_value(multiplier)
                    if multiplier is not None and str(multiplier).strip()
                    else None
                )
            else:
                effective_order = manual_order
            for source_model in self._relay_import_models(raw_source):
                existing = next(
                    (
                        model
                        for model in models
                        if isinstance(model, Mapping)
                        and str(model.get("provider_key_id", "")).strip() == key["id"]
                        and self._wire_model_name(model) == source_model
                        and str(model.get("model_name", "")).strip() == source_model
                    ),
                    None,
                )
                if existing is None:
                    model = self._new_model(
                        provider,
                        {
                            "model_name": source_model,
                            "litellm_model": f"openai/{source_model}",
                            "provider": provider_name,
                            "api_base": "",
                            "api_key": "",
                            "api_key_name": key["name"],
                            "provider_key_id": key["id"],
                            "order_mode": order_mode if mode == "linked" else "manual",
                            "manual_order": manual_order,
                            "effective_order": effective_order,
                            "order": effective_order
                            if effective_order is not None
                            else manual_order,
                            "enabled": True,
                            "model_enabled": True,
                            "upstream_url_surface": "openai/responses",
                        },
                        {
                            str(candidate.get("deployment_id", "")).strip()
                            for candidate_provider in providers
                            if isinstance(candidate_provider, Mapping)
                            for candidate in candidate_provider.get("models", [])
                            if isinstance(candidate, Mapping)
                            and str(candidate.get("deployment_id", "")).strip()
                        },
                    )
                    models.append(model)
                    imported_models += 1
                else:
                    existing.update(
                        {
                            "api_key_name": key["name"],
                            "provider_key_id": key["id"],
                            "order_mode": order_mode,
                            "manual_order": manual_order,
                            "effective_order": effective_order,
                            "order": effective_order
                            if effective_order is not None
                            else existing.get("order", manual_order),
                        }
                    )
                    self._normalize_model_binding(provider, existing)
                    updated_models += 1
            providers[provider_index] = provider
        return {
            "operation": "relay_import",
            "import_mode": mode,
            "resource_count": len(sources),
            "provider_key_count": imported_keys,
            "model_count": imported_models,
            "updated_model_count": updated_models,
        }

    def stage_relay_import(
        self,
        sources: object,
        *,
        import_mode: object = "linked",
    ) -> dict[str, Any]:
        summary = self._stage_relay_import(sources, import_mode=import_mode)
        self._last_operation = summary
        self.revision += 1
        result = self.snapshot()
        result["operation_summary"] = copy.deepcopy(summary)
        return result

    @staticmethod
    def _safe_relay_material_issue(value: object) -> dict[str, str] | None:
        if not isinstance(value, Mapping):
            return None
        result: dict[str, str] = {}
        for key in ("code", "station_id", "account_id", "resource_id"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                result[key] = candidate.strip()
        return result if result.get("code") else None

    def _relay_materials(self, relay: object) -> dict[str, Any]:
        if isinstance(relay, Mapping):
            return dict(relay)
        resolver = getattr(relay, "binding_materials", None)
        if not callable(resolver):
            resolver = getattr(relay, "resolve_bindings", None)
        if not callable(resolver):
            raise DomainError("Relay binding material is unavailable")
        try:
            resolved = resolver()
        except Exception:
            raise DomainError("Relay binding material is unavailable") from None
        if not isinstance(resolved, Mapping):
            raise DomainError("Relay binding material is unavailable")
        return dict(resolved)

    def materialize_relay_bindings(self, relay: object) -> dict[str, Any]:
        """Resolve private relay materials into the staged LiteLLM document.

        The input is intentionally Core-only and may contain API key values.
        This method never returns those values; it only updates the private
        provider draft and reports stable source/model identifiers.
        """

        payload = self._relay_materials(relay)
        raw_resources = payload.get("resources", [])
        if not isinstance(raw_resources, Sequence) or isinstance(
            raw_resources, (str, bytes, bytearray)
        ):
            raise DomainError("Relay binding material is invalid")
        materials: dict[tuple[str, str, str], dict[str, Any]] = {}
        issues: list[dict[str, Any]] = []
        for raw_resource in raw_resources:
            if not isinstance(raw_resource, Mapping):
                raise DomainError("Relay binding material is invalid")
            source = self._relay_source_filter(raw_resource)
            resource_key = (
                source["station_id"],
                source["account_id"],
                source["resource_id"],
            )
            if resource_key in materials:
                issues.append(
                    {"code": "duplicate_resource", "source": source}
                )
                continue
            materials[resource_key] = dict(raw_resource)
        for raw_issue in payload.get("issues", []):
            safe_issue = self._safe_relay_material_issue(raw_issue)
            if safe_issue is not None:
                issues.append(safe_issue)

        affected_models: list[dict[str, str]] = []
        materialized_keys = 0
        materialized_models = 0
        providers = self._draft.get("providers", [])
        if not isinstance(providers, list):
            raise DomainError("Provider/model configuration is invalid")
        for provider_index, raw_provider in enumerate(providers):
            if not isinstance(raw_provider, Mapping):
                continue
            provider = self._copy_provider_for_edit(raw_provider)
            keys = self._provider_api_keys(provider)
            models = provider.get("models")
            if not isinstance(models, list):
                models = []
                provider["models"] = models
            provider_base: str | None = None
            for key in keys:
                source = key["source"]
                if source.get("kind") != "relay":
                    continue
                resource_key = (
                    source["station_id"],
                    source["account_id"],
                    source["resource_id"],
                )
                resource = materials.get(resource_key)
                linked_models = [
                    model
                    for model in models
                    if isinstance(model, dict)
                    and str(model.get("provider_key_id", "")).strip() == key["id"]
                ]
                if resource is None:
                    issue = {
                        "code": "resource_missing",
                        "provider_key_id": key["id"],
                        "source": copy.deepcopy(source),
                    }
                    issues.append(issue)
                    for model in linked_models:
                        model["binding_health"] = {
                            "status": "resource_missing",
                            "detail": "The linked relay API key is unavailable",
                        }
                    continue
                if resource.get("enabled") is not True:
                    issues.append(
                        {
                            "code": "resource_disabled",
                            "provider_key_id": key["id"],
                            "source": copy.deepcopy(source),
                        }
                    )
                    for model in linked_models:
                        model["binding_health"] = {
                            "status": "resource_disabled",
                            "detail": "The linked relay API key is disabled",
                        }
                    continue
                credential = resource.get("api_key")
                if not isinstance(credential, str) or not credential.strip():
                    issues.append(
                        {
                            "code": "credential_missing",
                            "provider_key_id": key["id"],
                            "source": copy.deepcopy(source),
                        }
                    )
                    continue
                api_base = resource.get("api_base")
                if not isinstance(api_base, str) or not api_base.strip():
                    issues.append(
                        {
                            "code": "api_base_missing",
                            "provider_key_id": key["id"],
                            "source": copy.deepcopy(source),
                        }
                    )
                    continue
                api_base = api_base.strip()
                if provider_base is not None and provider_base != api_base:
                    issues.append(
                        {
                            "code": "provider_api_base_conflict",
                            "provider_key_id": key["id"],
                            "source": copy.deepcopy(source),
                        }
                    )
                    continue
                provider_base = api_base
                key["value"] = credential
                materialized_keys += 1
                catalog = set(self._relay_import_models(resource))
                multiplier_raw = resource.get("multiplier")
                multiplier: int | float | None
                try:
                    multiplier = (
                        self._order_value(multiplier_raw, label="Relay multiplier")
                        if multiplier_raw is not None and str(multiplier_raw).strip()
                        else None
                    )
                except DomainError:
                    multiplier = None
                for model in linked_models:
                    model["api_key_name"] = key["name"]
                    model["api_key"] = ""
                    model["provider_key_id"] = key["id"]
                    model["catalog_mode"] = "relay_linked"
                    model_id = str(model.get("deployment_id", "")).strip() or self._editor_id(
                        model, model=True
                    )
                    source_model_id = self._wire_model_name(model)
                    model["source_model_id"] = source_model_id
                    if source_model_id not in catalog:
                        issues.append(
                            {
                                "code": "catalog_model_missing",
                                "provider_key_id": key["id"],
                                "model_id": model_id,
                                "source": copy.deepcopy(source),
                            }
                        )
                        model["binding_health"] = {
                            "status": "catalog_model_missing",
                            "detail": "The selected source model is absent from the relay catalog",
                        }
                        continue
                    order_mode = str(model.get("order_mode", "manual")).strip() or "manual"
                    if order_mode == "relay_multiplier":
                        if multiplier is None:
                            issues.append(
                                {
                                    "code": "multiplier_missing",
                                    "provider_key_id": key["id"],
                                    "model_id": model_id,
                                    "source": copy.deepcopy(source),
                                }
                            )
                            model["binding_health"] = {
                                "status": "multiplier_missing",
                                "detail": "The linked relay group has no usable multiplier",
                            }
                            continue
                        model["effective_order"] = multiplier
                        model["order"] = multiplier
                    else:
                        manual_order = self._order_value(
                            model.get("manual_order", model.get("order", 0)),
                            label="Manual route order",
                        )
                        model["manual_order"] = manual_order
                        model["effective_order"] = manual_order
                        model["order"] = manual_order
                    model["binding_health"] = {"status": "linked"}
                    materialized_models += 1
                    affected_models.append(
                        {
                            "provider_key_id": key["id"],
                            "model_id": model_id,
                            "upstream_model": source_model_id,
                        }
                    )
            if provider_base is not None:
                provider["api_base"] = provider_base
            self._sync_primary_api_key(provider, keys)
            providers[provider_index] = provider
        return {
            "materialized": materialized_models,
            "materialized_provider_keys": materialized_keys,
            "affected_models": affected_models,
            "issues": issues,
        }

    @staticmethod
    def _relay_source_filter(value: object) -> dict[str, str]:
        if isinstance(value, Mapping) and isinstance(value.get("source"), Mapping):
            value = value["source"]
        if not isinstance(value, Mapping):
            raise DomainError("Relay dependency source is invalid")
        candidate = {
            "kind": value.get("kind", "relay"),
            "station_id": value.get("station_id"),
            "account_id": value.get("account_id"),
            "resource_id": value.get("resource_id"),
        }
        try:
            source = _relay_source(candidate, required=True)
        except ValueError:
            raise DomainError("Relay dependency source is invalid") from None
        if source["kind"] != "relay":
            raise DomainError("Relay dependency source is invalid")
        return source

    @staticmethod
    def _same_relay_source(left: Mapping[str, Any], right: Mapping[str, str]) -> bool:
        return all(
            str(left.get(key, "")).strip() == right[key]
            for key in ("station_id", "account_id", "resource_id")
        )

    def dependency_summary(self, source: object | None = None) -> dict[str, Any]:
        """Describe relay-bound dependencies without returning credentials."""

        source_filter: dict[str, str] | None = None
        provider_key_filter = ""
        if source is not None:
            if isinstance(source, str):
                provider_key_filter = source.strip()
            elif isinstance(source, Mapping) and source.get("provider_key_id"):
                provider_key_filter = str(source.get("provider_key_id", "")).strip()
            else:
                source_filter = self._relay_source_filter(source)

        providers = self._draft.get("providers", [])
        matched_keys: list[dict[str, Any]] = []
        matched_ids: set[str] = set()
        for provider_index, provider in enumerate(providers):
            if not isinstance(provider, Mapping):
                continue
            provider_id = str(provider.get("name", "")).strip() or self._editor_id(provider)
            for key in self._provider_api_keys(provider):
                key_source = key["source"]
                if key_source.get("kind") != "relay":
                    continue
                if provider_key_filter and key["id"] != provider_key_filter:
                    continue
                if source_filter is not None and not self._same_relay_source(
                    key_source, source_filter
                ):
                    continue
                matched_ids.add(key["id"])
                matched_keys.append(
                    {
                        "provider_id": provider_id,
                        "provider_key_id": key["id"],
                        "api_key_name": key["name"],
                        "source": copy.deepcopy(key_source),
                        "model_count": 0,
                    }
                )

        models: list[dict[str, Any]] = []
        key_summary = {item["provider_key_id"]: item for item in matched_keys}
        for provider in providers:
            if not isinstance(provider, Mapping):
                continue
            provider_id = str(provider.get("name", "")).strip() or self._editor_id(provider)
            raw_models = provider.get("models", [])
            if not isinstance(raw_models, list):
                continue
            for model in raw_models:
                if not isinstance(model, Mapping):
                    continue
                provider_key_id = str(model.get("provider_key_id", "")).strip()
                if provider_key_id not in matched_ids:
                    continue
                key_summary[provider_key_id]["model_count"] += 1
                models.append(
                    {
                        "provider_id": provider_id,
                        "model_id": str(model.get("deployment_id", "")).strip()
                        or self._editor_id(model, model=True),
                        "model_name": str(model.get("model_name", "")),
                        "provider_key_id": provider_key_id,
                        "catalog_mode": "relay_linked",
                        "order_mode": str(model.get("order_mode", "manual")),
                    }
                )
        return {
            "provider_key_count": len(matched_keys),
            "model_count": len(models),
            "provider_keys": matched_keys,
            "models": models,
        }

    def _policy_target_key_ids(self, resources: object) -> set[str]:
        if isinstance(resources, Mapping):
            values = resources.get("resources", [resources])
        else:
            values = resources
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
            raise DomainError("Relay dependency resources are invalid")
        source_filters = [self._relay_source_filter(value) for value in values]
        matched: set[str] = set()
        for provider in self._draft.get("providers", []):
            if not isinstance(provider, Mapping):
                continue
            for key in self._provider_api_keys(provider):
                if key["source"].get("kind") != "relay":
                    continue
                if any(
                    self._same_relay_source(key["source"], source)
                    for source in source_filters
                ):
                    matched.add(key["id"])
        return matched

    @staticmethod
    def _rebind_target_id(
        rebind: object,
        old_key: Mapping[str, Any],
    ) -> str:
        if isinstance(rebind, str):
            return rebind.strip()
        if not isinstance(rebind, Mapping):
            return ""
        direct = rebind.get(old_key["id"])
        if direct is None:
            direct = rebind.get(str(old_key["source"].get("resource_id", "")))
        if direct is None:
            direct = rebind.get("provider_key_id")
        return str(direct).strip() if isinstance(direct, str) else ""

    def _apply_relay_dependency_policy(
        self,
        resources: object,
        policy: object,
        rebind: object = None,
    ) -> dict[str, Any]:
        policy_name = str(policy).strip().lower()
        if policy_name not in {"delete", "detach", "detach_disabled", "rebind"}:
            raise DomainError("Relay dependency policy is invalid")
        target_ids = self._policy_target_key_ids(resources)
        before = self.dependency_summary()
        target_key_summaries = [
            item for item in before["provider_keys"] if item["provider_key_id"] in target_ids
        ]
        affected_models = [
            item for item in before["models"] if item["provider_key_id"] in target_ids
        ]
        result: dict[str, Any] = {
            "policy": policy_name,
            "provider_key_count": len(target_key_summaries),
            "affected_models": affected_models,
            "deleted_models": 0,
            "detached_models": 0,
            "disabled_detached_models": 0,
            "rebound_models": 0,
            "issues": [],
        }
        if not target_ids:
            return result

        original = self._draft.get("providers", [])
        if not isinstance(original, list):
            raise DomainError("Provider/model configuration is invalid")
        working = copy.deepcopy(original)
        key_locations: dict[str, tuple[int, dict[str, Any]]] = {}
        for provider_index, provider in enumerate(working):
            if not isinstance(provider, Mapping):
                continue
            for key in self._provider_api_keys(provider):
                if key["id"] in target_ids:
                    key_locations[key["id"]] = (provider_index, key)

        if policy_name == "detach":
            for key_id, (_provider_index, key) in key_locations.items():
                value = key.get("value")
                if not isinstance(value, str) or not value.strip():
                    result["issues"].append(
                        {
                            "code": "missing_materialized_key",
                            "provider_key_id": key_id,
                            "source": copy.deepcopy(key["source"]),
                        }
                    )
        target_rebinds: dict[str, tuple[int, dict[str, Any]]] = {}
        if policy_name == "rebind":
            for key_id, (_provider_index, key) in key_locations.items():
                target_id = self._rebind_target_id(rebind, key)
                if not target_id or target_id in target_ids:
                    result["issues"].append(
                        {
                            "code": "missing_rebind_target",
                            "provider_key_id": key_id,
                            "source": copy.deepcopy(key["source"]),
                        }
                    )
                    continue
                try:
                    target_provider_index, target_key_index = self._provider_key_location(
                        target_id
                    )
                    target_provider = working[target_provider_index]
                    target_key = self._provider_api_keys(target_provider)[target_key_index]
                except DomainError:
                    result["issues"].append(
                        {
                            "code": "missing_rebind_target",
                            "provider_key_id": key_id,
                            "source": copy.deepcopy(key["source"]),
                        }
                    )
                    continue
                if target_key["source"].get("kind") != "relay" or not str(
                    target_key.get("value", "")
                ).strip():
                    result["issues"].append(
                        {
                            "code": "invalid_rebind_target",
                            "provider_key_id": key_id,
                            "source": copy.deepcopy(key["source"]),
                        }
                    )
                    continue
                target_rebinds[key_id] = (target_provider_index, target_key)
        if result["issues"]:
            return result

        if policy_name == "delete":
            for provider in working:
                if not isinstance(provider, dict) or not isinstance(provider.get("models"), list):
                    continue
                before_count = len(provider["models"])
                provider["models"] = [
                    model
                    for model in provider["models"]
                    if not isinstance(model, Mapping)
                    or str(model.get("provider_key_id", "")).strip() not in target_ids
                ]
                result["deleted_models"] += before_count - len(provider["models"])
        elif policy_name == "detach":
            for key_id, (provider_index, key) in key_locations.items():
                provider = working[provider_index]
                if not isinstance(provider, dict):
                    continue
                keys = self._provider_api_keys(provider)
                detached_key = {
                    "id": self._new_provider_key_id(),
                    "name": self._unique_provider_key_name(
                        keys, f"{key['name']}-independent"
                    ),
                    "value": key["value"],
                    "source": {"kind": "independent"},
                }
                keys.append(detached_key)
                self._sync_primary_api_key(provider, keys)
                models = provider.get("models", [])
                if not isinstance(models, list):
                    continue
                for model in models:
                    if not isinstance(model, dict) or str(
                        model.get("provider_key_id", "")
                    ).strip() != key_id:
                        continue
                    order = self._order_value(
                        model.get("effective_order", model.get("order", 0)),
                        label="Manual route order",
                    )
                    model.update(
                        {
                            "api_key_name": detached_key["name"],
                            "provider_key_id": detached_key["id"],
                            "catalog_mode": "independent",
                            "source_model_id": "",
                            "order_mode": "manual",
                            "manual_order": order,
                            "effective_order": order,
                            "order": order,
                            "binding_health": {"status": "independent"},
                        }
                    )
                    result["detached_models"] += 1
        elif policy_name == "detach_disabled":
            for provider in working:
                if not isinstance(provider, dict):
                    continue
                models = provider.get("models", [])
                if not isinstance(models, list):
                    continue
                for model in models:
                    if not isinstance(model, dict) or str(
                        model.get("provider_key_id", "")
                    ).strip() not in target_ids:
                        continue
                    order = self._order_value(
                        model.get("effective_order", model.get("order", 0)),
                        label="Manual route order",
                    )
                    model.update(
                        {
                            "api_key": "",
                            "api_key_name": "",
                            "provider_key_id": "",
                            "catalog_mode": "independent",
                            "source_model_id": "",
                            "order_mode": "manual",
                            "manual_order": order,
                            "effective_order": order,
                            "order": order,
                            "enabled": False,
                            "model_enabled": False,
                            "binding_health": {
                                "status": "credential_required",
                                "detail": "A replacement API key is required before this model can be enabled",
                            },
                        }
                    )
                    result["disabled_detached_models"] += 1
        else:
            moves: list[tuple[int, dict[str, Any], int, dict[str, Any]]] = []
            for provider_index, provider in enumerate(working):
                if not isinstance(provider, Mapping):
                    continue
                models = provider.get("models", [])
                if not isinstance(models, list):
                    continue
                for model in models:
                    if not isinstance(model, Mapping):
                        continue
                    old_key_id = str(model.get("provider_key_id", "")).strip()
                    target = target_rebinds.get(old_key_id)
                    if target is not None:
                        moves.append((provider_index, dict(model), target[0], target[1]))
            for provider in working:
                if isinstance(provider, dict) and isinstance(provider.get("models"), list):
                    provider["models"] = [
                        model
                        for model in provider["models"]
                        if not isinstance(model, Mapping)
                        or str(model.get("provider_key_id", "")).strip() not in target_ids
                    ]
            for _source_index, model, target_provider_index, target_key in moves:
                target_provider = working[target_provider_index]
                if not isinstance(target_provider, dict):
                    continue
                models = target_provider.get("models")
                if not isinstance(models, list):
                    models = []
                    target_provider["models"] = models
                order_mode = str(model.get("order_mode", "manual")).strip() or "manual"
                if order_mode == "relay_multiplier":
                    # A rebind keeps the mode but requires the coordinator to
                    # materialize the target multiplier before Apply.
                    model["effective_order"] = None
                model.update(
                    {
                        "provider": str(target_provider.get("name", "")).strip(),
                        "api_base": "",
                        "api_key": "",
                        "api_key_name": target_key["name"],
                        "provider_key_id": target_key["id"],
                        "catalog_mode": "relay_linked",
                        "binding_health": {"status": "linked"},
                    }
                )
                models.append(model)
                result["rebound_models"] += 1

        for provider in working:
            if not isinstance(provider, dict):
                continue
            keys = [
                key
                for key in self._provider_api_keys(provider)
                if key["id"] not in target_ids
            ]
            self._sync_primary_api_key(provider, keys)
        working = [
            provider
            for provider in working
            if not isinstance(provider, Mapping)
            or self._provider_api_keys(provider)
            or bool(provider.get("models"))
        ]
        provider_bindings, model_bindings = self._editor_id_bindings()
        self._draft["providers"] = working
        self._restore_editor_id_bindings(provider_bindings, model_bindings)
        self._probe_overlay.clear()
        return result

    def apply_relay_dependency_policy(
        self,
        resources: object,
        policy: object,
        rebind: object = None,
    ) -> dict[str, Any]:
        result = self._apply_relay_dependency_policy(resources, policy, rebind)
        if not result["issues"]:
            self.revision += 1
        return result

    def _dispatch_model(self, action: str, data: Mapping[str, Any]) -> None:
        if action in {"model_link_relay_key", "model_rebind_relay_key"}:
            self._link_or_rebind_model(data)
            return
        if action == "model_detach_relay_key":
            self._detach_model_from_relay(data)
            return
        providers = self._draft["providers"]
        provider_index = self._provider_index(data)
        provider = self._copy_provider_for_edit(providers[provider_index])
        models = provider.get("models")
        if not isinstance(models, list):
            models = []
            provider["models"] = models
        if action == "model_move_provider":
            destination_id = data.get("destination_provider_id")
            destination_index = self._provider_index({"provider_id": destination_id})
            if destination_index == provider_index:
                return
            model_index = self._model_index(provider, data)
            model = self._copy_model_for_edit(models.pop(model_index))
            source_provider_name = str(provider.get("name", "")).strip()
            probe_key = self._probe_model_key(model)
            destination_provider = self._copy_provider_for_edit(providers[destination_index])
            destination_models = destination_provider.get("models")
            if not isinstance(destination_models, list):
                destination_models = []
                destination_provider["models"] = destination_models
            destination_keys = self._provider_api_keys(destination_provider)
            destination_key_name = ""
            destination_provider_key_id = ""
            if isinstance(destination_keys, Sequence) and not isinstance(
                destination_keys, (str, bytes, bytearray)
            ):
                for item in destination_keys:
                    if not isinstance(item, Mapping):
                        continue
                    destination_key_name = str(item.get("name", "")).strip()
                    if destination_key_name:
                        destination_provider_key_id = str(item.get("id", "")).strip()
                        break
            model.update(
                {
                    "provider": str(destination_provider.get("name", "")).strip(),
                    "api_base": "",
                    "api_key": "",
                    "api_key_name": destination_key_name,
                    "provider_key_id": destination_provider_key_id,
                    "catalog_mode": "independent",
                    "source_model_id": "",
                    "order_mode": "manual",
                    "manual_order": model.get("effective_order", model.get("order", 0)),
                }
            )
            self._normalize_model_binding(destination_provider, model)
            destination_models.append(model)
            destination_provider_name = str(destination_provider.get("name", "")).strip()
            source_probe = self._probe_overlay.get(source_provider_name, {}).pop(probe_key, None)
            if source_probe is not None:
                self._probe_overlay.setdefault(destination_provider_name, {})[probe_key] = source_probe
            providers[provider_index] = provider
            providers[destination_index] = destination_provider
            return
        if action == "model_duplicate":
            model_index = self._model_index(provider, data)
            model = self._copy_model_for_edit(models[model_index])
            used_ids: set[str] = set()
            for candidate in providers:
                if not isinstance(candidate, Mapping):
                    continue
                candidate_models = candidate.get("models", [])
                if not isinstance(candidate_models, list):
                    continue
                for item in candidate_models:
                    if isinstance(item, Mapping):
                        used_ids.add(str(item.get("deployment_id", "")).strip().lower())
            deployment_id = uuid.uuid4().hex[:8]
            while deployment_id in used_ids:
                deployment_id = uuid.uuid4().hex[:8]
            model["deployment_id"] = deployment_id
            models.insert(model_index + 1, model)
            self._model_editor_ids[id(model)] = "model-" + uuid.uuid4().hex
            providers[provider_index] = provider
            return
        if action in {"model_add", "add_model", "model_add_many", "add_models"}:
            values: list[object]
            if action in {"model_add_many", "add_models"}:
                supplied = data.get("models")
                if not isinstance(supplied, Sequence) or isinstance(supplied, (str, bytes, bytearray)):
                    raise DomainError("Models must be a list")
                values = list(supplied)
            else:
                values = [data.get("model", data.get("value", {}))]
            used_deployment_ids = {
                str(candidate.get("deployment_id", "")).strip().lower()
                for candidate_provider in providers
                if isinstance(candidate_provider, Mapping)
                for candidate in candidate_provider.get("models", [])
                if isinstance(candidate, Mapping)
                and str(candidate.get("deployment_id", "")).strip()
            }
            for value in values:
                models.append(self._new_model(provider, value, used_deployment_ids))
        elif action in {"model_patch", "patch_model"}:
            index = self._model_index(provider, data)
            model = self._copy_model_for_edit(models[index])
            previous_upstream_model = model.get("litellm_model")
            previous_fallback_surface = str(
                model.get("upstream_url_surface", "")
            ).strip()
            changes = self._changes(data, "model")
            if {"catalog_mode", "source_model_id"}.intersection(changes):
                raise DomainError("Relay association is selected through a provider key")
            provider_key_changed = "provider_key_id" in changes
            api_key_name_changed = "api_key_name" in changes
            if "name" in changes:
                changes["model_name"] = changes.pop("name")
            if "upstream_model" in changes:
                changes["litellm_model"] = changes.pop("upstream_model")
            current_order_mode = str(model.get("order_mode", "manual")).strip() or "manual"
            next_order_mode = str(changes.get("order_mode", current_order_mode)).strip() or "manual"
            if "order" in changes and "manual_order" not in changes and next_order_mode == "manual":
                changes["manual_order"] = changes["order"]
            if (
                next_order_mode == "relay_multiplier"
                and current_order_mode != "relay_multiplier"
                and "effective_order" not in changes
            ):
                changes["effective_order"] = None
            if "model_enabled" in changes:
                changes["enabled"] = changes["model_enabled"]
            elif "enabled" in changes:
                changes["model_enabled"] = changes["enabled"]
            if "supported_upstream_url_surfaces" in changes:
                raise DomainError(
                    "Protocol lists are unavailable; choose a protocol mode and backup protocol"
                )
            if "upstream_protocol_mode" in changes:
                mode = str(changes["upstream_protocol_mode"] or "").strip().lower()
                if mode not in {"fallback", "fixed"}:
                    raise DomainError("Protocol mode must be fallback or fixed")
                changes["upstream_protocol_mode"] = mode
            effective_mode = str(
                changes.get(
                    "upstream_protocol_mode",
                    model.get("upstream_protocol_mode", "fallback"),
                )
            ).strip().lower()
            if (
                "litellm_model" in changes
                and "upstream_url_surface" not in changes
                and effective_mode == "fallback"
                and previous_fallback_surface
                == infer_upstream_fallback_surface(previous_upstream_model)
            ):
                changes["upstream_url_surface"] = infer_upstream_fallback_surface(
                    changes["litellm_model"]
                )
            if isinstance(changes.get("litellm_extra"), Mapping):
                merged_extra = dict(model.get("litellm_extra", {})) if isinstance(model.get("litellm_extra"), Mapping) else {}
                merged_extra.update(changes["litellm_extra"])
                changes["litellm_extra"] = merged_extra
            model.update(changes)
            if api_key_name_changed and not provider_key_changed:
                selected_name = str(changes.get("api_key_name", "")).strip()
                matching_key = next(
                    (
                        item
                        for item in self._provider_api_keys(provider)
                        if item["name"] == selected_name
                    ),
                    None,
                )
                # A typed key label can be staged before its credential slot
                # is created/configured.  It is intentionally independent:
                # only a verified stable provider_key_id can select a relay
                # source.
                model["provider_key_id"] = matching_key["id"] if matching_key else ""
            if provider_key_changed:
                selected_id = str(changes.get("provider_key_id", "")).strip()
                matching_key = next(
                    (
                        item
                        for item in self._provider_api_keys(provider)
                        if item["id"] == selected_id
                    ),
                    None,
                )
                if selected_id and matching_key is None:
                    raise DomainError("The selected provider key is unavailable")
                model["api_key_name"] = matching_key["name"] if matching_key is not None else ""
            selected_key = self._model_provider_key(provider, model)
            selected_relay_key = (
                selected_key is not None
                and isinstance(selected_key.get("source"), Mapping)
                and selected_key["source"].get("kind") == "relay"
            )
            if (provider_key_changed or api_key_name_changed) and not selected_relay_key:
                # Moving off a relay ProviderKey must leave no model-local
                # relay state behind.  Preserve the user's manual order.
                if str(model.get("order_mode", "manual")).strip() == "relay_multiplier":
                    manual_order = self._order_value(
                        model.get("manual_order", model.get("order", 0)),
                        label="Manual route order",
                    )
                    model.update(
                        {
                            "order_mode": "manual",
                            "manual_order": manual_order,
                            "effective_order": manual_order,
                            "order": manual_order,
                        }
                    )
                model["source_model_id"] = ""
            if "litellm_model" in changes:
                model["litellm_model"] = self._canonical_upstream_model(model["litellm_model"], model)
            if "upstream_url_surface" in changes:
                model["litellm_model"] = self._canonical_upstream_model(model.get("litellm_model"), model)
            self._normalize_model_binding(provider, model)
            models[index] = model
        elif action in {"model_delete", "delete_model"}:
            removed = models.pop(self._model_index(provider, data))
            if isinstance(removed, Mapping):
                provider_name = str(provider.get("name", "")).strip()
                self._probe_overlay.get(provider_name, {}).pop(self._probe_model_key(removed), None)
        elif action in {"model_move", "move_model"}:
            source = data.get("from", data.get("source"))
            if type(source) is not int:
                source = self._model_index(provider, data)
            _move(models, source, _direction_destination(source, len(models), data), "model")
        else:
            raise DomainError("The requested model action is unavailable")
        providers[provider_index] = provider

    def _reorder_route_group(self, data: Mapping[str, Any]) -> None:
        """Rewrite one public-model route group's orders across providers."""

        public_model = str(data.get("public_model", "")).strip()
        route_ids = data.get("route_ids")
        if not public_model or not isinstance(route_ids, list) or not route_ids:
            raise DomainError("The route order is invalid")
        requested = [str(value).strip() for value in route_ids]
        if any(not value for value in requested) or len(set(requested)) != len(requested):
            raise DomainError("The route order is invalid")
        matched: dict[str, tuple[int, int]] = {}
        for provider_index, provider in enumerate(self._draft["providers"]):
            if not isinstance(provider, Mapping):
                continue
            models = provider.get("models", [])
            if not isinstance(models, list):
                continue
            for model_index, model in enumerate(models):
                if not isinstance(model, Mapping):
                    continue
                model_name = str(model.get("model_name", "")).strip()
                route_id = str(model.get("deployment_id", "")).strip() or self._editor_id(model, model=True)
                if model_name != public_model or not route_id:
                    continue
                if str(model.get("order_mode", "manual")).strip() == "relay_multiplier":
                    raise DomainError(
                        "Routes that follow a relay multiplier cannot be manually reordered"
                    )
                matched[route_id] = (provider_index, model_index)
        if set(matched) != set(requested):
            raise DomainError("The route order changed; refresh and try again")
        changed_providers: dict[int, dict[str, Any]] = {}
        for order, deployment_id in enumerate(requested, start=1):
            provider_index, model_index = matched[deployment_id]
            provider = changed_providers.get(provider_index)
            if provider is None:
                provider = self._copy_provider_for_edit(self._draft["providers"][provider_index])
                changed_providers[provider_index] = provider
            models = provider.get("models", [])
            if not isinstance(models, list):
                raise DomainError("The selected model is unavailable")
            model = self._copy_model_for_edit(models[model_index])
            model["order"] = order
            model["manual_order"] = order
            model["effective_order"] = order
            models[model_index] = model
        for provider_index, provider in changed_providers.items():
            self._draft["providers"][provider_index] = provider

    def dispatch(self, action: str, payload: object | None = None) -> dict[str, Any]:
        name = _action_name(action)
        data = _mapping(payload or {})
        if name in {"set_raw", "setraw"}:
            self._set_raw(data)
        elif name in {"providers_import_selected", "provider_import_selected", "import_selected"}:
            self._import_selected(data)
        elif name in {"providers_import_codex_current", "provider_import_codex_current", "import_codex_current"}:
            self._import_codex_current()
        elif name in {"providers_import_claude_current", "provider_import_claude_current", "import_claude_current"}:
            self._import_claude_current()
        elif name in {"providers_fetch_models", "provider_fetch_models", "fetch_models"}:
            self._fetch_models(data)
        elif name == "provider_fetch_relay_resource_models":
            self._fetch_relay_resource_models(data)
        elif name in {"relay_import", "providers_relay_import"}:
            self._last_operation = self._stage_relay_import(
                data.get("sources"),
                import_mode=data.get("import_mode", "linked"),
            )
        elif name == "provider_import_relay_key":
            self._last_operation = self._stage_provider_relay_key_import(data)
        elif name == "model_select_relay_resource":
            self._last_operation = self._select_model_relay_resource(data)
        elif name in {"set", "replace"}:
            if "document" in data or any(key in data for key in ("config", "config_text", "raw_yaml", "text")):
                self._set_raw(data)
            else:
                self._replace_draft(data.get("providers", data), data.get("document"))
        elif name in {"reset", "cancel", "reload", "restore_defaults"}:
            provider_bindings, model_bindings = self._editor_id_bindings()
            self._draft = copy.deepcopy(self._raw)
            self._restore_editor_id_bindings(provider_bindings, model_bindings)
            self._probe_overlay.clear()
        elif name in {"routes_reorder_group", "route_reorder_group"}:
            self._reorder_route_group(data)
        elif name in {"relay_dependency_policy", "relay_apply_dependency_policy"}:
            self._last_operation = self._apply_relay_dependency_policy(
                data.get("resources", data.get("source", data)),
                data.get("policy"),
                data.get("rebind"),
            )
            if self._last_operation["issues"]:
                raise DomainError("Relay dependency policy could not be applied")
        elif name.startswith("provider_") or name in {"add_provider", "patch_provider", "delete_provider", "move_provider", "clear_provider_key"}:
            self._dispatch_provider(name, data)
        elif name.startswith("model_") or name in {"add_model", "patch_model", "delete_model", "move_model"}:
            self._dispatch_model(name, data)
        else:
            raise DomainError("The requested provider/model action is unavailable")
        self.revision += 1
        result = self.snapshot()
        if hasattr(self, "_last_operation"):
            result["operation_summary"] = copy.deepcopy(self._last_operation)
        return result

    def secret_present(self, field: str, target: str | None = None) -> bool:
        if field == "import_link" and target is None:
            return False
        if field != "api_key" or not isinstance(target, str):
            raise DomainError("The requested secret field is unavailable")
        index, key_name = self._secret_target(target)
        provider = self._draft["providers"][index]
        if not isinstance(provider, Mapping):
            return False
        keys = self._provider_api_keys(provider)
        if key_name is None:
            if keys:
                value = keys[0].get("value", "")
                return isinstance(value, str) and bool(value.strip())
            direct = provider.get("api_key", "")
            return isinstance(direct, str) and bool(direct.strip())
        key = keys[self._api_key_index(keys, key_name)]
        value = key.get("value", "")
        return isinstance(value, str) and bool(value.strip())

    def trusted_secret_value(self, field: str, target: str | None = None) -> str:
        """Return the selected staged provider key for the native read lease.

        This method has no public caller: snapshots and regular provider
        actions continue to expose only configured/presence metadata.
        """

        if field != "api_key" or not isinstance(target, str):
            raise DomainError("The requested secret field is unavailable")
        index, key_name = self._secret_target(target)
        provider = self._draft["providers"][index]
        if not isinstance(provider, Mapping):
            raise DomainError("The selected provider is unavailable")
        keys = self._provider_api_keys(provider)
        if key_name is None:
            value = keys[0].get("value", "") if keys else provider.get("api_key", "")
        else:
            value = keys[self._api_key_index(keys, key_name)].get("value", "")
        if not isinstance(value, str):
            raise DomainError("The requested secret field is unavailable")
        return value

    def stage_secret(self, field: str, target: str | None, value: str) -> None:
        if field == "import_link" and target is None:
            if not value:
                raise DomainError("The provider import link is unavailable")
            self._import_link(value)
            self.revision += 1
            return
        if field != "api_key" or not isinstance(target, str):
            raise DomainError("The requested secret field is unavailable")
        index, key_name = self._secret_target(target)
        self._stage_provider_secret(index, key_name, value)
        self.revision += 1

    def _validate(
        self,
        *,
        allow_unmaterialized_relay_keys: bool = False,
    ) -> dict[str, Any]:
        from config_editor_core import api as config_api

        document = self._draft.get("document")
        providers = self._draft.get("providers")
        if isinstance(providers, list):
            for provider in providers:
                if not isinstance(provider, Mapping):
                    continue
                try:
                    keys = self._provider_api_keys(provider)
                except DomainError:
                    return {"valid": False, "errors": ["Provider API keys are invalid"]}
                if any(
                    isinstance(key.get("name"), str)
                    and bool(key["name"].strip())
                    and not (
                        isinstance(key.get("value"), str)
                        and bool(key["value"].strip())
                    )
                    and not (
                        allow_unmaterialized_relay_keys
                        and isinstance(key.get("source"), Mapping)
                        and key["source"].get("kind") == "relay"
                    )
                    for key in keys
                ):
                    return {"valid": False, "errors": ["Every API key needs a value"]}
        candidate_providers = copy.deepcopy(providers)
        if allow_unmaterialized_relay_keys and isinstance(candidate_providers, list):
            for provider in candidate_providers:
                if not isinstance(provider, dict):
                    continue
                keys = self._provider_api_keys(provider)
                changed = False
                for key in keys:
                    if (
                        key["source"].get("kind") == "relay"
                        and not str(key.get("value", "")).strip()
                    ):
                        key["value"] = "os.environ/LITELLM_MENU_RELAY_PREFLIGHT_PLACEHOLDER"
                        changed = True
                if changed:
                    self._sync_primary_api_key(provider, keys)
                relay_key_ids = {
                    key["id"]
                    for key in self._provider_api_keys(provider)
                    if key["source"].get("kind") == "relay"
                }
                models = provider.get("models", [])
                if isinstance(models, list):
                    for model in models:
                        if not isinstance(model, dict):
                            continue
                        if (
                            str(model.get("provider_key_id", "")).strip()
                            not in relay_key_ids
                            or str(model.get("order_mode", "manual")).strip()
                            != "relay_multiplier"
                            or model.get("effective_order") not in (None, "")
                        ):
                            continue
                        # Before Apply the multiplier has not been fetched
                        # yet. Validate the rest of this staged draft using a
                        # local candidate only; materialization remains the
                        # strict gate that writes the real effective order.
                        model["effective_order"] = self._order_value(
                            model.get("manual_order", 1),
                            label="Manual route order",
                        )
                        model["order"] = model["effective_order"]
        try:
            with tempfile.TemporaryDirectory(prefix="litellm-core-provider-validate-") as directory:
                target = Path(directory) / "config.yaml"
                source = _mapping(document, "document")
                config_text = source.get("config")
                if not isinstance(config_text, str):
                    raise DomainError("Provider/model configuration is invalid")
                atomic_write_text(target, config_text)
                disabled = source.get("disabled")
                if isinstance(disabled, str):
                    atomic_write_text(target.with_name("config.disabled-models.yaml"), disabled)
                config_api.save_config(candidate_providers, target, document=source)
        except DomainError:
            raise
        except Exception:
            return {"valid": False, "errors": ["Provider/model configuration is invalid"]}
        return {"valid": True, "errors": []}

    def validate_relay_preflight(self, payload: object | None = None) -> dict[str, Any]:
        """Validate a linked draft before Core fetches its private key material."""

        if payload is not None:
            before = copy.deepcopy(self._draft)
            provider_editor_ids = dict(self._provider_editor_ids)
            model_editor_ids = dict(self._model_editor_ids)
            try:
                data = _mapping(payload)
                if "providers" in data or "document" in data:
                    self._replace_draft(
                        data.get("providers", self._draft.get("providers", [])),
                        data.get("document"),
                    )
                return self._validate(allow_unmaterialized_relay_keys=True)
            finally:
                self._draft = before
                self._provider_editor_ids = provider_editor_ids
                self._model_editor_ids = model_editor_ids
        return self._validate(allow_unmaterialized_relay_keys=True)

    def validate(self, payload: object | None = None) -> dict[str, Any]:
        # Explicit candidate payloads are accepted by staging through the same
        # parser rather than inventing a second validation implementation.
        if payload is not None:
            before = copy.deepcopy(self._draft)
            provider_editor_ids = dict(self._provider_editor_ids)
            model_editor_ids = dict(self._model_editor_ids)
            try:
                data = _mapping(payload)
                if "providers" in data or "document" in data:
                    self._replace_draft(data.get("providers", self._draft.get("providers", [])), data.get("document"))
                result = self._validate()
            finally:
                self._draft = before
                self._provider_editor_ids = provider_editor_ids
                self._model_editor_ids = model_editor_ids
            return result
        return self._validate()

    def apply(self, payload: object | None = None) -> dict[str, Any]:
        from config_editor_core import api as config_api

        if payload is not None:
            data = _mapping(payload)
            if "providers" in data or "document" in data:
                self._replace_draft(data.get("providers", self._draft.get("providers", [])), data.get("document"))
        validation = self._validate()
        if not validation["valid"]:
            raise DomainError("Provider/model configuration is invalid")
        try:
            config_api.save_config(
                copy.deepcopy(self._draft["providers"]),
                self.config_path,
                self._disk_revision,
                copy.deepcopy(self._draft["document"]),
            )
        except Exception as exc:
            message = str(exc)
            if "changed on disk" in message:
                raise DomainError("Provider/model configuration changed on disk; reload before applying") from None
            raise _safe_problem(exc, "Provider/model configuration could not be saved") from None
        self.reload()
        return {"applied": True, **self.snapshot()}

    def _current_disk_revision(self) -> object:
        from config_editor_core.schema import _config_revision

        try:
            return copy.deepcopy(_config_revision(self.config_path))
        except Exception as exc:
            raise _safe_problem(exc, "Provider/model configuration could not be inspected") from None

    def external_disk_state(self) -> dict[str, bool]:
        """Compare config and disabled-model companion files with their baseline."""

        current = self._current_disk_revision()
        # ``_exists`` describes the last successfully loaded document.  The
        # disk watcher must instead describe the files that exist *now*, so a
        # deletion is treated as a real external change rather than a stale
        # in-memory state.
        exists = isinstance(current, Mapping) and bool(
            _mapping(current.get("config", {}), "config revision").get("exists")
        )
        return {"changed": current != self._disk_revision, "exists": exists}

    def external_disk_identity(self) -> str:
        """Expose a hash of the private parser revision only to Core, never RN."""

        current = self._current_disk_revision()
        try:
            encoded = json.dumps(current, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        except (TypeError, ValueError):
            raise DomainError("Provider/model configuration could not be inspected") from None
        return hashlib.sha256(encoded).hexdigest()

    def rebase_external_disk(self) -> dict[str, Any]:
        """Accept the on-disk revision while retaining the staged provider draft."""

        self._disk_revision = self._current_disk_revision()
        self.revision += 1
        return self.snapshot()

    def reload(self) -> dict[str, Any]:
        provider_bindings, model_bindings = self._editor_id_bindings()
        loaded = self._load()
        self._normalize_provider_model_bindings(loaded["providers"])
        self._raw = {"providers": copy.deepcopy(loaded["providers"]), "document": copy.deepcopy(loaded["document"])}
        self._draft = copy.deepcopy(self._raw)
        self._disk_revision = copy.deepcopy(loaded["disk_revision"])
        self._exists = bool(loaded["exists"])
        self._restore_editor_id_bindings(provider_bindings, model_bindings)
        self._probe_overlay.clear()
        self.revision += 1
        return self.snapshot()

    def export(self, *, include_sensitive: bool = False) -> dict[str, Any]:
        if include_sensitive:
            return {"domain": self.name, "providers": copy.deepcopy(self._draft["providers"]), "document": copy.deepcopy(self._draft["document"])}
        return self.snapshot()

    def import_package(self, payload: object) -> None:
        data = _mapping(payload, "provider/model package")
        self._replace_draft(data.get("providers", []), data.get("document"))
        self.revision += 1

    def prepare_probe(self, _payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        protocols: list[str] = []
        for provider in self._draft.get("providers", []):
            if not isinstance(provider, Mapping):
                continue
            for model in provider.get("models", []):
                if isinstance(model, Mapping):
                    protocol = model.get("upstream_url_surface")
                    if isinstance(protocol, str) and protocol not in protocols:
                        protocols.append(protocol)
        data = dict(_payload or {})
        selected = _selected_identifier(data, "provider_id", "provider", "name", "id", "index")
        providers = self._draft.get("providers", [])
        model_selected = _selected_identifier(
            data,
            "model_id",
            "deployment_id",
            "model_name",
            "model",
        )
        if model_selected is not None:
            provider_index = self._provider_index(data)
            provider = providers[provider_index]
            if not isinstance(provider, Mapping):
                raise DomainError("The selected provider is unavailable")
            models = provider.get("models", [])
            if not isinstance(models, list):
                raise DomainError("The selected model is unavailable")
            model_index = self._model_index(provider, data)
            model = models[model_index]
            if not isinstance(model, Mapping):
                raise DomainError("The selected model is unavailable")
            return {
                "kind": "model",
                "provider": copy.deepcopy(dict(provider)),
                "model": copy.deepcopy(dict(model)),
                "provider_id": self._editor_id(provider),
                "model_id": self._editor_id(model, model=True),
            }
        if selected is not None:
            indices = [self._provider_index(data)]
        else:
            indices = [
                index
                for index, provider in enumerate(providers)
                if isinstance(provider, Mapping) and self._provider_api_base(provider)
            ]
        if not indices:
            return {"kind": "none", "protocols": protocols}
        return {
            "kind": "providers",
            "protocols": protocols,
            "providers": [
                {
                    "provider": copy.deepcopy(dict(providers[index])),
                    "provider_id": self._editor_id(providers[index]),
                }
                for index in indices
                if isinstance(providers[index], Mapping)
            ],
        }

    def perform_probe(self, prepared: Mapping[str, Any]) -> dict[str, Any]:
        kind = prepared.get("kind")
        if kind == "model":
            return self._probe_model(
                _mapping(prepared.get("provider"), "provider"),
                _mapping(prepared.get("model"), "model"),
                provider_id=str(prepared.get("provider_id", "")),
                model_id=str(prepared.get("model_id", "")),
            )
        protocols = [item for item in prepared.get("protocols", []) if isinstance(item, str)]
        if kind == "none":
            return {
                "ok": False,
                "protocols": protocols + [self._MODEL_LIST_PROTOCOL],
                "detail": "No provider model endpoint is configured",
                "models": [],
                "model_count": 0,
            }
        targets = prepared.get("providers", [])
        if not isinstance(targets, list):
            raise DomainError("The selected provider is unavailable")
        probes: list[dict[str, Any]] = []
        all_models: list[str] = []
        seen_models: set[str] = set()
        for target in targets:
            if not isinstance(target, Mapping):
                continue
            provider = target.get("provider")
            if not isinstance(provider, Mapping):
                continue
            fetched = self._fetch_provider_models(provider, str(target.get("provider_id", "")))
            probes.append(fetched)
            for model in fetched["models"]:
                if model not in seen_models:
                    seen_models.add(model)
                    all_models.append(model)
        return {
            "ok": bool(probes) and all(probe["available"] for probe in probes),
            "protocols": protocols + ([self._MODEL_LIST_PROTOCOL] if self._MODEL_LIST_PROTOCOL not in protocols else []),
            "detail": "Provider model endpoint probe succeeded"
            if probes and all(probe["available"] for probe in probes)
            else "Provider model endpoint probe failed",
            "providers": [
                {"id": probe["provider_id"], "available": probe["available"], "model_count": probe["model_count"]}
                for probe in probes
            ],
            "models": all_models[: self._MAX_MODEL_CANDIDATES],
            "model_count": len(all_models),
        }

    def commit_probe(
        self,
        prepared: Mapping[str, Any],
        value: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        result = dict(value)
        overlay = result.pop("_overlay", None)
        if prepared.get("kind") != "model" or not isinstance(overlay, Mapping):
            return result, False
        provider_index = self._provider_index({"provider_id": prepared.get("provider_id")})
        provider = self._draft["providers"][provider_index]
        if not isinstance(provider, Mapping):
            raise DomainError("The selected provider is unavailable")
        model_index = self._model_index(provider, {"model_id": prepared.get("model_id")})
        models = provider.get("models", [])
        if not isinstance(models, list) or not isinstance(models[model_index], Mapping):
            raise DomainError("The selected model is unavailable")
        model = models[model_index]
        provider_name = str(provider.get("name", "")).strip()
        probe_key = self._probe_model_key(model)
        probe = overlay.get("probe")
        if not isinstance(probe, Mapping):
            raise DomainError("The model probe returned invalid state")
        self._probe_overlay.setdefault(provider_name, {})[probe_key] = copy.deepcopy(dict(probe))
        self.revision += 1
        return result, True

    def probe(self, _payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        prepared = self.prepare_probe(_payload)
        result, _changed = self.commit_probe(prepared, self.perform_probe(prepared))
        return result


__all__ = ["ProvidersModelsDomain"]
