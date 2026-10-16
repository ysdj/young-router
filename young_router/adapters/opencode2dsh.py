"""Python adapter for the bundled ``@opencode2dsh/dsh-plugin`` worker.

OpenCode Zen's anonymous free lane is not reimplemented here.  The upstream
project (https://github.com/FishBottle7/opencode2dsh, npm
``@opencode2dsh/dsh-plugin``) owns the free-model catalog and its fallback
chain, the CLI-identical request disguise, and the ``pi-ai`` wire layer that
streams from Zen; ``scripts/update_opencode2dsh.py`` stages the published
package on every artifact build.  This module owns only what the gateway needs
around it:

* one long-lived Node worker per Core process, started on a loopback port with
  an inbound bearer this process generates, supervised for as long as this Core
  runs, and
* the two environment references the managed LiteLLM child resolves
  (``YOUNG_ROUTER_OPENCODE2DSH_BASE`` and the matching key) so no port or token
  ever reaches ``config.yaml``.

The lane needs no credential of the user's, so unlike a desktop-app account
there is no sign-in to report: a worker that cannot reach Zen reports the
upstream's own refusal in its catalog read, and the provider stays usable
whenever the lane answers.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from typing import Any, Mapping

from ..node_runtime import node_command as node_runtime_command
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..browser_identity import browser_request_headers

OPENCODE2DSH_PROVIDER = "opencode2dsh"

#: The auth kind the configuration stores for this provider.  It names a lane
#: that authenticates itself, so the kind exists to identify the integration
#: rather than to carry a user credential.
OPENCODE2DSH_AUTH_KIND = "opencode2dsh_login"
OPENCODE2DSH_AUTH_KINDS = (OPENCODE2DSH_AUTH_KIND,)
AUTH_KIND_TO_PROVIDER = {OPENCODE2DSH_AUTH_KIND: OPENCODE2DSH_PROVIDER}
PROVIDER_TO_AUTH_KIND = {value: key for key, value in AUTH_KIND_TO_PROVIDER.items()}

# The lane this integration reaches is Zen's anonymous free tier, not Zen's
# paid models: the same endpoint answers 401 AuthError for a paid id under the
# anonymous credential.  The name states that, so a user never reads it as an
# account for the paid catalog.
OPENCODE2DSH_DISPLAY_NAME = "OpenCode Zen Free"

# Variable names the managed proxy resolves.  They are process-local names,
# never credentials: the token itself lives only in this Core process and in
# the worker's command line.
API_BASE_ENV = {OPENCODE2DSH_PROVIDER: "YOUNG_ROUTER_OPENCODE2DSH_BASE"}
API_KEY_ENV = {OPENCODE2DSH_PROVIDER: "YOUNG_ROUTER_OPENCODE2DSH_KEY"}
# Provider key slot name shown in the provider editor.  The slot exists only
# because LiteLLM requires a non-empty key for an OpenAI-compatible route; its
# value is the loopback bearer above.
API_KEY_NAME = "opencode-zen-free"

_OPENCODE2DSH_DIR_ENV = "YOUNG_ROUTER_OPENCODE2DSH_DIR"
_OPENCODE2DSH_NODE_ENV = "YOUNG_ROUTER_OPENCODE2DSH_NODE"
_OPENCODE2DSH_WORKER_ENV = "YOUNG_ROUTER_OPENCODE2DSH_WORKER"
_OPENCODE2DSH_START_TIMEOUT_SECONDS = 45.0
_OPENCODE2DSH_STATUS_TIMEOUT_SECONDS = 30.0
_OPENCODE2DSH_MODELS_TIMEOUT_SECONDS = 90.0
# Two reads answer through this client, and both are asked for more often than
# their upstream changes: the catalog churns about daily, and the provenance
# line beside it moves only when a fetch succeeds or fails.  Caching them keeps
# one opened model pane from paying a worker round trip (which reaches Zen and
# models.dev) per model switch, while an explicit refresh still reaches
# upstream.
_OPENCODE2DSH_STATUS_TTL_SECONDS = 20.0
_OPENCODE2DSH_MODELS_TTL_SECONDS = 120.0
# A worker that served for at least this long died a casualty, not a defect:
# it is replaced on the spot.  One that dies younger is restarted on the next
# delay step instead, and one that keeps dying young is left to the demand path
# (a pane read, the next proxy launch) after the steps run out.
_OPENCODE2DSH_SETTLED_SECONDS = 60.0
_OPENCODE2DSH_RESTART_DELAYS_SECONDS = (0.0, 0.5, 2.0, 5.0, 15.0)


class OpenCode2DSHUnavailable(RuntimeError):
    """The staged integration is missing or its worker cannot serve."""


def staged_root() -> Path:
    """Directory holding the staged upstream package and its dependencies."""

    configured = os.environ.get(_OPENCODE2DSH_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).with_name("opencode2dsh")


def worker_path() -> Path:
    configured = os.environ.get(_OPENCODE2DSH_WORKER_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).with_name("opencode2dsh_worker.mjs")


def node_command() -> str:
    return node_runtime_command(
        env_name=_OPENCODE2DSH_NODE_ENV,
        label=OPENCODE2DSH_DISPLAY_NAME,
        unavailable=OpenCode2DSHUnavailable,
    )


# The loopback base URL and the bearer this Core owns.  They are published to
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
    if provider != OPENCODE2DSH_PROVIDER:
        raise ValueError("Unknown OpenCode Zen provider")
    return provider


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class OpenCode2DSHRuntime:
    """Own the OpenCode Zen worker process and answer its control requests."""

    def __init__(self, runtime_root: Path | str | None = None) -> None:
        base = Path(runtime_root).expanduser() if runtime_root else Path.home() / ".young-router"
        self.root = base / ".litellm-runtime" / "opencode2dsh"
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
        self._models_cache: tuple[float, dict[str, Any]] | None = None
        self._log_path = self.root / "worker.log"
        self._state_path = self.root / "worker.json"
        stored = self._read_state()
        if stored:
            self._port = stored
        self._token = self._keyring_token()
        if self._port and available():
            self._environment = self._proxy_environment()
            self._publish_environ_locked()

    # -- state ------------------------------------------------------------

    def _read_state(self) -> int:
        """The loopback port a previous Core remembered, or 0."""

        try:
            document = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if not isinstance(document, dict):
            return 0
        port = document.get("port")
        return int(port) if type(port) is int and 0 < port <= 65535 else 0

    def _keyring_path(self) -> Path:
        return self.root / "worker.key"

    def _keyring_token(self) -> str:
        """The loopback bearer, remembered so a replacement Core resumes it.

        The running proxy resolved this bearer from its own environment when it
        launched and cannot learn another one, so a replacement Core has to
        answer with the same value the worker it inherits was started with.
        """

        try:
            token = self._keyring_path().read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
        if token:
            return token
        import secrets

        token = secrets.token_urlsafe(32)
        return token

    def _persist_state(self) -> None:
        """Remember the loopback address and bearer for a replacement Core."""

        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._keyring_path().write_text(self._token, encoding="utf-8")
        try:
            os.chmod(self._keyring_path(), 0o600)
        except OSError:
            pass
        self._state_path.write_text(
            json.dumps({"port": self._port, "token": self._token}), encoding="utf-8"
        )

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
            raise OpenCode2DSHUnavailable(f"OpenCode Zen worker is missing: {worker}")
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
        # A freshly started worker has its own catalog read ahead of it: what
        # the previous worker answered describes a process that is gone.
        self._status_cache = None
        self._models_cache = None
        self._started_at = time.monotonic()
        self._persist_state()
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
            name="young-router-opencode2dsh-supervisor",
            daemon=True,
        ).start()

    def _supervise(self, process: subprocess.Popen[bytes] | None) -> None:
        """Replace a worker that exited without this runtime asking it to.

        A worker that dies takes every OpenCode Zen route with it: the running
        proxy resolved its loopback base URL from its own environment when it
        launched and cannot learn another one.  One thread per worker blocks on
        the process itself, so an idle runtime costs nothing and a death is
        repaired on the remembered port the proxy is already calling.  The
        delay steps bound a worker that keeps dying young - a broken
        integration, not a casualty.
        """

        while True:
            if process is not None:
                process.wait()
                with self._lock:
                    if self._process is not process:
                        return
                    self._process = None
                    self._stop_process(process)
                    settled = time.monotonic() - self._started_at >= _OPENCODE2DSH_SETTLED_SECONDS
            else:
                settled = False
            with self._lock:
                if self._released.is_set():
                    return
                if settled:
                    self._restart_step = 0
                else:
                    self._restart_step = min(
                        self._restart_step + 1, len(_OPENCODE2DSH_RESTART_DELAYS_SECONDS)
                    )
                step = self._restart_step
            if step >= len(_OPENCODE2DSH_RESTART_DELAYS_SECONDS):
                return
            delay = _OPENCODE2DSH_RESTART_DELAYS_SECONDS[step]
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
                env={**os.environ, "YOUNG_ROUTER_OPENCODE2DSH_ROOT": str(self.root)},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=log,
                # The worker stays in Core's process group: a Core that is
                # replaced or killed takes its worker with it instead of
                # leaving an orphan on the loopback port.
                start_new_session=False,
                close_fds=True,
            )
        except OSError as exc:
            log.close()
            raise OpenCode2DSHUnavailable(f"OpenCode Zen worker could not start: {exc}") from exc
        ready = self._read_ready(process)
        log.close()
        if ready is None:
            self._stop_process(process)
            return None
        self._process = process
        return ready

    def _read_ready(self, process: subprocess.Popen[bytes]) -> dict[str, Any] | None:
        """Read the worker's one-line readiness record without blocking Core."""

        if process.stdout is None:
            return None
        deadline = time.monotonic() + _OPENCODE2DSH_START_TIMEOUT_SECONDS
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
        return {
            API_BASE_ENV[OPENCODE2DSH_PROVIDER]: f"{base}/v1",
            API_KEY_ENV[OPENCODE2DSH_PROVIDER]: self._token,
        }

    def configure_environ(self) -> dict[str, str]:
        """Publish the fragment process-locally for Core-side resolution."""

        environment = self.environment()
        _publish(environment)
        return environment

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()
            self._restart_step = 0

    def _stop_locked(self) -> None:
        self._released.set()
        process, self._process = self._process, None
        self._port = 0
        self._environment = {}
        self._status_cache = None
        self._models_cache = None
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
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except OSError:
                    pass

    # -- control surface --------------------------------------------------

    def _request(self, path: str, *, timeout: float) -> dict[str, Any]:
        if not self.ensure_started():
            raise OpenCode2DSHUnavailable("The OpenCode Zen integration is unavailable")
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
            raise OpenCode2DSHUnavailable("The OpenCode Zen worker did not answer") from exc
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpenCode2DSHUnavailable("The OpenCode Zen worker returned an invalid document") from exc
        if not isinstance(document, dict):
            raise OpenCode2DSHUnavailable("The OpenCode Zen worker returned an invalid document")
        return document

    def status(self, *, refresh: bool = False) -> dict[str, Any]:
        """Report the catalog provenance and roster size."""

        now = time.monotonic()
        if not refresh and self._status_cache is not None:
            stamped, cached = self._status_cache
            if now - stamped < _OPENCODE2DSH_STATUS_TTL_SECONDS:
                return cached
        if not available():
            return {"available": False, "detail": "integration_unstaged", "providers": {}}
        if not self.ensure_started():
            return {"available": False, "detail": "worker_unavailable", "providers": {}}
        document = self._request("/control/status", timeout=_OPENCODE2DSH_STATUS_TIMEOUT_SECONDS)
        providers = {
            OPENCODE2DSH_PROVIDER: {
                "provider": OPENCODE2DSH_PROVIDER,
                "state": "ready" if document.get("available") is True else "unavailable",
                "detail": str(document.get("detail") or ""),
                "modelCount": document.get("modelCount"),
            }
        }
        result = {
            "available": document.get("available") is True,
            "detail": str(document.get("detail") or ""),
            "providers": providers,
        }
        self._status_cache = (now, result)
        if refresh:
            # A refreshing status read re-reads the catalog upstream, so an
            # answer remembered from before this call describes a roster the
            # worker has already replaced.
            self._models_cache = None
        return result

    def models(self, provider: str = OPENCODE2DSH_PROVIDER, *, refresh: bool = False) -> dict[str, Any]:
        """Return the catalog, with the worker's own provenance.

        The catalog answers the model pane and the account import, so it is
        read far more often than it changes.  A fresh remembered answer is
        returned as-is; an explicit ``refresh`` (the pane's 刷新 button, the
        account import) always reaches upstream, and so does any read whose
        remembered answer was a failure.
        """

        name = validated_provider(provider)
        now = time.monotonic()
        if not refresh and self._models_cache is not None:
            stamped, cached = self._models_cache
            if now - stamped < _OPENCODE2DSH_MODELS_TTL_SECONDS:
                return copy.deepcopy(cached)
        if not available():
            return {"available": False, "detail": "integration_unstaged", "models": []}
        document = self._request("/control/models", timeout=_OPENCODE2DSH_MODELS_TIMEOUT_SECONDS)
        # A catalog read answers the provenance question too, so the status
        # document it carries replaces the one the status cache holds.
        self._status_cache = None
        entries = [
            item for item in (document.get("models") or []) if isinstance(item, dict)
        ] if isinstance(document.get("models"), list) else []
        result = {
            "available": document.get("available") is True and bool(entries),
            "detail": str(document.get("detail") or ""),
            "source": str(document.get("source") or ""),
            "models": entries,
            "status": {
                "provider": name,
                "state": "ready" if document.get("available") is True else "unavailable",
                "detail": str(document.get("detail") or ""),
            },
        }
        # A read that ran before the worker reached Zen comes back available
        # but empty; remembering that would blank the pane for the whole
        # interval, so only a catalog that named a model is kept.
        if result["models"]:
            self._models_cache = (now, copy.deepcopy(result))
        else:
            self._models_cache = None
        return result

    def provider_status(self, provider: str = OPENCODE2DSH_PROVIDER) -> dict[str, Any]:
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


def unsupported_status() -> dict[str, Any]:
    """Status document used when the integration is not staged."""

    return {"available": False, "detail": "integration_unstaged", "providers": {}}


def script_available() -> bool:
    return available() and worker_path().is_file()
