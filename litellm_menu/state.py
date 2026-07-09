from __future__ import annotations

from . import trace as _trace_module


from .base import (
    Any,
    Optional,
    _DEPLOYMENT_COOLDOWN_FILE_ENV,
    _RECENT_REQUESTS_DEFAULT_MAX_BYTES,
    _RECENT_REQUESTS_LOG_ENV,
    _RECENT_REQUESTS_MAX_BYTES_ENV,
    _RECENT_REQUESTS_MIN_MAX_BYTES,
    _ROUTE_RECOVERY_STATE_FILE_ENV,
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
    value = os.getenv(_RECENT_REQUESTS_MAX_BYTES_ENV, "").strip()
    if not value:
        return _RECENT_REQUESTS_DEFAULT_MAX_BYTES
    try:
        parsed = int(value)
    except ValueError:
        return _RECENT_REQUESTS_DEFAULT_MAX_BYTES
    return max(parsed, _RECENT_REQUESTS_MIN_MAX_BYTES)


def _rotate_recent_requests_log_if_needed(path: str) -> None:
    max_bytes = _recent_requests_max_bytes()
    backup_path = f"{path}.1"
    backup_temp_path = f"{backup_path}.rotate.tmp"
    try:
        if os.path.getsize(backup_path) > max_bytes:
            with open(backup_path, "rb") as source:
                try:
                    source.seek(-max_bytes, os.SEEK_END)
                except OSError:
                    source.seek(0)
                tail = source.read(max_bytes)
            with open(backup_temp_path, "wb") as target:
                target.write(tail)
            try:
                os.chmod(backup_temp_path, 0o600)
            except OSError:
                pass
            os.replace(backup_temp_path, backup_path)
    except FileNotFoundError:
        pass
    except OSError:
        try:
            os.unlink(backup_temp_path)
        except OSError:
            pass

    try:
        if os.path.getsize(path) <= max_bytes:
            return
    except FileNotFoundError:
        return
    except OSError:
        return

    temp_path = f"{path}.rotate.tmp"
    try:
        with open(path, "rb") as source:
            try:
                source.seek(-max_bytes, os.SEEK_END)
            except OSError:
                source.seek(0)
            tail = source.read(max_bytes)
        with open(temp_path, "wb") as target:
            target.write(tail)
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        os.replace(temp_path, backup_path)
        with open(path, "wb") as current:
            current.write(tail)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        pass


def _append_recent_request(record: dict[str, Any]) -> None:
    path = _recent_requests_log_path()
    if not path:
        return
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        _rotate_recent_requests_log_if_needed(path)
        line = (
            json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n"
        ).encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
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
        merged["updated_at"] = now
        recoveries[key] = merged
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
