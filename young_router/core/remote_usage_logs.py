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
import hashlib
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

from ..config.load import load_config
from young_router.api_base import isolated_http_opener, service_root
from young_router.browser_identity import browser_request_headers


MAX_TARGETS = 4
MAX_WORKERS = 4
PAGE_SIZE = 80
# How many rows one key of one station may contribute. The log view holds up
# to MAX_LINES and states its own limit in the status strip, so a reader cap
# below that would silently drop rows the view was willing to show. It stays
# bounded because the response is third-party and must not grow without limit.
MAX_ROWS_PER_TARGET = 2_000
DEFAULT_TIMEOUT_SECONDS = 4.0
# A relay answers a log page as one JSON document, and a station's own page
# size is often 1000 rows: a real, valid answer is routinely several hundred
# kilobytes, so a small cap rejected the busiest keys as if they were broken.
# The bound still exists, because the response is third-party and must not be
# read without limit; it is stated in rows so it tracks what the reader uses.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class UsageConfigError(ValueError):
    """The current LiteLLM configuration could not be read safely."""

# Which outcome a read ended in. The pane states each one in the user's own
# language, so Core answers with a stable code rather than a sentence that only
# ever reads correctly in English.
USAGE_REASON_CONFIG_UNREADABLE = "config_unreadable"
USAGE_REASON_UNKNOWN_SOURCE = "unknown_source"
USAGE_REASON_NO_CREDENTIAL = "no_credential"
USAGE_REASON_UNSUPPORTED = "unsupported"
USAGE_REASON_NO_ROWS = "no_rows"
USAGE_REASON_UNAVAILABLE = "unavailable"


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


def _provider_keys(provider: dict[str, Any]) -> tuple[dict[str, str], dict[str, str], list[str]]:
    by_name: dict[str, str] = {}
    by_id: dict[str, str] = {}
    ordered: list[str] = []
    raw_keys = provider.get("api_keys")
    if not isinstance(raw_keys, list):
        return by_name, by_id, ordered
    for index, item in enumerate(raw_keys, start=1):
        if not isinstance(item, dict):
            continue
        name = _string(item.get("name")) or f"key-{index}"
        value = _credential_value(item.get("value"))
        if not value or name in by_name:
            continue
        by_name[name] = value
        key_id = _string(item.get("id"))
        if key_id:
            by_id[key_id] = value
        ordered.append(value)
    return by_name, by_id, ordered


def _credential_for_model(provider: dict[str, Any], model: dict[str, Any]) -> str:
    direct = _credential_value(model.get("api_key"))
    if direct:
        return direct
    by_name, by_id, ordered = _provider_keys(provider)
    # The slot is the key the route uses, and a name only decides when no slot is
    # stated.  A route that states a key this provider does not carry has no
    # usage to read: answering with the provider's default (or its only key)
    # would present another account's numbers as this route's.
    key_id = _string(model.get("provider_key_id"))
    preferred_name = _string(model.get("api_key_name"))
    if key_id:
        return by_id.get(key_id, "")
    if preferred_name:
        return by_name.get(preferred_name, "")
    if "default" in by_name:
        return by_name["default"]
    return ordered[0] if len(ordered) == 1 else ""


@dataclass(frozen=True)
class UsageTarget:
    provider: str
    api_base: str
    api_key: str
    # The station's other configured credentials, tried in order when the
    # first cannot read the feed. A station keeps one identity in the dropdown
    # however many keys it carries.
    fallback_keys: tuple[str, ...] = ()

    def credentials(self) -> tuple[str, ...]:
        return (self.api_key, *self.fallback_keys)

    @property
    def origin(self) -> str:
        """The station's own address, as the relay panes state it."""

        return service_root(self.api_base) or self.api_base

    def display(self) -> str:
        """The relay's name as the user knows it.

        ``provider`` is the name the user gave the station when they added it
        (``sisct2``), which is what the relay panes list and what the user is
        looking for in a dropdown.  The family that answered is carried
        separately as ``source_family``, because it is its own column and
        fusing it to the name only made the name column too wide to read.
        """

        return self.provider

    @staticmethod
    def family_label(family: str) -> str:
        """The control plane that answered, named the way the panes name it."""

        if not family:
            return ""
        return "Sub2API" if family == "sub2api" else "NewAPI"

    @property
    def source_id(self) -> str:
        """A stable, non-secret handle for one configured relay station.

        The id names the *station*, never a credential: it is derived from the
        control-plane root alone, so every route that points at one station
        resolves to the same entry, and the React side hands a selection back
        without ever seeing the API key it selects.
        """

        digest = hashlib.sha256(self.origin.encode("utf-8")).hexdigest()
        return digest[:16]


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def default_config_path() -> pathlib.Path:
    configured = os.environ.get("LITELLM_CONFIG_FILE", "").strip()
    if configured:
        return pathlib.Path(configured).expanduser()
    root = os.environ.get("LITELLM_RUNTIME_ROOT", "").strip() or os.environ.get(
        "YOUNG_ROUTER_HOME", ""
    ).strip()
    if root:
        return pathlib.Path(root).expanduser() / "config.yaml"
    return pathlib.Path.home() / ".young-router" / "config.yaml"


