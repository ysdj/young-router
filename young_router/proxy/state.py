from __future__ import annotations

import asyncio
import contextvars
import json
import math
import os
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Optional

# Windows has no ``fcntl``: the advisory lock it guards here is a cooperative
# one between this app's own processes, and the platform without it already
# runs a single Core launcher.  Taken with the same guard the other shipped
# modules use, because this module is imported by the launcher, which must not
# reach the workers' runtime for a name like this one.
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows does not provide POSIX flock.
    fcntl = None  # type: ignore[assignment]

from ..atomic_io import atomic_write_text
from .. import runtime_store
from ..log_rotation import append_bounded_log, log_max_bytes
from .launcher_settings import (
    _DEPLOYMENT_COOLDOWN_FILE_ENV,
    _RECENT_REQUESTS_LOG_ENV,
    _ROUTE_RECOVERY_STATE_FILE_ENV,
    _SESSION_DEPLOYMENT_AFFINITY_FILE_ENV,
)


# An upstream attempt publishes a progress row before its call begins and a
# terminal row from the LiteLLM success/failure callbacks.  A client
# disconnect, a failover teardown, or a service restart can end an attempt
# without any terminal callback, which used to leave that row in an unbounded
# "sending" state in the log viewer.  These statuses mark a row as in flight;
# anything else is terminal for the attempt it belongs to.
_RECENT_REQUEST_IN_FLIGHT_STATUSES = frozenset({"pending", "sending", "stream"})
_RECENT_REQUEST_STALE_SECONDS_ENV = "YOUNG_ROUTER_LOG_REQUEST_STALE_SECONDS"
DEFAULT_RECENT_REQUEST_STALE_SECONDS = 900.0
_MIN_REQUEST_HEARTBEAT_SECONDS = 10.0
_MAX_REQUEST_HEARTBEAT_SECONDS = 60.0
_PENDING_RECENT_REQUESTS_MAX = 512
_RECENT_REQUESTS_SCAN_BYTES = 1024 * 1024
_PENDING_RECENT_REQUESTS: dict[str, dict[str, Any]] = {}
_PENDING_RECENT_REQUESTS_LOCK = threading.Lock()
_PENDING_SETTLEMENT_TASKS: set[Any] = set()

# Every in-flight start gets a serial number.  A settlement watch is scheduled
# for the attempt that was published when it was created, and a later attempt
# under the same request id (a retry, a fallback round) must not be closed by
# the watch the previous one left behind.
_ATTEMPT_SEQUENCE = 0
_ATTEMPT_SEQUENCE_LOCK = threading.Lock()


def _request_stale_seconds() -> float:
    """Seconds without progress after which an unfinished row is closed.

    ``0`` keeps unfinished rows open; the viewer then has to treat every row
    without a terminal callback as still active.
    """

    raw = os.getenv(_RECENT_REQUEST_STALE_SECONDS_ENV, "").strip()
    if not raw:
        return DEFAULT_RECENT_REQUEST_STALE_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_RECENT_REQUEST_STALE_SECONDS
    if not math.isfinite(value) or value <= 0:
        return 0.0
    return value


def _request_heartbeat_seconds(stale_seconds: float) -> float:
    """Throttle for rewriting an in-flight row while its stream progresses."""

    window = stale_seconds if stale_seconds > 0 else DEFAULT_RECENT_REQUEST_STALE_SECONDS
    return max(
        _MIN_REQUEST_HEARTBEAT_SECONDS,
        min(_MAX_REQUEST_HEARTBEAT_SECONDS, window / 6.0),
    )


