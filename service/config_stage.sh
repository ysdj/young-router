# shellcheck shell=bash

validate_config_file() {
  local path="$1"
  "$PYTHON" - "$path" <<'PY'
import pathlib
import re
import sys

try:
    import yaml
except Exception as exc:
    print(f"PyYAML is required to validate config.yaml: {exc}", file=sys.stderr)
    sys.exit(1)

path = pathlib.Path(sys.argv[1])
data = yaml.safe_load(path.read_text(encoding="utf-8"))
if not isinstance(data, dict):
    print("config.yaml must be a YAML mapping", file=sys.stderr)
    sys.exit(1)
if "model_list" not in data or not isinstance(data["model_list"], list):
    print("config.yaml must contain model_list", file=sys.stderr)
    sys.exit(1)
settings = data.get("litellm_settings") if isinstance(data.get("litellm_settings"), dict) else {}
callbacks = settings.get("callbacks")
if isinstance(callbacks, list):
    for callback in callbacks:
        callback_path = "" if callback is None else str(callback).strip()
        if callback_path and callback_path != "litellm_menu.callbacks.image_generation_routing_hook":
            print(
                "config.yaml litellm_settings.callbacks contains unsupported callback "
                f"{callback_path}; use litellm_menu.callbacks.image_generation_routing_hook",
                file=sys.stderr,
            )
            sys.exit(1)
providers = data.get("providers") if isinstance(data.get("providers"), dict) else {}
for provider_name, raw_provider in providers.items():
    provider = raw_provider if isinstance(raw_provider, dict) else {}
    if "api_key" in provider:
        print(
            f"config.yaml provider {provider_name} uses unsupported scalar api_key; "
            "use api_keys: [{name, value, enabled}]",
            file=sys.stderr,
        )
        sys.exit(1)
    if "disabled_api_keys" in provider:
        print(
            f"config.yaml provider {provider_name} uses unsupported disabled_api_keys; "
            "put enabled: false on the matching api_keys entry",
            file=sys.stderr,
        )
        sys.exit(1)
    raw_keys = provider.get("api_keys")
    if raw_keys is None:
        continue
    if not isinstance(raw_keys, list):
        print(f"config.yaml provider {provider_name} api_keys must be a list of objects", file=sys.stderr)
        sys.exit(1)
    for index, raw_key in enumerate(raw_keys, start=1):
        key = raw_key if isinstance(raw_key, dict) else {}
        if not key:
            print(f"config.yaml provider {provider_name} api_keys[{index}] must be an object", file=sys.stderr)
            sys.exit(1)
        if "api_key" in key:
            print(
                f"config.yaml provider {provider_name} api_keys[{index}] uses unsupported api_key; use value",
                file=sys.stderr,
            )
            sys.exit(1)
        if not str(key.get("value") or "").strip():
            print(f"config.yaml provider {provider_name} api_keys[{index}] needs value", file=sys.stderr)
            sys.exit(1)
for section_name in ("model_list", "disabled_model_list"):
    items = data.get(section_name)
    if items is None:
        continue
    if not isinstance(items, list):
        print(f"config.yaml {section_name} must be a list", file=sys.stderr)
        sys.exit(1)
    for index, raw_model in enumerate(items, start=1):
        model = raw_model if isinstance(raw_model, dict) else {}
        model_info = model.get("model_info") if isinstance(model.get("model_info"), dict) else {}
        for unsupported_key in (
            "upstream_api_mode",
            "supported_upstream_api_modes",
            "supports_image_generation",
        ):
            if unsupported_key in model_info:
                replacement = (
                    "supports_responses_image_generation_tool"
                    if unsupported_key == "supports_image_generation"
                    else "upstream_url_surface/supported_upstream_url_surfaces"
                )
                print(
                    f"config.yaml {section_name}[{index}] uses unsupported {unsupported_key}; "
                    f"use {replacement}",
                    file=sys.stderr,
                )
                sys.exit(1)
        deployment_id = str(model_info.get("id") or "").strip()
        if deployment_id and not re.fullmatch(r"[0-9a-f]{8}", deployment_id):
            print(
                f"config.yaml {section_name}[{index}] model_info.id must be an 8 character hex deployment token",
                file=sys.stderr,
            )
            sys.exit(1)
print(f"config.yaml OK: {len(data['model_list'])} model entries")
PY
}

remove_legacy_runtime_callback_artifacts() {
  rm -f \
    "$RUNTIME_DIR/callbacks.py" \
    "$RUNTIME_DIR/litellm_hooks.py" \
    "$RUNTIME_DIR/__pycache__/callbacks.cpython-"*.pyc \
    "$RUNTIME_DIR/__pycache__/litellm_hooks.cpython-"*.pyc
}

