"""Python adapter for the bundled ``dsh-connect-qoder`` worker.

The CN gateway and the international one are not reimplemented here.  The
upstream project (https://github.com/dingminhua/dsh-connect-qoder, npm
``@dmh2002/dsh-connect-qoder``) owns the cosy signing protocol, the PAT ->
jobToken exchange, the token refresh, both model catalogs, the usage read, and
its own loopback shim; ``scripts/update_connect_qoder.py`` stages the published
package on every artifact build.  This module owns only what the gateway needs
around it:

* one long-lived Node worker per Core process, started on a loopback port with
  an inbound bearer this process generates, supervised for as long as this
  Core runs, and
* the two environment references the managed LiteLLM child resolves
  (``YOUNG_ROUTER_QODER_BASE`` / ``..._GLOBAL_BASE`` and the matching keys)
  so no port or loopback token ever reaches ``config.yaml``.

The two providers are two REGIONS of one upstream product, not two variants of
one build: the CN gateway keeps its own PAT, catalog and credit, and the
international one keeps its own.  A signed-out region is a normal state, not an
error: the worker reports it and that provider stays disabled until a PAT is
stored.
"""

from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time
from typing import Any, Mapping

from ..node_runtime import node_command as node_runtime_command
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..browser_identity import browser_request_headers
from ..core.persistence import PersistenceError, atomic_write_json, read_json

QODER_PROVIDER = "qoder"
QODER_GLOBAL_PROVIDER = "qoder-global"
QODER_PROVIDERS = (QODER_PROVIDER, QODER_GLOBAL_PROVIDER)

QODER_AUTH_KIND = "qoder_login"
QODER_GLOBAL_AUTH_KIND = "qoder_global_login"
QODER_AUTH_KINDS = (QODER_AUTH_KIND, QODER_GLOBAL_AUTH_KIND)
AUTH_KIND_TO_PROVIDER = {
    QODER_AUTH_KIND: QODER_PROVIDER,
    QODER_GLOBAL_AUTH_KIND: QODER_GLOBAL_PROVIDER,
}
PROVIDER_TO_AUTH_KIND = {value: key for key, value in AUTH_KIND_TO_PROVIDER.items()}
# The upstream catalog states what each model bills at, so a route on either
# region can follow that rate the way a relay group's multiplier is followed.
# The same catalog also states each model's context window, so a route on this
# account has an upstream-published window the pane reports read-only.
CATALOG_AUTH_KINDS = frozenset(QODER_AUTH_KINDS)
CATALOG_AUTH_KIND_TO_PROVIDER = dict(AUTH_KIND_TO_PROVIDER)
RATE_AUTH_KINDS = CATALOG_AUTH_KINDS
RATE_AUTH_KIND_TO_PROVIDER = CATALOG_AUTH_KIND_TO_PROVIDER

QODER_DISPLAY_NAMES = {
    QODER_PROVIDER: "Qoder",
    QODER_GLOBAL_PROVIDER: "Qoder Global",
}

# The product-neutral spelling of the same facts (see ``workbuddy.AUTH_KINDS``).
AUTH_KINDS = QODER_AUTH_KINDS
DISPLAY_NAMES = QODER_DISPLAY_NAMES
PROVIDERS = QODER_PROVIDERS
# The operation name this integration's catalog read answers under.
MODELS_OPERATION = "qoder_models"
# Variable names the managed proxy resolves for each region.  They are
# process-local names, never credentials: the token itself lives only in this
# Core process and in the worker's command line.
API_BASE_ENV = {
    QODER_PROVIDER: "YOUNG_ROUTER_QODER_BASE",
    QODER_GLOBAL_PROVIDER: "YOUNG_ROUTER_QODER_GLOBAL_BASE",
}
API_KEY_ENV = {
    QODER_PROVIDER: "YOUNG_ROUTER_QODER_KEY",
    QODER_GLOBAL_PROVIDER: "YOUNG_ROUTER_QODER_GLOBAL_KEY",
}
# Provider key slot name shown in the provider editor.  The slot exists only
# because LiteLLM requires a non-empty key for an OpenAI-compatible route; its
# value is the loopback bearer above.
API_KEY_NAME = "qoder-local"

