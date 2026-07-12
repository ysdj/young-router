"""Codex Responses Lite standalone web-search endpoint.

Codex's Responses Lite client does not send hosted ``web_search`` through the
model request.  It invokes ``POST /alpha/search`` (relative to the configured
provider base URL) with a small command protocol instead.  LiteLLM's built-in
``/search`` endpoint speaks the Perplexity Search API, so this module provides
the narrow Codex protocol while reusing Menu's provider-neutral web-search
bridge.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from . import responses_web_search_bridge as _bridge
from . import state as _state
from . import trace as _trace


_MAX_SESSIONS = 256
_MAX_REFERENCES_PER_SESSION = 200
_MAX_SHARED_REFERENCES = 4096
_SHARED_REFERENCE_TTL_SECONDS = 7 * 24 * 60 * 60
_SEARCH_STATE_FILE_ENV = "LITELLM_MENU_SEARCH_STATE_FILE"
_REFERENCE_RE = re.compile(r"^turn\d+search\d+$", re.IGNORECASE)


@dataclass
class _SearchSession:
    references: dict[str, str] = field(default_factory=dict)


_sessions: dict[str, _SearchSession] = {}
_sessions_lock = threading.Lock()


def _session_key(payload: dict[str, Any]) -> str:
    for key in ("id", "session_id", "thread_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "codex-default"


def _session_for(key: str) -> _SearchSession:
    with _sessions_lock:
        session = _sessions.get(key)
        if session is None:
            if len(_sessions) >= _MAX_SESSIONS:
                _sessions.pop(next(iter(_sessions)), None)
            session = _SearchSession()
            _sessions[key] = session
        return session


def _clean_query(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    query = " ".join(value.split()).strip()
    return query or None


def _clean_url(value: Any) -> Optional[str]:
    return _bridge._external_web_search_clean_url(value)


def _command_list(commands: Any, name: str) -> list[Any]:
    if not isinstance(commands, dict):
        return []
    value = commands.get(name)
    if isinstance(value, list):
        return value
    return []


def _resolve_reference(value: Any, session: _SearchSession) -> Optional[str]:
    url = _clean_url(value)
    if url:
        return url
    if not isinstance(value, str):
        return None
    ref_id = value.strip()
    if not ref_id:
        return None
    local_url = session.references.get(ref_id)
    if local_url:
        return local_url
    if not _REFERENCE_RE.fullmatch(ref_id):
        return None
    shared_url = _shared_reference_url(ref_id)
    if shared_url:
        session.references[ref_id] = shared_url
    return shared_url


def _search_state_file_path() -> Optional[str]:
    configured = os.environ.get(_SEARCH_STATE_FILE_ENV, "").strip()
    if configured:
        return configured
    recovery_path = os.environ.get(
        "LITELLM_MENU_ROUTE_RECOVERY_STATE_FILE",
        "",
    ).strip()
    if not recovery_path:
        return None
    return os.path.join(os.path.dirname(recovery_path), "web-search-references.json")


def _shared_reference_url(ref_id: str) -> Optional[str]:
    path = _search_state_file_path()
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    references = payload.get("references") if isinstance(payload, dict) else None
    entry = references.get(ref_id) if isinstance(references, dict) else None
    url = entry.get("url") if isinstance(entry, dict) else entry
    return _clean_url(url)


def _remember_shared_references(references: dict[str, str]) -> None:
    path = _search_state_file_path()
    if not path or not references:
        return
    now = int(time.time())

    def update(payload: dict[str, Any]) -> None:
        stored = payload.get("references")
        if not isinstance(stored, dict):
            stored = {}
        cutoff = now - _SHARED_REFERENCE_TTL_SECONDS
        clean: dict[str, dict[str, Any]] = {}
        for ref_id, entry in stored.items():
            if not isinstance(ref_id, str) or not _REFERENCE_RE.fullmatch(ref_id):
                continue
            if isinstance(entry, dict):
                url = _clean_url(entry.get("url"))
                updated_at = entry.get("updated_at")
            else:
                url = _clean_url(entry)
                updated_at = now
            if not isinstance(updated_at, int) or updated_at < cutoff or not url:
                continue
            clean[ref_id] = {"url": url, "updated_at": updated_at}
        for ref_id, url in references.items():
            clean[ref_id] = {"url": url, "updated_at": now}
        if len(clean) > _MAX_SHARED_REFERENCES:
            ordered = sorted(
                clean.items(),
                key=lambda item: int(item[1].get("updated_at") or 0),
                reverse=True,
            )
            clean = dict(ordered[:_MAX_SHARED_REFERENCES])
        payload.clear()
        payload.update({"schema_version": 1, "references": clean})

    _state._locked_json_state_update(path, update)


def _remember_search_sources(
    session: _SearchSession,
    source_urls_by_action: list[list[str]],
    search_results: str,
) -> None:
    cards = _bridge._external_web_search_result_cards(search_results)
    urls: list[str] = []
    for group in source_urls_by_action:
        for value in group:
            url = _clean_url(value)
            if url and url not in urls:
                urls.append(url)
    for card in cards:
        url = _clean_url(card.get("url"))
        if url and url not in urls:
            urls.append(url)

    # Keep Codex's ordinary decimal ``turnNsearchN`` shape while deriving a
    # stable turn number from the result set.  The shared registry lets a later
    # open/find request resolve the reference even when Gunicorn selects a
    # different worker.
    turn_digest = hashlib.sha256("\n".join(urls).encode("utf-8")).digest()[:5]
    turn_id = int.from_bytes(turn_digest, "big")
    remembered: dict[str, str] = {}
    for result_index, url in enumerate(urls):
        ref_id = f"turn{turn_id}search{result_index}"
        session.references[ref_id] = url
        remembered[ref_id] = url
    _remember_shared_references(remembered)
    if len(session.references) > _MAX_REFERENCES_PER_SESSION:
        stale = list(session.references)[: len(session.references) - _MAX_REFERENCES_PER_SESSION]
        for ref_id in stale:
            session.references.pop(ref_id, None)
def _annotate_search_result_refs(
    session: _SearchSession,
    source_urls_by_action: list[list[str]],
    search_results: str,
) -> str:
    """Add stable reference labels to the human-readable bridge output."""
    # The bridge output is intentionally kept intact.  Prefixing each URL line
    # gives the model a ref id without changing title/snippet parsing.
    ref_by_url = {url: ref for ref, url in session.references.items()}
    if not ref_by_url:
        return search_results
    lines = search_results.splitlines()
    current_url: Optional[str] = None
    annotated: list[str] = []
    for line in lines:
        if line.startswith("URL:"):
            current_url = _clean_url(line.partition(":")[2].strip())
            annotated.append(line)
            if current_url and current_url in ref_by_url:
                annotated.append(f"Reference: {ref_by_url[current_url]}")
            continue
        annotated.append(line)
    return "\n".join(annotated)


def commands_to_actions(
    commands: Any,
    session: Optional[_SearchSession] = None,
) -> tuple[list[dict[str, str]], list[str]]:
    """Translate Codex ``commands`` into bridge actions.

    The second return value contains user-facing errors for references that
    cannot be resolved.  Keeping this conversion pure makes the endpoint easy
    to test without importing FastAPI or starting LiteLLM.
    """
    session = session or _SearchSession()
    actions: list[dict[str, str]] = []
    errors: list[str] = []

    for operation in _command_list(commands, "search_query"):
        raw_query = operation.get("q") if isinstance(operation, dict) else operation
        query = _clean_query(raw_query)
        if query:
            actions.append({"type": "search", "query": query})

    for operation in _command_list(commands, "open"):
        ref_id = operation.get("ref_id") if isinstance(operation, dict) else operation
        url = _resolve_reference(ref_id, session)
        if url:
            actions.append({"type": "openPage", "url": url})
        else:
            errors.append(f"Unable to resolve web-search reference: {ref_id}")

    for operation in _command_list(commands, "find"):
        ref_id = operation.get("ref_id") if isinstance(operation, dict) else None
        pattern = operation.get("pattern") if isinstance(operation, dict) else None
        url = _resolve_reference(ref_id, session)
        clean_pattern = _clean_query(pattern)
        if url and clean_pattern:
            actions.append({"type": "findInPage", "url": url, "pattern": clean_pattern})
        elif not url:
            errors.append(f"Unable to resolve web-search reference: {ref_id}")
        else:
            errors.append("Web-search find command is missing a pattern")

    return actions, errors


async def execute_search_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute a Codex SearchRequest payload and return SearchResponse JSON."""
    commands = payload.get("commands")
    session = _session_for(_session_key(payload))
    actions, errors = commands_to_actions(commands, session)
    _trace._route_trace(
        "standalone_web_search_start",
        command_types=[
            name
            for name in ("search_query", "open", "find")
            if _command_list(commands, name)
        ],
        action_types=[action.get("type") for action in actions],
        action_count=len(actions),
        unresolved_reference_count=len(errors),
    )
    if not actions:
        output = "\n".join(errors) if errors else "No web-search command was supplied."
        _trace._route_trace(
            "standalone_web_search_completed",
            action_count=0,
            source_count=0,
            output_chars=len(output),
            unresolved_reference_count=len(errors),
        )
        return {"output": output}

    page_cache: dict[str, str] = {}
    page_fetch_tasks: dict[str, asyncio.Task[str]] = {}
    message, source_urls, source_urls_by_action, _completed = await _bridge._external_web_search_run_actions(
        actions,
        page_cache,
        page_fetch_tasks,
    )
    if any(action.get("type") == "search" for action in actions):
        with _sessions_lock:
            _remember_search_sources(session, source_urls_by_action, message)
            message = _annotate_search_result_refs(session, source_urls_by_action, message)
    if errors:
        message = "\n\n".join(part for part in (message, "\n".join(errors)) if part)
    output = message or "No web-search results were returned."
    _trace._route_trace(
        "standalone_web_search_completed",
        action_count=len(actions),
        action_types=[action.get("type") for action in actions],
        source_count=len(source_urls),
        output_chars=len(output),
        unresolved_reference_count=len(errors),
    )
    return {"output": output}


def register() -> None:
    """Register the endpoint on LiteLLM's FastAPI app during worker startup."""
    from fastapi import Depends
    from fastapi.responses import ORJSONResponse
    from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
    from litellm.proxy.proxy_server import app

    async def endpoint(
        request: Any,
        _user_api_key_dict: Any = Depends(user_api_key_auth),
    ) -> ORJSONResponse:
        payload = await request.json()
        if not isinstance(payload, dict):
            payload = {}
        return ORJSONResponse(await execute_search_payload(payload))

    # ``from __future__ import annotations`` leaves a nested local ``Request``
    # annotation unresolved when FastAPI builds the route.  Supply the concrete
    # class without importing FastAPI at module-import time.
    from starlette.requests import Request

    endpoint.__annotations__["request"] = Request

    existing_paths = {
        route.path
        for route in getattr(app, "routes", [])
        if getattr(route, "path", None)
    }
    for path in ("/alpha/search", "/v1/alpha/search"):
        if path not in existing_paths:
            app.add_api_route(
                path,
                endpoint,
                methods=["POST"],
                response_class=ORJSONResponse,
                tags=["search"],
            )


__all__ = ["commands_to_actions", "execute_search_payload", "register"]
