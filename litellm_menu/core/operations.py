"""Production-only local operations used by the Python Core.

The native hosts never execute shell commands or read user configuration. This
module owns the small OS boundary needed by the Core: lifecycle control is
performed directly from Python, status is read from bounded private files, and
the existing configuration-package and usage readers remain the authoritative
format/parsing implementations.
"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
import tempfile
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any

from .persistence import (
    PersistenceError,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    read_json,
    read_text,
)
from .security import REDACT_TEXT, safe_exception_message


MAX_OPERATION_OUTPUT_BYTES = 128 * 1024
MAX_USAGE_ROWS = 100
SERVICE_STATES = frozenset({"starting", "running", "unhealthy", "stopped", "unknown"})
OWNER_RECORD_VERSION = 2
OWNER_TOKEN_ENV = "LITELLM_MENU_SERVICE_OWNER_TOKEN"
CORE_PID_ENV = "LITELLM_MENU_CORE_PID"
OWNER_TOKEN_BYTES = 32
MACOS_DEFAULT_WORKERS = "16"
PROXY_STOP_GRACE_SECONDS = 2.0


@dataclass(frozen=True)
class ServiceOwnerRecord:
    """Private identity evidence for one Core-owned LiteLLM child."""

    pid: int
    identity: str
    token: str
    core_pid: int
    core_identity: str


def _runtime_root(value: Path | str | None = None) -> Path:
    if value is not None:
        return Path(value).expanduser()
    configured = os.environ.get("LITELLM_RUNTIME_ROOT", "").strip() or os.environ.get(
        "LITELLM_MENU_HOME", ""
    ).strip()
    return Path(configured).expanduser() if configured else Path.home() / ".litellm-menu"


def _bounded_text(value: object, *, limit: int = 240) -> str:
    return REDACT_TEXT(str(value))[:limit]


def _safe_object(value: object, *, limit: int = 64) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key, item in list(value.items())[:limit]:
        if isinstance(item, (str, bool, int, float)) or item is None:
            result[str(key)[:80]] = _bounded_text(item) if isinstance(item, str) else item
    return result


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    config: Path
    runtime_config: Path
    settings: Path
    pid: Path
    owner: Path
    autostart: Path
    webdav_enabled: Path
    webdav_status: Path
    webdav_sync_state: Path
    recovery: Path
    cooldowns: Path

    @classmethod
    def from_root(cls, root: Path | str | None = None) -> "RuntimePaths":
        base = _runtime_root(root)
        runtime = base / ".litellm-runtime"
        return cls(
            root=base,
            config=Path(os.environ.get("LITELLM_CONFIG_FILE", base / "config.yaml")).expanduser(),
            runtime_config=Path(os.environ.get("LITELLM_RUNTIME_CONFIG", runtime / "config.yaml")).expanduser(),
            settings=Path(os.environ.get("LITELLM_MENU_RUNTIME_SETTINGS_FILE", base / "runtime-settings.env")).expanduser(),
            pid=Path(os.environ.get("LITELLM_NATIVE_PID_FILE", runtime / "litellm.pid")).expanduser(),
            owner=Path(os.environ.get("LITELLM_NATIVE_OWNER_FILE", runtime / "litellm.owner")).expanduser(),
            autostart=Path(os.environ.get("LITELLM_AUTOSTART_STATE_FILE", runtime / "autostart.enabled")).expanduser(),
            webdav_enabled=Path(os.environ.get("LITELLM_WEBDAV_SYNC_ENABLED_FILE", runtime / "webdav-sync.enabled")).expanduser(),
            webdav_status=Path(os.environ.get("LITELLM_WEBDAV_SYNC_STATUS_FILE", runtime / "webdav-sync-status.json")).expanduser(),
            webdav_sync_state=Path(os.environ.get("LITELLM_WEBDAV_SYNC_STATE", runtime / "webdav-sync-state.json")).expanduser(),
            recovery=Path(os.environ.get("LITELLM_MENU_ROUTE_RECOVERY_STATE_FILE", runtime / "route-recovery-state.json")).expanduser(),
            cooldowns=Path(os.environ.get("LITELLM_MENU_DEPLOYMENT_COOLDOWN_FILE", runtime / "deployment-cooldowns.json")).expanduser(),
        )


class CoreServiceController:
    """Own the managed LiteLLM child process without a shell UI backend."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        python: str | None = None,
        litellm_bin: str | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.paths = RuntimePaths.from_root(root)
        self.python = python or os.environ.get("LITELLM_NATIVE_PYTHON") or sys.executable
        configured_bin = litellm_bin or os.environ.get("LITELLM_BIN", "").strip()
        if not configured_bin:
            executable_name = "litellm.exe" if os.name == "nt" else "litellm"
            venv_bin = "Scripts" if os.name == "nt" else "bin"
            candidates = (
                self.paths.root / ".venv" / venv_bin / executable_name,
                Path(sys.executable).parent / executable_name,
            )
            configured_bin = next((str(path) for path in candidates if path.is_file()), executable_name)
        self.litellm_bin = configured_bin
        self._environment = dict(environment or {})

    @staticmethod
    def _process_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            process_query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            kernel32.GetExitCodeProcess.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        try:
            environment = os.environ.copy()
            environment["LC_ALL"] = "C"
            result = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1.0,
                check=False,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        state = result.stdout.strip()
        return result.returncode == 0 and bool(state) and not state.startswith("Z")

    @staticmethod
    def _windows_process_identity(pid: int) -> str | None:
        if os.name != "nt":
            return None
        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetProcessTimes.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
        ]
        kernel32.GetProcessTimes.restype = ctypes.c_int
        kernel32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return None
        try:
            creation = ctypes.c_ulonglong()
            exit_time = ctypes.c_ulonglong()
            kernel = ctypes.c_ulonglong()
            user = ctypes.c_ulonglong()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            image = ctypes.create_unicode_buffer(32768)
            image_size = ctypes.c_ulong(len(image))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(image_size)):
                return None
            image_digest = hashlib.sha256(os.path.normcase(image.value).encode("utf-8")).hexdigest()
            return f"windows:{creation.value}:{image_digest}"
        finally:
            kernel32.CloseHandle(handle)

    @staticmethod
    def _posix_process_identity(pid: int) -> str | None:
        if os.name == "nt":
            return None
        try:
            environment = os.environ.copy()
            environment["LC_ALL"] = "C"
            result = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1.0,
                check=False,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        started = " ".join(result.stdout.split())
        if result.returncode != 0 or not started:
            return None
        # Store only a fixed-size process identity, not a user path or command.
        digest = hashlib.sha256(started.encode("utf-8")).hexdigest()
        return f"posix-lstart-sha256:{digest}"

    @classmethod
    def _process_identity(cls, pid: int) -> str | None:
        if not cls._process_alive(pid):
            return None
        return cls._windows_process_identity(pid) if os.name == "nt" else cls._posix_process_identity(pid)

    @staticmethod
    def _posix_process_has_token(pid: int, token: str) -> bool:
        if os.name == "nt":
            return True
        try:
            result = subprocess.run(
                ["ps", "eww", "-o", "command=", "-p", str(pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        marker = f"{OWNER_TOKEN_ENV}={token}"
        return result.returncode == 0 and marker in result.stdout.split()

    def _read_owner_record(self) -> ServiceOwnerRecord | None:
        try:
            payload = read_json(self.paths.owner)
        except PersistenceError:
            return None
        if set(payload) != {"version", "pid", "identity", "token", "core_pid", "core_identity"}:
            return None
        pid = payload.get("pid")
        identity = payload.get("identity")
        token = payload.get("token")
        core_pid = payload.get("core_pid")
        core_identity = payload.get("core_identity")
        if (
            payload.get("version") != OWNER_RECORD_VERSION
            or type(pid) is not int
            or pid <= 0
            or not isinstance(identity, str)
            or not identity
            or len(identity.encode("utf-8")) > 160
            or not isinstance(token, str)
            or len(token) < 32
            or len(token.encode("utf-8")) > 256
            or type(core_pid) is not int
            or core_pid <= 0
            or not isinstance(core_identity, str)
            or not core_identity
            or len(core_identity.encode("utf-8")) > 160
        ):
            return None
        return ServiceOwnerRecord(
            pid=pid,
            identity=identity,
            token=token,
            core_pid=core_pid,
            core_identity=core_identity,
        )

    def _core_identity(self) -> tuple[int, str] | None:
        core_pid = os.getpid()
        identity = self._process_identity(core_pid)
        if identity is None:
            return None
        return core_pid, identity

    def _write_owner_record(self, process: subprocess.Popen[bytes] | subprocess.Popen[str], token: str) -> None:
        # Popen has returned, but the child may need a few scheduler turns
        # before process metadata becomes visible. Never publish an owner record
        # without an exact identity that a replacement Core can verify.
        identity: str | None = None
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and process.poll() is None:
            identity = self._process_identity(process.pid)
            if identity is not None and (os.name == "nt" or self._posix_process_has_token(process.pid, token)):
                break
            identity = None
            time.sleep(0.02)
        if identity is None:
            raise PersistenceError("LiteLLM service identity could not be recorded")
        core = self._core_identity()
        if core is None:
            raise PersistenceError("Core process identity could not be recorded")
        core_pid, core_identity = core
        atomic_write_json(
            self.paths.owner,
            {
                "version": OWNER_RECORD_VERSION,
                "pid": process.pid,
                "identity": identity,
                "token": token,
                "core_pid": core_pid,
                "core_identity": core_identity,
            },
        )

    def _remove_owner_files(self) -> None:
        for path in (self.paths.pid, self.paths.owner):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def _configured_runtime_values(self, *, strict: bool) -> dict[str, str]:
        """Read only explicitly persisted, schema-validated environment values."""

        try:
            source = read_text(self.paths.settings)
            if source is None:
                return {}
            from runtime_settings_io import load_specs, read_configured_settings_file

            specs = load_specs()
            with tempfile.TemporaryDirectory(prefix="litellm-core-runtime-env-") as directory:
                candidate = Path(directory) / "runtime-settings.env"
                atomic_write_text(candidate, source)
                # The authoritative parser validates every line and all
                # cross-field constraints before any values enter a process.
                return read_configured_settings_file(candidate, specs)
        except Exception:
            if strict:
                raise RuntimeError("Runtime settings are invalid") from None
            return {}

    def _runtime_env(self, *, strict: bool = True) -> dict[str, str]:
        env = os.environ.copy()
        env.update(self._environment)
        # This optional credential is file-owned: clearing it in Runtime
        # Settings must override an inherited host environment.
        env.pop("LITELLM_MENU_VISION_BRIDGE_API_KEY", None)
        configured = self._configured_runtime_values(strict=strict)
        # The native preview port is a host-owned isolation boundary. A
        # persisted user setting must not redirect an isolated preview back
        # onto production's 4000 listener.
        forced_port = self._environment.get("LITELLM_PORT")
        env.update(configured)
        if forced_port is not None:
            env["LITELLM_PORT"] = forced_port
        env["LITELLM_RUNTIME_ROOT"] = str(self.paths.root)
        env["LITELLM_CONFIG_FILE"] = str(self.paths.config)
        env["LITELLM_RUNTIME_CONFIG"] = str(self.paths.runtime_config)
        env["LITELLM_MENU_RUNTIME_SETTINGS_FILE"] = str(self.paths.settings)
        env["LITELLM_NATIVE_PID_FILE"] = str(self.paths.pid)
        env["LITELLM_MENU_ROUTE_RECOVERY_STATE_FILE"] = str(self.paths.recovery)
        env["LITELLM_MENU_DEPLOYMENT_COOLDOWN_FILE"] = str(self.paths.cooldowns)
        # Keep request summaries beside the managed configuration for both
        # Finder-launched and terminal-launched Core processes.  The native
        # preview path sets this explicitly too; production must not rely on
        # an inherited shell environment.
        env["LITELLM_RECENT_REQUESTS_LOG"] = str(self.paths.root / "recent-requests.jsonl")
        # LiteLLM resolves dotted callbacks relative to the staged config
        # before falling back to imports.  ``sitecustomize`` owns that safe,
        # allowlisted fallback for ``litellm_menu.*``; make it available to
        # the proxy process together with the bundled package root.
        core_root = Path(__file__).resolve().parents[2]
        python_path = [str(core_root)]
        inherited_python_path = env.get("PYTHONPATH", "")
        if inherited_python_path:
            python_path.extend(
                item for item in inherited_python_path.split(os.pathsep) if item
            )
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(python_path))
        env["LITELLM_TEMPLATE_ROOT"] = str(core_root)
        env["LITELLM_MENU_PROXY_PROCESS"] = "1"
        env["LITELLM_MENU_TIMESTAMP_OUTPUT"] = "1"
        env["LITELLM_MENU_SERVICE_LOG"] = str(self.paths.root / "menu-server.log")
        env["LITELLM_WORKER_STARTUP_HOOKS"] = (
            "litellm_menu.search_endpoint:register"
        )
        # The packaged LiteLLM already ships its model catalog.  Never make
        # each of the sixteen macOS worker starts wait on a best-effort
        # GitHub refresh before serving the local proxy.
        env["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"
        env[CORE_PID_ENV] = str(os.getpid())
        return env

    def _configured_port(self, env: Mapping[str, str] | None = None) -> int:
        source = env if env is not None else self._runtime_env(strict=False)
        value = source.get("LITELLM_PORT", "4000")
        try:
            port = int(value)
        except ValueError:
            return 4000
        return port if 1 <= port <= 65535 else 4000

    def _recorded_pid(self) -> int | None:
        try:
            raw = read_text(self.paths.pid)
            if raw is None:
                return None
            raw = raw.strip()
            pid = int(raw)
        except (PersistenceError, ValueError):
            return None
        record = self._read_owner_record()
        if record is None or pid <= 0 or record.pid != pid:
            return None
        identity = self._process_identity(pid)
        if identity is None or not secrets.compare_digest(identity, record.identity):
            return None
        if not self._posix_process_has_token(pid, record.token):
            return None
        return pid

    def _pid(self) -> int | None:
        """Return the proxy only when this exact Core owns it."""

        pid = self._recorded_pid()
        record = self._read_owner_record()
        core = self._core_identity()
        if pid is None or record is None or core is None:
            return None
        core_pid, core_identity = core
        if record.core_pid != core_pid or not secrets.compare_digest(record.core_identity, core_identity):
            return None
        return pid

    def _recorded_proxy_is_orphaned(self) -> int | None:
        """Return only a verified proxy whose recorded Core has exited."""

        pid = self._recorded_pid()
        record = self._read_owner_record()
        if pid is None or record is None:
            return None
        current_core = self._core_identity()
        if current_core is not None:
            current_pid, current_identity = current_core
            if record.core_pid == current_pid and secrets.compare_digest(record.core_identity, current_identity):
                return None
        recorded_core_identity = self._process_identity(record.core_pid)
        if recorded_core_identity is not None and secrets.compare_digest(recorded_core_identity, record.core_identity):
            return None
        return pid

    def _health(self, port: int | None = None) -> bool:
        target_port = self._configured_port() if port is None else port
        request = urllib.request.Request(
            f"http://127.0.0.1:{target_port}/health/liveliness",
            headers={"Accept": "application/json", "User-Agent": "LiteLLM-Menu-Core/1"},
            method="GET",
        )
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=0.5) as response:
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                return isinstance(status, int) and 200 <= status < 300
        except Exception:
            return False

    def _stage_runtime_config(self) -> None:
        if not self.paths.config.exists():
            raise RuntimeError("Provider/model configuration is unavailable")
        from config_editor_core.schema import _load_yaml

        try:
            _load_yaml(self.paths.config)
            data = self.paths.config.read_bytes()
            atomic_write_bytes(self.paths.runtime_config, data)
        except PersistenceError:
            raise
        except Exception:
            raise RuntimeError("Provider/model configuration is invalid") from None

    def _clear_transient_routing_state(self) -> None:
        """Remove recovery/cooldown data before creating a new proxy."""

        for path in (self.paths.recovery, self.paths.cooldowns):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise RuntimeError("Transient routing state could not be reset") from exc

    def status(self) -> dict[str, Any]:
        pid = self._pid()
        port = self._configured_port()
        healthy = self._health(port)
        if healthy and pid is not None:
            state = "running"
        elif pid is not None:
            state = "unhealthy"
        elif self._recorded_proxy_is_orphaned() is not None:
            # A verified child of a dead Core is recoverable by normal start;
            # it is not an unrelated listener occupying the configured port.
            state = "stopped"
        elif healthy:
            # A listener without this Core process's private ownership record
            # must never be stopped or adopted implicitly.
            state = "unknown"
        else:
            state = "stopped"
        result: dict[str, Any] = {"state": state, "auto_start_state": self.autostart_status()}
        if pid is not None:
            result["pid"] = pid
        if state == "running":
            result["port"] = port
        result["route_recovery"] = self._recovery_summary()
        result["webdav"] = self._webdav_summary()
        return result

    def dispatch(self, operation: str) -> dict[str, Any]:
        """Execute one supported lifecycle/menu operation and return status."""

        normalized = operation.strip().replace("-", "_")
        methods = {
            "start": self.start,
            "stop": self.stop,
            "restart": self.restart,
            "reload": self.reload,
            "health": self.status,
            "status": self.status,
            "autostart_enable": self.autostart_enable,
            "autostart_disable": self.autostart_disable,
            "autostart_status": lambda: {"auto_start_state": self.autostart_status(), **self.status()},
        }
        method = methods.get(normalized)
        if method is None:
            raise RuntimeError("The requested service operation is unavailable")
        result = method()
        if not isinstance(result, Mapping):
            raise RuntimeError("LiteLLM service returned invalid status")
        return dict(result)

    def start(self) -> dict[str, Any]:
        current = self.status()
        if current["state"] == "running":
            return current
        orphaned_pid = self._recorded_proxy_is_orphaned()
        if orphaned_pid is not None:
            self._stop_process_group(orphaned_pid)
            self._remove_owner_files()
            current = self.status()
        if current["state"] == "unknown":
            raise RuntimeError("The configured LiteLLM port is already in use")
        if current["state"] == "unhealthy":
            raise RuntimeError("A managed LiteLLM service is already active")
        self._stage_runtime_config()
        self._clear_transient_routing_state()
        self.paths.runtime_config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        environment = self._runtime_env()
        owner_token = secrets.token_urlsafe(OWNER_TOKEN_BYTES)
        environment[OWNER_TOKEN_ENV] = owner_token
        port = self._configured_port(environment)
        workers = environment.get("LITELLM_NUM_WORKERS", MACOS_DEFAULT_WORKERS if sys.platform == "darwin" else "16")
        launcher = [self.litellm_bin]
        if os.name == "nt" and Path(self.litellm_bin).suffix.lower() in {".cmd", ".bat"}:
            # Packaged Windows installs expose a small command shim for users,
            # but CreateProcess cannot execute batch files with shell=False.
            # Start LiteLLM through the bundled interpreter instead.
            launcher = [
                self.python,
                "-c",
                "from litellm import run_server; run_server()",
            ]
        if os.name != "nt" and sys.platform == "darwin":
            command = [
                self.python,
                "-m",
                "litellm_menu.macos_proxy",
                "--config",
                str(self.paths.runtime_config),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--workers",
                str(workers),
            ]
        else:
            command = [
                *launcher,
                "--config",
                str(self.paths.runtime_config),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--num_workers",
                str(workers),
                "--telemetry",
                "False",
            ]
            if os.name != "nt":
                command.append("--run_gunicorn")
        server_log = self.paths.root / "menu-server.log"
        server_log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with server_log.open("a", encoding="utf-8") as server_log_handle:
                process = subprocess.Popen(
                    command,
                    cwd=self.paths.root,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=server_log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=os.name != "nt",
                    close_fds=True,
                )
            self._write_owner_record(process, owner_token)
            atomic_write_text(self.paths.pid, f"{process.pid}\n")
        except (OSError, PersistenceError) as exc:
            if "process" in locals():
                self._stop_process_group(process.pid)
            self._remove_owner_files()
            raise RuntimeError("LiteLLM service could not start") from exc
        deadline = time.monotonic() + min(max(float(environment.get("LITELLM_HEALTH_WAIT_SECONDS", "60")), 1), 60)
        while time.monotonic() < deadline:
            if self._health():
                return self.status()
            if process.poll() is not None:
                break
            time.sleep(0.1)
        self._stop_process_group(process.pid)
        self._remove_owner_files()
        raise RuntimeError("LiteLLM service did not become healthy")

    @staticmethod
    def _stop_process_group(pid: int) -> None:
        try:
            if hasattr(os, "killpg"):
                os.killpg(pid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
        except OSError:
            return
        deadline = time.monotonic() + PROXY_STOP_GRACE_SECONDS
        while time.monotonic() < deadline and CoreServiceController._process_alive(pid):
            time.sleep(0.1)
        if CoreServiceController._process_alive(pid):
            try:
                if hasattr(os, "killpg"):
                    os.killpg(pid, signal.SIGKILL)
                else:
                    os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def stop(self) -> dict[str, Any]:
        pid = self._pid()
        if pid is None:
            # Do not erase an active replacement Core's ownership evidence.
            return self.status()
        self._stop_process_group(pid)
        self._remove_owner_files()
        return self.status()

    def restart(self) -> dict[str, Any]:
        self.stop()
        return self.start()

    def reload(self) -> dict[str, Any]:
        if self._pid() is None:
            raise RuntimeError("No managed LiteLLM service is running")
        return self.restart()

    def autostart_enable(self) -> dict[str, Any]:
        # Platform native hosts own the actual login-item registration. Core
        # persists only its protected preference and reports its real state.
        atomic_write_text(self.paths.autostart, "1\n")
        return {"auto_start_state": self.autostart_status()}

    def autostart_disable(self) -> dict[str, Any]:
        try:
            self.paths.autostart.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RuntimeError("Auto start preference could not be updated") from exc
        return {"auto_start_state": self.autostart_status()}

    def autostart_status(self) -> str:
        return "enabled" if self.paths.autostart.is_file() else "disabled"

    def _recovery_summary(self) -> dict[str, Any]:
        try:
            recoveries = read_json(self.paths.recovery, default={}).get("recoveries", {})
            cooldowns = read_json(self.paths.cooldowns, default={}).get("cooldowns", {})
        except PersistenceError:
            recoveries, cooldowns = {}, {}
        now = time.time()
        recovery_count = 0
        if isinstance(recoveries, Mapping):
            for value in recoveries.values():
                if not isinstance(value, Mapping):
                    continue
                heartbeat = value.get("heartbeat_at") or value.get("updated_at")
                try:
                    parsed_heartbeat = datetime.fromisoformat(str(heartbeat).replace("Z", "+00:00"))
                    if parsed_heartbeat.tzinfo is None:
                        parsed_heartbeat = parsed_heartbeat.replace(tzinfo=timezone.utc)
                    heartbeat_at = parsed_heartbeat.timestamp()
                except (TypeError, ValueError):
                    continue
                if now - heartbeat_at <= 45:
                    recovery_count += 1
        cooldown_count = 0
        if isinstance(cooldowns, Mapping):
            for value in cooldowns.values():
                if not isinstance(value, Mapping):
                    continue
                try:
                    cooldown_until = float(value.get("cooldown_until") or 0)
                except (TypeError, ValueError):
                    continue
                if cooldown_until > now:
                    cooldown_count += 1
        return {"summary": f"{recovery_count} recovering / {cooldown_count} cooldown", "recovering": recovery_count, "cooldown": cooldown_count}

    def _webdav_summary(self) -> dict[str, Any]:
        try:
            status = read_json(self.paths.webdav_status, default={})
        except PersistenceError:
            status = {}
        try:
            baseline = read_json(self.paths.webdav_sync_state, default={})
        except PersistenceError:
            baseline = {}
        checked_at = status.get("checked_at") if isinstance(status.get("checked_at"), str) else None
        action = status.get("action") if isinstance(status.get("action"), str) else None
        ok = status.get("ok") if isinstance(status.get("ok"), bool) else None
        if checked_at is None:
            checked_at = baseline.get("updated_at") if isinstance(baseline.get("updated_at"), str) else None
            action = baseline.get("action") if isinstance(baseline.get("action"), str) else action
            ok = True if checked_at is not None else ok
        return {
            "enabled": self.paths.webdav_enabled.is_file(),
            "ok": ok,
            "checked_at": _bounded_text(checked_at, limit=40) if checked_at else None,
            "action": _bounded_text(action, limit=80) if action else None,
        }


class ConfigurationPackageAdapter:
    """Adapt the established package format without exposing its contents."""

    def __init__(self, *, config_path: Path, settings_path: Path) -> None:
        self.config_path = config_path
        self.settings_path = settings_path

    @staticmethod
    def _legacy_sections(sections: Sequence[str]) -> tuple[str, ...]:
        selected = set(sections)
        valid = {"providers_models", "runtime"}
        if not selected or selected - valid:
            raise ValueError("Configuration packages support Providers & Models and Runtime Settings")
        translated: list[str] = []
        if "runtime" in selected:
            translated.append("runtime_settings")
        if "providers_models" in selected:
            translated.append("providers_models")
        return tuple(translated)

    def export(self, *, sections: Sequence[str], destination: Path) -> tuple[str, ...]:
        import configuration_package

        translated = self._legacy_sections(sections)
        return configuration_package.export_package(
            sections=translated,
            config_path=self.config_path if "providers_models" in translated else None,
            settings_file=self.settings_path if "runtime_settings" in translated else None,
            output_path=destination,
        )

    def load(self, source: Path) -> dict[str, Any]:
        import configuration_package

        return configuration_package.import_package(source)

    @staticmethod
    def core_sections(payload: Mapping[str, Any], selected: Sequence[str] | None) -> dict[str, object]:
        allowed = set(selected) if selected is not None else {"providers_models", "runtime"}
        result: dict[str, object] = {}
        if "providers_models" in allowed and isinstance(payload.get("providers_models"), Mapping):
            result["providers_models"] = dict(payload["providers_models"])
        if "runtime" in allowed and isinstance(payload.get("runtime_settings"), Mapping):
            result["runtime"] = dict(payload["runtime_settings"])
        if not result:
            raise ValueError("Configuration package does not contain the selected section")
        return result


class OnlineUsageReader:
    """Call the existing opt-in remote usage reader only on explicit refresh."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path

    def refresh(self) -> list[str]:
        import remote_usage_logs

        try:
            text = remote_usage_logs.render(self.config_path, 5.0)
        except Exception:
            return ["Online usage logs are unavailable."]
        lines = [REDACT_TEXT(line)[:512] for line in str(text).splitlines() if line.strip()]
        return lines[-MAX_USAGE_ROWS:] or ["No recent online usage rows."]


__all__ = [
    "ConfigurationPackageAdapter",
    "CoreServiceController",
    "OnlineUsageReader",
    "RuntimePaths",
]
