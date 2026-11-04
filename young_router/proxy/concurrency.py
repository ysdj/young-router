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

#: How often a queued turn looks again.  The count lives in a locked file, so
#: this is a read-modify-write; a quarter second is long enough that sixteen
#: workers waiting do not become the load, and short enough that a freed slot
#: is taken while the caller still reads it as "thinking".  A cadence, not a
#: setting: nothing an operator would tune, and the queue's own bound is the
#: recovery window that is already configured.
MODEL_CONCURRENCY_WAIT_INTERVAL_SECONDS = 0.25

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


def _waiters_map(payload: dict[str, Any]) -> dict[str, Any]:
    """The turns waiting for a slot, in the same document as the slots.

    A queue and the count it queues against are one fact: a waiter that cannot
    see the leases cannot tell when its turn came, and a claim that cannot see
    the waiters is what let a new request jump a line that was already waiting.
    """

    waiters = payload.setdefault("waiters", {})
    if not isinstance(waiters, dict):
        waiters = {}
        payload["waiters"] = waiters
    return waiters


def _prune(leases: dict[str, Any], now: float) -> None:
    ttl = lease_seconds()
    for lease_key, state in list(leases.items()):
        cleaned = _clean_lease(state, now=now, ttl=ttl)
        if cleaned is None:
            leases.pop(lease_key, None)
        elif cleaned is not state:
            leases[lease_key] = cleaned


def _prune_waiters(waiters: dict[str, Any], now: float) -> None:
    """Drop tickets nobody is holding any more.

    Same two rules as a lease: a ticket belongs to a live process and to a turn
    that is still waiting.  A ticket left behind by a killed worker or an
    abandoned turn would hold the head of the line forever, which is worse than
    the refusal this queue replaced -- the route would look busy to every later
    request while serving nobody.
    """

    ttl = lease_seconds()
    for wait_key, state in list(waiters.items()):
        if not isinstance(state, Mapping):
            waiters.pop(wait_key, None)
            continue
        deployment = state.get("deployment")
        try:
            requested_at = float(state.get("requested_at") or 0.0)
        except (TypeError, ValueError):
            waiters.pop(wait_key, None)
            continue
        if (
            not isinstance(deployment, str)
            or not deployment.strip()
            or not math.isfinite(requested_at)
            or requested_at <= 0
            or now - requested_at > ttl
            or _pid_alive(state.get("pid")) is False
        ):
            waiters.pop(wait_key, None)


def _update(callback: Any) -> Any:
    """Run one read-modify-write under the file lock every worker shares."""

    path = file_path()
    if not path:
        return None

    def update(payload: dict[str, Any]) -> Any:
        now = time.time()
        leases = _leases_map(payload)
        waiters = _waiters_map(payload)
        _prune(leases, now)
        _prune_waiters(waiters, now)
        return callback(leases, waiters, now), now

    try:
        return _state_module._locked_json_state_update(path, update)
    except OSError:
        # A read-only or vanished state directory is a fact about this host, not
        # about the request: the caller treats ``None`` as "no shared count".
        return None


def counts() -> Optional[dict[str, int]]:
    """In-flight requests per deployment, or ``None`` when there is no store."""

    def read(
        leases: dict[str, Any],
        _waiters: dict[str, Any],
        _now: float,
    ) -> dict[str, int]:
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