def active_usage_targets(path: pathlib.Path) -> list[UsageTarget]:
    """Return one safe read target for each configured relay station.

    A station is reached by whichever of its credentials can read the usage
    feed, so its keys are collected onto one target instead of becoming
    several: reading the same station once per key would list the same rows
    again, and a station with more keys than the target budget would crowd
    every other station out of the dropdown.
    """
    try:
        payload = load_config(path)
    except Exception as exc:
        raise UsageConfigError("The LiteLLM configuration could not be read.") from exc
    raw_providers = payload.get("providers")
    if not isinstance(raw_providers, list):
        raise UsageConfigError("The LiteLLM configuration has no provider list.")

    # The station's own root is its identity: several configured routes (and
    # several keys within one route) that point at the same control plane are
    # one relay to read and one entry to choose.
    order: list[str] = []
    roots: dict[str, str] = {}
    labels: dict[str, str] = {}
    credentials: dict[str, list[str]] = {}
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
            if not root:
                continue
            if root not in roots:
                order.append(root)
                roots[root] = api_base
                labels[root] = provider_name
                credentials[root] = []
            if api_key and api_key not in credentials[root]:
                credentials[root].append(api_key)

    targets: list[UsageTarget] = []
    for root in order:
        keys = credentials[root]
        if not keys:
            # A station with no readable credential has no usage to read, and
            # offering it would be a dropdown entry that always answers empty.
            continue
        targets.append(
            UsageTarget(
                provider=labels[root],
                api_base=roots[root],
                api_key=keys[0],
                fallback_keys=tuple(keys[1:]),
            )
        )
        if len(targets) >= MAX_TARGETS:
            break
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
            **browser_request_headers(),
            "Authorization": authorization,
        },
        method="GET",
    )
    try:
        with isolated_http_opener().open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", response.getcode()))
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        # A refusal is not the same fact as a missing route: 401/403 says the
        # endpoint exists and this credential may not read it, while 404 says
        # the station does not install it.  The difference decides whether the
        # next credential is worth trying.
        if exc.code in {401, 403}:
            return "refused", None
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


def _row_details(row: dict[str, Any]) -> dict[str, str]:
    """Project one usage row into the fields the log view renders.

    Only documented, presentational keys survive: a relay's response is
    third-party data, and the pane must never show a request body, an IP, a
    path, or anything else the station happened to include.  Every value is a
    string so the record is the same shape whichever endpoint answered.
    """

    fields = {
        "model": _safe_text(
            row.get("model_name") or row.get("model") or row.get("requested_model"),
            96,
        ),
        "status": _safe_text(row.get("status") or row.get("request_type"), 32),
        "channel": _safe_text(row.get("channel_name") or row.get("channel"), 64),
        "group": _safe_text(row.get("group_name") or row.get("group"), 64),
        "key_name": _safe_text(row.get("token_name") or row.get("key_name"), 64),
        "duration": _safe_number(row.get("use_time") or row.get("duration")),
        "prompt_tokens": _safe_number(row.get("prompt_tokens") or row.get("input_tokens")),
        "completion_tokens": _safe_number(
            row.get("completion_tokens") or row.get("output_tokens")
        ),
        "cache_tokens": _safe_number(
            row.get("cache_read_tokens") or row.get("cache_creation_tokens")
        ),
        "total_tokens": _total_tokens(row),
        "cost": _safe_number(row.get("quota") or row.get("cost") or row.get("amount")),
    }
    return {key: value for key, value in fields.items() if value}


