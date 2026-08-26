"""Core-owned login credentials for subscription-backed LiteLLM providers.

The UI only exchanges opaque provider metadata and short-lived status.  This
module owns the private token files and the small amount of provider-specific
OAuth orchestration needed by the bundled LiteLLM runtime.  Credential values
never leave this module through a snapshot or ordinary dispatch result.
"""

from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from .persistence import atomic_write_json, read_json

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CLAUDE_TOKEN_PREFIX = "sk-ant-oat"
_CLAUDE_TOKEN_RE = re.compile(r"sk-ant-oat[A-Za-z0-9_.:-]{16,8192}")
_CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_CLAUDE_OAUTH_AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
_CLAUDE_OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_CLAUDE_OAUTH_SCOPES = (
    "user:profile",
    "user:inference",
    "user:sessions:claude_code",
    "user:mcp_servers",
    "user:file_upload",
)
_CLAUDE_OAUTH_TIMEOUT_SECONDS = 15 * 60
_LEGACY_OPENAI_SESSION_REF = "chatgpt-account"
_SAFE_STATUS = {"signed_out", "authorizing", "signed_in", "expired", "error", "unsupported"}
_MAX_ERROR_BYTES = 160


def _validated_ref(value: object) -> str:
    ref = value.strip() if isinstance(value, str) else ""
    if not _REF_RE.fullmatch(ref):
        raise ValueError("Provider credential reference is invalid")
    return ref


def credential_env_name(credential_ref: str) -> str:
    ref = _validated_ref(credential_ref)
    digest = hashlib.sha256(ref.encode("utf-8")).hexdigest()[:32].upper()
    return f"LITELLM_MENU_AUTH_{digest}"


def _safe_error(value: object) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    # Never preserve token-shaped material in an error projection.
    text = re.sub(r"sk-(?:ant-|proj-)?[A-Za-z0-9_.:-]{8,}", "credential", text, flags=re.IGNORECASE)
    return text[:_MAX_ERROR_BYTES]


def _valid_claude_token(value: object) -> bool:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 8192:
        return False
    return _CLAUDE_TOKEN_RE.fullmatch(value) is not None and not any(
        char.isspace() or ord(char) < 32 for char in value
    )


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _claude_token_expired(record: dict[str, Any]) -> bool:
    expires_at = record.get("expires_at")
    return isinstance(expires_at, (int, float)) and time.time() >= float(expires_at)


class _ClaudeOAuthCallbackHandler(BaseHTTPRequestHandler):
    """Receive only the loopback OAuth callback; never expose its query data."""

    server: "_ClaudeOAuthCallbackServer"

    def _respond(self, status: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler protocol name
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self._respond(404, "<h1>Not found</h1>")
            return
        params = parse_qs(parsed.query, keep_blank_values=True)
        state = params.get("state", [""])[0]
        if state != self.server.expected_state:
            self._respond(400, "<h1>Sign-in could not be verified</h1>")
            return
        error = params.get("error", [""])[0].strip()
        if error:
            self.server.result = {"error": "authorization_denied"}
            self._respond(400, "<h1>Sign-in was not completed</h1>")
            self.server.wakeup.set()
            return
        code = params.get("code", [""])[0].strip()
        if not code:
            self._respond(400, "<h1>Sign-in did not return an authorization code</h1>")
            return
        self.server.result = {"code": code}
        self._respond(200, "<h1>Sign-in complete</h1><p>You can return to LiteLLM Menu.</p>")
        self.server.wakeup.set()

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _ClaudeOAuthCallbackServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], expected_state: str, wakeup: threading.Event) -> None:
        super().__init__(address, _ClaudeOAuthCallbackHandler)
        self.expected_state = expected_state
        self.wakeup = wakeup
        self.result: dict[str, str] = {}


