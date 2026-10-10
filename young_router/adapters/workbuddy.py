"""Python adapter for the bundled ``dsh-workbuddy-connect`` worker.

WorkBuddy (CN) and WorkBuddy AI (international) are not reimplemented here.
The upstream project (https://github.com/corrinehu/dsh-workbuddy-connect, npm
``dsh-workbuddy-connect``) owns the desktop app credential format, the token
refresh, both model catalogs, and the loopback shim that speaks the upstream's
Chat Completions wire format; ``scripts/update_workbuddy_connect.py`` stages
the published package on every artifact build.  This module owns only what the
gateway needs around it:

* one long-lived Node worker per Core process, started on a loopback port with
  an inbound bearer this process generates, supervised for as long as this
  Core runs, and
* the two environment references the managed LiteLLM child resolves
  (``YOUNG_ROUTER_WORKBUDDY_BASE`` / ``..._AI_BASE`` and the matching keys) so
  no port or token ever reaches ``config.yaml``.

A signed-out desktop app is a normal state, not an error: the worker reports
it and the provider stays disabled until the user signs in.
"""

from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import re
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

WORKBUDDY_PROVIDER = "workbuddy"
WORKBUDDY_AI_PROVIDER = "workbuddy-ai"
WORKBUDDY_PROVIDERS = (WORKBUDDY_PROVIDER, WORKBUDDY_AI_PROVIDER)

WORKBUDDY_AUTH_KIND = "workbuddy_login"
WORKBUDDY_AI_AUTH_KIND = "workbuddy_ai_login"
WORKBUDDY_AUTH_KINDS = (WORKBUDDY_AUTH_KIND, WORKBUDDY_AI_AUTH_KIND)
AUTH_KIND_TO_PROVIDER = {
    WORKBUDDY_AUTH_KIND: WORKBUDDY_PROVIDER,
    WORKBUDDY_AI_AUTH_KIND: WORKBUDDY_AI_PROVIDER,
}
PROVIDER_TO_AUTH_KIND = {value: key for key, value in AUTH_KIND_TO_PROVIDER.items()}
# The desktop catalog states what each model bills at, so a route on either
# variant can follow that rate the way a relay group's multiplier is followed.
RATE_AUTH_KINDS = frozenset(WORKBUDDY_AUTH_KINDS)
RATE_AUTH_KIND_TO_PROVIDER = dict(AUTH_KIND_TO_PROVIDER)

WORKBUDDY_DISPLAY_NAMES = {
    WORKBUDDY_PROVIDER: "WorkBuddy",
    WORKBUDDY_AI_PROVIDER: "WorkBuddy AI",
}
# Variable names the managed proxy resolves for each variant.  They are
# process-local names, never credentials: the token itself lives only in this
# Core process and in the worker's command line.
API_BASE_ENV = {
    WORKBUDDY_PROVIDER: "YOUNG_ROUTER_WORKBUDDY_BASE",
    WORKBUDDY_AI_PROVIDER: "YOUNG_ROUTER_WORKBUDDY_AI_BASE",
}
API_KEY_ENV = {
    WORKBUDDY_PROVIDER: "YOUNG_ROUTER_WORKBUDDY_KEY",
    WORKBUDDY_AI_PROVIDER: "YOUNG_ROUTER_WORKBUDDY_AI_KEY",
}
# Provider key slot name shown in the provider editor.  The slot exists only
# because LiteLLM requires a non-empty key for an OpenAI-compatible route; its
# value is the loopback bearer above.
API_KEY_NAME = "workbuddy-local"

_WORKBUDDY_DIR_ENV = "YOUNG_ROUTER_WORKBUDDY_DIR"
_WORKBUDDY_NODE_ENV = "YOUNG_ROUTER_WORKBUDDY_NODE"
_WORKBUDDY_WORKER_ENV = "YOUNG_ROUTER_WORKBUDDY_WORKER"
_WORKBUDDY_START_TIMEOUT_SECONDS = 45.0
_WORKBUDDY_STATUS_TIMEOUT_SECONDS = 20.0
_WORKBUDDY_MODELS_TIMEOUT_SECONDS = 90.0
# Two reads answer through this client, and both are asked for more often than
# their upstream changes.  A signed-in state moves when the user signs the
# desktop app in or out; a catalog churns about daily.  Caching them keeps one
# opened model pane from paying a worker round trip (and, for the credit, a
# desktop credential read) per model switch, while an explicit refresh still
# reaches upstream.  The TTLs differ because the cost and the churn differ:
# sign-in state is cheap to re-read and can change under the user, while the
# catalog is what the pane's 倍率 column shows and does not.
_WORKBUDDY_STATUS_TTL_SECONDS = 20.0
_WORKBUDDY_MODELS_TTL_SECONDS = 120.0
# A worker that served for at least this long died a casualty, not a defect:
# it is replaced on the spot.  One that dies younger is restarted on the next
# delay step instead, and one that keeps dying young is left to the demand
# path (a pane read, the next proxy launch) after the steps run out.
_WORKBUDDY_SETTLED_SECONDS = 60.0
_WORKBUDDY_RESTART_DELAYS_SECONDS = (0.0, 0.5, 2.0, 5.0, 15.0)


