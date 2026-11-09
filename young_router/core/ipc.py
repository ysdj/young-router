"""Authenticated loopback IPC for the Python Core.

The native host starts this server and injects its endpoint into React Native.
Core binds an ephemeral loopback port by default; no UI code knows or assumes
the proxy API port. A short-lived bootstrap credential is accepted
once at ``/ipc/hello`` and exchanged for an in-memory session credential. A
current session can renew itself before it expires without restarting Core.

The transport is intentionally standard-library HTTP/JSON so the same Core
can be hosted by a macOS AppKit process or a Windows WinUI process without a
JavaScript backend.  The JSON envelope itself is validated by
``young_router.core.protocol`` before any domain method is called.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import base64
import binascii
import hashlib
import hmac
import html
import http.server
import json
from pathlib import Path
import queue
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import parse_qs, urlsplit
import urllib.error
import urllib.request
import uuid

from .persistence import PersistenceError, atomic_write_json, read_json
from .protocol import (
    MAX_EDITOR_DOCUMENT_BYTES,
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    ProtocolError,
    RequestEnvelope,
    ResponseEnvelope,
    decode_message,
    encode_message,
    is_error_code,
    make_request,
    validate_method_result,
)
from .protocol import _encode_json
from .security import safe_error_message
from .service import ConfirmationNeeded, CoreError, CoreStore, PreparedImport, RevisionConflict


def _failure_response(request_id: str, code: object, message: object, *, retryable: bool = False) -> ResponseEnvelope:
    """Answer with a failure the contract accepts, keeping the cause it can.

    The value a domain hands here is the domain's own: a WebDAV sync raises
    with the remote server's HTTP status in ``code`` (an ``int``) and a
    sentence that can hold a newline.  The envelope refuses both, and a
    refusal raised *inside* an ``except`` block escapes ``do_POST``: the
    handler writes no reply at all, so the peer reads a closed socket as "the
    local Core is unavailable" while Core is answering everything else.  Only
    the value this wire cannot carry is replaced -- a code that is already a
    token survives -- so the reason still reaches the reader.
    """

    try:
        return ResponseEnvelope.failure(request_id, code, message, retryable=retryable)
    except ProtocolError:
        return ResponseEnvelope.failure(
            request_id,
            code if is_error_code(code) else "core_error",
            safe_error_message(message),
            retryable=retryable,
        )


def _encode_response_body(payload: Mapping[str, Any]) -> bytes:
    """Serialize one HTTP response body with the contract's own codec.

    Every snapshot this Core publishes to a window leaves through ``_send``,
    carrying six figures of JSON, and it used to be serialized here by a
    second, private copy of the encoder.  Routing it through the contract's
    encoder keeps one JSON implementation (so the fast codec, the non-finite
    refusal, and the size accounting cannot drift apart between the two
    directions) and removes the redundant ``dict()`` copy of a payload that is
    already a mapping.
    """

    return _encode_json(payload)


BOOTSTRAP_TOKEN_TTL_SECONDS = 120.0
SESSION_TOKEN_TTL_SECONDS = 30 * 60.0
#: How long a browser's page stays signed in.
#:
#: A native host renews a 30-minute lease from a process that is already
#: running.  A page is not that: the browser it lives in gets closed and
#: reopened, the tab is reloaded, and the Core it talks to is replaced by a
#: deploy or by the app's own restart -- and every one of those used to end
#: with the password form in front of a person who had already typed the key.
#: Thirty days is the lease the cookie states and the lease that is recorded on
#: disk, so neither the browser closing nor Core being replaced is a sign-out.
WEB_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60.0
#: What the cookie's ``Max-Age`` says, in seconds: the same 30 days.
WEB_SESSION_COOKIE_MAX_AGE_SECONDS = int(WEB_SESSION_TTL_SECONDS)
#: The file that remembers issued browser sessions across a Core replacement.
#: It holds digests, never tokens: a reader of the file cannot mint the cookie
#: it names.
WEB_SESSION_STORE_FILENAME = "web-sessions.json"
WEB_SESSION_STORE_VERSION = 1
#: How many browser sessions one installation remembers.  This is one person's
#: browser tabs, not a fleet of clients: an unbounded list would only grow.
MAX_PERSISTED_WEB_SESSIONS = 32
#: How far a live session's recorded expiry is moved forward at most this
#: often.  Every authenticated call renews the lease, and a rewrite of the file
#: on every 25-second event poll would be disk chatter nothing reads.
WEB_SESSION_PERSIST_INTERVAL_SECONDS = 60 * 60.0
# The browser host's session travels in this cookie rather than a readable
# header: page script can neither read ``X-LiteLLM-Core-Session`` nor be given
# a session it could exfiltrate.  ``SameSite=Strict`` also keeps it out of every
# cross-site request, which is what makes DNS rebinding a non-issue here.
BROWSER_SESSION_COOKIE = "yr-core-session"
# The page the browser host loads is served by Core itself, so the browser's
# origin is this listener: ``/ipc`` is same-origin and no CORS answer, preflight,
# or second port is involved.  Only these names are answered.
WEB_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "[::1]"})
#: The same name the proxy forwards in (`proxy/web_forward.py`), so this module
#: does not import the proxy to read one header string.
PUBLISHED_HOST_HEADER = "X-Young-Router-Page-Host"
# Where the page lives on the published local port.  The proxy owns that
# listener and forwards this prefix to Core, which lets the browser keep one
# origin: `/ui` is the page, `/ipc` is its API, and neither needs a CORS
# answer.  The spelling is one constant because the page's own bundle refers
# to its script by relative path -- moving the prefix moves every asset too.
WEB_UI_PREFIX = "/ui"
# The one document the shell frames: the raw editor's own page. See
# `serve_web_host` for why it must be a document of its own.
WEB_EDITOR_PAGE = "editor.html"
#: Transport encodings the page's text assets ship beside themselves, best
#: first.  The builder writes one sibling per codec (`bundle.<hash>.js.br`),
#: because the alternative -- deflating 1.2 MB of JavaScript inside this
#: listener -- spends a worker thread on the first page load of every browser,
#: and a build already has the bytes and a deterministic answer.  A sibling
#: cannot drift from its source: the script's name is its own digest, and the
#: page names the editor's script by its own.
WEB_ENCODINGS = (("br", ".br"), ("gzip", ".gz"))
# The browser host is one built bundle of static files: the page, its script,
# and whatever the script imports.  Anything else the listener would have to
# guess, so it answers 404 instead.
_WEB_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
    ".ico": "image/x-icon",
}


def _accepts_encoding(header: str, codec: str) -> bool:
    """Whether an ``Accept-Encoding`` header admits ``codec``.

    q values are honoured -- ``br;q=0`` is a refusal, not an absence -- and
    ``*`` stands for every codec the client did not name.  A header that states
    nothing is HTTP's default of identity only, so the file itself is answered,
    which every browser reads.
    """

    stated: dict[str, float] = {}
    for part in header.split(","):
        token, _separator, parameters = part.partition(";")
        token = token.strip().lower()
        if not token:
            continue
        quality = 1.0
        parameters = parameters.strip()
        if parameters[:2].lower() == "q=":
            try:
                quality = float(parameters[2:])
            except ValueError:
                continue
        stated[token] = quality
    if codec in stated:
        return stated[codec] > 0
    return stated.get("*", 0.0) > 0


def _web_asset(candidate: Path, accept_encoding: str) -> tuple[str, bytes, bool]:
    """One page asset, in the encoding the browser can read.

    Returns the codec that was used (empty for the file itself), its bytes, and
    whether any precompressed sibling exists.  The last value is what makes this
    answer depend on ``Accept-Encoding``, so it is also what decides whether
    ``Vary`` has to be stated.

    The list is ordered best-first and that order decides, because both
    encodings decode to the same asset: the client's job is to say what it
    accepts, and a stated refusal (``q=0``) is the one thing that rules a codec
    out.  Weighing q values instead would hand a browser the larger copy over a
    preference it expressed about a courier.
    """

    varied = False
    for codec, suffix in WEB_ENCODINGS:
        sibling = candidate.with_name(candidate.name + suffix)
        if not sibling.is_file():
            continue
        varied = True
        if not _accepts_encoding(accept_encoding, codec):
            continue
        try:
            return codec, sibling.read_bytes(), True
        except OSError:
            continue
    try:
        return "", candidate.read_bytes(), varied
    except OSError:
        return "", b"", varied

# The gate a browser meets before the app itself is served.
#
# One self-contained document: the app's bundle is not served to a page that
# has not signed in, so the gate may not depend on it.  The script is inline and
# posts to `/ipc/web/login`, which answers with the session cookie the app page
# then carries.
#
# Two rules about its words.  It names nothing: anyone who reaches the port
# reads this page before proving anything, so it says what a visitor has to do
# and never what the service is.  And it speaks the language the app resolved,
# so a window set to Chinese does not hand the browser an English form.
#
# `autocomplete="current-password"` lets a password manager fill it, which is
# the whole reason a person can live with a key here at all.
WEB_LOGIN_MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "lang": "en",
        "title": "Sign in",
        "intro": "Enter the access key to continue.",
        "key_label": "Access key",
        "submit": "Sign in",
        "invalid_key": "That key is not correct.",
        "unchecked_key": "The key could not be checked.",
        "unreachable": "The service is not answering.",
        "invalid_launch": "This sign-in link has expired. Enter the key instead.",
    },
    "zh-Hans": {
        "lang": "zh-Hans",
        "title": "登录",
        "intro": "输入访问密钥以继续。",
        "key_label": "访问密钥",
        "submit": "登录",
        "invalid_key": "密钥不正确。",
        "unchecked_key": "无法校验密钥。",
        "unreachable": "服务没有响应。",
        "invalid_launch": "此登录链接已失效，请手动输入密钥。",
    },
}

_WEB_LOGIN_TEMPLATE = """<!doctype html>
<html lang="__LANG__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<!-- No icon: the browser then asks this origin for no /favicon.ico,
     which is not a file this page owns and would be answered 404. -->
