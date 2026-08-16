#!/usr/bin/env python3
"""Fetch the user's own live usage rows from compatible relay control planes.

The reader is deliberately opt-in by response shape: it tries a small set of
documented, read-only endpoints against configured enabled credentials and
emits a sanitized text feed.  It never guesses hosts, persists remote data, or
prints a credential, URL, request body, IP address, or upstream response text.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import math
import os
import pathlib
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from config_editor_core.load import load_config
from litellm_menu.api_base import isolated_http_opener, service_root


MAX_TARGETS = 4
MAX_WORKERS = 4
PAGE_SIZE = 80
MAX_ROWS_PER_TARGET = 100
DEFAULT_TIMEOUT_SECONDS = 4.0
MAX_RESPONSE_BYTES = 256 * 1024


class UsageConfigError(ValueError):
    """The current LiteLLM configuration could not be read safely."""

def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _credential_value(value: Any) -> str:
    text = _string(value)
    if not text.startswith("os.environ/"):
        return "" if "\r" in text or "\n" in text else text
    variable = text.removeprefix("os.environ/").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
        return ""
    resolved = os.environ.get(variable, "").strip()
    return "" if "\r" in resolved or "\n" in resolved else resolved


def _provider_keys(provider: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    by_name: dict[str, str] = {}
    ordered: list[str] = []
    raw_keys = provider.get("api_keys")
    if not isinstance(raw_keys, list):
        return by_name, ordered
    for index, item in enumerate(raw_keys, start=1):
        if not isinstance(item, dict):
            continue
        name = _string(item.get("name")) or f"key-{index}"
        value = _credential_value(item.get("value"))
        if not value or name in by_name:
            continue
        by_name[name] = value
        ordered.append(value)
    return by_name, ordered


def _credential_for_model(provider: dict[str, Any], model: dict[str, Any]) -> str:
    direct = _credential_value(model.get("api_key"))
    if direct:
        return direct
    by_name, ordered = _provider_keys(provider)
    preferred_name = _string(model.get("api_key_name"))
    if preferred_name and preferred_name in by_name:
        return by_name[preferred_name]
    if "default" in by_name:
        return by_name["default"]
    return ordered[0] if len(ordered) == 1 else ""


@dataclass(frozen=True)
class UsageTarget:
    provider: str
    api_base: str
    api_key: str


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def default_config_path() -> pathlib.Path:
    configured = os.environ.get("LITELLM_CONFIG_FILE", "").strip()
    if configured:
        return pathlib.Path(configured).expanduser()
    root = os.environ.get("LITELLM_RUNTIME_ROOT", "").strip() or os.environ.get(
        "LITELLM_MENU_HOME", ""
    ).strip()
    if root:
        return pathlib.Path(root).expanduser() / "config.yaml"
    return pathlib.Path.home() / ".litellm-menu" / "config.yaml"


def active_usage_targets(path: pathlib.Path) -> list[UsageTarget]:
    """Return one safe read target for each configured model credential."""
    try:
        payload = load_config(path)
    except Exception as exc:
        raise UsageConfigError("The LiteLLM configuration could not be read.") from exc
    raw_providers = payload.get("providers")
    if not isinstance(raw_providers, list):
        raise UsageConfigError("The LiteLLM configuration has no provider list.")

    seen: set[tuple[str, str]] = set()
    provider_counts: dict[str, int] = {}
    targets: list[UsageTarget] = []
    for raw_provider in raw_providers:
        if not isinstance(raw_provider, dict):
            continue
        provider_name = _string(raw_provider.get("name")) or "Configured relay"
        provider_base = _string(raw_provider.get("api_base"))
        raw_models = raw_provider.get("models")
        if not isinstance(raw_models, list):
            continue
        for raw_model in raw_models:
            if not isinstance(raw_model, dict):
                continue
            api_base = _string(raw_model.get("api_base")) or provider_base
            api_key = _credential_for_model(raw_provider, raw_model)
            root = service_root(api_base)
            if not root or not api_key:
                continue
            key = (root, api_key)
            if key in seen:
                continue
            seen.add(key)
            provider_counts[provider_name] = provider_counts.get(provider_name, 0) + 1
            suffix = provider_counts[provider_name]
            label = provider_name if suffix == 1 else f"{provider_name} · Account {suffix}"
            targets.append(UsageTarget(provider=label, api_base=api_base, api_key=api_key))
            if len(targets) >= MAX_TARGETS:
                return targets
    return targets


def _control_api_root(api_base: str) -> str | None:
    """Return the installed control-plane prefix without duplicating `/api`.

    A normal OpenAI base URL ends in `/v1`, while a reverse proxy can expose
    the same API below `/api/v1`.  `service_root` deliberately retains that
    deployment prefix, so append the control-plane `/api` segment only when it
    is not already part of the root.
    """

    root = service_root(api_base)
    if root is None:
        return None
    path = urllib.parse.urlsplit(root).path.rstrip("/").lower()
    return root if path.endswith("/api") else f"{root}/api"


def _endpoint_candidates(api_base: str) -> list[tuple[str, str, str]]:
    control_root = _control_api_root(api_base)
    root = service_root(api_base)
    if control_root is None or root is None:
        return []
    return [
        # New API's key-scoped log feed is explicitly read-only and accepts a
        # normal gateway key.  It is the appropriate source for a Menu route,
        # which stores gateway credentials rather than dashboard sessions.
        ("newapi", f"{control_root}/log/token", "bearer"),
        # Sub2API exposes a credential-scoped aggregated usage document at
        # the compatible gateway path.  It is not a per-request log feed, but
        # it retains the latest usage by model without dashboard credentials.
        ("sub2api", f"{root}/v1/usage", "bearer"),
    ]


def _fetch_json(
    url: str,
    api_key: str,
    timeout: float,
    authorization_scheme: str,
) -> tuple[str, dict[str, Any] | None]:
    authorization = api_key if authorization_scheme == "raw" else f"Bearer {api_key}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": authorization,
            "User-Agent": "LiteLLM-Menu-Usage/1",
        },
        method="GET",
    )
    try:
        with isolated_http_opener().open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", response.getcode()))
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return "unsupported", None
    except (socket.timeout, TimeoutError):
        return "timeout", None
    except (urllib.error.URLError, OSError):
        return "network", None
    if not 200 <= status < 300 or len(body) > MAX_RESPONSE_BYTES:
        return "unsupported", None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "unsupported", None
    if not isinstance(payload, dict):
        return "unsupported", None
    if payload.get("success") is False:
        return "unsupported", None
    code = payload.get("code")
    if isinstance(code, int) and not isinstance(code, bool) and code != 0:
        return "unsupported", None
    return "ok", payload


def _items(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Return a recognized paginated item list, including an empty one.

    Empty usage history is a valid response.  Keeping it distinct from an
    unrecognized JSON object lets the UI say that a relay has no recent rows
    instead of falsely claiming that no supported endpoint exists.
    """

    for container in (payload.get("data"), payload):
        if isinstance(container, dict):
            for key in ("items", "data", "list", "logs"):
                value = container.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        elif isinstance(container, list):
            return [item for item in container if isinstance(item, dict)]
    return None


