from __future__ import annotations

import asyncio
import math
import threading
from collections.abc import Mapping

from . import trace as _trace_module
from .log_rotation import MIN_LOG_MAX_BYTES, append_bounded_log, log_max_bytes


from .base import (
    Any,
    Optional,
    _DEPLOYMENT_COOLDOWN_FILE_ENV,
    _RECENT_REQUESTS_LOG_ENV,
    _ROUTE_RECOVERY_STATE_FILE_ENV,
    _SESSION_DEPLOYMENT_AFFINITY_FILE_ENV,
    datetime,
    fcntl,
    json,
    os,
    time,
    timezone,
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


def _track_recent_request(record: Mapping[str, Any]) -> Optional[str]:
    """Keep an in-flight row joinable while its terminal callback may arrive."""

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
        tracked = _PENDING_RECENT_REQUESTS.get(request_id) or {}
        _PENDING_RECENT_REQUESTS[request_id] = {
            "record": dict(record),
            "touched_at": now,
            "reported_at": tracked.get("reported_at", now),
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
        if now - float(entry.get("reported_at") or 0.0) < interval:
            return
        entry["reported_at"] = now
        record = dict(entry["record"])
    _append_recent_request(
        {
            **record,
            "status": status,
            "ts": _utc_now_iso(),
            "heartbeat": True,
        }
    )


def _settle_recent_request(request_id: str, reason: str) -> bool:
    """Append the terminal row for an attempt that stopped reporting."""

    with _PENDING_RECENT_REQUESTS_LOCK:
        entry = _PENDING_RECENT_REQUESTS.pop(request_id, None)
    if entry is None:
        return False
    _append_recent_request(
        _aborted_recent_request_record(entry.get("record") or {}, reason)
    )
    return True


async def _settle_recent_request_after(
    request_id: str,
    stale_seconds: float,
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
    _settle_recent_request(request_id, "no_terminal_callback")


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
    task = loop.create_task(
        _settle_recent_request_after(request_id.strip(), stale_seconds)
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


def _runtime_root_from_this_file() -> Optional[str]:
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


def _atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp.{os.getpid()}.{time.time_ns()}"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, path)


def _locked_json_state_update(path: str, callback: Any) -> Any:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    lock_path = f"{path}.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        result = callback(payload)
        _atomic_write_json(path, payload)
        return result
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _recent_requests_max_bytes() -> int:
    return log_max_bytes()


def _append_recent_request(record: dict[str, Any]) -> Optional[str]:
    """Append one request row and keep the in-flight registry current.

    The registry is updated before the write so a terminal callback always
    clears the attempt's tracking first: a settlement task that already passed
    its own check can then only append the aborted row *before* the real
    terminal row, and the viewer keeps the real outcome.
    """

    request_id = _track_recent_request(record)
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