sync_runtime_config() {
  if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Missing config file: $CONFIG_FILE" >&2
    return 1
  fi

  mkdir -p "$RUNTIME_DIR"
  remove_legacy_runtime_callback_artifacts
  if [[ ! -f "$CALLBACK_SOURCE" ]]; then
    echo "Missing LiteLLM callback file: $CALLBACK_SOURCE" >&2
    return 1
  fi
  if [[ ! -d "$CALLBACK_PACKAGE_DIR" ]]; then
    echo "Missing LiteLLM callback package: $CALLBACK_PACKAGE_DIR" >&2
    return 1
  fi

  "$PYTHON" - "$CONFIG_FILE" "$RUNTIME_CONFIG" "$MIRROR_CHECKOUT_CONFIG_TO_ROOT" "$ROOT/config.yaml" <<'PY'
from __future__ import annotations

import os
import pathlib
import re
import sys
import tempfile

try:
    import yaml
except Exception as exc:
    print(f"PyYAML is required to validate config.yaml: {exc}", file=sys.stderr)
    sys.exit(1)


def atomic_write(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.tmp.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def as_dict(value):
    return value if isinstance(value, dict) else {}


def text(value):
    return "" if value is None else str(value).strip()


def validate_current_schema(data):
    settings = as_dict(data.get("litellm_settings"))
    callbacks = settings.get("callbacks")
    if isinstance(callbacks, list):
        for callback in callbacks:
            callback_path = text(callback)
            if callback_path and callback_path != "litellm_menu.callbacks.image_generation_routing_hook":
                print(
                    "config.yaml litellm_settings.callbacks contains unsupported callback "
                    f"{callback_path}; use litellm_menu.callbacks.image_generation_routing_hook",
                    file=sys.stderr,
                )
                sys.exit(1)

    providers = as_dict(data.get("providers"))
    for provider_name, raw_provider in providers.items():
        provider = as_dict(raw_provider)
        if "api_key" in provider:
            print(
                f"config.yaml provider {provider_name} uses unsupported scalar api_key; "
                "use api_keys: [{name, value, enabled}]",
                file=sys.stderr,
            )
            sys.exit(1)
        if "disabled_api_keys" in provider:
            print(
                f"config.yaml provider {provider_name} uses unsupported disabled_api_keys; "
                "put enabled: false on the matching api_keys entry",
                file=sys.stderr,
            )
            sys.exit(1)
        raw_keys = provider.get("api_keys")
        if raw_keys is None:
            continue
        if not isinstance(raw_keys, list):
            print(f"config.yaml provider {provider_name} api_keys must be a list of objects", file=sys.stderr)
            sys.exit(1)
        for index, raw_key in enumerate(raw_keys, start=1):
            key = as_dict(raw_key)
            if not key:
                print(f"config.yaml provider {provider_name} api_keys[{index}] must be an object", file=sys.stderr)
                sys.exit(1)
            if "api_key" in key:
                print(
                    f"config.yaml provider {provider_name} api_keys[{index}] uses unsupported api_key; use value",
                    file=sys.stderr,
                )
                sys.exit(1)
            if not text(key.get("value")):
                print(f"config.yaml provider {provider_name} api_keys[{index}] needs value", file=sys.stderr)
                sys.exit(1)

    for section_name in ("model_list", "disabled_model_list"):
        items = data.get(section_name)
        if items is None:
            continue
        if not isinstance(items, list):
            print(f"config.yaml {section_name} must be a list", file=sys.stderr)
            sys.exit(1)
        for index, raw_model in enumerate(items, start=1):
            model_info = as_dict(as_dict(raw_model).get("model_info"))
            for unsupported_key in (
                "upstream_api_mode",
                "supported_upstream_api_modes",
                "supports_image_generation",
            ):
                if unsupported_key in model_info:
                    replacement = (
                        "supports_responses_image_generation_tool"
                        if unsupported_key == "supports_image_generation"
                        else "upstream_url_surface/supported_upstream_url_surfaces"
                    )
                    print(
                        f"config.yaml {section_name}[{index}] uses unsupported {unsupported_key}; "
                        f"use {replacement}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
            deployment_id = text(model_info.get("id"))
            if deployment_id and not re.fullmatch(r"[0-9a-f]{8}", deployment_id):
                print(
                    f"config.yaml {section_name}[{index}] model_info.id must be an 8 character hex deployment token",
                    file=sys.stderr,
                )
                sys.exit(1)


def remove_legacy_context_metadata(data):
    changed = False
    for section_name in ("model_list", "disabled_model_list"):
        items = data.get(section_name)
        if not isinstance(items, list):
            continue
        for raw_model in items:
            if not isinstance(raw_model, dict):
                continue
            model_info = raw_model.get("model_info")
            if not isinstance(model_info, dict):
                continue
            for key in (
                "max_input_tokens",
                "context_metadata_source",
                "context_metadata_model_id",
            ):
                if key in model_info:
                    model_info.pop(key, None)
                    changed = True
    return changed

source_path = pathlib.Path(sys.argv[1])
runtime_path = pathlib.Path(sys.argv[2])
mirror_to_root = sys.argv[3] == "1"
root_config_path = pathlib.Path(sys.argv[4])

source_text = source_path.read_text(encoding="utf-8")
data = yaml.safe_load(source_text)
if not isinstance(data, dict):
    print("config.yaml must be a YAML mapping", file=sys.stderr)
    sys.exit(1)
if "model_list" not in data or not isinstance(data["model_list"], list):
    print("config.yaml must contain model_list", file=sys.stderr)
    sys.exit(1)
validate_current_schema(data)
removed_legacy_context = remove_legacy_context_metadata(data)
if removed_legacy_context:
    runtime_text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    atomic_write(runtime_path, runtime_text.encode("utf-8"))
    print("Removed legacy context metadata from staged runtime config")
else:
    runtime_text = source_text
    atomic_write(runtime_path, source_text.encode("utf-8"))
if mirror_to_root and source_path.resolve() != root_config_path.resolve():
    atomic_write(root_config_path, runtime_text.encode("utf-8"))
    print(f"Mirrored checkout config to {root_config_path}")
print(f"config.yaml OK: {len(data['model_list'])} model entries")
PY
}
