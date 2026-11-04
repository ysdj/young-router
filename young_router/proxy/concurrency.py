"""How many requests one deployment is serving at once, shared by every worker.

LiteLLM's own ``max_parallel_requests`` holds a per-process semaphore: its
client cache is ``local_only``, so sixteen uvicorn workers each count their own
requests.  A model capped at 8 therefore reached 8 x 16 in flight, which is not
a cap.  The count has to live in a file every worker locks, exactly like the
deployment cooldowns beside it, and it is this module that owns that file.

Two consequences a reader would otherwise break:

* A slot belongs to the *turn* and the deployment, not to the attempt.  A
  fallback resends one turn to a peer, so it takes the peer's slot and gives the
  first route's back; a turn that re-enters the same deployment (a protocol
  fallback retry) keeps the one slot it already holds instead of taking two.
* A slot whose owner is gone must stop counting.  A lease records its owner's
  pid and is pruned when that pid is no longer alive, because a killed worker
  never releases anything and a two-hour stale slot is a route that silently
  serves less than it should.  The proxy also clears every lease when it starts,
  since every lease in the file then belongs to a process that is gone.

The cost is one lock acquisition per claim and per release.  That is the price
of an accurate number: a counter kept in memory is not shared, and a counter
shared without a lock is not a counter.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Iterable, Mapping
from typing import Any, Optional

from . import state as _state_module
from .launcher_settings import _DEPLOYMENT_CONCURRENCY_FILE_ENV


#: The Runtime Settings key a route with no cap of its own follows, and the
#: value it takes when it states nothing.  Stated here because this module is
#: the one reader that has to answer before any settings document is parsed.
MODEL_CONCURRENCY_LIMIT_ENV = "YOUNG_ROUTER_MODEL_CONCURRENCY_LIMIT"
MODEL_CONCURRENCY_LIMIT_DEFAULT = 16
#: The ``model_info`` key one route states its own cap in.  Read on the
#: deployment the Router already holds, so no second lookup decides a cap.
MODEL_CONCURRENCY_LIMIT_KEY = "max_concurrency"
MODEL_CONCURRENCY_MAX = 4096

#: The proxy's own request budget, which is also the longest a live request can
#: hold a slot.  A lease outliving that is a lease nobody is behind anymore.
_REQUEST_TIMEOUT_ENV = "YOUNG_ROUTER_REQUEST_TIMEOUT_SECONDS"
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 7200.0
_LEASE_MARGIN_SECONDS = 120.0
_MIN_LEASE_SECONDS = 300.0


def _env_int(name: str) -> Optional[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def model_concurrency_limit() -> int:
    """How many requests one route may serve at once when it states no cap.

    ``0`` disables the cap entirely, which is how an operator restores plain
    LiteLLM routing.  A value this build cannot read falls back to the default
    rather than to no limit: a typo must not silently remove the protection.
    """

    value = _env_int(MODEL_CONCURRENCY_LIMIT_ENV)
    if value is None or value < 0:
        return MODEL_CONCURRENCY_LIMIT_DEFAULT
    return value


def model_concurrency_cap(deployment: Any) -> int:
    """The cap one deployment serves under: its own, else the global default.

    ``None``/absent on the route is the only value that inherits, so a route
    that states ``0`` stays explicitly unlimited even when the default is set.
    """

    if not isinstance(deployment, Mapping):
        return model_concurrency_limit()
    model_info = deployment.get("model_info")
    if not isinstance(model_info, Mapping):
        return model_concurrency_limit()
    raw = model_info.get(MODEL_CONCURRENCY_LIMIT_KEY)
    if raw is None or isinstance(raw, bool):
        return model_concurrency_limit()
    try:
        stated = int(str(raw).strip())
    except (TypeError, ValueError):
        return model_concurrency_limit()
    if stated < 0:
        return model_concurrency_limit()
    return stated


def lease_seconds() -> float:
    """Longest a lease may go unreleased before it stops counting.

    It is derived from the proxy's own request budget instead of being a number
    of its own: a live request can never outlast that budget, so a lease older
    than it plus a margin belongs to a process that will not release it.
    """

    raw = os.getenv(_REQUEST_TIMEOUT_ENV, "").strip()
    timeout = _DEFAULT_REQUEST_TIMEOUT_SECONDS
    if raw:
        try:
            parsed = float(raw)
        except ValueError:
            parsed = _DEFAULT_REQUEST_TIMEOUT_SECONDS
        if math.isfinite(parsed) and parsed > 0:
            timeout = parsed
    return max(_MIN_LEASE_SECONDS, timeout + _LEASE_MARGIN_SECONDS)


def file_path() -> Optional[str]:
    """The leases file, which lives beside the deployment cooldowns.

    Beside them, not inside them: a claim writes on every request, and the
    cooldown document also carries protocol fallbacks and capability memories
    that must not be rewritten (or contended for) at request rate.
    """

    configured = os.getenv(_DEPLOYMENT_CONCURRENCY_FILE_ENV, "").strip()
    if configured:
        return configured
    cooldowns = _state_module._deployment_cooldown_file_path()
    if not cooldowns:
        return None
    return os.path.join(os.path.dirname(cooldowns), "deployment-concurrency.json")


def _pid_alive(pid: Any) -> Optional[bool]:
    """Whether a lease's owner still exists; ``None`` when it cannot be told."""

    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _clean_lease(
    state: Any,
    *,
    now: float,
    ttl: float,
) -> Optional[dict[str, Any]]:
    """One lease as it still counts, or ``None`` when it must be dropped."""

    if not isinstance(state, Mapping):
        return None
    deployment = state.get("deployment")
    if not isinstance(deployment, str) or not deployment.strip():
        return None
    try:
        claimed_at = float(state.get("claimed_at") or 0.0)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(claimed_at) or claimed_at <= 0:
        return None
    if now - claimed_at > ttl:
        return None
    if _pid_alive(state.get("pid")) is False:
        return None
    cleaned = dict(state)
    cleaned["deployment"] = deployment.strip()
    cleaned["claimed_at"] = claimed_at
    return cleaned


