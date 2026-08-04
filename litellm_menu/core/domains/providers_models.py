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

from ..persistence import atomic_write_text
from ..security import REDACT_TEXT, redact
from ._shared import (
    LegacyDomainError,
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
        # Live multiplier data is a transient, read-only projection. It must never
        # enter the staged document because ``apply`` writes that document.
        self._billing_overlay: dict[str, dict[str, dict[str, Any]]] = {}
        self._probe_overlay: dict[str, dict[str, dict[str, Any]]] = {}
        self._disk_revision: object = None
        self._exists = False
        self._provider_editor_ids: dict[int, str] = {}
        self._model_editor_ids: dict[int, str] = {}
        self.revision = 0
        self.reload()

    @staticmethod
    def _empty_document() -> dict[str, str | None]:
        # The legacy parser accepts a config with no providers.  Do not create
        # this document on disk; it merely keeps a missing installation
        # renderable until the user explicitly restores/imports a config.
        return {"config": "model_list: []\n", "disabled": None}

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
            raise LegacyDomainError("Provider/model configuration is invalid")
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
            for source_model, copied_model in zip(source_models, copied_models, strict=False):
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

    @staticmethod
    def _billing_model_key(model: Mapping[str, Any]) -> str:
        """Return the same stable model identity used by provider_billing."""

        return str(model.get("deployment_id") or model.get("model_name") or "")

    def _probe_model_key(self, model: Mapping[str, Any]) -> str:
        """Return the editor identity so one model's result never aliases another's."""

        return self._editor_id(model, model=True)

    @staticmethod
    def _upstream_model_prefix(model: Mapping[str, Any]) -> str:
        surfaces = model.get("supported_upstream_url_surfaces", [])
        if not isinstance(surfaces, list):
            surfaces = []
        primary = str(model.get("upstream_url_surface", "")).strip()
        surface = str(surfaces[0]).strip() if surfaces else primary
        return "anthropic" if surface == "anthropic" else "openai"

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
    def _safe_billing_overlay(model: Mapping[str, Any]) -> dict[str, Any]:
        """Keep only the UI multiplier fields from an optional remote response."""

        return {
            "billing": redact(
                {
                    key: model.get(key)
                    for key in ("status", "detail", "source")
                    if model.get(key) is not None
                }
            ),
            "multiplier": redact(model.get("multiplier")) if model.get("multiplier") is not None else None,
        }

    def _safe_provider(self, provider: object, index: int) -> dict[str, Any]:
        if not isinstance(provider, Mapping):
            return {
                "id": f"provider-{index + 1}",
                "name": "",
                "enabled": False,
                "api_key_names": [],
                "models": [],
            }
        name = str(provider.get("name", "")).strip()
        billing_overlay = self._billing_overlay.get(name, {})
        keys = provider.get("api_keys", [])
        configured_key = bool(provider.get("api_key"))
        api_key_names: list[str] = []
        key_configured: dict[str, bool] = {}
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
                if key_name:
                    key_configured[key_name] = configured
                configured_key = configured_key or configured
        models: list[dict[str, Any]] = []
        raw_models = provider.get("models", [])
        if isinstance(raw_models, Sequence) and not isinstance(raw_models, (str, bytes, bytearray)):
            for model_index, model in enumerate(raw_models):
                if not isinstance(model, Mapping):
                    continue
                model_key_name = str(model.get("api_key_name", "")).strip()
                model_api_key = model.get("api_key")
                model_key = (
                    key_configured.get(model_key_name, False)
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
                live_billing = billing_overlay.get(self._billing_model_key(model), {})
                live_probe = self._probe_overlay.get(name, {}).get(self._probe_model_key(model), {})
                model_enabled = model.get("model_enabled", model.get("enabled", True)) is not False
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
                        "enabled": model_enabled,
                        "model_enabled": model_enabled,
                        "order": model.get("order", 1),
                        "ssl_verify": str(model.get("ssl_verify", "")),
                        "ssl_verify_present": bool(model.get("ssl_verify_present")),
                        "deployment_id": str(model.get("deployment_id", "")),
                        "upstream_url_surface": str(model.get("upstream_url_surface", "")),
                        "supported_upstream_url_surfaces": list(model.get("supported_upstream_url_surfaces", []))
                        if isinstance(model.get("supported_upstream_url_surfaces"), list)
                        else [],
                        "supports_responses_image_generation_tool": bool(model.get("supports_responses_image_generation_tool")),
                        "supports_responses_image_generation_tool_present": bool(model.get("supports_responses_image_generation_tool_present")),
                        "litellm_extra": safe_litellm_extra,
                        "api_key_configured": model_key,
                        "billing": redact(live_billing.get("billing", model.get("billing")))
                        if live_billing.get("billing", model.get("billing")) is not None
                        else None,
                        "multiplier": redact(live_billing.get("multiplier", model.get("multiplier")))
                        if live_billing.get("multiplier", model.get("multiplier")) is not None
                        else None,
                        "probe": redact(live_probe) if live_probe else None,
                    }
                )
        model_counts: dict[str, int] = {}
        for model in models:
            key_name = str(model.get("api_key_name", "")).strip()
            if key_name:
                model_counts[key_name] = model_counts.get(key_name, 0) + 1
        if isinstance(keys, Sequence) and not isinstance(keys, (str, bytes, bytearray)):
            for item in keys:
                if not isinstance(item, Mapping):
                    continue
                key_name = str(item.get("name", "")).strip()
                if not key_name or any(state["name"] == key_name for state in key_states):
                    continue
                key_value = item.get("value", "")
                key_states.append(
                    {
                        "name": key_name,
                        "configured": isinstance(key_value, str) and bool(key_value.strip()),
                        "model_count": model_counts.get(key_name, 0),
                    }
                )
        return {
            "id": name or self._editor_id(provider),
            "editor_id": self._editor_id(provider),
            "name": name,
            "enabled": provider.get("enabled") is not False,
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
            raise LegacyDomainError("Providers must be an array")
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
        self._billing_overlay.clear()
        self._probe_overlay.clear()

    def _set_raw(self, data: Mapping[str, Any]) -> None:
        from config_editor_core.load import load_config_document, normalize_config_document

        document = data.get("document")
        if document is None:
            config_text = data.get("config", data.get("config_text", data.get("raw_yaml", data.get("text"))))
            disabled_text = data.get("disabled", data.get("disabled_text"))
            if not isinstance(config_text, str):
                raise LegacyDomainError("Provider/model YAML must be text")
            document = {"config": config_text, "disabled": disabled_text}
        try:
            normalized = normalize_config_document(document)
            loaded = load_config_document(normalized)
        except Exception as exc:
            raise _safe_problem(exc, "Provider/model configuration is invalid") from None
        self._draft = {"providers": copy.deepcopy(loaded["providers"]), "document": copy.deepcopy(loaded["document"])}
        self._provider_editor_ids.clear()
        self._model_editor_ids.clear()
        self._billing_overlay.clear()
        self._probe_overlay.clear()

    def _import_selected(self, data: Mapping[str, Any]) -> None:
        """Stage an explicitly selected external config through its existing parser."""

        source = data.get("path")
        if not isinstance(source, str) or not source:
            raise LegacyDomainError("Select a provider configuration file")
        try:
            import external_provider_import

            self._stage_import_result(external_provider_import.import_explicit(Path(source)))
        except LegacyDomainError:
            raise
        except Exception as exc:
            raise _safe_problem(exc, "Provider configuration could not be imported") from None

    def _stage_import_result(self, imported: object) -> None:
        providers = imported.get("providers") if isinstance(imported, Mapping) else None
        if not isinstance(providers, list):
            raise LegacyDomainError("Provider configuration could not be imported")
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
        except LegacyDomainError:
            raise
        except Exception as exc:
            raise _safe_problem(exc, "Provider configuration could not be imported") from None

    def _import_claude_current(self) -> None:
        try:
            import external_provider_import

            self._stage_import_result(external_provider_import.import_claude_current())
        except LegacyDomainError:
            raise
        except Exception as exc:
            raise _safe_problem(exc, "Provider configuration could not be imported") from None

    def _refresh_multiplier(self) -> None:
        import provider_billing

        payload = provider_billing.collect_billing(self.config_path, timeout=5.0)
        providers = payload.get("providers", []) if isinstance(payload, Mapping) else []
        overlay: dict[str, dict[str, dict[str, Any]]] = {}
        if isinstance(providers, Sequence) and not isinstance(providers, (str, bytes, bytearray)):
            for provider in providers:
                if not isinstance(provider, Mapping):
                    continue
                provider_name = str(provider.get("name", "")).strip()
                models = provider.get("models", [])
                if not provider_name or not isinstance(models, Sequence) or isinstance(models, (str, bytes, bytearray)):
                    continue
                by_model: dict[str, dict[str, Any]] = {}
                for model in models:
                    if not isinstance(model, Mapping):
                        continue
                    identity = str(model.get("deployment_id") or model.get("model") or model.get("name") or "")
                    if identity:
                        by_model[identity] = self._safe_billing_overlay(model)
                if by_model:
                    overlay[provider_name] = by_model
        self._billing_overlay = overlay
        summary = payload.get("summary", {}) if isinstance(payload, Mapping) else {}
        self._last_operation = {
            "operation": "multiplier",
            "available": True,
            "summary": redact(summary) if isinstance(summary, Mapping) else {},
        }

    def _import_link(self, link: str) -> None:
        try:
            import external_provider_import

            self._stage_import_result(external_provider_import.import_link(link))
        except LegacyDomainError:
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
                raise LegacyDomainError("The selected API key has no value")
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
        """Normalize an OpenAI-compatible base through the existing billing helper."""

        try:
            # The billing module already implements the app's generic base
            # normalization for /v1, /v1/models and endpoint-shaped bases.
            from provider_billing import _service_root

            root = _service_root(api_base)
        except Exception:
            return None
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
    ) -> dict[str, Any]:
        """Fetch one standard `/v1/models` list without exposing remote details."""

        selected_key_name, credential = self._provider_credential(provider, api_key_name)
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
            from provider_billing import _billing_http_opener

            with _billing_http_opener().open(request, timeout=self._MODEL_LIST_TIMEOUT_SECONDS) as response:
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
            raise LegacyDomainError("The selected provider is unavailable")
        api_key_name = self._api_key_name(data["api_key_name"]) if "api_key_name" in data else None
        summary = self._fetch_provider_models(
            provider,
            self._safe_provider(provider, index)["id"],
            api_key_name,
        )
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
        from provider_billing import _billing_http_opener, _service_root

        root = _service_root(api_base)
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
            with _billing_http_opener().open(
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

        with ThreadPoolExecutor(max_workers=len(surfaces) + 1) as executor:
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
            import provider_billing

            multiplier_target = provider_billing.BillingTarget(
                provider=provider_name,
                model=str(model.get("model_name", "")).strip(),
                upstream_model=str(model.get("litellm_model", "")).strip(),
                deployment_id=str(model.get("deployment_id", "")).strip(),
                api_base=api_base,
                api_key=credential,
            )
            billing_future = executor.submit(
                provider_billing.probe_model,
                multiplier_target,
                timeout=5.0,
            )
            surface_results = {
                surface: future.result()
                for surface, future in surface_futures.items()
            }
            billing = billing_future.result()

        model_tokens = set(re.split(r"[^a-z0-9]+", f"{model.get('model_name', '')} {model_name}".lower()))
        priority = (
            ["anthropic", "openai/responses", "openai/chat"]
            if "claude" in model_tokens
            else surfaces
        )
        recommended_order = [
            surface
            for surface in priority
            if surface_results.get(surface, {}).get("available") is True
        ]
        recommended = recommended_order[0] if recommended_order else None
        unavailable_surfaces = [surface for surface in priority if surface not in recommended_order]
        summary = {
            "available_surfaces": recommended_order,
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
            "recommended_order": recommended_order,
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
            "protocols": recommended_order,
            "recommended_surface": recommended,
            "recommended_order": recommended_order,
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
                "billing": self._safe_billing_overlay(billing) if isinstance(billing, Mapping) else None,
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
        raise LegacyDomainError("The selected provider is unavailable")

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
            raise LegacyDomainError("The selected model is unavailable")
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
        raise LegacyDomainError("The selected model is unavailable")

    @classmethod
    def _api_key_name(cls, value: object) -> str:
        name = str(value).strip() if isinstance(value, str) else ""
        if (
            not name
            or len(name.encode("utf-8")) > 128
            or cls._API_KEY_TARGET_SEPARATOR in name
            or any(char in name for char in "\x00\r\n")
        ):
            raise LegacyDomainError("An API key name is required")
        return name

    @staticmethod
    def _provider_api_keys(provider: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw_keys = provider.get("api_keys", [])
        if raw_keys is None:
            return []
        if not isinstance(raw_keys, list):
            raise LegacyDomainError("Provider API keys are invalid")
        keys: list[dict[str, Any]] = []
        for item in raw_keys:
            if not isinstance(item, Mapping):
                raise LegacyDomainError("Provider API keys are invalid")
            keys.append(copy.deepcopy(dict(item)))
        return keys

    @staticmethod
    def _sync_primary_api_key(provider: dict[str, Any], keys: list[dict[str, Any]]) -> None:
        provider["api_keys"] = keys
        first_value = keys[0].get("value", "") if keys else ""
        provider["api_key"] = first_value if isinstance(first_value, str) else ""

    @classmethod
    def _api_key_index(cls, keys: Sequence[Mapping[str, Any]], name: str) -> int:
        for index, item in enumerate(keys):
            if str(item.get("name", "")).strip() == name:
                return index
        raise LegacyDomainError("The selected API key is unavailable")

    def _dispatch_provider_key(self, action: str, data: Mapping[str, Any]) -> None:
        providers = self._draft["providers"]
        provider_index = self._provider_index(data)
        provider = self._copy_provider_for_edit(providers[provider_index])
        keys = self._provider_api_keys(provider)

        if action == "provider_key_add":
            name = self._api_key_name(data.get("name"))
            if any(str(item.get("name", "")).strip() == name for item in keys):
                raise LegacyDomainError("The API key name is already in use")
            # The value is filled only through the native secret capability.
            # Validation remains false until that secure staging step finishes.
            keys.append({"name": name, "value": ""})
        elif action == "provider_key_patch":
            old_name = self._api_key_name(data.get("old_name"))
            name = self._api_key_name(data.get("name"))
            key_index = self._api_key_index(keys, old_name)
            if name != old_name and any(
                str(item.get("name", "")).strip() == name for item in keys
            ):
                raise LegacyDomainError("The API key name is already in use")
            keys[key_index]["name"] = name
            models = provider.get("models", [])
            if isinstance(models, list):
                for model in models:
                    if isinstance(model, dict) and str(model.get("api_key_name", "")).strip() == old_name:
                        model["api_key_name"] = name
        elif action == "provider_key_delete":
            name = self._api_key_name(data.get("name"))
            key_index = self._api_key_index(keys, name)
            if len(keys) <= 1:
                raise LegacyDomainError("A provider must retain at least one API key")
            keys.pop(key_index)
            replacement_name = str(keys[0].get("name", "")).strip()
            models = provider.get("models", [])
            if isinstance(models, list):
                for model in models:
                    if isinstance(model, dict) and str(model.get("api_key_name", "")).strip() == name:
                        model["api_key_name"] = replacement_name
        else:
            raise LegacyDomainError("The requested provider action is unavailable")

        self._sync_primary_api_key(provider, keys)
        providers[provider_index] = provider

    def _secret_target(self, target: str) -> tuple[int, str | None]:
        if target.count(self._API_KEY_TARGET_SEPARATOR) > 1:
            raise LegacyDomainError("The requested secret field is unavailable")
        if self._API_KEY_TARGET_SEPARATOR not in target:
            return self._provider_index({"provider_id": target}), None
        provider_id, key_name = target.split(self._API_KEY_TARGET_SEPARATOR, 1)
        if not provider_id or not key_name:
            raise LegacyDomainError("The requested secret field is unavailable")
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
        keys[key_index]["value"] = value
        self._sync_primary_api_key(provider, keys)
        providers[provider_index] = provider

    def _dispatch_provider(self, action: str, data: Mapping[str, Any]) -> None:
        providers = self._draft["providers"]
        if action in {"provider_key_add", "provider_key_patch", "provider_key_delete"}:
            self._dispatch_provider_key(action, data)
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
            providers.append(provider)
            self._register_new_provider(provider)
            return
        if action in {"provider_patch", "patch_provider"}:
            index = self._provider_index(data)
            provider = self._copy_provider_for_edit(providers[index])
            previous_name = str(provider.get("name", "")).strip()
            changes = self._changes(data, "provider")
            if "endpoint" in changes:
                changes["api_base"] = changes.pop("endpoint")
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
            current_name = str(provider.get("name", "")).strip()
            if current_name != previous_name:
                if previous_name in self._probe_overlay:
                    self._probe_overlay[current_name] = self._probe_overlay.pop(previous_name)
                if previous_name in self._billing_overlay:
                    self._billing_overlay[current_name] = self._billing_overlay.pop(previous_name)
            if "api_keys" in changes:
                self._sync_primary_api_key(provider, self._provider_api_keys(provider))
            providers[index] = provider
            return
        if action in {"provider_clear_key", "clear_provider_key"}:
            index = self._provider_index(data)
            provider = self._copy_provider_for_edit(providers[index])
            provider["api_keys"] = []
            provider["api_key"] = ""
            providers[index] = provider
            return
        if action in {"provider_delete", "delete_provider"}:
            removed = providers.pop(self._provider_index(data))
            if isinstance(removed, Mapping):
                name = str(removed.get("name", "")).strip()
                self._probe_overlay.pop(name, None)
                self._billing_overlay.pop(name, None)
            return
        if action in {"provider_move", "move_provider"}:
            source = data.get("from", data.get("source"))
            if type(source) is not int:
                source = self._provider_index(data)
            _move(providers, source, _direction_destination(source, len(providers), data), "provider")
            return
        raise LegacyDomainError("The requested provider action is unavailable")

    def _dispatch_model(self, action: str, data: Mapping[str, Any]) -> None:
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
            billing_key = self._billing_model_key(model)
            destination_provider = self._copy_provider_for_edit(providers[destination_index])
            destination_models = destination_provider.get("models")
            if not isinstance(destination_models, list):
                destination_models = []
                destination_provider["models"] = destination_models
            destination_keys = destination_provider.get("api_keys", [])
            destination_key_name = ""
            if isinstance(destination_keys, Sequence) and not isinstance(
                destination_keys, (str, bytes, bytearray)
            ):
                for item in destination_keys:
                    if not isinstance(item, Mapping):
                        continue
                    destination_key_name = str(item.get("name", "")).strip()
                    if destination_key_name:
                        break
            model.update(
                {
                    "provider": str(destination_provider.get("name", "")).strip(),
                    "api_base": "",
                    "api_key": "",
                    "api_key_name": destination_key_name,
                }
            )
            destination_models.append(model)
            destination_provider_name = str(destination_provider.get("name", "")).strip()
            source_probe = self._probe_overlay.get(source_provider_name, {}).pop(probe_key, None)
            if source_probe is not None:
                self._probe_overlay.setdefault(destination_provider_name, {})[probe_key] = source_probe
            source_billing = self._billing_overlay.get(source_provider_name, {}).pop(billing_key, None)
            if source_billing is not None:
                self._billing_overlay.setdefault(destination_provider_name, {})[billing_key] = source_billing
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
        if action in {"model_add", "add_model"}:
            value = data.get("model", data.get("value", {}))
            model = _copy_mapping(value, "model")
            if "name" in model and "model_name" not in model:
                model["model_name"] = model.pop("name")
            if "upstream_model" in model and "litellm_model" not in model:
                model["litellm_model"] = model.pop("upstream_model")
            if "model_enabled" in model:
                model["enabled"] = model["model_enabled"]
            elif "enabled" in model:
                model["model_enabled"] = model["enabled"]
            if "litellm_model" in model:
                model["litellm_model"] = self._canonical_upstream_model(model["litellm_model"], model)
            if not str(model.get("deployment_id", "")).strip():
                used_ids: set[str] = set()
                for candidate_provider in providers:
                    if not isinstance(candidate_provider, Mapping):
                        continue
                    candidate_models = candidate_provider.get("models", [])
                    if not isinstance(candidate_models, list):
                        continue
                    for candidate in candidate_models:
                        if isinstance(candidate, Mapping):
                            used_ids.add(str(candidate.get("deployment_id", "")).strip().lower())
                deployment_id = uuid.uuid4().hex[:8]
                while deployment_id in used_ids:
                    deployment_id = uuid.uuid4().hex[:8]
                model["deployment_id"] = deployment_id
            models.append(model)
            self._editor_id(model, model=True)
        elif action in {"model_patch", "patch_model"}:
            index = self._model_index(provider, data)
            model = self._copy_model_for_edit(models[index])
            changes = self._changes(data, "model")
            if "name" in changes:
                changes["model_name"] = changes.pop("name")
            if "upstream_model" in changes:
                changes["litellm_model"] = changes.pop("upstream_model")
            if "model_enabled" in changes:
                changes["enabled"] = changes["model_enabled"]
            elif "enabled" in changes:
                changes["model_enabled"] = changes["enabled"]
            if isinstance(changes.get("litellm_extra"), Mapping):
                merged_extra = dict(model.get("litellm_extra", {})) if isinstance(model.get("litellm_extra"), Mapping) else {}
                merged_extra.update(changes["litellm_extra"])
                changes["litellm_extra"] = merged_extra
            previous_model_key = self._billing_model_key(model)
            model.update(changes)
            if "litellm_model" in changes:
                model["litellm_model"] = self._canonical_upstream_model(model["litellm_model"], model)
            if any(key in changes for key in ("upstream_url_surface", "supported_upstream_url_surfaces")):
                model["litellm_model"] = self._canonical_upstream_model(model.get("litellm_model"), model)
            current_model_key = self._billing_model_key(model)
            provider_name = str(provider.get("name", "")).strip()
            if current_model_key != previous_model_key:
                if previous_model_key in self._billing_overlay.get(provider_name, {}):
                    self._billing_overlay[provider_name][current_model_key] = self._billing_overlay[provider_name].pop(previous_model_key)
            models[index] = model
        elif action in {"model_delete", "delete_model"}:
            removed = models.pop(self._model_index(provider, data))
            if isinstance(removed, Mapping):
                provider_name = str(provider.get("name", "")).strip()
                self._probe_overlay.get(provider_name, {}).pop(self._probe_model_key(removed), None)
                self._billing_overlay.get(provider_name, {}).pop(self._billing_model_key(removed), None)
        elif action in {"model_move", "move_model"}:
            source = data.get("from", data.get("source"))
            if type(source) is not int:
                source = self._model_index(provider, data)
            _move(models, source, _direction_destination(source, len(models), data), "model")
        else:
            raise LegacyDomainError("The requested model action is unavailable")
        providers[provider_index] = provider

    def _reorder_route_group(self, data: Mapping[str, Any]) -> None:
        """Rewrite one public-model route group's orders across providers."""

        public_model = str(data.get("public_model", "")).strip()
        route_ids = data.get("route_ids")
        if not public_model or not isinstance(route_ids, list) or not route_ids:
            raise LegacyDomainError("The route order is invalid")
        requested = [str(value).strip() for value in route_ids]
        if any(not value for value in requested) or len(set(requested)) != len(requested):
            raise LegacyDomainError("The route order is invalid")
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
                matched[route_id] = (provider_index, model_index)
        if set(matched) != set(requested):
            raise LegacyDomainError("The route order changed; refresh and try again")
        changed_providers: dict[int, dict[str, Any]] = {}
        for order, deployment_id in enumerate(requested, start=1):
            provider_index, model_index = matched[deployment_id]
            provider = changed_providers.get(provider_index)
            if provider is None:
                provider = self._copy_provider_for_edit(self._draft["providers"][provider_index])
                changed_providers[provider_index] = provider
            models = provider.get("models", [])
            if not isinstance(models, list):
                raise LegacyDomainError("The selected model is unavailable")
            model = self._copy_model_for_edit(models[model_index])
            model["order"] = order
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
        elif name == "providers_refresh_multiplier":
            self._refresh_multiplier()
        elif name in {"providers_fetch_models", "provider_fetch_models", "fetch_models"}:
            self._fetch_models(data)
        elif name in {"set", "replace"}:
            if "document" in data or any(key in data for key in ("config", "config_text", "raw_yaml", "text")):
                self._set_raw(data)
            else:
                self._replace_draft(data.get("providers", data), data.get("document"))
        elif name in {"reset", "cancel", "reload", "restore_defaults"}:
            provider_bindings, model_bindings = self._editor_id_bindings()
            self._draft = copy.deepcopy(self._raw)
            self._restore_editor_id_bindings(provider_bindings, model_bindings)
            self._billing_overlay.clear()
            self._probe_overlay.clear()
        elif name in {"routes_reorder_group", "route_reorder_group"}:
            self._reorder_route_group(data)
        elif name.startswith("provider_") or name in {"add_provider", "patch_provider", "delete_provider", "move_provider", "clear_provider_key"}:
            self._dispatch_provider(name, data)
        elif name.startswith("model_") or name in {"add_model", "patch_model", "delete_model", "move_model"}:
            self._dispatch_model(name, data)
        else:
            raise LegacyDomainError("The requested provider/model action is unavailable")
        self.revision += 1
        result = self.snapshot()
        if hasattr(self, "_last_operation"):
            result["operation_summary"] = copy.deepcopy(self._last_operation)
        return result

    def secret_present(self, field: str, target: str | None = None) -> bool:
        if field == "import_link" and target is None:
            return False
        if field != "api_key" or not isinstance(target, str):
            raise LegacyDomainError("The requested secret field is unavailable")
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
            raise LegacyDomainError("The requested secret field is unavailable")
        index, key_name = self._secret_target(target)
        provider = self._draft["providers"][index]
        if not isinstance(provider, Mapping):
            raise LegacyDomainError("The selected provider is unavailable")
        keys = self._provider_api_keys(provider)
        if key_name is None:
            value = keys[0].get("value", "") if keys else provider.get("api_key", "")
        else:
            value = keys[self._api_key_index(keys, key_name)].get("value", "")
        if not isinstance(value, str):
            raise LegacyDomainError("The requested secret field is unavailable")
        return value

    def stage_secret(self, field: str, target: str | None, value: str) -> None:
        if field == "import_link" and target is None:
            if not value:
                raise LegacyDomainError("The provider import link is unavailable")
            self._import_link(value)
            self.revision += 1
            return
        if field != "api_key" or not isinstance(target, str):
            raise LegacyDomainError("The requested secret field is unavailable")
        index, key_name = self._secret_target(target)
        self._stage_provider_secret(index, key_name, value)
        self.revision += 1

    def _validate(self) -> dict[str, Any]:
        from config_editor_core import api as config_api

        document = self._draft.get("document")
        providers = self._draft.get("providers")
        if isinstance(providers, list):
            for provider in providers:
                if not isinstance(provider, Mapping):
                    continue
                keys = provider.get("api_keys", [])
                if not isinstance(keys, list):
                    continue
                if any(
                    isinstance(key, Mapping)
                    and isinstance(key.get("name"), str)
                    and bool(key["name"].strip())
                    and not (isinstance(key.get("value"), str) and bool(key["value"].strip()))
                    for key in keys
                ):
                    return {"valid": False, "errors": ["Every API key needs a value"]}
        try:
            with tempfile.TemporaryDirectory(prefix="litellm-core-provider-validate-") as directory:
                target = Path(directory) / "config.yaml"
                source = _mapping(document, "document")
                config_text = source.get("config")
                if not isinstance(config_text, str):
                    raise LegacyDomainError("Provider/model configuration is invalid")
                atomic_write_text(target, config_text)
                disabled = source.get("disabled")
                if isinstance(disabled, str):
                    atomic_write_text(target.with_name("config.disabled-models.yaml"), disabled)
                config_api.save_config(copy.deepcopy(providers), target, document=source)
        except LegacyDomainError:
            raise
        except Exception:
            return {"valid": False, "errors": ["Provider/model configuration is invalid"]}
        return {"valid": True, "errors": []}

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
            raise LegacyDomainError("Provider/model configuration is invalid")
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
                raise LegacyDomainError("Provider/model configuration changed on disk; reload before applying") from None
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
            raise LegacyDomainError("Provider/model configuration could not be inspected") from None
        return hashlib.sha256(encoded).hexdigest()

    def rebase_external_disk(self) -> dict[str, Any]:
        """Accept the on-disk revision while retaining the staged provider draft."""

        self._disk_revision = self._current_disk_revision()
        self.revision += 1
        return self.snapshot()

    def reload(self) -> dict[str, Any]:
        provider_bindings, model_bindings = self._editor_id_bindings()
        loaded = self._load()
        self._raw = {"providers": copy.deepcopy(loaded["providers"]), "document": copy.deepcopy(loaded["document"])}
        self._draft = copy.deepcopy(self._raw)
        self._disk_revision = copy.deepcopy(loaded["disk_revision"])
        self._exists = bool(loaded["exists"])
        self._restore_editor_id_bindings(provider_bindings, model_bindings)
        self._billing_overlay.clear()
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
                    for protocol in model.get("supported_upstream_url_surfaces", []):
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
                raise LegacyDomainError("The selected provider is unavailable")
            models = provider.get("models", [])
            if not isinstance(models, list):
                raise LegacyDomainError("The selected model is unavailable")
            model_index = self._model_index(provider, data)
            model = models[model_index]
            if not isinstance(model, Mapping):
                raise LegacyDomainError("The selected model is unavailable")
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
            raise LegacyDomainError("The selected provider is unavailable")
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
            raise LegacyDomainError("The selected provider is unavailable")
        model_index = self._model_index(provider, {"model_id": prepared.get("model_id")})
        models = provider.get("models", [])
        if not isinstance(models, list) or not isinstance(models[model_index], Mapping):
            raise LegacyDomainError("The selected model is unavailable")
        model = models[model_index]
        provider_name = str(provider.get("name", "")).strip()
        probe_key = self._probe_model_key(model)
        billing_key = self._billing_model_key(model)
        probe = overlay.get("probe")
        if not isinstance(probe, Mapping):
            raise LegacyDomainError("The model probe returned invalid state")
        self._probe_overlay.setdefault(provider_name, {})[probe_key] = copy.deepcopy(dict(probe))
        billing = overlay.get("billing")
        if isinstance(billing, Mapping):
            self._billing_overlay.setdefault(provider_name, {})[billing_key] = copy.deepcopy(dict(billing))
        self.revision += 1
        return result, True

    def probe(self, _payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        prepared = self.prepare_probe(_payload)
        result, _changed = self.commit_probe(prepared, self.perform_probe(prepared))
        return result


__all__ = ["ProvidersModelsDomain"]