def claim(
    lease_key: str,
    deployment_key: str,
    cap: int,
    *,
    requested_at: Optional[float] = None,
    turn_key: Optional[str] = None,
) -> bool:
    """Take this turn's slot on *deployment_key*, or report that it is full.

    A turn that already holds the slot keeps it: the claim of a second attempt
    on the same deployment refreshes the lease it has instead of taking another
    one, which is what makes a protocol-fallback retry cost one request.

    ``requested_at`` is when this turn first found the pool full (see
    :func:`queue`).  A slot that frees goes to the turn that has been waiting
    longest, not to whoever asks next: without that, a route at its cap is a
    queue only by accident, and a stream of fresh requests can hold a waiting
    turn out for as long as it keeps arriving.
    """

    if not lease_key or not deployment_key or cap <= 0:
        return True
    # The queue is per turn (one ticket names the route it waits on), while a
    # lease is per turn *and* deployment.  Taking a slot withdraws the turn's
    # one place, whatever route it was queued for.
    ticket_key = turn_key or lease_key

    def take(
        leases: dict[str, Any],
        waiters: dict[str, Any],
        now: float,
    ) -> bool:
        if lease_key in leases:
            leases[lease_key]["claimed_at"] = now
            leases[lease_key]["pid"] = os.getpid()
            waiters.pop(ticket_key, None)
            return True
        in_flight = sum(
            1
            for state in leases.values()
            if isinstance(state, Mapping) and state.get("deployment") == deployment_key
        )
        if in_flight >= cap:
            return False
        mine = (
            requested_at
            if isinstance(requested_at, (int, float)) and math.isfinite(requested_at)
            else float("inf")
        )
        for wait_key, state in waiters.items():
            if wait_key == ticket_key or not isinstance(state, Mapping):
                continue
            if state.get("deployment") != deployment_key:
                continue
            try:
                theirs = float(state.get("requested_at") or 0.0)
            except (TypeError, ValueError):
                continue
            # Strictly older wins; an unreadable ticket must not block a line.
            if math.isfinite(theirs) and theirs < mine:
                return False
        leases[lease_key] = {
            "deployment": deployment_key,
            "claimed_at": now,
            "pid": os.getpid(),
        }
        waiters.pop(ticket_key, None)
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


def queue(lease_key: str, deployment_key: str, requested_at: float) -> bool:
    """Join the line for *deployment_key*, keeping the place already held.

    One ticket per turn, naming the route it is waiting on.  Re-entering (a
    recovery round asking again) refreshes the same ticket rather than taking a
    new place, which is what makes the wait survive between attempts.
    """

    if not lease_key or not deployment_key:
        return False
    if not isinstance(requested_at, (int, float)) or not math.isfinite(requested_at):
        return False

    def join(
        leases: dict[str, Any],
        waiters: dict[str, Any],
        now: float,
    ) -> bool:
        if lease_key in leases:
            return True
        state = waiters.get(lease_key)
        if isinstance(state, dict) and state.get("deployment") == deployment_key:
            state["pid"] = os.getpid()
            state["requested_at"] = float(requested_at)
            return True
        waiters[lease_key] = {
            "deployment": deployment_key,
            "requested_at": float(requested_at),
            "pid": os.getpid(),
            "queued_at": now,
        }
        return True

    result = _update(join)
    return bool(isinstance(result, tuple) and result[0] is True)


def release(
    lease_keys: Iterable[str],
    *,
    ticket_keys: Iterable[str] = (),
) -> int:
    """Give back the slots one turn holds, by their exact keys.

    ``ticket_keys`` drops this turn's places in line as well.  A turn that ends
    while still queued holds no lease, so releasing only leases would leave its
    ticket at the head of the line and refuse every later request that route.
    """

    keys = [key for key in lease_keys if isinstance(key, str) and key]
    tickets = [key for key in ticket_keys if isinstance(key, str) and key]
    if not keys and not tickets:
        return 0

    def drop(
        leases: dict[str, Any],
        waiters: dict[str, Any],
        _now: float,
    ) -> int:
        dropped = 0
        for key in keys:
            if leases.pop(key, None) is not None:
                dropped += 1
        for key in tickets:
            waiters.pop(key, None)
        return dropped

    result = _update(drop)
    return result[0] if isinstance(result, tuple) and isinstance(result[0], int) else 0


def waiting_count(deployment_key: str) -> Optional[int]:
    """How many turns are queued for one route right now."""

    def read(
        _leases: dict[str, Any],
        waiters: dict[str, Any],
        _now: float,
    ) -> int:
        return sum(
            1
            for state in waiters.values()
            if isinstance(state, Mapping) and state.get("deployment") == deployment_key
        )

    result = _update(read)
    return result[0] if isinstance(result, tuple) and isinstance(result[0], int) else None


def reset_leases() -> int:
    """Drop every lease in the file and report how many were there.

    Called once when the proxy starts: every lease then belongs to a process
    this start replaced, and a fresh worker that inherited one would be serving
    fewer requests than its own cap allows for the rest of the lease window.
    The queue goes with them, for the same reason: every ticket then belongs to
    a turn this start ended.
    """

    def clear(
        leases: dict[str, Any],
        waiters: dict[str, Any],
        _now: float,
    ) -> int:
        dropped = len(leases)
        leases.clear()
        waiters.clear()
        return dropped

    result = _update(clear)
    return result[0] if isinstance(result, tuple) and isinstance(result[0], int) else 0