_QODER_DIR_ENV = "YOUNG_ROUTER_QODER_DIR"
_QODER_NODE_ENV = "YOUNG_ROUTER_QODER_NODE"
_QODER_WORKER_ENV = "YOUNG_ROUTER_QODER_WORKER"
_QODER_START_TIMEOUT_SECONDS = 45.0
# A refreshing status read re-reads both regions' catalogs and credits before
# it answers, and the gateway is a network round trip per call.  The cached
# (non-refresh) read is the common case and answers in a worker round trip;
# this ceiling is for the explicit 刷新 the user waits on.
_QODER_STATUS_TIMEOUT_SECONDS = 60.0
_QODER_MODELS_TIMEOUT_SECONDS = 90.0
# Two reads answer through this client, and both are asked for more often than
# their upstream changes.  A signed-in state moves when the user stores or
# forgets a PAT; a catalog churns about daily.  Caching them keeps one
# opened model pane from paying a worker round trip (and, for the credit, a
# jobToken exchange) per model switch, while an explicit refresh still
# reaches upstream.  The TTLs differ because the cost and the churn differ:
# sign-in state is cheap to re-read and can change under the user, while the
# catalog is what the pane's 倍率 column shows and does not.
_QODER_STATUS_TTL_SECONDS = 20.0
_QODER_MODELS_TTL_SECONDS = 120.0
# A worker that served for at least this long died a casualty, not a defect:
# it is replaced on the spot.  One that dies younger is restarted on the next
# delay step instead, and one that keeps dying young is left to the demand
# path (a pane read, the next proxy launch) after the steps run out.
_QODER_SETTLED_SECONDS = 60.0
# A remembered port whose predecessor is still closing: retried briefly before
# the worker is started on a fresh port, because a running proxy resolved the
# remembered address and cannot learn another one.
_QODER_PORT_RETRIES = 6
_QODER_PORT_RETRY_SECONDS = 0.25
_QODER_RESTART_DELAYS_SECONDS = (0.0, 0.5, 2.0, 5.0, 15.0)