def _billing_multiplier(billing: object) -> float | None:
    """The credit rate one catalog entry bills at, as a number.

    The desktop catalog spells a rate the way its own UI does (``\"x0.79
    credits\"``, or a free entry that costs nothing).  Parsing it here keeps
    the worker's document shape in the adapter that owns the protocol, so a
    caller reasons about a rate rather than about how the catalog spells one.
    """

    if not isinstance(billing, Mapping):
        return None
    raw = billing.get("credits")
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        if text[:1] in {"x", "X", "×"}:
            text = text[1:].strip()
        text = re.sub(r"\s*credits?\s*$", "", text, flags=re.IGNORECASE).strip()
        try:
            value = float(text)
        except ValueError:
            return None
        if not math.isfinite(value) or value < 0:
            return None
        return value
    if billing.get("free") is True:
        return 0.0
    return None


def model_rate(entry: object) -> float | None:
    """The rate one catalog entry bills at, or ``None`` when it states none.

    A catalog entry is the worker's own document shape, so interpreting it
    belongs here rather than in each caller.  An entry that already carries a
    parsed ``rate`` is answered with it; otherwise the ``billing`` document is
    read the way the upstream spells it.
    """

    if not isinstance(entry, Mapping):
        return None
    parsed = entry.get("rate")
    if not isinstance(parsed, bool) and isinstance(parsed, (int, float)):
        value = float(parsed)
        return value if math.isfinite(value) and value >= 0 else None
    return _billing_multiplier(entry.get("billing"))


class WorkBuddyUnavailable(RuntimeError):
    """The staged integration is missing or its worker cannot serve."""


def staged_root() -> Path:
    """Directory holding the staged upstream package and its dependencies."""

    configured = os.environ.get(_WORKBUDDY_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).with_name("workbuddy-connect")


def worker_path() -> Path:
    configured = os.environ.get(_WORKBUDDY_WORKER_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).with_name("workbuddy_worker.mjs")


