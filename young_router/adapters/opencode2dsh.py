"""Bridge the existing OpenCode provider to the bundled free-model plugin.

The upstream project (https://github.com/Ebony-Vinyl/dsh-our-free-model) owns
the OpenCode-compatible request protocol, free-model routing, model metadata,
and loopback server. Its public core exports are imported in place by
``opencode2dsh_worker.mjs`` and ``scripts/update_opencode2dsh.py`` stages the
verified upstream tree for every artifact build.

This adapter owns only the Core-side process lifecycle and the two environment
references the managed LiteLLM child resolves. The provider id remains
``opencode2dsh`` so saved routes keep their identity.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any, Mapping

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..core.persistence import PersistenceError, read_json
from ..node_runtime import node_command as node_runtime_command


OPENCODE2DSH_PROVIDER = "opencode2dsh"
# The persisted auth-kind spelling keeps existing provider rows connected.
OPENCODE2DSH_AUTH_KIND = "opencode2dsh_login"
OPENCODE2DSH_AUTH_KINDS = (OPENCODE2DSH_AUTH_KIND,)
AUTH_KIND_TO_PROVIDER = {OPENCODE2DSH_AUTH_KIND: OPENCODE2DSH_PROVIDER}
PROVIDER_TO_AUTH_KIND = {OPENCODE2DSH_PROVIDER: OPENCODE2DSH_AUTH_KIND}
CATALOG_AUTH_KINDS = frozenset(OPENCODE2DSH_AUTH_KINDS)
CATALOG_AUTH_KIND_TO_PROVIDER = dict(AUTH_KIND_TO_PROVIDER)

OPENCODE2DSH_DISPLAY_NAME = "dsh-our-free-model"
API_BASE_ENV = {OPENCODE2DSH_PROVIDER: "YOUNG_ROUTER_OPENCODE2DSH_BASE"}
API_KEY_ENV = {OPENCODE2DSH_PROVIDER: "YOUNG_ROUTER_OPENCODE2DSH_KEY"}
API_KEY_NAME = "our-free-model-local"

_OPENCODE2DSH_DIR_ENV = "YOUNG_ROUTER_OPENCODE2DSH_DIR"
_OPENCODE2DSH_NODE_ENV = "YOUNG_ROUTER_OPENCODE2DSH_NODE"
_OPENCODE2DSH_WORKER_ENV = "YOUNG_ROUTER_OPENCODE2DSH_WORKER"
_OPENCODE2DSH_START_TIMEOUT_SECONDS = 45.0
_OPENCODE2DSH_STATUS_TIMEOUT_SECONDS = 30.0
_OPENCODE2DSH_MODELS_TIMEOUT_SECONDS = 90.0
_SOURCE_STATE = "sourceDir"


class OpenCode2DSHUnavailable(RuntimeError):
    """The staged free-model integration cannot serve."""


class OpenCode2DSHCatalogUnavailable(OpenCode2DSHUnavailable):
    """The worker answers, but the remote catalog cannot be read."""


def _lock_file(handle: Any, *, wait: bool) -> bool:
    """Lock one byte owned by the worker handoff handshake."""

    try:
        _lock_handle(handle, wait=wait)
    except OSError:
        return False
    return True


def _unlock_file(handle: Any) -> None:
    _unlock_handle(handle)


def _lock_handle(handle: Any, *, wait: bool) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))


def _unlock_handle(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_UN)


def staged_root() -> Path:
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


def upstream_environment() -> dict[str, str]:
    """Point the upstream's explicitly configurable ESBuild install at its own."""

    configured = os.environ.get("ESBUILD_BINARY_PATH", "").strip()
    binary = (
        Path(configured).expanduser()
        if configured
        else load_root() / "node_modules" / ".bin" / "esbuild"
    )
    if not binary.is_file():
        return {}
    return {"ESBUILD_BINARY_PATH": str(binary)}


_PUBLISHED_LOCK = threading.Lock()
_PUBLISHED: dict[str, str] = {}


def published_environment() -> dict[str, str]:
    with _PUBLISHED_LOCK:
        return dict(_PUBLISHED)


def _publish(values: Mapping[str, str]) -> None:
    with _PUBLISHED_LOCK:
        _PUBLISHED.clear()
        _PUBLISHED.update(values)


def available() -> bool:
    try:
        if not worker_path().is_file():
            return False
        return (load_root() / "package.json").is_file()
    except OSError:
        return False