def _row_text(record: dict[str, Any], fallback_timestamp: str) -> str:
    timestamp = record.get("timestamp") or fallback_timestamp
    model = record.get("model") or ""
    status = record.get("status") or ""
    tokens = record.get("total_tokens") or ""
    pieces = [
        part
        for part in [timestamp, model, status, f"tokens={tokens}" if tokens else ""]
        if part
    ]
    return "  ".join(pieces)


def _row_record(
    raw: dict[str, Any],
    target: UsageTarget,
    family: str,
    fallback_timestamp: str,
) -> dict[str, Any] | None:
    """Build one usage record carrying its relay's full name and its fields."""

    timestamp = _safe_timestamp(
        raw.get("created_at") or raw.get("createdAt") or raw.get("timestamp")
    ) or fallback_timestamp
    details = _row_details(raw)
    if not details.get("model") and not details.get("total_tokens"):
        # A row that names neither a model nor a token count is the station's
        # own placeholder; rendering it would be a blank line with a relay name.
        return None
    record: dict[str, Any] = {
        "timestamp": timestamp,
        "source": family,
        "source_id": target.source_id,
        "source_label": target.display(),
        "source_family": target.family_label(family),
    }
    record.update(details)
    return record


def fetch_target(
    target: UsageTarget,
    timeout: float,
    fallback_timestamp: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Read every one of the station's keys and merge what they report.

    A station scopes its log feed per key, so one key answers for the traffic
    it served and nothing else: the key an account happens to list first can
    legitimately answer zero rows while another is the busiest.  Reading only
    the first key that answered therefore reported "no usage" for a station
    the router was actively serving, which is why every key is read and their
    rows are merged.

    Which endpoint shape a station installs is a property of the *station*,
    not of a key, so it is settled once and the keys are then read against
    that one shape.  Probing every shape with every key multiplied the
    requests a refresh costs by the number of keys, and each request is the
    slow part.
    """

    fallback_timestamp = fallback_timestamp or _utc_now_iso()
    for kind, endpoint, authorization_scheme in _endpoint_candidates(target.api_base):
        credentials = target.credentials()
        if not credentials:
            continue
        probe = (kind, endpoint, authorization_scheme)
        # One key settles the shape.  A refusal settles it too: it proves the
        # endpoint exists, and only tells us this particular key cannot read
        # it, so the remaining keys are still worth spending on it.
        first_result, _payload, first_rows = _fetch_key(credentials[0], probe, timeout, kind)
        answered = first_result == "ok"
        if not answered and first_result != "refused":
            continue
        records: list[dict[str, Any]] = []
        if first_rows:
            records.extend(_records_from(first_rows, target, kind, fallback_timestamp))
        rest = credentials[1:]
        if rest:
            # The remaining keys are independent reads against one station, so
            # they run together instead of one after another.
            workers = min(MAX_WORKERS, len(rest))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(_fetch_key, key, probe, timeout, kind) for key in rest]
                for future in futures:
                    result, _payload, rows = future.result()
                    if result == "ok" and rows:
                        records.extend(_records_from(rows, target, kind, fallback_timestamp))
        if answered or records:
            return kind, _deduplicated_records(records)
    return "", []


def _fetch_key(
    api_key: str,
    probe: tuple[str, str, str],
    timeout: float,
    kind: str,
) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]] | None]:
    """Read one key's page: its result, the payload, and the recognized rows."""

    _kind, endpoint, authorization_scheme = probe
    result, payload = _fetch_json(endpoint, api_key, timeout, authorization_scheme)
    if result != "ok" or payload is None:
        return result, payload, None
    rows = _items(payload)
    if rows is None and kind == "sub2api":
        rows = _summary_rows(payload)
    return result, payload, rows


def _records_from(
    rows: list[dict[str, Any]],
    target: UsageTarget,
    kind: str,
    fallback_timestamp: str,
) -> list[dict[str, Any]]:
    return [
        record
        for record in (
            _row_record(row, target, kind, fallback_timestamp)
            for row in rows[:MAX_ROWS_PER_TARGET]
        )
        if record
    ]


def _deduplicated_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop rows the station reported through more than one key.

    A key-scoped feed can overlap, and the same request appearing twice would
    read as two requests the user made.  Rows are identified by what the
    station stated about them, so a genuine repeat (same model, same status,
    same counts at the same instant) is the only thing collapsed.
    """

    seen: set[tuple[Any, ...]] = set()
    unique: list[dict[str, Any]] = []
    for record in records:
        identity = (
            record.get("timestamp"),
            record.get("model"),
            record.get("status"),
            record.get("prompt_tokens"),
            record.get("completion_tokens"),
            record.get("total_tokens"),
            record.get("key_name"),
        )
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(record)
    return unique


def _fetch_target_safely(
    target: UsageTarget,
    timeout: float,
    fallback_timestamp: str,
) -> tuple[str, list[dict[str, Any]]]:
    try:
        return fetch_target(target, timeout, fallback_timestamp)
    except Exception:
        # A malformed third-party response must not prevent other configured
        # relays from refreshing, and the underlying exception may include a
        # host or remote text that the native window must never display.
        return "", []


def usage_sources(path: pathlib.Path) -> list[dict[str, str]]:
    """List the selectable read targets without exposing any credential.

    The dropdown that names them is what decides the scope of a refresh, so the
    list must be safe to render: an id and the station's own name.  A source
    that yields no usable credential is simply absent, which is how the UI says
    there is nothing to read.
    """

    try:
        targets = active_usage_targets(path)
    except UsageConfigError:
        return []
    return [
        {"id": target.source_id, "label": target.display()}
        for target in targets
    ]


def collect(
    path: pathlib.Path,
    timeout: float,
    source_id: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Gather every configured relay's usage rows as structured records.

    Returns the records and a status sentence for the view to show when there
    is nothing to list.  The caller renders the rows; this never formats the
    station's data into prose, because the pane owns how a row looks.
    """

    try:
        targets = active_usage_targets(path)
    except UsageConfigError:
        return [], USAGE_REASON_CONFIG_UNREADABLE
    if source_id:
        # An unknown id is not an error the user can fix: the configuration may
        # have moved under a dropdown whose selection is one refresh old. Read
        # nothing rather than silently answering for a different relay.
        targets = [target for target in targets if target.source_id == source_id]
        if not targets:
            return [], USAGE_REASON_UNKNOWN_SOURCE
    if not targets:
        return [], USAGE_REASON_NO_CREDENTIAL

    observed_at = _utc_now_iso()
    workers = min(MAX_WORKERS, len(targets))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(_fetch_target_safely, target, timeout, observed_at) for target in targets
        ]
        results = [future.result() for future in futures]

    records: list[dict[str, Any]] = []
    readable = False
    for (source, rows) in results:
        if not source:
            continue
        readable = True
        records.extend(rows)
    if not readable:
        return [], USAGE_REASON_UNSUPPORTED
    return records, ""


def render(
    path: pathlib.Path,
    timeout: float,
    source_id: str | None = None,
) -> str:
    """The CLI's own plain-text presentation of the same records."""

    records, status = collect(path, timeout, source_id)
    if status:
        return status
    if not records:
        return "No recent usage rows."
    groups: dict[str, list[str]] = {}
    for record in records:
        label = str(record.get("source_label") or "Relay")
        kind = str(record.get("source_family") or "")
        heading = f"{label} ({kind})" if kind else label
        groups.setdefault(heading, []).append(_row_text(record, _utc_now_iso()))
    sections = [f"{heading}\n" + "\n".join(rows) for heading, rows in groups.items()]
    return f"Updated {_utc_now_iso()}\n\n" + "\n\n".join(sections)


def main() -> int:
    parser = argparse.ArgumentParser(description="Print sanitized online relay usage logs.")
    parser.add_argument("--config", default=str(default_config_path()))
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--source", default="", help="Read only the relay whose source id this is.")
    args = parser.parse_args()
    timeout = max(0.5, min(float(args.timeout), 15.0))
    print(
        render(
            pathlib.Path(args.config).expanduser(),
            timeout,
            _string(args.source) or None,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
