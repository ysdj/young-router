from __future__ import annotations

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


def _append_recent_request(record: dict[str, Any]) -> None:
    path = _recent_requests_log_path()
    if not path:
        return
    try:
        line = (
            json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n"
        ).encode("utf-8")
        append_bounded_log(path, line, maximum_bytes=_recent_requests_max_bytes())
    except Exception:
        pass


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