class ProviderAuthManager:
    """Persist and operate on one app-owned credential profile per ref."""

    def __init__(self, root: Path | str | None = None) -> None:
        configured = Path(root).expanduser() if root is not None else Path.home() / ".litellm-menu"
        self.root = configured / ".litellm-runtime" / "provider-auth"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        # Each OpenAI account gets its own token directory. LiteLLM's ChatGPT
        # adapter can consume only one directory per proxy process; the
        # active-account marker below is therefore an explicit runtime switch,
        # not a claim that multiple accounts are concurrently routable.
        self.chatgpt_root = self.root / "chatgpt"
        self.chatgpt_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.chatgpt_root, 0o700)
        except OSError:
            pass
        self._lock = threading.RLock()
        self._states: dict[str, dict[str, Any]] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._cancel: set[str] = set()
        self._claude_flows: dict[str, dict[str, Any]] = {}
        # LiteLLM's ChatGPT adapter reads CHATGPT_TOKEN_DIR/AUTH_FILE from
        # process-global environment at Authenticator construction time and
        # the proxy itself has one active ChatGPT session. Serialize the
        # short-lived device-code worker and keep each profile independent.
        self._chatgpt_lock = threading.Lock()
        self._active_openai_file = self.root / "active-openai.json"

    def _canonical_ref(self, kind: str, ref: str) -> str:
        del kind
        return _validated_ref(ref)

    def _cancel_ref(self, ref: str) -> str:
        return _validated_ref(ref)

    def _path(self, ref: str) -> Path:
        return self.root / f"{_validated_ref(ref)}.json"

    def _read(self, ref: str) -> dict[str, Any]:
        value = read_json(self._path(ref))
        return dict(value) if isinstance(value, dict) else {}

    def _write(self, ref: str, value: dict[str, Any]) -> None:
        payload = dict(value)
        atomic_write_json(self._path(ref), payload)
        try:
            os.chmod(self._path(ref), 0o600)
        except OSError:
            pass

    def _set_state(self, ref: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            current = self._read(ref)
            current.update(changes)
            current["updated_at"] = int(time.time())
            self._write(ref, current)
            state = {key: value for key, value in current.items() if key not in {"token", "access_token", "refresh_token", "id_token"}}
            self._states[ref] = state
            return dict(state)

    def _chatgpt_account_root(self, credential_ref: str) -> Path:
        ref = _validated_ref(credential_ref)
        if ref == _LEGACY_OPENAI_SESSION_REF:
            return self.chatgpt_root
        return self.chatgpt_root / ref

    def _chatgpt_auth_file(self, credential_ref: str | None = None) -> Path:
        # No-argument form intentionally retains the legacy path for existing
        # installations and migration tests. New account operations always
        # pass their opaque credential reference.
        return self._chatgpt_account_root(
            _LEGACY_OPENAI_SESSION_REF if credential_ref is None else credential_ref
        ) / "auth.json"

    def _secure_chatgpt_auth_file(
        self, credential_ref: str | None = None, *, create: bool = False
    ) -> Path:
        account_root = self._chatgpt_account_root(
            _LEGACY_OPENAI_SESSION_REF if credential_ref is None else credential_ref
        )
        try:
            account_details = account_root.lstat()
        except FileNotFoundError:
            account_details = None
        if account_details is not None and (
            stat.S_ISLNK(account_details.st_mode)
            or not stat.S_ISDIR(account_details.st_mode)
        ):
            raise ValueError("ChatGPT account directory is invalid")
        if create and account_details is None:
            account_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(account_root, 0o700)
            except OSError:
                pass
        path = self._chatgpt_auth_file(credential_ref)
        try:
            details = path.lstat()
        except FileNotFoundError:
            if not create:
                return path
            descriptor: int | None = None
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(descriptor, b"{}\n")
                os.fsync(descriptor)
            except FileExistsError:
                pass
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise ValueError("ChatGPT auth file is invalid")
        try:
            os.chmod(path, 0o600)
        except OSError:
            raise ValueError("ChatGPT auth file permissions are invalid") from None
        return path

    def _read_active_openai_ref(self) -> str:
        value = read_json(self._active_openai_file)
        ref = value.get("credential_ref") if isinstance(value, dict) else ""
        try:
            return _validated_ref(ref)
        except ValueError:
            return ""

    def _write_active_openai_ref(self, credential_ref: str) -> None:
        ref = _validated_ref(credential_ref)
        atomic_write_json(
            self._active_openai_file,
            {"credential_ref": ref, "updated_at": int(time.time())},
            mode=0o600,
        )

    def _clear_active_openai_ref(self, credential_ref: str | None = None) -> None:
        active = self._read_active_openai_ref()
        if credential_ref is not None and active != _validated_ref(credential_ref):
            return
        try:
            self._active_openai_file.unlink()
        except FileNotFoundError:
            pass

    def active_openai_ref(self) -> str:
        """Return the account selected for the single ChatGPT proxy slot."""

        active = self._read_active_openai_ref()
        if active:
            auth_file = self._secure_chatgpt_auth_file(active)
            auth = read_json(auth_file)
            if isinstance(auth, dict) and isinstance(auth.get("access_token"), str) and auth.get("access_token"):
                return active
            self._clear_active_openai_ref(active)
        # Keep pre-multi-account installations usable without silently
        # assigning a newly-created account to the old shared slot.
        legacy = read_json(self._chatgpt_auth_file(_LEGACY_OPENAI_SESSION_REF))
        return _LEGACY_OPENAI_SESSION_REF if isinstance(legacy, dict) and legacy.get("access_token") else ""

    def activate(self, kind: str, credential_ref: str) -> dict[str, Any]:
        if kind != "openai_login":
            raise ValueError("Only OpenAI login accounts have an active runtime slot")
        ref = self._canonical_ref(kind, credential_ref)
        status = self.status(kind, ref)
        if status.get("status") != "signed_in":
            raise ValueError("The OpenAI account is not signed in")
        self._write_active_openai_ref(ref)
        return {"status": "signed_in", "configured": True, "active": True, "credential_ref": ref}

    def status(self, kind: str, credential_ref: str) -> dict[str, Any]:
        ref = self._canonical_ref(kind, credential_ref)
        if kind == "openai_login":
            auth_file = self._secure_chatgpt_auth_file(ref)
            auth = read_json(auth_file)
            if isinstance(auth, dict) and isinstance(auth.get("access_token"), str) and auth.get("access_token"):
                expires_at = auth.get("expires_at")
                if isinstance(expires_at, (int, float)) and time.time() >= float(expires_at):
                    return {"status": "expired", "configured": True, "active": self.active_openai_ref() == ref, "expires_at": expires_at}
                return {"status": "signed_in", "configured": True, "active": self.active_openai_ref() == ref, "expires_at": expires_at}
            with self._lock:
                state = dict(self._states.get(ref, {}))
            return {
                "status": state.get("status", "signed_out") if state.get("status") in _SAFE_STATUS else "signed_out",
                "configured": False,
                "active": self.active_openai_ref() == ref,
                **({"user_code": state["user_code"], "verification_uri": state["verification_uri"]} if state.get("status") == "authorizing" else {}),
            }
        if kind == "claude_login":
            record = self._read(ref)
            with self._lock:
                state = dict(self._states.get(ref, {}))
            transient_status = state.get("status")
            if transient_status in {"authorizing", "signed_out", "error"}:
                result: dict[str, Any] = {
                    "status": transient_status,
                    "configured": False,
                }
                if transient_status == "authorizing":
                    for key in ("verification_uri", "user_code", "redirect_uri"):
                        value = state.get(key)
                        if isinstance(value, str) and value:
                            result[key] = value
                return result
            token = record.get("token")
            if _valid_claude_token(token):
                if _claude_token_expired(record):
                    return {
                        "status": "expired",
                        "configured": True,
                        "expires_at": record.get("expires_at"),
                    }
                return {
                    "status": "signed_in",
                    "configured": True,
                    **({"expires_at": record["expires_at"]} if isinstance(record.get("expires_at"), (int, float)) else {}),
                }
            status = state.get("status", "signed_out")
            result: dict[str, Any] = {
                "status": status if status in _SAFE_STATUS else "signed_out",
                "configured": False,
            }
            if result["status"] == "authorizing":
                for key in ("verification_uri", "user_code", "redirect_uri"):
                    value = state.get(key)
                    if isinstance(value, str) and value:
                        result[key] = value
            return result
        return {"status": "signed_out", "configured": False}

    def import_claude_token(self, credential_ref: str, token: str) -> dict[str, Any]:
        ref = _validated_ref(credential_ref)
        value = token.strip() if isinstance(token, str) else ""
        if not _valid_claude_token(value):
            raise ValueError("Claude OAuth token is invalid")
        with self._lock:
            if ref in self._cancel:
                return {"status": "signed_out", "configured": False}
            self._set_state(
                ref,
                kind="claude_login",
                token=value,
                refresh_token="",
                expires_at=None,
                refresh_token_expires_at=None,
                status="signed_in",
                configured=True,
                last_error="",
                verification_uri="",
                user_code="",
                redirect_uri="",
            )
        return {"status": "signed_in", "configured": True}

    def _prepare_claude_start(self, ref: str) -> None:
        existing = self._read(ref)
        if existing.get("kind") not in (None, "claude_login"):
            raise ValueError("Provider credential profile belongs to another login method")
        state = {
            "kind": "claude_login",
            "status": "authorizing",
            "configured": False,
            "last_error": "",
            "updated_at": int(time.time()),
        }
        self._write(
            ref,
            state,
        )
        self._states[ref] = dict(state)

    def start(self, kind: str, credential_ref: str) -> dict[str, Any]:
        ref = self._canonical_ref(kind, credential_ref)
        if kind not in {"openai_login", "claude_login"}:
            raise ValueError("Provider login method is unavailable")
        with self._lock:
            existing = self._threads.get(ref)
            if existing is not None and existing.is_alive():
                return self.status(kind, ref)
            self._cancel.discard(ref)
            if kind == "claude_login":
                self._prepare_claude_start(ref)
            else:
                self._set_state(ref, kind=kind, status="authorizing", configured=False, last_error="")
            target = self._run_chatgpt if kind == "openai_login" else self._run_claude
            thread = threading.Thread(target=target, args=(ref,), daemon=True, name=f"litellm-auth-{ref}")
            self._threads[ref] = thread
            thread.start()
        return self.status(kind, ref)

    def cancel(self, credential_ref: str) -> dict[str, Any]:
        ref = self._cancel_ref(credential_ref)
        with self._lock:
            self._cancel.add(ref)
            flow = self._claude_flows.get(ref)
        if flow is not None:
            wakeup = flow.get("wakeup")
            if isinstance(wakeup, threading.Event):
                wakeup.set()
        try:
            record = self._read(ref)
            if record.get("kind") == "claude_login":
                self._write(
                    ref,
                    {
                        "kind": "claude_login",
                        "status": "signed_out",
                        "configured": False,
                        "last_error": "",
                        "updated_at": int(time.time()),
                    },
                )
                with self._lock:
                    self._states[ref] = {
                        "kind": "claude_login",
                        "status": "signed_out",
                        "configured": False,
                    }
            else:
                self._set_state(ref, status="signed_out", configured=False, user_code="", verification_uri="")
        except Exception:
            pass
        return {"status": "signed_out", "configured": False}

    def logout(self, kind: str, credential_ref: str) -> dict[str, Any]:
        ref = self._canonical_ref(kind, credential_ref)
        record = self._read(ref)
        record_kind = record.get("kind")
        if record_kind not in (None, kind):
            raise ValueError("Provider credential profile belongs to another login method")
        self.cancel(ref)
        if kind == "openai_login":
            auth_file = self._secure_chatgpt_auth_file(ref)
            try:
                auth_file.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                raise ValueError("ChatGPT credentials could not be removed") from None
            self._clear_active_openai_ref(ref)
        try:
            self._path(ref).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            raise ValueError("Provider credentials could not be removed") from None
        with self._lock:
            self._states.pop(ref, None)
        return {"status": "signed_out", "configured": False}

    def environment(self) -> dict[str, str]:
        active_ref = self.active_openai_ref()
        active_root = self._chatgpt_account_root(active_ref or _LEGACY_OPENAI_SESSION_REF)
        env: dict[str, str] = {"CHATGPT_TOKEN_DIR": str(active_root), "CHATGPT_AUTH_FILE": "auth.json"}
        for path in self.root.glob("*.json"):
            if path.name == "auth.json":
                continue
            record = read_json(path)
            if not isinstance(record, dict) or record.get("kind") != "claude_login":
                continue
            token = record.get("token")
            if _valid_claude_token(token) and not _claude_token_expired(record):
                ref = path.stem
                try:
                    env[credential_env_name(ref)] = token
                except ValueError:
                    continue
        return env

    def _is_cancelled(self, ref: str) -> bool:
        with self._lock:
            return ref in self._cancel

    def _run_chatgpt(self, ref: str) -> None:
        with self._chatgpt_lock:
            try:
                if self._is_cancelled(ref):
                    self._set_state(ref, kind="openai_login", status="signed_out", configured=False)
                    return
                auth_file = self._secure_chatgpt_auth_file(ref, create=True)
                from litellm.llms.chatgpt.authenticator import Authenticator
                from litellm.llms.chatgpt.common_utils import CHATGPT_DEVICE_VERIFY_URL

                # LiteLLM 1.97's Authenticator reads its paths from env only
                # in __init__. Configure the object directly so a device-code
                # flow never mutates the Core/proxy process environment.
                auth = Authenticator.__new__(Authenticator)
                auth.token_dir = str(self._chatgpt_account_root(ref))
                auth.auth_file = str(auth_file)
                device = auth._request_device_code()
                self._set_state(ref, kind="openai_login", status="authorizing", configured=False, verification_uri=CHATGPT_DEVICE_VERIFY_URL, user_code=device.get("user_code", ""))
                if self._is_cancelled(ref):
                    self._set_state(ref, status="signed_out", configured=False)
                    return
                code = auth._poll_for_authorization_code(device)
                if self._is_cancelled(ref):
                    self._set_state(ref, status="signed_out", configured=False)
                    return
                tokens = auth._exchange_code_for_tokens(code)
                if self._is_cancelled(ref):
                    self._set_state(ref, status="signed_out", configured=False)
                    return
                record = auth._build_auth_record(tokens)
                if not isinstance(record, dict) or not isinstance(record.get("access_token"), str) or not record["access_token"]:
                    raise ValueError("ChatGPT login returned an invalid token record")
                with self._lock:
                    if ref in self._cancel:
                        self._set_state(ref, status="signed_out", configured=False)
                        return
                    atomic_write_json(auth_file, record, mode=0o600)
                    if ref in self._cancel:
                        try:
                            auth_file.unlink()
                        except FileNotFoundError:
                            pass
                        self._set_state(ref, status="signed_out", configured=False)
                        return
                self._secure_chatgpt_auth_file(ref)
                if not self.active_openai_ref():
                    self._write_active_openai_ref(ref)
                self._set_state(ref, kind="openai_login", status="signed_in", configured=True, expires_at=record.get("expires_at"), user_code="", verification_uri="")
            except Exception as exc:
                if self._is_cancelled(ref):
                    self._set_state(ref, kind="openai_login", status="signed_out", configured=False, user_code="", verification_uri="")
                else:
                    self._set_state(ref, kind="openai_login", status="error", configured=False, last_error=_safe_error(exc), user_code="", verification_uri="")
            finally:
                with self._lock:
                    if self._threads.get(ref) is threading.current_thread():
                        self._threads.pop(ref, None)
                    self._cancel.discard(ref)

    @staticmethod
    def _claude_authorization_url(
        *, port: int, state: str, code_challenge: str
    ) -> tuple[str, str]:
        redirect_uri = f"http://localhost:{port}/callback"
        params = {
            "code": "true",
            "client_id": _CLAUDE_OAUTH_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(_CLAUDE_OAUTH_SCOPES),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        return f"{_CLAUDE_OAUTH_AUTHORIZE_URL}?{urlencode(params)}", redirect_uri

    @staticmethod
    def _exchange_claude_code(
        *, code: str, code_verifier: str, state: str, redirect_uri: str
    ) -> dict[str, Any]:
        payload = json.dumps(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": _CLAUDE_OAUTH_CLIENT_ID,
                "code_verifier": code_verifier,
                "state": state,
            }
        ).encode("utf-8")
        request = Request(
            _CLAUDE_OAUTH_TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read(256 * 1024)
                status = int(response.status)
        except HTTPError as exc:
            raise ValueError(f"token_exchange_http_{exc.code}") from None
        except (URLError, TimeoutError, OSError):
            raise ValueError("token_exchange_network") from None
        if status != 200:
            raise ValueError(f"token_exchange_http_{status}")
        try:
            result = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("token_exchange_invalid_response") from None
        if not isinstance(result, dict):
            raise ValueError("token_exchange_invalid_response")
        return result

    def _run_claude(self, ref: str) -> None:
        server: _ClaudeOAuthCallbackServer | None = None
        server_thread: threading.Thread | None = None
        try:
            if self._is_cancelled(ref):
                self._set_state(ref, kind="claude_login", status="signed_out", configured=False)
                return
            code_verifier = _base64url(secrets.token_bytes(32))
            code_challenge = _base64url(hashlib.sha256(code_verifier.encode("ascii")).digest())
            state = _base64url(secrets.token_bytes(32))
            wakeup = threading.Event()
            server = _ClaudeOAuthCallbackServer(("127.0.0.1", 0), state, wakeup)
            authorization_url, redirect_uri = self._claude_authorization_url(
                port=int(server.server_address[1]),
                state=state,
                code_challenge=code_challenge,
            )
            server_thread = threading.Thread(
                target=server.serve_forever,
                daemon=True,
                name=f"litellm-claude-oauth-callback-{ref}",
            )
            with self._lock:
                self._claude_flows[ref] = {
                    "server": server,
                    "wakeup": wakeup,
                    "redirect_uri": redirect_uri,
                }
            server_thread.start()
            self._set_state(
                ref,
                kind="claude_login",
                status="authorizing",
                configured=False,
                verification_uri=authorization_url,
                user_code="",
                redirect_uri=redirect_uri,
                auth_method="web_oauth",
                last_error="",
            )
            with self._lock:
                private = self._read(ref)
                for key in ("verification_uri", "user_code", "redirect_uri", "auth_method"):
                    private.pop(key, None)
                self._write(ref, private)
            deadline = time.monotonic() + _CLAUDE_OAUTH_TIMEOUT_SECONDS
            while not wakeup.wait(timeout=0.25):
                if self._is_cancelled(ref):
                    self._set_state(ref, kind="claude_login", status="signed_out", configured=False)
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError("oauth_timeout")
            if self._is_cancelled(ref):
                self._set_state(ref, kind="claude_login", status="signed_out", configured=False)
                return
            callback_result = dict(server.result)
            if callback_result.get("error"):
                raise ValueError("authorization_denied")
            code = callback_result.get("code", "")
            if not isinstance(code, str) or not code:
                raise ValueError("authorization_code_missing")
            tokens = self._exchange_claude_code(
                code=code,
                code_verifier=code_verifier,
                state=state,
                redirect_uri=redirect_uri,
            )
            access_token = tokens.get("access_token")
            if not _valid_claude_token(access_token):
                raise ValueError("oauth_token_invalid")
            expires_in = tokens.get("expires_in")
            expires_at = (
                time.time() + float(expires_in)
                if isinstance(expires_in, (int, float)) and float(expires_in) > 0
                else None
            )
            refresh_token = tokens.get("refresh_token")
            if not isinstance(refresh_token, str):
                refresh_token = ""
            refresh_expires_in = tokens.get("refresh_token_expires_in")
            refresh_expires_at = (
                time.time() + float(refresh_expires_in)
                if isinstance(refresh_expires_in, (int, float)) and float(refresh_expires_in) > 0
                else None
            )
            scopes = tokens.get("scope")
            if not isinstance(scopes, str):
                scopes = " ".join(_CLAUDE_OAUTH_SCOPES)
            with self._lock:
                if ref in self._cancel:
                    self._set_state(ref, status="signed_out", configured=False)
                    return
                self._write(
                    ref,
                    {
                        "kind": "claude_login",
                        "token": access_token,
                        "refresh_token": refresh_token,
                        "expires_at": expires_at,
                        "refresh_token_expires_at": refresh_expires_at,
                        "scopes": scopes,
                        "client_id": _CLAUDE_OAUTH_CLIENT_ID,
                        "status": "signed_in",
                        "configured": True,
                        "last_error": "",
                        "updated_at": int(time.time()),
                    },
                )
                self._states[ref] = {
                    "kind": "claude_login",
                    "status": "signed_in",
                    "configured": True,
                    "last_error": "",
                    "updated_at": int(time.time()),
                }
        except TimeoutError:
            if self._is_cancelled(ref):
                self._set_state(ref, kind="claude_login", status="signed_out", configured=False)
            else:
                self._set_state(ref, kind="claude_login", status="error", configured=False, last_error="timeout")
        except Exception as exc:
            if self._is_cancelled(ref):
                self._set_state(ref, kind="claude_login", status="signed_out", configured=False)
            else:
                self._set_state(ref, kind="claude_login", status="error", configured=False, last_error=_safe_error(exc))
        finally:
            if server is not None:
                try:
                    server.shutdown()
                except Exception:
                    pass
                try:
                    server.server_close()
                except Exception:
                    pass
            if server_thread is not None and server_thread is not threading.current_thread():
                server_thread.join(timeout=1)
            with self._lock:
                if self._claude_flows.get(ref, {}).get("server") is server:
                    self._claude_flows.pop(ref, None)
                if self._threads.get(ref) is threading.current_thread():
                    self._threads.pop(ref, None)
                self._cancel.discard(ref)

    def token(self, credential_ref: str) -> str:
        ref = _validated_ref(credential_ref)
        record = self._read(ref)
        value = record.get("token")
        return value if _valid_claude_token(value) else ""
