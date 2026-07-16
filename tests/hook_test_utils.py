from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from enum import Enum
import importlib.util
import importlib
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "litellm_menu" / "callbacks.py"
HOOK_MODULE_NAMES = (
    "base",
    "api_base",
    "trace",
    "state",
    "routing",
    "responses_tools",
    "responses_surfaces",
    "responses_web_search_bridge",
    "responses_output",
    "responses_execution",
    "responses_bridge",
    "patches",
    "tools",
    "computer_facade",
    "image_generation",
    "vision_bridge",
    "streaming",
    "external_web_search",
    "hook",
)


class HookTestNamespace:
    def __init__(self, modules: list[types.ModuleType]) -> None:
        object.__setattr__(self, "_owners", {})
        for module in modules:
            for key, value in vars(module).items():
                if key.startswith("__") or key == "annotations":
                    continue
                object.__setattr__(self, key, value)
                self._owners.setdefault(key, module)

    def __setattr__(self, name: str, value: object) -> None:
        object.__setattr__(self, name, value)
        owner = self._owners.get(name)
        if owner is not None:
            setattr(owner, name, value)


def load_hook_module():
    for name in [
        "litellm_menu",
        "litellm_menu.callbacks",
        "litellm",
        "litellm.main",
        "litellm.integrations",
        "litellm.integrations.custom_logger",
        "litellm.integrations.websearch_interception",
        "litellm.integrations.websearch_interception.handler",
        "litellm.integrations.websearch_interception.transformation",
        "litellm.llms",
        "litellm.llms.base_llm",
        "litellm.llms.base_llm.search",
        "litellm.llms.base_llm.search.transformation",
        "litellm.proxy",
        "litellm.proxy.proxy_server",
        "litellm.router",
        "litellm.router_utils",
        "litellm.router_utils.fallback_event_handlers",
        "litellm.responses",
        "litellm.responses.litellm_completion_transformation",
        "litellm.responses.litellm_completion_transformation.streaming_iterator",
        "litellm.responses.litellm_completion_transformation.transformation",
    ]:
        sys.modules.pop(name, None)
    for name in list(sys.modules):
        if name == "litellm_menu" or name.startswith("litellm_menu."):
            sys.modules.pop(name, None)

    litellm = types.ModuleType("litellm")

    class InternalServerError(Exception):
        def __init__(self, message: str, model: str = "", llm_provider: str = "") -> None:
            super().__init__(message)
            self.message = message
            self.model = model
            self.llm_provider = llm_provider

    class ServiceUnavailableError(Exception):
        def __init__(self, message: str, model: str = "", llm_provider: str = "") -> None:
            super().__init__(message)
            self.message = message
            self.model = model
            self.llm_provider = llm_provider
            self.status_code = 503

    litellm.InternalServerError = InternalServerError
    litellm.ServiceUnavailableError = ServiceUnavailableError

    integrations = types.ModuleType("litellm.integrations")
    custom_logger = types.ModuleType("litellm.integrations.custom_logger")

    class CustomLogger:
        pass

    custom_logger.CustomLogger = CustomLogger

    proxy = types.ModuleType("litellm.proxy")
    proxy_server = types.ModuleType("litellm.proxy.proxy_server")
    proxy_server.llm_router = None

    sys.modules["litellm"] = litellm
    sys.modules["litellm.integrations"] = integrations
    sys.modules["litellm.integrations.custom_logger"] = custom_logger
    sys.modules["litellm.proxy"] = proxy
    sys.modules["litellm.proxy.proxy_server"] = proxy_server

    importlib.import_module("litellm_menu.callbacks")
    modules = [
        importlib.import_module(f"litellm_menu.{name}")
        for name in HOOK_MODULE_NAMES
    ]
    return HookTestNamespace(modules), proxy_server


def jsonable_stream_chunk(chunk):
    if isinstance(chunk, str) and chunk.startswith("data: "):
        payload = chunk[len("data: ") :].strip()
        if payload == "[DONE]":
            return {"type": "done"}
        return json.loads(payload)
    if hasattr(chunk, "model_dump"):
        return chunk.model_dump(mode="json", exclude_none=True)
    return chunk


def assert_upstream_route_failed_terminal(testcase, chunks):
    json_chunks = [jsonable_stream_chunk(chunk) for chunk in chunks]
    completed_events = [
        chunk
        for chunk in json_chunks
        if isinstance(chunk, dict) and chunk.get("type") == "response.completed"
    ]
    failed_events = [
        chunk
        for chunk in json_chunks
        if isinstance(chunk, dict) and chunk.get("type") == "response.failed"
    ]
    testcase.assertEqual(completed_events, [])
    testcase.assertEqual(len(failed_events), 1)
    response = failed_events[0].get("response", {})
    testcase.assertEqual(response.get("status"), "failed")
    error = response.get("error", {})
    testcase.assertEqual(error.get("code"), "upstream_route_failure")
    testcase.assertEqual(
        error.get("message"),
        "The upstream model route failed before a final assistant response was available.",
    )
    testcase.assertNotIn("Temporary upstream route failure", error.get("message", ""))
    testcase.assertNotIn("LiteLLM fallback retries", error.get("message", ""))
    testcase.assertNotIn("Retry later", error.get("message", ""))
    return failed_events[0]


def assert_external_web_search_missing_answer_failed(testcase, chunks):
    failed_event = assert_upstream_route_failed_terminal(testcase, chunks)
    text = json.dumps([jsonable_stream_chunk(chunk) for chunk in chunks])
    testcase.assertNotIn(
        "The web search step completed, but the model did not return a final assistant response.",
        text,
    )
    return failed_event


class HookTestCase(unittest.IsolatedAsyncioTestCase):
    def set_log_env(self, path: Path) -> None:
        previous = os.environ.get("LITELLM_RECENT_REQUESTS_LOG")
        os.environ["LITELLM_RECENT_REQUESTS_LOG"] = str(path)

        def restore() -> None:
            if previous is None:
                os.environ.pop("LITELLM_RECENT_REQUESTS_LOG", None)
            else:
                os.environ["LITELLM_RECENT_REQUESTS_LOG"] = previous

        self.addCleanup(restore)

    def set_env(self, name: str, value: str | None) -> None:
        previous = os.environ.get(name)
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

        def restore() -> None:
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous

        self.addCleanup(restore)