def _safe_text(value: Any, limit: int = 120) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text[:limit]


def _safe_number(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(number):
        return ""
    return str(int(number)) if number.is_integer() else f"{number:.4g}"


def _safe_timestamp(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            return ""
        if not math.isfinite(timestamp):
            return ""
        # Relay log APIs commonly use Unix seconds.  Accept milliseconds too,
        # but reject implausible values instead of rendering arbitrary numbers.
        if timestamp > 10_000_000_000:
            timestamp /= 1_000
        if not 946_684_800 <= timestamp <= 4_102_444_800:
            return ""
        try:
            return (
                dt.datetime.fromtimestamp(timestamp, dt.timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except (OverflowError, OSError, ValueError):
            return ""
    text = _safe_text(value, 40)
    if not text:
        return ""
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _total_tokens(row: dict[str, Any]) -> str:
    for key in ("total_tokens", "token_count", "tokens"):
        value = _safe_number(row.get(key))
        if value:
            return value

    parts = (
        "prompt_tokens",
        "completion_tokens",
        "input_tokens",
        "output_tokens",
        "cache_creation_tokens",
        "cache_read_tokens",
    )
    total = 0.0
    found = False
    for key in parts:
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if not math.isfinite(number):
            continue
        total += number
        found = True
    return _safe_number(total) if found else ""


def _summary_rows(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Project a credential-scoped usage summary into safe model rows."""

    raw_models: Any = payload.get("model_stats")
    if not isinstance(raw_models, list):
        data = payload.get("data")
        raw_models = data.get("model_stats") if isinstance(data, dict) else None
    if not isinstance(raw_models, list):
        return None
    rows: list[dict[str, Any]] = []
    for raw_model in raw_models:
        if not isinstance(raw_model, dict):
            continue
        model = _safe_text(raw_model.get("model") or raw_model.get("model_name"), 96)
        if not model:
            continue
        row: dict[str, Any] = {"model": model}
        for key in (
            "total_tokens",
            "token_count",
            "tokens",
            "input_tokens",
            "output_tokens",
            "prompt_tokens",
            "completion_tokens",
            "cache_creation_tokens",
            "cache_read_tokens",
        ):
            if key in raw_model:
                row[key] = raw_model[key]
        rows.append(row)
    return rows


def _row_text(row: dict[str, Any], fallback_timestamp: str) -> str:
    timestamp = _safe_timestamp(
        row.get("created_at") or row.get("createdAt") or row.get("timestamp")
    ) or fallback_timestamp
    model = _safe_text(
        row.get("model_name") or row.get("model") or row.get("requested_model"),
        96,
    )
    status = _safe_text(row.get("status") or row.get("request_type"), 32)
    if not status:
        type_value = _safe_number(row.get("type"))
        status = f"type={type_value}" if type_value else ""
    tokens = _total_tokens(row)
    pieces = [
        part
        for part in [timestamp, model, status, f"tokens={tokens}" if tokens else ""]
        if part
    ]
    return "  ".join(pieces)


def fetch_target(
    target: UsageTarget,
    timeout: float,
    fallback_timestamp: str | None = None,
) -> tuple[str, list[str]]:
    fallback_timestamp = fallback_timestamp or _utc_now_iso()
    for kind, endpoint, authorization_scheme in _endpoint_candidates(target.api_base):
        result, payload = _fetch_json(
            endpoint,
            target.api_key,
            timeout,
            authorization_scheme,
        )
        if result != "ok" or payload is None:
            continue
        rows = _items(payload)
        if rows is None and kind == "sub2api":
            rows = _summary_rows(payload)
        if rows is None:
            continue
        rendered = [_row_text(row, fallback_timestamp) for row in rows[:MAX_ROWS_PER_TARGET]]
        rendered = [row for row in rendered if row]
        return kind, rendered
    return "", []


def _fetch_target_safely(
    target: UsageTarget,
    timeout: float,
    fallback_timestamp: str,
) -> tuple[str, list[str]]:
    try:
        return fetch_target(target, timeout, fallback_timestamp)
    except Exception:
        # A malformed third-party response must not prevent other configured
        # relays from refreshing, and the underlying exception may include a
        # host or remote text that the native window must never display.
        return "", []


def render(path: pathlib.Path, timeout: float) -> str:
    try:
        targets = active_usage_targets(path)
    except UsageConfigError:
        return "Configured relay logs are unavailable because the local configuration could not be read."
    if not targets:
        return "No configured relay exposes a usable credential for online usage logs."

    observed_at = _utc_now_iso()
    workers = min(MAX_WORKERS, len(targets))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(_fetch_target_safely, target, timeout, observed_at) for target in targets
        ]
        results = [future.result() for future in futures]

    sections: list[str] = []
    for target, (source, rows) in zip(targets, results):
        if source:
            body = "\n".join(rows) if rows else "No recent usage rows."
            sections.append(f"{target.provider} ({source})\n{body}")
    if not sections:
        return "No configured relay exposed a supported online usage-log endpoint."
    return f"Updated {observed_at}\n\n" + "\n\n".join(sections)


def main() -> int:
    parser = argparse.ArgumentParser(description="Print sanitized online relay usage logs.")
    parser.add_argument("--config", default=str(default_config_path()))
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()
    timeout = max(0.5, min(float(args.timeout), 15.0))
    print(render(pathlib.Path(args.config).expanduser(), timeout))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
