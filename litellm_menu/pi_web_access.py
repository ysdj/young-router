"""Python adapter for the bundled ``pi-web-access`` worker.

The web-search bridge remains Python-owned so its Responses and Codex protocol
semantics do not change.  Search and page extraction are delegated to the
Node worker built from the upstream ``pi-web-access`` extension.  The worker
speaks one-request JSONL on stdin/stdout; this adapter deliberately starts a
fresh worker for each action so a changed runtime JSON configuration is picked
up without a proxy restart or a stale module cache.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any
import uuid


_WEB_FETCH_TIMEOUT_ENV = "LITELLM_MENU_WEB_FETCH_TIMEOUT_SECONDS"
_WEB_SEARCH_MAX_RESULTS_ENV = "LITELLM_MENU_WEB_SEARCH_MAX_RESULTS"
_PI_WEB_ACCESS_NODE_ENV = "LITELLM_MENU_PI_WEB_ACCESS_NODE"
_PI_WEB_ACCESS_WORKER_ENV = "LITELLM_MENU_PI_WEB_ACCESS_WORKER"
_PI_WEB_ACCESS_ENTRY_ENV = "LITELLM_MENU_PI_WEB_ACCESS_ENTRY"
_PI_WEB_ACCESS_CONFIG_DIR_ENV = "LITELLM_MENU_WEB_SEARCH_CONFIG_DIR"
_DEFAULT_FETCH_TIMEOUT_SECONDS = 12.0
_DEFAULT_MAX_RESULTS = 5
_MAX_RESULTS = 20


def _external_web_search_int_env(
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _external_web_search_float_env(
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _worker_path() -> Path:
    configured = os.environ.get(_PI_WEB_ACCESS_WORKER_ENV, "").strip()
    return Path(configured).expanduser() if configured else Path(__file__).with_name("pi_web_access_worker.mjs")


def _node_command() -> str:
    configured = os.environ.get(_PI_WEB_ACCESS_NODE_ENV, "").strip()
    if configured:
        return configured
    node = shutil.which("node")
    if node:
        return node
    raise RuntimeError(
        "pi-web-access requires the bundled Node.js runtime; "
        f"set {_PI_WEB_ACCESS_NODE_ENV} to its executable path"
    )


def _worker_request(
    request: dict[str, Any],
    *,
    timeout_seconds: float,
    max_results: int | None = None,
) -> dict[str, Any]:
    worker = _worker_path()
    if not worker.is_file():
        raise RuntimeError(f"pi-web-access worker is missing: {worker}")

    request_id = uuid.uuid4().hex
    payload = dict(request)
    payload["id"] = request_id
    command = [_node_command(), str(worker)]
    command.extend(("--timeout", str(timeout_seconds)))
    if max_results is not None:
        command.extend(("--max-results", str(max_results)))
    configured_entry = os.environ.get(_PI_WEB_ACCESS_ENTRY_ENV, "").strip()
    if configured_entry:
        command.extend(("--entry", configured_entry))
    configured_dir = os.environ.get(_PI_WEB_ACCESS_CONFIG_DIR_ENV, "").strip()
    if configured_dir:
        command.extend(("--config-dir", configured_dir))

    try:
        completed = subprocess.run(
            command,
            input=json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
            text=True,
            capture_output=True,
            env=os.environ.copy(),
            timeout=max(1.0, timeout_seconds + 10.0),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"pi-web-access worker timed out after {timeout_seconds:g} seconds"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"Could not start pi-web-access worker: {exc}") from exc

    response: dict[str, Any] | None = None
    for line in reversed(completed.stdout.splitlines()):
        try:
            candidate = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(candidate, dict) and candidate.get("id") == request_id:
            response = candidate
            break
    if response is None:
        detail = completed.stderr.strip() or completed.stdout.strip()
        if completed.returncode:
            raise RuntimeError(
                f"pi-web-access worker exited with status {completed.returncode}"
                + (f": {detail[:500]}" if detail else "")
            )
        raise RuntimeError(
            "pi-web-access worker returned no response"
            + (f": {detail[:500]}" if detail else "")
        )
    if response.get("ok") is not True:
        error = response.get("error") or response.get("text") or "unknown worker error"
        raise RuntimeError(str(error))
    return response


def _clean_urls(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    urls: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        url = item.strip().rstrip(").,;]")
        if url.startswith(("http://", "https://")) and url not in urls:
            urls.append(url)
    return urls


def _pi_web_access_search_sync(query: str, *, page: int = 1) -> tuple[str, Any]:
    if not isinstance(page, int) or page < 1:
        raise ValueError("search page must be a positive integer")
    max_results = _external_web_search_int_env(
        _WEB_SEARCH_MAX_RESULTS_ENV,
        _DEFAULT_MAX_RESULTS,
        1,
        _MAX_RESULTS,
    )
    timeout = _external_web_search_float_env(
        _WEB_FETCH_TIMEOUT_ENV,
        _DEFAULT_FETCH_TIMEOUT_SECONDS,
        3.0,
        60.0,
    )
    response = _worker_request(
        {"action": "search", "query": query, "page": page},
        timeout_seconds=timeout,
        max_results=max_results,
    )
    text = response.get("text")
    if not isinstance(text, str) or not text.strip():
        text = "No results found."
    urls = _clean_urls(response.get("sourceUrls"))
    # The bridge's source extractor understands a nested URL-bearing object;
    # preserving the worker's details also makes diagnostics available to it.
    structured = {
        "results": [{"url": url} for url in urls],
        "details": response.get("details") if isinstance(response.get("details"), dict) else {},
    }
    return text, structured


def _pi_web_access_page_excerpt(
    url: str,
    *,
    timeout: float,
    max_chars: int,
) -> str:
    response = _worker_request(
        {"action": "openPage", "url": url},
        timeout_seconds=timeout,
    )
    text = response.get("text")
    if not isinstance(text, str):
        return ""
    prefix = f"Retrieved page content for URL: {url}\n\n"
    if text.startswith(prefix):
        text = text[len(prefix) :]
    return text[:max_chars].rstrip()


__all__ = [
    "_external_web_search_float_env",
    "_external_web_search_int_env",
    "_pi_web_access_page_excerpt",
    "_pi_web_access_search_sync",
]