def _stated_multiplier(value: object) -> float | None:
    """One catalog's own already-parsed rate, as a number.

    ``dsh-connect-qoder`` states the rate per catalog row as
    ``price_factor``; this worker forwards it as ``rate`` so every managed
    catalog speaks one name.  Both spellings are accepted so an older worker
    keeps answering for a build that lags this file.

    ``None`` and ``0`` stay apart on purpose: a model the upstream priced at
    nothing is a rate a route may follow, while a row that states no rate at
    all is not.  Collapsing the second into ``0.0`` would let a route follow a
    price nobody published.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def model_rate(entry: object) -> float | None:
    """The rate one catalog entry bills at, or ``None`` when it states none.

    A catalog entry is the worker's own document shape, so interpreting it
    belongs here rather than in each caller.  The two shapes an entry has
    carried are read in the order they were introduced: this adapter's own
    normalized ``rate`` and the library's ``priceFactor`` spelling.
    """

    if not isinstance(entry, Mapping):
        return None
    for key in ("rate", "priceFactor"):
        parsed = _stated_multiplier(entry.get(key))
        if parsed is not None:
            return parsed
    return None


class QoderUnavailable(RuntimeError):
    """The staged integration is missing or its worker cannot serve."""


def staged_root() -> Path:
    """Directory holding the staged upstream package and its dependencies."""

    configured = os.environ.get(_QODER_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).with_name("connect-qoder")


def worker_path() -> Path:
    configured = os.environ.get(_QODER_WORKER_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).with_name("qoder_worker.mjs")


def node_command() -> str:
    return node_runtime_command(
        env_name=_QODER_NODE_ENV,
        label="Qoder",
        unavailable=QoderUnavailable,
    )


# The loopback base URLs and the bearer this Core owns. They are published to
# the process environment for the proxy child, and kept here as well so a
# Core-side read (a probe, a catalog fetch) resolves exactly what the child
# was launched with.
_PUBLISHED_ENVIRONMENT: dict[str, str] = {}


def published_environment() -> dict[str, str]:
    """The environment fragment this deployment owns."""

    return dict(_PUBLISHED_ENVIRONMENT)


def _publish(values: Mapping[str, str]) -> None:
    if not values:
        return
    _PUBLISHED_ENVIRONMENT.update(values)
    os.environ.update(values)


def available() -> bool:
    """Whether the staged package and its entry point are present."""

    root = staged_root()
    return (root / "package.json").is_file() and (root / "lib" / "index.js").is_file()


def api_base_reference(provider: str) -> str:
    """The provider base URL reference stored in ``config.yaml``."""

    return f"os.environ/{API_BASE_ENV[validated_provider(provider)]}"


def api_key_reference(provider: str) -> str:
    """The provider key reference stored in ``config.yaml``."""

    return f"os.environ/{API_KEY_ENV[validated_provider(provider)]}"


def validated_provider(value: object) -> str:
    provider = str(value or "").strip().lower()
    if provider not in QODER_PROVIDERS:
        raise ValueError("Unknown Qoder provider")
    return provider


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class QoderRuntime:
    """Own the Qoder worker process and answer its control requests."""

    def __init__(self, runtime_root: Path | str | None = None) -> None:
        base = Path(runtime_root).expanduser() if runtime_root else Path.home() / ".young-router"
        self.root = base / ".litellm-runtime" / "qoder"
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        # Set while this runtime wants no worker at all: a released runtime
        # must not have one resurrected by the supervision of the worker it
        # just stopped.
        self._released = threading.Event()
        self._restart_step = 0
        self._started_at = 0.0
        self._port = 0
        self._token = ""
        self._environment: dict[str, str] = {}
        self._status_cache: tuple[float, dict[str, Any]] | None = None
        # One catalog per region, stamped like the status cache above: a model
        # pane reads this once per model switch, and the worker's own read is a
        # full credential resolve plus an upstream fetch.
        self._models_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._log_path = self.root / "worker.log"
        # The loopback port and its bearer outlive this Core: a replacement
        # Core resumes the same listener, so a proxy that is still running
        # keeps resolving the base URL and key it was launched with.
        self._state_path = self.root / "worker.json"
        stored = read_json(self._state_path, default={})
        if isinstance(stored, Mapping):
            port = stored.get("port")
            token = stored.get("token")
            if type(port) is int and 0 < port <= 65535:
                self._port = port
            if isinstance(token, str) and token.strip():
                self._token = token.strip()
        if not self._token:
            self._token = secrets.token_urlsafe(32)
        # A replacement Core resolves the same loopback base URL and bearer the
        # running proxy was launched with, so the fragment is rebuilt from the
        # remembered address before anything asks for it.
        if self._port and available():
            self._environment = self._proxy_environment()
            self._publish_environ_locked()

    # -- worker lifecycle -------------------------------------------------

    def environment(self, *, autostart: bool = False) -> dict[str, str]:
        """Return the proxy environment fragment for the running worker.

        ``autostart`` starts the worker when it is not running: the managed
        proxy needs those variables before it launches, while a snapshot read
        must never spawn a process on its own.
        """

        if autostart:
            self.ensure_started()
        with self._lock:
            self._publish_environ_locked()
            return dict(self._environment)

    def ensure_started(self) -> bool:
        """Start the worker when needed; return whether it is serving."""

        if not available():
            return False
        with self._lock:
            if self._healthy():
                return True
            # A worker asked for by name - a pane read, a probe, the next
            # proxy launch - gets the full restart budget again: the steps
            # below bound what supervision does on its own, never what the
            # user's own read asks for.
            self._restart_step = 0
            started = self._start()
            if started:
                # Core-side probes and model-list reads resolve the same
                # ``os.environ/`` references the proxy child does, so the
                # fragment has to be visible to this process as well.
                self.configure_environ()
            return started

    def _healthy(self) -> bool:
        if self._process is None or self._process.poll() is not None:
            return False
        return self._port > 0

    def _start(self) -> bool:
        # The remembered address survives the teardown below: it is what a
        # replacement Core or a restarted worker has to reclaim.
        remembered = self._port
        self._stop_locked()
        # A worker is wanted again: the release the teardown just signalled
        # must not end this generation's supervision.
        self._released.clear()
        if not available():
            return False
        node = node_command()
        worker = worker_path()
        if not worker.is_file():
            raise QoderUnavailable(f"Qoder worker is missing: {worker}")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The remembered port is a hint, not a promise: another process may
        # hold it, in which case the worker is restarted on a fresh one and the
        # new address is persisted for the next Core.  The retry matters: the
        # usual holder is this app's previous worker, still closing, and a
        # proxy that is already running resolved the remembered address — a
        # worker that silently moves elsewhere strands every Qoder route until
        # the next restart.
        ready = self._spawn_retrying(node, worker, remembered) if remembered else None
        if ready is None:
            ready = self._spawn(node, worker, 0)
        if ready is None:
            # A start that produced no worker leaves the address a running
            # proxy resolved exactly where it was.  Forgetting it would send
            # the next attempt to a port nobody was told about - a live worker
            # behind a proxy still calling a dead one, which is worse than the
            # failure it replaced.
            self._port = remembered
            self._environment = self._proxy_environment() if remembered else {}
            self._publish_environ_locked()
            return False
        self._port = int(ready.get("port") or 0)
        self._environment = self._proxy_environment()
        # A freshly started worker has its own credential read ahead of it: what
        # the previous worker answered describes a process that is gone.
        self._status_cache = None
        self._models_cache = {}
        self._started_at = time.monotonic()
        self._persist_state()
        # The fragment this process hands out is the address the worker it just
        # started serves, so Core-side reads resolve what the proxy child does.
        self._publish_environ_locked()
        self._watch_locked(self._process)
        return self._port > 0

    def _watch_locked(self, process: subprocess.Popen[bytes] | None) -> None:
        """Supervise one worker for as long as this runtime owns it."""

        if process is None:
            return
        threading.Thread(
            target=self._supervise,
            args=(process,),
            name="young-router-qoder-supervisor",
            daemon=True,
        ).start()

    def _supervise(self, process: subprocess.Popen[bytes] | None) -> None:
        """Replace a worker that exited without this runtime asking it to.

        A worker the upstream sidecar decides to end takes every Qoder
        route with it: the running proxy resolved its loopback base URL from
        its own environment when it launched and cannot learn another one, and
        nothing on the request path ever asks Core for a worker - the table
        projects the last observed account instead.  Every such route then
        reads as a temporary upstream failure for as long as the app runs.

        One thread per worker blocks on the process itself, so an idle runtime
        costs nothing and a death is repaired on the remembered port the proxy
        is already calling.  The delay steps bound a worker that keeps dying
        young - a broken integration, not a casualty - and a start that
        produced no worker at all is its own kind of young death, so one
        transient failure never ends the supervision that owes the proxy a
        listener.
        """

        while True:
            if process is not None:
                process.wait()
                with self._lock:
                    if self._process is not process:
                        # This runtime stopped or replaced the worker itself;
                        # its own teardown decided what happens next.
                        return
                    self._process = None
                    # The exit was not this runtime's doing, but the pipe it
                    # read the readiness line through is still open.
                    self._stop_process(process)
                    settled = time.monotonic() - self._started_at >= _QODER_SETTLED_SECONDS
            else:
                settled = False
            with self._lock:
                if self._released.is_set():
                    return
                if settled:
                    # A worker that served for a while is a casualty: replace
                    # it at once and forget any earlier young death.
                    self._restart_step = 0
                else:
                    self._restart_step = min(
                        self._restart_step + 1, len(_QODER_RESTART_DELAYS_SECONDS)
                    )
                step = self._restart_step
            if step >= len(_QODER_RESTART_DELAYS_SECONDS):
                return
            delay = _QODER_RESTART_DELAYS_SECONDS[step]
            if delay and self._released.wait(delay):
                return
            with self._lock:
                if self._released.is_set() or self._process is not None:
                    return
                try:
                    started = self._start() and self._process is not None
                except Exception:
                    started = False
                process = self._process if started else None

    def _spawn_retrying(self, node: str, worker: Path, port: int) -> dict[str, Any] | None:
        """Take the remembered port, giving a predecessor a moment to let go."""

        for attempt in range(_QODER_PORT_RETRIES):
            ready = self._spawn(node, worker, port)
            if ready is not None:
                return ready
            if attempt + 1 < _QODER_PORT_RETRIES:
                time.sleep(_QODER_PORT_RETRY_SECONDS)
        return None

    def _spawn(self, node: str, worker: Path, port: int) -> dict[str, Any] | None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        log = self._log_path.open("ab")
        command = [
            node,
            str(worker),
            "--port",
            str(port),
            "--token",
            self._token,
            "--root",
            str(self.root),
            "--entry",
            str(staged_root() / "lib" / "index.js"),
        ]
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.root),
                env={**os.environ, "YOUNG_ROUTER_QODER_ROOT": str(self.root)},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=log,
                # The worker stays in Core's process group: a Core that is
                # replaced or killed takes its credential-holding worker
                # with it instead of leaving an orphan on the loopback port.
                start_new_session=False,
                close_fds=True,
            )
        except OSError as exc:
            log.close()
            raise QoderUnavailable(f"Qoder worker could not start: {exc}") from exc
        ready = self._read_ready(process)
        log.close()
        if ready is None:
            self._stop_process(process)
            return None
        self._process = process
        return ready

    def _persist_state(self) -> None:
        """Remember the loopback address for a replacement Core."""

        try:
            atomic_write_json(self._state_path, {"port": self._port, "token": self._token})
        except PersistenceError as exc:
            raise QoderUnavailable("The Qoder worker state could not be stored") from exc

    def _read_ready(self, process: subprocess.Popen[bytes]) -> dict[str, Any] | None:
        """Read the worker's one-line readiness record without blocking Core."""

        if process.stdout is None:
            return None
        deadline = time.monotonic() + _QODER_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if line:
                try:
                    payload = json.loads(line.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    return None
                return payload if payload.get("ready") is True else None
            if process.poll() is not None:
                return None
        return None

    def _proxy_environment(self) -> dict[str, str]:
        base = f"http://127.0.0.1:{self._port}"
        environment: dict[str, str] = {}
        for provider in QODER_PROVIDERS:
            environment[API_BASE_ENV[provider]] = f"{base}/{provider}/v1"
            environment[API_KEY_ENV[provider]] = self._token
        return environment

    def configure_environ(self) -> dict[str, str]:
        """Publish the fragment process-locally for Core-side resolution."""

        environment = self.environment()
        _publish(environment)
        return environment

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()
            # A released runtime starts over: the worker started after this one
            # gets the full restart budget again.
            self._restart_step = 0

    def _stop_locked(self) -> None:
        # This runtime wants no worker until the next ``_start``: the release is
        # what stops the supervision of the worker being stopped here from
        # replacing it, and what ends a restart that is still waiting out its
        # delay step.
        self._released.set()
        process, self._process = self._process, None
        self._port = 0
        self._environment = {}
        self._status_cache = None
        self._models_cache = {}
        for name in (API_BASE_ENV, API_KEY_ENV):
            for variable in name.values():
                os.environ.pop(variable, None)
        if process is None:
            return
        self._stop_process(process)

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes]) -> None:
        try:
            if process.poll() is None:
                # The worker shares Core's process group, so signals address
                # its pid: a group signal would reach this process and
                # everything beside it.
                try:
                    process.terminate()
                except OSError:
                    return
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and process.poll() is None:
                    time.sleep(0.05)
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass
        finally:
            # Reading the readiness line opened this pipe; a replaced worker
            # would otherwise leave one descriptor per generation behind.
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except OSError:
                    pass

    # -- control surface --------------------------------------------------

    def _request(
        self,
        path: str,
        *,
        timeout: float,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.ensure_started():
            raise QoderUnavailable("The Qoder integration is unavailable")
        url = f"http://127.0.0.1:{self._port}{path}"
        body = None if payload is None else json.dumps(dict(payload)).encode("utf-8")
        # Loopback traffic owned by this process: it must not be reformatted by
        # an ambient proxy, and it carries the shared browser identity rather
        # than a self-naming client fingerprint.
        headers = {**browser_request_headers(), "Authorization": f"Bearer {self._token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method="GET" if body is None else "POST")
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed loopback URL
                payload = response.read()
        except HTTPError as exc:
            # A refused sign-in is the one failure whose own words the user
            # needs: "personal token is invalid" is actionable, while "the
            # worker did not answer" sends them looking for a network fault.
            # The worker's error body carries no credential (it masks the PAT),
            # so its message travels as the refusal reason.
            detail = ""
            try:
                document = json.loads(exc.read().decode("utf-8"))
                error = document.get("error") if isinstance(document, dict) else None
                detail = str(error.get("message", "")).strip() if isinstance(error, dict) else ""
            except Exception:
                detail = ""
            raise QoderUnavailable(
                detail or "The Qoder worker refused the request"
            ) from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise QoderUnavailable("The Qoder worker did not answer") from exc
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QoderUnavailable("The Qoder worker returned an invalid document") from exc
        if not isinstance(document, dict):
            raise QoderUnavailable("The Qoder worker returned an invalid document")
        return document

    def status(self, *, refresh: bool = False) -> dict[str, Any]:
        """Report both accounts' sign-in state and catalog provenance."""

        now = time.monotonic()
        if not refresh and self._status_cache is not None:
            stamped, cached = self._status_cache
            if now - stamped < _QODER_STATUS_TTL_SECONDS:
                return cached
        if not available():
            return {"available": False, "detail": "integration_unstaged", "providers": {}}
        if not self.ensure_started():
            return {"available": False, "detail": "worker_unavailable", "providers": {}}
        document = self._request(
            "/control/status?refresh=1" if refresh else "/control/status",
            timeout=_QODER_STATUS_TIMEOUT_SECONDS,
        )
        providers = document.get("providers")
        result = {
            "available": True,
            "providers": dict(providers) if isinstance(providers, dict) else {},
        }
        self._status_cache = (now, result)
        if refresh:
            # A refreshing status read also re-reads both catalogs upstream (the
            # worker refreshes every region before it answers), so an answer
            # remembered from before this call describes a catalog the worker
            # has already replaced.  That is what makes the account's own 刷新
            # button refresh the 倍率 column beside it.
            self._models_cache.clear()
        return result

    def models(self, provider: str, *, refresh: bool = False) -> dict[str, Any]:
        """Return one region's catalog, with the worker's own provenance.

        The catalog answers the 倍率 column of every route on this account, so
        it is read far more often than it changes.  A fresh remembered answer
        is returned as-is; an explicit ``refresh`` (the pane's 刷新 button, the
        account import) always reaches upstream, and so does any read whose
        remembered answer is stale or was a failure — a signed-out region must
        not stay signed-out in the cache once the user signs in.
        """

        name = validated_provider(provider)
        now = time.monotonic()
        if not refresh:
            remembered = self._models_cache.get(name)
            if remembered is not None and now - remembered[0] < _QODER_MODELS_TTL_SECONDS:
                return copy.deepcopy(remembered[1])
        if not available():
            return {"available": False, "detail": "integration_unstaged", "models": []}
        suffix = "&refresh=1" if refresh else ""
        document = self._request(
            f"/control/models?provider={name}{suffix}",
            timeout=_QODER_MODELS_TIMEOUT_SECONDS,
        )
        # A catalog read answers the account question too, so the status
        # document it carries replaces the one the status cache holds.
        self._status_cache = None
        if document.get("available") is not True:
            self._models_cache.pop(name, None)
            return {
                "available": False,
                "detail": str(document.get("status", {}).get("reason") or document.get("detail") or "signed_out")
                if isinstance(document.get("status"), dict)
                else str(document.get("detail") or "signed_out"),
                "models": [],
                "status": document.get("status") if isinstance(document.get("status"), dict) else {},
            }
        models = document.get("models")
        entries = [item for item in models if isinstance(item, dict)] if isinstance(models, list) else []
        for entry in entries:
            # The rate a route may follow is stated once, here, beside the
            # catalog entry that owns it, so every reader (the pane's 倍率
            # column and the order mode that follows it) agrees on the number.
            # ``model_rate`` accepts both the worker's ``rate`` and the
            # library's ``priceFactor`` spelling.
            rate = model_rate(entry)
            if rate is not None:
                entry["rate"] = rate
        result = {
            "available": True,
            "source": str(document.get("source") or ""),
            "fetched_at_ms": document.get("fetchedAtMs"),
            "models": entries,
            "status": document.get("status") if isinstance(document.get("status"), dict) else {},
        }
        # A read that ran before the worker had a credential in hand comes back
        # available but empty.  Remembering that would blank every 倍率 for the
        # whole interval, so only a catalog that actually named a model is kept.
        if result["models"]:
            self._models_cache[name] = (now, copy.deepcopy(result))
        else:
            self._models_cache.pop(name, None)
        return result

    def provider_status(self, provider: str) -> dict[str, Any]:
        document = self.status()
        providers = document.get("providers")
        if isinstance(providers, dict):
            entry = providers.get(validated_provider(provider))
            if isinstance(entry, dict):
                return entry
        return {}

    def environment_values(self) -> dict[str, str]:
        """Current environment fragment without starting the worker."""

        with self._lock:
            self._publish_environ_locked()
            return dict(self._environment)

    def _publish_environ_locked(self) -> None:
        """Expose the fragment to this process too, when it exists."""

        _publish(self._environment)

    def login(self, provider: str, pat: object) -> dict[str, Any]:
        """Store and verify one region's PAT, then answer its account state.

        The PAT is a Qoder credential, not a Young Router secret: it travels to
        the worker over loopback and is written there to a 0600 file this Core
        owns, so it never enters ``config.yaml`` and never round-trips through
        the provider key editor.  Verification happens before the write, so a
        wrong paste is reported as a refusal instead of a sign-in that later
        fails every turn.
        """

        provider_id = validated_provider(provider)
        value = pat if isinstance(pat, str) else ""
        document = self._request(
            "/control/login",
            timeout=_QODER_MODELS_TIMEOUT_SECONDS,
            payload={"provider": provider_id, "pat": value},
        )
        self._status_cache = None
        self._models_cache.pop(provider_id, None)
        status = document.get("status")
        return {
            "provider": provider_id,
            "available": document.get("available") is not False,
            "status": status if isinstance(status, dict) else {},
        }

    def logout(self, provider: str) -> dict[str, Any]:
        """Forget one region's pasted PAT, and say who holds the account after.

        The desktop app's sign-in is not this app's to end: ``app_session``
        reports whether that app is still signed in, so a pane can state the
        outcome it actually produced instead of claiming a sign-out that did
        not happen.
        """

        provider_id = validated_provider(provider)
        document = self._request(
            "/control/logout",
            timeout=_QODER_STATUS_TIMEOUT_SECONDS,
            payload={"provider": provider_id},
        )
        self._status_cache = None
        self._models_cache.pop(provider_id, None)
        status = document.get("status")
        return {
            "provider": provider_id,
            "removed": document.get("removed") is True,
            "app_session": document.get("appSession") is True,
            "status": status if isinstance(status, dict) else {},
        }

    def desktop_app(self, provider: str) -> dict[str, Any]:
        """The app that owns this region's sign-in, when it is installed."""

        provider_id = validated_provider(provider)
        document = self._request(f"/control/app?provider={provider_id}", timeout=10.0)
        return {
            "provider": provider_id,
            "app_name": str(document.get("appName") or "").strip(),
            "app_path": str(document.get("bundle") or "").strip(),
            "installed": document.get("installed") is True,
        }

    def open_desktop_app(self, provider: str) -> dict[str, Any]:
        """Open the app whose sign-in this region borrows.

        The sign-in itself happens in that app; this one can only hand the user
        over and then re-read what the app wrote.
        """

        document = self.desktop_app(provider)
        result = {**document, "opened": False}
        if not document["installed"]:
            return result
        try:
            if os.name == "nt":
                os.startfile(document["app_path"] or document["app_name"])  # type: ignore[attr-defined]
            elif document["app_path"]:
                subprocess.run(["open", document["app_path"]], check=True, timeout=20, capture_output=True)
            else:
                subprocess.run(["open", "-a", document["app_name"]], check=True, timeout=20, capture_output=True)
            result["opened"] = True
        except Exception:
            result["opened"] = False
        return result


def unsupported_status() -> dict[str, Any]:
    """Status document used when the integration is not staged."""

    return {"available": False, "detail": "integration_unstaged", "providers": {}}


def script_available() -> bool:
    return available() and worker_path().is_file()


def managed_runtime(runtime_root: Path | str | None = None) -> QoderRuntime:
    """The runtime a Core-less host starts for a config naming this worker.

    ``young_router.adapters.managed`` starts a worker through the factory its
    adapter publishes, so the contract that builds one stays in the file that
    owns it rather than in the deployment that happens to run it.
    """

    return QoderRuntime(runtime_root)
