"""The Our Free Model lane: process lifecycle around the bundled upstream.

The upstream project (https://github.com/Ebony-Vinyl/dsh-our-free-model) owns the
request protocol, the multi-producer model roster, the routing, and the loopback
gateway.  Its public core exports are imported in place by
``our_free_model_worker.mjs``, and ``scripts/update_our_free_model.py`` stages
the verified release tree for every artifact build.

This adapter owns only the Core-side process lifecycle and the two environment
references the managed LiteLLM child resolves.
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
from collections.abc import Sequence
from typing import Any, Mapping

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..core.persistence import PersistenceError, read_json
from ..node_runtime import node_command as node_runtime_command


# The provider ids a saved route addresses this lane by, and the auth kind that
# says a provider row *is* this lane.  All of them are persisted identifiers.
#
# One provider per upstream channel, because a channel is not a label on a row:
# it decides which wire the row is reached over and which window it accepts, and
# a provider is the only unit this app pins a transport to.  Keeping one provider
# for every channel meant every route had to share one surface, and the rows
# that answered on another were reported as "no such protocol".
#
# Two channels are served here: the anonymous free lane and Kilo.  The upstream
# also carries a third, ``eac``, but that lane is unlocked from a sealed
# credential this bridge never holds -- the package builds its roster only when
# ``sealedCredential()`` answers, and the worker passes none -- so a provider for
# it could list nothing and adding it was refused.  It is not addressed here
# rather than offered as an empty entry.
OUR_FREE_MODEL_PROVIDER = "our-free-model"
OUR_FREE_MODEL_KILO_PROVIDER = "our-free-model-kilo"
OUR_FREE_MODEL_AUTH_KIND = "our_free_model_login"

#: Channel tag (as the upstream catalog states it) → the provider that serves it.
#: The anonymous free lane states no channel, which is the empty string.
CHANNEL_TO_PROVIDER = {
    "": OUR_FREE_MODEL_PROVIDER,
    "kilo": OUR_FREE_MODEL_KILO_PROVIDER,
}
PROVIDER_TO_CHANNEL = {provider: channel for channel, provider in CHANNEL_TO_PROVIDER.items()}
OUR_FREE_MODEL_PROVIDERS = tuple(CHANNEL_TO_PROVIDER.values())

#: Channels the upstream's catalog can tag that no provider here serves.  The
#: co-paid lane is one: it opens only from a sealed credential this bridge never
#: holds, so its rows can never be fetched, and importing them as the anonymous
#: lane's would stage a route whose every turn is refused.  A tag in here is
#: dropped from every roster rather than folded into another channel.
UNSERVED_CHANNELS = frozenset({"eac"})

#: Per-channel names, because one gateway URL serves both channels and a
#: provider row addresses its own.  The lane's original name is kept for the
#: anonymous channel so a document written before the split still resolves.
_CHANNEL_ENV_SUFFIX = {
    OUR_FREE_MODEL_PROVIDER: "",
    OUR_FREE_MODEL_KILO_PROVIDER: "KILO_",
}
API_BASE_ENV = {provider: f"YOUNG_ROUTER_OUR_FREE_MODEL_{suffix}BASE" for provider, suffix in _CHANNEL_ENV_SUFFIX.items()}
API_KEY_ENV = {provider: f"YOUNG_ROUTER_OUR_FREE_MODEL_{suffix}KEY" for provider, suffix in _CHANNEL_ENV_SUFFIX.items()}

#: Every provider row that belongs to this lane authenticates the same way: one
#: login reaches both channels, so the auth kind states "this is the lane",
#: and the provider id states which of its channels the row is.
AUTH_KIND_TO_PROVIDER = {OUR_FREE_MODEL_AUTH_KIND: OUR_FREE_MODEL_PROVIDER}
PROVIDER_TO_AUTH_KIND = {provider: OUR_FREE_MODEL_AUTH_KIND for provider in OUR_FREE_MODEL_PROVIDERS}
OUR_FREE_MODEL_AUTH_KINDS = (OUR_FREE_MODEL_AUTH_KIND,)
CATALOG_AUTH_KINDS = frozenset(OUR_FREE_MODEL_AUTH_KINDS)
CATALOG_AUTH_KIND_TO_PROVIDER = dict(AUTH_KIND_TO_PROVIDER)

OUR_FREE_MODEL_DISPLAY_NAME_BASE = "Our Free Model"
OUR_FREE_MODEL_DISPLAY_NAME = "Our Free Model"
#: One heading per channel, so the provider list itself states which channel
#: a row is served from instead of leaving it to a tag inside the roster.
# Every channel states which one it is, including the lane's own: entries named
# after the same product would be indistinguishable in the provider list, and
# "Our Free Model" alone would read as the whole product rather than the one
# anonymised channel inside it.
OUR_FREE_MODEL_DISPLAY_NAMES = {
    OUR_FREE_MODEL_PROVIDER: f"{OUR_FREE_MODEL_DISPLAY_NAME_BASE} · OpenCode",
    OUR_FREE_MODEL_KILO_PROVIDER: f"{OUR_FREE_MODEL_DISPLAY_NAME_BASE} · Kilo",
}
#: The staged upstream package, as its own manifest names it.
UPSTREAM_PACKAGE_NAME = "dsh-our-free-model"
API_KEY_NAME = "our-free-model-local"

#: The staged upstream tree, named for the package it holds.
_STAGED_DIRECTORY = "our-free-model"
_DIR_ENV = "YOUNG_ROUTER_OUR_FREE_MODEL_DIR"
_NODE_ENV = "YOUNG_ROUTER_OUR_FREE_MODEL_NODE"
_WORKER_ENV = "YOUNG_ROUTER_OUR_FREE_MODEL_WORKER"
_START_TIMEOUT_SECONDS = 45.0
_STATUS_TIMEOUT_SECONDS = 30.0
_MODELS_TIMEOUT_SECONDS = 90.0
_SOURCE_STATE = "sourceDir"


class OurFreeModelUnavailable(RuntimeError):
    """The staged free-model integration cannot serve."""


class OurFreeModelCatalogUnavailable(OurFreeModelUnavailable):
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
    """Where the build staged the upstream release.

    The directory is the upstream package's own name, hyphenated: a hyphenated
    directory cannot be imported, so it can never shadow ``our_free_model.py``
    the way a same-named package directory would.
    """

    configured = os.environ.get(_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).with_name(_STAGED_DIRECTORY)


def worker_path() -> Path:
    configured = os.environ.get(_WORKER_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).with_name("our_free_model_worker.mjs")


def node_command() -> str:
    return node_runtime_command(
        env_name=_NODE_ENV,
        label=OUR_FREE_MODEL_DISPLAY_NAME,
        unavailable=OurFreeModelUnavailable,
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
    configured = os.environ.get(_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    adjacent = worker_path().parent / _STAGED_DIRECTORY
    return adjacent if (adjacent / "package.json").is_file() else staged_root()


def api_base_reference(provider: str) -> str:
    """The ``os.environ/`` base reference one stored provider id resolves.

    A config from an older build names the legacy variable and the proxy child
    resolves references by the name the document carries, so this answers with
    the reference that matches the id it was given rather than always the new
    one.  A route is therefore still startable before its document is rewritten.
    """

    return f"os.environ/{API_BASE_ENV[validated_provider(provider)]}"


def api_key_reference(provider: str) -> str:
    return f"os.environ/{API_KEY_ENV[validated_provider(provider)]}"


def validated_provider(value: object) -> str:
    """One of this lane's provider ids, as stored, or a refusal.

    Every channel is its own provider, so every one of them is a valid answer.
    The answer is the spelling that was asked about, not a normalised one: every
    caller uses it to address a stored row or a published environment name, and
    both of those use the stored spelling.
    """

    provider = str(value or "").strip()
    if provider not in OUR_FREE_MODEL_PROVIDERS:
        raise ValueError("Unknown Our Free Model provider")
    return provider


def provider_for_channel(channel: object) -> str:
    """The provider that serves one upstream channel; the lane when unnamed.

    An empty string answers for a channel no provider here serves: the caller
    filters on a provider id, and no row is ever filed under one.
    """

    tag = str(channel or "").strip().casefold()
    if tag in UNSERVED_CHANNELS:
        return ""
    return CHANNEL_TO_PROVIDER.get(tag, OUR_FREE_MODEL_PROVIDER)


def provider_for_channel_tag(channel: object) -> str:
    """The provider a channel tag addresses; the lane when the tag names none.

    The tag is the stored identity, so this is the one place that maps it.  A tag
    this build does not know is refused rather than folded into the lane: it came
    from a document this code cannot describe, and guessing would route its rows
    over another channel's wire.
    """

    tag = str(channel or "").strip().casefold()
    try:
        return CHANNEL_TO_PROVIDER[tag]
    except KeyError:
        raise ValueError(f"Unknown Our Free Model channel: {tag}") from None


def _route_membership(value: object) -> dict[str, list[str]]:
    """The picker routes the upstream published, as id lists.

    The upstream decides which models this egress can use and which its
    regional gate refuses; this side only carries that answer.  An unreadable
    or absent answer is an empty mapping rather than a guess, so a roster with
    no verdicts shows one group instead of an invented second one.
    """

    if not isinstance(value, Mapping):
        return {}
    routes: dict[str, list[str]] = {}
    for route, ids in value.items():
        name = str(route or "").strip()
        if not name or not isinstance(ids, Sequence) or isinstance(ids, (str, bytes, bytearray)):
            continue
        resolved = [str(model_id) for model_id in ids if isinstance(model_id, str) and model_id.strip()]
        if resolved:
            routes[name] = resolved
    return routes


def _wire_membership(value: object) -> dict[str, str]:
    """The surface each row is served on, as the upstream routes it.

    An imported route used to be pinned to ``openai/responses`` on the grounds
    that the gateway serves both surfaces.  It does — but not for every row: the
    package routes all but a couple of ids to ``/inference/v1/chat/completions``,
    so a pinned Responses route asked an endpoint that row does not have and the
    probe answered "no such protocol".

    Only the two surface names this bridge can express are kept.  A row the
    upstream says nothing about is absent, and the caller then keeps its own
    default rather than reading a missing answer as Chat.
    """

    if not isinstance(value, Mapping):
        return {}
    wires: dict[str, str] = {}
    for model_id, wire in value.items():
        name = str(model_id or "").strip()
        surface = str(wire or "").strip()
        if not name or surface not in {"responses", "chat"}:
            continue
        wires[name] = surface
    return wires


def _channel_membership(value: object) -> dict[str, str]:
    """The producer each row belongs to, as the upstream catalog states it.

    The upstream serves several producers behind one gateway — the anonymous
    free lane and the Kilo channel on this bridge — and its catalog entries
    carry the producer in ``channel``.  The gateway's OpenAI-shaped ``/v1/models``
    cannot express it (every row answers ``owned_by: 'our-free-model'``), and an
    id cannot be used to recover it: ``big-pickle`` and ``kilo-auto/free`` are
    both just ids.  This carries the upstream's own answer instead of deriving
    one from a spelling.

    The anonymous lane has no channel name.  It is reported as the empty string
    rather than dropped, because "this row is on the lane that names no
    producer" is an answer, and dropping it would file those rows under whatever
    heading happened to be left.
    """

    if not isinstance(value, Mapping):
        return {}
    channels: dict[str, str] = {}
    for model_id, channel in value.items():
        name = str(model_id or "").strip()
        if not name:
            continue
        channels[name] = str(channel or "").strip()
    return channels


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _safe_problem(error: object) -> str:
    """One line from a worker failure, without request bodies or headers."""

    text = str(error or "").strip().splitlines()
    return text[0][:800] if text else f"{OUR_FREE_MODEL_DISPLAY_NAME} worker failed"


class OurFreeModelRuntime:
    """Own one worker serving the upstream's free-model runtime."""

    def __init__(
        self,
        runtime_root: Path | str | None = None,
        *,
        settings: Mapping[str, str] | None = None,
    ) -> None:
        base = Path(runtime_root) if runtime_root is not None else Path.home() / ".young-router"
        self.root = base / ".litellm-runtime" / OUR_FREE_MODEL_PROVIDER
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
                raise OurFreeModelUnavailable(f"The {OUR_FREE_MODEL_DISPLAY_NAME} worker is not running")
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
                raise OurFreeModelUnavailable(
                    f"The bundled {OUR_FREE_MODEL_DISPLAY_NAME} package is missing: {staged}"
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
                raise OurFreeModelUnavailable(
                    f"{OUR_FREE_MODEL_DISPLAY_NAME} worker could not start: {_safe_problem(exc)}"
                ) from exc
        self._ready_pipe = handoff
        self._worker_lock = worker_lock
        return process

    def _wait_ready(self, process: subprocess.Popen[bytes], port: int) -> None:
        handoff = self._ready_pipe
        if handoff is None:
            raise OurFreeModelUnavailable(f"The {OUR_FREE_MODEL_DISPLAY_NAME} worker hand-off is unavailable")
        deadline = time.monotonic() + _START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            # The worker creates its lease while the handoff remains held.
            worker_lock = self._worker_lock
            if worker_lock is None:
                raise OurFreeModelUnavailable(f"The {OUR_FREE_MODEL_DISPLAY_NAME} worker hand-off was lost")
            _unlock_handle(handoff)
            handoff.flush()
            if process.poll() is not None:
                raise OurFreeModelUnavailable(self._worker_failure(process, port))
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
            raise OurFreeModelUnavailable(f"The {OUR_FREE_MODEL_DISPLAY_NAME} worker did not start in time")
        ready_path = self.root / "online.json"
        if ready_path.stat().st_mode & 0o077:
            raise OurFreeModelUnavailable(f"The {OUR_FREE_MODEL_DISPLAY_NAME} readiness file is not private")
        ready = read_json(ready_path, default={})
        try:
            control_port = ready["controlPort"]
            gateway_port = ready["gatewayPort"]
            if type(control_port) is not int or type(gateway_port) is not int:
                raise TypeError
        except (KeyError, TypeError):
            raise OurFreeModelUnavailable(f"The {OUR_FREE_MODEL_DISPLAY_NAME} readiness file is invalid") from None
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
        # Two provider rows, one gateway: the channels are routing inside the
        # package, not separate hosts, so every row publishes the same address
        # and token under its own name.
        values: dict[str, str] = {}
        for provider in OUR_FREE_MODEL_PROVIDERS:
            values[API_BASE_ENV[provider]] = f"{base}/v1"
            values[API_KEY_ENV[provider]] = self._token
        return values

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
            timeout=_STATUS_TIMEOUT_SECONDS,
        )
        # One worker answers for the whole lane, so every channel row reports
        # the same version and lane state.  Stating it per row keeps every
        # provider in the list answering for itself instead of leaving the two
        # that are not the original row silent.
        state = "ready" if document.get("ok") is True else "failed"
        return {
            "providers": {
                provider: {
                    "provider": provider,
                    "state": state,
                    **{
                        key: document[key]
                        for key in ("version", "laneState")
                        if document.get(key) is not None
                    },
                }
                for provider in OUR_FREE_MODEL_PROVIDERS
            }
        }

    def provider_status(self, provider: str = OUR_FREE_MODEL_PROVIDER) -> dict[str, Any]:
        validated_provider(provider)
        return dict(self.status().get("providers", {}).get(provider, {}))

    def models(
        self,
        provider: str = OUR_FREE_MODEL_PROVIDER,
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
                timeout=_MODELS_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            raise OurFreeModelCatalogUnavailable("The free-model catalog is unavailable") from exc
        if refresh and not document.get("models"):
            document = self._request(
                "/control/models",
                timeout=_MODELS_TIMEOUT_SECONDS,
            )
        # One worker serves every channel, so its answer is the whole roster.
        # A caller that asked for one provider asked for that channel's slice:
        # returning all of it made the Kilo rows appear under the anonymous
        # channel's heading, because the picker groups by channel and had no
        # reason to doubt a roster it was handed for that channel.
        channel = PROVIDER_TO_CHANNEL[provider]
        channels = _channel_membership(document.get("channels"))
        models = [
            dict(entry)
            for entry in document.get("models") or []
            if isinstance(entry, Mapping)
            and provider_for_channel(channels.get(str(entry.get("id", "")).strip())) == provider
        ]
        return {
            "operation": "our_free_model_models",
            "provider": provider,
            "available": document.get("ok") is True,
            "source": "dsh-our-free-model",
            "models": models,
            # The upstream's own picker groups this roster into "what this
            # egress can use" and "what its regional gate refuses".  Those
            # routes travel as their membership lists rather than as a field on
            # each row, because a row belongs to exactly one of them and the
            # upstream decides which.
            "routes": _route_membership(document.get("routes")),
            # Which producer serves each row.  Separate from ``routes`` because
            # the two answer different questions: ``routes`` is the availability
            # split the picker groups by, this is the upstream the row is
            # actually served from.
            "channels": _channel_membership(document.get("channels")),
            # Which surface each row answers on.  Stated per row because the
            # upstream routes per row: a provider-level default cannot describe
            # a roster whose inner split is its own.
            "wires": _wire_membership(document.get("wires")),
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
            raise OurFreeModelUnavailable(f"The {OUR_FREE_MODEL_DISPLAY_NAME} worker is not running")
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
            raise OurFreeModelUnavailable(
                f"The {OUR_FREE_MODEL_DISPLAY_NAME} worker answered HTTP {exc.code}"
            ) from exc
        except (URLError, OSError, UnicodeDecodeError, ValueError) as exc:
            raise OurFreeModelUnavailable(_safe_problem(exc)) from exc
        if not isinstance(parsed, dict):
            raise OurFreeModelUnavailable(f"The {OUR_FREE_MODEL_DISPLAY_NAME} worker returned an invalid document")
        return parsed


def unsupported_status() -> dict[str, Any]:
    """Every channel row reports the same absence: one worker serves them all."""

    return {
        "providers": {
            provider: {"provider": provider, "state": "missing"}
            for provider in OUR_FREE_MODEL_PROVIDERS
        }
    }


__all__ = [
    "API_BASE_ENV",
    "API_KEY_ENV",
    "API_KEY_NAME",
    "AUTH_KIND_TO_PROVIDER",
    "CATALOG_AUTH_KINDS",
    "CATALOG_AUTH_KIND_TO_PROVIDER",
    "OUR_FREE_MODEL_AUTH_KIND",
    "OUR_FREE_MODEL_AUTH_KINDS",
    "CHANNEL_TO_PROVIDER",
    "OUR_FREE_MODEL_DISPLAY_NAME",
    "OUR_FREE_MODEL_DISPLAY_NAMES",
    "OUR_FREE_MODEL_KILO_PROVIDER",
    "OUR_FREE_MODEL_PROVIDER",
    "OUR_FREE_MODEL_PROVIDERS",
    "PROVIDER_TO_AUTH_KIND",
    "PROVIDER_TO_CHANNEL",
    "UNSERVED_CHANNELS",
    "provider_for_channel",
    "provider_for_channel_tag",
    "OurFreeModelRuntime",
    "OurFreeModelCatalogUnavailable",
    "OurFreeModelUnavailable",
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
