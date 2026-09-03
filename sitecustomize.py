from __future__ import annotations

import importlib
import importlib.machinery
from datetime import datetime, timezone
import json
import os
import signal
import sys
import threading
import time
import urllib.request
from typing import Any, Callable, Optional



def _bounded_writer(stream: Any, text: str) -> int:
    from litellm_menu.log_rotation import write_bounded_stream

    return write_bounded_stream(stream, text)


_IMAGE_EDIT_USAGE_PATCH_ATTR = "_openai_image_edit_usage_patch"
_CONFIG_CALLBACK_IMPORT_PATCH_ATTR = "_litellm_menu_config_callback_import_patch"
_SYSTEM_PROXY_LOOKUP_PATCH_ATTR = "_litellm_menu_system_proxy_lookup_patch"
_SYSTEM_PROXY_SNAPSHOT_ENV = "LITELLM_MENU_SYSTEM_PROXY_SNAPSHOT"
_CONFIG_CALLBACK_ORIGINAL_ATTR = "_litellm_menu_config_callback_import_original"
_OPTIONAL_DATABASE_ERROR_PATCH_ATTR = "_litellm_menu_optional_database_error_patch"
_TIMESTAMPED_OUTPUT_ATTR = "_litellm_menu_timestamped_output"
_CORE_PARENT_WATCHDOG_ATTR = "_litellm_menu_core_parent_watchdog"


class _TimestampedOutputState:
    def __init__(self) -> None:
        self.at_line_start = True
        self.lock = threading.RLock()


class _TimestampedOutput:
    """Prefix each proxy console line while preserving the wrapped stream API."""

    def __init__(self, stream: Any, state: _TimestampedOutputState) -> None:
        self._stream = stream
        self._state = state
        setattr(self, _TIMESTAMPED_OUTPUT_ATTR, True)

    def write(self, value: str) -> int:
        if not isinstance(value, str) or not value:
            return self._stream.write(value)
        rendered: list[str] = []
        with self._state.lock:
            for segment in value.splitlines(keepends=True):
                is_line_break = segment in {"\n", "\r", "\r\n"}
                if self._state.at_line_start and not is_line_break:
                    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                    rendered.append(f"[{stamp}] ")
                rendered.append(segment)
                self._state.at_line_start = segment.endswith(("\n", "\r"))
            _bounded_writer(self._stream, "".join(rendered))
        return len(value)

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _install_timestamped_proxy_output() -> None:
    if os.environ.get("LITELLM_MENU_TIMESTAMP_OUTPUT") != "1":
        return
    if getattr(sys.stdout, _TIMESTAMPED_OUTPUT_ATTR, False):
        return
    state = _TimestampedOutputState()
    sys.stdout = _TimestampedOutput(sys.stdout, state)
    sys.stderr = _TimestampedOutput(sys.stderr, state)


def _install_core_parent_watchdog() -> None:
    """Terminate the proxy group when its owning Core disappears.

    This is installed only in the proxy master, whose direct parent is the
    Core. Worker children inherit the environment but do not meet that parent
    relationship, so they are terminated by the master process group instead.
    """

    if os.name == "nt":
        return
    try:
        core_pid = int(os.environ.get("LITELLM_MENU_CORE_PID", ""))
    except ValueError:
        return
    # `start_new_session=True` makes the proxy master its process-group
    # leader. Workers inherit the environment but are not group leaders, so
    # only the master may decide to terminate the whole proxy group.
    if core_pid <= 0 or os.getpid() != os.getpgrp():
        return
    if getattr(sys, _CORE_PARENT_WATCHDOG_ATTR, False):
        return
    setattr(sys, _CORE_PARENT_WATCHDOG_ATTR, True)
    process_group = os.getpgrp()

    def watch_parent() -> None:
        # Core can be killed in the short fork-to-Python-start interval. In
        # that case the master already has PPID 1: terminate immediately
        # rather than leaving the configured port orphaned.
        while os.getppid() == core_pid:
            time.sleep(0.1)
        try:
            os.killpg(process_group, signal.SIGTERM)
        except OSError:
            pass

    threading.Thread(
        target=watch_parent,
        name="litellm-proxy-core-watchdog",
        daemon=True,
    ).start()