def node_command() -> str:
    return node_runtime_command(
        env_name=_WORKBUDDY_NODE_ENV,
        label="WorkBuddy",
        unavailable=WorkBuddyUnavailable,
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
    if provider not in WORKBUDDY_PROVIDERS:
        raise ValueError("Unknown WorkBuddy provider")
    return provider


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class WorkBuddyRuntime:
    """Own the WorkBuddy worker process and answer its control requests."""

    def __init__(self, runtime_root: Path | str | None = None) -> None:
        base = Path(runtime_root).expanduser() if runtime_root else Path.home() / ".young-router"
        self.root = base / ".litellm-runtime" / "workbuddy"
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
        # One catalog per variant, stamped like the status cache above: a model
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
            raise WorkBuddyUnavailable(f"WorkBuddy worker is missing: {worker}")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The remembered port is a hint, not a promise: another process may
        # hold it, in which case the worker is restarted on a fresh one and the
        # new address is persisted for the next Core.
        ready = self._spawn(node, worker, remembered)
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
            name="young-router-workbuddy-supervisor",
            daemon=True,
        ).start()

    def _supervise(self, process: subprocess.Popen[bytes] | None) -> None:
        """Replace a worker that exited without this runtime asking it to.

        A worker the upstream sidecar decides to end takes every WorkBuddy
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
                    settled = time.monotonic() - self._started_at >= _WORKBUDDY_SETTLED_SECONDS
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
                        self._restart_step + 1, len(_WORKBUDDY_RESTART_DELAYS_SECONDS)
                    )
                step = self._restart_step
            if step >= len(_WORKBUDDY_RESTART_DELAYS_SECONDS):
                return
            delay = _WORKBUDDY_RESTART_DELAYS_SECONDS[step]
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
                env={**os.environ, "YOUNG_ROUTER_WORKBUDDY_ROOT": str(self.root)},
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
            raise WorkBuddyUnavailable(f"WorkBuddy worker could not start: {exc}") from exc
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
            raise WorkBuddyUnavailable("The WorkBuddy worker state could not be stored") from exc

    def _read_ready(self, process: subprocess.Popen[bytes]) -> dict[str, Any] | None:
        """Read the worker's one-line readiness record without blocking Core."""

        if process.stdout is None:
            return None
        deadline = time.monotonic() + _WORKBUDDY_START_TIMEOUT_SECONDS
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
        for provider in WORKBUDDY_PROVIDERS:
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

    def _request(self, path: str, *, timeout: float) -> dict[str, Any]:
        if not self.ensure_started():
            raise WorkBuddyUnavailable("The WorkBuddy integration is unavailable")
        url = f"http://127.0.0.1:{self._port}{path}"
        # Loopback traffic owned by this process: it must not be reformatted by
        # an ambient proxy, and it carries the shared browser identity rather
        # than a self-naming client fingerprint.
        request = Request(
            url,
            headers={**browser_request_headers(), "Authorization": f"Bearer {self._token}"},
        )
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed loopback URL
                payload = response.read()
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise WorkBuddyUnavailable("The WorkBuddy worker did not answer") from exc
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkBuddyUnavailable("The WorkBuddy worker returned an invalid document") from exc
        if not isinstance(document, dict):
            raise WorkBuddyUnavailable("The WorkBuddy worker returned an invalid document")
        return document

    def status(self, *, refresh: bool = False) -> dict[str, Any]:
        """Report both accounts' sign-in state and catalog provenance."""

        now = time.monotonic()
        if not refresh and self._status_cache is not None:
            stamped, cached = self._status_cache
            if now - stamped < _WORKBUDDY_STATUS_TTL_SECONDS:
                return cached
        if not available():
            return {"available": False, "detail": "integration_unstaged", "providers": {}}
        if not self.ensure_started():
            return {"available": False, "detail": "worker_unavailable", "providers": {}}
        document = self._request(
            "/control/status?refresh=1" if refresh else "/control/status",
            timeout=_WORKBUDDY_STATUS_TIMEOUT_SECONDS,
        )
        providers = document.get("providers")
        result = {
            "available": True,
            "providers": dict(providers) if isinstance(providers, dict) else {},
        }
        self._status_cache = (now, result)
        if refresh:
            # A refreshing status read also re-reads both catalogs upstream (the
            # worker refreshes every variant before it answers), so an answer
            # remembered from before this call describes a catalog the worker
            # has already replaced.  That is what makes the account's own 刷新
            # button refresh the 倍率 column beside it.
            self._models_cache.clear()
        return result

    def models(self, provider: str, *, refresh: bool = False) -> dict[str, Any]:
        """Return one variant's catalog, with the worker's own provenance.

        The catalog answers the 倍率 column of every route on this account, so
        it is read far more often than it changes.  A fresh remembered answer
        is returned as-is; an explicit ``refresh`` (the pane's 刷新 button, the
        account import) always reaches upstream, and so does any read whose
        remembered answer is stale or was a failure — a signed-out variant must
        not stay signed-out in the cache once the user signs in.
        """

        name = validated_provider(provider)
        now = time.monotonic()
        if not refresh:
            remembered = self._models_cache.get(name)
            if remembered is not None and now - remembered[0] < _WORKBUDDY_MODELS_TTL_SECONDS:
                return copy.deepcopy(remembered[1])
        if not available():
            return {"available": False, "detail": "integration_unstaged", "models": []}
        suffix = "&refresh=1" if refresh else ""
        document = self._request(
            f"/control/models?provider={name}{suffix}",
            timeout=_WORKBUDDY_MODELS_TIMEOUT_SECONDS,
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
            rate = _billing_multiplier(entry.get("billing"))
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

    def open_desktop_app(self, provider: str) -> dict[str, Any]:
        """Open the desktop app that owns this variant's sign-in."""

        provider_id = validated_provider(provider)
        document = self._request(f"/control/app?provider={provider_id}", timeout=10.0)
        app_name = str(document.get("appName") or "").strip()
        bundle = str(document.get("bundle") or "").strip()
        result = {"app_name": app_name, "app_path": bundle, "opened": False}
        if not app_name and not bundle:
            return result
        try:
            if os.name == "nt":
                os.startfile(bundle or app_name)  # type: ignore[attr-defined]
                result["opened"] = True
            else:
                command = ["open", bundle] if bundle else ["open", "-a", app_name]
                subprocess.run(command, check=True, timeout=20, capture_output=True)
                result["opened"] = True
        except Exception:
            result["opened"] = False
        return result


def unsupported_status() -> dict[str, Any]:
    """Status document used when the integration is not staged."""

    return {"available": False, "detail": "integration_unstaged", "providers": {}}


def script_available() -> bool:
    return available() and worker_path().is_file()