def load_root() -> Path:
    configured = os.environ.get(
        "YOUNG_ROUTER_OUR_FREE_MODEL_SOURCE_DIR", ""
    ).strip()
    if configured:
        return Path(configured).expanduser()
    configured = os.environ.get(_OPENCODE2DSH_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    adjacent = worker_path().parent / "opencode2dsh"
    return adjacent if (adjacent / "package.json").is_file() else staged_root()


def api_base_reference(provider: str) -> str:
    return f"os.environ/{API_BASE_ENV[validated_provider(provider)]}"


def api_key_reference(provider: str) -> str:
    return f"os.environ/{API_KEY_ENV[validated_provider(provider)]}"


def validated_provider(value: object) -> str:
    provider = str(value or "").strip()
    if provider != OPENCODE2DSH_PROVIDER:
        raise ValueError("Unknown OpenCode provider")
    return provider


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _safe_problem(error: object) -> str:
    """One line from a worker failure, without request bodies or headers."""

    text = str(error or "").strip().splitlines()
    return text[0][:800] if text else "dsh-our-free-model worker failed"


class OpenCode2DSHRuntime:
    """Own one worker serving the upstream's free-model runtime."""

    def __init__(
        self,
        runtime_root: Path | str | None = None,
        *,
        settings: Mapping[str, str] | None = None,
    ) -> None:
        base = Path(runtime_root) if runtime_root is not None else Path.home() / ".young-router"
        self.root = base / ".litellm-runtime" / OPENCODE2DSH_PROVIDER
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings = dict(settings or {})
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._ready_pipe: Any | None = None
        self._worker_lock: Any | None = None
        self._port = 0
        self._gateway_port = 0
        self._token = ""
        self._stopping = False

    def environment(self, *, autostart: bool = False) -> dict[str, str]:
        if autostart:
            self.ensure_started()
        return self.environment_values()

    def configure_environ(self) -> dict[str, str]:
        return self.environment(autostart=False)

    def autostart_if_configured(self) -> None:
        """Start the worker only when a saved route already adopts this lane."""

        if not self._gateway_port and not published_environment():
            self.ensure_started()

    def ensure_configured(self) -> bool:
        """Confirm the loopback worker is running without reading the outer network."""

        with self._lock:
            if self._process is None or self._process.poll() is not None:
                raise OpenCode2DSHUnavailable("The dsh-our-free-model worker is not running")
            self._request("/health", timeout=2.0, authenticated=False)
            return True

    def ensure_started(self) -> bool:
        with self._lock:
            if self._process is not None and self._process.poll() is None and self._healthy():
                return True
            self._stopping = False
            self._stop()
            source_dir = str(self.settings.get(_SOURCE_STATE, "")).strip()
            if source_dir:
                os.environ["YOUNG_ROUTER_OUR_FREE_MODEL_SOURCE_DIR"] = source_dir
            staged = load_root()
            manifest = staged / "package.json"
            if not manifest.is_file() or not worker_path().is_file():
                raise OpenCode2DSHUnavailable(
                    f"The bundled free-model integration is missing: {staged}"
                )
            port = _free_port()
            token = secrets.token_urlsafe(32)
            os.environ["YOUNG_ROUTER_OUR_FREE_MODEL_SOURCE_DIR"] = str(staged)
            process = self._spawn(worker_path(), port, token, manifest)
            self._process = process
            self._port = 0
            self._token = token
            try:
                self._wait_ready(process, port)
            except Exception:
                self._stop_process(process)
                raise
            self._publish_environ_locked()
            return True

    def _healthy(self) -> bool:
        if not self._port:
            return False
        try:
            self._request("/health", timeout=2.0, authenticated=False)
        except Exception:
            return False
        return True

    def _spawn(
        self,
        worker: Path,
        port: int,
        token: str,
        manifest: Path,
    ) -> subprocess.Popen[bytes]:
        args = [
            node_command(),
            str(worker),
            "--port",
            str(port),
            "--root",
            str(self.root),
        ]
        for key, value in sorted(self.settings.items()):
            args.extend(("--setting", f"{key}={value}"))
        log_path = self.root / f"worker.{port}.log"
        log_path.unlink(missing_ok=True)
        handoff = tempfile.TemporaryFile(mode="w+", dir=self.root, prefix="handoff.", suffix=".lock")
        _lock_file(handoff, wait=True)
        handoff.seek(0)
        handoff.truncate()
        handoff.write(token)
        handoff.flush()
        os.fsync(handoff.fileno())
        (self.root / "online.json").unlink(missing_ok=True)
        worker_lock = tempfile.TemporaryFile(
            mode="w+", dir=self.root, prefix="worker.", suffix=".lock"
        )
        _lock_handle(worker_lock, wait=False)
        args.extend(
            (
                "--handoff",
                str(handoff.fileno()),
                "--lease",
                str(worker_lock.fileno()),
                "--entry",
                str(manifest),
            )
        )
        with log_path.open("wb") as log:
            try:
                process = subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env={
                        **os.environ,
                        **upstream_environment(),
                        "YOUNG_ROUTER_OUR_FREE_MODEL_TOKEN": token,
                        "YOUNG_ROUTER_OUR_FREE_MODEL_ROOT": str(self.root),
                        "RUNNER_TEMP": str(self.root),
                    },
                    pass_fds=(handoff.fileno(), worker_lock.fileno()) if os.name != "nt" else (),
                    start_new_session=os.name != "nt",
                )
            except OSError as exc:
                _unlock_handle(worker_lock)
                worker_lock.close()
                _unlock_handle(handoff)
                handoff.close()
                raise OpenCode2DSHUnavailable(
                    f"dsh-our-free-model worker could not start: {_safe_problem(exc)}"
                ) from exc
        self._ready_pipe = handoff
        self._worker_lock = worker_lock
        return process

    def _wait_ready(self, process: subprocess.Popen[bytes], port: int) -> None:
        handoff = self._ready_pipe
        if handoff is None:
            raise OpenCode2DSHUnavailable("The dsh-our-free-model worker hand-off is unavailable")
        deadline = time.monotonic() + _OPENCODE2DSH_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            # The worker creates its lease while the handoff remains held.
            worker_lock = self._worker_lock
            if worker_lock is None:
                raise OpenCode2DSHUnavailable("The dsh-our-free-model worker hand-off was lost")
            _unlock_handle(handoff)
            handoff.flush()
            if process.poll() is not None:
                raise OpenCode2DSHUnavailable(self._worker_failure(process, port))
            try:
                _lock_handle(worker_lock, wait=False)
            except OSError:
                time.sleep(0.05)
                continue
            if not (self.root / "online.json").is_file():
                _unlock_handle(worker_lock)
                time.sleep(0.05)
                continue
            self._ready_handle = None
            self._ready_pipe = None
            self._worker_lock = None
            break
        else:
            raise OpenCode2DSHUnavailable("The dsh-our-free-model worker did not start in time")
        ready_path = self.root / "online.json"
        if ready_path.stat().st_mode & 0o077:
            raise OpenCode2DSHUnavailable("The dsh-our-free-model readiness file is not private")
        ready = read_json(ready_path, default={})
        try:
            control_port = ready["controlPort"]
            gateway_port = ready["gatewayPort"]
            if type(control_port) is not int or type(gateway_port) is not int:
                raise TypeError
        except (KeyError, TypeError):
            raise OpenCode2DSHUnavailable("The dsh-our-free-model readiness file is invalid") from None
        self._port = control_port
        self._gateway_port = gateway_port
        handoff.close()

    def _worker_failure(self, process: subprocess.Popen[bytes], port: int) -> str:
        try:
            text = (self.root / f"worker.{port}.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        last = next((line.strip() for line in reversed(text.splitlines()) if line.strip()), "")
        return f"Exit {process.returncode}: {last}" if last else f"Exit {process.returncode}"

    def _proxy_environment(self) -> dict[str, str]:
        if not self._token:
            return {}
        try:
            online = read_json(self.root / "online.json", default={})
            gateway_port = int(online["gatewayPort"])
        except (PersistenceError, KeyError, TypeError, ValueError):
            gateway_port = self._gateway_port
        if not gateway_port:
            return {}
        base = f"http://127.0.0.1:{gateway_port}"
        return {
            API_BASE_ENV[OPENCODE2DSH_PROVIDER]: f"{base}/v1",
            API_KEY_ENV[OPENCODE2DSH_PROVIDER]: self._token,
        }

    def environment_values(self) -> dict[str, str]:
        with self._lock:
            source_dir = str(self.settings.get(_SOURCE_STATE, "")).strip()
            if source_dir:
                os.environ["YOUNG_ROUTER_OUR_FREE_MODEL_SOURCE_DIR"] = source_dir
            return self._proxy_environment() if self._gateway_port else {}

    def _publish_environ_locked(self) -> None:
        values = self._proxy_environment()
        _publish(values)
        os.environ.update(values)

    def stop(self) -> None:
        with self._lock:
            self._stop()
        _publish({})
        for name in (*API_BASE_ENV.values(), *API_KEY_ENV.values()):
            os.environ.pop(name, None)

    def _stop(self) -> None:
        process = self._process
        self._stopping = True
        self._process = None
        self._port = 0
        self._gateway_port = 0
        self._token = ""
        if self._worker_lock is not None:
            _unlock_handle(self._worker_lock)
            self._worker_lock.close()
            self._worker_lock = None
        if self._ready_pipe is not None:
            _unlock_handle(self._ready_pipe)
            self._ready_pipe.close()
            self._ready_pipe = None
        self._stop_process(process)

    def _stop_process(self, process: subprocess.Popen[bytes] | None) -> None:
        self._stopping = True
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    def status(self, *, refresh: bool = False) -> dict[str, Any]:
        document = self._request(
            "/control/status",
            timeout=_OPENCODE2DSH_STATUS_TIMEOUT_SECONDS,
        )
        return {
            "providers": {
                OPENCODE2DSH_PROVIDER: {
                    "provider": OPENCODE2DSH_PROVIDER,
                    "state": "ready" if document.get("ok") is True else "failed",
                    **{
                        key: document[key]
                        for key in ("version", "laneState")
                        if document.get(key) is not None
                    },
                }
            }
        }

    def provider_status(self, provider: str = OPENCODE2DSH_PROVIDER) -> dict[str, Any]:
        validated_provider(provider)
        return dict(self.status().get("providers", {}).get(provider, {}))

    def models(
        self,
        provider: str = OPENCODE2DSH_PROVIDER,
        *,
        refresh: bool = False,
    ) -> dict[str, Any]:
        validated_provider(provider)
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                self.ensure_started()
        try:
            document = self._request(
                "/control/refresh" if refresh else "/control/models",
                method="POST" if refresh else "GET",
                body={"probe": False} if refresh else None,
                timeout=_OPENCODE2DSH_MODELS_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            raise OpenCode2DSHCatalogUnavailable("The free-model catalog is unavailable") from exc
        if refresh and not document.get("models"):
            document = self._request(
                "/control/models",
                timeout=_OPENCODE2DSH_MODELS_TIMEOUT_SECONDS,
            )
        return {
            "operation": "opencode2dsh_models",
            "provider": provider,
            "available": document.get("ok") is True,
            "source": "dsh-our-free-model",
            "models": list(document.get("models") or []),
        }

    def _request(
        self,
        path: str,
        *,
        port: int | None = None,
        timeout: float,
        authenticated: bool = True,
        method: str = "GET",
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            target_port = port or self._port
            token = self._token
        if not target_port:
            raise OpenCode2DSHUnavailable("The dsh-our-free-model worker is not running")
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"http://127.0.0.1:{target_port}{path}",
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise OpenCode2DSHUnavailable(
                f"The dsh-our-free-model worker answered HTTP {exc.code}"
            ) from exc
        except (URLError, OSError, UnicodeDecodeError, ValueError) as exc:
            raise OpenCode2DSHUnavailable(_safe_problem(exc)) from exc
        if not isinstance(parsed, dict):
            raise OpenCode2DSHUnavailable("The dsh-our-free-model worker returned an invalid document")
        return parsed


def unsupported_status() -> dict[str, Any]:
    return {
        "providers": {
            OPENCODE2DSH_PROVIDER: {
                "provider": OPENCODE2DSH_PROVIDER,
                "state": "missing",
            }
        }
    }


__all__ = [
    "API_BASE_ENV",
    "API_KEY_ENV",
    "API_KEY_NAME",
    "AUTH_KIND_TO_PROVIDER",
    "CATALOG_AUTH_KINDS",
    "CATALOG_AUTH_KIND_TO_PROVIDER",
    "OPENCODE2DSH_AUTH_KIND",
    "OPENCODE2DSH_AUTH_KINDS",
    "OPENCODE2DSH_DISPLAY_NAME",
    "OPENCODE2DSH_PROVIDER",
    "OpenCode2DSHRuntime",
    "OpenCode2DSHCatalogUnavailable",
    "OpenCode2DSHUnavailable",
    "api_base_reference",
    "api_key_reference",
    "available",
    "node_command",
    "published_environment",
    "staged_root",
    "unsupported_status",
    "validated_provider",
    "worker_path",
]