class _PostImportPatchLoader:
    def __init__(self, loader: Any, patch: Callable[[Any], None]) -> None:
        self._loader = loader
        self._patch = patch

    def create_module(self, spec: Any) -> Any:
        creator = getattr(self._loader, "create_module", None)
        return creator(spec) if callable(creator) else None

    def exec_module(self, module: Any) -> None:
        execute = getattr(self._loader, "exec_module", None)
        if not callable(execute):
            raise ImportError("LiteLLM patch target has no module loader")
        execute(module)
        self._patch(module)


class _PostImportPatchFinder:
    def __init__(self, module_name: str, patch: Callable[[Any], None]) -> None:
        self._module_name = module_name
        self._patch = patch

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname != self._module_name:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return None
        spec.loader = _PostImportPatchLoader(spec.loader, self._patch)
        try:
            sys.meta_path.remove(self)
        except ValueError:
            pass
        return spec


def _patch_after_import(module_name: str, patch: Callable[[Any], None]) -> None:
    module = sys.modules.get(module_name)
    if module is not None:
        patch(module)
        return
    sys.meta_path.insert(0, _PostImportPatchFinder(module_name, patch))


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


def _patch_litellm_openai_image_edit_usage(transformation: Any) -> None:
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
        from litellm.utils import ImageResponse

        return ImageResponse(**normalized_response_json)

    setattr(patched_transform_image_edit_response, _IMAGE_EDIT_USAGE_PATCH_ATTR, True)
    setattr(patched_transform_image_edit_response, "_original", original)
    config_cls.transform_image_edit_response = patched_transform_image_edit_response


def _patch_litellm_config_callback_import(utils: Any) -> None:
    original = getattr(utils, "get_instance_fn", None)
    if not callable(original) or getattr(original, _CONFIG_CALLBACK_IMPORT_PATCH_ATTR, False):
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
    setattr(patched_get_instance_fn, _CONFIG_CALLBACK_ORIGINAL_ATTR, original)
    utils.get_instance_fn = patched_get_instance_fn

    callback_utils_module = sys.modules.get("litellm.proxy.common_utils.callback_utils")
    if callback_utils_module is not None and getattr(callback_utils_module, "get_instance_fn", None) is original:
        callback_utils_module.get_instance_fn = patched_get_instance_fn


def _patch_litellm_callback_utils(callback_utils_module: Any) -> None:
    utils = sys.modules.get("litellm.proxy.types_utils.utils")
    patched = getattr(utils, "get_instance_fn", None) if utils is not None else None
    original = getattr(patched, _CONFIG_CALLBACK_ORIGINAL_ATTR, None)
    if original is not None and getattr(callback_utils_module, "get_instance_fn", None) is original:
        callback_utils_module.get_instance_fn = patched


def _patch_litellm_optional_database_error_handler(exception_handler_module: Any) -> None:
    handler = getattr(exception_handler_module, "PrismaDBExceptionHandler", None)
    original = getattr(handler, "is_database_service_unavailable_error", None)
    if not callable(original) or getattr(original, _OPTIONAL_DATABASE_ERROR_PATCH_ATTR, False):
        return

    def patched_is_database_service_unavailable_error(error: Exception) -> bool:
        try:
            return bool(original(error))
        except ModuleNotFoundError as missing:
            # LiteLLM imports optional Prisma/OTel modules while classifying
            # every authentication exception. This bundle does not ship its
            # database backend, so a plain missing-key error must remain a
            # normal 401 instead of becoming an unhandled 500.
            name = missing.name or ""
            if name == "prisma" or name == "opentelemetry" or name.startswith("opentelemetry."):
                return False
            raise

    setattr(patched_is_database_service_unavailable_error, _OPTIONAL_DATABASE_ERROR_PATCH_ATTR, True)
    setattr(patched_is_database_service_unavailable_error, "_original", original)
    handler.is_database_service_unavailable_error = staticmethod(patched_is_database_service_unavailable_error)