def _aborted_recent_request_record(
    record: Mapping[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Terminal row for an attempt that will never report its own outcome.

    The attempt keeps its own timestamp and route fields so the row stays where
    it belongs in the time-sorted viewer; the close moment is recorded in the
    reason payload instead of replacing the observed time.
    """

    aborted = {
        key: value
        for key, value in record.items()
        if key
        not in {
            "status",
            "duration_ms",
            "time_to_first_token_ms",
            "first_stream_output_at",
            "response_cost",
            "usage",
            "error",
            "heartbeat",
        }
    }
    aborted["status"] = "aborted"
    aborted["aborted"] = {"reason": reason, "settled_at": _utc_now_iso()}
    return aborted


def _elapsed_milliseconds(started: Any, ended: Any) -> Optional[int]:
    """Milliseconds between two of this log's own ISO instants."""

    try:
        started_at = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        ended_at = datetime.fromisoformat(str(ended).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    elapsed = round((ended_at - started_at).total_seconds() * 1000)
    return elapsed if elapsed >= 0 else None


def _failed_recent_request_record(
    record: Mapping[str, Any],
    error: Mapping[str, Any],
    *,
    deployment_id: Any = None,
) -> dict[str, Any]:
    """Terminal row for an attempt whose client-facing stream ended in error.

    The row keeps the attempt's own route fields, so it stays attached to the
    deployment that failed; the close moment replaces the start because this is
    when the failure was observed (the same instant a failure callback would
    have stamped).
    """

    failed = {
        key: value
        for key, value in record.items()
        if key not in {"status", "heartbeat", "error", "duration_ms", "aborted"}
    }
    failed["status"] = "failure"
    failed["ts"] = _utc_now_iso()
    if (
        not failed.get("deployment_id")
        and isinstance(deployment_id, str)
        and deployment_id.strip()
    ):
        failed["deployment_id"] = deployment_id.strip()
    duration = _elapsed_milliseconds(record.get("ts"), failed["ts"])
    if duration is not None:
        failed["duration_ms"] = duration
    failed["error"] = dict(error)
    return failed


def _track_recent_request(
    record: Mapping[str, Any],
    *,
    attempt: Optional[int] = None,
) -> Optional[str]:
    """Keep an in-flight row joinable while its terminal callback may arrive.

    ``attempt`` repeats the start a heartbeat is refreshing.  A heartbeat is the
    same attempt still reporting, not a second one: giving it a new serial would
    leave the watch scheduled for the original start unable to recognise the row
    it was created for, and that row would then never be closed.
    """

    if not isinstance(record, Mapping):
        return None
    request_id = record.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        return None
    request_id = request_id.strip()
    status = str(record.get("status") or "").strip().casefold()
    now = time.monotonic()
    with _PENDING_RECENT_REQUESTS_LOCK:
        if status not in _RECENT_REQUEST_IN_FLIGHT_STATUSES:
            _PENDING_RECENT_REQUESTS.pop(request_id, None)
            return None
        # Re-inserting under the same key does not move a dict entry, so the
        # map is refreshed by popping first: the cap below evicts the *oldest*
        # entry, and an active stream must not be the one that is dropped.
        _PENDING_RECENT_REQUESTS.pop(request_id, None)
        _PENDING_RECENT_REQUESTS[request_id] = {
            "record": dict(record),
            "touched_at": now,
            # A row that starts after (or while) a client closed the previous
            # attempt says the client asked again, so the settlement clock
            # restarts with it instead of expiring the new attempt early.
            "reported_at": now,
            "attempt": attempt if attempt is not None else _next_attempt_sequence(),
        }
        while len(_PENDING_RECENT_REQUESTS) > _PENDING_RECENT_REQUESTS_MAX:
            _PENDING_RECENT_REQUESTS.pop(next(iter(_PENDING_RECENT_REQUESTS)))
    return request_id


def _touch_recent_request(
    request_id: Any,
    *,
    status: str = "stream",
    interval_seconds: Optional[float] = None,
) -> None:
    """Refresh one in-flight row while its upstream stream keeps progressing.

    The rewrite is throttled so a busy stream cannot flood the bounded request
    log, and the row keeps the position it already had in the viewer.
    """

    if not isinstance(request_id, str) or not request_id.strip():
        return
    request_id = request_id.strip()
    now = time.monotonic()
    interval = (
        float(interval_seconds)
        if interval_seconds is not None
        else _request_heartbeat_seconds(_request_stale_seconds())
    )
    with _PENDING_RECENT_REQUESTS_LOCK:
        entry = _PENDING_RECENT_REQUESTS.get(request_id)
        if entry is None:
            return
        entry["touched_at"] = now
        # A heartbeat is what says this row is still live, so it moves to the
        # end of the eviction order (see ``_track_recent_request``).
        _PENDING_RECENT_REQUESTS.pop(request_id, None)
        _PENDING_RECENT_REQUESTS[request_id] = entry
        if now - float(entry.get("reported_at") or 0.0) < interval:
            return
        entry["reported_at"] = now
        record = dict(entry["record"])
        attempt = entry.get("attempt")
    _append_recent_request(
        {
            **record,
            "status": status,
            "ts": _utc_now_iso(),
            "heartbeat": True,
        },
        attempt=attempt if isinstance(attempt, int) else None,
    )


def _next_attempt_sequence() -> int:
    global _ATTEMPT_SEQUENCE
    with _ATTEMPT_SEQUENCE_LOCK:
        _ATTEMPT_SEQUENCE += 1
        return _ATTEMPT_SEQUENCE


def _in_flight_attempt(request_id: Any) -> Optional[int]:
    """Serial number of the attempt this request id is currently reporting.

    A caller that just published a row reads it back here to say *which* start
    its settlement belongs to; the client-facing generator reads it to close
    whatever it was delivering.
    """

    if not isinstance(request_id, str) or not request_id.strip():
        return None
    with _PENDING_RECENT_REQUESTS_LOCK:
        entry = _PENDING_RECENT_REQUESTS.get(request_id.strip())
        return int(entry.get("attempt") or 0) if entry else None


def _settle_recent_request(
    request_id: str,
    reason: str,
    *,
    attempt: Optional[int] = None,
) -> bool:
    """Append the terminal row for an attempt that stopped reporting.

    ``attempt`` names the start this settlement was scheduled for.  A watch
    that wakes up after a later attempt replaced that start leaves the newer
    row alone: only the attempt it was created for is its to close.
    """

    with _PENDING_RECENT_REQUESTS_LOCK:
        entry = _PENDING_RECENT_REQUESTS.get(request_id)
        if entry is None:
            return False
        if attempt is not None and int(entry.get("attempt") or 0) != attempt:
            return False
        entry = _PENDING_RECENT_REQUESTS.pop(request_id, None)
    if entry is None:
        return False
    _append_recent_request(
        _aborted_recent_request_record(entry.get("record") or {}, reason)
    )
    return True


def _settle_failed_request(
    request_id: Any,
    error: Mapping[str, Any],
    *,
    deployment_id: Any = None,
    attempt: Optional[int] = None,
) -> bool:
    """Append the terminal failure row for an attempt that ended in an error.

    LiteLLM's streaming generators report a stream that fails through
    ``post_call_failure_hook``, not through the logging callback that writes a
    request row, so on the Messages and Chat wires no ``async_log_failure_event``
    follows: the row the turn published stayed 发送中 and the viewer renamed it
    已中断 once the stale window expired, which reported an interrupted turn
    where there had been a classified failure.

    The in-flight registry is what keeps this from doubling a failure the
    logging callback did report: either side closes the attempt, and only the
    first one finds the entry.
    """

    if not isinstance(request_id, str) or not request_id.strip():
        return False
    request_id = request_id.strip()
    with _PENDING_RECENT_REQUESTS_LOCK:
        entry = _PENDING_RECENT_REQUESTS.get(request_id)
        if entry is None:
            return False
        if attempt is not None and int(entry.get("attempt") or 0) != attempt:
            return False
        entry = _PENDING_RECENT_REQUESTS.pop(request_id, None)
    if entry is None:
        return False
    record = entry.get("record")
    stashed = entry.get("failure")
    stashed = stashed if isinstance(stashed, Mapping) else {}
    stashed_error = stashed.get("error")
    # The reason the hop itself gave outranks the wrapper a later layer built
    # around it: ``ServiceUnavailableError`` says "the stream ended", while the
    # stash names the 502 that ended it.
    effective_error = stashed_error if isinstance(stashed_error, Mapping) and stashed_error else error
    effective_deployment = deployment_id or stashed.get("deployment_id")
    _append_recent_request(
        _failed_recent_request_record(
            record if isinstance(record, Mapping) else {},
            effective_error,
            deployment_id=effective_deployment,
        )
    )
    return True


def _remember_failed_attempt(
    request_id: Any,
    error: Mapping[str, Any],
    *,
    deployment_id: Any = None,
) -> bool:
    """Remember how the attempt in flight failed, without writing its row yet.

    A refusal does not end the turn by itself: the router may move this turn to
    a sibling route, which publishes a new attempt row.  Writing the failure
    here would do it before anyone knows whether a client stop is what ended the
    stream; the row is therefore written when the router demonstrably moves on
    (``_close_abandoned_recent_request``) or when the turn ends in an error.
    """

    if not isinstance(request_id, str) or not request_id.strip():
        return False
    with _PENDING_RECENT_REQUESTS_LOCK:
        entry = _PENDING_RECENT_REQUESTS.get(request_id.strip())
        if entry is None:
            return False
        entry["failure"] = {
            "error": dict(error),
            "deployment_id": deployment_id,
        }
    return True


def _close_abandoned_recent_request(request_id: Any) -> bool:
    """Write the failure row of the attempt a new attempt is replacing.

    The router only starts another deployment call because this attempt is over,
    and its refusal is what ``_remember_failed_attempt`` kept.  Consuming it here
    is also what keeps a reader's stop from being reported as a route failure:
    nobody starts a new attempt after a client goes away.
    """

    if not isinstance(request_id, str) or not request_id.strip():
        return False
    request_id = request_id.strip()
    with _PENDING_RECENT_REQUESTS_LOCK:
        entry = _PENDING_RECENT_REQUESTS.get(request_id)
        stashed = dict(entry.get("failure") or {}) if isinstance(entry, Mapping) else {}
    if not stashed:
        return False
    return _settle_failed_request(
        request_id,
        stashed.get("error") or {},
        deployment_id=stashed.get("deployment_id"),
    )


async def _settle_recent_request_after(
    request_id: str,
    stale_seconds: float,
    attempt: Optional[int] = None,
) -> None:
    while True:
        with _PENDING_RECENT_REQUESTS_LOCK:
            entry = _PENDING_RECENT_REQUESTS.get(request_id)
            if entry is None:
                return
            remaining = stale_seconds - (
                time.monotonic() - float(entry.get("touched_at") or 0.0)
            )
        if remaining <= 0:
            break
        await asyncio.sleep(min(remaining, _MAX_REQUEST_HEARTBEAT_SECONDS))
    _settle_recent_request(request_id, "no_terminal_callback", attempt=attempt)


def _schedule_recent_request_settlement(record: Mapping[str, Any]) -> None:
    """Close an in-flight row the runtime can prove will not report again."""

    if not isinstance(record, Mapping):
        return
    stale_seconds = _request_stale_seconds()
    request_id = record.get("request_id")
    if not stale_seconds or not isinstance(request_id, str) or not request_id.strip():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    # An empty context, deliberately: a task inherits a snapshot of every
    # context variable its creator holds, and this one is created from inside a
    # request whose context carries the replay -- the multi-megabyte body, its
    # images, and the route marker.  The watch only sleeps for the stale window
    # (fifteen minutes by default) and reads module state, so it needs none of
    # it; inheriting it pinned one body per in-flight attempt.  Measured
    # 2026-11-12 on the gateway: a worker at 380 MB idle held 1.8 GB of
    # anonymous heap after a few hundred Codex turns, at a rate of one watch per
    # attempt and a fifteen-minute window -- ~900 live snapshots of a 2 MB
    # replay.  Eight such workers took the box past its memory (sshd stopped
    # answering; only a power-cycle from the console brought it back).
    task = loop.create_task(
        _settle_recent_request_after(
            request_id.strip(),
            stale_seconds,
            _in_flight_attempt(request_id),
        ),
        context=contextvars.Context(),
    )
    _PENDING_SETTLEMENT_TASKS.add(task)
    task.add_done_callback(_PENDING_SETTLEMENT_TASKS.discard)


def settle_stale_recent_requests(*, reason: str = "service_restart") -> int:
    """Close request rows left in flight by an earlier proxy process.

    A new proxy process owns an empty in-flight registry, so every row that is
    still in an in-flight status inside the bounded request-log tail belongs to
    an earlier process and can never reach its terminal callback.  Append one
    aborted row per affected request id instead of leaving those rows reporting
    an unbounded sending state.
    """

    path = _recent_requests_log_path()
    if not path:
        return 0
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _RECENT_REQUESTS_SCAN_BYTES))
            payload = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return 0
    last_records: dict[str, Mapping[str, Any]] = {}
    for line in payload.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(record, Mapping):
            continue
        request_id = record.get("request_id")
        if isinstance(request_id, str) and request_id.strip():
            last_records[request_id.strip()] = record
    settled = 0
    for record in last_records.values():
        if (
            str(record.get("status") or "").strip().casefold()
            not in _RECENT_REQUEST_IN_FLIGHT_STATUSES
        ):
            continue
        _append_recent_request(_aborted_recent_request_record(record, reason))
        settled += 1
    return settled



def _recent_requests_log_path() -> Optional[str]:
    value = os.getenv(_RECENT_REQUESTS_LOG_ENV, "").strip()
    if value:
        return value
    runtime_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.basename(runtime_dir) == ".litellm-runtime":
        return os.path.join(os.path.dirname(runtime_dir), "recent-requests.jsonl")
    return None


# Where a deployment says its own state lives.  A desktop install imports the
# package out of ``<root>/.litellm-runtime`` and needs nothing; a gateway imports
# it out of an immutable snapshot directory, and without one of these it had no
# runtime root at all -- so the cooldown document, the cross-worker concurrency
# leases and the route-recovery state silently became per-process memory.
# Observed live 2026-11-04 on the gateway: a route refused on every request and
# no cooldown was ever written, because each of eight workers kept its own count
# and never reached the failure threshold.
_RUNTIME_ROOT_ENV = "LITELLM_RUNTIME_ROOT"
_RUNTIME_ROOT_FALLBACK_ENV = "YOUNG_ROUTER_HOME"


def _runtime_root_from_this_file() -> Optional[str]:
    for name in (_RUNTIME_ROOT_ENV, _RUNTIME_ROOT_FALLBACK_ENV):
        configured = str(os.getenv(name, "") or "").strip()
        if configured:
            return os.path.expanduser(configured)
    runtime_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.basename(runtime_dir) == ".litellm-runtime":
        return os.path.dirname(runtime_dir)
    return None


def _deployment_cooldown_file_path() -> Optional[str]:
    value = os.getenv(_DEPLOYMENT_COOLDOWN_FILE_ENV, "").strip()
    if value:
        return value
    runtime_root = _runtime_root_from_this_file()
    if not runtime_root:
        return None
    return os.path.join(runtime_root, ".litellm-runtime", "deployment-cooldowns.json")


def _route_recovery_state_file_path() -> Optional[str]:
    value = os.getenv(_ROUTE_RECOVERY_STATE_FILE_ENV, "").strip()
    if value:
        return value
    runtime_root = _runtime_root_from_this_file()
    if not runtime_root:
        return None
    return os.path.join(runtime_root, ".litellm-runtime", "route-recovery-state.json")


def _session_deployment_affinity_file_path() -> Optional[str]:
    value = os.getenv(_SESSION_DEPLOYMENT_AFFINITY_FILE_ENV, "").strip()
    if value:
        return value
    runtime_root = _runtime_root_from_this_file()
    if not runtime_root:
        return None
    return os.path.join(runtime_root, ".litellm-runtime", "session-deployment-affinity.json")


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _locked_json_state_update(path: str, callback: Any, *, read_only: bool = False) -> Any:
    """Run one read-modify-write of a shared document under its own lock.

    The lock is now the store's write transaction (see ``runtime_store``): one
    ``BEGIN IMMEDIATE`` around the read, the callback and the write, which is
    the same guarantee the ``flock`` gave on this platform and gives on the one
    without ``fcntl`` -- sixteen workers on Windows shared these documents with
    no exclusion at all.  A document is still parsed and serialised whole; what
    went away is the sidecar lock file, the two fsyncs and the directory fsync
    every write paid while holding it.

    ``read_only`` is for the callers that only count or filter -- the cooldown
    and protocol-fallback filters run on every attempt.  They answer from a
    snapshot without taking the write lock, and a prune they perform is
    carried out by the next writer instead.
    """

    return runtime_store.update_document(path, callback, read_only=read_only)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _recent_requests_max_bytes() -> int:
    return log_max_bytes()


def _append_recent_request(
    record: dict[str, Any],
    *,
    attempt: Optional[int] = None,
) -> Optional[str]:
    """Append one request row and keep the in-flight registry current.

    The registry is updated before the write so a terminal callback always
    clears the attempt's tracking first: a settlement task that already passed
    its own check can then only append the aborted row *before* the real
    terminal row, and the viewer keeps the real outcome.
    """

    request_id = _track_recent_request(record, attempt=attempt)
    path = _recent_requests_log_path()
    if not path:
        return request_id
    try:
        line = (
            json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n"
        ).encode("utf-8")
        append_bounded_log(path, line, maximum_bytes=_recent_requests_max_bytes())
    except Exception:
        pass
    return request_id


def _safe_log_text(value: Any, *, limit: int = 180) -> Optional[str]:
    if value is None:
        return None
    # Imported here: this module is loaded by the launcher, which must not pull
    # the workers' runtime into its own startup.
    from . import trace as _trace_module

    text = _trace_module._sanitize_trace_text(str(value), limit=limit)
    return text or None


def _upsert_route_recovery_state(record: dict[str, Any]) -> None:
    path = _route_recovery_state_file_path()
    if not path:
        return
    key = _safe_log_text(record.get("key"), limit=240)
    if not key:
        return
    now = _utc_now_iso()

    def update(payload: dict[str, Any]) -> None:
        recoveries = payload.setdefault("recoveries", {})
        if not isinstance(recoveries, dict):
            recoveries = {}
            payload["recoveries"] = recoveries
        existing = recoveries.get(key)
        if not isinstance(existing, dict):
            existing = {}
        merged = {**existing}
        for item_key, item_value in record.items():
            if item_value not in (None, "", [], {}):
                merged[item_key] = item_value
        merged["key"] = key
        merged["status"] = str(merged.get("status") or "polling")
        merged["started_at"] = existing.get("started_at") or merged.get("started_at") or now
        merged["heartbeat_at"] = now
        merged["updated_at"] = now
        recoveries[key] = merged
        payload["updated_at"] = now

    try:
        _locked_json_state_update(path, update)
    except Exception:
        pass


def _touch_route_recovery_state(key: str) -> None:
    path = _route_recovery_state_file_path()
    safe_key = _safe_log_text(key, limit=240)
    if not path or not safe_key:
        return
    now = _utc_now_iso()

    def update(payload: dict[str, Any]) -> None:
        recoveries = payload.setdefault("recoveries", {})
        if not isinstance(recoveries, dict):
            return
        existing = recoveries.get(safe_key)
        if not isinstance(existing, dict):
            return
        existing["heartbeat_at"] = now
        existing["updated_at"] = now
        cooldown_until = existing.get("cooldown_until")
        try:
            remaining = max(0.0, float(cooldown_until) - time.time())
        except (TypeError, ValueError):
            remaining = None
        if remaining is not None:
            existing["cooldown_remaining_seconds"] = round(remaining, 3)
        payload["updated_at"] = now

    try:
        _locked_json_state_update(path, update)
    except Exception:
        pass


def _remove_route_recovery_state(key: str) -> None:
    path = _route_recovery_state_file_path()
    safe_key = _safe_log_text(key, limit=240)
    if not path or not safe_key:
        return

    def update(payload: dict[str, Any]) -> None:
        recoveries = payload.setdefault("recoveries", {})
        if isinstance(recoveries, dict):
            recoveries.pop(safe_key, None)
        payload["updated_at"] = _utc_now_iso()

    try:
        _locked_json_state_update(path, update)
    except Exception:
        pass