<link rel="icon" href="data:,">
<title>__TITLE__</title>
<style>
  :root { color-scheme: light dark; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center;
    justify-content: center; background: #f5f5f7; color: #1d1d1f;
    font: 13px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  }
  @media (prefers-color-scheme: dark) {
    body { background: #1e1e1e; color: #f2f2f7; }
    form { background: #2a2a2c !important; border-color: #3a3a3c !important; }
    input { background: #1e1e1e !important; color: #f2f2f7 !important; border-color: #48484a !important; }
  }
  form {
    width: 300px; padding: 28px 26px; background: #fff; border: 1px solid #d8d8dc;
    border-radius: 10px; box-shadow: 0 8px 30px rgba(0,0,0,.10);
  }
  h1 { margin: 0 0 4px; font-size: 15px; font-weight: 600; }
  p { margin: 0 0 18px; color: #8e8e93; font-size: 12px; }
  input {
    width: 100%; box-sizing: border-box; padding: 7px 9px; font: inherit;
    border: 1px solid #c8c8cc; border-radius: 6px; background: #fff; color: inherit;
  }
  input:focus { outline: 2px solid #0a84ff; outline-offset: -1px; border-color: transparent; }
  button {
    width: 100%; margin-top: 14px; padding: 7px 9px; font: inherit; font-weight: 500;
    color: #fff; background: #0a84ff; border: 0; border-radius: 6px; cursor: pointer;
  }
  button:disabled { opacity: .5; cursor: default; }
  #error { margin-top: 10px; min-height: 16px; color: #ff3b30; font-size: 12px; }
</style>
</head>
<body>
<form id="login">
  <h1>__TITLE__</h1>
  <p>__INTRO__</p>
  <input id="password" type="password" name="password" autocomplete="current-password"
         autofocus aria-label="__KEY_LABEL__" required>
  <button id="submit" type="submit">__SUBMIT__</button>
  <div id="error" role="alert"></div>
</form>
<script>
  var strings = __STRINGS__;
  var form = document.getElementById("login");
  var field = document.getElementById("password");
  var button = document.getElementById("submit");
  var error = document.getElementById("error");
  /**
   * A terminal launch prints an address carrying this process's launch token.
   * Opening that address is the proof, so the form is never shown: the page
   * signs itself in and then takes the token out of the address bar.
   *
   * Any other visitor reaches this page without one, and gets the form.
   */
  var launch = /(?:^|[#&])launch=([^&]+)/.exec(location.hash || "");
  if (launch) {
    button.disabled = true;
    fetch("/ipc/web/launch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ launch: decodeURIComponent(launch[1]) }),
      credentials: "same-origin"
    }).then(function (response) {
      if (response.ok) {
        // The token has done its work and must not stay in this tab's history.
        var address = new URL(location.href);
        address.hash = "";
        history.replaceState(null, "", address.toString());
        location.reload();
        return;
      }
      button.disabled = false;
      error.textContent = strings.invalid_launch;
    }).catch(function () {
      button.disabled = false;
      error.textContent = strings.unreachable;
    });
  }
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (field.value === "") return;
    button.disabled = true;
    error.textContent = "";
    fetch("/ipc/web/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: field.value }),
      // The cookie this answer sets is the session the app page then uses.
      credentials: "same-origin"
    }).then(function (response) {
      if (response.ok) {
        // Reload rather than swap in the app: the shell is served by the same
        // route, so asking again now answers with it.
        location.reload();
        return;
      }
      return response.json().catch(function () { return {}; }).then(function (body) {
        button.disabled = false;
        field.select();
        error.textContent = body && body.error && body.error.code === "invalid_password"
          ? strings.invalid_key
          : strings.unchecked_key;
      });
    }).catch(function () {
      button.disabled = false;
      error.textContent = strings.unreachable;
    });
  });
</script>
</body>
</html>
"""


def _accepted_language(header: str) -> str:
    """The app's language from a browser's ``Accept-Language``.

    Read only when the preference is 自动, and read as the browser states it:
    the page follows ``navigator.languages``, and the gate is the same document
    from the same reader's point of view.  Tags are ranked by their own ``q``
    values, because a header's order is not its preference; the first tag this
    app is translated into wins, and a browser that asks for neither language
    the app speaks (or for ``*``) answers ``""`` so the caller can fall back to
    the machine's own resolution.
    """

    best = ""
    best_quality = 0.0
    for entry in header.split(","):
        parts = entry.split(";")
        tag = parts[0].strip().lower().replace("_", "-")
        if not tag or tag == "*":
            continue
        quality = 1.0
        for parameter in parts[1:]:
            name, _, value = parameter.partition("=")
            if name.strip().lower() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        if quality <= 0 or quality <= best_quality:
            continue
        supported = "zh-Hans" if tag == "zh" or tag.startswith("zh-") else "en" if tag == "en" or tag.startswith("en-") else ""
        if not supported:
            continue
        best = supported
        best_quality = quality
    return best


def _web_login_page(language: str) -> bytes:
    """The gate in one language, from one table of words.

    The language is the app's own resolution, so the form is in the language
    the window is already speaking instead of the language this file happens
    to be written in.
    """

    messages = WEB_LOGIN_MESSAGES.get(language, WEB_LOGIN_MESSAGES["en"])
    page = _WEB_LOGIN_TEMPLATE
    for name, value in messages.items():
        page = page.replace(f"__{name.upper()}__", html.escape(value, quote=True))
    page = page.replace(
        "__STRINGS__",
        json.dumps(
            {
                key: messages[key]
                for key in ("invalid_key", "unchecked_key", "unreachable", "invalid_launch")
            },
            ensure_ascii=False,
        ),
    )
    return page.encode("utf-8")


EVENT_POLL_TIMEOUT_SECONDS = 25.0
MAX_EVENTS = 32
EDITOR_CAPABILITY_TTL_SECONDS = 10 * 60.0
MAX_EDITOR_CAPABILITIES = 32
SECRET_CAPABILITY_TTL_SECONDS = 10 * 60.0
MAX_SECRET_CAPABILITIES = 32
# A provider API key may be rendered as plaintext in the native provider
# editor.  Its read lease is intentionally much shorter than a write lease.
SECRET_READ_CAPABILITY_TTL_SECONDS = 30.0
MAX_SECRET_READ_CAPABILITIES = 32
IMPORT_PLAN_TTL_SECONDS = 10 * 60.0
MAX_IMPORT_PLANS = 16
# ``socketserver.BaseServer.serve_forever`` otherwise checks its shutdown
# request only every 0.5 seconds. This is the Core's final local teardown
# step after the managed proxy has stopped, so keep it responsive without
# changing the proxy's graceful shutdown contract.
SERVER_SHUTDOWN_POLL_INTERVAL_SECONDS = 0.05


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
        return f"http://{self.address}:{self.port}/ipc"

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
    #: What this lease renews for.  A native host's is the 30-minute session
    #: above; a browser page's is ``web_session_ttl_seconds``.
    ttl_seconds: float = SESSION_TOKEN_TTL_SECONDS
    #: Whether this is a browser page's lease, which is the one recorded on
    #: disk instead of living only as long as this process.
    browser: bool = False


def browser_session_digest(token: str) -> str:
    """The on-disk name of one browser session.

    The cookie is the credential; the file states only what can be checked, so
    a copy of the file is not a set of usable sessions.
    """

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _PreparedResponse:
    """One answer that is already bytes, so the envelope is not rebuilt.

    Everything else returns a ``ResponseEnvelope``, which validates the whole
    result on construction and encodes it on the way out -- two Python walks
    over a 273 KB snapshot, roughly 14 ms on the host this was measured on.  A
    window polls the same revision many times a second, and the bytes of that
    answer cannot change while the revision stands, so the second poll reuses
    them: the envelope's ``request_id`` is spliced in front of the cached
    ``result`` and the object is never walked again.
    """

    status: int
    body: bytes


@dataclass
class _Subscription:
    subscription_id: str
    session_token: str
    # The event topics this subscription asked for, or None for every event.
    # A topic is the event's own ``event`` name; the contract's vocabulary is
    # exactly what Core publishes, so an empty set is a subscription that asked
    # for nothing.
    topics: frozenset[str] | None = None
    queue: queue.Queue[dict[str, Any]] = field(default_factory=lambda: queue.Queue(maxsize=MAX_EVENTS))


@dataclass
class _EditorCapability:
    token: str
    session_token: str
    domain: str
    document: str
    revision: int
    expires_at: float
    text_digest: str = ""
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


@dataclass(frozen=True)
class _SecretReadCapability:
    token: str
    session_token: str
    domain: str
    field: str
    target: str | None
    revision: int
    expires_at: float


@dataclass(frozen=True, repr=False)
class _ImportPlanCapability:
    """One-use, session-bound lease for a parsed import package."""

    token: str
    session_token: str
    prepared: PreparedImport = field(repr=False)
    expires_at: float


class _CoreRequestHandler(http.server.BaseHTTPRequestHandler):
    """Request handler bound to one ``CoreIPCServer`` instance."""

    server_version = "YoungRouterCore/1"
    sys_version = ""
    # Every settings control is one loopback round trip, and a control a user
    # drags or types into issues several.  HTTP/1.0 closes the socket after each
    # answer, so each of those paid a fresh TCP handshake on the same machine;
    # keeping the connection open lets the host reuse it for the next request.
    # The protocol is still one request per response, framed by Content-Length.
    protocol_version = "HTTP/1.1"

    @property
    def _owner(self) -> "CoreIPCServer":
        return self.server.core_ipc_owner  # type: ignore[attr-defined]

    def handle_one_request(self) -> None:
        """Answer one request, with the body flag belonging to that request.

        A keep-alive connection is a loop over this method on *one* handler
        instance, so ``_body_consumed`` set by request 1 is still set for
        request 2.  That is a desync, not a saved byte: a 401 or a 404 answered
        without reading its body then skipped the drain, the unread JSON stayed
        in the socket buffer, and the next request line was read back as
        `{"protocol_version":…}GET` — which http.server answers with a 501
        "Unsupported method" page.  The page then showed an "Error response"
        document instead of the app, in the middle of an ordinary pane switch.

        The reset belongs here rather than in each ``do_*`` so no route can
        forget it: every answer, early or late, starts from "this request's
        body is unread".
        """

        self._body_consumed = False
        super().handle_one_request()

    def log_message(self, _format: str, *_args: object) -> None:
        # HTTP's default stderr logger includes request paths and would make a
        # credential-bearing query/body easy to correlate.  The Core has no
        # request logger at this boundary: the host reports its own failures,
        # and a line per request would both leak settings traffic onto stderr
        # and cost more than the request itself.
        return

    def _authorization(self) -> str:
        value = self.headers.get("Authorization", "")
        if not value.startswith("Bearer "):
            return ""
        token = value[7:].strip()
        return token if len(token.encode("utf-8")) <= 512 else ""

    def _read_body(self) -> bytes:
        # The body belongs to this request, whether or not it is used: a route
        # that reads it marks it consumed so the reply does not drain it twice.
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            raise ProtocolError("invalid_message", "IPC request body is invalid") from None
        if length < 0 or length > MAX_MESSAGE_BYTES:
            raise ProtocolError("message_too_large", "IPC message exceeds the size limit")
        body = self.rfile.read(length)
        self._body_consumed = True
        if len(body) != length:
            raise ProtocolError("invalid_message", "IPC request body is incomplete")
        return body

    def _consume_unread_body(self) -> None:
        """Consume a request body this handler never read, before answering.

        A keep-alive connection hands the next request to this handler on the
        same socket, so an unread body is not discarded by the stdlib: its bytes
        stay in the buffer and the next request line starts with what was left
        of it -- ``{}`` read back as the method ``{}POST``, which the server
        answers 501.

        This runs *before* the reply is written.  Reading it afterwards makes
        the two sides wait on each other: the peer is waiting for a response it
        has been told is coming, and this side is waiting for bytes the peer has
        already sent and will not resend.  Any frame this cannot drain means the
        connection cannot be reused, so it is closed instead of guessed at.
        """

        if getattr(self, "_body_consumed", False):
            return
        self._body_consumed = True
        raw_length = self.headers.get("Content-Length", "")
        if not raw_length:
            return
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            self.close_connection = True
            return
        if length <= 0:
            return
        if length > MAX_MESSAGE_BYTES:
            self.close_connection = True
            return
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                self.close_connection = True
                return
            remaining -= len(chunk)

    def _browser_session_cookie_header(self, token: str, *, max_age_seconds: int = WEB_SESSION_COOKIE_MAX_AGE_SECONDS) -> str:
        # ``Max-Age`` is what makes the page outlive the browser: a session
        # cookie is dropped when the browser closes, and a person who reopened
        # it met the password form again for a key they had already typed.
        # The value is the same 30 days the lease itself runs for, so the
        # cookie never outlives the session it names.  ``0`` is the sign-out
        # answer: it deletes the cookie the browser is holding.
        parts = [
            f"{BROWSER_SESSION_COOKIE}={token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
            f"Max-Age={max(0, int(max_age_seconds))}",
        ]
        # The published page is reached over TLS (and says so on the hop the
        # proxy forwarded); a loopback page is plain http on this machine, and
        # a `Secure` cookie there would simply never be sent back.
        if (self.headers.get("X-Forwarded-Proto", "") or "").strip().lower() == "https":
            parts.append("Secure")
        return "; ".join(parts)

    def _send(
        self,
        status: int,
        payload: Mapping[str, Any],
        *,
        session_token: str | None = None,
        clear_session: bool = False,
    ) -> None:
        self._send_encoded(status, _encode_response_body(payload), session_token=session_token, clear_session=clear_session)

    def _send_encoded(
        self,
        status: int,
        encoded: bytes,
        *,
        session_token: str | None = None,
        clear_session: bool = False,
    ) -> None:
        """Answer with bytes that are already the body this route sends."""
        # A body this handler never read would otherwise become the next
        # request line on this kept-alive socket.
        self._consume_unread_body()
        # An error also leaves the peer free to reuse the connection: the
        # framing is complete either way, and closing on every 4xx is what made
        # a rejected request cost a reconnect.
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        if session_token:
            self.send_header("X-LiteLLM-Core-Session", session_token)
            self.send_header("Set-Cookie", self._browser_session_cookie_header(session_token))
        elif clear_session:
            # Sign-out: the browser is told to forget the cookie it holds, so
            # the page it reloads meets the gate that a dead lease deserves.
            self.send_header("Set-Cookie", self._browser_session_cookie_header("", max_age_seconds=0))
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

    # The browser host is the one caller whose credential cannot travel in a
    # readable header: page script may not read ``X-LiteLLM-Core-Session``, and
    # a session in ``localStorage`` would be script-readable.  It is therefore
    # carried in this cookie instead -- ``HttpOnly``, ``SameSite=Strict``, and
    # ``Path=/`` so the same page that serves the bundle hands it back to
    # ``/ipc``.  The two native hosts keep using the header; a request that
    # states both must agree, or it is refused rather than resolved.
    def _browser_session_cookie(self) -> str:
        raw = self.headers.get("Cookie", "")
        if not raw:
            return ""
        for part in raw.split(";"):
            name, _separator, value = part.strip().partition("=")
            if name == BROWSER_SESSION_COOKIE:
                return value.strip()
        return ""

    def _session_authorization(self) -> str:
        header = self._authorization()
        cookie = self._browser_session_cookie()
        if header and cookie and not hmac.compare_digest(header, cookie):
            return ""
        return header or cookie

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler hook
        route = urlsplit(self.path).path.rstrip("/") or "/"
        if route == "/ipc/hello":
            self._owner._handle_hello(self)
            return
        if route == "/ipc/web/login":
            # The one route a page may call with nothing but a typed password.
            self._owner._handle_web_login(self)
            return
        if route == "/ipc/web/launch":
            # The one route a page may call with nothing but the credential a
            # terminal put in the address it printed.
            self._owner._handle_web_launch(self)
            return
        if route == "/ipc/web/logout":
            # The page's own sign-out: it names the cookie it wants ended.
            self._owner._handle_web_logout(self)
            return
        if route == "/ipc/host/shutdown":
            token = self._session_authorization()
            if not self._owner._valid_session(token):
                self._send_error(401, code="unauthorized", message="Core IPC authentication failed")
                return
            try:
                if self._read_body().strip() not in {b"", b"{}"}:
                    raise ProtocolError("invalid_request", "Host shutdown request is invalid")
                self._owner.core.shutdown(stopped_by="app_exit")
            except (CoreError, ProtocolError) as exc:
                self._send_error(400, code=exc.code, message=exc.message, retryable=exc.code == "service_error")
                return
            except Exception:
                self._send_error(500, code="service_error", message="LiteLLM service could not stop", retryable=True)
                return
            self._send(200, {"protocol_version": PROTOCOL_VERSION, "stopped": True})
            return
        if route == "/ipc/host/file-download" and self.command == "POST":
            token = self._session_authorization()
            if not self._owner._valid_session(token):
                self._send_error(401, code="unauthorized", message="Core IPC authentication failed")
                return
            try:
                data = decode_message(self._read_body())
                if set(data) == {"purpose", "name"}:
                    if data.get("purpose") != "export" or not isinstance(data.get("name"), str):
                        raise CoreError("invalid_file_capability", "The selected file is unavailable")
                    name = Path(data["name"]).name
                    if not name or name in {".", ".."} or len(name.encode("utf-8")) > 255:
                        raise CoreError("invalid_file_capability", "The selected file is unavailable")
                    path = Path(tempfile.gettempdir()) / f"young-router-export-{secrets.token_urlsafe(12)}.json"
                    capability = self._owner.register_file_capability(str(path), "export")
                    with self._owner._browser_file_lock:
                        self._owner._browser_files[capability] = path
                        self._owner._browser_file_sessions[capability] = token
                elif set(data) == {"token"}:
                    body = self._owner.take_browser_file(data.get("token"), token)
                    if body is None:
                        raise CoreError("invalid_file_capability", "The export is unavailable")
                    encoded = _encode_response_body({"protocol_version": PROTOCOL_VERSION, "data": base64.b64encode(body).decode("ascii")})
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                    return
                else:
                    raise CoreError("invalid_file_capability", "The selected file is unavailable")
            except (CoreError, ProtocolError, ValueError, OSError):
                self._send_error(400, code="invalid_file_capability", message="The export is unavailable")
                return
            self._send(200, {"protocol_version": PROTOCOL_VERSION, "token": capability})
            return
        if route == "/ipc/host/file-upload":
            token = self._session_authorization()
            if not self._owner._valid_session(token):
                self._send_error(401, code="unauthorized", message="Core IPC authentication failed")
                return
            try:
                data = decode_message(self._read_body())
                if set(data) != {"purpose", "name", "size", "data"} or data.get("purpose") != "import":
                    raise CoreError("invalid_file_capability", "The selected file is unavailable")
                name = data.get("name")
                size = data.get("size")
                encoded = data.get("data")
                if not isinstance(name, str) or type(size) is not int or not isinstance(encoded, str) or size < 0 or size > MAX_EDITOR_DOCUMENT_BYTES:
                    raise CoreError("invalid_file_capability", "The selected file is unavailable")
                suffix = Path(name).suffix.lower()
                if suffix not in {".json", ".yaml", ".yml", ".toml", ".sql"}:
                    raise CoreError("invalid_file_capability", "The selected file is unavailable")
                content = base64.b64decode(encoded, validate=True)
                if len(content) != size:
                    raise CoreError("invalid_file_capability", "The selected file is unavailable")
                # The document is Core's private, short-lived temporary file;
                # the import capability is its only route back to the content.
                with tempfile.NamedTemporaryFile(prefix="young-router-import-", suffix=suffix, delete=False) as stream:
                    stream.write(content)
                    path = Path(stream.name)
                capability = self._owner.register_file_capability(str(path), "import")
                with self._owner._browser_file_lock:
                    self._owner._browser_files[capability] = path
                    self._owner._browser_file_sessions[capability] = token
            except (CoreError, ProtocolError, ValueError, OSError, binascii.Error):
                self._send_error(400, code="invalid_file_capability", message="The selected file is unavailable")
                return
            self._send(200, {"protocol_version": PROTOCOL_VERSION, "token": capability})
            return
        if route == "/ipc/host/file-download":
            token = self._session_authorization()
            if not self._owner._valid_session(token):
                self._send_error(401, code="unauthorized", message="Core IPC authentication failed")
                return
            try:
                data = decode_message(self._read_body())
                if set(data) != {"purpose", "name"} or data.get("purpose") != "export" or not isinstance(data.get("name"), str):
                    raise CoreError("invalid_file_capability", "The selected file is unavailable")
                name = Path(data["name"]).name
                if not name or name in {".", ".."} or len(name.encode("utf-8")) > 255:
                    raise CoreError("invalid_file_capability", "The selected file is unavailable")
                path = Path(tempfile.gettempdir()) / f"young-router-export-{secrets.token_urlsafe(12)}.json"
                capability = self._owner.register_file_capability(str(path), "export")
            except (CoreError, ProtocolError, ValueError, OSError):
                self._send_error(400, code="invalid_file_capability", message="The selected file is unavailable")
                return
            self._send(200, {"protocol_version": PROTOCOL_VERSION, "token": capability})
            return
        if route == "/ipc/host/autostart":
            token = self._session_authorization()
            if not self._owner._valid_session(token):
                self._send_error(401, code="unauthorized", message="Core IPC authentication failed")
                return
            # The browser host's login item.  The desktop hosts register theirs
            # in their own process (`SMAppService`, the Windows Run key their
            # leaf writes); a page has no such process behind it, so its Core —
            # which is that app's Core, on that same machine — writes it.  The
            # preference itself is still Core's, written by the panes' own
            # service dispatch, so this answers only whether the login item now
            # exists.
            from . import login_item

            try:
                data = decode_message(self._read_body())
                if set(data) != {"enabled"} or not isinstance(data.get("enabled"), bool):
                    raise CoreError("invalid_login_item", "The login item could not be updated")
                registered = login_item.set_login_item(bool(data["enabled"]))
            except (CoreError, ProtocolError) as exc:
                self._send_error(400, code=exc.code, message=exc.message)
                return
            except Exception:
                self._send_error(400, code="invalid_login_item", message="The login item could not be updated")
                return
            self._send(200, {
                "protocol_version": PROTOCOL_VERSION,
                "registered": registered,
                "available": login_item.available(),
            })
            return
        if route == "/ipc/host/file-capability":
            token = self._session_authorization()
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
        if route in {"/ipc/host/relay/browser/frames", "/ipc/host/relay/browser/input"}:
            token = self._session_authorization()
            if not self._owner._valid_session(token):
                self._send_error(401, code="unauthorized", message="Core IPC authentication failed")
                return
            # One sign-in's screen and keyboard.  The frame is a JPEG the page
            # paints; the input is the person's click and typing turned into the
            # DevTools calls that mean the same thing.  Both answer only for a
            # login this Core minted, so a page cannot drive another's browser.
            from . import relay_mirror

            try:
                data = decode_message(self._read_body())
                entry = self._owner.core.relay_browser_mirror(data.get("login_id"))
                mirror = getattr(entry, "mirror", None) if entry is not None else None
                if mirror is None:
                    raise CoreError("relay_login_failed", "Relay sign-in window is closed")
                if route.endswith("/frames"):
                    since = data.get("since", 0)
                    if not isinstance(since, int) or since < 0:
                        raise CoreError("relay_login_failed", "Relay sign-in frame is invalid")
                    seq, frame = mirror.latest(since)
                    payload: dict[str, Any] = {
                        "protocol_version": PROTOCOL_VERSION,
                        "status": "frame" if frame else "idle",
                        "seq": seq,
                        "width": int(mirror.width),
                        "height": int(mirror.height),
                    }
                    if frame:
                        payload["frame"] = base64.b64encode(frame).decode("ascii")
                    self._send(200, payload)
                    return
                event = data.get("event")
                if not isinstance(event, Mapping):
                    raise CoreError("relay_login_failed", "Relay sign-in input is invalid")
                dispatched = relay_mirror.dispatch(
                    entry.port,
                    entry.origin,
                    event,
                    width=int(mirror.width),
                    height=int(mirror.height),
                )
                self._send(200, {"protocol_version": PROTOCOL_VERSION, "dispatched": bool(dispatched)})
            except CoreError as exc:
                self._send_error(400, code=exc.code, message=exc.message)
            except Exception:
                self._send_error(400, code="relay_login_failed", message="Relay sign-in input is unavailable")
            return
        if route in {"/ipc/host/relay/browser/start", "/ipc/host/relay/browser/poll", "/ipc/host/relay/browser/cancel", "/ipc/host/relay/browser/focus"}:
            token = self._session_authorization()
            if not self._owner._valid_session(token):
                self._send_error(401, code="unauthorized", message="Core IPC authentication failed")
                return
            # The page host's sign-in window: Core opens a real browser at the
            # station's own address, because a page cannot present another
            # origin's login form and cannot read a cookie another tab minted.
            # The window is the station's own, so a captcha, a code, or an
            # OAuth redirect behaves exactly as it does in any browser.
            try:
                data = decode_message(self._read_body())
                if route.endswith("/browser/start"):
                    allowed = {"account_id", "type", "label", "origin", "username", "pending_account", "station_id", "station_name", "station_type", "station_origin"}
                    required = {"account_id", "type", "label", "origin"}
                    if not required.issubset(data) or set(data).difference(allowed):
                        raise CoreError("relay_login_failed", "Relay login result is invalid")
                    result = self._owner.core.start_relay_browser_login(
                        account_id=data.get("account_id"),
                        account_type=data.get("type"),
                        label=data.get("label"),
                        origin=data.get("origin"),
                        username=data.get("username", ""),
                        pending_account=data.get("pending_account", False),
                        station_id=data.get("station_id"),
                        station_name=data.get("station_name"),
                        station_type=data.get("station_type"),
                        station_origin=data.get("station_origin"),
                        # The page's own origin decides whether a window this
                        # Core opens is one the person can see: a published page
                        # is open in somebody else's browser, and the answer
                        # routes it to the sign-in the page itself can run.
                        page_is_local=self._owner._page_is_local(self),
                    )
                elif route.endswith("/browser/poll"):
                    if set(data) != {"login_id"} or not isinstance(data.get("login_id"), str):
                        raise CoreError("relay_login_failed", "Relay login result is invalid")
                    result = self._owner.core.poll_relay_browser_login(data.get("login_id"))
                elif route.endswith("/browser/focus"):
                    if set(data) != {"login_id"} or not isinstance(data.get("login_id"), str):
                        raise CoreError("relay_login_failed", "Relay login result is invalid")
                    result = self._owner.core.focus_relay_browser_login(data.get("login_id"))
                else:
                    if set(data) != {"login_id"} or not isinstance(data.get("login_id"), str):
                        raise CoreError("relay_login_failed", "Relay login result is invalid")
                    result = self._owner.core.cancel_relay_browser_login(data.get("login_id"))
                self._send(200, {"protocol_version": PROTOCOL_VERSION, **result})
            except CoreError as exc:
                self._send_error(400, code=exc.code, message=exc.message)
            except Exception:
                self._send_error(400, code="relay_login_failed", message="Relay login result is invalid")
            return
        if route in {"/ipc/host/secret/capability", "/ipc/host/secret/stage", "/ipc/host/relay/login", "/ipc/host/relay/restore", "/ipc/host/relay/resources", "/ipc/host/relay/import"}:
            token = self._session_authorization()
            if not self._owner._valid_session(token):
                self._send_error(401, code="unauthorized", message="Core IPC authentication failed")
                return
            try:
                data = decode_message(self._read_body())
                if route.endswith("/relay/login"):
                    allowed = {"account_id", "type", "label", "origin", "username", "cookie", "access_token", "refresh_token", "password", "user_id", "station_id", "station_name", "station_type", "station_origin", "remember_password", "auto_grouping", "pending_account"}
                    required = {"account_id", "type", "label", "origin", "username"}
                    if not required.issubset(data) or set(data).difference(allowed):
                        raise CoreError("relay_login_failed", "Relay login result is invalid")
                    result = self._owner.core.accept_relay_login(
                        account_id=data.get("account_id"),
                        account_type=data.get("type"),
                        label=data.get("label"),
                        origin=data.get("origin"),
                        username=data.get("username"),
                        cookie=data.get("cookie", ""),
                        access_token=data.get("access_token", ""),
                        refresh_token=data.get("refresh_token", ""),
                        password=data.get("password", ""),
                        user_id=data.get("user_id", ""),
                        station_id=data.get("station_id"),
                        station_name=data.get("station_name"),
                        station_type=data.get("station_type"),
                        station_origin=data.get("station_origin"),
                        remember_password=data.get("remember_password"),
                        auto_grouping=data.get("auto_grouping"),
                        pending_account=data.get("pending_account", False),
                    )
                    self._send(200, {"protocol_version": PROTOCOL_VERSION, **result})
                elif route.endswith("/relay/restore"):
                    allowed = {"account_id", "type", "label", "origin", "login_status", "username", "cookie", "access_token", "refresh_token", "user_id", "quota"}
                    required = {"account_id", "type", "label", "origin", "login_status"}
                    if not required.issubset(data) or set(data).difference(allowed):
                        raise CoreError("relay_restore_failed", "Relay login result is invalid")
                    # The profile quota the host's probe read.  It is optional -- a
                    # host that did not read one leaves the balance to Core's own
                    # refresh -- but a value that is not a number is refused rather
                    # than dropped, so a broken bridge is not read as "no balance".
                    quota = data.get("quota")
                    if quota is not None and (isinstance(quota, bool) or not isinstance(quota, (int, float))):
                        raise CoreError("relay_restore_failed", "Relay login result is invalid")
                    result = self._owner.core.restore_relay_session(
                        account_id=data.get("account_id"),
                        account_type=data.get("type"),
                        label=data.get("label"),
                        origin=data.get("origin"),
                        login_status=data.get("login_status"),
                        username=data.get("username", ""),
                        cookie=data.get("cookie", ""),
                        access_token=data.get("access_token", ""),
                        refresh_token=data.get("refresh_token", ""),
                        user_id=data.get("user_id", ""),
                        quota=quota,
                    )
                    self._send(200, {"protocol_version": PROTOCOL_VERSION, **result})
                elif route.endswith("/relay/import"):
                    if set(data) != {"account_id", "resource_ids", "revision"} or not isinstance(data.get("account_id"), str) or not isinstance(data.get("resource_ids"), list) or type(data.get("revision")) is not int:
                        raise CoreError("relay_import_failed", "Relay account is unavailable")
                    result = self._owner.core.import_relay_resources(data["account_id"], data["resource_ids"], revision=data["revision"])
                    self._send(200, {"protocol_version": PROTOCOL_VERSION, **result})
                elif route.endswith("/relay/resources"):
                    if set(data) != {"account_id", "revision"} or not isinstance(data.get("account_id"), str) or type(data.get("revision")) is not int:
                        raise CoreError("relay_resources_failed", "Relay account is unavailable")
                    result = self._owner.core.refresh_relay_resources(data["account_id"], revision=data["revision"])
                    self._send(200, {"protocol_version": PROTOCOL_VERSION, **result})
                elif route.endswith("/capability"):
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
        if route in {"/ipc/host/secret/read-capability", "/ipc/host/secret/read"}:
            token = self._session_authorization()
            if not self._owner._valid_session(token):
                self._send_error(401, code="unauthorized", message="Core IPC authentication failed")
                return
            try:
                data = decode_message(self._read_body())
                if route.endswith("read-capability"):
                    if set(data) != {"domain", "field", "target"}:
                        raise CoreError("invalid_secret", "The requested secret field is unavailable")
                    capability = self._owner.register_secret_read_capability(
                        data.get("domain"),
                        data.get("field"),
                        data.get("target"),
                        session_token=token,
                    )
                    self._send(200, {"protocol_version": PROTOCOL_VERSION, **capability})
                else:
                    if set(data) != {"secret_read_token"}:
                        raise CoreError("invalid_secret", "The requested secret field is unavailable")
                    value = self._owner.read_secret_capability(
                        data.get("secret_read_token"), session_token=token
                    )
                    self._send(200, {"protocol_version": PROTOCOL_VERSION, "value": value})
            except (CoreError, ProtocolError) as exc:
                self._send_error(400, code=exc.code, message=exc.message, retryable=exc.code == "revision_conflict")
            except Exception:
                self._send_error(400, code="invalid_secret", message="The requested secret field is unavailable")
            return
        if route != "/ipc":
            self._send_error(404, code="not_found", message="Core IPC route is unavailable")
            return
        token = self._session_authorization()
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
        if isinstance(response, _PreparedResponse):
            self._send_encoded(response.status, response.body)
            return
        if response.ok and request.method == "snapshot" and isinstance(response.result, Mapping):
            # The first poll of a revision pays for the validation and the
            # encoding; every later one is answered from these bytes.
            self._owner.remember_snapshot_result(response.result)
        self._send(200 if response.ok else 400, response.to_mapping())

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler hook
        route = urlsplit(self.path)
        if route.path.rstrip("/") != "/ipc/events":
            if self._owner.serve_web_host(self, route.path):
                return
            self._send_error(404, code="not_found", message="Core IPC route is unavailable")
            return
        token = self._session_authorization()
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
        web_session_ttl_seconds: float = WEB_SESSION_TTL_SECONDS,
        web_sessions_path: Path | str | None = None,
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
        self.web_session_ttl_seconds = max(1.0, float(web_session_ttl_seconds))
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._core_unsubscribe: Callable[[], None] | None = None
        self._bootstrap_token = ""
        self._bootstrap_expires_at = 0.0
        self._bootstrap_consumed = False
        self._sessions: dict[str, _Session] = {}
        self._subscriptions: dict[str, _Subscription] = {}
        #: ``(revision, result bytes)`` of the last snapshot answered to a poll.
        self._snapshot_body: tuple[int, bytes] | None = None
        self._editor_capabilities: dict[str, _EditorCapability] = {}
        self._secret_capabilities: dict[str, _SecretCapability] = {}
        self._secret_read_capabilities: dict[str, _SecretReadCapability] = {}
        self._import_plans: dict[str, _ImportPlanCapability] = {}
        self._browser_files: dict[str, Path] = {}
        self._browser_file_lock = threading.RLock()
        self._browser_file_sessions: dict[str, str] = {}
        # The browser host's own bundle.  Empty means "this Core serves no web
        # host", which is what every native install is: the two native hosts
        # read their React code from the app bundle, never from this listener.
        self._web_root: Path | None = None
        # The web host's own gate: one port and one password a person types.
        # Nothing here is a session credential for the IPC; the browser page
        # still exchanges a session over `/ipc/hello`, but only after this
        # password has been presented.
        self._web_password = ""
        # A page key edited in General is read through this, so the next
        # sign-in uses it without replacing the listener.
        self._web_password_provider: Callable[[], str] | None = None
        # The language the gate is drawn in, resolved by the app's own
        # language domain.  Read per request for the same reason the key is:
        # changing it must not need a restart.
        self._web_locale_provider: Callable[[], object] | None = None
        # The credential a terminal launch puts in the printed address.  Empty
        # for a Core started by a native host or by a plain CLI run.
        self._web_launch_token = ""
        # Whether the address this process publishes carries that credential.
        self._web_auto_login = False
        # The browser's own lease outlives this process, so the digests of the
        # sessions it issued are recorded beside the Core metadata.  A host
        # that states no path (a test, a bare CLI run) keeps them in memory
        # only, which is the old behaviour stated rather than guessed at.
        self._web_sessions_path = Path(web_sessions_path).expanduser() if web_sessions_path is not None else None
        self._web_sessions: dict[str, float] = {}
        self._web_sessions_written: dict[str, float] = {}
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

    def discard_browser_file(self, token: object) -> None:
        if not isinstance(token, str):
            return
        with self._browser_file_lock:
            path = self._browser_files.pop(token, None)
            self._browser_file_sessions.pop(token, None)
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass

    def take_browser_file(self, token: object, session_token: str) -> bytes | None:
        if not isinstance(token, str) or not token or not self._valid_session(session_token):
            return None
        with self._browser_file_lock:
            if self._browser_file_sessions.get(token) != session_token:
                return None
            path = self._browser_files.pop(token, None)
            self._browser_file_sessions.pop(token, None)
        if path is None:
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None
        finally:
            try:
                path.unlink()
            except OSError:
                pass

    def serve_web_host(self, handler: "_CoreRequestHandler", path: str) -> bool:
        """Answer one `GET` for the browser host, or say it is not this server's.

        The page is served by the same listener that answers `/ipc`, so the
        browser's origin is this Core: `/ipc` is same-origin, no CORS answer or
        preflight exists, and no second port has to be published.  Only
        loopback `Host` names are answered, which is what keeps a DNS-rebinding
        page (whose `Host` is its own attacker domain) from reaching it.

        Returns ``False`` for every path this server does not serve, so the
        caller keeps answering 404 exactly as before when no bundle is staged.
        """

        root = self._web_root
        if root is None:
            return False
        host = (handler.headers.get("Host", "").rsplit(":", 1)[0] or "").strip("[]").lower()
        if host not in WEB_HOSTS:
            return False
        # One bundle, two listeners.  The proxy on the published local port
        # forwards `/ui` here so the page and the API share one address; a
        # Core reached directly (a terminal launch, a test) answers the same
        # paths, because a second spelling would be a second page.
        #
        # The page names its script relatively, so the mount root has to end in
        # a slash before the shell is answered: at `/ui` the browser would
        # resolve `./bundle.js` against the proxy's root, take LiteLLM's 404 as
        # the script, and draw the shell with nothing in it.  The published
        # address (``web_host_url``) already carries the slash; this makes the
        # spelling a person types reach it too.
        if path == WEB_UI_PREFIX:
            handler.send_response(308)
            handler.send_header("Location", WEB_UI_PREFIX + "/")
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return True
        if path.startswith(WEB_UI_PREFIX + "/"):
            path = path[len(WEB_UI_PREFIX):]
        relative = path.lstrip("/") or "index.html"
        if ".." in relative.split("/"):
            return False
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return False
        content_type = _WEB_CONTENT_TYPES.get(candidate.suffix.lower())
        if content_type is None or not candidate.is_file():
            return False
        # A text asset is answered in whichever encoding the builder wrote
        # beside it and the browser says it reads; the sibling is never an
        # asset of its own (`.br` is not in `_WEB_CONTENT_TYPES`, so asking for
        # it by name is a 404 rather than a second copy of the bundle).
        encoding, body, varied = _web_asset(candidate, handler.headers.get("Accept-Encoding", ""))
        if not body:
            return False
        # The app's own shell is only handed to a page that has signed in.
        # Everything else -- the bundle, the login page itself -- is public,
        # because those are bytes any page could fetch from a port on this
        # machine, and gating them behind a cookie would only mean the login
        # page could not load.
        # A page opened on this machine is the owner's page: it is served the
        # app, and the shell response hands it the session cookie the pane's
        # `/ipc` calls need, so no form stands between a person and their own
        # settings.  A page published behind a reverse proxy keeps the key.
        local_session = ""
        if candidate.name == "index.html" and not self._page_signed_in(handler):
            if self._page_is_local(handler):
                # The owner's own page, signed in as it always was -- with a
                # lease that now lasts as long as the browser keeps the cookie.
                local_session = self._mint_web_session()
            else:
                body = _web_login_page(self._web_language(handler))
                content_type = "text/html; charset=utf-8"
                relative = "login.html"
                # The gate is drawn per request and carries no asset of its
                # own, so it is stated uncompressed: it has no sibling, and
                # `varied` would only make a cache key on a header nothing
                # here decides.
                encoding = ""
                varied = False
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        if encoding:
            handler.send_header("Content-Encoding", encoding)
        # Stated whenever a sibling exists, not only when this answer used one:
        # a cache that stored the brotli bytes must not hand them to a browser
        # that asked for neither, and one that stored the raw bytes must not be
        # read as the whole answer to a browser that would have taken brotli.
        if varied:
            handler.send_header("Vary", "Accept-Encoding")
        if local_session:
            handler.send_header("Set-Cookie", handler._browser_session_cookie_header(local_session))
        # The shell is never cached; every asset beside it is immutable for a
        # year, which is only true because the script is named after its own
        # bytes (`bundle.<hash>.js`) -- a fixed `bundle.js` under this header is
        # a browser frozen on the build it happened to load first, which is how
        # a fixed string stayed on screen for a day.
        handler.send_header(
            "Cache-Control",
            "no-store" if candidate.suffix.lower() == ".html" else "public, max-age=31536000, immutable",
        )
        # The page frames nothing and loads nothing but itself.  One document
        # is the exception: the raw editor is a page of its own, framed by the
        # shell, because this page's CSP would refuse its inline editor bundle
        # and a `srcdoc`/blob frame inherits that policy.  It is answered
        # `SAMEORIGIN` -- no other origin may frame it, and it carries no
        # configuration, only the editor that a signed-in page drives.
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header(
            "X-Frame-Options",
            "SAMEORIGIN" if candidate.name == WEB_EDITOR_PAGE else "DENY",
        )
        handler.end_headers()
        try:
            handler.wfile.write(body)
        except OSError:
            pass
        return True

    def _page_published_host(self, handler: "_CoreRequestHandler") -> str:
        """The `Host` the browser typed, when a forwarder states it.

        The proxy on the published port always connects to Core over loopback
        and states that loopback name as `Host` (the check that stops a
        DNS-rebinding page).  It repeats the browser's own name in
        ``PUBLISHED_HOST_HEADER``, which is the only thing that separates "the
        owner opened the page on this machine" from "somebody reached the page
        through a reverse proxy in front of it".
        """

        forwarded = (handler.headers.get(PUBLISHED_HOST_HEADER, "") or "").strip()
        if forwarded:
            return forwarded
        return (handler.headers.get("Host", "") or "").strip()

    def _page_is_local(self, handler: "_CoreRequestHandler") -> bool:
        """Whether this request came from this machine's own browser.

        A loopback page is not published anywhere: only the owner (or a process
        running as the owner) can open it, so it is served without a key and
        without a sign-in form.  A page reached through a reverse proxy is
        published to whoever can reach that proxy, and stays gated.
        """

        peer = handler.client_address[0] if handler.client_address else ""
        if peer not in {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}:
            return False
        host = self._page_published_host(handler).rsplit(":", 1)[0].strip("[]").lower()
        return host in WEB_HOSTS

    def _page_signed_in(self, handler: "_CoreRequestHandler") -> bool:
        """Whether this browser already holds a live session cookie.

        A signed-in page is served the app; everyone else gets the password
        form.  The form is not a security boundary by itself -- the boundary is
        `/ipc/web/login` and the session cookie -- but it is what a person
        meets, so it must never be the app's shell shown empty and broken.

        The cookie is the whole answer, and it is checked through the same path
        every other request uses: the lease this process holds, or the one a
        replaced Core recorded before it went away.
        """

        cookie = handler._browser_session_cookie()
        if not cookie:
            return False
        return self._valid_session(cookie)

    def enable_web_host(
        self,
        root: Path | str,
        *,
        password: str = "",
        password_provider: Callable[[], str] | None = None,
        locale_provider: Callable[[], object] | None = None,
        auto_login: bool = False,
    ) -> None:
        """Serve one built bundle from this listener, behind one key.

        The key is the page's whole gate for anyone who reaches the port: a
        person opens the address, types it once, and the page keeps a session
        cookie from then on.  It is a convenience lock on a port this machine's
        owner opened -- not a cryptographic secret this code is in a position
        to protect -- so the General pane draws it in the clear.

        ``password_provider`` is how an edit in General reaches the gate
        without a restart: the value is read per sign-in instead of captured
        here.  ``password`` remains for a caller that states one directly (a
        terminal, a test).  ``locale_provider`` is the same lease for the
        gate's language: the person's choice is read per request, so changing
        it never needs the listener replaced.  It returns the language
        domain's own ``snapshot()`` (``choice`` plus ``resolved``) or a plain
        resolved string; see ``_web_language`` for what each half decides.

        ``auto_login`` is for a caller that is *already* the proof: a terminal
        that started this process, or the app's own window opening its page.
        It decides whether the *published* address carries the launch
        credential; the credential itself is always minted here, because
        ``launch_url`` is how a native host opens a page it has already
        proved itself to.  The credential lives only in this process's memory,
        never in a document and never in a snapshot, so the page it opens asks
        for no key while every other browser on that port still does.
        """

        resolved = Path(root).expanduser().resolve()
        if not (resolved / "index.html").is_file():
            raise IPCError("The browser host bundle has no index.html")
        with self._lock:
            if self._server is None:
                raise IPCError("The browser host needs a running Core listener")
            self._web_root = resolved
            self._web_password = password
            self._web_password_provider = password_provider
            self._web_locale_provider = locale_provider
            # Never read from the document and never written to it: a launch
            # credential that survived a restart would be a permanent way past
            # the password.
            self._web_launch_token = secrets.token_urlsafe(32)
            self._web_auto_login = bool(auto_login)

    def _web_language(self, handler: "_CoreRequestHandler") -> str:
        """The language the gate is drawn in right now.

        The app's language domain is the one authority for the preference, and
        the gate has to agree with the page behind it:

        - a stated preference (``en`` / ``zh-Hans``) is the answer, whatever the
          browser asks for;
        - ``system`` -- 自动 in General -- follows the platform, and on the page
          the platform is the browser.  The page reads ``navigator.languages``
          for exactly this, so the one document a browser meets before the app
          must read the same list rather than the language this *machine* is
          configured in: a Chinese browser in front of a host whose ``LANG`` is
          C.UTF-8 used to meet an English gate and then a Chinese app;
        - a Core with no language domain at all (a test, a probe) answers in
          English instead of guessing.

        ``locale_provider`` therefore states both halves -- the domain's own
        ``snapshot()`` is the intended value -- and a plain string is read as
        the resolved language, which is what a caller that has already decided
        can honestly say.
        """

        provider = self._web_locale_provider
        if provider is None:
            return "en"
        try:
            value = provider()
        except Exception:
            # An unreadable preference is not a reason to fail the gate.
            return "en"
        if not isinstance(value, Mapping):
            # A caller that states one language has already decided which it is;
            # there is no choice left for the browser to make.
            return str(value or "en")
        if str(value.get("choice") or "system") == "system":
            accepted = _accepted_language(handler.headers.get("Accept-Language", ""))
            if accepted:
                return accepted
        return str(value.get("resolved") or "en")

    def set_web_password(self, password: str) -> None:
        """Take an edited password, without a restart of the listener.

        The page's gate is read per request, so an edited password applies to
        the next sign-in and leaves already-signed-in pages signed in: changing
        the lock must not sign out the person changing it.
        """

        with self._lock:
            self._web_password = password

    def _web_password_value(self) -> str:
        """The key the page's gate compares against right now."""

        provider = self._web_password_provider
        if provider is None:
            return self._web_password
        try:
            return str(provider() or "")
        except Exception:
            # A document that cannot be read is not an unlocked door.
            return ""

    @property
    def web_host_url(self) -> str:
        """The address a person opens.

        Credential-free by default: the password is typed into the page, so the
        URL stays something that can be spoken, bookmarked, or written on a
        sticky note.  A Core started with ``auto_login`` adds its launch
        credential as a fragment, which no server, log, or ``Referer`` header
        sees, and which stops working when this process does.
        """

        with self._lock:
            if self._web_root is None or self._web_password_value() == "":
                return ""
            port = self._web_page_port()
            launch = self._web_launch_token if self._web_auto_login else ""
        if not port:
            return ""
        if launch:
            return f"http://{self.address}:{port}{WEB_UI_PREFIX}/#launch={launch}"
        return f"http://{self.address}:{port}{WEB_UI_PREFIX}/"

    @property
    def web_launch_url(self) -> str:
        """The address a host opens after it has proved itself to Core.

        The credential rides in the fragment: no request path, no server log
        and no ``Referer`` header ever carries it, and it stops working when
        this process does.  A native host asks for this instead of copying the
        page key into another browser, which is what makes clicking the address
        in General a signed-in page.
        """

        with self._lock:
            if self._web_root is None or self._web_password_value() == "":
                return ""
            port = self._web_page_port()
            launch = self._web_launch_token
        if not port:
            return ""
        return f"http://{self.address}:{port}{WEB_UI_PREFIX}/#launch={launch}" if launch else ""

    def _web_page_port(self) -> int:
        """The port the page is published on, which is not this listener's.

        A running proxy answers `/ui` and `/ipc` on the address every client
        already points at, so the page a person opens is that number -- the one
        the General pane prints.  Publishing this listener's own ephemeral port
        instead sent a click on that address to a second, random port: same
        app, different address, and nothing on screen said why the number had
        changed.  A host with no proxy keeps the listener's port, because that
        is then the only place the page is.
        """

        published = 0
        reader = getattr(self.core, "published_page_port", None)
        if callable(reader):
            try:
                published = int(reader() or 0)
            except Exception:
                published = 0
        return published or self.port

    def register_editor_capability(self, domain: str, document: str, *, session_token: str) -> dict[str, Any]:
        descriptor = self.core.editor_document(domain, document)
        text = str(descriptor["text"])
        token = secrets.token_urlsafe(32)
        capability = _EditorCapability(
            token=token,
            session_token=session_token,
            domain=str(descriptor["domain"]),
            document=str(descriptor["document"]),
            revision=int(descriptor["revision"]),
            expires_at=time.monotonic() + EDITOR_CAPABILITY_TTL_SECONDS,
            text_digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            # The Core supplied the text and revision atomically above, so
            # this newly minted lease is ready to stage immediately.
            read=True,
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

    def register_secret_read_capability(
        self,
        domain: object,
        field: object,
        target: object,
        *,
        session_token: str,
    ) -> dict[str, Any]:
        """Issue a read-once, native-only lease for one visible credential.

        This is deliberately narrower than ordinary secret staging.  The
        public React IPC schema remains unable to request or receive a secret.
        """

        if (
            not isinstance(domain, str)
            or not isinstance(field, str)
            or (target is not None and not isinstance(target, str))
            or not self._valid_session(session_token)
        ):
            raise CoreError("invalid_secret", "The requested secret field is unavailable")
        descriptor = self.core.trusted_secret_read_descriptor(domain, field, target)
        token = secrets.token_urlsafe(32)
        capability = _SecretReadCapability(
            token=token,
            session_token=session_token,
            domain=str(descriptor["domain"]),
            field=str(descriptor["field"]),
            target=descriptor["target"],
            revision=int(descriptor["revision"]),
            expires_at=time.monotonic() + SECRET_READ_CAPABILITY_TTL_SECONDS,
        )
        with self._lock:
            now = time.monotonic()
            self._secret_read_capabilities = {
                key: item
                for key, item in self._secret_read_capabilities.items()
                if item.expires_at >= now and item.session_token in self._sessions
            }
            session_items = [
                item
                for item in self._secret_read_capabilities.values()
                if hmac.compare_digest(item.session_token, session_token)
            ]
            if len(session_items) >= MAX_SECRET_READ_CAPABILITIES:
                oldest = min(session_items, key=lambda item: item.expires_at)
                self._secret_read_capabilities.pop(oldest.token, None)
            self._secret_read_capabilities[token] = capability
        return {
            "secret_read_token": token,
            "revision": capability.revision,
            "present": bool(descriptor["present"]),
        }

    def read_secret_capability(self, token: object, *, session_token: str) -> str:
        """Consume a session-bound plaintext-secret lease before reading it."""

        if (
            not isinstance(token, str)
            or not token
            or len(token.encode("utf-8")) > 256
            or not self._valid_session(session_token)
        ):
            raise CoreError("invalid_secret", "The requested secret field is unavailable")
        with self._lock:
            capability = self._secret_read_capabilities.get(token)
            if capability is None or capability.expires_at < time.monotonic():
                self._secret_read_capabilities.pop(token, None)
                raise CoreError("invalid_secret", "The requested secret field is unavailable")
            if not hmac.compare_digest(capability.session_token, session_token):
                raise CoreError("invalid_secret", "The requested secret field is unavailable")
            # A failure still consumes the lease: native code must request a
            # fresh capability after a revision change or transport retry.
            self._secret_read_capabilities.pop(token, None)
        return self.core.trusted_secret_value(
            capability.domain,
            capability.field,
            capability.target,
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
                expected_text_digest=capability.text_digest,
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
            text_digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            # The active editor already owns the text it just staged. Requiring
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
        return {
            **result,
            "domain": replacement.domain,
            "document": replacement.document,
            "editor_token": replacement_token,
            "text": text,
            "baseline": result["baseline"],
        }

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
            self._load_web_sessions()
            self._core_unsubscribe = self.core.subscribe(self._publish)
            self._thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": SERVER_SHUTDOWN_POLL_INTERVAL_SECONDS},
                name="litellm-core-ipc",
                daemon=True,
            )
            self._thread.start()
            return self.endpoint

    def stop(self) -> None:
        with self._lock:
            server = self._server
            self._server = None
            unsubscribe = self._core_unsubscribe
            self._core_unsubscribe = None
            self._sessions.clear()
            self._web_sessions.clear()
            self._web_sessions_written.clear()
            self._subscriptions.clear()
            self._editor_capabilities.clear()
            self._secret_capabilities.clear()
            self._secret_read_capabilities.clear()
            self._import_plans.clear()
            files = list(self._browser_files.values())
            self._browser_files.clear()
            self._bootstrap_token = ""
            self._bootstrap_consumed = True
        if unsubscribe is not None:
            unsubscribe()
        for path in files:
            try:
                path.unlink()
            except OSError:
                pass
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
        """Open one session: native hosts with their token, pages with the password.

        Two hosts, two ways in, and they are not variants of each other:

        - a native host holds the bootstrap credential this process minted and
          handed over the private descriptor, so presenting it is the proof;
        - a browser page presents nothing but what a person typed.  Its cookie
          is issued by `/ipc/web/login` after the web password is accepted, and
          from then on the cookie is the whole identity.

        A page that already signed in keeps its cookie across reloads, so
        `/ipc/hello` accepts it too instead of asking for the password again.
        """

        # Hello never reads a body, and a browser keeps its connection open for
        # the next call: an unconsumed ``{}`` stays in the buffer and the next
        # request line is read back as the method ``{}POST``.  ``_send`` drains
        # it, but only on a path that answers through ``_send`` -- so the body
        # is claimed here, before any early return below.
        try:
            handler._read_body()
        except ProtocolError:
            handler.close_connection = True
        token = handler._authorization()
        cookie = handler._browser_session_cookie()
        bootstrap = self._valid_bootstrap(token)
        # A page that already signed in keeps its cookie across reloads, so
        # ``/ipc/hello`` accepts it too instead of asking for the password
        # again -- including the cookie a replaced Core only knows from its
        # own recorded digest.
        candidate = token or cookie
        valid_session = bool(candidate) and self._valid_session(candidate)
        if bootstrap:
            self._issue_session(handler, token, consumed_bootstrap=True)
            return
        if valid_session:
            self._issue_session(handler, candidate, consumed_bootstrap=False)
            return
        handler._send_error(401, code="unauthorized", message="Core IPC authentication failed")

    def _issue_session(self, handler: _CoreRequestHandler, token: str, *, consumed_bootstrap: bool) -> None:
        with self._lock:
            now = time.monotonic()
            if consumed_bootstrap and hmac.compare_digest(token, self._bootstrap_token):
                self._bootstrap_consumed = True
            existing = self._sessions.get(token)
            if existing is not None:
                # Keep the credential stable: subscriptions and editor
                # revisions are intentionally bound to this session.
                existing.expires_at = now + existing.ttl_seconds
                session_token = existing.token
                if existing.browser:
                    self._record_web_session(session_token)
            else:
                session_token = secrets.token_urlsafe(32)
                self._sessions[session_token] = _Session(
                    token=session_token,
                    expires_at=now + self.session_ttl_seconds,
                    ttl_seconds=self.session_ttl_seconds,
                )
        handler._send(
            200,
            {
                "protocol_version": PROTOCOL_VERSION,
                "ok": True,
                "session": {"expires_in": int(existing.ttl_seconds if existing is not None else self.session_ttl_seconds)},
            },
            session_token=session_token,
        )

    def _handle_web_login(self, handler: _CoreRequestHandler) -> None:
        """Exchange the typed web password for this page's session cookie."""

        try:
            payload = json.loads(handler._read_body() or b"{}")
        except (ValueError, ProtocolError):
            payload = {}
        submitted = payload.get("password") if isinstance(payload, Mapping) else None
        if not isinstance(submitted, str) or not self._accepts_web_password(submitted):
            # A refused password is the one place a page is told it was wrong,
            # so the answer says which failure it is instead of a bare 401 the
            # page would read as an expired session.
            handler._send(401, {"ok": False, "error": {"code": "invalid_password", "message": "Web password is incorrect"}})
            return
        session_token = self._mint_web_session()
        handler._send(200, {"ok": True, "session": {"expires_in": int(self.web_session_ttl_seconds)}}, session_token=session_token)

    def _handle_web_logout(self, handler: _CoreRequestHandler) -> None:
        """End this browser's page session and clear its cookie.

        Signing out has to mean something after a restart, so the lease is
        dropped on disk as well as in memory: a cookie copied out of the
        browser before this call is dead either way.  The answer is idempotent
        -- a page whose cookie is already gone is signed out too -- and it
        clears the cookie the browser is holding, which is what the reload that
        follows reads as "show me the gate".
        """

        with self._lock:
            tokens = {value for value in (handler._authorization(), handler._browser_session_cookie()) if value}
            for token in tokens:
                self._drop_session(token)
        handler._send(200, {"ok": True}, clear_session=True)

    def _handle_web_launch(self, handler: _CoreRequestHandler) -> None:
        """Admit the page a terminal printed the address for.

        The credential is the launch token this process minted and put in that
        one address, so presenting it is the same proof the native hosts make
        with their bootstrap token.  It is accepted repeatedly and for the life
        of this process, because a person who opens the printed URL from a
        terminal expects a reload of that tab to still work.
        """

        try:
            payload = json.loads(handler._read_body() or b"{}")
        except (ValueError, ProtocolError):
            payload = {}
        submitted = payload.get("launch") if isinstance(payload, Mapping) else None
        with self._lock:
            expected = self._web_launch_token
        if (
            not isinstance(submitted, str)
            or expected == ""
            or not hmac.compare_digest(submitted.encode("utf-8"), expected.encode("utf-8"))
        ):
            handler._send(401, {"ok": False, "error": {"code": "invalid_launch", "message": "This launch address is not valid"}})
            return
        session_token = self._mint_web_session()
        handler._send(200, {"ok": True, "session": {"expires_in": int(self.web_session_ttl_seconds)}}, session_token=session_token)

    def _accepts_web_password(self, submitted: str) -> bool:
        expected = self._web_password_value()
        if expected == "":
            # No password is no gate, and a page that answers its own prompt
            # with nothing must not be what opens it.
            return False
        return hmac.compare_digest(submitted.encode("utf-8"), expected.encode("utf-8"))

    def _valid_bootstrap(self, token: str) -> bool:
        """Whether this is the native host's unspent one-shot credential."""

        if not token:
            return False
        with self._lock:
            return bool(
                not self._bootstrap_consumed
                and time.monotonic() <= self._bootstrap_expires_at
                and hmac.compare_digest(token, self._bootstrap_token)
            )

    def _valid_session(self, token: str) -> bool:
        if not token:
            return False
        now = time.monotonic()
        with self._lock:
            session = self._sessions.get(token)
            if session is None and self._resume_web_session(token):
                # A Core that was replaced -- a deploy, the app's own restart --
                # starts with no sessions at all.  The recorded digest is what
                # makes the cookie this browser already holds still mean
                # something, so the page keeps its seat instead of meeting the
                # key form again.
                session = self._sessions.get(token)
            if session is None or session.expires_at < now:
                self._drop_session(token)
                return False
            if not hmac.compare_digest(session.token, token):
                return False
            # Event polling and normal host operations are authenticated
            # activity. Keep their session alive so a healthy Core is never
            # reaped solely because its host has been idle in the foreground.
            session.expires_at = now + session.ttl_seconds
            if session.browser:
                self._record_web_session(token)
            return True

    def _drop_session(self, token: str) -> None:
        """Forget one lease and everything that was minted under it."""

        with self._lock:
            session = self._sessions.pop(token, None)
            recorded = self._web_sessions.pop(browser_session_digest(token), None) is not None
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
            self._secret_read_capabilities = {
                key: item
                for key, item in self._secret_read_capabilities.items()
                if not hmac.compare_digest(item.session_token, token)
            }
            self._import_plans = {
                key: item
                for key, item in self._import_plans.items()
                if not hmac.compare_digest(item.session_token, token)
            }
            # A dead session's event subscriptions go with it.  They were
            # never swept, so every relaunched host left subscriptions (and
            # the events queued in them) behind for the process lifetime,
            # and a poll reusing the id read as a quiet heartbeat.
            #
            # An unknown token has no record to walk: a browser tab that was
            # reloaded, or any caller presenting a token this Core never
            # minted, arrives here with ``session is None``.  Walking its
            # subscriptions raised AttributeError, which aborts the request
            # handler thread and closes the connection with no answer at all —
            # the caller then retries a request that will fail the same way.
            for subscription_id in tuple(session.subscriptions if session is not None else ()):
                subscription = self._subscriptions.get(subscription_id)
                if subscription is not None and hmac.compare_digest(subscription.session_token, token):
                    self._subscriptions.pop(subscription_id, None)
            if recorded:
                self._save_web_sessions(force=True)

    def _mint_web_session(self) -> str:
        """One browser page's lease: 30 days, in memory and recorded."""

        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = _Session(
                token=token,
                expires_at=time.monotonic() + self.web_session_ttl_seconds,
                ttl_seconds=self.web_session_ttl_seconds,
                browser=True,
            )
            self._web_sessions[browser_session_digest(token)] = time.time() + self.web_session_ttl_seconds
            self._save_web_sessions()
        return token

    def _record_web_session(self, token: str) -> None:
        """Move a live browser lease's recorded expiry forward.

        Every authenticated call renews the lease, and the record only has to
        be as fresh as the next restart that reads it: an hour of slack costs
        nothing and keeps the file from being rewritten on every event poll.
        """

        digest = browser_session_digest(token)
        with self._lock:
            expiry = time.time() + self.web_session_ttl_seconds
            if expiry - self._web_sessions.get(digest, 0.0) < WEB_SESSION_PERSIST_INTERVAL_SECONDS:
                return
            self._web_sessions[digest] = expiry
            self._save_web_sessions()

    def _resume_web_session(self, token: str) -> bool:
        """Adopt a browser lease this process did not mint.

        The token is checked against the recorded digest, and the record's own
        expiry decides whether it is still worth anything; a token nothing
        recorded is simply unknown.
        """

        digest = browser_session_digest(token)
        with self._lock:
            expiry = self._web_sessions.get(digest)
            now = time.time()
            if expiry is None:
                return False
            if expiry <= now:
                self._web_sessions.pop(digest, None)
                self._save_web_sessions(force=True)
                return False
            self._sessions[token] = _Session(
                token=token,
                expires_at=time.monotonic() + max(1.0, min(expiry - now, self.web_session_ttl_seconds)),
                ttl_seconds=self.web_session_ttl_seconds,
                browser=True,
            )
            return True

    def _load_web_sessions(self) -> None:
        """Read the browser sessions an earlier process recorded."""

        path = self._web_sessions_path
        if path is None:
            return
        try:
            payload = read_json(path, default={})
        except PersistenceError:
            # An unreadable record is not a reason to refuse to serve a page:
            # the cost is the sessions it held, which is the same page the
            # gate draws for anyone who has not signed in.
            payload = {}
        entries = payload.get("sessions")
        restored: dict[str, float] = {}
        now = time.time()
        if isinstance(entries, Sequence) and not isinstance(entries, (str, bytes, bytearray)):
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                digest = entry.get("digest")
                expires_at = entry.get("expires_at")
                if not isinstance(digest, str) or len(digest) != 64:
                    continue
                if type(expires_at) not in {int, float}:
                    continue
                if float(expires_at) <= now:
                    continue
                restored[digest] = float(expires_at)
        with self._lock:
            self._web_sessions = dict(restored)
            self._web_sessions_written = dict(restored)

    def _save_web_sessions(self, *, force: bool = False) -> None:
        """Write the browser sessions a later process may resume."""

        now = time.time()
        with self._lock:
            live = {digest: expiry for digest, expiry in self._web_sessions.items() if expiry > now}
            if len(live) > MAX_PERSISTED_WEB_SESSIONS:
                newest = sorted(live.items(), key=lambda item: item[1], reverse=True)[:MAX_PERSISTED_WEB_SESSIONS]
                live = dict(newest)
            self._web_sessions = live
            path = self._web_sessions_path
            if path is None:
                return
            if not force and live == self._web_sessions_written:
                return
            payload = {
                "version": WEB_SESSION_STORE_VERSION,
                "sessions": [
                    {"digest": digest, "expires_at": int(expiry)}
                    for digest, expiry in sorted(live.items(), key=lambda item: item[1], reverse=True)
                ],
            }
            try:
                atomic_write_json(path, payload)
            except PersistenceError:
                # A record that cannot be written is bookkeeping, not a
                # request: this page keeps its lease, and only a restart that
                # cannot find the record asks for the key again.
                return
            self._web_sessions_written = dict(live)

    def _register_import_plan(self, prepared: PreparedImport, *, session_token: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = time.monotonic()
            self._import_plans = {
                key: item
                for key, item in self._import_plans.items()
                if item.expires_at >= now and item.session_token in self._sessions
            }
            session_items = [
                item
                for item in self._import_plans.values()
                if hmac.compare_digest(item.session_token, session_token)
            ]
            if len(session_items) >= MAX_IMPORT_PLANS:
                oldest = min(session_items, key=lambda item: item.expires_at)
                self._import_plans.pop(oldest.token, None)
            self._import_plans[token] = _ImportPlanCapability(
                token=token,
                session_token=session_token,
                prepared=prepared,
                expires_at=now + IMPORT_PLAN_TTL_SECONDS,
            )
        return token

    def _consume_import_plan(
        self,
        token: object,
        *,
        session_token: str,
        expected_revision: int | None = None,
        sections: Sequence[object] | None = None,
    ) -> PreparedImport:
        """Validate and consume one import plan for its owning session.

        Revision and subset checks happen before the one-use lease is removed.
        A stale UI therefore gets a deterministic conflict without consuming a
        still-valid preview, while a client from another authenticated session
        can never pop the owner's plan.
        """

        if not isinstance(token, str) or not token or len(token.encode("utf-8")) > 256:
            raise CoreError("invalid_import_plan", "Choose the import file again")
        with self._lock:
            capability = self._import_plans.get(token)
            now = time.monotonic()
            if capability is None:
                raise CoreError("invalid_import_plan", "Choose the import file again")
            if capability.expires_at < now:
                self._import_plans.pop(token, None)
                raise CoreError("invalid_import_plan", "Choose the import file again")
            if not hmac.compare_digest(capability.session_token, session_token):
                raise CoreError("invalid_import_plan", "Choose the import file again")
            if expected_revision is not None:
                if expected_revision != capability.prepared.revision or self.core.revision != capability.prepared.revision:
                    raise RevisionConflict(capability.prepared.revision, self.core.revision)
            if sections is not None:
                if isinstance(sections, (str, bytes, bytearray)) or not isinstance(sections, Sequence) or not sections:
                    raise CoreError("invalid_sections", "Choose at least one detected configuration section")
                requested = tuple(sections)
                if len(set(requested)) != len(requested) or not set(requested).issubset(capability.prepared.detected_sections):
                    raise CoreError("invalid_sections", "Choose only sections detected in the selected file")
            self._import_plans.pop(token, None)
            return capability.prepared

    def _publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subscriptions = tuple(self._subscriptions.values())
        topic = str(event.get("event", ""))
        for subscription in subscriptions:
            if subscription.topics is not None and topic not in subscription.topics:
                # The client named the topics it wants: it is not sent the
                # events it did not ask for.
                continue
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

    def _new_subscription(self, session_token: str, topics: frozenset[str] | None = None) -> str:
        subscription_id = uuid.uuid4().hex
        with self._lock:
            subscription = _Subscription(subscription_id, session_token, topics=topics)
            self._subscriptions[subscription_id] = subscription
            session = self._sessions.get(session_token)
            if session is not None:
                session.subscriptions.add(subscription_id)
        return subscription_id

    def _prepared_snapshot(self, request_id: str) -> "_PreparedResponse | None":
        """The cached answer for this revision, or ``None`` to build one.

        The cache is by revision because that is the store's own statement
        about its projection: the same revision is the same snapshot, so the
        bytes beside it are still its bytes.  A miss leaves the caller to build
        and validate one, and the next call to ``_remember_snapshot`` stores it.
        """

        cached = self._snapshot_body
        if cached is None or cached[0] != self.core.revision:
            return None
        return _PreparedResponse(200, self._snapshot_envelope(request_id, cached[1]))

    def _snapshot_envelope(self, request_id: str, result: bytes) -> bytes:
        prefix = (
            b'{"protocol_version":'
            + str(PROTOCOL_VERSION).encode("ascii")
            + b',"request_id":'
            + _encode_json(request_id)
            + b',"ok":true,"result":'
        )
        return prefix + result + b"}"

    def remember_snapshot_result(self, result: Mapping[str, Any]) -> None:
        """Keep this revision's encoded result, so its next poll is a copy."""

        self._snapshot_body = (self.core.revision, _encode_json(result))

    def next_event(self, subscription_id: str, *, session_token: str, timeout: float) -> dict[str, Any] | None:
        with self._lock:
            subscription = self._subscriptions.get(subscription_id)
            if subscription is None or subscription.session_token != session_token:
                return None
        try:
            return subscription.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def handle_request(self, request: RequestEnvelope, *, session_token: str) -> "ResponseEnvelope | _PreparedResponse":
        if not self._valid_session(session_token):
            return ResponseEnvelope.failure(request.request_id, "unauthorized", "Core IPC authentication failed")
        snapshot_result: dict[str, Any] | None = None
        try:
            params = request.params
            if request.method == "snapshot":
                prepared = self._prepared_snapshot(request.request_id)
                if prepared is not None:
                    return prepared
                result: Any = {"snapshot": self.core.snapshot()}
                snapshot_result = result
            elif request.method == "web_launch_url":
                # The page's own address, with the credential this host has
                # already proved itself for.  Answered over the authenticated
                # protocol rather than a snapshot, so the credential never sits
                # in state every window renders.
                url = self.web_launch_url
                if url == "":
                    raise CoreError("web_unavailable", "The browser page is not being served")
                result = {"url": url}
            elif request.method == "disk_state":
                domains = params.get("domains")
                if not isinstance(domains, list) or not domains or any(not isinstance(domain, str) for domain in domains):
                    raise CoreError("invalid_domain", "The disk-state domains are invalid")
                result = self.core.disk_state(domains)
            elif request.method == "logs":
                tab = params.get("tab")
                known_revision = params.get("revision")
                if not isinstance(tab, str):
                    raise CoreError("logs_unavailable", "Log tab is invalid")
                result = self.core.log_view(
                    tab,
                    known_revision if type(known_revision) is int else None,
                )
            elif request.method == "files":
                # The external-settings listing is read-only: it names the
                # registered client configuration files and never their text.
                result = self.core.client_files()
            elif request.method == "editor":
                if set(params) == {"domain", "document"}:
                    domain = params.get("domain")
                    document = params.get("document")
                    if not isinstance(domain, str) or not isinstance(document, str):
                        raise CoreError("invalid_editor", "The requested editor is unavailable")
                    result = self.register_editor_capability(
                        domain, document, session_token=session_token
                    )
                else:
                    editor_token = params.get("editor_token")
                    text = params.get("text")
                    if not isinstance(editor_token, str) or not isinstance(text, str):
                        raise CoreError("invalid_editor", "The editor document is invalid")
                    result = self.stage_editor_capability(
                        editor_token, text, session_token=session_token
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
                # The topics a subscription names are the events it is sent;
                # no topics means every event.  The vocabulary is validated
                # against the contract, so a subscription cannot ask for an
                # event Core never publishes.
                result = {"subscription_id": self._new_subscription(session_token, frozenset(str(topic) for topic in topics) if topics is not None else None)}
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
            elif request.method == "import_preview":
                source = params.get("source_token")
                if not isinstance(source, str):
                    raise CoreError("invalid_file_capability", "The selected file is unavailable")
                prepared = self.core.prepare_import(
                    source_token=source,
                    revision=params["revision"],
                )
                self.discard_browser_file(source)
                result = {
                    "revision": prepared.revision,
                    "import_plan_token": self._register_import_plan(
                        prepared,
                        session_token=session_token,
                    ),
                    "detected_sections": list(prepared.detected_sections),
                    "preview": dict(prepared.preview),
                }
            elif request.method == "import":
                sections = params.get("sections")
                if sections is not None and (not isinstance(sections, Sequence) or isinstance(sections, (str, bytes, bytearray))):
                    raise CoreError("invalid_sections", "Configuration sections are invalid")
                import_plan_token = params.get("import_plan_token")
                if not isinstance(import_plan_token, str):
                    raise CoreError("invalid_import_plan", "Choose the import file again")
                if sections is None:
                    raise CoreError("invalid_sections", "Choose at least one detected configuration section")
                prepared = self._consume_import_plan(
                    import_plan_token,
                    session_token=session_token,
                    expected_revision=params["revision"],
                    sections=sections,
                )
                result = self.core.import_package(
                    package=prepared.package,
                    sections=sections,
                    revision=prepared.revision,
                )
            else:  # defensive; RequestEnvelope already checks this.
                raise CoreError("unsupported_method", "Unsupported Core operation")
            validate_method_result(request.method, result)
            answer = ResponseEnvelope.success(request.request_id, result)
            if snapshot_result is not None:
                # Validated, so it is also worth keeping: the next poll of this
                # revision is answered from these bytes instead of being built
                # and walked again.
                self.remember_snapshot_result(snapshot_result)
            return answer
        except ConfirmationNeeded as exc:
            return _failure_response(request.request_id, exc.code, exc.message)
        except CoreError as exc:
            return _failure_response(request.request_id, exc.code, exc.message, retryable=exc.code in {"state_unavailable", "service_error"})
        except ProtocolError as exc:
            return _failure_response(request.request_id, exc.code, exc.message, retryable=exc.retryable)
        except Exception:
            return _failure_response(request.request_id, "core_error", "Core operation failed")


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
        self._exchange_session(self._bootstrap_token)

    def _exchange_session(self, credential: str) -> None:
        # An empty byte payload forces POST; ``None`` is reserved for GET
        # event polling in ``_http``.
        status, body, headers = self._http("/ipc/hello", payload=b"", token=credential)
        if status != 200:
            raise IPCError("Core IPC authentication failed")
        session = headers.get("X-LiteLLM-Core-Session", "")
        try:
            envelope = decode_message(body)
        except ProtocolError:
            raise IPCError("Core IPC returned an invalid response") from None
        session_data = envelope.get("session") if isinstance(envelope, Mapping) else None
        expires_in = session_data.get("expires_in") if isinstance(session_data, Mapping) else None
        if (
            not session
            or set(envelope) != {"protocol_version", "ok", "session"}
            or envelope.get("protocol_version") != PROTOCOL_VERSION
            or envelope.get("ok") is not True
            or type(expires_in) is not int
            or expires_in <= 0
        ):
            raise IPCError("Core IPC authentication failed")
        self._session_token = session

    def renew_session(self) -> None:
        """Renew the current authenticated Core session before it expires."""

        self._ensure_session()
        self._exchange_session(self._session_token)

    def call(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        self._ensure_session()
        request = make_request(method, params, request_id=uuid.uuid4().hex)
        status, body, _headers = self._http("/ipc", payload=encode_message(request), token=self._session_token)
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
            "/ipc/host/file-capability",
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

    def subscribe(self, callback: Callable[[dict[str, Any]], None], *, topics: Sequence[str] | None = None) -> Callable[[], None]:
        result = self.call("subscribe", {"topics": list(topics)} if topics is not None else {})
        if not isinstance(result, Mapping) or not isinstance(result.get("subscription_id"), str):
            raise IPCError("Core IPC returned an invalid subscription")
        subscription_id = str(result["subscription_id"])
        stop = threading.Event()

        def poll() -> None:
            while not stop.is_set():
                query = f"/ipc/events?subscription_id={subscription_id}&timeout={EVENT_POLL_TIMEOUT_SECONDS}"
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
    "BROWSER_SESSION_COOKIE",
    "CoreIPCClient",
    "CoreIPCServer",
    "EVENT_POLL_TIMEOUT_SECONDS",
    "IPCError",
    "IpcEndpoint",
    "SESSION_TOKEN_TTL_SECONDS",
    "WEB_SESSION_COOKIE_MAX_AGE_SECONDS",
    "WEB_SESSION_STORE_FILENAME",
    "WEB_SESSION_TTL_SECONDS",
]