def _install_litellm_openai_image_edit_usage_patch() -> None:
    _patch_after_import(
        "litellm.llms.openai.image_edit.transformation",
        _patch_litellm_openai_image_edit_usage,
    )


def _install_litellm_config_callback_import_patch() -> None:
    _patch_after_import(
        "litellm.proxy.types_utils.utils",
        _patch_litellm_config_callback_import,
    )
    _patch_after_import(
        "litellm.proxy.common_utils.callback_utils",
        _patch_litellm_callback_utils,
    )


def _install_litellm_optional_database_error_patch() -> None:
    _patch_after_import(
        "litellm.proxy.db.exception_handler",
        _patch_litellm_optional_database_error_handler,
    )


def _install_uvicorn_websocket_frame_limit_patch() -> None:
    """Raise uvicorn's inbound WebSocket frame limit before any Config exists.

    Codex sends each Responses turn request as a single text frame that
    includes the immutable replay prefix, so a long task with signed image
    history can exceed uvicorn's default 16 MiB ``ws_max_size``.  uvicorn
    then closes the connection with code 1009 before the request reaches
    the pipeline and Codex reports ``websocket closed by server before
    response.completed`` followed by its reconnect ladder.  The installer
    lives in ``litellm_menu.base`` so both this early hook (interpreter
    startup, covering the LiteLLM CLI launch path) and the callback-time
    ``litellm_menu.patches.install_all`` share one implementation.
    """

    def _patch(uvicorn_module: Any) -> None:
        from litellm_menu.base import _install_websocket_frame_limit_patch

        _install_websocket_frame_limit_patch()

    _patch_after_import("uvicorn", _patch)


def _install_system_proxy_lookup_patch() -> None:
    raw_snapshot = os.environ.pop(_SYSTEM_PROXY_SNAPSHOT_ENV, "")
    if not raw_snapshot and os.environ.get("LITELLM_MENU_DISABLE_SYSTEM_PROXY_LOOKUP") != "1":
        return
    if getattr(urllib.request.getproxies, _SYSTEM_PROXY_LOOKUP_PATCH_ATTR, False):
        return

    original_getproxies = urllib.request.getproxies
    original_proxy_bypass = urllib.request.proxy_bypass

    if raw_snapshot:
        try:
            snapshot = json.loads(raw_snapshot)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Cached macOS proxy settings are invalid") from exc
        if not isinstance(snapshot, dict):
            raise RuntimeError("Cached macOS proxy settings are invalid")
        source = snapshot.get("source")
        proxies = snapshot.get("proxies")
        settings = snapshot.get("settings", {})
        if source not in {"environment", "macos"} or not isinstance(proxies, dict):
            raise RuntimeError("Cached macOS proxy settings are invalid")
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in proxies.items()):
            raise RuntimeError("Cached macOS proxy settings are invalid")
        if source == "macos" and not isinstance(settings, dict):
            raise RuntimeError("Cached macOS proxy settings are invalid")

        def patched_getproxies() -> dict[str, str]:
            return dict(proxies)

        if source == "macos":

            def patched_proxy_bypass(host: str) -> bool:
                return urllib.request._proxy_bypass_macosx_sysconf(host, settings)

        else:

            def patched_proxy_bypass(host: str) -> bool:
                return urllib.request.proxy_bypass_environment(host, proxies)

    else:

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
if os.environ.get("LITELLM_MENU_PROXY_PROCESS") == "1":
    _install_core_parent_watchdog()
    _install_timestamped_proxy_output()
    _install_litellm_config_callback_import_patch()
    _install_litellm_optional_database_error_patch()
    _install_litellm_openai_image_edit_usage_patch()
    _install_uvicorn_websocket_frame_limit_patch()
