"""Core-owned login credentials for subscription-backed LiteLLM providers.

The UI only exchanges opaque provider metadata and short-lived status.  This
module owns the private token files and the small amount of provider-specific
OAuth orchestration needed by the bundled LiteLLM runtime.  Credential values
never leave this module through a snapshot or ordinary dispatch result.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Any, Mapping

from .persistence import atomic_write_json, read_json
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_LEGACY_OPENAI_SESSION_REF = "chatgpt-account"
_SAFE_STATUS = {"signed_out", "authorizing", "signed_in", "expired", "error", "unsupported"}
_MAX_ERROR_BYTES = 160


def _chatgpt_account_identity(auth: Mapping[str, Any]) -> str:
    """The account an OpenAI credential belongs to, as its own id token states it.

    Only the display identity is taken from the token — never a token, a claim
    beyond the address, or anything the file does not already state — and it is
    returned only when it is a plain, bounded string a pane can print.
    """

    token = auth.get("id_token")
    if not isinstance(token, str) or token.count(".") < 2:
        return ""
    payload = token.split(".")[1]
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(raw.decode("utf-8"))
    except Exception:
        return ""
    if not isinstance(claims, dict):
        return ""
    value = claims.get("email")
    if not isinstance(value, str):
        return ""
    email = value.strip()
    if not email or len(email) > 320 or "@" not in email:
        return ""
    if any(character < " " or character == "\x7f" for character in email):
        return ""
    return email


def _validated_ref(value: object) -> str:
    ref = value.strip() if isinstance(value, str) else ""
    if not _REF_RE.fullmatch(ref):
        raise ValueError("Provider credential reference is invalid")
    return ref


def credential_env_name(credential_ref: str) -> str:
    ref = _validated_ref(credential_ref)
    digest = hashlib.sha256(ref.encode("utf-8")).hexdigest()[:32].upper()
    return f"YOUNG_ROUTER_AUTH_{digest}"


def _safe_error(value: object) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    # Never preserve token-shaped material in an error projection.
    text = re.sub(r"sk-(?:ant-|proj-)?[A-Za-z0-9_.:-]{8,}", "credential", text, flags=re.IGNORECASE)
    return text[:_MAX_ERROR_BYTES]


class ProviderAuthManager:
    """Persist and operate on one app-owned credential profile per ref."""

    def __init__(self, root: Path | str | None = None) -> None:
        configured = Path(root).expanduser() if root is not None else Path.home() / ".young-router"
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
        # One sign-in attempt per credential profile is current; every other
        # attempt for that profile is stale and must stop writing.  The number
        # is the generation, not a flag: LiteLLM's device-code poll cannot be
        # interrupted, so a cancelled attempt keeps its worker alive until the
        # poll times out, and a flag could not tell that worker apart from the
        # attempt that replaced it.
        self._attempts: dict[str, int] = {}
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

    def chatgpt_request_credential(self, credential_ref: str) -> dict[str, str]:
        """The bearer and account id a signed-in ChatGPT account asks with.

        Reading an account's own roster is a request *as that account*, so the
        caller needs its token; Core is the only place the token exists, and it
        stays here — the pane asks for a model list, never for this.
        """

        ref = self._canonical_ref("openai_login", credential_ref)
        auth = read_json(self._secure_chatgpt_auth_file(ref))
        if not isinstance(auth, dict):
            return {}
        token = auth.get("access_token")
        if not isinstance(token, str) or not token:
            return {}
        credential = {"access_token": token}
        account_id = auth.get("account_id")
        if isinstance(account_id, str) and account_id:
            credential["account_id"] = account_id
        return credential

    def status(self, kind: str, credential_ref: str) -> dict[str, Any]:
        ref = self._canonical_ref(kind, credential_ref)
        if kind == "openai_login":
            auth_file = self._secure_chatgpt_auth_file(ref)
            auth = read_json(auth_file)
            if isinstance(auth, dict) and isinstance(auth.get("access_token"), str) and auth.get("access_token"):
                expires_at = auth.get("expires_at")
                # Which account this credential belongs to.  A pane that offers
                # "sign out" over a row that only says "OpenAI" is asking the
                # person to end a session they cannot name, and with several
                # accounts on one provider it cannot be inferred from the row.
                # The id token already states it; nothing else is read from it.
                account = _chatgpt_account_identity(auth)
                identity = {"account": account} if account else {}
                if isinstance(expires_at, (int, float)) and time.time() >= float(expires_at):
                    return {"status": "expired", "configured": True, "active": self.active_openai_ref() == ref, "expires_at": expires_at, **identity}
                return {"status": "signed_in", "configured": True, "active": self.active_openai_ref() == ref, "expires_at": expires_at, **identity}
            with self._lock:
                state = dict(self._states.get(ref, {}))
            result: dict[str, Any] = {
                "status": state.get("status", "signed_out") if state.get("status") in _SAFE_STATUS else "signed_out",
                "configured": False,
                "active": self.active_openai_ref() == ref,
            }
            if state.get("status") == "authorizing":
                # Device-code issuance is asynchronous, so this projection is
                # read while the challenge does not exist yet: ``start`` answers
                # through it before the worker has one, and the host re-reads it
                # to decide when to open its WebView. Indexing the two keys made
                # that first read raise KeyError, which the login operation
                # reports as a failed sign-in -- the flow died before the code
                # it was waiting for was ever requested, and nothing was
                # imported. A missing code is absence, never an error.
                for key in ("user_code", "verification_uri"):
                    value = state.get(key)
                    if isinstance(value, str) and value:
                        result[key] = value
            return result
        return {"status": "signed_out", "configured": False}

    def start(self, kind: str, credential_ref: str) -> dict[str, Any]:
        ref = self._canonical_ref(kind, credential_ref)
        if kind not in {"openai_login"}:
            raise ValueError("Provider login method is unavailable")
        with self._lock:
            # A press of 登录 always starts the sign-in it names.  Refusing to
            # start while an earlier worker was still alive made a cancelled
            # (or closed) attempt swallow every later one: LiteLLM's poll
            # outlives the cancel by up to its own device-code timeout, so the
            # pane sat on 未登录 and the host never received a challenge.  The
            # superseded worker now goes stale instead of blocking the new one.
            generation = self._attempts.get(ref, 0) + 1
            self._attempts[ref] = generation
            self._cancel.discard(ref)
            self._set_state(ref, kind=kind, status="authorizing", configured=False, last_error="")
            thread = threading.Thread(
                target=self._run_chatgpt,
                args=(ref, generation),
                daemon=True,
                name=f"litellm-auth-{ref}",
            )
            self._threads[ref] = thread
            thread.start()
        return self.status(kind, ref)

    def cancel(self, credential_ref: str) -> dict[str, Any]:
        ref = self._cancel_ref(credential_ref)
        with self._lock:
            self._cancel.add(ref)
            # Cancelling supersedes the attempt: a worker that asked to poll
            # before this point must not write the credential it obtains, and
            # must not overwrite the signed-out state this call stores.
            self._attempts[ref] = self._attempts.get(ref, 0) + 1
        try:
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
        return {"CHATGPT_TOKEN_DIR": str(active_root), "CHATGPT_AUTH_FILE": "auth.json"}

    def _is_current(self, ref: str, generation: int) -> bool:
        with self._lock:
            return self._attempts.get(ref, 0) == generation

    def _run_chatgpt(self, ref: str, generation: int) -> None:
        """Run one device-code attempt for ``ref``.

        The poll below is LiteLLM's and cannot be interrupted, so this worker
        outlives its own cancellation by up to a device-code timeout.  Every
        write therefore asks whether this attempt is still the current one, and
        a stale attempt writes nothing -- no state, no credential.
        """

        def current() -> bool:
            return self._is_current(ref, generation)

        try:
            if not current():
                return
            auth_file = self._secure_chatgpt_auth_file(ref, create=True)
            from litellm.llms.chatgpt.authenticator import Authenticator
            from litellm.llms.chatgpt.common_utils import CHATGPT_DEVICE_VERIFY_URL

            # Each attempt owns its Authenticator: the adapter's paths are set
            # on this instance, so no process environment is read and parallel
            # attempts for different accounts cannot collide.
            auth = Authenticator.__new__(Authenticator)
            auth.token_dir = str(self._chatgpt_account_root(ref))
            auth.auth_file = str(auth_file)
            device = auth._request_device_code()
            if not current():
                return
            self._set_state(ref, kind="openai_login", status="authorizing", configured=False, verification_uri=CHATGPT_DEVICE_VERIFY_URL, user_code=device.get("user_code", ""))
            code = auth._poll_for_authorization_code(device)
            if not current():
                return
            tokens = auth._exchange_code_for_tokens(code)
            if not current():
                return
            record = auth._build_auth_record(tokens)
            if not isinstance(record, dict) or not isinstance(record.get("access_token"), str) or not record["access_token"]:
                raise ValueError("ChatGPT login returned an invalid token record")
            with self._lock:
                if self._attempts.get(ref, 0) != generation:
                    return
                atomic_write_json(auth_file, record, mode=0o600)
            self._secure_chatgpt_auth_file(ref)
            if not self.active_openai_ref():
                self._write_active_openai_ref(ref)
            if not current():
                return
            self._set_state(ref, kind="openai_login", status="signed_in", configured=True, expires_at=record.get("expires_at"), user_code="", verification_uri="")
        except Exception as exc:
            if not current():
                return
            self._set_state(ref, kind="openai_login", status="error", configured=False, last_error=_safe_error(exc), user_code="", verification_uri="")
        finally:
            with self._lock:
                if self._threads.get(ref) is threading.current_thread():
                    self._threads.pop(ref, None)
                    self._cancel.discard(ref)

