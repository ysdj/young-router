from __future__ import annotations

import importlib
import os
import urllib.request
from typing import Any, Optional


_IMAGE_EDIT_USAGE_PATCH_ATTR = "_openai_image_edit_usage_patch"
_CONFIG_CALLBACK_IMPORT_PATCH_ATTR = "_litellm_menu_config_callback_import_patch"
_SYSTEM_PROXY_LOOKUP_PATCH_ATTR = "_litellm_menu_system_proxy_lookup_patch"


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _int_or_zero(value: Any) -> int:
    converted = _int_or_none(value)
    return converted if converted is not None else 0


def _normalize_image_response_usage(response_json: Any) -> Any:
    if not isinstance(response_json, dict):
        return response_json

    usage = response_json.get("usage")
    if not isinstance(usage, dict):
        return response_json

    normalized_usage = dict(usage)
    prompt_tokens = _int_or_none(normalized_usage.get("prompt_tokens"))
    completion_tokens = _int_or_none(normalized_usage.get("completion_tokens"))

    input_tokens = _int_or_none(normalized_usage.get("input_tokens"))
    if input_tokens is None:
        input_tokens = prompt_tokens if prompt_tokens is not None else 0
        normalized_usage["input_tokens"] = input_tokens

    output_tokens = _int_or_none(normalized_usage.get("output_tokens"))
    if output_tokens is None:
        output_tokens = completion_tokens if completion_tokens is not None else 0
        normalized_usage["output_tokens"] = output_tokens

    if _int_or_none(normalized_usage.get("total_tokens")) is None:
        normalized_usage["total_tokens"] = input_tokens + output_tokens

    details = normalized_usage.get("input_tokens_details")
    if isinstance(details, dict):
        normalized_usage["input_tokens_details"] = {
            "image_tokens": _int_or_zero(details.get("image_tokens")),
            "text_tokens": _int_or_zero(details.get("text_tokens")),
        }
    else:
        prompt_details = normalized_usage.get("prompt_tokens_details")
        if isinstance(prompt_details, dict):
            normalized_usage["input_tokens_details"] = {
                "image_tokens": _int_or_zero(prompt_details.get("image_tokens")),
                "text_tokens": _int_or_zero(prompt_details.get("text_tokens")),
            }
        else:
            normalized_usage["input_tokens_details"] = {
                "image_tokens": 0,
                "text_tokens": 0,
            }

    normalized_response = dict(response_json)
    normalized_response["usage"] = normalized_usage
    return normalized_response


def _install_litellm_openai_image_edit_usage_patch() -> None:
    try:
        from litellm.llms.openai.image_edit import transformation
        from litellm.utils import ImageResponse
    except Exception:
        return

    config_cls = getattr(transformation, "OpenAIImageEditConfig", None)
    if config_cls is None:
        return

    original = getattr(config_cls, "transform_image_edit_response", None)
    if original is None or getattr(original, _IMAGE_EDIT_USAGE_PATCH_ATTR, False):
        return

    def patched_transform_image_edit_response(
        self: Any,
        model: str,
        raw_response: Any,
        logging_obj: Any,
    ) -> Any:
        try:
            raw_response_json = raw_response.json()
        except Exception:
            raise transformation.OpenAIError(
                message=raw_response.text,
                status_code=raw_response.status_code,
            )

        normalized_response_json = _normalize_image_response_usage(raw_response_json)
        return ImageResponse(**normalized_response_json)

    setattr(patched_transform_image_edit_response, _IMAGE_EDIT_USAGE_PATCH_ATTR, True)
    setattr(patched_transform_image_edit_response, "_original", original)
    config_cls.transform_image_edit_response = patched_transform_image_edit_response


def _install_litellm_config_callback_import_patch() -> None:
    try:
        from litellm.proxy.types_utils import utils
    except Exception:
        return

    original = getattr(utils, "get_instance_fn", None)
    if original is None or getattr(original, _CONFIG_CALLBACK_IMPORT_PATCH_ATTR, False):
        return

    def patched_get_instance_fn(
        value: str,
        config_file_path: Optional[str] = None,
    ) -> Any:
        try:
            return original(value, config_file_path=config_file_path)
        except ImportError:
            if (
                config_file_path is None
                or not isinstance(value, str)
                or not value.startswith("litellm_menu.")
            ):
                raise

            parts = value.split(".")
            if len(parts) < 2:
                raise
            module_name = ".".join(parts[:-1])
            instance_name = parts[-1]
            module_file_path = os.path.join(
                os.path.dirname(config_file_path),
                *module_name.split("."),
            ) + ".py"
            if os.path.exists(module_file_path):
                raise

            module = importlib.import_module(module_name)
            return getattr(module, instance_name)

    setattr(patched_get_instance_fn, _CONFIG_CALLBACK_IMPORT_PATCH_ATTR, True)
    setattr(patched_get_instance_fn, "_original", original)
    utils.get_instance_fn = patched_get_instance_fn

    try:
        callback_utils_module = importlib.import_module(
            "litellm.proxy.common_utils.callback_utils"
        )
    except Exception:
        return
    if getattr(callback_utils_module, "get_instance_fn", None) is original:
        callback_utils_module.get_instance_fn = patched_get_instance_fn


def _install_system_proxy_lookup_patch() -> None:
    if os.environ.get("LITELLM_MENU_DISABLE_SYSTEM_PROXY_LOOKUP") != "1":
        return
    if getattr(urllib.request.getproxies, _SYSTEM_PROXY_LOOKUP_PATCH_ATTR, False):
        return

    original_getproxies = urllib.request.getproxies
    original_proxy_bypass = urllib.request.proxy_bypass

    def patched_getproxies() -> dict[str, str]:
        return urllib.request.getproxies_environment()

    def patched_proxy_bypass(host: str) -> bool:
        return urllib.request.proxy_bypass_environment(
            host,
            urllib.request.getproxies_environment(),
        )

    setattr(patched_getproxies, _SYSTEM_PROXY_LOOKUP_PATCH_ATTR, True)
    setattr(patched_getproxies, "_original", original_getproxies)
    setattr(patched_proxy_bypass, _SYSTEM_PROXY_LOOKUP_PATCH_ATTR, True)
    setattr(patched_proxy_bypass, "_original", original_proxy_bypass)
    urllib.request.getproxies = patched_getproxies
    urllib.request.proxy_bypass = patched_proxy_bypass


_install_system_proxy_lookup_patch()
_install_litellm_config_callback_import_patch()
_install_litellm_openai_image_edit_usage_patch()
