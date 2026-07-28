"""Core domain adapters for the pre-existing LiteLLM Menu Python services.

The React Native clients do not get a second settings implementation.  These
adapters deliberately keep the old Python modules as the parser, validator and
writer for their respective formats while presenting the small, staged domain
contract consumed by :class:`~litellm_menu.core.service.CoreStore`.

No adapter returns raw configuration text, credentials, or local paths from
``snapshot``.  Raw text remains in the Core process until a trusted explicit
``apply`` or ``export(include_sensitive=True)`` operation needs it.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Iterator

from ..persistence import PersistenceError, atomic_write_text, read_bytes
from ..security import REDACTED, REDACT_TEXT, redact, safe_exception_message


class LegacyDomainError(ValueError):
    """A deliberately source-safe error from a legacy-backed Core domain."""


def _mapping(value: object, label: str = "payload") -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LegacyDomainError(f"{label} must be an object")
    return dict(value)


def _copy_mapping(value: object, label: str = "payload") -> dict[str, Any]:
    return copy.deepcopy(_mapping(value, label))


def _action_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LegacyDomainError("A settings action is required")
    return value.strip().replace("-", "_").replace("/", "_").replace(".", "_").lower()


def _safe_problem(_: BaseException, fallback: str) -> LegacyDomainError:
    """Never pass source parser output (which can contain credentials) on."""

    return LegacyDomainError(fallback)


def _file_bytes(path: Path) -> bytes | None:
    try:
        return read_bytes(path)
    except PersistenceError as exc:
        raise LegacyDomainError(safe_exception_message(exc)) from None


def _same_file(path: Path, expected: bytes | None) -> bool:
    return _file_bytes(path) == expected


def _default_runtime_root() -> Path:
    configured = os.environ.get("LITELLM_RUNTIME_ROOT", "").strip() or os.environ.get(
        "LITELLM_MENU_HOME", ""
    ).strip()
    return Path(configured).expanduser() if configured else Path.home() / ".litellm-menu"


def _default_provider_config_path() -> Path:
    configured = os.environ.get("LITELLM_CONFIG_FILE", "").strip()
    return Path(configured).expanduser() if configured else _default_runtime_root() / "config.yaml"


def _default_runtime_settings_path() -> Path:
    configured = os.environ.get("LITELLM_MENU_RUNTIME_SETTINGS_FILE", "").strip()
    return Path(configured).expanduser() if configured else _default_runtime_root() / "runtime-settings.env"


def _default_webdav_enabled_path(settings_path: Path) -> Path:
    configured = os.environ.get("LITELLM_WEBDAV_SYNC_ENABLED_FILE", "").strip()
    return Path(configured).expanduser() if configured else settings_path.parent / ".litellm-runtime" / "webdav-sync.enabled"


def _selected_identifier(data: Mapping[str, Any], *keys: str) -> object:
    for key in keys:
        if key in data:
            return data[key]
    target = data.get("target")
    if isinstance(target, Mapping):
        for key in keys:
            if key in target:
                return target[key]
    return None


def _index(value: object, length: int, label: str) -> int:
    if type(value) is not int or value < 0 or value >= length:
        raise LegacyDomainError(f"The selected {label} is unavailable")
    return value


def _move(values: list[Any], source: object, destination: object, label: str) -> None:
    source_index = _index(source, len(values), label)
    if type(destination) is not int or destination < 0 or destination >= len(values):
        raise LegacyDomainError(f"The selected {label} destination is unavailable")
    item = values.pop(source_index)
    values.insert(destination, item)


def _direction_destination(source: int, length: int, data: Mapping[str, Any]) -> int:
    destination = data.get("to", data.get("destination"))
    if type(destination) is int:
        return destination
    direction = data.get("direction")
    if direction == "up":
        return max(0, source - 1)
    if direction == "down":
        return min(length - 1, source + 1)
    raise LegacyDomainError("A move destination is required")


class ProvidersModelsDomain:
    """Staged providers/models editing through ``config_editor_core``."""

    name = "providers_models"
    _MODEL_LIST_PROTOCOL = "openai-models-v1"
    _MODEL_LIST_TIMEOUT_SECONDS = 5.0
    _MAX_MODEL_LIST_BYTES = 512 * 1024
    _MAX_MODEL_CANDIDATES = 256
    _API_KEY_TARGET_SEPARATOR = "\x1f"

    def __init__(self, config_path: Path | str | None = None):
        self.config_path = Path(config_path).expanduser() if config_path else _default_provider_config_path()
        self._raw: dict[str, Any] = {}
        self._draft: dict[str, Any] = {}
        # Live billing is a transient, read-only projection.  It must never
        # enter the staged document because ``apply`` writes that document.
        self._billing_overlay: dict[str, dict[str, dict[str, Any]]] = {}
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
    def _billing_model_key(model: Mapping[str, Any]) -> str:
        """Return the same stable model identity used by provider_billing."""

        return str(model.get("deployment_id") or model.get("model_name") or "")

    @staticmethod
    def _safe_billing_overlay(model: Mapping[str, Any]) -> dict[str, Any]:
        """Keep only the UI billing fields from an optional remote response."""

        return {
            "billing": redact(
                {
                    key: model.get(key)
                    for key in ("status", "detail", "source", "balance", "group", "mode")
                    if model.get(key) is not None
                }
            ),
            "usage": redact(model.get("usage")) if model.get("usage") is not None else None,
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
        if isinstance(keys, Sequence) and not isinstance(keys, (str, bytes, bytearray)):
            for item in keys:
                if not isinstance(item, Mapping):
                    continue
                key_name = str(item.get("name", "")).strip()
                if key_name and key_name not in api_key_names:
                    api_key_names.append(key_name)
                key_value = item.get("value", "")
                configured_key = configured_key or (isinstance(key_value, str) and bool(key_value.strip()))
        models: list[dict[str, Any]] = []
        raw_models = provider.get("models", [])
        if isinstance(raw_models, Sequence) and not isinstance(raw_models, (str, bytes, bytearray)):
            for model_index, model in enumerate(raw_models):
                if not isinstance(model, Mapping):
                    continue
                model_key = bool(model.get("api_key")) or bool(model.get("api_key_name"))
                litellm_extra = model.get("litellm_extra", {})
                safe_litellm_extra = {
                    key: redact(value, _key=key)
                    for key, value in litellm_extra.items()
                    if isinstance(litellm_extra, Mapping)
                    and key in {"rpm", "tpm", "timeout", "allowed_openai_params", "drop_params", "additional_drop_params"}
                }
                live_billing = billing_overlay.get(self._billing_model_key(model), {})
                models.append(
                    {
                        "id": str(model.get("deployment_id") or model.get("model_name") or self._editor_id(model, model=True)),
                        "model_name": str(model.get("model_name", "")),
                        "name": str(model.get("model_name", "")),
                        "display_name": str(model.get("model_name", "")),
                        "litellm_model": str(model.get("litellm_model", "")),
                        "upstream_model": str(model.get("litellm_model", "")),
                        "provider": str(model.get("provider", name)),
                        "api_base": REDACT_TEXT(str(model.get("api_base", ""))),
                        "api_key_name": str(model.get("api_key_name", "")).strip(),
                        "enabled": model.get("enabled") is not False,
                        "model_enabled": model.get("model_enabled") is not False,
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
                        "usage": redact(live_billing.get("usage", model.get("usage")))
                        if live_billing.get("usage", model.get("usage")) is not None
                        else None,
                        "multiplier": redact(live_billing.get("multiplier", model.get("multiplier")))
                        if live_billing.get("multiplier", model.get("multiplier")) is not None
                        else None,
                    }
                )
        return {
            "id": name or self._editor_id(provider),
            "name": name,
            "enabled": provider.get("enabled") is not False,
            "api_base": REDACT_TEXT(str(provider.get("api_base", ""))),
            "api_key_configured": configured_key,
            "api_key_names": api_key_names,
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

    def _import_link(self, link: str) -> None:
        try:
            import external_provider_import

            self._stage_import_result(external_provider_import.import_link(link))
        except LegacyDomainError:
            raise
        except Exception as exc:
            raise _safe_problem(exc, "Provider configuration could not be imported") from None

    def _refresh_billing(self) -> None:
        try:
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
            safe_summary = redact(summary) if isinstance(summary, Mapping) else {}
            self._last_operation = {
                "operation": "billing",
                "available": True,
                "summary": safe_summary,
            }
        except Exception:
            # Billing is an optional remote read. Preserve both the editable
            # draft and any last-known display overlay, and never surface
            # remote diagnostics or credentials.
            self._last_operation = {"operation": "billing", "available": False}
            return

    def is_read_only_action(self, action: str, payload: object | None = None) -> bool:
        """Tell CoreStore that remote billing refresh does not stage config."""

        del payload
        return _action_name(action) in {"providers_refresh_billing", "provider_refresh_billing", "refresh_billing"}

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

        keys = cls._provider_api_keys(provider)
        if api_key_name is not None:
            selected_name = cls._api_key_name(api_key_name)
            key_index = cls._api_key_index(keys, selected_name)
            value = keys[key_index].get("value")
            if not isinstance(value, str) or not value.strip():
                raise LegacyDomainError("The selected API key has no value")
            return selected_name, value.strip()

        direct = provider.get("api_key")
        if isinstance(direct, str) and direct.strip():
            credential = direct.strip()
            matching_names = [
                str(item.get("name", "")).strip()
                for item in keys
                if isinstance(item.get("value"), str) and item["value"].strip() == credential
            ]
            selected_name = "default" if "default" in matching_names else (matching_names[0] if matching_names else "")
            return selected_name, credential

        configured: list[tuple[str, str]] = []
        for item in keys:
            value = item.get("value")
            if not isinstance(value, str) or not value.strip():
                continue
            configured.append((str(item.get("name", "")).strip(), value.strip()))
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
        provider = _copy_mapping(providers[provider_index], "provider")
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
        provider = _copy_mapping(providers[provider_index], "provider")
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
            return
        if action in {"provider_patch", "patch_provider"}:
            index = self._provider_index(data)
            provider = _copy_mapping(providers[index], "provider")
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
            if "api_keys" in changes:
                self._sync_primary_api_key(provider, self._provider_api_keys(provider))
            providers[index] = provider
            return
        if action in {"provider_clear_key", "clear_provider_key"}:
            index = self._provider_index(data)
            provider = _copy_mapping(providers[index], "provider")
            provider["api_keys"] = []
            provider["api_key"] = ""
            providers[index] = provider
            return
        if action in {"provider_delete", "delete_provider"}:
            providers.pop(self._provider_index(data))
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
        provider = _copy_mapping(providers[provider_index], "provider")
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
            model = _copy_mapping(models.pop(model_index), "model")
            destination_provider = _copy_mapping(providers[destination_index], "provider")
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
            providers[provider_index] = provider
            providers[destination_index] = destination_provider
            return
        if action == "model_duplicate":
            model_index = self._model_index(provider, data)
            model = _copy_mapping(models[model_index], "model")
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
            providers[provider_index] = provider
            return
        if action in {"model_add", "add_model"}:
            value = data.get("model", data.get("value", {}))
            model = _copy_mapping(value, "model")
            if "name" in model and "model_name" not in model:
                model["model_name"] = model.pop("name")
            if "upstream_model" in model and "litellm_model" not in model:
                model["litellm_model"] = model.pop("upstream_model")
            models.append(model)
        elif action in {"model_patch", "patch_model"}:
            index = self._model_index(provider, data)
            model = _copy_mapping(models[index], "model")
            changes = self._changes(data, "model")
            if "name" in changes:
                changes["model_name"] = changes.pop("name")
            if "upstream_model" in changes:
                changes["litellm_model"] = changes.pop("upstream_model")
            if isinstance(changes.get("litellm_extra"), Mapping):
                merged_extra = dict(model.get("litellm_extra", {})) if isinstance(model.get("litellm_extra"), Mapping) else {}
                merged_extra.update(changes["litellm_extra"])
                changes["litellm_extra"] = merged_extra
            model.update(changes)
            models[index] = model
        elif action in {"model_delete", "delete_model"}:
            models.pop(self._model_index(provider, data))
        elif action in {"model_move", "move_model"}:
            source = data.get("from", data.get("source"))
            if type(source) is not int:
                source = self._model_index(provider, data)
            _move(models, source, _direction_destination(source, len(models), data), "model")
        else:
            raise LegacyDomainError("The requested model action is unavailable")
        providers[provider_index] = provider

    def dispatch(self, action: str, payload: object | None = None) -> dict[str, Any]:
        name = _action_name(action)
        data = _mapping(payload or {})
        if name in {"set_raw", "setraw"}:
            self._set_raw(data)
        elif name in {"providers_import_selected", "provider_import_selected", "import_selected"}:
            self._import_selected(data)
        elif name in {"providers_import_codex_current", "provider_import_codex_current", "import_codex_current"}:
            self._import_codex_current()
        elif name in {"providers_refresh_billing", "provider_refresh_billing", "refresh_billing"}:
            self._refresh_billing()
        elif name in {"providers_fetch_models", "provider_fetch_models", "fetch_models"}:
            self._fetch_models(data)
        elif name in {"set", "replace"}:
            if "document" in data or any(key in data for key in ("config", "config_text", "raw_yaml", "text")):
                self._set_raw(data)
            else:
                self._replace_draft(data.get("providers", data), data.get("document"))
        elif name in {"reset", "cancel", "reload", "restore_defaults"}:
            self._draft = copy.deepcopy(self._raw)
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
            try:
                data = _mapping(payload)
                if "providers" in data or "document" in data:
                    self._replace_draft(data.get("providers", self._draft.get("providers", [])), data.get("document"))
                result = self._validate()
            finally:
                self._draft = before
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

    def reload(self) -> dict[str, Any]:
        loaded = self._load()
        self._raw = {"providers": copy.deepcopy(loaded["providers"]), "document": copy.deepcopy(loaded["document"])}
        self._draft = copy.deepcopy(self._raw)
        self._disk_revision = copy.deepcopy(loaded["disk_revision"])
        self._exists = bool(loaded["exists"])
        self._provider_editor_ids.clear()
        self._model_editor_ids.clear()
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

    def probe(self, _payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        result = self._validate()
        protocols: list[str] = []
        for provider in self._draft.get("providers", []):
            if not isinstance(provider, Mapping):
                continue
            for model in provider.get("models", []):
                if isinstance(model, Mapping):
                    for protocol in model.get("supported_upstream_url_surfaces", []):
                        if isinstance(protocol, str) and protocol not in protocols:
                            protocols.append(protocol)
        if not result["valid"]:
            return {"ok": False, "protocols": protocols, "detail": "Provider/model validation failed"}

        data = dict(_payload or {})
        selected = _selected_identifier(data, "provider_id", "provider", "name", "id", "index")
        providers = self._draft.get("providers", [])
        if selected is not None:
            indices = [self._provider_index(data)]
        else:
            indices = [
                index
                for index, provider in enumerate(providers)
                if isinstance(provider, Mapping) and self._provider_api_base(provider)
            ]
        if not indices:
            return {
                "ok": False,
                "protocols": protocols + [self._MODEL_LIST_PROTOCOL],
                "detail": "No provider model endpoint is configured",
                "models": [],
                "model_count": 0,
            }

        probes: list[dict[str, Any]] = []
        all_models: list[str] = []
        seen_models: set[str] = set()
        for index in indices:
            provider = providers[index]
            if not isinstance(provider, Mapping):
                continue
            fetched = self._fetch_provider_models(provider, self._safe_provider(provider, index)["id"])
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


_CODEX_ENVIRONMENT_LOCK = threading.RLock()


@contextmanager
def _codex_environment(runtime_config_path: Path, codex_home: Path | None) -> Iterator[None]:
    """Use codex_config's public functions without leaking env changes.

    The legacy module intentionally discovers CODEX_HOME at call time.  A
    narrow process-global lock makes an explicit Core test/host path safe
    without keeping a divergent implementation of its editor protocol.
    """

    with _CODEX_ENVIRONMENT_LOCK:
        old_home = os.environ.get("CODEX_HOME")
        old_runtime = os.environ.get("LITELLM_CONFIG_FILE")
        try:
            if codex_home is not None:
                os.environ["CODEX_HOME"] = str(codex_home)
            os.environ["LITELLM_CONFIG_FILE"] = str(runtime_config_path)
            yield
        finally:
            if old_home is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = old_home
            if old_runtime is None:
                os.environ.pop("LITELLM_CONFIG_FILE", None)
            else:
                os.environ["LITELLM_CONFIG_FILE"] = old_runtime


class CodexSettingsDomain:
    """Staged Codex TOML/JSON editor backed by ``codex_config``."""

    name = "codex"

    def __init__(
        self,
        runtime_config_path: Path | str | None = None,
        *,
        codex_home: Path | str | None = None,
    ):
        self.runtime_config_path = Path(runtime_config_path).expanduser() if runtime_config_path else _default_provider_config_path()
        self.codex_home = Path(codex_home).expanduser() if codex_home else None
        self._raw: dict[str, Any] = {}
        self._draft: dict[str, Any] = {}
        self._baseline: tuple[str, str] = ("", "{}\n")
        self.revision = 0
        self.reload()

    def _load_editor(self) -> dict[str, Any]:
        import codex_config

        try:
            with _codex_environment(self.runtime_config_path, self.codex_home):
                payload = codex_config.load_editor(self.runtime_config_path)
        except Exception as exc:
            raise _safe_problem(exc, "Codex settings could not be loaded") from None
        if not isinstance(payload, Mapping):
            raise LegacyDomainError("Codex settings are invalid")
        config_text = payload.get("config_text", "")
        auth_text = payload.get("auth_text", "{}\n")
        if not isinstance(config_text, str) or not isinstance(auth_text, str):
            raise LegacyDomainError("Codex settings are invalid")
        return copy.deepcopy(dict(payload))

    @staticmethod
    def _safe_snapshot(payload: Mapping[str, Any], revision: int) -> dict[str, Any]:
        errors = payload.get("validation_errors", [])
        warnings = payload.get("warnings", [])
        return {
            "domain": "codex",
            "revision": revision,
            "config_exists": bool(payload.get("config_exists")),
            "auth_file_exists": bool(payload.get("auth_exists")),
            "structured": redact(payload.get("structured", {})),
            "models": redact(payload.get("models", [])),
            "validation_errors": redact(errors if isinstance(errors, list) else []),
            "warnings": redact(warnings if isinstance(warnings, list) else []),
            "raw_editor_available": True,
        }

    def snapshot(self) -> dict[str, Any]:
        return self._safe_snapshot(self._draft, self.revision)

    def _sync(self, config_text: str, auth_text: str, patch: object | None = None) -> dict[str, Any]:
        import codex_config

        payload: dict[str, Any] = {"config_text": config_text, "auth_text": auth_text}
        if patch is not None:
            payload["patch"] = copy.deepcopy(patch)
        try:
            with _codex_environment(self.runtime_config_path, self.codex_home):
                result = codex_config.sync_editor(payload, self.runtime_config_path)
        except Exception as exc:
            raise _safe_problem(exc, "Codex settings are invalid") from None
        if not isinstance(result, Mapping) or not isinstance(result.get("config_text"), str) or not isinstance(result.get("auth_text"), str):
            raise LegacyDomainError("Codex settings are invalid")
        return copy.deepcopy(dict(result))

    def dispatch(self, action: str, payload: object | None = None) -> dict[str, Any]:
        name = _action_name(action)
        data = _mapping(payload or {})
        config_text = self._draft.get("config_text", "")
        auth_text = self._draft.get("auth_text", "{}\n")
        if not isinstance(config_text, str) or not isinstance(auth_text, str):
            raise LegacyDomainError("Codex settings are invalid")
        if name in {"set_raw", "setraw"}:
            document = data.get("document")
            text = data.get("text")
            next_config = data.get("config_text", data.get("raw_toml", data.get("toml", config_text)))
            next_auth = data.get("auth_text", data.get("raw_json", data.get("auth", auth_text)))
            if document in {"config", "config.toml", "toml"} and isinstance(text, str):
                next_config = text
            if document in {"auth", "auth.json", "json"} and isinstance(text, str):
                next_auth = text
            if not isinstance(next_config, str) or not isinstance(next_auth, str):
                raise LegacyDomainError("Codex editor text must be text")
            self._draft = self._sync(next_config, next_auth)
        elif name in {"set", "patch", "edit", "select_model", "selectmodel"}:
            if "config_text" in data or "auth_text" in data or "raw_toml" in data or "raw_json" in data:
                next_config = data.get("config_text", data.get("raw_toml", config_text))
                next_auth = data.get("auth_text", data.get("raw_json", auth_text))
                if not isinstance(next_config, str) or not isinstance(next_auth, str):
                    raise LegacyDomainError("Codex editor text must be text")
                self._draft = self._sync(next_config, next_auth)
            else:
                patch = data.get("patch", data.get("structured", data))
                if name in {"select_model", "selectmodel"} and "litellm_model" not in _mapping(patch):
                    patch = {"litellm_model": data.get("model", data.get("selection"))}
                self._draft = self._sync(config_text, auth_text, patch)
        elif name in {"reset", "cancel", "reload", "restore_defaults"}:
            self._draft = copy.deepcopy(self._raw)
        else:
            raise LegacyDomainError("The requested Codex action is unavailable")
        self.revision += 1
        return self.snapshot()

    def secret_present(self, field: str, target: str | None = None) -> bool:
        if field != "api_key" or target is not None:
            raise LegacyDomainError("The requested secret field is unavailable")
        structured = self._draft.get("structured", {})
        return isinstance(structured, Mapping) and bool(structured.get("api_key"))

    def stage_secret(self, field: str, target: str | None, value: str) -> None:
        if field != "api_key" or target is not None:
            raise LegacyDomainError("The requested secret field is unavailable")
        config_text = self._draft.get("config_text", "")
        auth_text = self._draft.get("auth_text", "{}\n")
        if not isinstance(config_text, str) or not isinstance(auth_text, str):
            raise LegacyDomainError("Codex settings are invalid")
        self._draft = self._sync(config_text, auth_text, {"api_key": value or None})
        self.revision += 1

    def validate(self, payload: object | None = None) -> dict[str, Any]:
        draft = self._draft
        if payload is not None:
            data = _mapping(payload)
            config_text = data.get("config_text", draft.get("config_text", ""))
            auth_text = data.get("auth_text", draft.get("auth_text", "{}\n"))
            if not isinstance(config_text, str) or not isinstance(auth_text, str):
                return {"valid": False, "errors": ["Codex editor text must be text"]}
            try:
                draft = self._sync(config_text, auth_text, data.get("patch", data.get("structured")))
            except LegacyDomainError:
                return {"valid": False, "errors": ["Codex settings are invalid"]}
        errors = draft.get("validation_errors", [])
        return {"valid": not bool(errors), "errors": list(errors) if isinstance(errors, list) else ["Codex settings are invalid"]}

    def apply(self, payload: object | None = None) -> dict[str, Any]:
        import codex_config

        if payload is not None:
            data = _mapping(payload)
            if "config_text" in data or "auth_text" in data:
                self.dispatch("set_raw", data)
        validation = self.validate()
        if not validation["valid"]:
            raise LegacyDomainError("Codex settings are invalid")
        current = self._load_editor()
        if (current.get("config_text"), current.get("auth_text")) != self._baseline:
            raise LegacyDomainError("Codex settings changed on disk; reload before applying")
        try:
            with _codex_environment(self.runtime_config_path, self.codex_home):
                codex_config.apply_editor(
                    {
                        "config_text": self._draft["config_text"],
                        "auth_text": self._draft["auth_text"],
                    },
                    self.runtime_config_path,
                )
        except Exception as exc:
            raise _safe_problem(exc, "Codex settings could not be saved") from None
        self.reload()
        return {"applied": True, **self.snapshot()}

    def reload(self) -> dict[str, Any]:
        payload = self._load_editor()
        self._raw = copy.deepcopy(payload)
        self._draft = copy.deepcopy(payload)
        self._baseline = (str(payload.get("config_text", "")), str(payload.get("auth_text", "{}\n")))
        self.revision += 1
        return self.snapshot()

    def export(self, *, include_sensitive: bool = False) -> dict[str, Any]:
        if include_sensitive:
            return {
                "domain": self.name,
                "config_text": self._draft.get("config_text", ""),
                "auth_text": self._draft.get("auth_text", "{}\n"),
            }
        return self.snapshot()

    def import_package(self, payload: object) -> None:
        data = _mapping(payload, "Codex package")
        self.dispatch("set_raw", data)

    def probe(self, _payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        result = self.validate()
        return {"ok": bool(result["valid"]), "protocols": [], "detail": "Codex validation completed" if result["valid"] else "Codex validation failed"}


class RuntimeSettingsDomain:
    """Staged runtime settings backed by ``runtime_settings_io`` validation."""

    name = "runtime"
    _SECRET_KEYS = frozenset({"LITELLM_MENU_VISION_BRIDGE_API_KEY"})

    def __init__(self, settings_path: Path | str | None = None):
        self.settings_path = Path(settings_path).expanduser() if settings_path else _default_runtime_settings_path()
        self.specs: dict[str, Any] = {}
        self._metadata: dict[str, dict[str, Any]] = {}
        self._raw_values: dict[str, str] = {}
        self._draft_values: dict[str, str] = {}
        self._baseline_bytes: bytes | None = None
        self.revision = 0
        self.reload()

    @staticmethod
    def _defaults(specs: Mapping[str, Any]) -> dict[str, str]:
        from runtime_settings_io import normalize_payload_value

        return {key: normalize_payload_value(spec, spec.default) for key, spec in specs.items()}

    def _load(self) -> tuple[dict[str, Any], dict[str, str], bytes | None]:
        from runtime_settings_io import load_specs, read_settings_file

        from ..runtime_settings_schema import runtime_settings_metadata

        try:
            specs = load_specs()
            metadata_items = runtime_settings_metadata()
            metadata = {
                str(item["key"]): copy.deepcopy(item)
                for item in metadata_items
                if isinstance(item, Mapping) and isinstance(item.get("key"), str)
            }
            baseline = _file_bytes(self.settings_path)
            values = read_settings_file(self.settings_path, specs)
        except LegacyDomainError:
            raise
        except Exception as exc:
            raise _safe_problem(exc, "Runtime settings could not be loaded") from None
        self._metadata = metadata
        return specs, values, baseline

    def _field_projection(self, key: str, spec: Any, value: str) -> dict[str, Any]:
        default = self._defaults({key: spec})[key]
        metadata = self._metadata.get(key, {})
        is_secret = key in self._SECRET_KEYS or metadata.get("secret") is True
        baseline_value = self._raw_values.get(key, default)
        ui_kind = {
            "bool": "toggle",
            "bool_auto": "toggle",
            "enum": "choice",
            "int": "integer",
            "float": "number",
            "mb": "number",
            "string": "text",
        }.get(spec.kind, spec.kind)
        options = list(spec.options)
        if spec.kind == "bool_auto" and not options:
            options = ["auto", "off"]
        return {
            "id": key,
            "key": key,
            "kind": ui_kind,
            "storage_kind": spec.kind,
            "category": str(metadata.get("category", "Runtime")),
            "label": str(metadata.get("label", key)),
            "unit": str(metadata.get("unit", "")),
            "help": str(metadata.get("help", "")),
            "default": spec.default,
            "minimum": spec.minimum,
            "maximum": spec.maximum,
            "options": options,
            "value": REDACTED if is_secret and value else value,
            "configured": value != default,
            "retained": is_secret and baseline_value != default,
            "will_clear": is_secret and baseline_value != default and value == default,
            "secret": is_secret,
            "retain_existing": str(metadata.get("retain_existing", "")),
        }

    def _is_secret_setting(self, key: str) -> bool:
        return key in self._SECRET_KEYS or self._metadata.get(key, {}).get("secret") is True

    def snapshot(self) -> dict[str, Any]:
        fields = [self._field_projection(key, spec, self._draft_values.get(key, "")) for key, spec in self.specs.items()]
        values = {
            key: (REDACTED if key in self._SECRET_KEYS and value else value)
            for key, value in self._draft_values.items()
        }
        return {
            "domain": self.name,
            "revision": self.revision,
            "fields": fields,
            "settings": fields,
            "values": values,
            "raw_editor_available": True,
        }

    def _validate_values(self, values: Mapping[str, object]) -> dict[str, str]:
        from runtime_settings_io import validate_values

        try:
            return validate_values(dict(values), self.specs)
        except Exception as exc:
            raise _safe_problem(exc, "Runtime settings are invalid") from None

    def _set_values(self, value: object) -> None:
        data = _mapping(value, "runtime values")
        values = data.get("values", data)
        updates = _mapping(values, "runtime values")
        draft = copy.deepcopy(self._draft_values)
        for key, item in updates.items():
            if key not in self.specs:
                raise LegacyDomainError("Runtime settings contain an unsupported field")
            if item == "__LITELLM_MENU_RETAIN_EXISTING__":
                if key not in self._SECRET_KEYS:
                    raise LegacyDomainError("Runtime settings are invalid")
                continue
            if not isinstance(item, str):
                raise LegacyDomainError("Runtime setting values must be text")
            draft[key] = item
        self._draft_values = self._validate_values(draft)

    def _set_setting(self, data: Mapping[str, Any]) -> None:
        key = data.get("key")
        if not isinstance(key, str) or key not in self.specs:
            raise LegacyDomainError("Runtime settings contain an unsupported field")
        value = data.get("value")
        if isinstance(value, bool):
            if self.specs[key].kind == "bool_auto":
                value = "auto" if value else "off"
            elif self.specs[key].kind == "bool":
                value = "1" if value else "0"
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str):
            raise LegacyDomainError("Runtime setting values must be text")
        self._set_values({"values": {key: value}})

    def _clear_setting(self, data: Mapping[str, Any]) -> None:
        key = data.get("key")
        if not isinstance(key, str) or key not in self.specs:
            raise LegacyDomainError("Runtime settings contain an unsupported field")
        self._draft_values[key] = self._defaults({key: self.specs[key]})[key]

    def _set_raw(self, data: Mapping[str, Any]) -> None:
        source = data.get("raw_text", data.get("text", data.get("settings_text")))
        if not isinstance(source, str):
            raise LegacyDomainError("Runtime settings text must be text")
        try:
            with tempfile.TemporaryDirectory(prefix="litellm-core-runtime-validate-") as directory:
                path = Path(directory) / "runtime-settings.env"
                atomic_write_text(path, source)
                from runtime_settings_io import read_settings_file

                values = read_settings_file(path, self.specs)
        except Exception as exc:
            raise _safe_problem(exc, "Runtime settings are invalid") from None
        self._draft_values = values

    def dispatch(self, action: str, payload: object | None = None) -> dict[str, Any]:
        name = _action_name(action)
        data = _mapping(payload or {})
        if name in {"set", "patch", "replace"}:
            self._set_values(data)
        elif name in {"set_setting", "setsetting"}:
            self._set_setting(data)
        elif name in {"clear_setting", "clearsetting"}:
            self._clear_setting(data)
        elif name in {"set_raw", "setraw"}:
            self._set_raw(data)
        elif name in {"restore_defaults", "defaults"}:
            self._draft_values = self._defaults(self.specs)
        elif name in {"reset", "cancel", "reload"}:
            self._draft_values = copy.deepcopy(self._raw_values)
        else:
            raise LegacyDomainError("The requested runtime action is unavailable")
        self.revision += 1
        return self.snapshot()

    def secret_present(self, field: str, target: str | None = None) -> bool:
        if field != "setting" or not isinstance(target, str) or target not in self.specs:
            raise LegacyDomainError("The requested secret field is unavailable")
        if not self._is_secret_setting(target):
            raise LegacyDomainError("The requested secret field is unavailable")
        return bool(self._draft_values.get(target, ""))

    def stage_secret(self, field: str, target: str | None, value: str) -> None:
        if field != "setting" or not isinstance(target, str) or target not in self.specs:
            raise LegacyDomainError("The requested secret field is unavailable")
        if not self._is_secret_setting(target):
            raise LegacyDomainError("The requested secret field is unavailable")
        self._set_values({"values": {target: value}})
        self.revision += 1

    def validate(self, payload: object | None = None) -> dict[str, Any]:
        try:
            values = self._draft_values
            if payload is not None:
                data = _mapping(payload)
                values = copy.deepcopy(values)
                updates = _mapping(data.get("values", data), "runtime values")
                values.update(updates)
            self._validate_values(values)
        except LegacyDomainError:
            return {"valid": False, "errors": ["Runtime settings are invalid"]}
        return {"valid": True, "errors": []}

    @staticmethod
    def _stored_value(spec: Any, value: str) -> str:
        if spec.kind != "mb":
            return value
        try:
            bytes_value = round(float(value) * 1024 * 1024)
        except (TypeError, ValueError, OverflowError):
            raise LegacyDomainError("Runtime settings are invalid") from None
        return str(bytes_value)

    def _encoded_draft(self) -> str:
        normalized = self._validate_values(self._draft_values)
        defaults = self._defaults(self.specs)
        lines = ["# LiteLLM Menu runtime thresholds. Generated by Python Core."]
        for key, spec in self.specs.items():
            if normalized[key] == defaults[key]:
                continue
            lines.append(f"{key}={self._stored_value(spec, normalized[key])}")
        return "\n".join(lines) + "\n"

    def apply(self, payload: object | None = None) -> dict[str, Any]:
        if payload is not None:
            data = _mapping(payload)
            if "values" in data:
                self._set_values(data)
        validation = self.validate()
        if not validation["valid"]:
            raise LegacyDomainError("Runtime settings are invalid")
        if not _same_file(self.settings_path, self._baseline_bytes):
            raise LegacyDomainError("Runtime settings changed on disk; reload before applying")
        try:
            atomic_write_text(self.settings_path, self._encoded_draft())
        except PersistenceError as exc:
            raise LegacyDomainError(safe_exception_message(exc)) from None
        self.reload()
        return {"applied": True, **self.snapshot()}

    def reload(self) -> dict[str, Any]:
        specs, values, baseline = self._load()
        self.specs = specs
        self._raw_values = copy.deepcopy(values)
        self._draft_values = copy.deepcopy(values)
        self._baseline_bytes = baseline
        self.revision += 1
        return self.snapshot()

    def export(self, *, include_sensitive: bool = False) -> dict[str, Any]:
        if include_sensitive:
            return {"domain": self.name, "values": copy.deepcopy(self._draft_values)}
        return self.snapshot()

    def import_package(self, payload: object) -> None:
        data = _mapping(payload, "runtime package")
        self._set_values(data.get("values", data))
        self.revision += 1

    def probe(self, _payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        validation = self.validate()
        return {"ok": bool(validation["valid"]), "protocols": [], "detail": "Runtime validation completed" if validation["valid"] else "Runtime validation failed"}


class WebDAVSettingsDomain:
    """Staged WebDAV settings using the existing WebDAV core client."""

    name = "webdav"

    def __init__(
        self,
        settings_path: Path | str | None = None,
        *,
        enabled_path: Path | str | None = None,
    ):
        from webdav import core as webdav_core

        self.settings_path = Path(settings_path).expanduser() if settings_path else webdav_core.default_settings_file()
        self.enabled_path = Path(enabled_path).expanduser() if enabled_path else _default_webdav_enabled_path(self.settings_path)
        self._raw_settings: dict[str, Any] = {}
        self._draft_settings: dict[str, Any] = {}
        self._raw_enabled = False
        self._draft_enabled = False
        self._baseline_settings: bytes | None = None
        self._baseline_enabled: bool = False
        self._last_probe = "unknown"
        self.revision = 0
        self.reload()

    @staticmethod
    def _settings_raw(settings: object) -> dict[str, Any]:
        return {
            "url": str(getattr(settings, "url", "")),
            "username": str(getattr(settings, "username", "")),
            "password": str(getattr(settings, "password", "")),
            "remote_name": str(getattr(settings, "remote_name", "")),
            "sync_interval_minutes": getattr(settings, "sync_interval_minutes", 30),
            "timeout_seconds": getattr(settings, "timeout_seconds", 30),
        }

    def _enabled(self) -> bool:
        try:
            details = self.enabled_path.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            raise LegacyDomainError("WebDAV enablement state is unavailable") from None
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise LegacyDomainError("WebDAV enablement state is unavailable")
        return True

    def _load(self) -> tuple[dict[str, Any], bool, bytes | None]:
        from webdav import core as webdav_core

        try:
            baseline = _file_bytes(self.settings_path)
            settings = webdav_core.load_settings(self.settings_path)
            enabled = self._enabled()
        except LegacyDomainError:
            raise
        except Exception as exc:
            raise _safe_problem(exc, "WebDAV settings could not be loaded") from None
        return self._settings_raw(settings), enabled, baseline

    def _settings(self, raw: Mapping[str, Any] | None = None) -> Any:
        from webdav import core as webdav_core

        try:
            return webdav_core._settings_from_raw(dict(raw or self._draft_settings))
        except Exception as exc:
            raise _safe_problem(exc, "WebDAV settings are invalid") from None

    def snapshot(self) -> dict[str, Any]:
        settings = self._settings()
        sanitized = settings.sanitized()
        password_present = bool(self._draft_settings.get("password"))
        return {
            "domain": self.name,
            "revision": self.revision,
            "enabled": self._draft_enabled,
            "configured": bool(settings.configured),
            "url": sanitized["url"],
            "username": sanitized["username"],
            "remote_name": sanitized["remote_name"],
            "sync_interval_minutes": sanitized["sync_interval_minutes"],
            "sync_interval": sanitized["sync_interval_minutes"],
            "timeout_seconds": sanitized["timeout_seconds"],
            "timeout": sanitized["timeout_seconds"],
            # The Core projection redacts this field again; its only purpose
            # is to give the summary a presence bit without carrying a value.
            "password": REDACTED if password_present else "",
            "password_configured": password_present,
            "last_probe": self._last_probe,
        }

    def _patch(self, data: Mapping[str, Any]) -> None:
        allowed = {"url", "username", "password", "remote_name", "sync_interval_minutes", "timeout_seconds"}
        source = data.get("settings", data.get("values", data))
        updates = _mapping(source, "WebDAV settings")
        aliases = {
            "sync_interval": "sync_interval_minutes",
            "timeout": "timeout_seconds",
        }
        updates = {aliases.get(key, key): value for key, value in updates.items()}
        unknown = set(updates).difference(allowed | {"enabled", "keep_password", "clear_password"})
        if unknown:
            raise LegacyDomainError("WebDAV settings contain an unsupported field")
        draft = copy.deepcopy(self._draft_settings)
        for key in allowed:
            if key not in updates:
                continue
            value = updates[key]
            if key == "password" and value == "__LITELLM_MENU_RETAIN_EXISTING__":
                continue
            draft[key] = value
        if updates.get("clear_password") is True:
            draft["password"] = ""
        if "enabled" in updates:
            self._draft_enabled = bool(updates["enabled"])
        self._settings(draft)
        self._draft_settings = draft

    def dispatch(self, action: str, payload: object | None = None) -> dict[str, Any]:
        name = _action_name(action)
        data = _mapping(payload or {})
        if name in {"set", "patch", "replace", "set_raw", "setraw"}:
            self._patch(data)
        elif name in {"clear_password", "clearpassword"}:
            self._draft_settings["password"] = ""
        elif name in {"enable", "webdav_enable"}:
            self._draft_enabled = True
        elif name in {"disable", "webdav_disable"}:
            self._draft_enabled = False
        elif name in {"restore_defaults", "defaults"}:
            self._draft_settings = {
                "url": "",
                "username": "",
                "password": "",
                "remote_name": "litellm-menu-config.json",
                "sync_interval_minutes": 30,
                "timeout_seconds": 30,
            }
            self._draft_enabled = False
        elif name in {"reset", "cancel", "reload"}:
            self._draft_settings = copy.deepcopy(self._raw_settings)
            self._draft_enabled = self._raw_enabled
        else:
            raise LegacyDomainError("The requested WebDAV action is unavailable")
        self.revision += 1
        return self.snapshot()

    def secret_present(self, field: str, target: str | None = None) -> bool:
        if field != "password" or target is not None:
            raise LegacyDomainError("The requested secret field is unavailable")
        return bool(self._draft_settings.get("password"))

    def stage_secret(self, field: str, target: str | None, value: str) -> None:
        if field != "password" or target is not None:
            raise LegacyDomainError("The requested secret field is unavailable")
        draft = copy.deepcopy(self._draft_settings)
        draft["password"] = value
        self._settings(draft)
        self._draft_settings = draft
        self.revision += 1

    def validate(self, payload: object | None = None) -> dict[str, Any]:
        try:
            raw = self._draft_settings
            enabled = self._draft_enabled
            if payload is not None:
                data = _mapping(payload)
                raw = copy.deepcopy(raw)
                updates = _mapping(data.get("settings", data.get("values", data)), "WebDAV settings")
                raw.update({key: value for key, value in updates.items() if key in raw})
                enabled = bool(updates.get("enabled", enabled))
            settings = self._settings(raw)
            if enabled and not settings.configured:
                raise LegacyDomainError("WebDAV URL is required before enabling sync")
        except LegacyDomainError:
            return {"valid": False, "errors": ["WebDAV settings are invalid"]}
        return {"valid": True, "errors": []}

    def _write_enabled(self, enabled: bool) -> None:
        if enabled:
            try:
                atomic_write_text(self.enabled_path, "1\n")
            except PersistenceError as exc:
                raise LegacyDomainError(safe_exception_message(exc)) from None
            return
        try:
            details = self.enabled_path.lstat()
        except FileNotFoundError:
            return
        except OSError:
            raise LegacyDomainError("WebDAV enablement state could not be saved") from None
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise LegacyDomainError("WebDAV enablement state could not be saved")
        try:
            self.enabled_path.unlink()
        except OSError:
            raise LegacyDomainError("WebDAV enablement state could not be saved") from None

    def apply(self, payload: object | None = None) -> dict[str, Any]:
        from webdav import core as webdav_core

        if payload is not None:
            data = _mapping(payload)
            if data:
                self._patch(data)
        validation = self.validate()
        if not validation["valid"]:
            raise LegacyDomainError("WebDAV settings are invalid")
        if not _same_file(self.settings_path, self._baseline_settings) or self._enabled() != self._baseline_enabled:
            raise LegacyDomainError("WebDAV settings changed on disk; reload before applying")
        settings = self._settings()
        try:
            # Save through the existing module so URL normalization and its
            # on-disk compatibility remain exactly the same as the old app.
            webdav_core.save_settings(self.settings_path, settings)
            self._write_enabled(self._draft_enabled)
        except LegacyDomainError:
            raise
        except Exception as exc:
            raise _safe_problem(exc, "WebDAV settings could not be saved") from None
        self.reload()
        return {"applied": True, **self.snapshot()}

    def reload(self) -> dict[str, Any]:
        settings, enabled, baseline = self._load()
        self._raw_settings = copy.deepcopy(settings)
        self._draft_settings = copy.deepcopy(settings)
        self._raw_enabled = enabled
        self._draft_enabled = enabled
        self._baseline_settings = baseline
        self._baseline_enabled = enabled
        self.revision += 1
        return self.snapshot()

    def export(self, *, include_sensitive: bool = False) -> dict[str, Any]:
        if include_sensitive:
            return {"domain": self.name, "settings": copy.deepcopy(self._draft_settings), "enabled": self._draft_enabled}
        return self.snapshot()

    def import_package(self, payload: object) -> None:
        data = _mapping(payload, "WebDAV package")
        self._patch(data)
        if "enabled" in data:
            self._draft_enabled = bool(data["enabled"])
        self.revision += 1

    def probe(self, _payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        from webdav import core as webdav_core

        try:
            settings = self._settings()
            if not settings.configured:
                raise LegacyDomainError("WebDAV is not configured")
            client = webdav_core.WebDAVClient(settings)
            client.try_mkcol(webdav_core.collection_url(settings))
            try:
                client.head(webdav_core.bundle_url(settings))
            except webdav_core.WebDAVHTTPError as exc:
                if exc.code != 404:
                    raise
            self._last_probe = "ok"
            return {"ok": True, "protocols": ["webdav"], "detail": "WebDAV probe succeeded"}
        except Exception:
            self._last_probe = "failed"
            return {"ok": False, "protocols": ["webdav"], "detail": "WebDAV probe failed"}


__all__ = [
    "CodexSettingsDomain",
    "LegacyDomainError",
    "ProvidersModelsDomain",
    "RuntimeSettingsDomain",
    "WebDAVSettingsDomain",
]
