"""Run the Core/IPC process used by the native desktop hosts.

The launcher writes a private endpoint descriptor for the host and then keeps
the in-process Core alive until SIGTERM/SIGINT.  The descriptor contains the
one-shot bootstrap credential only when it is explicitly requested by the
launcher; it is always written with mode ``0600`` and never logged.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import threading
from typing import Any

from .ipc import CoreIPCServer
from .persistence import AtomicJSONStore, PersistenceError
from .service import CoreStore


def _positive_pid(value: str) -> int:
    try:
        pid = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("parent PID must be a positive integer") from exc
    if pid <= 0:
        raise argparse.ArgumentTypeError("parent PID must be a positive integer")
    return pid


def _watch_parent(parent_pid: int, stop: threading.Event, *, poll_interval: float = 1.0) -> None:
    """Stop Core when its original native host is no longer its parent.

    One check per second keeps a killed host's Core from lingering while
    leaving the idle machine in its deep idle states; the host's own quit path
    signals this process directly, so the interval only bounds the fallback.
    """

    if os.name == "nt":
        _watch_windows_parent(parent_pid, stop, poll_interval=poll_interval)
        return

    while not stop.is_set():
        # Foundation launches Core directly. Checking the parent relationship
        # also avoids treating an unrelated process that reused the PID as the
        # original host.
        if os.getppid() != parent_pid:
            stop.set()
            return
        stop.wait(poll_interval)


def _watch_windows_parent(parent_pid: int, stop: threading.Event, *, poll_interval: float) -> None:
    """Hold the original Windows process object so PID reuse cannot fool us."""

    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    wait_timeout = 258
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    # MSIX full-trust hosts can deny PROCESS_SYNCHRONIZE to their child. The
    # launcher passes its inheritable process handle in this environment
    # value; OpenProcess remains the fallback for other Windows hosts.
    inherited = os.environ.get("YOUNG_ROUTER_PARENT_HANDLE", "").strip()
    try:
        handle = wintypes.HANDLE(int(inherited, 16)) if inherited else wintypes.HANDLE()
    except ValueError:
        handle = wintypes.HANDLE()
    if not handle:
        handle = kernel32.OpenProcess(synchronize, False, parent_pid)
    if not handle:
        stop.set()
        return
    try:
        wait_ms = max(1, int(poll_interval * 1000))
        while not stop.is_set():
            result = kernel32.WaitForSingleObject(handle, wait_ms)
            if result == wait_timeout:
                continue
            # WAIT_OBJECT_0 means the host exited; WAIT_FAILED and every
            # other result are equally unsafe to keep Core alive against.
            stop.set()
            return
    finally:
        kernel32.CloseHandle(handle)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Young Router Python Core IPC service")
    parser.add_argument("--metadata", type=Path, help="private Core metadata path")
    parser.add_argument("--config", type=Path, help="explicit provider configuration path")
    parser.add_argument("--endpoint-file", type=Path, help="private endpoint descriptor path")
    parser.add_argument("--parent-pid", type=_positive_pid, help="native host process identifier")
    # A machine with no window is still a machine this app runs on: a gateway
    # under systemd states that it supervises the proxy it publishes, and does
    # so explicitly rather than by inventing an endpoint descriptor a native
    # host would have written.
    parser.add_argument(
        "--manage-service",
        action="store_true",
        help="supervise this installation's proxy (a server host with no native app)",
    )
    parser.add_argument("--claude-settings", type=Path, help="explicit Claude settings path")
    parser.add_argument("--language-file", type=Path, help="explicit language preference path")
    parser.add_argument("--runtime-root", type=Path, help="private managed runtime root")
    parser.add_argument("--address", default="127.0.0.1", help="loopback address (never a public interface)")
    parser.add_argument("--port", type=int, default=0, help="port; zero chooses a random ephemeral port")
    parser.add_argument("--print-endpoint", action="store_true", help="print the private descriptor for a supervising host")
    # The browser host is the third way to drive this Core: the native hosts
    # read their React code from their own app bundle, this one reads it from
    # ``--web-host`` through the same listener.  It is off unless named, so no
    # native install starts serving pages because it upgraded.
    parser.add_argument("--web-host", type=Path, help="serve this built browser bundle at /ui on the local port")
    parser.add_argument("--web-url-file", type=Path, help="rewrite this file with the browser host's address")
    parser.add_argument("--web-password", help="web host password; omit to use the one in the configuration")
    # A terminal is already the proof: whoever runs this command on this machine
    # is the machine's owner.  The address this process prints therefore signs
    # itself in, and every other browser on the port still meets the password.
    parser.add_argument(
        "--web-require-password",
        action="store_true",
        help="make even this run's printed address ask for the web password",
    )
    return parser


def _packaged_web_host() -> Path | None:
    """The browser bundle shipped beside Core, when this install has one.

    ``Core/web`` is where the build puts it: the page is part of the product,
    not an optional download, so a person who never typed ``--web-host`` still
    finds it at the local port.  A Core run from a working tree has no such
    directory and serves no page.
    """

    from ..node_runtime import core_root

    candidate = core_root() / "web"
    return candidate if (candidate / "index.html").is_file() else None


def _publish_web_url(path: Path | None, url: str) -> None:
    """Keep one small file holding the address a person should open.

    The URL carries a credential in its fragment, so the file is written
    0600 like the endpoint descriptor beside it and never logged.
    """

    if path is None or not url:
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(url + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _write_endpoint(path: Path, server: CoreIPCServer) -> None:
    descriptor = server.endpoint_descriptor(include_bootstrap_token=True)
    # AtomicJSONStore rejects symlink targets and applies 0600 to both the
    # temporary file and final descriptor.
    AtomicJSONStore(path).write(descriptor)


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Windows validates the parent by opening the supplied process object in
    # the watchdog. Python's getppid() is not the durable identity there.
    if args.parent_pid is not None and os.name != "nt" and os.getppid() != args.parent_pid:
        return 0
    stop = threading.Event()
    parent_watchdog: threading.Thread | None = None
    if args.parent_pid is not None:
        parent_watchdog = threading.Thread(
            target=_watch_parent,
            args=(args.parent_pid, stop),
            name="litellm-core-parent-watchdog",
            daemon=True,
        )
        parent_watchdog.start()
    core = CoreStore.with_default_domains(
        metadata_path=args.metadata,
        config_path=args.config,
        claude_settings_path=args.claude_settings,
        language_path=args.language_file,
        runtime_root=args.runtime_root,
        reset_transient_routing_state=True,
    )
    server = CoreIPCServer(core, address=args.address, port=args.port)
    # The interval loop lives beside the IPC server: it reads the saved switch
    # and interval, so a machine nobody is looking at still syncs.
    webdav_scheduler = core.webdav_scheduler()
    # The generated client model lists follow the router with no window open:
    # a provider Apply regenerates them through the proxy reload, and this loop
    # is the path for a change that owed no reload.
    # Optional: a store that predates this loop (or a test double) simply runs
    # without it, because the lists are bookkeeping rather than a service.
    client_model_sync = getattr(core, "client_model_sync", None)
    client_model_sync = client_model_sync() if callable(client_model_sync) else None
    # The image-context cache is swept after every write; this loop is the
    # path that still runs when the pass is off and no write arrives.
    image_context_sweeper = core.image_context_sweeper()
    # The proxy follows the app, not the other way round: a window that is not
    # open, a background launch, and the gap after a failed start are all served
    # by this loop, so no 不健康/已停止 state is left waiting for a click.  Only a
    # Core a native host launched -- or a server unit that said so with
    # ``--manage-service`` -- owns that promise; a bare CLI Core (no endpoint,
    # no parent, no flag) is a diagnostic process and must not spawn a proxy.
    manages_service = (
        args.manage_service or args.endpoint_file is not None or args.parent_pid is not None
    )
    service_supervisor = core.service_supervisor() if manages_service else None

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        if stop.is_set():
            return 0
        server.start()
        # The page a person opens is the browser host's built bundle.  An
        # explicit ``--web-host`` states one; otherwise the copy packaged
        # beside Core is the page, so an install that shipped it serves it
        # without a launcher flag it would have to keep in step.
        web_root = args.web_host or _packaged_web_host()
        if web_root is not None:
            # The page is answered by this listener and published on the
            # proxy's own port, where `/ui` and `/ipc` are forwarded back here
            # so the browser keeps one origin.  Core's child cannot discover an
            # ephemeral port, so it is written down before anything launches
            # that child -- a supervisor pass or a window's own start.  A Core
            # with no page states no endpoint, because forwarding a prefix
            # nothing answers would put Core's settings API on the model port
            # for no reason.
            announce = getattr(core, "announce_web_endpoint", None)
            if callable(announce):
                announce(server.address, server.port)
            # The key is the Runtime document's, so a restart reopens the same
            # page behind the same key instead of minting a secret nobody was
            # told.  A document that states none gets one generated and
            # written -- that is the default the General pane then shows.
            runtime = core.settings_domain("runtime")
            password = args.web_password or ""
            provider = None
            if runtime is not None and callable(getattr(runtime, "ensure_web_password", None)):
                password = password or runtime.ensure_web_password()
                # A key stated on this command line is the gate: the caller
                # chose it, and a document read per sign-in would quietly
                # replace it -- the process then tells a terminal its address
                # is open while the document's key is the one that answers.
                # A launch that stated none follows the document, so an edit
                # in General reaches the next sign-in without a restart.
                provider = None if args.web_password else runtime.web_password
            # The gate speaks the language the window speaks, and reads it per
            # request for the same reason the key is read per sign-in: an edit
            # in Language changes the form without replacing the listener.
            language = core.settings_domain("language")

            def locale() -> str:
                try:
                    return str(language.snapshot().get("resolved") or "en") if language is not None else "en"
                except Exception:
                    return "en"

            server.enable_web_host(
                web_root,
                password=password,
                password_provider=provider,
                locale_provider=locale,
                auto_login=not args.web_require_password,
            )
            # Printed once at start and then again whenever the URL file is
            # missing, so a person can always find the address without reading
            # a log.
            _publish_web_url(args.web_url_file, server.web_host_url)
            print(server.web_host_url, flush=True)
        webdav_scheduler.start()
        if client_model_sync is not None:
            client_model_sync.start()
        image_context_sweeper.start()
        if service_supervisor is not None:
            service_supervisor.start()
        if args.endpoint_file is not None:
            _write_endpoint(args.endpoint_file, server)
        if args.print_endpoint:
            # This is intended for a supervising native process.  It is the
            # only CLI mode that emits the descriptor; regular logs contain
            # no endpoint, token, or user path.
            import json

            print(json.dumps(server.endpoint_descriptor(include_bootstrap_token=True), separators=(",", ":")))
        stop.wait()
        return 0
    except (PersistenceError, OSError):
        return 1
    finally:
        stop.set()
        if parent_watchdog is not None and parent_watchdog is not threading.current_thread():
            parent_watchdog.join(timeout=1.0)
        try:
            webdav_scheduler.stop()
        except Exception:
            pass
        try:
            image_context_sweeper.stop()
        except Exception:
            pass
        try:
            # SIGTERM is how a host or a script *replaces* Core; the app
            # quitting on its own goes through ``/v1/host/shutdown``.  The stop
            # is the same one, so the record says which it was: a reader that
            # finds ``stopped`` needs to know whether a person asked for it.
            core.shutdown(stopped_by="core_replaced")
        except Exception:
            # The owner-aware controller refuses unrelated processes. A failed
            # best-effort signal cleanup must not expose local diagnostics.
            pass
        server.stop()
        if args.endpoint_file is not None:
            try:
                args.endpoint_file.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(run())
