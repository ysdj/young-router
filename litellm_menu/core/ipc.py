"""Authenticated loopback IPC for the Python Core.

The native host starts this server and injects its endpoint into React Native.
Core binds an ephemeral loopback port by default; no UI code knows or assumes
the legacy LiteLLM API port.  A short-lived bootstrap credential is accepted
once at ``/v1/hello`` and exchanged for an in-memory session credential.

The transport is intentionally standard-library HTTP/JSON so the same Core
can be hosted by a macOS AppKit process or a Windows WinUI process without a
JavaScript backend.  The JSON envelope itself is validated by
``litellm_menu.core.protocol`` before any domain method is called.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hmac
import http.server
import json
import queue
import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import parse_qs, urlsplit
import urllib.error
import urllib.request
import uuid

from .protocol import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    ProtocolError,
    RequestEnvelope,
    ResponseEnvelope,
    decode_message,
    encode_message,
    make_request,
    validate_method_result,
)
from .security import safe_exception_message
from .service import ConfirmationNeeded, CoreError, CoreStore


BOOTSTRAP_TOKEN_TTL_SECONDS = 120.0
SESSION_TOKEN_TTL_SECONDS = 30 * 60.0
EVENT_POLL_TIMEOUT_SECONDS = 25.0
MAX_EVENTS = 32
EDITOR_CAPABILITY_TTL_SECONDS = 10 * 60.0
MAX_EDITOR_CAPABILITIES = 32
SECRET_CAPABILITY_TTL_SECONDS = 10 * 60.0
MAX_SECRET_CAPABILITIES = 32


class IPCError(RuntimeError):
    """An IPC transport error with no raw endpoint/body details."""


@dataclass(frozen=True)
class IpcEndpoint:
    kind: str
    address: str
    port: int
    one_time_auth: bool = True
    _bootstrap_token: str = field(default="", repr=False, compare=False)

    @property
    def url(self) -> str:
        return f"http://{self.address}:{self.port}/v1"

    @property
    def bootstrap_token(self) -> str:
        """The bootstrap token for the process that started this endpoint."""

        return self._bootstrap_token

    def to_mapping(self, *, include_bootstrap_token: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "address": self.address,
            "port": self.port,
            "one_time_auth": self.one_time_auth,
        }
        # Native launch code may need this in-memory descriptor, but callers
        # serializing a status snapshot must not accidentally print it.
        if include_bootstrap_token:
            payload["bootstrap_token"] = self._bootstrap_token
        return payload


@dataclass
class _Session:
    token: str
    expires_at: float
    subscriptions: set[str] = field(default_factory=set)


@dataclass
class _Subscription:
    subscription_id: str
    session_token: str
    queue: queue.Queue[dict[str, Any]] = field(default_factory=lambda: queue.Queue(maxsize=MAX_EVENTS))


@dataclass
class _EditorCapability:
    token: str
    session_token: str
    domain: str
    document: str
    revision: int
    expires_at: float
    read: bool = False
    staging: bool = False


@dataclass(frozen=True)
class _SecretCapability:
    token: str
    session_token: str
    domain: str
    field: str
    target: str | None
    revision: int
    expires_at: float


class _CoreRequestHandler(http.server.BaseHTTPRequestHandler):
    """Request handler bound to one ``CoreIPCServer`` instance."""

    server_version = "LiteLLMMenuCore/1"
    sys_version = ""

    @property
    def _owner(self) -> "CoreIPCServer":
        return self.server.core_ipc_owner  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: object) -> None:
        # HTTP's default stderr logger includes request paths and would make a
        # credential-bearing query/body easy to correlate.  The Core has no
        # request logger at this boundary.
        return

    def _authorization(self) -> str:
        value = self.headers.get("Authorization", "")
        if not value.startswith("Bearer "):
            return ""
        token = value[7:].strip()
        return token if len(token.encode("utf-8")) <= 512 else ""

    def _read_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            raise ProtocolError("invalid_message", "IPC request body is invalid") from None
        if length < 0 or length > MAX_MESSAGE_BYTES:
            raise ProtocolError("message_too_large", "IPC message exceeds the size limit")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ProtocolError("invalid_message", "IPC request body is incomplete")
        return body

    def _send(self, status: int, payload: Mapping[str, Any], *, session_token: str | None = None) -> None:
        encoded = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        if session_token:
            self.send_header("X-LiteLLM-Core-Session", session_token)
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except OSError:
            pass

    def _send_error(self, status: int, *, request_id: str = "invalid", code: str, message: str, retryable: bool = False) -> None:
        try:
            payload = ResponseEnvelope.failure(request_id, code, message, retryable=retryable).to_mapping()
        except ProtocolError:
            payload = {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": "invalid",
                "ok": False,
                "error": {"code": "ipc_error", "message": "Core IPC request failed", "retryable": False},
            }
        self._send(status, payload)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler hook
        route = urlsplit(self.path).path.rstrip("/") or "/"
        if route == "/v1/hello":
            self._owner._handle_hello(self)
            return
        if route == "/v1/host/shutdown":
            token = self._authorization()
            if not self._owner._valid_session(token):
                self._send_error(401, code="unauthorized", message="Core IPC authentication failed")
                return
            try:
                if self._read_body().strip() not in {b"", b"{}"}:
                    raise ProtocolError("invalid_request", "Host shutdown request is invalid")
                self._owner.core.shutdown()
            except (CoreError, ProtocolError) as exc:
                self._send_error(400, code=exc.code, message=exc.message, retryable=exc.code == "service_error")
                return
            except Exception:
                self._send_error(500, code="service_error", message="LiteLLM service could not stop", retryable=True)
                return
            self._send(200, {"protocol_version": PROTOCOL_VERSION, "stopped": True})
            return
        if route == "/v1/host/file-capability":
            token = self._authorization()
            if not self._owner._valid_session(token):
                self._send_error(401, code="unauthorized", message="Core IPC authentication failed")
                return
            try:
                data = decode_message(self._read_body())
                if set(data) != {"purpose", "path"}:
                    raise CoreError("invalid_file_capability", "The selected file is unavailable")
                purpose = data.get("purpose")
                path = data.get("path")
                if not isinstance(purpose, str) or not isinstance(path, str):
                    raise CoreError("invalid_file_capability", "The selected file is unavailable")
                if not path or len(path.encode("utf-8")) > 16_384 or "\x00" in path or "\r" in path or "\n" in path:
                    raise CoreError("invalid_file_capability", "The selected file is unavailable")
                capability = self._owner.register_file_capability(path, purpose)
            except (CoreError, ProtocolError) as exc:
                self._send_error(400, code=exc.code, message=exc.message)
                return
            except Exception:
                self._send_error(400, code="invalid_file_capability", message="The selected file is unavailable")
                return
            self._send(200, {"protocol_version": PROTOCOL_VERSION, "token": capability})
            return
        if route in {"/v1/host/secret/capability", "/v1/host/secret/stage"}:
            token = self._authorization()
            if not self._owner._valid_session(token):
                self._send_error(401, code="unauthorized", message="Core IPC authentication failed")
                return
            try:
                data = decode_message(self._read_body())
                if route.endswith("/capability"):
                    if not set(data).issubset({"domain", "field", "target", "purpose"}) or not {"domain", "field"}.issubset(data):
                        raise CoreError("invalid_secret", "The requested secret field is unavailable")
                    purpose = data.get("purpose", "settings")
                    if purpose != "settings":
                        raise CoreError("invalid_secret", "The requested secret field is unavailable")
                    result = self._owner.register_secret_capability(
                        data.get("domain"),
                        data.get("field"),
                        data.get("target"),
                        session_token=token,
                    )
                    self._send(200, {"protocol_version": PROTOCOL_VERSION, **result})
                else:
                    if set(data) == {"secret_token", "value"} and isinstance(data.get("value"), str):
                        value = data["value"]
                    elif set(data) == {"secret_token", "clear"} and data.get("clear") is True:
                        value = ""
                    else:
                        raise CoreError("invalid_secret", "The secret value is invalid")
                    result = self._owner.stage_secret_capability(
                        data.get("secret_token"),
                        value,
                        session_token=token,
                    )
                    self._send(200, {"protocol_version": PROTOCOL_VERSION, **result})
            except (CoreError, ProtocolError) as exc:
                self._send_error(400, code=exc.code, message=exc.message, retryable=exc.code == "revision_conflict")
            except Exception:
                self._send_error(400, code="invalid_secret", message="The requested secret field is unavailable")
            return
        if route in {"/v1/host/editor/read", "/v1/host/editor/stage"}:
            token = self._authorization()
            if not self._owner._valid_session(token):
                self._send_error(401, code="unauthorized", message="Core IPC authentication failed")
                return
            try:
                data = decode_message(self._read_body())
                if route.endswith("/read"):
                    if set(data) != {"editor_token"}:
                        raise CoreError("invalid_editor", "The requested editor is unavailable")
                    text = self._owner.read_editor_capability(
                        data.get("editor_token"), session_token=token
                    )
                    self._send(200, {"protocol_version": PROTOCOL_VERSION, "text": text})
                else:
                    if set(data) != {"editor_token", "text"} or not isinstance(data.get("text"), str):
                        raise CoreError("invalid_editor", "The editor document is invalid")
                    result = self._owner.stage_editor_capability(
                        data.get("editor_token"), data["text"], session_token=token
                    )
                    self._send(
                        200,
                        {
                            "protocol_version": PROTOCOL_VERSION,
                            "revision": result["revision"],
                            "editor_token": result["editor_token"],
                        },
                    )
            except (CoreError, ProtocolError) as exc:
                self._send_error(400, code=exc.code, message=exc.message, retryable=exc.code == "revision_conflict")
            except Exception:
                self._send_error(400, code="invalid_editor", message="The requested editor is unavailable")
            return
        if route != "/v1":
            self._send_error(404, code="not_found", message="Core IPC route is unavailable")
            return
        token = self._authorization()
        if not self._owner._valid_session(token):
            self._send_error(401, code="unauthorized", message="Core IPC authentication failed")
            return
        try:
            raw = self._read_body()
            data = decode_message(raw)
            request = RequestEnvelope.from_mapping(data)
        except ProtocolError as exc:
            request_id = "invalid"
            if isinstance(locals().get("data"), Mapping) and isinstance(data.get("request_id"), str):
                request_id = data["request_id"][:256]
            self._send_error(400, request_id=request_id, code=exc.code, message=exc.message)
            return
        response = self._owner.handle_request(request, session_token=token)
        self._send(200 if response.ok else 400, response.to_mapping())

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler hook
        route = urlsplit(self.path)
        if route.path.rstrip("/") != "/v1/events":
            self._send_error(404, code="not_found", message="Core IPC route is unavailable")
            return
        token = self._authorization()
        if not self._owner._valid_session(token):
            self._send_error(401, code="unauthorized", message="Core IPC authentication failed")
            return
        query = parse_qs(route.query, keep_blank_values=False)
        subscription_id = (query.get("subscription_id") or [""])[0]
        timeout_text = (query.get("timeout") or [str(EVENT_POLL_TIMEOUT_SECONDS)])[0]
        try:
            timeout = max(0.0, min(float(timeout_text), EVENT_POLL_TIMEOUT_SECONDS))
        except ValueError:
            timeout = EVENT_POLL_TIMEOUT_SECONDS
        event = self._owner.next_event(subscription_id, session_token=token, timeout=timeout)
        self._send(200, {"protocol_version": PROTOCOL_VERSION, "event": event})


class CoreIPCServer:
    """Serve one ``CoreStore`` on a random authenticated loopback port."""

    def __init__(
        self,
        core: CoreStore,
        *,
        address: str = "127.0.0.1",
        port: int = 0,
        bootstrap_ttl_seconds: float = BOOTSTRAP_TOKEN_TTL_SECONDS,
        session_ttl_seconds: float = SESSION_TOKEN_TTL_SECONDS,
    ) -> None:
        if address not in {"127.0.0.1", "::1", "localhost"}:
            raise IPCError("Core IPC must bind to a loopback address")
        if type(port) is not int or port < 0 or port > 65535:
            raise IPCError("Core IPC port is invalid")
        self.core = core
        self.address = address
        self.requested_port = port
        self.bootstrap_ttl_seconds = max(1.0, float(bootstrap_ttl_seconds))
        self.session_ttl_seconds = max(1.0, float(session_ttl_seconds))
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._core_unsubscribe: Callable[[], None] | None = None
        self._bootstrap_token = ""
        self._bootstrap_expires_at = 0.0
        self._bootstrap_consumed = False
        self._sessions: dict[str, _Session] = {}
        self._subscriptions: dict[str, _Subscription] = {}
        self._editor_capabilities: dict[str, _EditorCapability] = {}
        self._secret_capabilities: dict[str, _SecretCapability] = {}
        self._lock = threading.RLock()

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def port(self) -> int:
        server = self._server
        if server is None:
            return 0
        return int(server.server_address[1])

    @property
    def bootstrap_token(self) -> str:
        # Exposed only to the native process that owns this object.  It is
        # intentionally omitted from ``endpoint_descriptor`` by default.
        return self._bootstrap_token

    @property
    def endpoint(self) -> IpcEndpoint:
        return IpcEndpoint(
            kind="loopback",
            address=self.address,
            port=self.port,
            one_time_auth=True,
            _bootstrap_token=self._bootstrap_token,
        )

    def endpoint_descriptor(self, *, include_bootstrap_token: bool = False) -> dict[str, Any]:
        return self.endpoint.to_mapping(include_bootstrap_token=include_bootstrap_token)

    def register_file_capability(self, path: str, purpose: str) -> str:
        """Register a native file-panel selection without exposing its path to RN.

        This method is for the AppKit/WinUI host process, which receives the
        user-selected URL/path from a native panel and immediately exchanges
        it for an opaque Core capability.  React only ever sees the returned
        token and therefore cannot log or persist a private local path.
        """

        return self.core.file_capabilities.register(path, purpose)

    def register_editor_capability(self, domain: str, document: str, *, session_token: str) -> dict[str, Any]:
        descriptor = self.core.editor_descriptor(domain, document)
        token = secrets.token_urlsafe(32)
        capability = _EditorCapability(
            token=token,
            session_token=session_token,
            domain=str(descriptor["domain"]),
            document=str(descriptor["document"]),
            revision=int(descriptor["revision"]),
            expires_at=time.monotonic() + EDITOR_CAPABILITY_TTL_SECONDS,
        )
        with self._lock:
            now = time.monotonic()
            self._editor_capabilities = {
                key: item
                for key, item in self._editor_capabilities.items()
                if item.expires_at >= now and item.session_token in self._sessions
            }
            session_items = [
                item
                for item in self._editor_capabilities.values()
                if hmac.compare_digest(item.session_token, session_token)
            ]
            if len(session_items) >= MAX_EDITOR_CAPABILITIES:
                oldest = min(session_items, key=lambda item: item.expires_at)
                self._editor_capabilities.pop(oldest.token, None)
            self._editor_capabilities[token] = capability
        return {**descriptor, "editor_token": token}

    def register_secret_capability(
        self,
        domain: object,
        field: object,
        target: object | None,
        *,
        session_token: str,
    ) -> dict[str, Any]:
        if not isinstance(domain, str) or not isinstance(field, str):
            raise CoreError("invalid_secret", "The requested secret field is unavailable")
        descriptor = self.core.secret_descriptor(domain, field, target)
        token = secrets.token_urlsafe(32)
        capability = _SecretCapability(
            token=token,
            session_token=session_token,
            domain=str(descriptor["domain"]),
            field=str(descriptor["field"]),
            target=descriptor["target"] if isinstance(descriptor["target"], str) else None,
            revision=int(descriptor["revision"]),
            expires_at=time.monotonic() + SECRET_CAPABILITY_TTL_SECONDS,
        )
        with self._lock:
            now = time.monotonic()
            self._secret_capabilities = {
                key: item
                for key, item in self._secret_capabilities.items()
                if item.expires_at >= now and item.session_token in self._sessions
            }
            session_items = [
                item
                for item in self._secret_capabilities.values()
                if hmac.compare_digest(item.session_token, session_token)
            ]
            if len(session_items) >= MAX_SECRET_CAPABILITIES:
                oldest = min(session_items, key=lambda item: item.expires_at)
                self._secret_capabilities.pop(oldest.token, None)
            self._secret_capabilities[token] = capability
        return {
            "secret_token": token,
            "revision": capability.revision,
            "present": bool(descriptor["present"]),
        }

    def stage_secret_capability(self, token: object, value: str, *, session_token: str) -> dict[str, Any]:
        if not isinstance(token, str) or not token or len(token.encode("utf-8")) > 256:
            raise CoreError("invalid_secret", "The requested secret field is unavailable")
        with self._lock:
            capability = self._secret_capabilities.get(token)
            if capability is None or capability.expires_at < time.monotonic():
                self._secret_capabilities.pop(token, None)
                raise CoreError("invalid_secret", "The requested secret field is unavailable")
            if not hmac.compare_digest(capability.session_token, session_token):
                raise CoreError("invalid_secret", "The requested secret field is unavailable")
            # Secret capabilities are consumed before staging. A native dialog
            # must request a fresh capability after any conflict or failure.
            self._secret_capabilities.pop(token, None)
        return self.core.stage_secret(
            capability.domain,
            capability.field,
            capability.target,
            value,
            revision=capability.revision,
        )

    def _editor_capability(self, token: object, *, session_token: str) -> _EditorCapability:
        if not isinstance(token, str) or not token or len(token.encode("utf-8")) > 256:
            raise CoreError("invalid_editor", "The requested editor is unavailable")
        with self._lock:
            capability = self._editor_capabilities.get(token)
            if capability is None:
                raise CoreError("invalid_editor", "The requested editor is unavailable")
            if capability.expires_at < time.monotonic():
                self._editor_capabilities.pop(token, None)
                raise CoreError("invalid_editor", "The requested editor is unavailable")
            if not hmac.compare_digest(capability.session_token, session_token):
                # A different authenticated client cannot consume or revoke a
                # capability that belongs to the editor's original session.
                raise CoreError("invalid_editor", "The requested editor is unavailable")
            return capability

    def read_editor_capability(self, token: object, *, session_token: str) -> str:
        capability = self._editor_capability(token, session_token=session_token)
        text = self.core.trusted_editor_text(
            capability.domain,
            capability.document,
            revision=capability.revision,
        )
        with self._lock:
            current = self._editor_capabilities.get(capability.token)
            if current is capability:
                current.read = True
        return text

    def stage_editor_capability(self, token: object, text: str, *, session_token: str) -> dict[str, Any]:
        capability = self._editor_capability(token, session_token=session_token)
        with self._lock:
            if not capability.read or capability.staging:
                raise CoreError("invalid_editor", "The requested editor is unavailable")
            capability.staging = True
        try:
            result = self.core.stage_editor_text(
                capability.domain,
                capability.document,
                text,
                revision=capability.revision,
            )
        except Exception:
            with self._lock:
                current = self._editor_capabilities.get(capability.token)
                if current is capability:
                    current.staging = False
            raise
        replacement_token = secrets.token_urlsafe(32)
        replacement = _EditorCapability(
            token=replacement_token,
            session_token=capability.session_token,
            domain=capability.domain,
            document=capability.document,
            revision=int(result["revision"]),
            expires_at=time.monotonic() + EDITOR_CAPABILITY_TTL_SECONDS,
            # The native editor already owns the text it just staged. Requiring
            # it to read the same document again would reset selection/undo.
            read=True,
        )
        with self._lock:
            current = self._editor_capabilities.get(capability.token)
            if current is not capability:
                raise CoreError("invalid_editor", "The requested editor is unavailable")
            self._editor_capabilities.pop(capability.token, None)
            self._editor_capabilities[replacement_token] = replacement
            # A Codex window owns more than one raw document. Staging config
            # must not make the still-open auth editor stale merely because
            # both share the Core's global revision. Advance sibling
            # capabilities for the same authenticated session; their document
            # contents have not been read or copied by this operation.
            for sibling in self._editor_capabilities.values():
                if sibling is replacement:
                    continue
                if (
                    hmac.compare_digest(sibling.session_token, capability.session_token)
                    and sibling.domain == capability.domain
                    and sibling.document != capability.document
                    and not sibling.staging
                ):
                    sibling.revision = replacement.revision
        return {**result, "editor_token": replacement_token}

    def start(self) -> IpcEndpoint:
        with self._lock:
            if self._server is not None:
                return self.endpoint
            try:
                server = http.server.ThreadingHTTPServer((self.address, self.requested_port), _CoreRequestHandler)
            except OSError:
                raise IPCError("Core IPC could not bind its loopback endpoint") from None
            server.daemon_threads = True
            server.allow_reuse_address = True
            server.core_ipc_owner = self  # type: ignore[attr-defined]
            self._server = server
            self._bootstrap_token = secrets.token_urlsafe(32)
            self._bootstrap_expires_at = time.monotonic() + self.bootstrap_ttl_seconds
            self._bootstrap_consumed = False
            self._core_unsubscribe = self.core.subscribe(self._publish)
            self._thread = threading.Thread(target=server.serve_forever, name="litellm-core-ipc", daemon=True)
            self._thread.start()
            return self.endpoint

    def stop(self) -> None:
        with self._lock:
            server = self._server
            self._server = None
            unsubscribe = self._core_unsubscribe
            self._core_unsubscribe = None
            self._sessions.clear()
            self._subscriptions.clear()
            self._editor_capabilities.clear()
            self._secret_capabilities.clear()
            self._bootstrap_token = ""
            self._bootstrap_consumed = True
        if unsubscribe is not None:
            unsubscribe()
        if server is not None:
            server.shutdown()
            server.server_close()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def __enter__(self) -> "CoreIPCServer":
        self.start()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.stop()

    def _handle_hello(self, handler: _CoreRequestHandler) -> None:
        token = handler._authorization()
        with self._lock:
            valid = bool(
                token
                and not self._bootstrap_consumed
                and time.monotonic() <= self._bootstrap_expires_at
                and hmac.compare_digest(token, self._bootstrap_token)
            )
            if valid:
                self._bootstrap_consumed = True
                session_token = secrets.token_urlsafe(32)
                self._sessions[session_token] = _Session(
                    token=session_token,
                    expires_at=time.monotonic() + self.session_ttl_seconds,
                )
            else:
                session_token = ""
        if not valid:
            handler._send_error(401, code="unauthorized", message="Core IPC authentication failed")
            return
        handler._send(
            200,
            {"protocol_version": PROTOCOL_VERSION, "ok": True, "session": {"expires_in": int(self.session_ttl_seconds)}},
            session_token=session_token,
        )

    def _valid_session(self, token: str) -> bool:
        if not token:
            return False
        now = time.monotonic()
        with self._lock:
            session = self._sessions.get(token)
            if session is None or session.expires_at < now:
                self._sessions.pop(token, None)
                self._editor_capabilities = {
                    key: item
                    for key, item in self._editor_capabilities.items()
                    if not hmac.compare_digest(item.session_token, token)
                }
                self._secret_capabilities = {
                    key: item
                    for key, item in self._secret_capabilities.items()
                    if not hmac.compare_digest(item.session_token, token)
                }
                return False
            return hmac.compare_digest(session.token, token)

    def _publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subscriptions = tuple(self._subscriptions.values())
        for subscription in subscriptions:
            try:
                subscription.queue.put_nowait(event)
            except queue.Full:
                # Keep the newest snapshot; a slow UI can always call
                # ``snapshot`` after receiving the marker.
                try:
                    subscription.queue.get_nowait()
                    subscription.queue.put_nowait(event)
                except queue.Empty:
                    pass

    def _new_subscription(self, session_token: str) -> str:
        subscription_id = uuid.uuid4().hex
        with self._lock:
            subscription = _Subscription(subscription_id, session_token)
            self._subscriptions[subscription_id] = subscription
            session = self._sessions.get(session_token)
            if session is not None:
                session.subscriptions.add(subscription_id)
        return subscription_id

    def next_event(self, subscription_id: str, *, session_token: str, timeout: float) -> dict[str, Any] | None:
        with self._lock:
            subscription = self._subscriptions.get(subscription_id)
            if subscription is None or subscription.session_token != session_token:
                return None
        try:
            return subscription.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def handle_request(self, request: RequestEnvelope, *, session_token: str) -> ResponseEnvelope:
        if not self._valid_session(session_token):
            return ResponseEnvelope.failure(request.request_id, "unauthorized", "Core IPC authentication failed")
        try:
            params = request.params
            if request.method == "snapshot":
                result: Any = {"snapshot": self.core.snapshot()}
            elif request.method == "editor":
                domain = params.get("domain")
                document = params.get("document")
                if not isinstance(domain, str) or not isinstance(document, str):
                    raise CoreError("invalid_editor", "The requested editor is unavailable")
                result = self.register_editor_capability(
                    domain, document, session_token=session_token
                )
            elif request.method == "dispatch":
                action = params.get("action")
                expected = params.get("revision")
                if not isinstance(action, Mapping):
                    raise CoreError("invalid_action", "A Core action is required")
                self.core.reject_plaintext_secret_action(action)
                result = self.core.dispatch(action, expected_revision=expected if type(expected) is int else None)
            elif request.method == "subscribe":
                topics = params.get("topics")
                if topics is not None and (not isinstance(topics, Sequence) or isinstance(topics, (str, bytes, bytearray))):
                    raise CoreError("invalid_subscription", "Subscription topics are invalid")
                result = {"subscription_id": self._new_subscription(session_token)}
            elif request.method == "validate":
                domain = params.get("domain")
                if domain is not None and not isinstance(domain, str):
                    raise CoreError("invalid_domain", "A settings domain is required")
                revision = params.get("revision")
                result = {"validate": self.core.validate(domain, revision=revision if type(revision) is int else None)}
            elif request.method == "apply":
                domain = params.get("domain")
                domains = params.get("domains")
                if domain is not None and not isinstance(domain, str):
                    raise CoreError("invalid_domain", "A settings domain is required")
                if domains is not None and (
                    not isinstance(domains, Sequence)
                    or isinstance(domains, (str, bytes, bytearray))
                    or not all(isinstance(item, str) for item in domains)
                ):
                    raise CoreError("invalid_domain", "Settings domains are invalid")
                if domain is None and domains is None:
                    raise CoreError("invalid_domain", "A settings domain is required")
                revision = params.get("revision")
                confirmation = params.get("confirmation")
                result = self.core.apply(
                    domain,
                    domains=domains,
                    revision=revision if type(revision) is int else None,
                    confirmation=confirmation,
                )
                result = {"revision": result.get("revision", self.core.revision), "applied": True, "domains": result.get("domains", [domain] if domain is not None else list(domains or []))}
            elif request.method == "reload":
                domain = params.get("domain")
                if domain is not None and not isinstance(domain, str):
                    raise CoreError("invalid_domain", "A settings domain is required")
                revision = params.get("revision")
                result = self.core.reload(domain, revision=revision if type(revision) is int else None)
            elif request.method == "probe":
                domain = params.get("domain")
                result = self.core.probe(params, domain=domain if isinstance(domain, str) else None)
            elif request.method == "export":
                sections = params.get("sections")
                if not isinstance(sections, Sequence) or isinstance(sections, (str, bytes, bytearray)):
                    raise CoreError("invalid_sections", "Choose at least one configuration section")
                destination = params.get("destination_token")
                if destination is not None and not isinstance(destination, str):
                    raise CoreError("invalid_file_capability", "The selected file is unavailable")
                result = self.core.export(sections, destination_token=destination)
                result.pop("package", None)
            elif request.method == "import":
                source = params.get("source_token")
                if source is not None and not isinstance(source, str):
                    raise CoreError("invalid_file_capability", "The selected file is unavailable")
                sections = params.get("sections")
                if sections is not None and (not isinstance(sections, Sequence) or isinstance(sections, (str, bytes, bytearray))):
                    raise CoreError("invalid_sections", "Configuration sections are invalid")
                result = self.core.import_package(
                    source_token=source,
                    sections=sections,
                    revision=params["revision"],
                )
            else:  # defensive; RequestEnvelope already checks this.
                raise CoreError("unsupported_method", "Unsupported Core operation")
            validate_method_result(request.method, result)
            return ResponseEnvelope.success(request.request_id, result)
        except ConfirmationNeeded as exc:
            return ResponseEnvelope.failure(request.request_id, exc.code, exc.message)
        except CoreError as exc:
            return ResponseEnvelope.failure(request.request_id, exc.code, exc.message, retryable=exc.code in {"state_unavailable", "service_error"})
        except ProtocolError as exc:
            return ResponseEnvelope.failure(request.request_id, exc.code, exc.message, retryable=exc.retryable)
        except Exception:
            return ResponseEnvelope.failure(request.request_id, "core_error", "Core operation failed")


class CoreIPCClient:
    """Small Python client used by contract tests and native-host probes."""

    def __init__(self, endpoint: IpcEndpoint | Mapping[str, Any], bootstrap_token: str, *, timeout: float = 10.0):
        if isinstance(endpoint, IpcEndpoint):
            self.endpoint = endpoint
        elif isinstance(endpoint, Mapping):
            try:
                self.endpoint = IpcEndpoint(
                    kind=str(endpoint["kind"]),
                    address=str(endpoint["address"]),
                    port=int(endpoint["port"]),
                    one_time_auth=bool(endpoint.get("one_time_auth", True)),
                )
            except (KeyError, TypeError, ValueError):
                raise IPCError("Core IPC endpoint is invalid") from None
        else:
            raise IPCError("Core IPC endpoint is invalid")
        if self.endpoint.kind != "loopback" or self.endpoint.address not in {"127.0.0.1", "::1", "localhost"} or not 0 < self.endpoint.port <= 65535:
            raise IPCError("Core IPC endpoint is invalid")
        if not isinstance(bootstrap_token, str) or not bootstrap_token:
            raise IPCError("Core IPC authentication failed")
        self._bootstrap_token = bootstrap_token
        self._session_token = ""
        self._timeout = max(0.5, float(timeout))
        self._subscription_threads: list[tuple[threading.Event, threading.Thread]] = []

    def _url(self, path: str) -> str:
        return f"http://{self.endpoint.address}:{self.endpoint.port}{path}"

    def _http(self, path: str, *, payload: bytes | None = None, token: str) -> tuple[int, bytes, Mapping[str, str]]:
        request = urllib.request.Request(self._url(path), data=payload, method="POST" if payload is not None else "GET")
        request.add_header("Authorization", f"Bearer {token}")
        if payload is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return int(response.status), response.read(MAX_MESSAGE_BYTES + 1), dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            body = exc.read(MAX_MESSAGE_BYTES + 1)
            return int(exc.code), body, dict(exc.headers.items())
        except (urllib.error.URLError, TimeoutError, OSError):
            raise IPCError("Core IPC is unavailable") from None

    def _ensure_session(self) -> None:
        if self._session_token:
            return
        # An empty byte payload forces POST; ``None`` is reserved for GET
        # event polling in ``_http``.
        status, _body, headers = self._http("/v1/hello", payload=b"", token=self._bootstrap_token)
        if status != 200:
            raise IPCError("Core IPC authentication failed")
        session = headers.get("X-LiteLLM-Core-Session", "")
        if not session:
            raise IPCError("Core IPC authentication failed")
        self._session_token = session

    def call(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        self._ensure_session()
        request = make_request(method, params, request_id=uuid.uuid4().hex)
        status, body, _headers = self._http("/v1", payload=encode_message(request), token=self._session_token)
        try:
            response = ResponseEnvelope.from_mapping(decode_message(body))
        except ProtocolError:
            raise IPCError("Core IPC returned an invalid response") from None
        if response.request_id != request.request_id:
            raise IPCError("Core IPC returned an invalid response")
        if not response.ok:
            error = response.error or {}
            raise IPCError(str(error.get("message", "Core operation failed")))
        if status >= 400:
            raise IPCError("Core IPC request failed")
        return response.result

    def register_file_capability(self, path: str, purpose: str) -> str:
        """Exchange a native file-panel result for an opaque Core token."""

        self._ensure_session()
        body = encode_message({"purpose": purpose, "path": path})
        status, response_body, _headers = self._http(
            "/v1/host/file-capability",
            payload=body,
            token=self._session_token,
        )
        if status != 200:
            raise IPCError("The selected file is unavailable")
        try:
            response = decode_message(response_body)
        except ProtocolError:
            raise IPCError("Core IPC returned an invalid response") from None
        if set(response) != {"protocol_version", "token"} or response.get("protocol_version") != PROTOCOL_VERSION:
            raise IPCError("Core IPC returned an invalid response")
        capability = response.get("token")
        if not isinstance(capability, str) or not capability:
            raise IPCError("Core IPC returned an invalid response")
        return capability

    def read_editor(self, editor_token: str) -> str:
        """Exercise the trusted native-host editor read path."""

        self._ensure_session()
        status, body, _headers = self._http(
            "/v1/host/editor/read",
            payload=encode_message({"editor_token": editor_token}),
            token=self._session_token,
        )
        if status != 200:
            raise IPCError("The requested editor is unavailable")
        try:
            response = decode_message(body)
        except ProtocolError:
            raise IPCError("Core IPC returned an invalid response") from None
        if set(response) != {"protocol_version", "text"} or response.get("protocol_version") != PROTOCOL_VERSION:
            raise IPCError("Core IPC returned an invalid response")
        text = response.get("text")
        if not isinstance(text, str):
            raise IPCError("Core IPC returned an invalid response")
        return text

    def stage_editor(self, editor_token: str, text: str) -> int:
        """Exercise the trusted native-host editor stage path."""

        self._ensure_session()
        status, body, _headers = self._http(
            "/v1/host/editor/stage",
            payload=encode_message({"editor_token": editor_token, "text": text}),
            token=self._session_token,
        )
        if status != 200:
            raise IPCError("The editor document could not be staged")
        try:
            response = decode_message(body)
        except ProtocolError:
            raise IPCError("Core IPC returned an invalid response") from None
        if set(response) != {"protocol_version", "revision", "editor_token"} or response.get("protocol_version") != PROTOCOL_VERSION:
            raise IPCError("Core IPC returned an invalid response")
        revision = response.get("revision")
        replacement_token = response.get("editor_token")
        if (
            type(revision) is not int
            or revision < 0
            or not isinstance(replacement_token, str)
            or not replacement_token
        ):
            raise IPCError("Core IPC returned an invalid response")
        return revision

    def subscribe(self, callback: Callable[[dict[str, Any]], None], *, topics: Sequence[str] | None = None) -> Callable[[], None]:
        result = self.call("subscribe", {"topics": list(topics)} if topics is not None else {})
        if not isinstance(result, Mapping) or not isinstance(result.get("subscription_id"), str):
            raise IPCError("Core IPC returned an invalid subscription")
        subscription_id = str(result["subscription_id"])
        stop = threading.Event()

        def poll() -> None:
            while not stop.is_set():
                query = f"/v1/events?subscription_id={subscription_id}&timeout={EVENT_POLL_TIMEOUT_SECONDS}"
                try:
                    status, body, _headers = self._http(query, token=self._session_token)
                except IPCError:
                    # The native host can stop Core before its JS/Python
                    # subscription teardown runs. Treat that ordering as a
                    # normal disconnect and keep the poll cancellable.
                    if stop.wait(0.2):
                        return
                    continue
                if status != 200:
                    if stop.wait(0.2):
                        return
                    continue
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                event = payload.get("event") if isinstance(payload, Mapping) else None
                if isinstance(event, Mapping):
                    try:
                        callback(dict(event))
                    except Exception:
                        continue

        thread = threading.Thread(target=poll, name="litellm-core-events", daemon=True)
        self._subscription_threads.append((stop, thread))
        thread.start()

        def unsubscribe() -> None:
            stop.set()
            if thread is not threading.current_thread():
                thread.join(timeout=1.0)

        return unsubscribe

    def close(self) -> None:
        for stop, thread in tuple(self._subscription_threads):
            stop.set()
            if thread is not threading.current_thread():
                thread.join(timeout=1.0)
        self._subscription_threads.clear()


__all__ = [
    "BOOTSTRAP_TOKEN_TTL_SECONDS",
    "CoreIPCClient",
    "CoreIPCServer",
    "EVENT_POLL_TIMEOUT_SECONDS",
    "IPCError",
    "IpcEndpoint",
    "SESSION_TOKEN_TTL_SECONDS",
]
