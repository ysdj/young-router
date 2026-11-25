"""Core-owned login credentials for subscription-backed LiteLLM providers.

The UI only exchanges opaque provider metadata and short-lived status.  This
module owns the private token files and the small amount of provider-specific
OAuth orchestration needed by the bundled LiteLLM runtime.  Credential values
never leave this module through a snapshot or ordinary dispatch result.
"""

from __future__ import annotations

import base64
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
#: How long one failed refresh keeps the next attempt from being made.
#:
#: Every snapshot projection reads each account's status, so an account whose
#: refresh token the issuer refuses would otherwise send a token refresh on
#: every render of the provider pane, the log view, and the scheduled check.
#: The wait is the pane's own backoff, not a verdict about the sign-in: the
#: person's next explicit re-login (`start`) clears it.
_REFRESH_BACKOFF_SECONDS = 60


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
        # One sign-in attempt per credential profile is current; every other
        # attempt for that profile is stale and must stop writing.  The number
        # is the generation, not a flag: LiteLLM's device-code poll cannot be
        # interrupted, so a cancelled attempt keeps its worker alive until the
        # poll times out, and a flag could not tell that worker apart from the
        # attempt that replaced it.
        self._attempts: dict[str, int] = {}
        # One refresh at a time per account, and when each last failed.  A
        # refresh rotates the refresh token, so two concurrent attempts with
        # the same old token would race and one of them would be refused for
        # good; the failed-at stamp is the backoff above.
        self._refreshing: dict[str, threading.Lock] = {}
        self._refresh_failed_at: dict[str, float] = {}
        # The result of the last successful renew: Core is built once per service
        # and several of its readers project the same credential, so the answer
        # is stated here instead of being re-verified by every projection.
        self._refresh_settled: dict[str, dict[str, Any]] = {}
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

    def active_openai_ref(
        self, known_ref: str = "", known_auth: Mapping[str, Any] | None = None
    ) -> str:
        """Return the account selected for the single ChatGPT proxy slot.

        ``status`` passes the credential it has just read; reading that same
        file again only to learn whether it is the active one doubles the I/O
        for every projected provider row.
        """

        active = self._read_active_openai_ref()
        if active:
            if active == known_ref and known_auth is not None:
                auth = known_auth
            else:
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

    def _refresh_lock(self, ref: str) -> threading.Lock:
        with self._lock:
            lock = self._refreshing.get(ref)
            if lock is None:
                lock = threading.Lock()
                self._refreshing[ref] = lock
            return lock

    def _refresh_chatgpt_tokens(self, ref: str, *, wait: bool = True) -> dict[str, Any] | None:
        """Redeem this account's refresh token, and return its new record.

        The access token a ChatGPT sign-in minted lasts hours, the refresh
        token lasts weeks: a fleet that only ever read ``expires_at`` therefore
        reported every account as expired as soon as the first token aged out,
        even though the grant behind it was still good.  The exchange is
        LiteLLM's own (it is the same call the proxy adapter makes when a
        request finds the token stale), and the record it writes is the shape
        the adapter reads back.

        ``wait`` is the caller's patience, not a preference.  A projection that
        answers a snapshot (``status``) passes ``False``: it starts the renewal
        and returns at once, because a snapshot is read while a person waits
        for a window, and an unreachable issuer would otherwise hold that
        window for the exchange's whole timeout.  A request that is *about* the
        credential (a model listing, a probe) passes ``True`` and gets the new
        token, since it could not ask the upstream without one.

        Returns the refreshed record, or ``None`` when this account cannot be
        refreshed right now (no refresh token, an exchange the issuer refused,
        a refresh that just failed and is still backing off, or one still in
        flight for a caller that does not wait).  Nothing here ever raises: a
        status projection must still answer when the network does not.
        """

        with self._lock:
            failed_at = self._refresh_failed_at.get(ref)
        if failed_at is not None and time.time() - failed_at < _REFRESH_BACKOFF_SECONDS:
            return None
        # Plain lookups only while the caller's own lock is not held: the record
        # below is read under this manager's lock, and nesting the two orders
        # would be a deadlock waiting for a second thread.
        with self._lock:
            settled = self._refresh_settled.get(ref)
        if isinstance(settled, dict):
            settled_expiry = settled.get("expires_at")
            if isinstance(settled_expiry, (int, float)) and time.time() < float(settled_expiry) - 60:
                # This exact credential was just renewed; asking the issuer again
                # would rotate the token once more for no reason.
                return dict(settled)
        # The account's own directory is audited before the exchange, exactly as
        # every other read of it is: a symlinked account directory is refused
        # rather than written through.
        try:
            auth_file = self._secure_chatgpt_auth_file(ref)
            stored = read_json(auth_file)
        except Exception:
            return None
        if not isinstance(stored, dict):
            return None
        refresh_token = stored.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            return None
        lock = self._refresh_lock(ref)
        if not wait:
            # One renewal per account is in flight at most, and the caller is
            # told nothing yet: the row keeps reading 登录已过期 for the second or
            # two the exchange takes, and the next snapshot states the answer.
            if lock.locked():
                return None
            threading.Thread(
                target=self._refresh_chatgpt_tokens,
                args=(ref,),
                daemon=True,
                name=f"litellm-refresh-{ref}",
            ).start()
            return None
        if not lock.acquire(blocking=False):
            # Another caller is already redeeming this grant; its answer is the
            # one to read, so wait for that lock rather than start a second
            # exchange with the same old token.
            lock.acquire()
            lock.release()
            with self._lock:
                record = self._refresh_settled.get(ref)
            return dict(record) if isinstance(record, dict) else None
        try:
            # Re-read under the lock: the token may have rotated while this
            # caller waited for its own lock to be free.
            stored = read_json(auth_file)
            refresh_token = stored.get("refresh_token") if isinstance(stored, dict) else None
            if not isinstance(refresh_token, str) or not refresh_token:
                return None
            expires_at = stored.get("expires_at")
            if isinstance(expires_at, (int, float)) and time.time() < float(expires_at) - 60:
                # Another caller refreshed it between the two reads.
                return dict(stored)
            # LiteLLM's adapter cannot refresh from inside a running event loop
            # worker; this is an ordinary Core thread, which can.  The exchange
            # is called directly rather than through ``get_access_token``:
            # that method falls back to a *device-code login*, which waits for a
            # person and would hold this thread for the poll's whole lifetime
            # from inside what is supposed to be a status read.
            from litellm.llms.chatgpt.authenticator import Authenticator

            auth = Authenticator.__new__(Authenticator)
            auth.token_dir = str(auth_file.parent)
            auth.auth_file = str(auth_file)
            auth._refresh_tokens(refresh_token)
            record = read_json(auth_file)
            if not isinstance(record, dict) or not record.get("access_token"):
                return None
            with self._lock:
                self._refresh_failed_at.pop(ref, None)
                self._refresh_settled[ref] = dict(record)
            return dict(record)
        except Exception:
            with self._lock:
                self._refresh_failed_at[ref] = time.time()
            return None
        finally:
            lock.release()

    def _status_from_auth(self, ref: str, auth: Mapping[str, Any]) -> dict[str, Any]:
        """The projection of one readable credential, refreshed if it aged out.

        A credential whose access token has expired is renewed once before it
        is called expired: the token is short-lived by design and the grant
        behind it usually is not, so "expired" is the answer only after the
        redemption the adapter itself would have made has been tried and
        refused.  A refresh that fails is why the person sees 登录已过期 and the
        pane's re-login action.
        """

        # Which account this credential belongs to.  A pane that offers "sign
        # out" over a row that only says "OpenAI" is asking the person to end a
        # session they cannot name, and with several accounts on one provider it
        # cannot be inferred from the row.  The id token already states it;
        # nothing else is read from it.
        account = _chatgpt_account_identity(auth)
        identity = {"account": account} if account else {}
        active = self.active_openai_ref(ref, auth) == ref
        # Backoff is asked of the refresh itself; reading it here as well made
        # the expired branch skip the very call that would have cleared it.
        expires_at = auth.get("expires_at")
        if isinstance(expires_at, (int, float)) and time.time() >= float(expires_at):
            # The renewal starts here and reports on a later read: this method
            # answers a snapshot, and a snapshot may not wait on the network.
            refreshed = self._refresh_chatgpt_tokens(ref, wait=False)
            if refreshed is not None:
                auth = refreshed
                account = _chatgpt_account_identity(auth)
                identity = {"account": account} if account else {}
                expires_at = auth.get("expires_at")
        if isinstance(expires_at, (int, float)) and time.time() >= float(expires_at):
            return {"status": "expired", "configured": True, "active": active, "expires_at": expires_at, **identity}
        return {"status": "signed_in", "configured": True, "active": active, "expires_at": expires_at, **identity}

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
        expires_at = auth.get("expires_at")
        if isinstance(expires_at, (int, float)) and time.time() >= float(expires_at):
            # The caller is about to ask the upstream as this account.  An
            # access token the issuer already aged out answers 401 there, which
            # the pane can only read as "the login is gone"; the refresh the
            # adapter would make on that same request is made here first, so a
            # listing or a probe is not refused over a token that was still
            # renewable.
            refreshed = self._refresh_chatgpt_tokens(ref)
            if isinstance(refreshed, dict):
                auth = refreshed
        token = auth.get("access_token")
        if not isinstance(token, str) or not token:
            return {}
        credential = {"access_token": token}
        account_id = auth.get("account_id")
        if isinstance(account_id, str) and account_id:
            credential["account_id"] = account_id
        return credential

    def _pending_authorization(self, ref: str) -> dict[str, Any] | None:
        """The challenge of a sign-in this account is waiting on, or nothing.

        Only a worker that is still alive counts.  A record left in
        ``authorizing`` by a process that has since restarted, or by an attempt
        whose poll timed out, describes no live challenge: reporting it would
        print a device code nobody is waiting for and hide the credential's real
        state behind it.
        """

        with self._lock:
            thread = self._threads.get(ref)
            state = dict(self._states.get(ref, {}))
        if thread is None or not thread.is_alive() or state.get("status") != "authorizing":
            return None
        result: dict[str, Any] = {"status": "authorizing", "configured": False, "active": False}
        for key in ("user_code", "verification_uri"):
            value = state.get(key)
            if isinstance(value, str) and value:
                result[key] = value
        # Keep saying who is signing in.  The challenge replaces the credential's
        # own state, and leaving the account id out made the row that a person
        # was re-authorizing render with no address at all -- the list said
        # 正在等待登录 beside an empty name, which reads as a different, nameless
        # account.  The identity is not part of the challenge and does not
        # depend on the sign-in finishing; it comes from the credential still on
        # disk, which is the account this attempt belongs to.
        identity = self._pending_account_identity(ref)
        if identity:
            result["account"] = identity
        return result

    def _pending_account_identity(self, ref: str) -> str:
        """The account a sign-in belongs to, read without touching the network."""

        try:
            auth = read_json(self._secure_chatgpt_auth_file(ref))
        except Exception:
            return ""
        return _chatgpt_account_identity(auth) if isinstance(auth, dict) else ""

    def status(self, kind: str, credential_ref: str) -> dict[str, Any]:
        ref = self._canonical_ref(kind, credential_ref)
        if kind == "openai_login":
            auth_file = self._secure_chatgpt_auth_file(ref)
            auth = read_json(auth_file)
            if isinstance(auth, dict) and isinstance(auth.get("access_token"), str) and auth.get("access_token"):
                # A sign-in in progress outranks the credential it is replacing.
                # 重新登录 on an expired account starts a device code while the old
                # (unusable) credential is still on disk; reading the file first
                # kept reporting that grant as expired, so the pane never saw
                # 正在等待登录, never showed the code, and the sign-in it had just
                # started could not be finished.
                pending = self._pending_authorization(ref)
                if pending is not None:
                    return pending
                return self._status_from_auth(ref, auth)
            with self._lock:
                state = dict(self._states.get(ref, {}))
            result: dict[str, Any] = {
                "status": state.get("status", "signed_out") if state.get("status") in _SAFE_STATUS else "signed_out",
                "configured": False,
                "active": self.active_openai_ref(ref, auth) == ref,
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
            # A press of 登录 is the person's own answer to a session this
            # account could not renew: the backoff that kept silent refreshes
            # from repeating belongs to the attempt that failed, not to this
            # one.
            self._refresh_failed_at.pop(ref, None)
            self._refresh_settled.pop(ref, None)
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
            # A credential that no longer exists has neither a settled renew nor
            # a backoff: a later sign-in to the same reference starts clean.
            self._refresh_settled.pop(ref, None)
            self._refresh_failed_at.pop(ref, None)
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
            # The credential and the account slot it names are written in one
            # critical section, and the slot is claimed only while this attempt
            # is still current: a cancel landing between the two writes would
            # otherwise select the account the person just cancelled.
            with self._lock:
                if self._attempts.get(ref, 0) != generation:
                    return
                atomic_write_json(auth_file, record, mode=0o600)
                self._secure_chatgpt_auth_file(ref)
                if self._attempts.get(ref, 0) != generation:
                    return
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