def _leases_map(payload: dict[str, Any]) -> dict[str, Any]:
    payload.setdefault("schema_version", 1)
    leases = payload.setdefault("leases", {})
    if not isinstance(leases, dict):
        leases = {}
        payload["leases"] = leases
    return leases


def _prune(leases: dict[str, Any], now: float) -> None:
    ttl = lease_seconds()
    for lease_key, state in list(leases.items()):
        cleaned = _clean_lease(state, now=now, ttl=ttl)
        if cleaned is None:
            leases.pop(lease_key, None)
        elif cleaned is not state:
            leases[lease_key] = cleaned


def _update(callback: Any) -> Any:
    """Run one read-modify-write under the file lock every worker shares."""

    path = file_path()
    if not path:
        return None

    def update(payload: dict[str, Any]) -> Any:
        now = time.time()
        leases = _leases_map(payload)
        _prune(leases, now)
        return callback(leases, now), now

    try:
        return _state_module._locked_json_state_update(path, update)
    except OSError:
        # A read-only or vanished state directory is a fact about this host, not
        # about the request: the caller treats ``None`` as "no shared count".
        return None


def counts() -> Optional[dict[str, int]]:
    """In-flight requests per deployment, or ``None`` when there is no store."""

    def read(leases: dict[str, Any], _now: float) -> dict[str, int]:
        counted: dict[str, int] = {}
        for state in leases.values():
            deployment = state.get("deployment")
            if isinstance(deployment, str) and deployment:
                counted[deployment] = counted.get(deployment, 0) + 1
        return counted

    result = _update(read)
    return result[0] if isinstance(result, tuple) and isinstance(result[0], dict) else None


def count_for(deployment_key: str) -> Optional[int]:
    """How many requests one deployment is serving right now."""

    counted = counts()
    if counted is None:
        return None
    return counted.get(deployment_key, 0)


def claim(lease_key: str, deployment_key: str, cap: int) -> bool:
    """Take this turn's slot on *deployment_key*, or report that it is full.

    A turn that already holds the slot keeps it: the claim of a second attempt
    on the same deployment refreshes the lease it has instead of taking another
    one, which is what makes a protocol-fallback retry cost one request.
    """

    if not lease_key or not deployment_key or cap <= 0:
        return True

    def take(leases: dict[str, Any], now: float) -> bool:
        if lease_key in leases:
            leases[lease_key]["claimed_at"] = now
            leases[lease_key]["pid"] = os.getpid()
            return True
        in_flight = sum(
            1
            for state in leases.values()
            if isinstance(state, Mapping) and state.get("deployment") == deployment_key
        )
        if in_flight >= cap:
            return False
        leases[lease_key] = {
            "deployment": deployment_key,
            "claimed_at": now,
            "pid": os.getpid(),
        }
        return True

    result = _update(take)
    if isinstance(result, tuple) and isinstance(result[0], bool):
        return result[0]
    # No shared store means no shared count, and a cap a process cannot make
    # true for the other workers is a number it must not pretend to enforce.
    # ``_with_active_model_concurrency`` reports the same fact once per process
    # as ``model_concurrency_store_unavailable``, so the operator learns the cap
    # is inactive instead of believing a route is capped.
    return True


def release(lease_keys: Iterable[str]) -> int:
    """Give back the slots one turn holds, by their exact keys."""

    keys = [key for key in lease_keys if isinstance(key, str) and key]
    if not keys:
        return 0

    def drop(leases: dict[str, Any], _now: float) -> int:
        dropped = 0
        for key in keys:
            if leases.pop(key, None) is not None:
                dropped += 1
        return dropped

    result = _update(drop)
    return result[0] if isinstance(result, tuple) and isinstance(result[0], int) else 0


def reset_leases() -> int:
    """Drop every lease in the file and report how many were there.

    Called once when the proxy starts: every lease then belongs to a process
    this start replaced, and a fresh worker that inherited one would be serving
    fewer requests than its own cap allows for the rest of the lease window.
    """

    def clear(leases: dict[str, Any], _now: float) -> int:
        dropped = len(leases)
        leases.clear()
        return dropped

    result = _update(clear)
    return result[0] if isinstance(result, tuple) and isinstance(result[0], int) else 0
