"""The Python Core state source shared by both native shells.

``CoreStore`` owns the lifecycle around domain adapters: drafts are staged in
the adapter, validation and confirmation happen here, and only ``apply`` is
allowed to cross a persistence boundary.  A domain adapter may wrap one of
the existing Python modules (providers, Codex, WebDAV, runtime settings) or a
new domain such as Claude/language.  It must not be duplicated in React.

The store intentionally has no UI or shell-script dependency.  It is usable
in unit tests and from the local IPC server without importing LiteLLM itself.
"""

from __future__ import annotations

import contextlib
from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import dataclass, field
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from .emitter import CoalescingEmitter
from .entity_ops import MAX_PATCH_OPS, EntityOp, domain_diff
from .log_tabs import LOG_TABS
from .oplog import note as _note
from .persistence import AtomicJSONStore, PersistenceError, atomic_write_bytes, atomic_write_json, read_bytes, read_json
from .protocol import (
    MAX_EDITOR_DOCUMENT_BYTES,
    PATCH_TOP_LEVEL_FIELDS,
    PROTOCOL_VERSION,
    ProtocolError,
    _encode_json,
    is_error_code,
    make_event,
    make_job_event,
    make_patch_event,
    make_resync_event,
)
from .security import REDACTED, _key_text, redact, safe_exception_message, safe_error_message


CORE_METADATA_VERSION = 1
PACKAGE_FORMAT = "young-router-core-package"
PACKAGE_VERSION = 1
DOMAIN_FILE_FORMAT = "young-router-domain-settings"
DOMAIN_FILE_VERSION = 1
SUBSCRIPTION_QUEUE_LIMIT = 32

#: How many long-running operations may be in flight at once.  They are the
#: operations that used to hold the write lane across a network round trip --
#: a probe of one provider, a probe of a station -- so two of them is what a
#: person actually starts; more would only multiply the requests one station
#: sees.  A queued job still answers with its id immediately.
JOB_POOL_WORKERS = 2


class _PatchUnavailable(Exception):
    """A revision moved in a way no entity-level patch can describe."""

# How often the thread that ends abandoned sign-ins looks.  Shorter than the
# idle timeout by a wide margin so a forgotten browser is closed within a tick
# or two of going quiet, and cheap enough to run unconditionally: the sweep is
# one lock and a subtraction per sign-in.
BROWSER_LOGIN_REAP_SECONDS = 15.0

_SECRET_FIELDS: dict[tuple[str, str], bool] = {
    ("providers_models", "api_key"): True,
    # A Qoder PAT is a real upstream credential, but it belongs to the Qoder
    # worker's own store rather than to the provider key editor: the wizard
    # stages it under this field, the sign-in action hands it to the worker,
    # and the worker writes it to its 0600 file.  Keying the slot by region
    # means one window's paste can never answer for the other region.
    ("providers_models", "qoder_pat"): True,
    ("codex", "api_key"): False,
    ("claude", "deployment_token"): False,
    ("claude", "desktop_gateway_api_key"): False,
    ("claude", "auto_memory_directory"): False,
    ("runtime", "setting"): True,
    ("webdav", "password"): False,
    ("relay_accounts", "api_key"): True,
    ("relay_accounts", "password"): True,
    # The dashboard session, keyed by the account id.  It is read only by the
    # native host, through the one-time lease, so a window it opens itself can
    # install the login the user already made.
    ("relay_accounts", "session"): True,
}

# How long a plaintext read waits for the store lock before reading the live
# document instead.  The field is on screen already; a relay round trip that
# holds the lock must not hold the key back with it.
_SECRET_READ_LOCK_TIMEOUT_SECONDS = 0.05
_PLAINTEXT_SECRET_FIELDS = {
    ("providers_models", "api_key"),
    ("codex", "api_key"),
    ("claude", "deployment_token"),
    ("claude", "desktop_gateway_api_key"),
    ("relay_accounts", "api_key"),
    ("relay_accounts", "password"),
    ("relay_accounts", "session"),
    ("runtime", "setting"),
}

_MULTILINE_SECRET_TARGETS = frozenset({
    "YOUNG_ROUTER_DSH_VISION_ROUTER_CONFIG_JSON",
})
#: The runtime slots a native control may draw in the clear.  A proxy key is
#: the credential this app hands the user's own clients, so its field shows it
#: instead of a placeholder; the read still goes over the read-once native lease
#: and never through a snapshot.
_PLAINTEXT_RUNTIME_TARGETS = _MULTILINE_SECRET_TARGETS | {
    "YOUNG_ROUTER_MASTER_KEY",
    # The page's key is read in the clear for the same reason the proxy key is:
    # a person copies it into another browser's password form.
    "YOUNG_ROUTER_WEB_PASSWORD",
}
_DSH_VISION_ROUTER_CONFIG_MAX_BYTES = 256 * 1024


def _allows_multiline_secret(domain: str, field: str, target: str | None) -> bool:
    return domain == "runtime" and field == "setting" and target in _MULTILINE_SECRET_TARGETS


def _secret_value_limit(domain: str, field: str, target: str | None) -> int:
    if domain == "runtime" and field == "setting" and target == "YOUNG_ROUTER_DSH_VISION_ROUTER_CONFIG_JSON":
        return _DSH_VISION_ROUTER_CONFIG_MAX_BYTES
    return 16_384

# Keep the Core's direct and legacy IPC callers tolerant of route-shaped
# domain names.  The unified UI emits canonical names, but older native
# clients may still send the settings route ids.
_DOMAIN_ALIASES = {
    "providers-models": "providers_models",
    "providers_models": "providers_models",
    "codex-settings": "codex",
    "codex_settings": "codex",
    "claude-settings": "claude",
    "claude_settings": "claude",
    "runtime-settings": "runtime",
    "runtime_settings": "runtime",
    "webdav-settings": "webdav",
    "webdav_settings": "webdav",
    "relay-accounts": "relay_accounts",
    "relay_accounts": "relay_accounts",
    "language": "language",
    "logs": "logs",
}

SERVICE_STATES = frozenset({"starting", "running", "unhealthy", "stopped", "unknown"})
# Domains whose own ``apply`` validates the whole staged draft before it writes
# anything, so Core's pre-Apply pass would ask the same question twice.  It is a
# list, not a guess: a domain that stops validating itself must be removed
# here, and the provider domain's Apply both validates and saves through one
# render (``config.api.save_config(..., probe=True)``).
_VALIDATES_ON_APPLY = frozenset({"providers_models"})
# The operations that go all the way to the managed proxy process.  A cached
# status read is not one of them: each of these can wait out the whole drain
# budget while the proxy finishes the turns it already accepted, so none may run
# under the store lock that every snapshot needs.
SERVICE_PROXY_OPERATIONS = frozenset({"start", "start_async", "restart", "reload", "stop"})
# The subset that answers by replacing (or first creating) the proxy. Their
# transitional answer is "starting", so a window that asked for one sees the
# proxy leave its state immediately instead of after the whole replacement.
SERVICE_TRANSITION_OPERATIONS = frozenset({"start", "start_async", "restart", "reload"})
# What the person last asked of the proxy.  It is recorded beside the service
# state because the two answer different questions: the state says what the
# child is doing now, the intent says whether this installation is supposed to
# keep one running at all.  Automatic recovery reads the intent, so it can
# restart a proxy that died and still never undo 停止服务.
SERVICE_INTENTS = frozenset({"running", "stopped"})
# How long an owned proxy may fail its own liveness endpoint before automatic
# recovery replaces it.  A burst of long turns can make one 0.5 s probe miss
# while the proxy is still serving them; a listener that answers nothing for
# this long is not serving anybody.
SERVICE_RECOVERY_PATIENCE_SECONDS = 90.0
# The wait after each failed automatic recovery, by attempt number.  A start
# that keeps failing (no configuration, a port that will never open) must not
# spin: it retries on this ladder and keeps retrying at the last step, because
# the repair for those is a person editing something, not a relaunch.
SERVICE_RECOVERY_RETRY_SECONDS = (15.0, 60.0, 180.0, 300.0)


def _service_recovery_delay(failed_attempts: int) -> float:
    """How long the next automatic recovery attempt waits."""

    index = min(max(int(failed_attempts), 1), len(SERVICE_RECOVERY_RETRY_SECONDS)) - 1
    return SERVICE_RECOVERY_RETRY_SECONDS[index]

# Import previews identify only settings that have a Core-owned adapter. Logs
# are a read-only projection and must never become a staged configuration
# section just because a package contains a similarly named key.
IMPORTABLE_DOMAINS = (
    "providers_models",
    "codex",
    "claude",
    "runtime",
    "webdav",
    "language",
    "relay_accounts",
)


def _default_domain_state(name: str) -> dict[str, Any]:
    """Return a neutral, secret-free initial state for every shared route."""

    if name == "providers_models":
        return {"providers": [], "revision": 0}
    if name == "runtime":
        return {"categories": [], "values": {}, "revision": 0}
    if name == "webdav":
        return {"enabled": False, "configured": False, "last_probe": "unknown", "revision": 0}
    if name == "logs":
        return {"tabs": list(LOG_TABS), "revision": 0}
    if name == "codex":
        return {"config_exists": False, "auth_file_exists": False, "revision": 0}
    return {"revision": 0}


class CoreError(ProtocolError):
    """A safe, stable domain/store error."""


class RevisionConflict(CoreError):
    def __init__(self, expected: int, actual: int):
        self.expected = expected
        self.actual = actual
        super().__init__("revision_conflict", "The Core state changed; reload and try again")


class DomainNotFound(CoreError):
    def __init__(self, domain: str):
        self.domain = domain
        super().__init__("domain_not_found", "The requested settings domain is unavailable")


class ConfirmationNeeded(CoreError):
    def __init__(self, codes: Iterable[str]):
        self.codes = tuple(dict.fromkeys(str(code) for code in codes if str(code)))
        super().__init__("confirmation_required", "Explicit confirmation is required for this change")


@runtime_checkable
class DomainAdapter(Protocol):
    """Minimal adapter contract implemented by Core-owned domains."""

    name: str

    def draft_state(self) -> object: ...

    def snapshot(self) -> Mapping[str, Any]: ...

    def dispatch(self, action: str, payload: object | None = None) -> Mapping[str, Any] | None: ...

    def validate(self, payload: object | None = None) -> Mapping[str, Any]: ...

    def apply(self, payload: object | None = None) -> Mapping[str, Any] | None: ...

    def reload(self) -> Mapping[str, Any] | None: ...

    # Optional: a domain that can name its own entities implements this and
    # lightens every event it appears in.  A domain that does not is published
    # as one ``replace`` op by ``entity_ops.domain_diff``.
    def diff(self, previous: Mapping[str, Any], current: Mapping[str, Any]) -> list[EntityOp]: ...


@dataclass(frozen=True)
class FileCapability:
    token: str
    path: Path
    purpose: str


@dataclass(frozen=True, repr=False)
class PreparedImport:
    """Parsed package held by an authenticated IPC import-preview lease.

    The package is intentionally not part of the wire result.  ``CoreIPCServer``
    stores this object behind a session-bound opaque token until the user
    submits a subset of ``detected_sections`` for staging.
    """

    package: Mapping[str, Any] = field(repr=False)
    detected_sections: tuple[str, ...]
    preview: Mapping[str, Mapping[str, bool]]
    revision: int


class FileCapabilityRegistry:
    """Resolve native file-picker choices without exposing paths over IPC."""

    def __init__(self) -> None:
        self._items: dict[str, FileCapability] = {}
        self._lock = threading.RLock()

    def register(self, path: Path | str, purpose: str) -> str:
        target = Path(path).expanduser()
        purpose_text = str(purpose).strip()
        if purpose_text not in {"import", "export", "claude-profile"}:
            raise CoreError("invalid_file_capability", "Unsupported file capability")
        try:
            details = target.lstat()
        except FileNotFoundError:
            details = None
        except OSError:
            raise CoreError("invalid_file_capability", "The selected file is unavailable") from None
        if details is not None and (stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode)):
            # An export target may not exist yet, but it still cannot be a
            # symlink or directory.
            raise CoreError("invalid_file_capability", "The selected file is unavailable")
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._items[token] = FileCapability(token=token, path=target, purpose=purpose_text)
        return token

    def resolve(self, token: object, purpose: str, *, consume: bool = True) -> Path:
        if not isinstance(token, str) or not token or len(token.encode("utf-8")) > 256:
            raise CoreError("invalid_file_capability", "The selected file is unavailable")
        with self._lock:
            item = self._items.get(token)
            if item is None or item.purpose != purpose:
                raise CoreError("invalid_file_capability", "The selected file is unavailable")
            if consume:
                self._items.pop(token, None)
        try:
            details = item.path.lstat()
        except FileNotFoundError:
            if purpose == "export":
                return item.path
            raise CoreError("invalid_file_capability", "The selected file is unavailable") from None
        except OSError:
            raise CoreError("invalid_file_capability", "The selected file is unavailable") from None
        if stat.S_ISLNK(details.st_mode) or (purpose in {"import", "claude-profile"} and not stat.S_ISREG(details.st_mode)):
            raise CoreError("invalid_file_capability", "The selected file is unavailable")
        return item.path


def _canonical_domain(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CoreError("invalid_domain", "A settings domain is required")
    clean = value.strip()
    return _DOMAIN_ALIASES.get(clean, clean)


def _safe_public(value: object) -> object:
    """Redact secrets, paths, and raw config-like fields in a snapshot."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = _key_text(key)
            # Raw editor text and complete documents are available only through
            # an explicit trusted editor operation, never in ``snapshot``.
            if key_text in {
                "raw",
                "raw_text",
                "raw_json",
                "config_text",
                "auth_text",
                "settings_text",
                "source_text",
                "document",
                "config_document",
                "private_path",
                "file_path",
            }:
                if item in (None, "", False, [], {}):
                    result[str(key)] = item
                else:
                    result[str(key)] = REDACTED
                continue
            result[str(key)] = _safe_public(redact(item, _key=key))
        return result
    if isinstance(value, list):
        return [_safe_public(item) for item in value]
    if isinstance(value, tuple):
        return [_safe_public(item) for item in value]
    # Every mapping and sequence above was rebuilt, and ``redact`` returned a
    # scalar leaf verbatim in this projection.  An immutable scalar therefore
    # needs no copy; anything else (a set, an arbitrary object) still does, so
    # the snapshot can never hand a caller Core's own mutable state.
    if value is None or value is True or value is False or type(value) is int or type(value) is float or type(value) is str:
        return value
    return copy.deepcopy(value)


# What each domain's action answer may leave in the snapshot's
# ``action_summaries`` slot.  A domain's dispatch answers with its whole
# projection -- ``providers_models`` includes every provider and route -- and
# storing that verbatim made the slot a second copy of the domain: one
# ``provider.add`` patch measured 6,993 B of ``action_summaries`` against 558 B
# of ops at fourteen providers.  A pane reads the summary a named operation
# produced (``operation_summary``), and a relay detection reads the type it
# names; every other key is state the patch already carries as ops.
_ACTION_SUMMARY_FIELDS: dict[str, frozenset[str]] = {
    "providers_models": frozenset({"operation_summary"}),
    # A detection's type and confidence are one answer, and the resource status
    # is what the refresh pane reads back for the same action.
    "relay_accounts": frozenset({"detected_type", "confidence", "resource_status"}),
}
_DEFAULT_ACTION_SUMMARY_FIELDS = frozenset({"operation_summary"})

#: Relay actions whose write also releases linked provider slots.
#: Removing a station or a key, and aligning a key group, retires the provider
#: keys and routes that pointed at the retired material, so the transaction
#: writes both domains and must declare both write lanes.
_RELAY_DEPENDENCY_ACTIONS = frozenset(
    {
        "station_remove",
        "relay_station_remove",
        "remove_station",
        "account_remove",
        "relay_account_remove",
        "remove_account",
        "api_key_delete",
        "relay_api_key_delete",
        "account_api_key_delete",
        "api_key_detach",
        "relay_api_key_detach",
        "account_api_key_detach",
        # Alignment retires the keys whose group the station dropped, exactly
        # as an explicit delete does, so the linked provider slots and their
        # routes have to be released before the station is called.  Without
        # this the remote key was gone while the document still pointed at it:
        # the pane listed the dangling key and its route kept answering with a
        # credential the station had already revoked.
        "api_key_set_auto_grouping",
        "relay_api_key_set_auto_grouping",
        "account_api_key_set_auto_grouping",
        "account_set_auto_grouping",
        "relay_account_set_auto_grouping",
        "api_key_auto_group_align",
        "relay_api_key_auto_group_align",
        "account_auto_group_align",
        "relay_account_auto_group_align",
    }
)

#: Provider actions that read relay-side material: the binding source of a
#: key, the key material behind a route, the slot credentials an already-linked
#: key holds.  They write the provider draft while reading the relay domain, so
#: they declare both lanes.
_PROVIDER_RELAY_RESOURCE_ACTIONS = frozenset(
    {
        "provider_import_relay_key",
        "model_select_relay_resource",
        "provider_fetch_relay_resource_models",
    }
)

#: Provider actions that resolve a station's own identity from the relay
#: domain before touching the provider draft.
_PROVIDER_RELAY_STATION_ACTIONS = frozenset({"provider_select_relay_station"})

#: Relay actions that stage the account's resources into the provider draft.
_RELAY_RESOURCE_IMPORT_ACTIONS = frozenset(
    {
        "resources_import",
        "relay_resources_import",
        "account_resources_import",
    }
)


def _action_summary_entry(name: str, result: object) -> dict[str, Any]:
    """Keep only the keys a client reads from one dispatch's answer.

    ``action_summaries`` is a slot for the answer to the last action, not a
    second projection: the state half of the answer already travels as the
    patch's ops.  The write point is where the distinction is known, so a
    domain that starts returning something new does not quietly re-inflate
    every later patch.
    """

    if not isinstance(result, Mapping):
        return {}
    fields = _ACTION_SUMMARY_FIELDS.get(name, _DEFAULT_ACTION_SUMMARY_FIELDS)
    return {key: value for key, value in result.items() if key in fields}


def _relay_binding_projection(state: object) -> list[tuple[str, ...]]:
    """The relay-bound part of a providers draft, for change detection.

    Two things in a provider draft bind material that only the relay domain can
    resolve: a key that resolves through a station, and a model that points at
    such a key.  Comparing this projection against the applied baseline tells an
    Apply whether it has any relay work to do at all — a draft that only adds an
    independent model, renames a provider, or edits a manual order has none, and
    it must not be dragged into a relay transaction whose backlog (a pending key
    create, an expired session) would otherwise fail an edit that has nothing to
    do with the relay.

    A linked route's own order mode is part of that row: following the
    multiplier is a value only the station's group states, so the edit that asks
    for it needs the relay domain to resolve it — the same dependency a new
    model on that key already carries.

    A key's *name* is deliberately not part of the projection.  The station owns
    that name and renames it whenever a group is renamed, and the slot that
    binds the key follows the rename on the read which observes it — the relay's
    own Apply, whose refresh revisits the station.  Comparing the name here
    would only make every providers edit re-read the station for a change it
    does not make, and would refuse an unrelated model edit for a rename that is
    the relay's own work to apply.
    """

    providers = state.get("providers") if isinstance(state, Mapping) else None
    projection: list[tuple[str, ...]] = []
    if not isinstance(providers, Sequence) or isinstance(providers, (str, bytes, bytearray)):
        return projection
    for provider in providers:
        if not isinstance(provider, Mapping):
            continue
        provider_name = str(provider.get("name", "")).strip()
        relay_key_names: set[str] = set()
        relay_key_ids: set[str] = set()
        api_keys = provider.get("api_keys")
        if isinstance(api_keys, Sequence) and not isinstance(api_keys, (str, bytes, bytearray)):
            for key in api_keys:
                if not isinstance(key, Mapping):
                    continue
                source = key.get("source") if isinstance(key.get("source"), Mapping) else {}
                # Only a key that resolves through a station is relay material;
                # an independent key, and the provider name it happens to live
                # under, is this domain's own business.
                if str(source.get("kind", "")) != "relay":
                    continue
                projection.append(
                    (
                        "key",
                        provider_name,
                        str(key.get("name", "")),
                        str(source.get("station_id", "")),
                        str(source.get("account_id", "")),
                        str(source.get("resource_id", "")),
                    )
                )
                relay_key_names.add(str(key.get("name", "")))
                if key.get("id"):
                    relay_key_ids.add(str(key.get("id")))
        models = provider.get("models")
        if isinstance(models, Sequence) and not isinstance(models, (str, bytes, bytearray)):
            for model in models:
                if not isinstance(model, Mapping):
                    continue
                # The slot is the route's key, so a route that carries one is
                # relay-bound only when that slot is the relay key.  Its name is
                # the weaker identity and decides only for a route that states no
                # slot, so a renamed or reused name cannot drag a local edit into
                # the relay transaction (and cannot refuse it for a station
                # problem the edit has nothing to do with).
                key_name = str(model.get("api_key_name", ""))
                key_id = str(model.get("provider_key_id", ""))
                if key_id:
                    if key_id not in relay_key_ids:
                        continue
                elif key_name not in relay_key_names:
                    continue
                projection.append(
                    (
                        "model",
                        provider_name,
                        str(model.get("model_name", model.get("name", ""))),
                        key_id,
                        key_name,
                        str(model.get("order_mode", "manual")).strip() or "manual",
                    )
                )
    return projection


_RELAY_DEPENDENCY_POLICY_NAMES = {
    "delete_models": "delete",
    "delete": "delete",
    "detach": "detach",
    "release": "detach",
    "detach_disabled": "detach_disabled",
    "rebind": "rebind",
}


def _relay_dependency_policy_name(value: object) -> str:
    """The provider-side policy one relay action or journal entry states.

    The relay domain owns the vocabulary a user chooses (``delete_models``,
    ``detach_only``, ...) while the provider domain owns the effect (``delete``,
    ``detach``, ...).  Both the dispatch that stages a key write and the Apply
    that executes it read the policy through this one table, so a journaled
    delete cannot be read with a different meaning than the one it was staged
    with.
    """

    return _RELAY_DEPENDENCY_POLICY_NAMES.get(str(value or "detach"), "")


def _relay_operation_dependencies(operations: object) -> dict[str, list[dict[str, str]]]:
    """Group the staged remote deletions of one Apply by their own policy.

    The journal is the durable record of work this Apply is about to make true,
    so it is also the record of which provider slots and routes that work will
    strand.  A deletion staged by an older Core, or by a dispatch that reported
    no dependencies, is exactly the case an Apply must not inherit: reading the
    policy from each operation is what makes releasing its linked rows a
    property of the journal rather than of the call that happened to write it.
    """

    grouped: dict[str, list[dict[str, str]]] = {}
    if not isinstance(operations, Sequence) or isinstance(operations, (str, bytes, bytearray)):
        return grouped
    for operation in operations:
        if not isinstance(operation, Mapping):
            continue
        if operation.get("kind") != "api_key_delete":
            continue
        if operation.get("state", operation.get("status", "staged")) != "staged":
            continue
        policy = str(operation.get("dependency_policy") or "detach")
        resource = {
            "station_id": str(operation.get("station_id", "")),
            "account_id": str(operation.get("account_id", "")),
            "resource_id": str(operation.get("resource_id", "")),
        }
        if not resource["account_id"] or not resource["resource_id"]:
            continue
        grouped.setdefault(policy, []).append(resource)
    return grouped


def _relay_dependency_sources(
    provider_keys: object,
    slot_credentials: object,
    *,
    dependency_baseline: object,
    dependency_draft: object,
) -> list[dict[str, Any]]:
    """The relay sources one dependency-only Apply actually has to resolve.

    ``provider_keys`` is ``dependency_summary()``'s own list: one row per linked
    ProviderKey, each carrying its stable slot id and its relay source.  The
    edit's own rows come from the binding projection, so an edited route is
    always resolved, and a slot whose own binding changed (a newly imported key,
    a key that moved to another resource) is resolved with it.

    A slot that carries no persisted credential joins regardless of the edit:
    the document is then not a substitute for a station read — there is no value
    to fall back to — so resolving it is the only way its routes can be written.
    Every other slot stays exactly as it is, which is what keeps one route's
    stale upstream from refusing an edit about a different route.

    Sources are deduplicated by their own identity: two provider keys may bind
    the same station key, and one resolution answers both.  Reporting that pair
    twice is what the materializer treats as a conflicting duplicate resource.
    """

    rows = (
        [item for item in provider_keys if isinstance(item, Mapping)]
        if isinstance(provider_keys, Sequence) and not isinstance(provider_keys, (str, bytes, bytearray))
        else []
    )
    if not rows:
        return []
    before = set(_relay_binding_projection(dependency_baseline))
    changed = [row for row in _relay_binding_projection(dependency_draft) if row not in before]
    key_ids: set[str] = set()
    key_names: set[tuple[str, str]] = set()
    for row in changed:
        if row[0] == "model":
            if row[3]:
                key_ids.add(row[3])
            else:
                # A route that states no slot is bound by its own key name, and
                # that name is the weaker identity the projection also records.
                key_names.add((row[1], row[4]))
        else:
            key_names.add((row[1], row[2]))
    held: set[tuple[str, str]] = set()
    if isinstance(slot_credentials, Mapping):
        for raw_key, value in slot_credentials.items():
            if not isinstance(value, str) or not value:
                continue
            try:
                account_id, resource_id = raw_key
            except (TypeError, ValueError):
                continue
            held.add((str(account_id), str(resource_id)))
    resolved: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in rows:
        source = item.get("source")
        if not isinstance(source, Mapping):
            continue
        slot_id = str(item.get("provider_key_id", "")).strip()
        name = (str(item.get("provider_id", "")), str(item.get("api_key_name", "")))
        needed = slot_id in key_ids or name in key_names
        if not needed:
            # A slot with no credential cannot be resolved from the document, so
            # the station has to answer for it even when this edit does not name
            # it.
            identity = (
                str(source.get("account_id", "")).strip(),
                str(source.get("resource_id", "")).strip(),
            )
            needed = identity not in held
        if not needed:
            continue
        identity = (
            str(source.get("station_id", "")),
            str(source.get("account_id", "")),
            str(source.get("resource_id", "")),
        )
        resolved.setdefault(identity, dict(source))
    return list(resolved.values())


def _as_mapping(value: object, label: str = "payload") -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CoreError("invalid_payload", f"{label} must be an object")
    return dict(value)


def _validation_summary(value: object) -> dict[str, Any]:
    """Normalize domain validation into the shared TypeScript shape."""

    if not isinstance(value, Mapping):
        return {
            "valid": False,
            "issues": [{"path": "", "code": "invalid", "message": "Validation failed", "severity": "error"}],
        }
    valid = value.get("valid") is True
    raw_issues = value.get("issues", value.get("errors", []))
    if isinstance(raw_issues, str):
        raw_issues = [raw_issues]
    issues: list[dict[str, Any]] = []
    if isinstance(raw_issues, Sequence) and not isinstance(raw_issues, (str, bytes, bytearray)):
        for issue in raw_issues:
            if isinstance(issue, Mapping):
                message = safe_error_message(issue.get("message", "Validation failed"))
                path = _safe_issue_path(issue.get("path", ""))
                code = str(issue.get("code", "invalid"))[:64]
                severity = str(issue.get("severity", "error"))
            else:
                message = safe_error_message(issue)
                path, code, severity = "", "invalid", "error"
            if severity not in {"error", "warning"}:
                severity = "error"
            issues.append({"path": path, "code": code, "message": message, "severity": severity})
    if any(issue["severity"] == "error" for issue in issues):
        valid = False
    return {"valid": valid, "issues": issues}


def _safe_issue_path(value: object) -> str:
    """Keep validation locations useful without returning a local path."""

    text = str(value).strip()
    if not text:
        return ""
    if len(text) > 160 or text.startswith(("/", "~", "\\")) or re.match(r"^[A-Za-z]:[\\/]", text):
        return "configuration"
    if "/" in text or "\\" in text or ".." in text.split("."):
        return "configuration"
    return text if re.fullmatch(r"[A-Za-z0-9_.\[\]-]+", text) else "configuration"


def _display_path(path: str) -> str:
    """Spell one user file path with the home directory written as ``~``.

    The external-settings pane shows where each registered client file lives;
    the account's own home directory is noise there and should not be spelled
    out.  Only the spelling is shortened: the row keeps its real ``path`` for
    the operations that must address the file itself.
    """

    home = str(Path.home())
    if path == home:
        return "~"
    prefix = home + os.sep
    if not home or not path.startswith(prefix):
        return path
    return "~/" + path[len(prefix):].replace(os.sep, "/")


def _secret_presence(value: object) -> bool:
    if value in (None, "", False, [], {}, REDACTED):
        return False if value != REDACTED else True
    if isinstance(value, Mapping):
        return any(_secret_presence(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_secret_presence(item) for item in value)
    return True


def _mapping_contains_key(value: object, keys: set[str]) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key).strip()).lower().replace("-", "_")
            if text in keys:
                return True
            if _mapping_contains_key(item, keys):
                return True
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_mapping_contains_key(item, keys) for item in value)
    return False


def _relay_transient_dispatch_result(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap a relay refresh/import projection in the dispatch result envelope.

    The dispatch contract is a revision plus an optional action-scoped
    summary, while those helpers report their own projection (resource status
    and counts).  Returning that projection as the whole result adds top-level
    fields the IPC contract rejects, so it travels as the action summary and
    the caller reads the status from there.
    """

    revision = summary.get("revision")
    return {
        "revision": revision if type(revision) is int else 0,
        "action_summary": {key: value for key, value in summary.items() if key != "revision"},
    }


def _checkpoint_adapter(adapter: DomainAdapter, *, error_code: str = "import_failed") -> dict[str, Any]:
    """Capture one adapter before a transaction mutates it."""

    checkpoint = getattr(adapter, "transaction_checkpoint", None)
    restore = getattr(adapter, "restore_transaction", None)
    if callable(checkpoint) and callable(restore):
        try:
            return {"kind": "transaction", "value": copy.deepcopy(checkpoint())}
        except Exception:
            raise CoreError(error_code, "Settings could not be prepared for a transaction") from None
    state = getattr(adapter, "__dict__", None)
    if not isinstance(state, dict):
        raise CoreError(error_code, "Settings could not be prepared for a transaction")
    try:
        return {"kind": "state", "value": copy.deepcopy(state)}
    except Exception:
        raise CoreError(error_code, "Settings could not be prepared for a transaction") from None


def _restore_adapter(adapter: DomainAdapter, checkpoint: Mapping[str, Any]) -> None:
    """Restore the same adapter instance so no caller observes partial state."""

    kind = checkpoint.get("kind")
    value = checkpoint.get("value")
    if kind == "transaction":
        restore = getattr(adapter, "restore_transaction", None)
        if not callable(restore) or not isinstance(value, Mapping):
            raise RuntimeError("Configuration package adapter cannot be restored")
        restore(copy.deepcopy(value))
        return
    if kind != "state" or not isinstance(value, Mapping):
        raise RuntimeError("Configuration package adapter cannot be restored")
    state = getattr(adapter, "__dict__", None)
    if not isinstance(state, dict):
        raise RuntimeError("Configuration package adapter cannot be restored")
    state.clear()
    state.update(value)


@dataclass(frozen=True)
class _FileCheckpoint:
    path: Path
    data: bytes | None
    mode: int
    backups: frozenset[Path]


def _adapter_persistence_paths(adapter: DomainAdapter) -> tuple[Path, ...]:
    """Resolve only files explicitly owned by a domain adapter."""

    delegate = getattr(adapter, "_delegate", None)
    if delegate is not None:
        adapter = delegate
    paths: list[Path] = []
    owned_paths = getattr(adapter, "persistence_paths", None)
    if callable(owned_paths):
        for value in owned_paths():
            if isinstance(value, (str, Path)):
                paths.append(Path(value).expanduser())
    for attribute in ("config_path", "runtime_config_path", "settings_path", "preference_path", "enabled_path", "status_path", "model_catalog_path"):
        value = getattr(adapter, attribute, None)
        if isinstance(value, (str, Path)):
            paths.append(Path(value).expanduser())
    name = str(getattr(adapter, "name", ""))
    if name == "providers_models":
        config_path = getattr(adapter, "config_path", None)
        if isinstance(config_path, (str, Path)):
            target = Path(config_path).expanduser()
            paths.append(target.with_name(f"{target.stem}.disabled-models.yaml"))
    elif name == "codex":
        home_value = getattr(adapter, "codex_home", None)
        home = Path(home_value).expanduser() if isinstance(home_value, (str, Path)) else Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
        paths.extend((home / "config.toml", home / "auth.json"))
    return tuple(dict.fromkeys(paths))


def _document_master_key(path: Path) -> str:
    """The key the document on disk hands its clients, or ``""`` if unreadable.

    Read through the same parser every other reader of this document uses, so
    the answer a sync compares before and after an install is the answer the
    proxy's own environment is built from.  An unreadable or unparsable
    document states nothing: a comparison against ``""`` then only claims a
    move when a key actually appears, which is the safe direction -- the worst
    case is a rolling reload where a replacement was owed.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    try:
        from .domains.runtime import master_key_from_document

        return master_key_from_document(text)
    except Exception:
        return ""


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except OSError:
        return False


def _checkpoint_files(paths: Iterable[Path]) -> tuple[_FileCheckpoint, ...]:
    checkpoints: list[_FileCheckpoint] = []
    try:
        for path in dict.fromkeys(paths):
            data = read_bytes(path)
            mode = stat.S_IMODE(path.stat().st_mode) if data is not None else 0o600
            backups = frozenset(candidate for candidate in path.parent.glob(f"{path.name}.bak-*") if candidate.is_file() and not candidate.is_symlink())
            checkpoints.append(_FileCheckpoint(path=path, data=data, mode=mode, backups=backups))
    except (OSError, PersistenceError):
        raise CoreError("apply_failed", "Settings could not be prepared for an atomic apply") from None
    return tuple(checkpoints)


def _restore_files(checkpoints: Iterable[_FileCheckpoint]) -> None:
    for checkpoint in checkpoints:
        if checkpoint.data is not None:
            # A rollback of a file that never changed must not write it.  The
            # bytes are equal either way, but the mtime is the document's
            # identity to everything watching it: a WebDAV sync that stops on a
            # conflict rolls every checkpoint back, and rewriting an unchanged
            # `config.yaml` told the client-model loop that somebody had edited
            # the roster -- one rolling proxy reload every sync interval, for as
            # long as nobody answered the conflict.  Measured on lv: a reload
            # every two minutes for eleven hours, from a sync that wrote
            # nothing.
            current = read_bytes(checkpoint.path)
            if current != checkpoint.data:
                atomic_write_bytes(checkpoint.path, checkpoint.data, mode=checkpoint.mode)
        else:
            try:
                details = checkpoint.path.lstat()
            except FileNotFoundError:
                details = None
            if details is not None:
                if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                    raise PersistenceError("Settings rollback target is unsafe")
                checkpoint.path.unlink()
        for candidate in checkpoint.path.parent.glob(f"{checkpoint.path.name}.bak-*"):
            if candidate in checkpoint.backups:
                continue
            details = candidate.lstat()
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                raise PersistenceError("Settings rollback backup is unsafe")
            candidate.unlink()


class RecoverableDomain:
    """Keep a malformed route registered and retry its real factory on Reload."""

    def __init__(self, name: str, factory: Callable[[], DomainAdapter]):
        self.name = _canonical_domain(name)
        self._factory = factory
        self._delegate: DomainAdapter | None = None
        self._recovery_candidate: DomainAdapter | None = None
        self.revision = 0

    def snapshot(self) -> Mapping[str, Any]:
        if self._delegate is not None:
            return self._delegate.snapshot()
        neutral = _default_domain_state(self.name)
        if self.name == "claude":
            neutral["settings"] = {}
        if self.name == "language":
            neutral["choice"] = "system"
        return {"domain": self.name, "available": False, "error_code": "settings_unavailable", **neutral}

    def draft_state(self) -> object:
        if self._delegate is not None:
            return self._delegate.draft_state()
        return {"available": False}

    def _require_delegate(self) -> DomainAdapter:
        if self._delegate is None:
            raise CoreError("domain_unavailable", "Settings could not be loaded")
        return self._delegate

    def dispatch(self, action: str, payload: object | None = None) -> Mapping[str, Any] | None:
        if action == "reload":
            return self.reload()
        return self._require_delegate().dispatch(action, payload)

    def validate(self, payload: object | None = None) -> Mapping[str, Any]:
        if self._delegate is None:
            return {"valid": False, "errors": ["Settings could not be loaded"]}
        return self._delegate.validate(payload)

    def apply(self, payload: object | None = None) -> Mapping[str, Any] | None:
        return self._require_delegate().apply(payload)

    def reload(self) -> Mapping[str, Any] | None:
        if self._delegate is not None:
            return self._delegate.reload()
        try:
            delegate = self._recovery_candidate or self._factory()
        except Exception:
            raise CoreError("domain_unavailable", "Settings could not be loaded") from None
        self._recovery_candidate = None
        self._delegate = delegate
        self.revision += 1
        return delegate.snapshot()

    def external_disk_state(self) -> Mapping[str, bool]:
        """Probe a failed source so a valid external repair recovers itself."""

        if self._delegate is not None:
            detector = getattr(self._delegate, "external_disk_state", None)
            return detector() if callable(detector) else {"changed": False, "exists": True}
        if self._recovery_candidate is None:
            try:
                self._recovery_candidate = self._factory()
            except Exception:
                return {"changed": False, "exists": True}
        return {"changed": True, "exists": True}

    def external_disk_identity(self) -> str:
        if self._delegate is not None:
            getter = getattr(self._delegate, "external_disk_identity", None)
            if callable(getter):
                return str(getter())
            return "recoverable-loaded"
        if self._recovery_candidate is not None:
            getter = getattr(self._recovery_candidate, "external_disk_identity", None)
            if callable(getter):
                return str(getter())
            return "recoverable-ready"
        return "recoverable-unavailable"

    def __getattr__(self, name: str) -> Any:
        """Expose trusted delegate capabilities after automatic recovery."""

        if name.startswith("_") or self._delegate is None:
            raise AttributeError(name)
        return getattr(self._delegate, name)


class MemoryDomain:
    """A tiny adapter useful for tests and for generic Core-owned metadata.

    Production domains should wrap their existing Python modules; this class
    exists so the transport/store can be tested without creating user files.
    """

    def __init__(self, name: str, initial: Mapping[str, Any] | None = None):
        self.name = _canonical_domain(name)
        self._state = copy.deepcopy(dict(initial or {}))
        self._draft = copy.deepcopy(self._state)
        self._raw = copy.deepcopy(self._state)
        self.revision = 0

    def snapshot(self) -> Mapping[str, Any]:
        return {"domain": self.name, "revision": self.revision, "state": copy.deepcopy(self._draft)}

    def draft_state(self) -> object:
        return copy.deepcopy(self._draft)

    def dispatch(self, action: str, payload: object | None = None) -> Mapping[str, Any]:
        data = _as_mapping(payload)
        if action in {"set", "patch"}:
            self._draft.update(copy.deepcopy(data))
        elif action in {"replace", "set_raw"}:
            self._draft = copy.deepcopy(data.get("state", data))
        elif action in {"reset", "cancel", "reload"}:
            self._draft = copy.deepcopy(self._raw)
        else:
            raise CoreError("unknown_action", "The requested settings action is unavailable")
        self.revision += 1
        return self.snapshot()

    def validate(self, payload: object | None = None) -> Mapping[str, Any]:
        candidate = self._draft if payload is None else payload
        return {"valid": isinstance(candidate, Mapping), "errors": [] if isinstance(candidate, Mapping) else ["Settings must be an object"]}

    def apply(self, payload: object | None = None) -> Mapping[str, Any]:
        self._raw = copy.deepcopy(self._draft)
        self._state = copy.deepcopy(self._raw)
        self.revision += 1
        return {"applied": True, **self.snapshot()}

    def reload(self) -> Mapping[str, Any]:
        self._draft = copy.deepcopy(self._raw)
        self.revision += 1
        return self.snapshot()

    def export(self) -> Mapping[str, Any]:
        return {"domain": self.name, "state": copy.deepcopy(self._draft)}


class UnavailableDomain:
    """Fail closed when a production settings source cannot be parsed."""

    def __init__(self, name: str):
        self.name = _canonical_domain(name)

    def snapshot(self) -> Mapping[str, Any]:
        return {
            "domain": self.name,
            "revision": 0,
            "available": False,
            "error": "Settings could not be loaded",
        }

    def draft_state(self) -> object:
        return {"available": False}

    def dispatch(self, action: str, payload: object | None = None) -> Mapping[str, Any]:
        raise CoreError("domain_unavailable", "Settings could not be loaded")

    def validate(self, payload: object | None = None) -> Mapping[str, Any]:
        return {"valid": False, "errors": ["Settings could not be loaded"]}

    def apply(self, payload: object | None = None) -> Mapping[str, Any]:
        raise CoreError("domain_unavailable", "Settings could not be loaded")

    def reload(self) -> Mapping[str, Any]:
        raise CoreError("domain_unavailable", "Settings could not be loaded")



def _storage_user_id(storage: Mapping[str, str]) -> str:
    """The station's account id, wherever that fork kept it.

    A New API fork wants its own id as the `New-Api-User` header on every
    dashboard read, and the id is not in the cookie.  Some forks write it under
    `id`, and the one measured on 2026-11-08 (pawuvo.com) writes the whole
    profile as JSON under `user` instead — with `id` nowhere else.  Reading only
    the flat key left every mirror sign-in on that station unverifiable: the
    person was signed in, the cookie was right, and the profile read answered
    401 because the header never went out.
    """

    for key in ("id", "user_id", "userId"):
        value = storage.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raw = storage.get("user")
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            user = json.loads(raw)
        except ValueError:
            return ""
        if isinstance(user, Mapping):
            for key in ("id", "user_id", "userId"):
                value = user.get(key)
                if isinstance(value, (int, str)) and str(value).strip():
                    return str(value).strip()
    return ""


#: The store lock guards in-memory state and nothing else.  A window's
#: snapshot needs it for every frame, so no operation may hold it across a
#: socket, a subprocess, or a file read: a relay Apply legitimately talks to a
#: station for minutes, and every window used to wait on this one lock while
#: it did.  Mutations serialize on ``_mutation`` instead, which readers never
#: take.  The budget below is the contract, and ``_BudgetLock`` records every
#: hold that breaks it together with the stack that took the lock, so the next
#: regression names itself instead of hiding in a latency sample.
CORE_LOCK_BUDGET_SECONDS = 0.2


class CoreLockBudgetError(RuntimeError):
    """A store-lock hold crossed the budget (strict mode only)."""


class _BudgetLock:
    """An RLock that remembers how long each hold lasted.

    A plain ``RLock`` cannot answer "who held me for two seconds" after the
    fact, and the answer is what makes the 200 ms rule enforceable.  Every
    outermost acquire records a stack and a monotonic start; the matching
    release reports the hold when it crossed the budget.  Nested acquires are
    one hold, exactly as they are one critical section for the lock.
    """

    def __init__(self, budget: float = CORE_LOCK_BUDGET_SECONDS) -> None:
        self._inner = threading.RLock()
        self._budget = float(budget)
        self._stats = threading.Lock()
        self._depth: dict[int, int] = {}
        self._started: dict[int, tuple[float, list[str]]] = {}
        self._overruns: list[dict[str, object]] = []
        self._strict = os.environ.get("YOUNG_ROUTER_CORE_LOCK_STRICT") == "1"

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        acquired = self._inner.acquire(blocking, timeout) if (not blocking or timeout >= 0) else self._inner.acquire()
        if acquired:
            self._entered()
        return bool(acquired)

    def release(self) -> None:
        record = self._leaving()
        # Release first: reporting a long hold must not extend it, and strict
        # mode's exception must not leave the lock owned.
        self._inner.release()
        if record is not None:
            _log_lock_overrun(record, self._budget)
            if self._strict:
                raise CoreLockBudgetError(
                    f"Core store lock held {float(record['seconds']) * 1000:.0f} ms "
                    f"(budget {self._budget * 1000:.0f} ms)"
                )

    def __enter__(self) -> "_BudgetLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def _is_owned(self) -> bool:
        owned = getattr(self._inner, "_is_owned", None)
        return bool(owned()) if callable(owned) else False

    def _entered(self) -> None:
        ident = threading.get_ident()
        with self._stats:
            depth = self._depth.get(ident, 0)
            self._depth[ident] = depth + 1
            if depth == 0:
                self._started[ident] = (time.perf_counter(), traceback.format_stack(limit=14))

    def _leaving(self) -> dict[str, object] | None:
        ident = threading.get_ident()
        record: dict[str, object] | None = None
        with self._stats:
            depth = self._depth.get(ident, 0) - 1
            if depth <= 0:
                self._depth.pop(ident, None)
                started = self._started.pop(ident, None)
                if started is not None:
                    elapsed = time.perf_counter() - started[0]
                    if elapsed > self._budget:
                        record = {
                            "seconds": elapsed,
                            "thread": ident,
                            "stack": started[1],
                        }
                        self._overruns.append(record)
                        if len(self._overruns) > 50:
                            del self._overruns[:-50]
            else:
                self._depth[ident] = depth
        return record

    def overruns(self) -> tuple[dict[str, object], ...]:
        with self._stats:
            return tuple(self._overruns)

    def reset_overruns(self) -> None:
        with self._stats:
            self._overruns.clear()


def _log_lock_overrun(record: Mapping[str, object], budget: float) -> None:
    """Report one over-budget hold; never let logging break the release.

    The warning goes to stderr (the host captures it) and to a dedicated file
    beside the Core record: a replaced Core's stderr is not always kept, and an
    over-budget hold is the one diagnostic that has to outlive the process.
    """

    try:
        stack = record.get("stack")
        rendered = "".join(stack) if isinstance(stack, (list, tuple)) else str(stack)
        message = (
            f"{time.strftime('%Y-%m-%dT%H:%M:%S')} Core store lock held "
            f"{float(record.get('seconds', 0.0)) * 1000.0:.0f} ms "
            f"(budget {budget * 1000.0:.0f} ms)\n{rendered}"
        )
        logging.getLogger("young_router.core.lock").warning(
            "Core store lock held %.0f ms (budget %.0f ms); acquired at:\n%s",
            float(record.get("seconds", 0.0)) * 1000.0,
            budget * 1000.0,
            rendered,
        )
        # The dedicated file is for the store's own budget.  Unit tests build
        # deliberately tiny budget locks; writing those to the installation's
        # log would make a real violation indistinguishable from test noise.
        if budget >= CORE_LOCK_BUDGET_SECONDS:
            root = Path(os.environ.get("YOUNG_ROUTER_HOME", str(Path.home() / ".young-router")))
            try:
                root.mkdir(parents=True, exist_ok=True)
                with open(root / "core-lock.log", "a", encoding="utf-8") as handle:
                    handle.write(message + "\n")
            except OSError:
                pass
    except Exception:
        pass


class CoreStore:
    """One thread-safe, versioned state source for native shells."""

    # A quit must not wait for a full proxy restart: the worker stops whatever
    # it started as soon as it observes the stopping flag.
    _SERVICE_RELOAD_SHUTDOWN_JOIN_SECONDS = 1.0
    # Nor may it wait out a replacement another lane has already begun.  The
    # stop below is the last word either way: it ends whatever that lane
    # started, so waiting longer only postpones the quit.
    _SERVICE_TRANSITION_SHUTDOWN_WAIT_SECONDS = 5.0

    def __init__(
        self,
        *,
        metadata_path: Path | str | None = None,
        domains: Iterable[DomainAdapter] = (),
        file_capabilities: FileCapabilityRegistry | None = None,
        service_handlers: Mapping[str, Callable[[str], object]] | None = None,
        workbuddy_runtime: object | None = None,
        qoder_runtime: object | None = None,
        our_free_model_runtime: object | None = None,
    ) -> None:
        # ``_lock`` is the state lock: short critical sections only (see
        # ``_BudgetLock``).  ``_mutation`` is the cross-domain transaction
        # lane: a commit that writes more than one domain -- an Apply, a WebDAV
        # pull, a relay removal that also releases provider slots -- holds it
        # for the whole transaction, and it is allowed to span remote work.
        # One domain's ordinary write takes only that domain's lock (see
        # ``_domain_scope``), so a probe cannot queue an unrelated edit behind
        # it.  Neither lock is the one every window needs for its next frame.
        self._lock = _BudgetLock()
        self._mutation = threading.RLock()
        self._mutation_owner: int | None = None
        self._mutation_depth = 0
        # One lock per registered domain: the write lane an edit in that domain
        # holds, and the only lane an unrelated domain's edit waits on.
        self._domain_locks: dict[str, threading.RLock] = {}
        # Which threads are inside a domain write, by ident and nesting depth.
        # A projection reads all domains at once, so any *other* holder means
        # the projection must answer the last committed view instead of reading
        # a domain mid-write -- the same rule ``_mutation_owner`` enforces for
        # cross-domain transactions.
        self._domain_holds: dict[int, int] = {}
        self._metadata_store = AtomicJSONStore(metadata_path) if metadata_path else None
        self.file_capabilities = file_capabilities or FileCapabilityRegistry()
        self._service_handlers = dict(service_handlers or {})
        self._shutdown_handler = self._service_handlers.get("stop")
        # The WorkBuddy worker is Core-owned: it must not outlive the process
        # that keeps its credential copy and its loopback bearer.
        self._workbuddy_runtime = workbuddy_runtime
        # The Qoder worker is Core-owned for the same reason: it stores the
        # region's PAT and holds the loopback bearer every Qoder route uses.
        self._qoder_runtime = qoder_runtime
        # The Our Free Model worker is Core-owned for the same reason: it must
        # not outlive the process that holds its loopback bearer.
        self._our_free_model_runtime = our_free_model_runtime
        self._domains: dict[str, DomainAdapter] = {}
        self._baselines: dict[str, object] = {}
        self._drafts: dict[str, dict[str, Any]] = {}
        self._subscribers: dict[str, Callable[[dict[str, Any]], None]] = {}
        # Publishing is off the mutation lane: a commit marks an event pending
        # here and the emitter thread projects and fans it out once the burst
        # settles.  ``_emit`` is still the send itself; the lambda is what
        # makes it the one a test replaced by the time the window expires.
        self._emitter = CoalescingEmitter(lambda: self._emit())
        self._revision = 0
        # The last domain projection and the revision it describes: a
        # window that is catching up asks for a revision it already
        # heard about several times per commit.  See ``snapshot``.
        self._snapshot_cache: tuple[int, dict[str, Any]] | None = None
        # The last projection handed to a window.  While another thread is
        # inside a mutation, ``snapshot`` answers this instead of reading
        # adapter state that mutation is rewriting; the adapter projections
        # are only consistent under the mutation that owns them.
        self._snapshot_view: dict[str, Any] | None = None
        # What the last published event left a window holding, and the revision
        # it carried: the next event is the patch that takes a window from here
        # to the store's current state.  It is the whole projection, not only
        # its domains, because the fields a window cannot derive from an op (a
        # staged draft, the disk markers, the service row, an action summary)
        # have to be compared against what the windows already hold.  ``None``
        # means no window has received a full state from this store yet, so the
        # next event must be one (first subscription, or a gap that reset the
        # chain).
        self._last_published_projection: dict[str, Any] | None = None
        self._last_published_revision = 0
        # One send at a time, so two publishers cannot both compute a patch
        # against the same base and record their answers out of order.  Reentrant
        # because a subscriber's own callback may publish on this thread.
        self._publish_guard = threading.RLock()
        # Long-running operations (a probe) run here, off the request thread:
        # the caller gets a job id at once and the answer arrives as a ``job``
        # event, so a slow upstream costs its own read rather than the write
        # lane every window edits on.
        self._job_guard = threading.Lock()
        self._job_pool: ThreadPoolExecutor | None = None
        self._service: dict[str, Any] = {"state": "unknown"}
        # Post-apply proxy restarts take several seconds; they run on their own
        # thread so the committed edit and every later IPC request stay fast.
        self._service_reload_guard = threading.Lock()
        self._service_reload_thread: threading.Thread | None = None
        self._client_model_sync: Any | None = None
        self._service_reload_pending = False
        self._service_reload_stopping = False
        # Whether the queued restart must replace the process (a runtime
        # setting the live environment cannot adopt) instead of rolling its
        # workers.  Cleared by the worker when it consumes the request.
        self._service_reload_replace_process = False
        # Manual lifecycle operations and the background post-apply restart
        # replace the same proxy, so they take turns on one transition guard.
        self._service_transition_guard = threading.Lock()
        # What the person last asked of the proxy (``SERVICE_INTENTS``), loaded
        # from the Core record so a replacement Core honours a 停止服务 that was
        # given to the previous one.
        self._service_intent = "running"
        # Automatic recovery's own memory: since when the owned child has been
        # failing its liveness probe, how many recovery attempts failed in a
        # row, and when the next one may run.  Guarded by ``self._lock``.
        self._service_supervision_unhealthy_since: float | None = None
        self._service_supervision_attempts = 0
        self._service_supervision_retry_at = 0.0
        # Set before the proxy is stopped for a quit or a Core replacement, so
        # no supervision pass can start a replacement behind the stop.
        self._service_supervision_stopping = False
        self._service_supervision_guard = threading.Lock()
        # The recovery loop is created on request, started by the Core entry
        # point, and stopped with the store -- like the WebDAV and image-context
        # loops beside it.
        self._service_supervisor: Any | None = None
        # A proxy operation replaces the proxy, which the drain budget now lets
        # take minutes.  It must therefore not hold ``self._lock`` while it
        # runs: every ``snapshot`` needs that lock, the native hosts cap one
        # request at 30 s, and a host whose request times out tears Core down --
        # which is how a restart left every window reporting "启动中" with
        # nothing left to correct it.  This count is how many such operations
        # are between their transitional publish and their answer; while it is
        # non-zero ``snapshot`` keeps projecting the state the dispatch already
        # published instead of a controller reading taken mid-replacement.
        self._service_proxy_operations = 0
        # How many of those are *starts*.  A start has no earlier proxy to be
        # mid-replacement, so once the controller answers running there is no
        # other reading it could be hiding: the start's own follow-up work (a
        # Codex file rewrite under this lock) can outlast a window's patience,
        # and a proxy that is serving while every snapshot still says 启动中 is
        # the same lie in the other direction.
        self._service_start_operations = 0
        self._last_actions: dict[str, dict[str, Any]] = {}
        # The image-context cache's own day, applied from outside the request
        # path: the store sweeps on writes, which is what stops when the pass
        # is off.  Created on request and started by the entry point.
        self._image_context_sweeper: Any | None = None
        # The WebDAV interval loop is created on request, started by the Core
        # entry point, and stopped with the store.
        self._webdav_scheduler: Any | None = None
        # When a user action last reached this store.  An automatic WebDAV sync
        # holds the store for its whole round trip, so it waits for quiet.
        self._last_activity = time.monotonic()
        # The page host's sign-in windows: a real browser this Core launched at
        # a station's own address, keyed by the id the page polls.  They live
        # here rather than in a domain because a window is not configuration --
        # nothing about it is written down, and a Core that stops takes them
        # with it (see ``_shutdown_browser_logins``).
        self._browser_logins: dict[str, dict[str, Any]] = {}
        # The thread that ends abandoned sign-ins; see `_reap_browser_logins`.
        self._browser_reaper: threading.Thread | None = None
        self._browser_reaper_stop: threading.Event | None = None
        self._disk: dict[str, dict[str, Any]] = {}
        self._disk_identities: dict[str, str | None] = {}
        self._logs: dict[str, dict[str, Any]] = {
            tab: {
                "tab": tab,
                "available": False,
                "paused": False,
                "line_count": 0,
                "filter": "",
                "limit": 0,
            }
            for tab in LOG_TABS
        }
        if self._metadata_store is not None:
            self._load_metadata()
        for adapter in domains:
            self.register_domain(adapter)

    @contextlib.contextmanager
    def _mutation_scope(self):
        """Hold the cross-domain transaction lane for one whole mutation.

        This is what an Apply, a WebDAV pull, a relay removal that also
        rewrites provider slots, and a proxy lifecycle operation take: they
        write several domains as one transaction, so they must exclude every
        other writer.  A write that touches one domain uses ``_domain_scope``
        instead and never waits on this lane.

        The owner is published under the state lock before the mutation
        touches anything, so a ``snapshot`` either takes its projection first
        (then this mutation starts after it) or sees the owner and serves the
        last committed projection.  There is no window in which a reader can
        observe a domain halfway through a dispatch.
        """

        if self._holds_domain_locks():
            # A cross-domain operation nested inside a domain write (a commit
            # that stages a relay import from inside its own dispatch): the
            # domain locks it already holds are its serialization, and waiting
            # for the lane here would take the two locks in the opposite order
            # from a cross-domain commit -- the one way this design can
            # deadlock.  Nothing is yielded under a lock it did not already own.
            yield
            return
        self._acquire_mutation_lane()
        try:
            if self._mutation_depth == 1 and self._snapshot_view is None:
                # The first mutation of a Core's life may land before any
                # window has asked for a snapshot (a start at launch).  A
                # snapshot that arrives during that mutation must not wait on
                # the lane; publish the pre-mutation projection now, while no
                # adapter is halfway through a write.
                self._publish_initial_snapshot_view()
            yield
        finally:
            self._release_mutation_lane()

    def _acquire_mutation_lane(self) -> None:
        """Take the transaction lane and publish this thread as its owner."""

        ident = threading.get_ident()
        self._mutation.acquire()
        with self._lock:
            if self._mutation_depth == 0:
                self._mutation_owner = ident
            self._mutation_depth += 1

    def _release_mutation_lane(self) -> None:
        """Release the transaction lane and its ownership record."""

        with self._lock:
            self._mutation_depth -= 1
            if self._mutation_depth == 0:
                self._mutation_owner = None
        self._mutation.release()

    def _domain_lock(self, name: str) -> threading.RLock:
        """The write lane for one domain, created the first time it is needed.

        ``register_domain`` creates it, so a registered domain always has its
        own; this also answers for a name a caller declared but never
        registered (the operation then fails with ``domain_not_found`` while
        holding it, which is what the single-lane form did too).
        """

        with self._lock:
            lock = self._domain_locks.get(name)
            if lock is None:
                lock = threading.RLock()
                self._domain_locks[name] = lock
            return lock

    def _domain_names(self) -> list[str]:
        """Every registered domain, in the order scopes declare them."""

        with self._lock:
            return sorted(self._domains)

    def _ordered_domain_locks(self, names: Iterable[str]) -> list[threading.RLock]:
        """The locks one scope declares, deduplicated and in the fixed order.

        Every multi-domain scope takes its locks by sorted name.  That is what
        makes two overlapping cross-domain commits impossible to interleave in
        opposite orders.
        """

        ordered = sorted({name for name in names if isinstance(name, str) and name})
        return [self._domain_lock(name) for name in ordered]

    def _all_domain_locks(self) -> list[threading.RLock]:
        with self._lock:
            return [self._domain_locks[name] for name in sorted(self._domain_locks)]

    def _holds_domain_locks(self) -> bool:
        ident = threading.get_ident()
        with self._lock:
            return self._domain_holds.get(ident, 0) > 0

    def _note_domain_hold(self, delta: int) -> None:
        ident = threading.get_ident()
        with self._lock:
            depth = self._domain_holds.get(ident, 0) + delta
            if depth > 0:
                self._domain_holds[ident] = depth
            else:
                self._domain_holds.pop(ident, None)

    def _foreign_domain_writer(self) -> bool:
        """Whether another thread is inside a domain write right now."""

        ident = threading.get_ident()
        with self._lock:
            return any(holder != ident for holder in self._domain_holds)

    @contextlib.contextmanager
    def _domain_scope(self, *names: str):
        """Hold the write lane of every named domain for one mutation.

        One name is the ordinary case: a dispatch, an Apply of one section, a
        reload, a probe's commit.  It takes exactly that domain's lock, so a
        slow operation in another domain -- a relay login or a WebDAV round
        trip, which used to hold the one global lane for their whole network
        call -- can no longer queue an edit behind it.

        Several names are a cross-domain action, which must declare the whole
        set it writes (``relay_accounts``, ``providers_models`` for a removal
        that releases linked slots).  Those also take ``_mutation``, so the
        transaction is still serialized against every other cross-domain
        commit, and they take the domain locks in sorted order so two of them
        can never interleave in opposite orders.

        A scope entered while the thread already holds a domain lock is the
        nested case (an Apply validating one section, an editor stage whose
        dispatch is the actual write).  It takes only the locks it names, in
        the fixed order, and never reaches back for the transaction lane: a
        thread may hold the lane and then domain locks, never the other way
        around.
        """

        ordered = [_canonical_domain(name) for name in names if isinstance(name, str) and name]
        nested = self._holds_domain_locks()
        if not nested:
            # The first write of a Core's life may beat every reader to it.
            self._publish_snapshot_view_if_absent()
        lane = False
        if len(set(ordered)) > 1 and not self._holds_domain_locks():
            self._acquire_mutation_lane()
            lane = True
        locks = self._ordered_domain_locks(dict.fromkeys(ordered)) if ordered else []
        for lock in locks:
            lock.acquire()
        self._note_domain_hold(1)
        try:
            yield
        finally:
            self._note_domain_hold(-1)
            for lock in reversed(locks):
                lock.release()
            if lane:
                self._release_mutation_lane()

    @contextlib.contextmanager
    def _projection_scope(self, *, wait: bool = True):
        """Hold the read lane one projection needs: no domain mid-write.

        A projection walks every domain, so it must not read one while a
        writer is halfway through it -- that is exactly the half-written state
        ``snapshot`` promises never to publish.  It therefore takes the
        transaction lane and every domain lock, in that order and in the same
        sorted order every cross-domain commit uses.

        ``wait=False`` is the reader's first attempt: the caller answers the
        last committed view instead of queueing behind a write.  ``owned`` is
        false in that case and the caller must not project.

        A caller that already holds domain locks is inside a write (the only
        such caller is a direct ``_emit`` from a mutation that is about to
        publish): projecting its own state is the answer, and reaching for the
        remaining domain locks would invert the lock order.
        """

        if self._holds_domain_locks():
            # Already inside a write; the same projection is what its caller
            # asked for.  Nothing further to take.
            acquired = self._mutation.acquire(blocking=wait)
            if not acquired:
                yield False
                return
            ident = threading.get_ident()
            with self._lock:
                if self._mutation_depth == 0:
                    self._mutation_owner = ident
                self._mutation_depth += 1
            try:
                yield True
            finally:
                with self._lock:
                    self._mutation_depth -= 1
                    if self._mutation_depth == 0:
                        self._mutation_owner = None
                self._mutation.release()
            return
        if not self._mutation.acquire(blocking=wait):
            yield False
            return
        ident = threading.get_ident()
        with self._lock:
            if self._mutation_depth == 0:
                self._mutation_owner = ident
            self._mutation_depth += 1
        locks = self._all_domain_locks()
        for lock in locks:
            lock.acquire()
        self._note_domain_hold(1)
        try:
            yield True
        finally:
            self._note_domain_hold(-1)
            for lock in reversed(locks):
                lock.release()
            with self._lock:
                self._mutation_depth -= 1
                if self._mutation_depth == 0:
                    self._mutation_owner = None
            self._mutation.release()

    def _publish_snapshot_view_if_absent(self) -> None:
        """Give a reader something to answer with before the first write lands."""

        with self._lock:
            if self._snapshot_view is not None:
                return
        with self._projection_scope():
            self._publish_initial_snapshot_view()

    def _publish_initial_snapshot_view(self) -> None:
        """Publish the pre-mutation projection for readers that arrive later."""

        projected = self._project_domains()
        view = self._assemble_snapshot(projected)
        with self._lock:
            if self._snapshot_view is None:
                self._snapshot_cache = (self._revision, projected)
                self._snapshot_view = view

    def _advance_revision(self) -> int:
        """Advance the shared revision under the state lock.

        Mutations run on the mutation lane, but service supervision and the
        watcher still update the revision from their own threads.  A bare
        ``self._revision += 1`` was a read-modify-write those two could lose;
        this is the one place the counter moves.
        """

        with self._lock:
            self._revision += 1
            self._snapshot_cache = None
            return self._revision

    def _restore_revision(self, value: object) -> None:
        """Put the revision back to a checkpoint, under the state lock."""

        with self._lock:
            self._revision = int(value)  # type: ignore[arg-type]
            self._snapshot_cache = None

    @classmethod
    def with_default_domains(
        cls,
        *,
        metadata_path: Path | str | None = None,
        config_path: Path | str | None = None,
        codex_home: Path | str | None = None,
        runtime_settings_path: Path | str | None = None,
        webdav_settings_path: Path | str | None = None,
        webdav_enabled_path: Path | str | None = None,
        claude_settings_path: Path | str | None = None,
        language_path: Path | str | None = None,
        runtime_root: Path | str | None = None,
        reset_transient_routing_state: bool = False,
    ) -> "CoreStore":
        """Construct production domains without importing the LiteLLM proxy.

        Existing Python modules remain the only parser and persistence layer
        for providers, Codex, runtime settings, and WebDAV.  A malformed
        optional user file degrades only that route during bootstrap so the
        host can still open the remaining settings windows.
        """

        from .domains._shared import _default_provider_config_path, _default_runtime_root, _default_runtime_settings_path
        from ..webdav import core as webdav_core
        from .domains.claude import ClaudeSettingsDomain
        from .domains.clients import ClientSettingsDomain
        from .domains.codex import CodexSettingsDomain
        from .domains.language import LanguageSettingsDomain
        from .domains.logs import LogsDomain
        from .domains.providers_models import ProvidersModelsDomain
        from .domains.relay_accounts import RelayAccountsDomain
        from .domains.runtime import RuntimeSettingsDomain
        from .domains.webdav import WebDAVSettingsDomain
        from .operations import CoreServiceController
        from .provider_auth import ProviderAuthManager
        from ..adapters.workbuddy import WorkBuddyRuntime
        from ..adapters.qoder import QoderRuntime
        from ..adapters.our_free_model import OurFreeModelRuntime

        controller = CoreServiceController(runtime_root)
        provider_auth = ProviderAuthManager(runtime_root)
        if reset_transient_routing_state:
            controller.reset_transient_routing_state()

        resolved_runtime_settings_path = (
            Path(runtime_settings_path).expanduser()
            if runtime_settings_path is not None
            else _default_runtime_settings_path()
        )
        resolved_runtime_root = (
            Path(runtime_root).expanduser() if runtime_root is not None else _default_runtime_root()
        )
        # This Core's WebDAV identity belongs to this Core's root.  A process
        # started with ``--runtime-root`` and no environment naming that root
        # used to resolve these four paths from the home directory, so it read
        # another installation's credentials, baseline and enabled flag and its
        # interval loop pushed its own document over that installation's remote
        # (2026-11-09: the probe instance at ``/tmp/yrprobe`` replaced the live
        # gateway bundle, and the gateway served zero models until it was
        # pulled back).  The ``LITELLM_WEBDAV_*`` variables still win: they name
        # one file for this process, exactly as ``LITELLM_CONFIG_FILE`` does.
        resolved_webdav_settings_path = (
            Path(webdav_settings_path).expanduser()
            if webdav_settings_path is not None
            else webdav_core.default_settings_file(resolved_runtime_root)
        )
        resolved_webdav_state_path = webdav_core.default_state_file(resolved_runtime_root)
        resolved_webdav_status_path = webdav_core.default_status_file(resolved_runtime_root)
        resolved_webdav_conflict_path = webdav_core.default_conflict_file(resolved_runtime_root)
        # One worker per Core process serves both WorkBuddy variants; the
        # service controller and the provider domain must share that instance
        # so the proxy child receives the very base URLs it is serving.
        workbuddy_runtime = WorkBuddyRuntime(resolved_runtime_root)
        controller.attach_workbuddy(workbuddy_runtime)
        # One worker per Core process serves both Qoder regions; the service
        # controller and the provider domain share that instance so the proxy
        # child receives the very base URLs it is serving.
        qoder_runtime = QoderRuntime(resolved_runtime_root)
        controller.attach_qoder(qoder_runtime)
        # One worker per Core process serves every Our Free Model route; the
        # service controller and the provider domain share that instance so the
        # proxy child receives the very base URL it is serving.
        runtime_settings = RuntimeSettingsDomain(
            resolved_runtime_settings_path, config_path=config_path
        )
        our_free_model_runtime = OurFreeModelRuntime(
            resolved_runtime_root,
            settings={
                "defaultMaxTokens": runtime_settings.snapshot()["values"][
                    "YOUNG_ROUTER_OUR_FREE_MODEL_MAX_TOKENS"
                ],
                "exposeRegionModels": runtime_settings.snapshot()["values"][
                    "YOUNG_ROUTER_OUR_FREE_MODEL_EXPOSE_REGION_MODELS"
                ],
                "probeIntervalMinutes": runtime_settings.snapshot()["values"][
                    "YOUNG_ROUTER_OUR_FREE_MODEL_REFRESH_INTERVAL_MINUTES"
                ],
                "standaloneProbe": runtime_settings.snapshot()["values"][
                    "YOUNG_ROUTER_OUR_FREE_MODEL_PROBE_ON_REFRESH"
                ],
                "sourceDir": runtime_settings.snapshot()["values"][
                    "YOUNG_ROUTER_OUR_FREE_MODEL_SOURCE_DIR"
                ],
            },
        )
        runtime_settings.attach_our_free_model(our_free_model_runtime)
        controller.attach_our_free_model(our_free_model_runtime)
        # A saved route's loopback base is resolved by the managed proxy and by
        # Core's own catalog/probe reads. Start its worker once, at Core launch;
        # a fresh provider adopts the same worker when it is created later.
        from ..adapters import our_free_model as our_free_model_module
        from ..config import load as config_load
        import yaml

        resolved_config_path = (
            Path(config_path).expanduser()
            if config_path is not None
            else _default_provider_config_path()
        )
        try:
            document = config_load.config_document_from_path(
                resolved_config_path,
                allow_missing=True,
            )
            draft = config_load.load_config_document(document)
            # ``_model_to_editor`` records a route's provider. A route that
            # states only its provider's base inherits it, so the route's
            # provider owns the launch decision.
            adopted_provider_names = {
                str(provider.get("name") or "")
                for provider in draft["providers"]
                if isinstance(provider, Mapping)
                and provider.get("auth_kind") in our_free_model_module.OUR_FREE_MODEL_AUTH_KINDS
            }
            adopted_route = any(
                model.get("provider") in adopted_provider_names
                for provider in draft["providers"]
                if isinstance(provider, Mapping)
                for model in provider.get("models", [])
                if isinstance(model, Mapping)
            )
            if adopted_route:
                our_free_model_runtime.autostart_if_configured()
        except (OSError, ValueError, yaml.YAMLError):
            # The Providers pane owns the error for a malformed optional
            # document; it must not prevent the other settings from opening.
            pass
        adapters: list[DomainAdapter] = []
        settings_factories: tuple[tuple[str, Callable[[], DomainAdapter]], ...] = (
            (
                "providers_models",
                lambda: ProvidersModelsDomain(
                    resolved_config_path,
                    auth_manager=provider_auth,
                    workbuddy=workbuddy_runtime,
                    qoder=qoder_runtime,
                    our_free_model=our_free_model_runtime,
                ),
            ),
            (
                "codex",
                lambda: CodexSettingsDomain(
                    config_path,
                    codex_home=codex_home,
                    runtime_settings_path=resolved_runtime_settings_path,
                    runtime_root=resolved_runtime_root,
                ),
            ),
            ("runtime", lambda: runtime_settings),
            (
                "webdav",
                lambda: WebDAVSettingsDomain(
                    resolved_webdav_settings_path,
                    enabled_path=webdav_enabled_path,
                    status_path=resolved_webdav_status_path,
                    state_path=resolved_webdav_state_path,
                    conflict_path=resolved_webdav_conflict_path,
                    config_path=config_path,
                ),
            ),
        )
        for name, factory in settings_factories:
            try:
                adapters.append(factory())
            except Exception:
                # Never claim Apply succeeded against a placeholder when the
                # user's real source is malformed.
                adapters.append(UnavailableDomain(name))
        claude_factory: Callable[[], DomainAdapter] = lambda: ClaudeSettingsDomain(
            claude_settings_path,
            runtime_config_path=config_path,
            runtime_settings_path=resolved_runtime_settings_path,
            runtime_root=resolved_runtime_root,
        )
        try:
            adapters.append(claude_factory())
        except Exception:
            adapters.append(RecoverableDomain("claude", claude_factory))
        clients_factory: Callable[[], DomainAdapter] = lambda: ClientSettingsDomain()
        try:
            adapters.append(clients_factory())
        except Exception:
            adapters.append(RecoverableDomain("clients", clients_factory))
        language_factory: Callable[[], DomainAdapter] = lambda: LanguageSettingsDomain(language_path)
        try:
            adapters.append(language_factory())
        except Exception:
            adapters.append(RecoverableDomain("language", language_factory))
        relay_accounts_domain: DomainAdapter
        try:
            relay_path = None
            if config_path is not None:
                from ..webdav import core as webdav_core

                relay_path = webdav_core.relay_accounts_path(Path(config_path).expanduser())
            relay_accounts_domain = RelayAccountsDomain(runtime_root, storage_path=relay_path)
        except Exception:
            relay_accounts_domain = UnavailableDomain("relay_accounts")
        adapters.append(relay_accounts_domain)
        # The log domain reads relay dashboards, and a dashboard token is minted
        # from the login the relay-account domain holds.  Handing that store over
        # lets a refresh token the station rotates during a usage read be kept,
        # which is what the next read mints its token from: a token kept only in
        # memory would be lost on the next restart, and the account would have to
        # be signed in again.
        adapters.append(
            LogsDomain(
                resolved_runtime_root,
                config_path=resolved_config_path,
                runtime_settings_path=runtime_settings_path,
                relay_accounts=relay_accounts_domain,
            )
        )
        operations = (
            "start",
            "stop",
            "restart",
            "reload",
            "health",
            "status",
            "autostart_enable",
            "autostart_disable",
            "autostart_status",
            "launch_background_enable",
            "launch_background_disable",
            "launch_background_status",
        )
        service_handlers = {operation: controller.dispatch for operation in operations}
        # Staging the runtime document is what the proxy actually loads, so it
        # answers whether a commit is one the proxy can see.  It is not a
        # lifecycle operation: it never touches the child process.
        service_handlers["stage_runtime_config"] = controller.stage_runtime_config
        service_handlers["runtime_fingerprint"] = controller.runtime_fingerprint
        # Core's own listener is only known once it is started, which is after
        # this store exists; the entry point reports it through this handler so
        # the proxy it launches can forward the page and its API.
        service_handlers["set_web_endpoint"] = controller.set_web_endpoint
        initial_service = controller.status()
        store = cls(
            metadata_path=metadata_path,
            domains=adapters,
            service_handlers=service_handlers,
            workbuddy_runtime=workbuddy_runtime,
            qoder_runtime=qoder_runtime,
            our_free_model_runtime=our_free_model_runtime,
        )
        if isinstance(initial_service, Mapping):
            store._set_service_from_result(initial_service, increment=False)
        return store

    def shutdown(self, *, stopped_by: str = "app_exit") -> dict[str, Any]:
        """Stop only the proxy owned by this Core before the native host exits.

        ``stopped_by`` is written beside the stop, because "stopped" on its own
        does not say who stopped it and the two answers are worlds apart.  A
        host replaces Core with SIGTERM -- a failed session renewal, a request
        that outlived its budget -- and the stop it leaves behind used to be
        indistinguishable from the user pressing 停止服务: the replacement came
        up reading a record that claimed a decision nobody made, and the window
        it handed over said 已停止 over a router that was serving a second
        earlier.  ``core_replaced`` publishes that reason with the state; the
        app's own quit keeps ``app_exit``.  The state itself is the truth about
        the child and stays ``stopped`` either way -- what the record no longer
        does is imply that a person asked for it.
        """

        self._stop_webdav_scheduler()
        self._stop_image_context_sweeper()
        # A sign-in window this Core opened belongs to this Core: a replacement
        # or a quit must not leave a browser running on a throwaway profile that
        # nobody will ever collect.
        self._close_browser_logins()
        # Nobody may start a proxy behind this stop.  The flag is set before
        # the loop is joined, so a pass that is between its check and its own
        # start sees it; a pass already inside the replacement holds the
        # transition guard, and the stop below waits for exactly that.
        with self._lock:
            self._service_supervision_stopping = True
        self._stop_service_supervisor()
        with self._service_reload_guard:
            self._service_reload_stopping = True
            reload_thread = self._service_reload_thread
        if reload_thread is not None and reload_thread is not threading.current_thread():
            # A restart may already be replacing the proxy. Give it a bounded
            # moment to settle; the worker stops whatever it started once it
            # observes the stopping flag, so no proxy outlives this Core.
            reload_thread.join(timeout=self._SERVICE_RELOAD_SHUTDOWN_JOIN_SECONDS)
        with self._lock:
            handler = self._shutdown_handler
            if handler is None:
                self._service = {"state": "stopped"}
        if handler is None:
            # Worker shutdown stops child processes: seconds of waiting that
            # no snapshot may be queued behind.
            self._stop_managed_workers()
            self.stop_jobs()
            self._emitter.stop()
            return dict(self._service)
        acquired = self._service_transition_guard.acquire(
            timeout=self._SERVICE_TRANSITION_SHUTDOWN_WAIT_SECONDS
        )
        try:
            result = handler("stop")
        except Exception as exc:
            raise CoreError("service_error", safe_exception_message(exc)) from None
        finally:
            if acquired:
                self._service_transition_guard.release()
        with self._lock:
            if not isinstance(result, Mapping):
                raise CoreError("service_error", "LiteLLM service returned invalid status")
            self._set_service_from_result(result, increment=False)
            self._persist_metadata(stopped_by=stopped_by)
        self._stop_managed_workers()
        self.stop_jobs()
        # A commit that landed just before this quit still owes its event.
        self._emitter.stop()
        return dict(self._service)

    def _stop_managed_workers(self) -> None:
        """Stop every Core-owned worker; never fail Core shutdown."""

        for runtime in (self._workbuddy_runtime, self._qoder_runtime, self._our_free_model_runtime):
            if runtime is None:
                continue
            try:
                runtime.stop()
            except Exception:
                pass

    def _load_metadata(self) -> None:
        assert self._metadata_store is not None
        try:
            payload = self._metadata_store.read(default={})
        except PersistenceError:
            # The host passes this private file so the shared revision and
            # service projection survive a host-driven Core replacement. It is
            # optional local state: an unreadable or malformed file must never
            # prevent Core from starting, because the app would then have no
            # working Core at all. Starting fresh only risks one missed
            # revision comparison, never a wrong mutation.
            return
        if not payload:
            return
        if payload.get("version") != CORE_METADATA_VERSION:
            return
        revision = payload.get("revision", 0)
        if type(revision) is not int or revision < 0:
            return
        self._revision = revision
        service = payload.get("service")
        if isinstance(service, Mapping) and service.get("state") in SERVICE_STATES:
            self._service = _safe_public(service)
        intent = payload.get("service_intent")
        if isinstance(intent, str) and intent in SERVICE_INTENTS:
            self._service_intent = intent

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    @property
    def domains(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._domains)

    def register_domain(self, adapter: DomainAdapter) -> str:
        name_value = getattr(adapter, "name", None)
        name = _canonical_domain(name_value)
        if (
            not callable(getattr(adapter, "draft_state", None))
            or not callable(getattr(adapter, "snapshot", None))
            or not callable(getattr(adapter, "dispatch", None))
        ):
            raise CoreError("invalid_domain", "The settings domain is unavailable")
        with self._lock:
            self._domains[name] = adapter
            # Every registered domain owns a write lane from registration on, so
            # a scope declared for it can never race its own creation.
            self._domain_locks.setdefault(name, threading.RLock())
            current = self._adapter_draft_state(name)
            self._baselines[name] = copy.deepcopy(current)
            self._drafts[name] = {
                "dirty": False,
                "base_revision": self._revision,
                "validation": {"valid": True, "issues": []},
            }
            self._disk[name] = {"changed": False, "generation": 0, "keep_draft": False}
            self._disk_identities[name] = self._external_disk_identity(adapter)
        return name

    def settings_domain(self, name: str) -> DomainAdapter | None:
        """One registered settings domain, for a host that needs its own door.

        The browser host is such a host: its port and password are not routing
        settings, so they never travel through the snapshot or the draft.  The
        caller that stages them still needs the adapter that owns the document.
        """

        return self._domains.get(name)

    def _adapter_snapshot(self, name: str) -> object:
        adapter = self._domains.get(name)
        if adapter is None:
            raise DomainNotFound(name)
        try:
            value = adapter.snapshot()
        except CoreError:
            raise
        except Exception as exc:
            raise CoreError("domain_unavailable", safe_exception_message(exc)) from None
        if not isinstance(value, Mapping):
            raise CoreError("domain_unavailable", "The settings domain returned invalid state")
        return _safe_public(value)

    def _adapter_draft_state(self, name: str) -> object:
        adapter = self._domains.get(name)
        if adapter is None:
            raise DomainNotFound(name)
        try:
            return copy.deepcopy(adapter.draft_state())
        except CoreError:
            raise
        except Exception as exc:
            raise CoreError("domain_unavailable", safe_exception_message(exc)) from None

    def _record_relay_transient_update(
        self,
        *,
        was_dirty: bool,
        prior_baseline: object,
        prior_base_revision: object,
    ) -> None:
        """Keep an existing relay draft dirty across login/refresh reads.

        Login restoration and resource discovery update private session/cache
        state, but are not an implicit Apply of staged station, account, or
        remote-key CRUD. A clean relay state can adopt that update as its new
        baseline; a dirty one must retain the baseline from before the read.
        """

        if was_dirty:
            self._baselines["relay_accounts"] = copy.deepcopy(prior_baseline)
            base_revision = prior_base_revision if type(prior_base_revision) is int else self._revision
            self._mark_domain(
                "relay_accounts",
                dirty=True,
                base_revision=base_revision,
            )
            return
        self._baselines["relay_accounts"] = self._adapter_draft_state("relay_accounts")
        self._mark_domain(
            "relay_accounts",
            dirty=False,
            validation={"valid": True, "issues": []},
            base_revision=self._revision,
        )

    def _persist_metadata(self, *, stopped_by: str | None = None) -> None:
        """Write the Core record the next Core reads.

        ``stopped_by`` names who ended the service and is written only by a
        shutdown; an ordinary write leaves the key out, so a stale reason can
        never outlive the stop it described.  Its absence then means exactly
        one thing: this record was written by a running Core, not by a teardown.
        """

        if self._metadata_store is None:
            return
        document: dict[str, Any] = {
            "version": CORE_METADATA_VERSION,
            "revision": self._revision,
            "service": _safe_public(self._service),
            "service_intent": self._service_intent,
        }
        if stopped_by is not None:
            document["stopped_by"] = stopped_by
        try:
            self._metadata_store.write(document)
        except PersistenceError as exc:
            raise CoreError("state_unavailable", safe_exception_message(exc)) from None

    def _emit(self, snapshot: Mapping[str, Any] | None = None) -> None:
        """Publish one state event to every subscribed window.

        The event is the change since the last one, not the state itself: a
        commit names the rows it moved and a window applies those rows, so the
        payload of an edit is the edit.  The whole projection is sent only when
        a window has no state to patch from (first subscription, or a gap) or
        when no entity-level description exists (see ``_patch_event``).

        ``snapshot`` lets a caller that just built the projection hand it over
        instead of asking for the same projection twice: Apply already answers
        its caller with one, and rebuilding it under this lock only to serialize
        it for the fan-out was half of a commit's cost.

        Nothing is projected when no window is listening.  A background launch,
        a closed settings window, and the gap while a window reloads all publish
        to nobody, and building a projection those callers would each discard is
        the same waste a second parse would be: the work is only worth doing for
        a reader that exists.

        The live service row is refreshed either way.  That read is a side effect
        of the projection rather than of publishing -- a commit's own restart
        decision asks whether the proxy is running, and the commit refreshes the
        row where it answers (see ``_request_emit``); this keeps a direct caller
        of ``_emit`` from publishing over a stale one -- so it is kept explicit
        here instead of riding along on work nobody consumes.  It is a memoized
        read, cheap enough to pay on every snapshot.
        """

        # A send outside the emitter thread answers whatever was pending, so
        # that request must not also produce a second event a window later.
        self._emitter.claim()
        with self._lock:
            # Subscriptions open and close from the IPC threads; the fan-out
            # runs on the emitter thread (or on a caller that sends directly),
            # so the tuple has to be taken under the state lock or an
            # unsubscribe would resize the dict mid-iteration.
            callbacks = tuple(self._subscribers.values())
        if not callbacks:
            self._service_row_is_live()
            return
        # Projection happens outside ``_publish_guard``: a projection takes the
        # mutation lane, and a mutation that publishes directly would then be
        # waiting for this lock while holding the lane this one waits for.
        # Publishing under the guard keeps the recorded chain ordered; a
        # projection that lost the race is refused by the revision check below.
        projected = self.snapshot() if snapshot is None else snapshot
        revision = projected.get("revision")
        if type(revision) is not int:
            return
        with self._publish_guard:
            with self._lock:
                previous = self._last_published_projection
                base_revision = self._last_published_revision
            if previous is None:
                event = make_event("snapshot", revision, projected)
            else:
                # A projection taken while another thread owns the mutation
                # lane is the last committed view, not the one this send was
                # asked about.  That is the same answered-by-a-later-commit
                # rule ``snapshot`` gives a window: publishing it after the
                # newer event would move the chain backwards, so the commit
                # that owns the lane asks for its own event instead.
                if revision < base_revision:
                    return
                try:
                    event = self._patch_event(projected, previous, base_revision)
                except Exception:
                    # The revision moved in a way no patch can describe: a diff
                    # that raised, a domain that cannot say what changed, or an
                    # op list too large to be worth sending.  Reading the full
                    # state is the answer that is always correct.
                    event = make_resync_event(revision)
                if event is None:
                    # Nothing about the projection changed, so the windows that
                    # already hold it owe no repaint and no read.
                    return
            # One serialization, then each subscriber gets its own parse of the
            # same bytes: a window that mutates its copy must not reach another's,
            # and a deepcopy of the whole projection was slower than the JSON the
            # event is about to become anyway.
            encoded = _encode_json(event)
            # The whole projection, not just its domains: the next patch has to
            # say whether a staged draft, the disk markers, the service row, or
            # an action summary moved as well, and "equal to what the windows
            # already hold" is exactly what this copy answers.  It is taken
            # before the lock for the same reason the event was encoded there:
            # an action summary can carry every model a station answered, and a
            # copy of that inside the critical section is a hold the budget
            # measures (see ``snapshot``).  Only a publisher reads this copy,
            # and ``_publish_guard`` is already serializing publishers.
            published = copy.deepcopy(dict(projected))
            with self._lock:
                self._last_published_projection = published
                self._last_published_revision = revision
            for callback in callbacks:
                try:
                    callback(json.loads(encoded))
                except Exception:
                    # A disconnected UI must not break a state transition.
                    continue

    def _patch_event(
        self,
        projected: Mapping[str, Any],
        previous: Mapping[str, Any],
        base_revision: int,
    ) -> dict[str, Any] | None:
        """Build the patch from ``previous``'s projection to ``projected``.

        ``None`` means nothing changed: a projection at the base revision with
        no moved rows owes the windows nothing.  A revision that moved but has
        no honest entity-level description raises ``_PatchUnavailable``, and
        the caller sends a resync instead: a patch that leaves a row out is a
        row the window keeps drawing, so an incomplete answer is worse than a
        full one.

        The ops describe the domains; the top-level fields a window cannot
        derive from them (a staged draft's dirty flag, the disk markers, the
        service row, an action summary) travel beside them, and only when they
        actually moved.
        """

        domains = projected.get("domains")
        previous_domains = previous.get("domains")
        revision = projected.get("revision")
        if not isinstance(domains, Mapping) or not isinstance(previous_domains, Mapping) or type(revision) is not int:
            raise _PatchUnavailable
        ops: list[dict[str, Any]] = []
        changed: list[str] = []
        for name, current in domains.items():
            if not isinstance(name, str):
                raise _PatchUnavailable
            before = previous_domains.get(name)
            if before == current:
                continue
            adapter = self._domains.get(name)
            if adapter is None:
                raise _PatchUnavailable
            try:
                domain_ops = domain_diff(adapter, before, current)
            except Exception:
                # A domain whose diff cannot answer must not take the event
                # channel down with it: the window reads the state instead.
                raise _PatchUnavailable from None
            if not domain_ops:
                raise _PatchUnavailable
            for op in domain_ops:
                if len(ops) >= MAX_PATCH_OPS:
                    raise _PatchUnavailable
                ops.append(op.to_mapping())
            changed.append(name)
        if not ops:
            if revision == base_revision:
                return None
            raise _PatchUnavailable
        # The fields a window cannot derive from the ops it just applied.  An
        # equal field is omitted: restating an unchanged draft or disk marker
        # would make every patch a snapshot with ops attached.
        top_level = {
            name: copy.deepcopy(projected.get(name))
            for name in PATCH_TOP_LEVEL_FIELDS
            if previous.get(name) != projected.get(name)
        }
        return make_patch_event(base_revision, revision, changed, ops, top_level)

    def _request_emit(self) -> None:
        """Mark one state event pending for the emitter.

        Write paths call this instead of ``_emit``: the response returns with
        its own revision, and the projection plus fan-out happens on the
        emitter thread once the burst settles.  ``_emit`` stays the
        synchronous send for a test that drives it and for a transitional
        claim that must be visible before the next statement runs.

        The live service row is **not** refreshed here.  That read is a real
        health probe of the proxy -- a loopback request with the controller's
        own timeout -- and doing it before returning from every commit put half
        a second of network wait on the write path every window shares.
        The publish already refreshes it: ``_emit`` reads it when nobody is
        listening and ``snapshot`` reads it for every send that has a reader.
        A commit whose *own* restart decision needs the answer (``apply``)
        reads it before it opens its write lane, so it still decides on the
        proxy it just observed rather than on a row from an earlier commit.
        """

        self._emitter.request()

    # -- jobs: work too slow to answer inside a request ---------------------
    def _job_executor(self) -> ThreadPoolExecutor:
        """The pool long-running operations run on, created on first use."""

        with self._job_guard:
            if self._job_pool is None:
                self._job_pool = ThreadPoolExecutor(
                    max_workers=JOB_POOL_WORKERS,
                    thread_name_prefix="young-router-core-job",
                )
            return self._job_pool

    def start_job(
        self,
        work: Callable[[], Mapping[str, Any]],
        *,
        name: str,
        on_done: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Start one long operation and answer with its id, not its result.

        The caller gets ``{revision, job_id}`` before any network work begins.
        ``running`` is published first -- before the id is returned -- so a
        subscriber that was already listening learns the job exists, and then
        ``done`` or ``failed`` carries the result.  The work runs on the job
        pool, which is what keeps a probe's own budget off the write lane and
        off the request that started it.
        """

        job_id = uuid.uuid4().hex
        with self._lock:
            revision = self._revision
        self._publish_job_event(job_id, "running", revision)
        try:
            self._job_executor().submit(self._run_job, job_id, name, work, on_done)
        except RuntimeError:
            # The pool is gone: this store is shutting down.  A failed job is
            # still an answer the caller can act on.
            self._publish_job_event(
                job_id, "failed", revision, {"detail": "The operation was cancelled"}
            )
        return {"revision": revision, "job_id": job_id}

    def _run_job(
        self,
        job_id: str,
        name: str,
        work: Callable[[], Mapping[str, Any]],
        on_done: Callable[[Mapping[str, Any]], None] | None,
    ) -> None:
        try:
            result = work()
        except Exception as exc:
            self._publish_job_event(
                job_id, "failed", self._current_revision(), {"detail": safe_exception_message(exc)}
            )
            return
        safe = _safe_public(result if isinstance(result, Mapping) else {})
        if not isinstance(safe, Mapping):
            safe = {}
        if on_done is not None:
            try:
                on_done(safe)
            except Exception:
                # Recording the answer must never turn it into a failure.
                pass
        self._publish_job_event(job_id, "done", self._current_revision(), dict(safe))

    def _current_revision(self) -> int:
        with self._lock:
            return self._revision

    def _publish_job_event(
        self,
        job_id: str,
        state: str,
        revision: int,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        """Send one job event to every subscribed window.

        Job events are not part of the state chain: they describe one caller's
        operation, not a revision a window has to apply in order, so they never
        touch ``_last_published_projection`` (the next patch must still be a
        patch of the state every window holds) and they are encoded once for
        all subscribers like every other event.
        """

        try:
            event = make_job_event(job_id, state, revision, result)
        except ProtocolError:
            # A result this wire cannot carry (a value that is not JSON, a
            # sentence with a newline) still owes the caller an answer.
            try:
                event = make_job_event(
                    job_id,
                    "failed",
                    revision,
                    {"detail": "The operation returned an invalid result"},
                )
            except ProtocolError:
                return
        try:
            encoded = _encode_json(event)
        except Exception:
            return
        with self._lock:
            callbacks = tuple(self._subscribers.values())
        for callback in callbacks:
            try:
                callback(json.loads(encoded))
            except Exception:
                # A disconnected UI must not break the operation.
                continue

    def stop_jobs(self) -> None:
        """End the job pool; a queued job never starts, a running one is left.

        A quit must not wait for a probe's own upstream budget.  Cancelling the
        queued work keeps an operation nobody is left to listen to from starting
        after the windows that asked for it are gone.
        """

        with self._job_guard:
            pool = self._job_pool
            self._job_pool = None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        if not callable(callback):
            raise CoreError("invalid_subscription", "A subscription callback is required")
        token = uuid.uuid4().hex
        with self._lock:
            self._subscribers[token] = callback

        def unsubscribe() -> None:
            with self._lock:
                self._subscribers.pop(token, None)

        return unsubscribe

    def snapshot(self) -> dict[str, Any]:
        """Project the current state for every window that asks for one.

        This is a projection, never a probe: nothing that can block on a
        subprocess, a network round trip, or a credential read may run on this
        path.  Every window in the app reads this method, so one such read here
        freezes all of them — a provider projection that reached a live worker
        once did exactly that.  A read of external state belongs to a user's
        own action, on its own lane, reporting on its own control.

        A window that is catching up asks for a revision it just heard about,
        and it asks several times per commit: the debounced Apply takes its own
        pre-Apply read, the action's response refreshes it again, and the
        coalesced event fans out a third.  Each of those reprojected ten domain
        snapshots — every one of which redacts and bounds its own state — under
        this lock.  That half is a function of the shared revision alone, so it
        is memoized on it, and every mutation bumps that revision under this
        same lock before publishing.  The service row and ``disk`` stay live:
        both move without a revision (a controller's ownership answer, a file
        that appeared), and a window must see either on its very next read.
        """

        # A mutation in another thread owns the adapters: their state is not
        # frozen just because this reader arrived, so projecting them here
        # would read a draft mid-write.  The last committed projection is the
        # truth every window already has; publish that instead of waiting
        # (which is what holding the lock used to do) or reading a half-written
        # one.  The service row is the exception: a restart publishes its
        # transition without a revision, and a window has to see 启动中 while
        # the controller is being replaced.
        def foreign_view() -> dict[str, Any] | None:
            with self._lock:
                foreign_lane = self._mutation_owner not in (None, threading.get_ident())
                foreign_domain = any(
                    holder != threading.get_ident() for holder in self._domain_holds
                )
                if (foreign_lane or foreign_domain) and self._snapshot_view is not None:
                    view = copy.deepcopy(self._snapshot_view)
                    view["service"] = copy.deepcopy(_safe_public(self._service))
                    return view
                return None

        # The controller status read is a probe, not a projection: it can spend
        # its endpoint timeout waiting for a proxy that is booting.  It used to
        # run under this lock on every snapshot, which is how a booting
        # controller froze every window for its probe budget.  Ask it here,
        # with no lock held, then publish what it answered.  It is asked even
        # while another thread mutates: the service row moves without a
        # revision, and a start that is already serving must stop saying
        # 启动中 before the operation's own answer lands.
        self._service_row_is_live()
        early = foreign_view()
        if early is not None:
            return early
        # A projection is a read of every domain at once, so it holds the
        # transaction lane and every domain lock (see ``_projection_scope``).
        # A lane already owned by another thread means a projection is either
        # in flight or about to be published; answer the last committed view
        # rather than queueing the reader behind it.
        with self._projection_scope(wait=False) as owned:
            if owned:
                # Adopting an external edit reloads a domain adapter and can
                # parse a whole settings document.  That is not a projection,
                # and it happens under this scope -- never under the state
                # lock -- so a reader sees the file and the state it implies
                # together.
                self._adopt_external_disk_changes()
                live: dict[str, Any] | None = None
                with self._lock:
                    view = self._snapshot_view
                    cached = self._snapshot_cache
                    if (
                        view is not None
                        and cached is not None
                        and cached[0] == self._revision
                        and view.get("revision") == self._revision
                    ):
                        # Only the small live fields are copied under the lock.
                        # The domain projection can be hundreds of milliseconds
                        # of deep-copying; it is copied below, with no lock held.
                        live = {
                            "service": copy.deepcopy(_safe_public(self._service)),
                            "drafts": copy.deepcopy(self._drafts),
                            "disk": copy.deepcopy(self._disk),
                            # A fetch result can carry hundreds of model names.
                            # Every writer replaces a domain's entry whole, so the
                            # outer dict is copied here and the entries are
                            # copied below, with no lock held.
                            "action_summaries": dict(self._last_actions),
                        }
                    else:
                        view = None
                if view is not None and live is not None:
                    result = copy.deepcopy(view)
                    result.update(live)
                    result["action_summaries"] = copy.deepcopy(result["action_summaries"])
                    return result
                # A projection is not a read of one field: it walks ten domains
                # and deep-copies what they answer, which measured 270-375 ms on
                # a real installation.  It runs under this scope, never under
                # the state lock.
                return self._project_and_publish()
        fallback = foreign_view()
        if fallback is not None:
            return fallback
        # No committed view exists to answer with yet (the very first read of a
        # Core's life): this one waits for the write in flight.
        with self._projection_scope():
            self._adopt_external_disk_changes()
            return self._project_and_publish()

    def _project_and_publish(self) -> dict[str, Any]:
        """Build and publish one projection while the caller owns the lane.

        The caller holds ``_mutation`` and no state lock: domain projections
        may take hundreds of milliseconds, and holding ``_lock`` across them
        was exactly the freeze the budget found.  A cache hit still takes the
        state lock briefly for the live fields.
        """

        with self._lock:
            view = self._snapshot_view
            cached = self._snapshot_cache
            if (
                view is not None
                and cached is not None
                and cached[0] == self._revision
                and view.get("revision") == self._revision
            ):
                live = {
                    "service": copy.deepcopy(_safe_public(self._service)),
                    "drafts": copy.deepcopy(self._drafts),
                    "disk": copy.deepcopy(self._disk),
                    "action_summaries": dict(self._last_actions),
                }
            else:
                view = None
                live = None
        if view is not None and live is not None:
            result = copy.deepcopy(view)
            result.update(live)
            result["action_summaries"] = copy.deepcopy(result["action_summaries"])
            return result
        projected = self._project_domains()
        view = self._assemble_snapshot(projected)
        with self._lock:
            self._snapshot_cache = (self._revision, projected)
            self._snapshot_view = view
        return view

    def _adopt_external_disk_changes(self) -> None:
        """Reload clean drafts whose files changed, without the state lock.

        The probe is a file read per domain; the adoption is a parse.  Both
        used to run inside ``snapshot``'s critical section, so a large settings
        document could hold the lock for the whole parse.  A change takes the
        mutation lane (the one writer of adapter state), and a snapshot with no
        change pays only the probe.
        """

        changed = False
        for adapter in self._domains.values():
            detector = getattr(adapter, "external_disk_state", None)
            if not callable(detector):
                continue
            try:
                state = detector()
            except Exception:
                continue
            if isinstance(state, Mapping) and state.get("changed"):
                changed = True
                break
        if not changed:
            return
        with self._mutation_scope():
            self._refresh_external_disk_state()

    def _service_row_is_live(self) -> None:
        """Project the controller's live answer into this read.

        The managed proxy can outlive a replaced Core briefly.  This is a
        read-only projection that deliberately neither persists nor emits:
        snapshot callers need the live status without creating a synthetic Core
        state transition.  The caller holds ``self._lock``.
        """

        reload_thread = self._service_reload_thread
        planned_restart = reload_thread is not None and reload_thread.is_alive()
        transitional = self._service.get("state") == "starting"
        owns_transition = (planned_restart or self._service_proxy_operations > 0) and transitional
        status_handler = self._service_handlers.get("status")
        if status_handler is None:
            return
        # A planned restart owns the transitional state: the controller reports
        # stopped/unhealthy — or nothing at all, while its own replacement is in
        # flight — and a cached status must not hide the planned restart from
        # the menu or the settings panes.
        #
        # A start is the exception, and only in one direction: it never has an
        # earlier proxy to report, so a running answer is the truth and the
        # window must see it even while the start's own follow-up work is still
        # under this lock.  Anything else (the child died, or the listener was
        # taken) leaves the announcement standing until the operation answers.
        if not owns_transition or self._service_start_operations > 0:
            result = status_handler("status")
            if not isinstance(result, Mapping):
                raise CoreError("service_error", "LiteLLM service returned invalid status")
            if not owns_transition or str(result.get("state")) == "running":
                self._set_service_from_result(result, increment=False)

    def _project_domains(self) -> dict[str, Any]:
        """Every domain projection a snapshot summarizes.

        This is the expensive half, and it is a function of the shared revision
        alone, which is what makes it memoizable.  The caller holds the lock.
        """

        # Relay snapshots display reverse dependency counts, while the
        # provider/model domain remains the sole source of binding truth.
        # Project only stable IDs and counts across this in-process seam;
        # no API base or credential is copied into the relay snapshot.
        relay = self._domains.get("relay_accounts")
        providers_domain = self._domains.get("providers_models")
        dependency_summary = getattr(providers_domain, "dependency_summary", None)
        set_binding_summary = getattr(relay, "set_binding_summary", None)
        if callable(dependency_summary) and callable(set_binding_summary):
            try:
                summary = dependency_summary()
                provider_keys = summary.get("provider_keys", []) if isinstance(summary, Mapping) else []
                resources: list[dict[str, Any]] = []
                if isinstance(provider_keys, Sequence) and not isinstance(provider_keys, (str, bytes, bytearray)):
                    for item in provider_keys:
                        if not isinstance(item, Mapping):
                            continue
                        source = item.get("source")
                        if not isinstance(source, Mapping) or source.get("kind") != "relay":
                            continue
                        count = item.get("model_count", 0)
                        if type(count) is not int or count < 0:
                            continue
                        resources.append(
                            {
                                "station_id": source.get("station_id"),
                                "account_id": source.get("account_id"),
                                "resource_id": source.get("resource_id"),
                                "linked_model_count": count,
                                "binding_status": "linked" if count else "independent",
                            }
                        )
                set_binding_summary({"resources": resources})
            except Exception:
                # A reverse-count projection is informational. Domain
                # validation during Apply still enforces binding health.
                pass
        domain_states = {name: self._adapter_snapshot(name) for name in self._domains}
        return {
            "providers_models": self._providers_summary(domain_states),
            "webdav": self._webdav_summary(domain_states),
            "logs": self._logs_summary(domain_states),
            "language": self._language_summary(domain_states),
            # The typed RN snapshot consumes the named fields above.  The
            # namespaced map keeps the contract extensible for Codex,
            # Claude, runtime, and future domains without another reducer.
            "domains": domain_states,
        }

    def _assemble_snapshot(self, domains: Mapping[str, Any]) -> dict[str, Any]:
        """Combine the memoized domain half with the live halves of a snapshot.

        The service row is read on every call because the controller answers it
        live and a planned restart is not a revision.  ``disk`` is read here for
        the same reason: the watcher publishes a detected change without a
        mutation.  Everything copied out belongs to the caller.
        """

        # ``disk`` is live, so it is read here on every call: the watcher
        # publishes a detected file change without that being a revision.  The
        # controller's service row is refreshed by ``snapshot`` before this
        # lock is taken; probing it here held the state lock across an endpoint
        # timeout.
        return {
            "protocol_version": PROTOCOL_VERSION,
            "revision": self._revision,
            "service": copy.deepcopy(_safe_public(self._service)),
            "providers_models": copy.deepcopy(domains["providers_models"]),
            "drafts": copy.deepcopy(self._drafts),
            "disk": copy.deepcopy(self._disk),
            "webdav": copy.deepcopy(domains["webdav"]),
            "logs": copy.deepcopy(domains["logs"]),
            "language": domains["language"],
            "action_summaries": copy.deepcopy(self._last_actions),
            "domains": copy.deepcopy(domains["domains"]),
        }

    def disk_state(self, domains: Sequence[str]) -> dict[str, Any]:
        """Refresh only the external-file markers needed by the settings UI."""

        if isinstance(domains, (str, bytes, bytearray)) or not isinstance(domains, Sequence):
            raise CoreError("invalid_domain", "The disk-state domains are invalid")
        try:
            requested = tuple(dict.fromkeys(_canonical_domain(domain) for domain in domains))
        except CoreError:
            raise
        if not requested:
            raise CoreError("invalid_domain", "At least one disk-state domain is required")
        with self._domain_scope(*requested):
            for name in requested:
                if name not in self._domains:
                    raise DomainNotFound(name)
            self._refresh_external_disk_state(requested)
            return {
                "revision": self._revision,
                "disk": {name: copy.deepcopy(self._disk[name]) for name in requested},
            }

    def log_view(self, tab: str, known_revision: int | None = None) -> dict[str, Any]:
        """Load one requested log tab without inflating global snapshots."""

        with self._domain_scope("logs"):
            adapter = self._domains.get("logs")
            view = getattr(adapter, "view", None) if adapter is not None else None
            if not callable(view):
                raise CoreError("logs_unavailable", "Logs are unavailable")
            try:
                result = view(tab, known_revision)
            except CoreError:
                raise
            except Exception as exc:
                raise CoreError("logs_unavailable", safe_exception_message(exc)) from None
            safe_result = _safe_public(result)
            if not isinstance(safe_result, Mapping):
                raise CoreError("logs_unavailable", "Logs are unavailable")
            return dict(safe_result)

    def _refresh_external_disk_state(self, domains: Iterable[str] | None = None) -> None:
        """Auto-reload clean drafts and expose only sanitized conflict state.

        This walks every domain, but it owns none of them: a caller holds the
        write lane of whatever it is actually editing, and the other domains are
        probed only when their own lane is free.  A domain another thread is
        writing is left alone until the next pass -- its detector reads the
        same state the write is rewriting -- which is why this may neither wait
        nor deadlock: an external edit adopts one pass later instead of queueing
        a commit behind it.
        """

        selected = None if domains is None else set(domains)
        for name, adapter in self._domains.items():
            if selected is not None and name not in selected:
                continue
            detector = getattr(adapter, "external_disk_state", None)
            if not callable(detector):
                continue
            lock = self._domain_lock(name)
            if not lock.acquire(blocking=False):
                # Another lane owns this domain right now; it settles its own
                # disk marker, and this pass leaves the record as it found it.
                continue
            self._note_domain_hold(1)
            try:
                record = self._disk.setdefault(name, {"changed": False, "generation": 0, "keep_draft": False})
                try:
                    state = detector()
                except Exception:
                    continue
                changed = bool(state.get("changed")) if isinstance(state, Mapping) else False
                identity = self._external_disk_identity(adapter)
                identity_changed = identity is not None and identity != self._disk_identities.get(name)
                if changed and (not record.get("changed") or identity_changed):
                    record["generation"] = int(record.get("generation", 0)) + 1
                    record["keep_draft"] = False
                if identity is not None:
                    self._disk_identities[name] = identity
                if changed and not self._drafts.get(name, {}).get("dirty"):
                    try:
                        adapter.reload()
                        self._baselines[name] = self._adapter_draft_state(name)
                        self._mark_domain(name, dirty=False, validation={"valid": True, "issues": []}, base_revision=self._revision + 1)
                        self._advance_revision()
                        changed = False
                        # This reload advances the shared revision without an
                        # event, so windows learn it from their own responses.
                        # Record it for the same reason every other bump does: a
                        # replacement Core must resume at or above every revision
                        # a window has seen.
                        try:
                            self._persist_metadata()
                        except PersistenceError:
                            # The reload itself succeeded; only the ordering
                            # record is optional. Never fail a disk probe over it.
                            pass
                    except Exception:
                        pass
                record["changed"] = changed
                if not changed:
                    record["keep_draft"] = False
            finally:
                self._note_domain_hold(-1)
                lock.release()

    @staticmethod
    def _external_disk_identity(adapter: DomainAdapter) -> str | None:
        """Read a bounded opaque identity that is never included in snapshots."""

        getter = getattr(adapter, "external_disk_identity", None)
        if not callable(getter):
            return None
        try:
            identity = getter()
        except Exception:
            return None
        if not isinstance(identity, str) or not identity or len(identity) > 256:
            return None
        return identity

    def _editor_adapter(self, domain: str, document: str) -> tuple[str, DomainAdapter]:
        if not isinstance(document, str) or not document:
            raise CoreError("invalid_editor", "The requested editor is unavailable")
        name = _canonical_domain(domain)
        adapter = self._domains.get(name)
        if name == "codex":
            if document not in {"config", "auth"}:
                raise CoreError("invalid_editor", "The requested editor is unavailable")
        elif name == "claude":
            if document not in {"settings", "desktop", "developer"}:
                raise CoreError("invalid_editor", "The requested editor is unavailable")
        elif name == "clients":
            # Every external-client file is a registered document; the UI can
            # never name a path, only one of these Core-owned ids.
            documents = getattr(adapter, "documents", ())
            if document not in documents:
                raise CoreError("invalid_editor", "The requested editor is unavailable")
        elif name == "webdav":
            # A stopped sync shows its two sides here: the synced files are the
            # only documents this domain has, and there is nothing to type into
            # them -- the reader compares and then chooses a side.
            documents = getattr(adapter, "review_documents", ()) if adapter is not None else ()
            if document not in documents:
                raise CoreError("invalid_editor", "The requested editor is unavailable")
        else:
            raise CoreError("invalid_editor", "The requested editor is unavailable")
        if adapter is None:
            raise DomainNotFound(name)
        return name, adapter

    def client_files(self) -> dict[str, Any]:
        """List the external client configuration files the UI may edit.

        This read-only operation is the single Core surface that returns a
        local path, and it only ever names the registered files owned by the
        Codex, Claude, and external-client adapters.  File contents never
        travel here; they stay behind the authenticated editor lease.  Every
        row carries the file's real ``path`` for the pane's own actions next to
        the home-shortened ``display_path`` it shows.
        """

        with self._domain_scope("codex", "claude", "clients"):
            files: list[dict[str, Any]] = []
            for name in ("codex", "claude", "clients"):
                adapter = self._domains.get(name)
                provider = getattr(adapter, "client_files", None)
                if not callable(provider):
                    continue
                try:
                    rows = provider()
                except Exception:
                    # One unavailable client must not hide the files of the
                    # others; the pane simply lists what Core can manage.
                    continue
                if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
                    continue
                for row in rows:
                    if not isinstance(row, Mapping):
                        continue
                    entry = dict(row)
                    path = entry.get("path")
                    if isinstance(path, str) and path:
                        entry["display_path"] = _display_path(path)
                    files.append(entry)
            return {"revision": self._revision, "files": files}

    def editor_document(self, domain: str, document: str) -> dict[str, Any]:
        """Read one versioned editor document as a single Core operation.

        The editor lease is derived from both the document text and its Core
        revision.  Reading those separately leaves a small race where an
        unrelated state update can advance the revision after the descriptor
        is created but before its text is read, producing a false conflict.
        Keep them under the same lock so a newly opened editor always receives
        a self-consistent capability baseline.
        """

        with self._domain_scope(domain):
            name, _adapter = self._editor_adapter(domain, document)
            return {
                "domain": name,
                "document": document,
                "revision": self._revision,
                "text": self._trusted_editor_text_unlocked(domain, document),
                "baseline": self._trusted_editor_baseline_text_unlocked(domain, document),
            }

    def _trusted_editor_text_unlocked(self, domain: str, document: str) -> str:
        """Read one editor document while the caller holds ``self._lock``."""

        name, adapter = self._editor_adapter(domain, document)
        try:
            if name == "codex":
                exporter = getattr(adapter, "export", None)
                if not callable(exporter):
                    raise CoreError("invalid_editor", "The requested editor is unavailable")
                raw = exporter(include_sensitive=True)
                key = "config_text" if document == "config" else "auth_text"
                text = raw.get(key) if isinstance(raw, Mapping) else None
            elif name == "clients":
                reader = getattr(adapter, "raw_text", None)
                text = reader(document) if callable(reader) else None
            elif name == "webdav":
                # The local side of the comparison: the file as it is on disk,
                # which is the state the stopped sync compared.
                reader = getattr(adapter, "review_document", None)
                text = reader(document) if callable(reader) else None
            else:
                raw_text = getattr(adapter, "raw_text", None)
                text = raw_text(include_sensitive=True, document=document) if callable(raw_text) else None
        except CoreError:
            raise
        except Exception as exc:
            raise CoreError("editor_unavailable", safe_exception_message(exc)) from None
        if not isinstance(text, str):
            raise CoreError("editor_unavailable", "The requested editor is unavailable")
        if len(text.encode("utf-8")) > MAX_EDITOR_DOCUMENT_BYTES:
            raise CoreError("editor_too_large", "The editor document exceeds the size limit")
        return text

    def _trusted_editor_baseline_text_unlocked(self, domain: str, document: str) -> str:
        """Render one editor document from Core's last applied draft state.

        Raw editors are allowed to read sensitive text only through this
        authenticated path.  The normal document text is the staged draft;
        the matching baseline must instead remain the last applied/reloaded
        state so structured UI edits are included in the diff.
        """

        name, _adapter = self._editor_adapter(domain, document)
        state = self._baselines.get(name)
        try:
            if name == "codex":
                if not isinstance(state, Mapping):
                    raise ValueError
                key = "config_text" if document == "config" else "auth_text"
                text = state.get(key)
            elif name == "clients":
                reader = getattr(_adapter, "baseline_text", None)
                text = reader(document) if callable(reader) else None
            elif name == "webdav":
                # What the local side is compared against: the remote copy the
                # stopped run kept aside, so the diff is the same two texts the
                # sync itself saw.
                reader = getattr(_adapter, "review_baseline", None)
                text = reader(document) if callable(reader) else None
            else:
                if not isinstance(state, Mapping):
                    raise ValueError
                if document == "settings":
                    # A standalone Claude Code domain stores its draft object
                    # directly. When a Claude Desktop source is enabled,
                    # ``draft_state`` wraps it under ``settings``.
                    value = state.get("settings") if "desktop" in state or "developer" in state else state
                elif document == "desktop":
                    desktop = state.get("desktop")
                    value = desktop.get("config") if isinstance(desktop, Mapping) else None
                else:
                    value = state.get("developer")
                if not isinstance(value, Mapping):
                    raise ValueError
                text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        except (TypeError, ValueError, UnicodeError):
            raise CoreError("editor_unavailable", "The requested editor is unavailable") from None
        if not isinstance(text, str):
            raise CoreError("editor_unavailable", "The requested editor is unavailable")
        if len(text.encode("utf-8")) > MAX_EDITOR_DOCUMENT_BYTES:
            raise CoreError("editor_too_large", "The editor document exceeds the size limit")
        return text

    @staticmethod
    def _editor_text_digest(text: str) -> str:
        """Return an opaque editor-content identity for capability rebasing."""

        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def trusted_editor_text(self, domain: str, document: str, *, revision: int) -> str:
        """Read raw text for the authenticated, versioned editor path."""

        with self._domain_scope(domain):
            self._check_revision(revision)
            return self._trusted_editor_text_unlocked(domain, document)

    def stage_editor_text(
        self,
        domain: str,
        document: str,
        text: str,
        *,
        revision: int,
        expected_text_digest: str | None = None,
    ) -> dict[str, Any]:
        """Stage one versioned code-editor document."""

        if not isinstance(text, str):
            raise CoreError("invalid_editor", "The editor document is invalid")
        if len(text.encode("utf-8")) > MAX_EDITOR_DOCUMENT_BYTES:
            raise CoreError("editor_too_large", "The editor document exceeds the size limit")
        with self._domain_scope(domain):
            name, _adapter = self._editor_adapter(domain, document)
            if name == "webdav":
                # The comparison is a read of two documents, not an editor: a
                # staged text would have to become a config file without the
                # sync's own validation, so this surface refuses every write.
                raise CoreError("invalid_editor", "The requested editor is unavailable")
            # Global Core revisions also advance for unrelated operations such
            # as status/menu telemetry. A raw editor must not turn one of
            # those into a false "changed outside this window" conflict. When
            # the document the capability originally read is still identical,
            # safely rebase only that trusted capability onto the current Core
            # revision. A real change to this document continues to reject.
            self._refresh_external_disk_state()
            if expected_text_digest is not None and revision != self._revision:
                if self._editor_text_digest(self._trusted_editor_text_unlocked(domain, document)) != expected_text_digest:
                    raise RevisionConflict(revision, self._revision)
                revision = self._revision
            self._check_revision(revision)
            payload: dict[str, Any]
            if name == "codex" or name == "clients":
                payload = {"document": document, "text": text}
            else:
                payload = {"document": document, "raw_json": text}
            result = self.dispatch(
                {"domain": name, "type": "set_raw", "payload": payload},
                expected_revision=revision,
                _trusted_native_capability=True,
            )
            return {
                **result,
                "baseline": self._trusted_editor_baseline_text_unlocked(domain, document),
            }

    def secret_descriptor(self, domain: str, field: str, target: object | None = None) -> dict[str, Any]:
        """Validate one native-only secret slot without returning its value."""

        with self._domain_scope(domain):
            name = _canonical_domain(domain)
            field_name = field.strip() if isinstance(field, str) else ""
            target_required = _SECRET_FIELDS.get((name, field_name))
            if target_required is None:
                raise CoreError("invalid_secret", "The requested secret field is unavailable")
            if target_required:
                if not isinstance(target, str) or not target or len(target.encode("utf-8")) > 256:
                    raise CoreError("invalid_secret", "The requested secret field is unavailable")
                target_value: str | None = target
            else:
                if target not in (None, ""):
                    raise CoreError("invalid_secret", "The requested secret field is unavailable")
                target_value = None
            adapter = self._domains.get(name)
            presence = getattr(adapter, "secret_present", None) if adapter is not None else None
            staging = getattr(adapter, "stage_secret", None) if adapter is not None else None
            if not callable(presence) or not callable(staging):
                raise CoreError("invalid_secret", "The requested secret field is unavailable")
            try:
                present = bool(presence(field_name, target_value))
            except Exception:
                raise CoreError("invalid_secret", "The requested secret field is unavailable") from None
            return {
                "domain": name,
                "field": field_name,
                "target": target_value,
                "revision": self._revision,
                "present": present,
            }

    def _trusted_secret_document_read(self, domain: str, field: object, target: object | None) -> tuple[dict[str, Any], str]:
        """Validate one plaintext-native field and read its live value.

        Deliberately lock-free: the caller decides how long it may wait for the
        store, because the value is drawn on screen already.  Every lookup here
        is a read of the document this process already holds.
        """

        name = _canonical_domain(domain)
        field_name = field.strip() if isinstance(field, str) else ""
        descriptor_key = (name, field_name)
        if descriptor_key not in _PLAINTEXT_SECRET_FIELDS:
            raise CoreError("invalid_secret", "The requested secret field is unavailable")
        if descriptor_key == ("runtime", "setting") and target not in _PLAINTEXT_RUNTIME_TARGETS:
            raise CoreError("invalid_secret", "The requested secret field is unavailable")
        target_required = _SECRET_FIELDS[descriptor_key]
        if target_required and (
            not isinstance(target, str)
            or not target
            or len(target.encode("utf-8")) > 256
        ):
            raise CoreError("invalid_secret", "The requested secret field is unavailable")
        if not target_required and target not in (None, ""):
            raise CoreError("invalid_secret", "The requested secret field is unavailable")
        target_value = target if target_required else None
        adapter = self._domains.get(name)
        reader = getattr(adapter, "trusted_secret_value", None) if adapter is not None else None
        if not callable(reader):
            raise CoreError("invalid_secret", "The requested secret field is unavailable")
        try:
            value = reader(field_name, target_value)
        except Exception:
            raise CoreError("invalid_secret", "The requested secret field is unavailable") from None
        if (
            not isinstance(value, str)
            or len(value.encode("utf-8")) > _secret_value_limit(name, field_name, target_value)
            or "\x00" in value
            or (not _allows_multiline_secret(name, field_name, target_value) and any(character in value for character in "\r\n"))
        ):
            raise CoreError("invalid_secret", "The requested secret field is unavailable")
        return {
            "domain": name,
            "field": field_name,
            "target": target_value,
            "revision": self._revision,
            "present": bool(value.strip()),
        }, value

    def published_page_port(self) -> int:
        """The local port the browser page is actually reachable on.

        The page and the API share one address: `/ui` and `/ipc` are answered on
        the proxy's published port, which Core reaches only as the child it
        supervises -- so Core's own listener port is the wrong number to publish
        and is never one a person should be told to open.  A proxy that is not
        running (a stopped service, or a host whose page is served by Core
        alone) leaves the page on Core's own listener, which is where the
        terminal launch puts it.

        A projection, never a probe: it reads the status this store last
        published, exactly like every pane's own port row.
        """

        with self._lock:
            service = self._service
            port = service.get("port") if isinstance(service, Mapping) else None
            if str(service.get("state", "")) == "running" and isinstance(port, int) and port > 0:
                return port
        return 0

    def trusted_secret_descriptor(self, domain: str, field: str, target: object | None = None) -> dict[str, Any]:
        """Validate one explicitly plaintext-native credential field.

        These values are read only through a short-lived native capability.
        They are never included in snapshots or the React request protocol.
        """

        with self._lock:
            return self._trusted_secret_document_read(domain, field, target)[0]

    def trusted_secret_read_descriptor(
        self,
        domain: str,
        field: str,
        target: object | None = None,
    ) -> dict[str, Any]:
        """The descriptor of a plaintext read lease, without waiting.

        Issuing the lease is the first half of drawing a key that is already on
        screen, so it takes the store lock only when the lock is free: a relay
        round trip holding it must not delay the field behind that round trip.
        """

        locked = self._lock.acquire(timeout=_SECRET_READ_LOCK_TIMEOUT_SECONDS)
        try:
            return self._trusted_secret_document_read(domain, field, target)[0]
        finally:
            if locked:
                self._lock.release()

    def trusted_secret_value(
        self,
        domain: str,
        field: str,
        target: object | None,
        *,
        revision: int,
    ) -> str:
        """Read one plaintext field via an already-authorized native lease.

        A provider pane draws the key the moment it opens, so this read may not
        queue behind the store's own work: a relay login or a station refresh
        holds the store lock across its network round trip, and a key waiting
        for that lock stayed on its loading hint for as long as the round trip
        took.  The capability this call consumes was issued for a revision the
        store already authorized, so a busy store is not a reason to wait — the
        read uses the free lock when it can have it and the live document
        otherwise, and the caller re-reads after any later change.
        """

        locked = self._lock.acquire(timeout=_SECRET_READ_LOCK_TIMEOUT_SECONDS)
        try:
            if locked:
                self._check_revision(revision)
            _descriptor, value = self._trusted_secret_document_read(domain, field, target)
            return value
        finally:
            if locked:
                self._lock.release()

    def stage_secret(
        self,
        domain: str,
        field: str,
        target: str | None,
        value: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        """Stage a secret received only from an authenticated native control."""

        if not isinstance(value, str) or len(value.encode("utf-8")) > _secret_value_limit(_canonical_domain(domain), field, target) or "\x00" in value or (not _allows_multiline_secret(_canonical_domain(domain), field, target) and any(char in value for char in "\r\n")):
            raise CoreError("invalid_secret", "The secret value is invalid")
        should_emit = False
        with self._domain_scope(domain):
            self._check_revision(revision)
            descriptor = self.secret_descriptor(domain, field, target)
            name = str(descriptor["domain"])
            adapter = self._domains[name]
            before = copy.deepcopy(self._baselines.get(name))
            try:
                adapter.stage_secret(str(descriptor["field"]), descriptor["target"], value)  # type: ignore[attr-defined]
                after = self._adapter_draft_state(name)
                present = bool(adapter.secret_present(str(descriptor["field"]), descriptor["target"]))  # type: ignore[attr-defined]
            except Exception as exc:
                raise CoreError(
                    "invalid_secret",
                    safe_exception_message(exc, known_secrets=(value,)),
                ) from None
            dirty = after != before
            self._advance_revision()
            self._mark_domain(
                name,
                dirty=dirty,
                base_revision=(
                    self._revision
                    if not dirty
                    else self._drafts.get(name, {}).get("base_revision", self._revision)
                ),
            )
            self._persist_metadata()
            should_emit = True
            response = {"revision": self._revision, "present": present}
        if should_emit:
            self._request_emit()
        return response

    def import_relay_resources(
        self,
        account_id: object,
        resource_ids: object,
        *,
        revision: int,
        mode: object = "linked",
    ) -> dict[str, Any]:
        """Stage only the API resources explicitly selected by the user."""

        should_emit = False
        with self._domain_scope("relay_accounts", "providers_models"):
            self._check_revision(revision)
            relay = self._domains.get("relay_accounts")
            providers = self._domains.get("providers_models")
            importer = getattr(relay, "import_resources", None) if relay is not None else None
            if not isinstance(account_id, str) or not account_id or not callable(importer) or providers is None:
                raise CoreError("relay_import_failed", "Relay account is unavailable")
            import_mode = str(mode).strip().lower()
            if import_mode not in {"linked", "independent"}:
                raise CoreError("relay_import_failed", "Relay import mode is invalid")
            provider_checkpoint = _checkpoint_adapter(providers, error_code="relay_import_failed")
            try:
                result = importer(account_id, resource_ids, providers, mode=import_mode)
            except Exception as exc:
                try:
                    _restore_adapter(providers, provider_checkpoint)
                except Exception:
                    raise CoreError("relay_import_failed", "Provider/model import could not be rolled back") from None
                raise CoreError("relay_import_failed", safe_exception_message(exc)) from None
            self._advance_revision()
            provider_dirty = self._adapter_draft_state("providers_models") != self._baselines.get("providers_models")
            self._mark_domain(
                "providers_models",
                dirty=provider_dirty,
                base_revision=(
                    self._drafts.get("providers_models", {}).get("base_revision", self._revision)
                    if provider_dirty
                    else self._revision
                ),
            )
            self._last_actions["relay_accounts"] = {
                "resources_ready": True,
                "account_id": account_id,
                "import_mode": import_mode,
                "resource_count": int(result.get("resource_count", 0)) if isinstance(result, Mapping) else 0,
                "model_count": int(result.get("model_count", 0)) if isinstance(result, Mapping) else 0,
            }
            self._persist_metadata()
            should_emit = True
            response = {
                "revision": self._revision,
                "imported": True,
                "import_mode": import_mode,
                "resource_count": int(result.get("resource_count", 0)) if isinstance(result, Mapping) else 0,
                "model_count": int(result.get("model_count", 0)) if isinstance(result, Mapping) else 0,
            }
        if should_emit:
            self._request_emit()
        return response

    def accept_relay_login(
        self,
        *,
        account_id: object,
        account_type: object,
        label: object,
        origin: object,
        username: object,
        cookie: object = "",
        access_token: object = "",
        refresh_token: object = "",
        password: object = "",
        user_id: object = "",
        station_id: object = None,
        station_name: object = None,
        station_type: object = None,
        station_origin: object = None,
        remember_password: object = None,
        auto_grouping: object = None,
        pending_account: object = False,
    ) -> dict[str, Any]:
        """Accept one native browser session and expose only public account state.

        ``remember_password`` is the host's post-login prompt decision. An
        absent value keeps the account's stored preference; a boolean applies
        the choice, so a previously saved password can be retired from the
        same prompt.

        ``user_id`` is the station's own account id captured with this session;
        a New API fork requires it as the ``New-Api-User`` header on every later
        dashboard read, so the login that proves the session is where it is
        recorded.
        """

        remember_choice = remember_password if isinstance(remember_password, bool) else None
        auto_group_choice = auto_grouping if isinstance(auto_grouping, bool) else None
        known_secrets = tuple(
            value for value in (cookie, access_token, refresh_token, password) if isinstance(value, str) and value
        )
        should_emit = False
        with self._domain_scope("relay_accounts"):
            relay = self._domains.get("relay_accounts")
            accepter = getattr(relay, "accept_login_result", None) if relay is not None else None
            relay_checkpoint = getattr(relay, "transaction_checkpoint", None) if relay is not None else None
            relay_restore = getattr(relay, "restore_transaction", None) if relay is not None else None
            if (
                not callable(accepter)
                or not callable(relay_checkpoint)
                or not callable(relay_restore)
            ):
                raise CoreError("relay_login_failed", "Relay account is unavailable")
            relay_was_dirty = bool(self._drafts.get("relay_accounts", {}).get("dirty"))
            relay_prior_baseline = copy.deepcopy(self._baselines.get("relay_accounts"))
            relay_prior_base_revision = self._drafts.get("relay_accounts", {}).get("base_revision")
            relay_state = relay_checkpoint()
            core_checkpoint = {
                "revision": self._revision,
                "drafts": copy.deepcopy(self._drafts),
                "last_actions": copy.deepcopy(self._last_actions),
                "baselines": copy.deepcopy(self._baselines),
            }
            persistence_paths = list(_adapter_persistence_paths(relay))
            storage_path = getattr(relay, "storage_path", None)
            if isinstance(storage_path, (str, Path)):
                persistence_paths.append(Path(storage_path).expanduser())
            if self._metadata_store is not None:
                persistence_paths.append(self._metadata_store.path)
            try:
                file_checkpoints = _checkpoint_files(persistence_paths)
            except CoreError:
                raise CoreError("relay_login_failed", "Relay login could not be prepared") from None
            try:
                current = self._adapter_snapshot("relay_accounts")
                accounts = current.get("accounts", []) if isinstance(current, Mapping) else []
                if pending_account is True and isinstance(username, str) and username.strip():
                    # Adding a station asks for a login before the identity is
                    # known, so the duplicate can only be recognised here, once
                    # the station has said who it is.  A person signing the same
                    # account in again is not adding a second one: measured on
                    # lv, pressing add twice on one station left two rows with
                    # the same address, one 已登录 and one 未登录, each claiming
                    # a different balance.
                    finder = getattr(relay, "account_for_login_identity", None)
                    twin = finder(origin=str(origin or ""), username=username) if callable(finder) else ""
                    if isinstance(twin, str) and twin:
                        account_id = twin
                        pending_account = False
                        matching = next(
                            (item for item in accounts if isinstance(item, Mapping) and item.get("id") == twin),
                            None,
                        )
                        if isinstance(matching, Mapping):
                            label = matching.get("label", label)
                            account_type = matching.get("type", account_type)
                matching = next(
                    (item for item in accounts if isinstance(item, Mapping) and item.get("id") == account_id),
                    None,
                )
                if not isinstance(matching, Mapping):
                    # Pending login: the host started the webview before any
                    # account existed, so a successful sign-in creates the
                    # shell now. A cancelled flow never reaches this branch
                    # and leaves no reserved slot behind. Stale accepts for
                    # unknown accounts without the pending flag still fail.
                    creator = getattr(relay, "add_account_for_login", None)
                    if (
                        not callable(creator)
                        or pending_account is not True
                        or not isinstance(account_id, str)
                        or not account_id
                    ):
                        raise CoreError("relay_login_failed", "Relay account is unavailable")
                    creator(
                        account_id=account_id,
                        account_type=str(account_type) if isinstance(account_type, str) else "",
                        label=str(label) if isinstance(label, str) else "",
                        origin=str(origin) if isinstance(origin, str) else "",
                        station_id=str(station_id) if isinstance(station_id, str) and station_id else None,
                        station_name=str(station_name) if isinstance(station_name, str) and station_name else None,
                        station_type=str(station_type) if isinstance(station_type, str) and station_type else None,
                        station_origin=str(station_origin) if isinstance(station_origin, str) and station_origin else None,
                        remember_password=remember_choice is True,
                    )
                    current = self._adapter_snapshot("relay_accounts")
                    accounts = current.get("accounts", []) if isinstance(current, Mapping) else []
                    matching = next(
                        (item for item in accounts if isinstance(item, Mapping) and item.get("id") == account_id),
                        None,
                    )
                    if not isinstance(matching, Mapping):
                        raise CoreError("relay_login_failed", "Relay account is unavailable")
                if (
                    matching.get("type") != account_type
                    or matching.get("label") != label
                    or matching.get("origin") != origin
                ):
                    raise CoreError("relay_login_failed", "Relay account does not match the saved site")
                host_session = cookie if isinstance(cookie, str) else ""
                host_token = access_token if isinstance(access_token, str) else ""
                if host_session or host_token:
                    public = accepter(
                        str(account_id),
                        username=str(username),
                        cookie=host_session,
                        access_token=host_token,
                        refresh_token=str(refresh_token) if isinstance(refresh_token, str) else "",
                        password=str(password) if isinstance(password, str) else "",
                        user_id=user_id,
                        remember_password=remember_choice,
                        auto_grouping=auto_group_choice,
                    )
                else:
                    # A host with no browser of its own (the page host) sends
                    # the station account's credentials instead of a session
                    # cookie: a cookie minted in another tab is unreadable from
                    # the page, and the station's login page cannot be run
                    # under this app's origin.  Core posts the same form the
                    # station's page would have posted, so the session still
                    # arrives through the one verified-login boundary.  The
                    # codes name the reason the form has to show, since the
                    # station's own sentence is English Core prose and the
                    # pane's words are already localized.
                    from .domains.relay_accounts import (
                        RelayAccountsError,
                        RelaySessionLimitError,
                        RelayTransportError,
                    )

                    signer = getattr(relay, "sign_in_with_password", None)
                    if (
                        not callable(signer)
                        or not isinstance(username, str)
                        or not username.strip()
                        or not isinstance(password, str)
                        or not password
                    ):
                        raise CoreError("relay_login_failed", "Relay login result is incomplete")
                    try:
                        public = signer(
                            str(account_id),
                            username=username,
                            password=password,
                            remember_password=remember_choice,
                        )
                    except RelaySessionLimitError:
                        raise CoreError(
                            "relay_session_limit",
                            "Relay login was refused: the station's concurrent-session limit is reached",
                        ) from None
                    except RelayTransportError:
                        raise CoreError("relay_unreachable", "Relay is unavailable") from None
                    except RelayAccountsError:
                        raise CoreError("relay_login_rejected", "Relay login was rejected") from None
                # Login is the durable browser-session boundary. Resource
                # discovery is a separate explicit action so a station API
                # outage cannot change the result of a successful sign-in.
                self._advance_revision()
                self._record_relay_transient_update(
                    was_dirty=relay_was_dirty,
                    prior_baseline=relay_prior_baseline,
                    prior_base_revision=relay_prior_base_revision,
                )
                self._last_actions["relay_accounts"] = {
                    "logged_in": True,
                    "account_id": str(account_id),
                    "resources_ready": False,
                    "resource_count": 0,
                }
                self._persist_metadata()
            except CoreError:
                try:
                    _restore_files(file_checkpoints)
                    relay_restore(relay_state)
                finally:
                    self._restore_revision(core_checkpoint["revision"])
                    self._drafts = core_checkpoint["drafts"]
                    self._last_actions = core_checkpoint["last_actions"]
                    self._baselines = core_checkpoint["baselines"]
                raise
            except Exception as exc:
                rollback_failed = False
                try:
                    _restore_files(file_checkpoints)
                    relay_restore(relay_state)
                except Exception:
                    rollback_failed = True
                self._restore_revision(core_checkpoint["revision"])
                self._drafts = core_checkpoint["drafts"]
                self._last_actions = core_checkpoint["last_actions"]
                self._baselines = core_checkpoint["baselines"]
                if rollback_failed:
                    raise CoreError("relay_login_failed", "Relay login could not be rolled back") from None
                raise CoreError(
                    "relay_login_failed",
                    safe_exception_message(exc, known_secrets=known_secrets),
                ) from None
            should_emit = True
            response = {
                "revision": self._revision,
                "login_status": "signed_in",
                "username": str(public.get("username", username)),
            }
        if should_emit:
            self._request_emit()
        return response

    def start_relay_browser_login(
        self,
        *,
        account_id: object,
        account_type: object,
        label: object,
        origin: object,
        username: object = "",
        pending_account: object = False,
        station_id: object = None,
        station_name: object = None,
        station_type: object = None,
        station_origin: object = None,
        page_is_local: bool = True,
    ) -> dict[str, Any]:
        """Open a real browser at a station so a page host can sign in.

        The two native hosts present the station's own login page in a WebKit
        view they own; a browser page cannot (another origin's cookies are
        unreadable there, and the station's page cannot be served under this
        app's origin).  What it can do is ask the Core beside it — same
        machine, same person — to open that page in a real browser.  The
        window is the station's own, so a captcha, a code, or a redirect works
        exactly as it does in any browser; Core then reads the session that
        browser holds and finishes the same verified-login boundary the native
        hosts use.

        Nothing is created here: a pending account still exists only after the
        station accepted a session.  The window and the throwaway profile are
        this Core's, closed with it (see ``_close_browser_logins``).
        """

        from . import relay_browser

        # A published page is open in somebody else's browser -- a gateway, a
        # server, a machine on the LAN.  A window this Core opened would appear
        # on the server's screen (if it has one), which is not where the person
        # is; so is a machine with no screen at all.  Both run the browser
        # headless and stream its frames to the page.
        embedded = (not page_is_local) or relay_browser.embedded_display()
        reason = relay_browser.unavailable_reason()
        if reason:
            return {"status": "unsupported", "reason": reason}
        if not isinstance(origin, str) or not origin.strip():
            raise CoreError("relay_login_failed", "Relay origin is invalid")
        with self._lock:
            relay = self._domains.get("relay_accounts")
            if relay is None or not callable(getattr(relay, "verify_browser_session", None)):
                raise CoreError("relay_login_failed", "Relay account is unavailable")
            # A window somebody opened and walked away from holds a browser and
            # a profile; the next sign-in is the one moment this side can see it.
            # Taken out of the map here and closed *after* the lock: stopping a
            # process and waiting for it is seconds, and every window's snapshot
            # needs this lock.
            stale = self._reap_browser_logins()
            login_id = secrets.token_urlsafe(18)
            profile_root = self._relay_browser_profiles_root()
        for entry in stale:
            try:
                entry["browser"].close()
            except Exception:
                pass
        try:
            browser = relay_browser.launch(
                origin.strip(),
                profile_root=profile_root,
                account_id=str(account_id),
                embedded=embedded,
            )
        except relay_browser.BrowserUnavailable:
            return {"status": "unsupported", "reason": "no_browser"}
        except relay_browser.BrowserLaunchFailed:
            # A browser is installed and this launch did not reach it: the
            # window never appeared, so the pane must not pretend one is open.
            # The page says so and offers the credential form instead.
            return {"status": "unsupported", "reason": "launch_failed"}
        self._start_browser_login_reaper()
        with self._lock:
            self._browser_logins[login_id] = {
                "browser": browser,
                "account_id": str(account_id),
                "type": str(account_type),
                "label": str(label),
                "origin": origin.strip(),
                "username": str(username) if isinstance(username, str) else "",
                "pending_account": pending_account is True,
                "station_id": station_id,
                "station_name": station_name,
                "station_type": station_type,
                "station_origin": station_origin,
                "last_cookie": "",
            }
        result: dict[str, Any] = {"login_id": login_id, "browser": browser.browser.name, "status": "waiting"}
        mirror = getattr(browser, "mirror", None)
        if mirror is not None:
            # The page draws the station itself, from the frames this session
            # streams, at exactly the size the browser was opened.
            result["mirror"] = {"width": int(mirror.width), "height": int(mirror.height)}
        return result

    def relay_browser_mirror(self, login_id: object) -> Mapping[str, Any] | None:
        """The session one login streams from, or None.

        Frames and input are answered only for a login that exists here, which
        is what keeps one page from driving another person's browser: the id
        is minted per sign-in and never leaves the page that asked for it.
        """

        with self._lock:
            entry = self._browser_logins.get(str(login_id))
            if entry is not None:
                entry["browser"].last_seen_at = time.monotonic()
        if entry is None:
            return None
        browser = entry.get("browser")
        if browser is None or getattr(browser, "expired", False):
            return None
        mirror = getattr(browser, "mirror", None)
        return browser if mirror is not None else None

    def poll_relay_browser_login(self, login_id: object) -> dict[str, Any]:
        """Answer whether the browser window has produced a usable session.

        A jar read is not a login: the cookie is offered to the station, and
        only a profile answer accepts it.  A station that refuses (or a jar
        that holds only its own consent cookies) leaves the window open and the
        caller waiting; the cookie is remembered so the same refused jar is not
        sent to the station on every poll.
        """

        from .domains.relay_accounts import RelayAccountsError, RelayTransportError

        with self._lock:
            entry = self._browser_logins.get(str(login_id))
            if entry is not None:
                entry["browser"].last_seen_at = time.monotonic()
        if entry is None:
            return {"status": "unknown"}
        browser = entry["browser"]
        if browser.expired:
            self._forget_browser_login(str(login_id))
            return {"status": "expired"}
        if browser.process.poll() is not None:
            # The window is gone.  A session it produced before closing is
            # still a session — the person may have closed the window right
            # after the station answered — so the jar is read once more before
            # the close is reported.
            outcome = self._verify_browser_jar(str(login_id), entry)
            self._forget_browser_login(str(login_id))
            if outcome is not None:
                return outcome
            return {"status": "closed"}
        outcome = self._verify_browser_jar(str(login_id), entry)
        if outcome is not None:
            self._forget_browser_login(str(login_id))
            return outcome
        with self._lock:
            current = self._browser_logins.get(str(login_id))
            if current is None:
                return {"status": "closed"}
            if current["last_cookie"] == entry["last_cookie"] and current.get("last_error"):
                return {"status": "waiting", "detail": current["last_error"]}
        return {"status": "waiting"}

    def _verify_browser_jar(self, login_id: str, entry: Mapping[str, Any]) -> dict[str, Any] | None:
        """Offer one jar to the station; answer a finished login, or None.

        The jar is only sent when it changed since the last attempt, so a page
        polling once a second does not turn into one station login attempt a
        second.  ``None`` means "still waiting"; a returned mapping ends the
        sign-in.
        """

        from . import relay_browser
        from .domains.relay_accounts import RelayAccountsError, RelayTransportError

        browser = entry["browser"]
        try:
            cookies = browser.cookies()
            storage = browser.storage()
        except Exception:
            return None
        header = relay_browser.cookie_header(cookies, entry["origin"])
        token = ""
        for key in ("access_token", "token", "accessToken", "auth_token"):
            value = storage.get(key)
            if isinstance(value, str) and value.strip():
                token = value.strip()
                break
        if not header and not token:
            # Neither credential yet: the page is still the login form.
            return None
        user_id = _storage_user_id(storage)
        signature = f"{header}\u0000{token}\u0000{user_id}"
        if entry.get("last_cookie") == signature:
            return None
        entry["last_cookie"] = signature
        with self._lock:
            relay = self._domains.get("relay_accounts")
            verifier = getattr(relay, "verify_browser_session", None) if relay is not None else None
        if not callable(verifier):
            return None
        try:
            verified = verifier(
                origin=entry["origin"],
                account_type=entry["type"],
                cookie=header,
                access_token=token,
                user_id=user_id,
            )
        except RelayTransportError:
            return None
        except RelayAccountsError:
            # The station answered and did not accept this jar: the window stays
            # up and the person is still signing in.
            return None
        except Exception:
            return None
        return self._accept_browser_session(entry, verified)

    def _accept_browser_session(self, entry: Mapping[str, Any], verified: Mapping[str, Any]) -> dict[str, Any]:
        """Hand one verified browser session to the one login boundary."""

        try:
            result = self.accept_relay_login(
                account_id=entry["account_id"],
                account_type=entry["type"],
                label=entry["label"],
                origin=entry["origin"],
                username=str(verified.get("username") or entry["username"]),
                cookie=str(verified.get("cookie", "")),
                access_token=str(verified.get("access_token", "")),
                user_id=str(verified.get("user_id", "")),
                station_id=entry["station_id"],
                station_name=entry["station_name"],
                station_type=entry["station_type"],
                station_origin=entry["station_origin"],
                pending_account=entry["pending_account"] is True,
            )
        except CoreError as exc:
            return {"status": "rejected", "code": exc.code}
        return {"status": "signed_in", **{key: value for key, value in result.items() if key != "status"}}

    def focus_relay_browser_login(self, login_id: object) -> dict[str, Any]:
        """Bring one sign-in window back to the front.

        A window that opened behind the person's own browser, or onto another
        Space, is a window they cannot find; the waiting surface offers one
        press that raises it instead of leaving them to guess.
        """

        with self._lock:
            entry = self._browser_logins.get(str(login_id))
        if entry is None:
            return {"focused": False}
        try:
            focused = bool(entry["browser"].focus())
        except Exception:
            focused = False
        return {"focused": focused}

    def cancel_relay_browser_login(self, login_id: object) -> dict[str, Any]:
        """End one sign-in window, and answer whether there was one."""

        return {"cancelled": self._forget_browser_login(str(login_id))}

    def _forget_browser_login(self, login_id: str) -> bool:
        with self._lock:
            entry = self._browser_logins.pop(login_id, None)
        if entry is None:
            return False
        entry["browser"].close()
        return True

    def _reap_browser_logins(self) -> list[dict[str, Any]]:
        """Take out every sign-in that has expired or gone quiet, to be closed.

        Two ways one ends without anybody asking: the ten-minute lifetime, and
        a page that stopped polling — closed, crashed, or on a network that
        dropped, so its cancel never arrived.  A browser left by either is a few
        hundred megabytes on a host nobody is looking at, and the second one had
        no bound at all before this: `expired` was only ever read by a poll, and
        a page that never polls again never reads it.

        Called from every entry point AND from a timer, because the case that
        matters most is the one where no entry point is reached again.
        """

        now = time.monotonic()
        gone: list[dict[str, Any]] = []
        for key, entry in list(self._browser_logins.items()):
            browser = entry.get("browser")
            if browser is None:
                self._browser_logins.pop(key, None)
                continue
            if browser.expired or browser.idle(now):
                gone.append(self._browser_logins.pop(key))
        return gone

    def _start_browser_login_reaper(self) -> None:
        """One daemon thread that closes abandoned sign-ins, whatever the page does."""

        with self._lock:
            if self._browser_reaper is not None and self._browser_reaper.is_alive():
                return
            stop = threading.Event()

            def loop() -> None:
                while not stop.wait(BROWSER_LOGIN_REAP_SECONDS):
                    try:
                        for entry in self._reap_browser_logins():
                            try:
                                entry["browser"].close()
                            except Exception:
                                pass
                    except Exception:
                        pass

            self._browser_reaper_stop = stop
            self._browser_reaper = threading.Thread(target=loop, name="yr-browser-reaper", daemon=True)
            self._browser_reaper.start()

    def _close_browser_logins(self) -> None:
        with self._lock:
            stop = self._browser_reaper_stop
            if stop is not None:
                stop.set()
            self._browser_reaper_stop = None
            self._browser_reaper = None
            entries = list(self._browser_logins.values())
            self._browser_logins.clear()
        for entry in entries:
            try:
                entry["browser"].close()
            except Exception:
                pass

    def _relay_browser_profiles_root(self) -> Path:
        """Where throwaway sign-in profiles live.

        Beside the Core metadata when the host states one (both native hosts
        do) so a profile never lands in the person's home; a temporary
        directory otherwise.  Each launch makes its own subdirectory inside it.
        """

        if self._metadata_store is not None:
            root = Path(self._metadata_store.path).expanduser().parent / ".relay-browser"
        else:
            root = Path(tempfile.gettempdir()) / "young-router-relay-browser"
        return root

    def refresh_relay_resources(
        self,
        account_id: object,
        *,
        revision: int,
    ) -> dict[str, Any]:
        """Refresh selectable relay API metadata without staging providers."""

        should_emit = False
        with self._domain_scope("relay_accounts"):
            self._check_revision(revision)
            relay = self._domains.get("relay_accounts")
            refresher = getattr(relay, "refresh_resources", None) if relay is not None else None
            if not isinstance(account_id, str) or not account_id or not callable(refresher):
                raise CoreError("relay_resources_failed", "Relay account is unavailable")
            relay_was_dirty = bool(self._drafts.get("relay_accounts", {}).get("dirty"))
            relay_prior_baseline = copy.deepcopy(self._baselines.get("relay_accounts"))
            relay_prior_base_revision = self._drafts.get("relay_accounts", {}).get("base_revision")
            try:
                result = refresher(account_id)
            except Exception as exc:
                raise CoreError("relay_resources_failed", safe_exception_message(exc)) from None
            resources = result.get("resources", []) if isinstance(result, Mapping) else []
            resource_status = result.get("resource_status", "unavailable") if isinstance(result, Mapping) else "unavailable"
            resource_count = len(resources) if isinstance(resources, list) else 0
            self._advance_revision()
            self._record_relay_transient_update(
                was_dirty=relay_was_dirty,
                prior_baseline=relay_prior_baseline,
                prior_base_revision=relay_prior_base_revision,
            )
            self._last_actions["relay_accounts"] = {
                "resources_ready": resource_status == "ready",
                "account_id": account_id,
                "resource_status": resource_status,
                "resource_count": resource_count,
            }
            self._persist_metadata()
            should_emit = True
            response = {
                "revision": self._revision,
                "account_id": account_id,
                "resource_status": resource_status,
                "resource_count": resource_count,
            }
        if should_emit:
            self._request_emit()
        return response

    def restore_relay_session(
        self,
        *,
        account_id: object,
        account_type: object,
        label: object,
        origin: object,
        login_status: object,
        username: object = "",
        cookie: object = "",
        access_token: object = "",
        refresh_token: object = "",
        user_id: object = "",
        quota: object = None,
    ) -> dict[str, Any]:
        """Restore or update one locally validated native relay session.

        Unlike :meth:`accept_relay_login`, this never fetches API keys or
        modifies the provider/model draft. It is used after a Core restart to
        repopulate the process-local relay session from the per-account native
        credential store, or to persist an explicit expired/signed-out result.

        ``user_id`` is the station's own account id, which the host carries
        beside the session because a New API fork requires it as the
        ``New-Api-User`` header on every dashboard read.

        ``quota`` is the New API profile the host's own probe just read.  The
        probe that proves the session is the same answer the account's balance
        comes from, so carrying it here settles both in one write instead of
        paying a second read for a number the entry already had.
        """

        known_secrets = tuple(
            value for value in (cookie, access_token, refresh_token) if isinstance(value, str) and value
        )
        should_emit = False
        with self._domain_scope("relay_accounts"):
            relay = self._domains.get("relay_accounts")
            accepter = getattr(relay, "accept_login_result", None) if relay is not None else None
            status_setter = getattr(relay, "set_login_status", None) if relay is not None else None
            session_restorer = getattr(relay, "restore_saved_session", None) if relay is not None else None
            password_restorer = getattr(relay, "restore_saved_password", None) if relay is not None else None
            status_settler = getattr(relay, "settle_login_status", None) if relay is not None else None
            recent_login_answer = getattr(relay, "recent_login_check_answer", None) if relay is not None else None
            if (
                not callable(accepter)
                or not callable(status_setter)
                or not callable(session_restorer)
                or not callable(password_restorer)
                or not callable(status_settler)
                or not callable(recent_login_answer)
            ):
                raise CoreError("relay_restore_failed", "Relay account is unavailable")
            relay_was_dirty = bool(self._drafts.get("relay_accounts", {}).get("dirty"))
            relay_prior_baseline = copy.deepcopy(self._baselines.get("relay_accounts"))
            relay_prior_base_revision = self._drafts.get("relay_accounts", {}).get("base_revision")
            current = self._adapter_snapshot("relay_accounts")
            accounts = current.get("accounts", []) if isinstance(current, Mapping) else []
            matching = next(
                (item for item in accounts if isinstance(item, Mapping) and item.get("id") == account_id),
                None,
            )
            if not isinstance(matching, Mapping):
                raise CoreError("relay_restore_failed", "Relay account is unavailable")
            if (
                matching.get("type") != account_type
                or matching.get("label") != label
                or matching.get("origin") != origin
            ):
                raise CoreError("relay_restore_failed", "Relay account does not match the saved site")
            try:
                if login_status == "signed_in":
                    public = accepter(
                        str(account_id),
                        username=str(username),
                        cookie=str(cookie) if isinstance(cookie, str) else "",
                        access_token=str(access_token) if isinstance(access_token, str) else "",
                        refresh_token=str(refresh_token) if isinstance(refresh_token, str) else "",
                        user_id=user_id,
                        preserve_resources=True,
                        quota=quota,
                    )
                elif login_status in {"signed_out", "expired"} and matching.get("remember_password") is True:
                    # A login this Core already determined inside its re-check
                    # window answers the check with that observation, so the
                    # site is not asked to re-authenticate -- or to re-refuse --
                    # a login it answered moments ago.  Re-entering the pane
                    # inside the window therefore costs no station round trip,
                    # and a determined 未登录 stays 未登录 instead of being
                    # re-probed on every mount.
                    public = recent_login_answer(str(account_id))
                    # Whether the station was ever reached.  The host's
                    # ``signed_out`` is an unauthenticated probe, so it may only
                    # be recorded once a read actually arrived somewhere: a
                    # dropped connection or a timeout is not the station saying
                    # no, and recording it as one is what retired a session the
                    # user still held after a momentary network failure.
                    # ``RelayTransportError`` is raised exactly by a read that
                    # never reached an answer.
                    unreachable = False
                    # The station answered and refused the sign-in itself,
                    # because the account already holds as many concurrent
                    # sessions as the site allows.  The remembered password is
                    # not what was rejected, so recording 未登录 here would state
                    # a login the station never stated and would bury the one
                    # reason the user can act on.  The observation is left as it
                    # was and the caller names the refusal.
                    refused: CoreError | None = None
                    if public is None:
                        from .domains.relay_accounts import (
                            RelayLoginRefusedError,
                            RelaySessionLimitError,
                            RelayTransportError,
                        )

                        # A stored credential is asked whether it still works
                        # before any sign-in is attempted -- for ``expired``
                        # exactly as for ``signed_out``.  The ``expired`` branch
                        # used to go straight to the remembered password, so a
                        # cookie that still worked was thrown away and every entry
                        # made the station mint another session; an account at the
                        # site's session cap then could not sign in at all, from a
                        # password nobody had changed.  Probing costs one
                        # authenticated read and mints nothing, so a working
                        # cookie is simply reused and the count never grows.
                        try:
                            public = session_restorer(str(account_id))
                        except RelayTransportError:
                            unreachable = True
                        except Exception:
                            public = None
                        if public is None and not unreachable:
                            try:
                                # The probe above already answered for this
                                # credential, so the sign-in is reached with
                                # that answer in hand rather than repeating it.
                                public = password_restorer(str(account_id), force=True)
                            except RelayTransportError:
                                unreachable = True
                            except RelaySessionLimitError:
                                refused = CoreError(
                                    "relay_login_session_limit",
                                    "The station refused a new session, so the login was left unchanged",
                                )
                            except RelayLoginRefusedError:
                                refused = CoreError(
                                    "relay_login_backoff",
                                    "A recent sign-in was refused, so this one was not attempted yet",
                                )
                            except Exception:
                                public = status_setter(str(account_id), str(login_status))
                        # ``expired`` is the station's own rejection of its
                        # session, so it is recorded whether or not the renewal
                        # above reached the site; only the host's signed-out
                        # guess is dropped, and the caller is told the check did
                        # not happen instead of being given a login state.
                        if unreachable and login_status == "signed_out":
                            raise CoreError(
                                "relay_restore_unavailable",
                                "The station could not be reached, so the login was not re-checked",
                            )
                        if refused is not None and public is None:
                            raise refused
                    if public is None:
                        public = status_settler(str(account_id), str(login_status))
                elif login_status in {"signed_out", "expired"}:
                    # A host observation of an account that holds no session,
                    # and every explicit ``expired``, are recorded as they are;
                    # the session Core already holds is reconciled against it in
                    # ``settle_login_status``.
                    public = status_settler(str(account_id), str(login_status))
                else:
                    raise CoreError("relay_restore_failed", "Relay login status is invalid")
                self._advance_revision()
                self._record_relay_transient_update(
                    was_dirty=relay_was_dirty,
                    prior_baseline=relay_prior_baseline,
                    prior_base_revision=relay_prior_base_revision,
                )
                self._last_actions["relay_accounts"] = {
                    "session_restored": public.get("login_status") == "signed_in",
                    "account_id": str(account_id),
                    "login_status": str(public.get("login_status", login_status)),
                }
                self._persist_metadata()
            except CoreError:
                raise
            except Exception as exc:
                raise CoreError(
                    "relay_restore_failed",
                    safe_exception_message(exc, known_secrets=known_secrets),
                ) from None
            should_emit = True
            response = {
                "revision": self._revision,
                "login_status": str(public.get("login_status", login_status)),
                "username": str(public.get("username", "")),
            }
        if should_emit:
            self._request_emit()
        return response

    @staticmethod
    def _providers_summary(states: Mapping[str, object]) -> dict[str, Any]:
        value = states.get("providers_models", {})
        if not isinstance(value, Mapping):
            return {"providers": [], "revision": 0}
        raw = value.get("providers", [])
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
            raw = []
        providers: list[dict[str, Any]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                continue
            provider_id = item.get("editor_id", item.get("id", item.get("name", f"provider-{index + 1}")))
            display_name = item.get("display_name", item.get("name", provider_id))
            raw_models = item.get("models", [])
            models: list[dict[str, Any]] = []
            if isinstance(raw_models, Sequence) and not isinstance(raw_models, (str, bytes, bytearray)):
                for model_index, model in enumerate(raw_models):
                    if not isinstance(model, Mapping):
                        continue
                    model_id = model.get(
                        "editor_id",
                        model.get("id", model.get("deployment_id", model.get("model_name", f"model-{model_index + 1}"))),
                    )
                    public_model = model.get(
                        "public_model",
                        model.get("model_name", model.get("name", model.get("display_name", model_id))),
                    )
                    model_display_name = model.get("display_name", model.get("name", public_model))
                    upstream_model = model.get("upstream_model", model.get("litellm_model", ""))
                    if isinstance(upstream_model, str) and "/" in upstream_model:
                        upstream_model = upstream_model.split("/", 1)[1]
                    order_value = model.get("order", 0)
                    try:
                        order = int(order_value) if not isinstance(order_value, bool) else 0
                    except (TypeError, ValueError, OverflowError):
                        order = 0
                    summary = {
                        "id": str(model_id),
                        "display_name": str(model_display_name),
                        "public_model": str(public_model),
                        "upstream_model": str(upstream_model),
                        "enabled": model.get("model_enabled", model.get("enabled", True)) is not False,
                        "order": order,
                    }
                    models.append(summary)
            model_count = item.get("model_count", len(models))
            try:
                model_count = max(0, int(model_count))
            except (TypeError, ValueError):
                model_count = 0
            provider_summary = {
                "id": str(provider_id),
                "display_name": str(display_name),
                "enabled": item.get("enabled") is not False,
                "model_count": model_count,
                "api_key": {
                    "present": bool(item.get("api_key_configured"))
                    or _secret_presence(item.get("api_key", item.get("api_keys")))
                },
                "endpoint": str(item.get("endpoint", item.get("api_base", "")) or ""),
                "models": models,
            }
            # The account metadata travels with the summary.  It is what tells a
            # login-backed provider apart from an API-key one, and the pane
            # routes each row to its own action on it: a row whose ``auth_kind``
            # is missing reads as an API-key provider, so the ordinary
            # ``provider.delete`` is dispatched for a lane Core refuses there
            # ("Login providers are managed in Service Provider Management") and
            # the entry can never be removed from this list.  A summary is a
            # projection of the detail record, never a narrower record: every
            # field a client acts on has to survive it.
            if item.get("auth_kind") is not None:
                provider_summary["auth_kind"] = item.get("auth_kind")
            if item.get("auth_status") is not None:
                provider_summary["auth_status"] = item.get("auth_status")
            if item.get("auth_active") is not None:
                provider_summary["auth_active"] = item.get("auth_active")
            providers.append(provider_summary)
        revision = value.get("revision", 0)
        return {"providers": providers, "revision": revision if type(revision) is int else 0}

    @staticmethod
    def _webdav_summary(states: Mapping[str, object]) -> dict[str, Any]:
        value = states.get("webdav", {})
        if not isinstance(value, Mapping):
            value = {}
        return {
            "enabled": bool(value.get("enabled", value.get("configured", False))),
            "configured": bool(value.get("configured", value.get("url"))),
            "last_probe": value.get("last_probe", "unknown") if value.get("last_probe", "unknown") in {"unknown", "ok", "failed"} else "unknown",
            "password": {"present": _secret_presence(value.get("password"))},
        }

    def _logs_summary(self, states: Mapping[str, object]) -> dict[str, dict[str, Any]]:
        """Expose only lightweight log state in global snapshots."""

        result = copy.deepcopy(self._logs)
        value = states.get("logs", {})
        if not isinstance(value, Mapping):
            return result
        tabs = value.get("tabs", value)
        if not isinstance(tabs, Mapping):
            return result
        for tab in LOG_TABS:
            raw = tabs.get(tab)
            if not isinstance(raw, Mapping):
                continue
            limit = raw.get("limit", SUBSCRIPTION_QUEUE_LIMIT)
            if type(limit) is not int or limit < 1:
                limit = SUBSCRIPTION_QUEUE_LIMIT
            result[tab] = {
                "tab": tab,
                "available": bool(raw.get("available")),
                "paused": bool(raw.get("paused")),
                "line_count": max(0, raw.get("line_count", 0))
                if type(raw.get("line_count", 0)) is int
                else 0,
                "filter": str(raw.get("filter", ""))[:256],
                "limit": limit,
            }
        return result

    @staticmethod
    def _language_summary(states: Mapping[str, object]) -> str:
        value = states.get("language", {})
        if isinstance(value, Mapping):
            choice = value.get("choice", value.get("language", "system"))
            if choice in {"system", "en", "zh-Hans"}:
                return str(choice)
        return "system"

    def _check_revision(self, expected_revision: object | None) -> None:
        if expected_revision is None:
            return
        if type(expected_revision) is not int or expected_revision < 0:
            raise CoreError("invalid_revision", "Revision is invalid")
        if expected_revision != self._revision:
            raise RevisionConflict(expected_revision, self._revision)

    def _check_revision_before_refresh(self, expected_revision: object | None, revision_before_refresh: int, revision_after_refresh: int | None = None) -> None:
        """Accept a revision that only this call's own refresh invalidated.

        ``apply`` refreshes external files before it takes the caller's
        revision, because a clean draft may have to reload the file it applies
        over.  That reload advances the shared revision, and a caller cannot
        have seen it: it read a snapshot, made an edit, and asked to commit it.
        Comparing the caller's number against the *post*-refresh revision
        rejected exactly that write -- the pane then reported the commit as not
        applied while the edit was staged correctly, which reads as the app
        refusing a change for no visible reason.

        The caller's number is therefore compared against the revision this
        call started from.  A caller that was already behind when it arrived
        still conflicts, because the refresh changed nothing about its staleness.
        """

        if expected_revision is None:
            return
        if type(expected_revision) is not int or expected_revision < 0:
            raise CoreError("invalid_revision", "Revision is invalid")
        current = self._revision if revision_after_refresh is None else revision_after_refresh
        if expected_revision == revision_before_refresh:
            return
        if expected_revision != current:
            raise RevisionConflict(expected_revision, current)

    def _mark_domain(self, name: str, *, dirty: bool | None = None, validation: Mapping[str, Any] | None = None, base_revision: int | None = None) -> None:
        record = self._drafts.setdefault(name, {"dirty": False, "base_revision": self._revision, "validation": {"valid": True, "issues": []}})
        if dirty is not None:
            record["dirty"] = bool(dirty)
        if validation is not None:
            record["validation"] = _validation_summary(validation)
        if base_revision is not None:
            record["base_revision"] = base_revision

    def _dispatch_worker_status(
        self,
        action_type: str,
        normalized_action: str,
        payload: object,
        expected_revision: object | None,
    ) -> dict[str, Any]:
        """Read a local worker's account state without holding the store lock.

        Starting WorkBuddy or Our Free Model and asking it for the desktop
        credential is a local round trip with a twenty- or thirty-second
        ceiling.  The pane that asks is explicitly allowed to wait -- its other
        controls stay usable -- but Core's store lock is shared by every window:
        holding it across that wait froze snapshots and queued a debounced
        Apply behind a worker that was still booting.  The read mutates only the
        adapter's own observation caches and ``_last_operation`` (assigned as a
        whole), so it runs outside the lock; the bookkeeping that publishes it
        is taken under the lock afterwards.
        """

        name = "providers_models"
        with self._lock:
            self._check_revision(expected_revision)
            adapter = self._domains.get(name)
            if adapter is None:
                raise DomainNotFound(name)
            before = self._adapter_draft_state(name)
        try:
            result = adapter.dispatch(action_type, payload)
        except CoreError:
            raise
        except Exception as exc:
            domain_code = getattr(exc, "code", "")
            if isinstance(domain_code, str) and domain_code.strip():
                raise CoreError(domain_code.strip(), safe_exception_message(exc)) from None
            raise CoreError("domain_error", safe_exception_message(exc)) from None
        with self._lock:
            after = self._adapter_draft_state(name)
            action_summary: dict[str, Any] | None = None
            if isinstance(result, Mapping):
                safe_result = _safe_public(result)
                if isinstance(safe_result, Mapping):
                    self._last_actions[name] = _action_summary_entry(name, safe_result)
                    summary = safe_result.get("operation_summary")
                    if (
                        isinstance(summary, Mapping)
                        and summary.get("operation") == normalized_action
                    ):
                        action_summary = dict(summary)
            # A live read stages nothing; if one somehow moved the draft, mark
            # it like every other dispatch rather than hiding the change.
            changed = after != before
            if changed:
                self._advance_revision()
                self._mark_domain(
                    name,
                    dirty=after != self._baselines.get(name),
                    base_revision=self._revision,
                )
                self._persist_metadata()
            response: dict[str, Any] = {"revision": self._revision}
            if action_summary is not None:
                response["action_summary"] = action_summary
        if changed:
            # Publishing a snapshot is not a state transition; the worker's own
            # answer must not wait behind it.
            self._request_emit()
        return response

    def _dispatch_codex_model_catalog_toggle(self, enabled: bool, expected_revision: object | None) -> dict[str, Any]:
        name = "codex"
        # A due public-context refresh is a remote round trip.  Do it before
        # the store lock, the same way the background client-model lane does,
        # so the toggle cannot freeze every other window while it waits.
        codex = self._domains.get(name)
        refresh_contexts = getattr(codex, "refresh_model_contexts_if_due", None)
        if callable(refresh_contexts):
            try:
                refresh_contexts()
            except Exception:
                pass
        should_emit = False
        with self._domain_scope("codex"):
            self._refresh_external_disk_state()
            self._check_revision(expected_revision)
            adapter = self._domains.get(name)
            if adapter is None:
                raise DomainNotFound(name)
            toggle = getattr(adapter, "set_model_catalog_enabled_immediately", None)
            baseline = getattr(adapter, "catalog_baseline_state", None)
            if not callable(toggle) or not callable(baseline):
                raise CoreError("domain_error", "Codex model catalog is unavailable")
            adapter_checkpoint = _checkpoint_adapter(adapter, error_code="apply_failed")
            persistence_paths = list(_adapter_persistence_paths(adapter))
            if self._metadata_store is not None:
                persistence_paths.append(self._metadata_store.path)
            file_checkpoints = _checkpoint_files(persistence_paths)
            core_checkpoint = {
                "revision": self._revision,
                "drafts": copy.deepcopy(self._drafts),
                "last_actions": copy.deepcopy(self._last_actions),
                "baselines": copy.deepcopy(self._baselines),
                "disk": copy.deepcopy(self._disk),
                "disk_identities": copy.deepcopy(self._disk_identities),
            }
            try:
                result = toggle(enabled)
                self._baselines[name] = baseline()
                dirty = self._adapter_draft_state(name) != self._baselines[name]
                self._disk[name] = {
                    "changed": False,
                    "generation": int(self._disk.get(name, {}).get("generation", 0)),
                    "keep_draft": False,
                }
                self._disk_identities[name] = self._external_disk_identity(adapter)
                self._advance_revision()
                self._mark_domain(
                    name,
                    dirty=dirty,
                    validation={"valid": True, "issues": []},
                    base_revision=self._revision,
                )
                self._persist_metadata()
            except Exception as exc:
                try:
                    _restore_files(file_checkpoints)
                    _restore_adapter(adapter, adapter_checkpoint)
                except Exception:
                    raise CoreError("apply_failed", "Settings could not be rolled back") from None
                self._restore_revision(core_checkpoint["revision"])
                self._drafts = core_checkpoint["drafts"]
                self._last_actions = core_checkpoint["last_actions"]
                self._baselines = core_checkpoint["baselines"]
                self._disk = core_checkpoint["disk"]
                self._disk_identities = core_checkpoint["disk_identities"]
                if isinstance(exc, CoreError):
                    raise
                raise CoreError("apply_failed", safe_exception_message(exc)) from None
            should_emit = True
            # The dispatch envelope is a revision plus an optional action-scoped
            # summary.  Returning the catalog projection as the whole result
            # adds a top-level field the IPC contract rejects, which fails the
            # call before the switch is reported as applied.
            summary = _safe_public(result)
            response = {
                "revision": self._revision,
                "action_summary": dict(summary) if isinstance(summary, Mapping) else {},
            }
        if should_emit:
            self._request_emit()
        return response

    # -- WebDAV interval sync ---------------------------------------------
    def quiet_seconds(self) -> float:
        """Seconds since a user action last reached this store."""

        return max(0.0, time.monotonic() - self._last_activity)

    def webdav_auto_sync_state(self) -> dict[str, Any]:
        """What the interval loop reads: the saved switch, interval, and stamp.

        Only committed state counts here.  A half-typed URL in an open window
        must not start background traffic, and the loop re-reads on every pass,
        so nothing has to be pushed at it when the settings change.
        """

        from ..webdav import core as webdav_core

        idle = {"enabled": False, "configured": False, "sync_interval_minutes": 0, "last_sync_at": None}
        webdav = self._domains.get("webdav")
        settings_path = getattr(webdav, "settings_path", None)
        enabled_path = getattr(webdav, "enabled_path", None)
        state_path = getattr(webdav, "state_path", None)
        if not all(isinstance(value, (str, Path)) for value in (settings_path, enabled_path, state_path)):
            return idle
        try:
            settings = webdav_core.load_settings(Path(settings_path).expanduser())
            enabled = Path(enabled_path).expanduser().is_file()
            state = webdav_core.load_sync_state(Path(state_path).expanduser())
        except Exception:
            return idle
        last_sync_at = state.get("updated_at")
        return {
            "enabled": enabled,
            "configured": settings.configured,
            "sync_interval_minutes": settings.sync_interval_minutes,
            "last_sync_at": last_sync_at if isinstance(last_sync_at, str) else None,
        }

    def run_automatic_webdav_sync(self) -> str:
        """Run one smart sync for the interval loop, and say how it ended.

        This goes straight to the sync rather than through ``dispatch``: only
        the Core decides to run it, so no IPC action can ask for the unattended
        behaviour.  There is one sync, not a direction to read first: every run
        compares both sides and takes the newer one.

        The answer is the loop's own input.  A conflict is a question for the
        reader -- the pane shows both sides and stops -- so an unattended retry
        can only ask it again; measured on lv, a two-minute interval retried a
        conflict 300+ times overnight, and every retry wrote the same files and
        rolled the proxy reload over with them.  Any other failure is worth
        another try, on a backing-off interval.
        """

        try:
            self._dispatch_webdav_sync(
                "sync",
                {"sections": ["providers_models", "relay_accounts"]},
                None,
                automatic=True,
            )
        except CoreError as exc:
            return str(exc.code or "webdav_sync_failed")
        return "ok"

    def webdav_scheduler(self) -> Any:
        """The interval loop, created once and owned by this store."""

        if self._webdav_scheduler is None:
            from .webdav_scheduler import WebDAVSyncScheduler

            self._webdav_scheduler = WebDAVSyncScheduler(
                snapshot=self.webdav_auto_sync_state,
                sync=self.run_automatic_webdav_sync,
                settle_seconds=self.quiet_seconds,
            )
        return self._webdav_scheduler

    def _kick_webdav_scheduler(self) -> None:
        scheduler = self._webdav_scheduler
        if scheduler is not None:
            scheduler.kick()

    def _stop_webdav_scheduler(self) -> None:
        scheduler = self._webdav_scheduler
        if scheduler is not None:
            scheduler.stop()

    # -- proxy supervision -------------------------------------------------
    def client_model_sync(self) -> Any:
        """The loop that keeps the generated client model lists current.

        Created on request and started by the Core entry point, like the loops
        above.  A window's snapshot already re-derives these lists, but that
        only runs while somebody is looking, and the routes can change with no
        reload owed.
        """

        if self._client_model_sync is None:
            from . import client_model_sync as _client_model_sync

            self._client_model_sync = _client_model_sync.attach(
                self, self.client_model_documents()
            )
        return self._client_model_sync

    def client_model_documents(self) -> tuple[Any, ...]:
        """The documents that decide which model names the router serves.

        The provider document is the source of every public name, so its own
        timestamp is the one signal that says "the roster may have moved".  A
        runtime document is included when a caller configured one apart from it,
        because the router reads both.
        """

        paths: list[Any] = []
        for name in ("providers_models", "runtime"):
            adapter = self._domains.get(name)
            for attribute in ("config_path", "runtime_config_path", "settings_path"):
                value = getattr(adapter, attribute, None)
                if isinstance(value, (str, Path)) and Path(value).expanduser() not in paths:
                    paths.append(Path(value).expanduser())
        return tuple(paths)

    def adopt_external_route_change(self) -> None:
        """Make a document changed by somebody else reach the running proxy.

        A provider Apply reloads the proxy itself, so its own edits need
        nothing here.  A document rewritten by another process -- a restored
        backup, a model toggled by a tool that does not know this app -- leaves
        the running proxy serving the previous roster, and no refresh can see a
        difference it is never told about.  Reloading is what makes the router's
        own ``/v1/models`` answer the new document, and the client model lists
        follow on the reload that reload already triggers.
        """

        self._schedule_service_reload_after_apply()

    def refresh_client_model_lists(
        self,
        *,
        include_claude: bool = True,
        refresh_contexts: bool = True,
    ) -> None:
        """Regenerate the client model documents this app still generates.

        A provider Apply calls this with the default so the Claude picker
        follows the routes it just changed.  The background watcher passes the
        same default: the picker it generates is the one Claude Code *and* the
        desktop Code tab read, and a row of it can be out of date without the
        roster moving at all -- a window the context registry re-measured, a
        shape an older release wrote.  No reload announces that, so the pass
        that compares the file with what would be written has to be the
        watcher's, not a reload's.  Claude Desktop's own model list is still not
        written here; it discovers ``/v1/models``.  Idempotent: each generator
        compares the document on disk with what the router serves and writes
        only a difference.
        """

        codex_domain = self._domains.get("codex")
        if refresh_contexts:
            refresh_contexts_now = getattr(codex_domain, "refresh_model_contexts_if_due", None)
            if callable(refresh_contexts_now):
                try:
                    refresh_contexts_now()
                except Exception:
                    pass
        live_domains = ("claude", "codex") if include_claude else ("codex",)
        revisions_before = {
            name: getattr(self._domains.get(name), "revision", None)
            for name in live_domains
        }
        # The generators read and rewrite client files, and one of them asks the
        # running proxy what it serves -- which is precisely the question that
        # cannot be answered while the proxy is being replaced.  The domains keep
        # their own locks, so this work runs outside the store's: a window
        # polling its snapshot must never wait for a client document.
        if include_claude:
            claude = self._domains.get("claude")
            refresh = getattr(claude, "refresh_model_lists", None)
            if callable(refresh):
                try:
                    refresh()
                except Exception:
                    pass
        codex = self._domains.get("codex")
        refresh_catalog = getattr(codex, "refresh_model_catalog", None)
        if callable(refresh_catalog):
            try:
                refresh_catalog()
            except Exception:
                pass
        # The live read belongs to this lane, not to ``snapshot``.  A window
        # polls the projection many times a second while the proxy reloads, and
        # a read taken there both blocks every other request on the store lock
        # and pays the endpoint timeout once per poll.  When the observation
        # lands, publish it like any other mutation so the memoized projection
        # is rebuilt on the next read.
        revisions_after = {
            name: getattr(self._domains.get(name), "revision", None)
            for name in live_domains
        }
        if revisions_after != revisions_before:
            with self._lock:
                self._advance_revision()
                self._snapshot_cache = None
                try:
                    self._persist_metadata()
                except PersistenceError:
                    pass
            self._request_emit()

    def service_supervisor(self, *, initial_delay: float | None = None) -> Any:
        """The loop that restores the proxy without a person's click.

        Created on request and started by the Core entry point, exactly like
        the loops above.  A pass does nothing unless this installation still
        intends to run a proxy, so an explicit 停止服务 is never undone.

        ``initial_delay`` is the caller's statement about this process rather
        than a tuning knob: a window that is about to launch the proxy itself
        keeps the loop's own head start (``None`` uses the loop's default), and
        a host unit that has no window asks for the first pass immediately,
        because no other starter exists on that machine.
        """

        if self._service_supervisor is None:
            from .service_supervisor import ServiceSupervisor

            head_start = {} if initial_delay is None else {"initial_delay": float(initial_delay)}
            self._service_supervisor = ServiceSupervisor(
                supervise=self.supervise_service_once, **head_start
            )
        return self._service_supervisor

    def _stop_service_supervisor(self) -> None:
        supervisor = self._service_supervisor
        if supervisor is not None:
            try:
                supervisor.stop()
            except Exception:
                pass

    # -- image-context cleanup --------------------------------------------
    def image_context_sweeper(self) -> Any:
        """The loop that enforces the replay cache's own day.

        The store already sweeps after every write, but a write is the thing
        that stops arriving once the pass is switched off or a task is left
        behind.  Core owns the clock, so an installation nobody is looking at
        still drops yesterday's pictures.
        """

        if self._image_context_sweeper is None:
            from .image_context_cleanup import ImageContextSweeper
            from ..proxy import image_context_store

            self._image_context_sweeper = ImageContextSweeper(sweep=image_context_store.sweep)
        return self._image_context_sweeper

    def _stop_image_context_sweeper(self) -> None:
        sweeper = self._image_context_sweeper
        if sweeper is not None:
            sweeper.stop()

    @staticmethod
    def _webdav_sync_sections(payload: object) -> tuple[str, str]:
        data = _as_mapping(payload)
        if set(data).difference({"sections"}):
            raise CoreError("invalid_sections", "WebDAV sync options are invalid")
        sections = data.get("sections")
        if sections is None:
            return ("providers_models", "relay_accounts")
        if isinstance(sections, (str, bytes, bytearray)) or not isinstance(sections, Sequence):
            raise CoreError("invalid_sections", "WebDAV sync sections are invalid")
        try:
            selected = tuple(dict.fromkeys(_canonical_domain(value) for value in sections))
        except CoreError:
            raise CoreError("invalid_sections", "WebDAV sync sections are invalid") from None
        required = {"providers_models", "relay_accounts"}
        if set(selected) != required:
            raise CoreError(
                "invalid_sections",
                "WebDAV sync requires Providers & Models and Relay Accounts together",
            )
        return ("providers_models", "relay_accounts")

    def _dispatch_webdav_sync(
        self,
        operation: str,
        payload: object,
        expected_revision: object | None,
        *,
        automatic: bool = False,
    ) -> dict[str, Any]:
        """Run one legacy-bundle WebDAV operation transactionally.

        ``automatic`` marks the interval loop's own run.  A window's unsaved
        draft stops a *reader's* sync, because that sync would race the edit
        they are making; an unattended one must not be blocked by it, or the
        interval would quietly do nothing whenever any pane held a draft.  It
        syncs the files on disk and lets the pane's disk-changed prompt
        reconcile whatever the reader had open.
        """

        sections = self._webdav_sync_sections(payload)
        from ..webdav import core as webdav_core
        from ..webdav import operations as webdav_operations

        should_emit = False
        with self._domain_scope("webdav", "providers_models", "relay_accounts"):
            self._refresh_external_disk_state()
            self._check_revision(expected_revision)
            webdav = self._domains.get("webdav")
            providers = self._domains.get("providers_models")
            relay = self._domains.get("relay_accounts")
            runtime_adapter = self._domains.get("runtime")
            if webdav is None or providers is None or relay is None:
                raise CoreError("webdav_sync_failed", "WebDAV sync sources are unavailable")
            if not automatic:
                if any(self._drafts.get(name, {}).get("dirty") for name in ("webdav", *sections)):
                    raise CoreError("webdav_sync_unsaved", "Save or discard pending changes before WebDAV sync")
                if any(self._disk.get(name, {}).get("changed") for name in ("webdav", *sections)):
                    raise CoreError("webdav_sync_unsaved", "Reload changed local files before WebDAV sync")

            settings_getter = getattr(webdav, "sync_settings", None)
            if not callable(settings_getter):
                raise CoreError("webdav_sync_failed", "WebDAV sync is unavailable")
            try:
                settings = settings_getter()
            except Exception:
                raise CoreError("webdav_sync_failed", "WebDAV is not configured") from None
            config_value = getattr(providers, "config_path", None)
            relay_value = getattr(relay, "storage_path", None)
            state_value = getattr(webdav, "state_path", None)
            status_value = getattr(webdav, "status_path", None)
            conflict_value = getattr(webdav, "conflict_path", None)
            if not all(isinstance(value, (str, Path)) for value in (config_value, relay_value, state_value, status_value, conflict_value)):
                raise CoreError("webdav_sync_failed", "WebDAV sync sources are unavailable")
            config_path = Path(config_value).expanduser()
            relay_path = Path(relay_value).expanduser()
            state_path = Path(state_value).expanduser()
            status_path = Path(status_value).expanduser()
            conflict_path = Path(conflict_value).expanduser()
            if not _same_path(relay_path, webdav_core.relay_accounts_path(config_path)):
                raise CoreError("webdav_sync_failed", "WebDAV sync sources are unavailable")

            adapter_checkpoints = {
                name: _checkpoint_adapter(adapter, error_code="webdav_sync_failed")
                for name, adapter in (("providers_models", providers), ("relay_accounts", relay))
            }
            core_checkpoint = {
                "revision": self._revision,
                "drafts": copy.deepcopy(self._drafts),
                "last_actions": copy.deepcopy(self._last_actions),
                "baselines": copy.deepcopy(self._baselines),
                "disk": copy.deepcopy(self._disk),
                "disk_identities": copy.deepcopy(self._disk_identities),
                "service": copy.deepcopy(self._service),
            }
            persistence_paths = [
                config_path,
                webdav_core.disabled_models_path(config_path),
                relay_path,
                state_path,
            ]
            if self._metadata_store is not None:
                persistence_paths.append(self._metadata_store.path)
            try:
                file_checkpoints = _checkpoint_files(persistence_paths)
            except CoreError:
                raise CoreError("webdav_sync_failed", "WebDAV sync could not prepare local files") from None

            pulled = False
            outcome = operation
            action_summary: dict[str, Any] = {}
            archived_remote: str | None = None
            adopted_remote: dict[str, Any] | None = None
            # Read before anything is installed, because the bundle replaces
            # both: the bytes the running proxy was handed, and the key its
            # clients present.  A pull that installs a document without this
            # is a copy on disk that nothing serves -- and on a machine with no
            # window open (a gateway, a background app) nothing would ever
            # notice, so the routes would stay as they were until the next
            # unrelated restart.
            runtime_document_before = self._runtime_document_fingerprint()
            master_key_before = _document_master_key(config_path)
            try:
                client = webdav_core.WebDAVClient(settings)
                # A setting that still carried an earlier default name is read
                # as the current one; move the file it pointed at so the
                # collection keeps one live bundle under this app's name.
                if settings.remote_name not in webdav_core.LEGACY_REMOTE_NAMES:
                    try:
                        adopted_remote = webdav_operations.adopt_renamed_remote_bundle(client, settings, webdav_core.LEGACY_REMOTE_NAMES)
                    except Exception:
                        adopted_remote = None
                if operation == "push":
                    webdav_operations.push_bundle(client, settings, config_path, state_path, "push")
                elif operation == "pull":
                    webdav_operations.pull_bundle(client, settings, config_path, state_path, "pull")
                    pulled = True
                else:
                    local = webdav_core.local_snapshot(config_path)
                    try:
                        remote_manifest = webdav_operations.read_remote_manifest(client, settings)
                    except webdav_core.SyncError as exc:
                        if exc.code != webdav_core.INCOMPATIBLE_BUNDLE_CODE:
                            raise
                        # Smart sync decides by comparing, and a bundle from
                        # another app identity or archive version cannot be
                        # compared.  Rather than stopping to ask for a choice
                        # nothing can inform, the sync keeps that file under a
                        # dated sibling name and pushes this Mac's state over
                        # it: the file the guard refuses to read is never lost,
                        # and the user asked for a sync, not for a decision.
                        archived_remote = webdav_operations.archive_remote_bundle(client, settings)
                        remote_manifest = None
                    remote = webdav_operations.remote_snapshot(client, settings, remote_manifest)
                    base = webdav_core.baseline_snapshot(state_path)
                    # One comparison decides every sync, including the
                    # unattended one: a side that moved alone is the side to
                    # take.  Two sides that both moved are a real conflict --
                    # the pane shows both texts and the reader keeps one -- and
                    # no branch asks for a saved direction, because there is no
                    # such setting any more.  What counts as a move is the
                    # compared digest: a relay balance another machine read is
                    # nobody's edit, so it cannot manufacture a conflict.  A
                    # remote with no record is asked once through its bundle,
                    # which is the download the conflict branch would make.
                    plan, remote = webdav_operations.decide_sync(client, settings, local, remote, base)
                    if plan == "unchanged":
                        # Byte equality proved the remote holds these same
                        # digests, so they can be stated for it.
                        agreed = webdav_core.SyncSnapshot(remote.manifest, remote.digests or local.digests)
                        webdav_core.save_sync_state(state_path, settings, agreed, "sync")
                        if remote.digests is None:
                            webdav_operations.publish_remote_digests(client, settings, agreed)
                        outcome = "unchanged"
                    elif plan == "push":
                        webdav_operations.push_bundle(client, settings, config_path, state_path, "sync-push")
                        outcome = "push"
                    elif plan == "conflict":
                        # Both ends moved, so neither is a version of the other:
                        # the run writes nothing, keeps the remote side beside
                        # the sync state, and stops.  The pane compares the two
                        # and sends back the side the reader picks.
                        try:
                            webdav_operations.remember_remote_bundle(client, settings, conflict_path)
                        except Exception:
                            # Failing to keep the comparison is not the cause the
                            # reader has to act on: the conflict is, and the
                            # pane can still state it with the choice and no
                            # diff.
                            webdav_core.clear_conflict_record(conflict_path)
                        raise webdav_core.SyncError(
                            "WebDAV sync stopped because both ends changed",
                            "webdav_sync_conflict",
                        )
                    else:
                        # The comparison above already read both manifests; a
                        # second one would only restate what this branch
                        # knows, so accept the remote bundle directly.
                        webdav_operations.pull_bundle(
                            client, settings, config_path, state_path, "sync-pull",
                            skip_when_unchanged=False,
                        )
                        pulled = True
                        outcome = "pull"

                # A host serves a projection of the document (see
                # ``host_profile``), and the key in it is that host's own.  The
                # document may well have arrived stating another machine's key;
                # the proxy never sees it, so nothing here owes a replacement.
                from . import host_profile as host_profile_module

                host_profile_owns_key = host_profile_module.active() and bool(
                    host_profile_module.stated_key()
                )
                master_key_moved = (
                    pulled and not host_profile_owns_key
                    and _document_master_key(config_path) != master_key_before
                )
                if pulled:
                    reloaded: list[tuple[str, Any]] = [
                        ("providers_models", providers), ("relay_accounts", relay)
                    ]
                    if master_key_moved and runtime_adapter is not None:
                        # The bundle carries ``general_settings.master_key``, and
                        # the runtime pane is where that key is stated.  A key
                        # that arrived from another machine is this machine's key
                        # now, so the pane's baseline has to follow it instead of
                        # reporting a file this sync itself rewrote.  A pane with
                        # uncommitted edits keeps them: the poll states the file
                        # changed, and the reader decides.
                        if not self._drafts.get("runtime", {}).get("dirty"):
                            reloaded.append(("runtime", runtime_adapter))
                    for name, adapter in reloaded:
                        adapter.reload()
                        self._baselines[name] = self._adapter_draft_state(name)
                        self._disk[name] = {
                            "changed": False,
                            "generation": int(self._disk.get(name, {}).get("generation", 0)),
                            "keep_draft": False,
                        }
                        self._disk_identities[name] = self._external_disk_identity(adapter)
                        self._mark_domain(
                            name,
                            dirty=False,
                            validation={"valid": True, "issues": []},
                            base_revision=self._revision + 1,
                        )
                webdav_core.save_sync_status(status_path, operation, True)
                # The two ends agree after a push, a pull, or a run that had
                # nothing to do, so the kept-aside remote side has served its
                # purpose.  Leaving it would offer a comparison of a conflict
                # that no longer exists.
                webdav_core.clear_conflict_record(conflict_path)
                self._last_actions["webdav"] = {
                    "action": operation,
                    "ok": True,
                    "outcome": outcome,
                    "sections": list(sections),
                }
                if adopted_remote is not None:
                    # A setting renamed from an earlier default moved its file;
                    # the pane states it when the server would not let the old
                    # one go.
                    action_summary["adopted_remote"] = adopted_remote
                if archived_remote is not None:
                    # What the pane states after the sync: this Mac's state
                    # replaced a remote file that is still on the server under
                    # the name below.
                    action_summary = {
                        "outcome": outcome,
                        "archived_remote": archived_remote,
                        "sections": list(sections),
                    }
                self._advance_revision()
                self._persist_metadata()
                status_handler = self._service_handlers.get("status")
                if status_handler is not None:
                    try:
                        service_result = status_handler("health")
                    except Exception:
                        service_result = None
                    if isinstance(service_result, Mapping):
                        self._set_service_from_result(service_result, increment=False)
            except Exception as exc:
                rollback_failed = False
                try:
                    _restore_files(file_checkpoints)
                    for name, adapter in (("providers_models", providers), ("relay_accounts", relay)):
                        _restore_adapter(adapter, adapter_checkpoints[name])
                except Exception:
                    rollback_failed = True
                self._restore_revision(core_checkpoint["revision"])
                self._drafts = core_checkpoint["drafts"]
                self._last_actions = core_checkpoint["last_actions"]
                self._baselines = core_checkpoint["baselines"]
                self._disk = core_checkpoint["disk"]
                self._disk_identities = core_checkpoint["disk_identities"]
                self._service = core_checkpoint["service"]
                # A run that stopped leaves the documents untouched, so the
                # revision it rolled back to is correct -- but it did write the
                # status file and, when both ends changed, the comparison this
                # window has to offer.  Neither is part of that revision, and
                # the projection is memoized on it, so the pane would keep
                # reading the state from before the run: the outcome and the
                # row that opens the comparison would appear only after some
                # unrelated action moved the revision.  Drop the memo; the next
                # read rebuilds it at the same revision.
                self._snapshot_cache = None
                # A sync error names its own cause (an incompatible remote
                # bundle, a conflict); anything else is a plain failure.  The
                # cause keeps its name even when the rollback itself failed:
                # the pane has to state why the sync stopped, and "the rollback
                # failed too" is not something the reader can act on.
                #
                # Only a name this wire can carry may be used.  A
                # ``WebDAVHTTPError`` subclasses ``SyncError`` but keeps the
                # remote server's *status* in ``code`` (an ``int``), and the
                # sentence it carries can hold a newline.  Handing either to the
                # response envelope made Core refuse its own answer and close
                # the socket, which the host could only read as "the local Core
                # is unavailable" while Core was answering everything else.  An
                # HTTP status is the remote's word, so it stays in the message
                # and the pane states its own sentence for the failure.
                named_cause = isinstance(exc, webdav_core.SyncError) and is_error_code(exc.code)
                cause_code = exc.code if named_cause else "webdav_sync_failed"
                try:
                    webdav_core.save_sync_status(status_path, operation, False, cause_code)
                except Exception:
                    pass
                if rollback_failed:
                    raise CoreError(cause_code, "WebDAV sync could not roll back local files") from None
                if isinstance(exc, CoreError):
                    raise
                if isinstance(exc, webdav_core.SyncError):
                    # The remote's own sentence, redacted and held to the
                    # one-line/512-byte rule the envelope enforces.
                    raise CoreError(cause_code, safe_error_message(exc)) from None
                raise CoreError(cause_code, "WebDAV sync failed") from None
            should_emit = True
            if pulled:
                # After the commit, not inside it: the restart runs on its own
                # thread and a rollback must never leave one scheduled.
                self._schedule_reload_after_webdav_pull(
                    runtime_document_before, master_key_changed=master_key_moved
                )
            result: dict[str, Any] = {"revision": self._revision}
            if action_summary:
                result["action_summary"] = action_summary
        if should_emit:
            self._request_emit()
        return result

    def _dispatch_probe_job(self, name: str, payload: object) -> dict[str, Any]:
        """Start one probe as a job and answer with its id.

        A probe is a network read with the upstream's own budget -- the native
        hosts grant the old request 900 s for exactly that reason.  Run inside
        a dispatch it would hold a request, and before the domain lanes it held
        the one write lock every window's next edit needs.  As a job it answers
        at once; the read itself runs on the job pool, publishes ``running``
        and then ``done``, and the write it does make (the stored finding, and
        the revision that follows it) is what the event's result describes.

        ``action_summary`` keeps its old meaning for callers that read it: a
        probe whose result names an operation still lands in the domain's
        summary slot when the job finishes.
        """

        data = dict(payload) if isinstance(payload, Mapping) else {}

        def work() -> Mapping[str, Any]:
            return self.probe(data, domain=name)

        def on_done(result: Mapping[str, Any]) -> None:
            summary = _action_summary_entry(name, _safe_public(result))
            if not summary:
                return
            with self._lock:
                self._last_actions[name] = summary
            self._request_emit()

        return self.start_job(work, name=name, on_done=on_done)

    def _dispatch_domain_set(self, name: str, normalized_action: str) -> tuple[str, ...]:
        """The write lanes one dispatch action declares before it starts.

        A dispatch is usually one domain's write.  The exceptions are the
        documented cross-domain actions: a relay removal that also releases the
        provider slots linked to it, and a provider action that resolves the
        relay's own key material.  Naming them here -- once, before the
        transaction opens -- is what lets those take both lanes in the fixed
        order instead of discovering the second one halfway through.
        """

        names = {name}
        if name == "relay_accounts" and (
            normalized_action in _RELAY_DEPENDENCY_ACTIONS
            or normalized_action in _RELAY_RESOURCE_IMPORT_ACTIONS
        ):
            names.add("providers_models")
        elif name == "providers_models" and (
            normalized_action in _PROVIDER_RELAY_RESOURCE_ACTIONS
            or normalized_action in _PROVIDER_RELAY_STATION_ACTIONS
        ):
            names.add("relay_accounts")
        return tuple(sorted(names))

    def dispatch(
        self,
        action: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
        _trusted_native_capability: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(action, Mapping):
            raise CoreError("invalid_action", "A Core action is required")
        data = dict(action)
        self._last_activity = time.monotonic()
        if not _trusted_native_capability:
            self.reject_plaintext_secret_action(data)
        action_type = data.get("type", data.get("action"))
        if not isinstance(action_type, str) or not action_type.strip():
            raise CoreError("invalid_action", "A Core action is required")
        domain_value = data.get("domain")
        normalized_action = action_type.replace("-", "_").replace(".", "_").lower()
        if domain_value is not None and normalized_action == "probe":
            # A probe is the one action whose answer is a job: it reads the
            # upstream, it can take minutes, and nothing else in the store has
            # to wait for it.
            return self._dispatch_probe_job(_canonical_domain(domain_value), data.get("payload"))
        if domain_value is not None and _canonical_domain(domain_value) == "webdav":
            # The switch, the interval, and the direction all reshape the
            # interval loop, so let it re-read instead of waiting out its sleep.
            self._kick_webdav_scheduler()
        if (
            domain_value is not None
            and _canonical_domain(domain_value) == "codex"
            and normalized_action == "codex_model_catalog_set"
        ):
            payload = _as_mapping(data.get("payload"))
            enabled = payload.get("enabled")
            if not isinstance(enabled, bool):
                raise CoreError("invalid_action", "The Codex model catalog switch is invalid")
            return self._dispatch_codex_model_catalog_toggle(
                enabled,
                expected_revision if expected_revision is not None else data.get("expected_revision"),
            )
        if domain_value is not None and _canonical_domain(domain_value) == "webdav":
            webdav_operation = {
                "push": "push",
                "webdav_push": "push",
                "pull": "pull",
                "webdav_pull": "pull",
                "sync": "sync",
                "webdav_sync": "sync",
            }.get(normalized_action)
            if webdav_operation is not None:
                return self._dispatch_webdav_sync(
                    webdav_operation,
                    data.get("payload"),
                    expected_revision if expected_revision is not None else data.get("expected_revision"),
                )
        if (
            domain_value is not None
            and _canonical_domain(domain_value) == "providers_models"
            and normalized_action in {
                "workbuddy_status",
                "qoder_status",
                "qoder_models",
                "qoder_login",
                "qoder_logout",
                "qoder_open_app",
                "our_free_model_status",
            }
        ):
            return self._dispatch_worker_status(
                action_type,
                normalized_action,
                data.get("payload"),
                expected_revision if expected_revision is not None else data.get("expected_revision"),
            )
        if domain_value is None and action_type.startswith("service.") and action_type.removeprefix("service.") in SERVICE_PROXY_OPERATIONS:
            # A proxy operation runs the controller's own replacement, so it
            # holds the mutation lane (never the state lock): see
            # ``_dispatch_service``.  Serializing it with panes' edits is what
            # keeps a restart from rewriting the Codex files under a commit;
            # snapshots stay live because they take neither lane.
            should_emit = False
            with self._mutation_scope():
                self._check_revision(expected_revision if expected_revision is not None else data.get("expected_revision"))
                previous_revision = self._revision
                self._dispatch_service(action_type, _as_mapping(data.get("payload")))
                should_emit = self._revision != previous_revision
                response = {"revision": self._revision}
            if should_emit:
                self._request_emit()
            return response
        should_emit = False
        # Which write lanes this action declares before it touches anything.
        # A plain domain action takes only its own, so an edit here cannot
        # queue behind a slow operation in another domain; an action that also
        # rewrites a second domain -- a relay removal that releases linked
        # provider slots, a provider action that resolves relay key material --
        # names both and takes the cross-domain transaction lane as well.
        # ``service.*`` answers a proxy operation, which touches no domain but
        # must stay serialized with every writer, so it keeps the whole lane.
        declared = (
            self._dispatch_domain_set(_canonical_domain(domain_value), normalized_action)
            if domain_value is not None
            else ()
        )
        scope = self._domain_scope(*declared) if declared else self._mutation_scope()
        with scope:
            self._check_revision(expected_revision if expected_revision is not None else data.get("expected_revision"))
            previous_revision = self._revision
            action_summary: dict[str, Any] | None = None
            if domain_value is None and action_type.startswith("service."):
                result = self._dispatch_service(action_type, _as_mapping(data.get("payload")))
            else:
                name = _canonical_domain(domain_value)
                adapter = self._domains.get(name)
                if adapter is None:
                    raise DomainNotFound(name)
                before = copy.deepcopy(self._baselines.get(name))
                # What this dispatch leaves staged.  ``after`` is read once at
                # the end of the transaction, so this single capture answers
                # both questions the response depends on: whether the draft
                # moved away from its baseline (a real staged edit, which must
                # advance the shared revision) and whether the action changed
                # the draft at all.  Capturing it for every action is what lets
                # a live read that mutates nothing -- an account question, a
                # catalog fetch, an authorization poll, a probe -- answer
                # without advancing the revision every other window keys its
                # Apply on.  Advancing it there is not harmless bookkeeping:
                # the pane's own debounced Apply then reads a revision it never
                # caused and reports the user's work back as "settings changed
                # outside this window".
                # A station binding is cosmetic only when the provider draft
                # it edits was already clean.  A provider the wizard just added
                # is still staged: its add lives in this same draft, and clearing
                # the dirty flag here made the binding look like the whole draft
                # was applied.  The wizard then closed, the staged provider was
                # gone, and the station account the native login had already
                # stored had no provider row left to reach it.
                provider_was_dirty = (
                    name == "providers_models"
                    and self._drafts.get("providers_models", {}).get("dirty") is True
                )
                action_draft_before = self._adapter_draft_state(name)
                payload = data.get("payload")
                if name == "providers_models" and action_type.replace("-", "_").replace(".", "_") in {
                    "providers_import_selected",
                    "provider_import_selected",
                    "import_selected",
                } and isinstance(payload, Mapping):
                    file_token = payload.get("file_token", payload.get("fileToken"))
                    if file_token is not None:
                        selected_path = self.file_capabilities.resolve(file_token, "import")
                        payload = {"path": str(selected_path)}
                if name == "relay_accounts" and normalized_action in _RELAY_RESOURCE_IMPORT_ACTIONS:
                    resource_data = _as_mapping(payload)
                    # The revision the caller stated is the one this dispatch
                    # checks, exactly as the ordinary path and the codex/webdav
                    # special cases do: staging an import from a stale window
                    # has to conflict instead of silently rewriting the draft.
                    return _relay_transient_dispatch_result(
                        self.import_relay_resources(
                            resource_data.get("id", resource_data.get("account_id")),
                            resource_data.get("resource_ids"),
                            revision=expected_revision if expected_revision is not None else data.get("expected_revision"),
                            mode=resource_data.get("import_mode", resource_data.get("mode", "linked")),
                        )
                    )
                if name == "relay_accounts" and normalized_action in {
                    "resources_refresh",
                    "relay_resources_refresh",
                    "account_resources_refresh",
                }:
                    resource_data = _as_mapping(payload)
                    return _relay_transient_dispatch_result(
                        self.refresh_relay_resources(
                            resource_data.get("id", resource_data.get("account_id")),
                            revision=expected_revision if expected_revision is not None else data.get("expected_revision"),
                        )
                    )
                if name == "providers_models" and normalized_action == "provider_select_relay_station":
                    station_data = _as_mapping(payload)
                    relay = self._domains.get("relay_accounts")
                    resolver = getattr(relay, "provider_station_source", None)
                    if not callable(resolver):
                        raise CoreError("domain_error", "Relay stations are unavailable")
                    try:
                        source = resolver(station_data)
                    except Exception as exc:
                        raise CoreError("domain_error", safe_exception_message(exc)) from None
                    if not isinstance(source, Mapping):
                        raise CoreError("domain_error", "Relay station is unavailable")
                    payload = {
                        "provider_id": station_data.get("provider_id"),
                        "source": dict(source),
                    }
                relay_dependency_action = (
                    name == "relay_accounts" and normalized_action in _RELAY_DEPENDENCY_ACTIONS
                )
                providers = self._domains.get("providers_models") if relay_dependency_action else None
                provider_before = self._adapter_draft_state("providers_models") if providers is not None else None
                relay_checkpoint = _checkpoint_adapter(adapter, error_code="domain_error") if relay_dependency_action else None
                provider_checkpoint = _checkpoint_adapter(providers, error_code="domain_error") if providers is not None else None
                try:
                    if name == "providers_models" and normalized_action in _PROVIDER_RELAY_RESOURCE_ACTIONS:
                        resource_data = _as_mapping(payload)
                        relay = self._domains.get("relay_accounts")
                        resolver = getattr(relay, "binding_source", None)
                        if not callable(resolver):
                            raise CoreError(
                                "domain_error",
                                "Relay API resources are unavailable",
                            )
                        resolved = resolver(resource_data)
                        if not isinstance(resolved, Mapping):
                            raise CoreError(
                                "domain_error",
                                "Relay API resource is unavailable",
                            )
                        source = dict(resolved)
                        if normalized_action == "provider_fetch_relay_resource_models":
                            materializer = getattr(relay, "binding_materials", None)
                            if not callable(materializer):
                                raise CoreError(
                                    "domain_error",
                                    "Relay API key material is unavailable",
                                )
                            # The durable half of "a key this Core already holds".
                            # A linked slot persists its value in the provider
                            # document, while the relay domain's in-process cache
                            # does not survive a restart -- and an account whose
                            # dashboard session has expired cannot hand its keys
                            # over at all.  Passing the slots is what makes 获取模型
                            # work from the credential already on disk instead of
                            # reporting the key unresolvable, exactly as an Apply
                            # resolves it; the station is still read for any
                            # source the slots do not cover.  The provider domain
                            # is looked up here rather than reusing the
                            # dependency-transaction local below, which is None
                            # for this action.
                            providers_domain = self._domains.get("providers_models")
                            slot_reader = getattr(providers_domain, "relay_slot_credentials", None)
                            materials = materializer(
                                {"resources": [source]},
                                slot_credentials=slot_reader() if callable(slot_reader) else None,
                            )
                            rows = materials.get("resources") if isinstance(materials, Mapping) else None
                            material = next(
                                (
                                    item
                                    for item in rows
                                    if isinstance(item, Mapping)
                                    and all(item.get(key) == source.get(key) for key in ("station_id", "account_id", "resource_id"))
                                ),
                                None,
                            ) if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, bytearray)) else None
                            if not isinstance(material, Mapping) or not isinstance(material.get("api_key"), str):
                                raise CoreError(
                                    "domain_error",
                                    "Relay API key material is unavailable",
                                )
                            source = dict(material)
                        payload = {
                            "provider_id": resource_data.get("provider_id"),
                            **(
                                {"model_id": resource_data.get("model_id")}
                                if normalized_action == "model_select_relay_resource"
                                else {}
                            ),
                            "source": source,
                            "api_key_name": resolved.get("api_key_name"),
                        }
                    result = adapter.dispatch(action_type, payload)
                    if relay_dependency_action and providers is not None:
                        relay_snapshot = adapter.snapshot()
                        details = relay_snapshot.get("last_action") if isinstance(relay_snapshot, Mapping) else None
                        resources = details.get("resources") if isinstance(details, Mapping) else None
                        policy = details.get("dependency_policy") if isinstance(details, Mapping) else None
                        if isinstance(resources, Sequence) and not isinstance(resources, (str, bytes, bytearray)):
                            normalized_policy = _relay_dependency_policy_name(policy)
                            handler = getattr(providers, "apply_relay_dependency_policy", None)
                            if not normalized_policy or not callable(handler):
                                raise CoreError("domain_error", "Linked model dependency handling is unavailable")
                            rebind = _as_mapping(payload).get("rebind") if isinstance(payload, Mapping) else None
                            dependency_result = handler(resources, normalized_policy, rebind=rebind)
                            if isinstance(dependency_result, Mapping):
                                dependency_issues = dependency_result.get("issues")
                                if (
                                    isinstance(dependency_issues, Sequence)
                                    and not isinstance(dependency_issues, (str, bytes, bytearray))
                                    and dependency_issues
                                ):
                                    raise CoreError(
                                        "domain_error",
                                        "Linked model dependency handling could not be completed",
                                    )
                                self._last_actions["providers_models"] = _action_summary_entry(
                                    "providers_models", _safe_public(dependency_result)
                                )
                except ConfirmationNeeded:
                    if relay_checkpoint is not None:
                        _restore_adapter(adapter, relay_checkpoint)
                    if providers is not None and provider_checkpoint is not None:
                        _restore_adapter(providers, provider_checkpoint)
                    raise
                except Exception as exc:
                    if relay_checkpoint is not None:
                        try:
                            _restore_adapter(adapter, relay_checkpoint)
                            if providers is not None and provider_checkpoint is not None:
                                _restore_adapter(providers, provider_checkpoint)
                        except Exception:
                            raise CoreError("domain_error", "Linked model dependency handling could not be rolled back") from None
                    # Domain exceptions are authored to be safe; normalize
                    # them into the stable Core protocol error shape.  A cause
                    # the domain named travels as the IPC code, so the window's
                    # own message table decides what the user reads instead of
                    # every locale showing the domain's English sentence.
                    if isinstance(exc, CoreError):
                        raise
                    domain_code = getattr(exc, "code", "")
                    if isinstance(domain_code, str) and domain_code.strip():
                        raise CoreError(domain_code.strip(), safe_exception_message(exc)) from None
                    raise CoreError("domain_error", safe_exception_message(exc)) from None
                after = self._adapter_draft_state(name)
                if isinstance(result, Mapping):
                    safe_result = _safe_public(result)
                    if isinstance(safe_result, Mapping):
                        self._last_actions[name] = _action_summary_entry(name, safe_result)
                        summary = safe_result.get("operation_summary")
                        # The operation a live read answers with.  Every action
                        # that produces a summary names itself in it, so the
                        # map covers the whole vocabulary rather than a subset:
                        # a name that is missing here silently drops the
                        # action-scoped summary, and a caller that asked for a
                        # catalog then reads the shared slot another window's
                        # read has already replaced.
                        expected_operation = {
                            "fetch_models": "fetch_models",
                            "providers_fetch_models": "fetch_models",
                            "provider_fetch_models": "fetch_models",
                            "provider_fetch_relay_resource_models": "fetch_models",
                            "service_provider_add": "service_provider_add",
                            "service_add_provider": "service_provider_add",
                            "provider_auth_start": "provider_auth_start",
                            "provider_auth_cancel": "provider_auth_cancel",
                            "provider_auth_logout": "provider_auth_logout",
                            "provider_auth_status": "provider_auth_status",
                            "service_provider_auth_start": "service_provider_auth_start",
                            "service_provider_auth_cancel": "service_provider_auth_cancel",
                            "service_provider_auth_logout": "service_provider_auth_logout",
                            "service_provider_auth_status": "service_provider_auth_status",
                            "workbuddy_status": "workbuddy_status",
                            "workbuddy_models": "workbuddy_models",
                            "workbuddy_login": "workbuddy_login",
                            "qoder_status": "qoder_status",
                            "qoder_models": "qoder_models",
                            "qoder_login": "qoder_login",
                            "qoder_logout": "qoder_logout",
                            "our_free_model_status": "our_free_model_status",
                            "our_free_model_models": "our_free_model_models",
                            "model_import_service_catalog": "model_import_service_catalog",
                        }.get(normalized_action)
                        if (
                            name == "providers_models"
                            and expected_operation is not None
                            and isinstance(summary, Mapping)
                            and summary.get("operation") == expected_operation
                        ):
                            # Return the summary produced by this dispatch as
                            # an action-scoped value.  The snapshot keeps the
                            # latest summary for rendering, but another
                            # window's status poll can replace that slot before
                            # the caller refreshes it.
                            action_summary = dict(summary)
                        elif (
                            name == "relay_accounts"
                            and normalized_action in {"detect_type", "account_detect_type", "relay_detect_type"}
                            and isinstance(safe_result, Mapping)
                            and "detected_type" in safe_result
                        ):
                            # A detection is the same kind of answer as a
                            # catalog read: it names no operation, so it only
                            # lands in the shared slot every relay action
                            # overwrites.  Measured on lv: the pane's own account
                            # refresh replaced it between the dispatch and the
                            # `snapshot` behind it, and the wizard reported
                            # 无法识别中转站类别 for a station that had just
                            # answered /api/status.
                            action_summary = {
                                key: value for key, value in safe_result.items() if key != "revision"
                            }
                cosmetic_binding = bool(
                    name == "providers_models"
                    and normalized_action == "provider_select_relay_station"
                    and not provider_was_dirty
                    and getattr(adapter, "_cosmetic_binding", False)
                )
                if cosmetic_binding:
                    # The station binding changed only the hidden source
                    # metadata; the visible name and URL are untouched.  Keep
                    # the draft clean so closing the window never asks to
                    # discard a change that altered nothing visible.
                    self._baselines[name] = copy.deepcopy(after)
                dirty = False if cosmetic_binding else after != before
                # Only a dispatch that left the draft somewhere new is a state
                # change.  A read whose answer travels as this response's own
                # ``action_summary`` staged nothing, so it publishes no new
                # revision for other windows to reconcile against.
                if after != action_draft_before:
                    self._advance_revision()
                    self._mark_domain(
                        name,
                        dirty=dirty,
                        base_revision=(
                            self._drafts.get(name, {}).get("base_revision", self._revision)
                            if dirty
                            else self._revision
                        ),
                    )
                    if providers is not None and provider_before is not None:
                        provider_after = self._adapter_draft_state("providers_models")
                        provider_dirty = provider_after != self._baselines.get("providers_models")
                        self._mark_domain(
                            "providers_models",
                            dirty=provider_dirty,
                            base_revision=(
                                self._drafts.get("providers_models", {}).get("base_revision", self._revision)
                                if provider_dirty
                                else self._revision
                            ),
                        )
                    self._persist_metadata()
            if self._revision != previous_revision:
                should_emit = True
            response: dict[str, Any] = {"revision": self._revision}
            if action_summary is not None:
                response["action_summary"] = action_summary
        if should_emit:
            self._request_emit()
        return response

    def reject_plaintext_secret_action(self, action: Mapping[str, Any]) -> None:
        """Reject secret-bearing actions received through ordinary IPC."""

        if not isinstance(action, Mapping):
            raise CoreError("invalid_action", "A Core action is required")
        action_type = action.get("type", action.get("action"))
        if not isinstance(action_type, str):
            return
        if action.get("domain") is None and action_type.startswith("service."):
            return
        name = _canonical_domain(action.get("domain"))
        normalized = action_type.strip().lower().replace("-", "_").replace(".", "_")
        payload = action.get("payload")
        forbidden: set[str] = set()
        if name == "providers_models":
            forbidden = {"api_key", "api_keys"}
            if normalized in {"provider_clear_key", "clear_provider_key", "set_raw", "setraw"}:
                raise CoreError("secret_requires_native", "Use the native secure input for this secret field")
        elif name == "codex":
            forbidden = {"api_key", "auth_text", "raw_json"}
            if normalized in {"set_raw", "setraw"}:
                raise CoreError("secret_requires_native", "Use the versioned code editor for this document")
        elif name == "claude":
            forbidden = {
                "env",
                "desktop_profile",
                "auto_memory_directory",
                "autoMemoryDirectory",
                "token",
                "auth_token",
                "anthropic_auth_token",
                "anthropic_api_key",
                "raw_json",
            }
            if normalized in {"set_raw", "setraw"}:
                raise CoreError("secret_requires_native", "Use the versioned code editor for this document")
        elif name == "webdav":
            forbidden = {"password"}
            if normalized in {"clear_password", "clearpassword"}:
                raise CoreError("secret_requires_native", "Use the native secure input for this secret field")
        elif name == "runtime":
            adapter = self._domains.get(name)
            is_secret = getattr(adapter, "_is_secret_setting", None)
            data = payload if isinstance(payload, Mapping) else {}
            target = data.get("key")
            if isinstance(target, str) and callable(is_secret) and is_secret(target):
                raise CoreError("secret_requires_native", "Use the native secure input for this secret field")
            values = data.get("values", data)
            if isinstance(values, Mapping) and callable(is_secret) and any(
                isinstance(key, str) and is_secret(key) for key in values
            ):
                raise CoreError("secret_requires_native", "Use the native secure input for this secret field")
            if normalized in {"set_raw", "setraw"}:
                raise CoreError("secret_requires_native", "Use the versioned code editor for this document")
        if forbidden and _mapping_contains_key(payload, forbidden):
            raise CoreError("secret_requires_native", "Use the native secure input for this secret field")

    def _dispatch_service(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        operation = action.removeprefix("service.")
        handler = self._service_handlers.get(operation)
        if handler is None:
            # Test-only stores can use an injected handler. Never use this
            # optimistic fallback in production construction.
            state = {"start": "starting", "start_async": "starting", "running": "running", "stop": "stopped", "stopped": "stopped", "restart": "starting", "reload": self._service.get("state", "unknown"), "health": self._service.get("state", "unknown")}.get(operation)
            with self._lock:
                previous_service = copy.deepcopy(self._service)
                if state in SERVICE_STATES:
                    self._service["state"] = state
                return self._project_service_dispatch(previous_service, payload)
        # The controller replaces the proxy on its own threads.  That is the
        # one service call that is not a cache read: under the drain budget a
        # restart legitimately takes minutes.  Holding ``self._lock`` across it
        # blocked every ``snapshot`` in the app, each native host caps a
        # request at 30 s and tears Core down when it expires, and the window
        # that had optimistically drawn "starting" was then left with nothing
        # to correct it.  ``dispatch`` therefore runs the proxy operations with
        # the store lock released, and this method only ever holds it for the
        # short critical sections that project their answer.  The count is
        # raised and every exit path lowers it in ``finally``, so a persist or
        # controller failure can never leave a window unable to see a snapshot.
        published = False
        with self._lock:
            previous_service = copy.deepcopy(self._service)
            self._service_proxy_operations += 1
            if operation in {"start", "start_async"}:
                self._service_start_operations += 1
            try:
                # What the person asked for is recorded before the process is
                # touched: a stop that lands mid-recovery must still win, and a
                # start that fails is still a start this installation owes.
                self._record_service_intent(operation)
                if operation in SERVICE_TRANSITION_OPERATIONS and self._service.get("state") != "starting":
                    # Publish the transition now so every window sees the proxy
                    # leave its state without waiting for the replacement.  The
                    # count is raised first because ``_emit`` re-enters
                    # ``snapshot``, which would otherwise project the
                    # controller's mid-replacement reading back over the state
                    # this dispatch just published.
                    self._publish_service_transition()
                    self._advance_revision()
                    self._persist_metadata()
                    published = True
            except BaseException:
                self._service_proxy_operations = max(0, self._service_proxy_operations - 1)
                if operation in {"start", "start_async"}:
                    self._service_start_operations = max(0, self._service_start_operations - 1)
                raise
        if published:
            # The event fans out to every window and re-enters ``snapshot``;
            # neither may hold the state lock while a controller answers.
            self._emit()
        try:
            # A manual restart and a background post-apply restart must never
            # replace the proxy at the same time.
            with self._service_transition_guard:
                result = handler(operation)
            if not isinstance(result, Mapping):
                raise CoreError("service_error", "LiteLLM service returned invalid status")
            # The answer lands while the count is still held: releasing it first
            # would let a concurrent snapshot read the controller
            # mid-replacement and publish that reading over the state this
            # dispatch just settled.
            reconcile = False
            with self._lock:
                self._set_service_from_result(result, increment=False)
                if str(result.get("state")) == "running" and operation in {"start", "start_async", "restart", "reload"}:
                    # A manual start or restart replaced the process, so a
                    # runtime setting that was still owed one now has its
                    # environment in the running proxy and is no longer owed.
                    # ``reload`` rolls the workers and does not: it leaves the
                    # request standing for the pass that can satisfy it.
                    if operation in {"start", "start_async", "restart"}:
                        self._service_reload_replace_process = False
                    reconcile = True
            if reconcile:
                # Each of these reads and rewrites a client document on disk.
                # They run on the mutation lane (the proxy operation holds it),
                # never under the state lock every window polls.
                self._follow_codex_local_api()
                self.refresh_client_model_lists(refresh_contexts=False)
                # A start or restart is also where the image-context switch
                # and the client's fetch-server entry are reconciled: the
                # pass reads its switch from the proxy's environment, which
                # only a replaced process has.
                self._sync_codex_image_context_mcp(["codex"])
            with self._lock:
                return self._project_service_dispatch(previous_service, payload)
        except CoreError:
            self._project_service_reload_failure()
            raise
        except Exception as exc:
            # The proxy's real state decides what the window shows next; the
            # failed operation itself is still reported to its caller.
            self._project_service_reload_failure()
            raise CoreError("service_error", safe_exception_message(exc)) from None
        finally:
            # Every exit path releases the count, including a persist failure
            # or a cancelled operation: a leak would stop Core from ever
            # projecting a real service state again.
            with self._lock:
                self._service_proxy_operations = max(0, self._service_proxy_operations - 1)
                if operation in {"start", "start_async"}:
                    self._service_start_operations = max(0, self._service_start_operations - 1)

    def _record_service_intent(self, operation: str) -> None:
        """Record what the person asked of the proxy, not what it last did.

        The caller holds ``self._lock``.  Only the operations a person can
        dispatch move the intent; a background reload changes the proxy's
        document, never whether this installation keeps one running.
        """

        intent = {
            "start": "running",
            "start_async": "running",
            "restart": "running",
            "stop": "stopped",
        }.get(operation)
        if intent is None or intent == self._service_intent:
            return
        self._service_intent = intent
        self._persist_metadata()

    def _publish_service_transition(self) -> None:
        """Publish the proxy leaving its state, without erasing its record.

        A planned replacement is announced so every window sees the proxy
        leave the state it was in instead of after the whole replacement.  The
        announcement is a claim about one process being replaced, never about
        the rest of the service record: the port, the recovery summary, and the
        WebDAV result describe settings this replacement does not touch, and a
        pane that loses them during a restart is showing a partly blank service
        row for a router that is still serving.

        The caller holds ``self._lock``.
        """

        if self._service.get("state") == "starting":
            return
        self._service["state"] = "starting"
        # ``detail`` describes the state being left; keeping it would caption
        # the transition with the failure that preceded it.
        self._service.pop("detail", None)

    def _project_service_dispatch(self, previous_service: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        """Apply one service answer to the public projection.

        The caller holds ``self._lock``; this is the short critical section
        that ends a lifecycle dispatch, never the controller call itself.
        """

        if isinstance(payload.get("detail"), str):
            self._service["detail"] = safe_error_message(payload["detail"])
        # A manual health check which projects the same public status is not a
        # state transition, so it does not redraw every settings surface.
        if self._service != previous_service:
            self._advance_revision()
            self._persist_metadata()
        return dict(self._service)

    def _set_service_from_result(self, result: Mapping[str, Any], *, increment: bool) -> None:
        """Project a real controller status into the public Core snapshot."""

        state = result.get("state")
        if state not in SERVICE_STATES:
            state = "unknown"
        service: dict[str, Any] = {"state": state}
        for key in (
            "detail",
            "auto_start_state",
            "launch_background_state",
            "route_recovery",
            "webdav",
        ):
            value = result.get(key)
            if value is not None:
                service[key] = _safe_public(value)
        pid = result.get("pid")
        if type(pid) is int and pid > 0:
            service["pid"] = pid
        port = result.get("port")
        if state == "running" and type(port) is int and 1 <= port <= 65535:
            service["port"] = port
        self._service = service
        if increment:
            self._advance_revision()

    def _runtime_document_fingerprint(self) -> str | None:
        """What the running proxy loads, read before a commit rewrites it.

        ``runtime_config`` is written only by start/reload, so before an Apply
        commits it still holds the exact bytes the live proxy was handed.
        """

        reader = self._service_handlers.get("runtime_fingerprint")
        value = reader() if callable(reader) else None
        return value if isinstance(value, str) else None

    def _schedule_reload_after_webdav_pull(
        self, runtime_document_before: str | None, *, master_key_changed: bool
    ) -> None:
        """Make a document that arrived from another machine reach its proxy.

        A pull used to stop at reloading the adapters: the pane showed the new
        routes while the proxy went on serving the old document, and on a host
        nobody is looking at -- a gateway, a background app -- nothing ever
        noticed.  The workers load the staged document as they come up, so
        installing ``config.yaml`` without this is a file on disk that is not
        served.

        A key that moved needs the process replaced rather than rolled: LiteLLM
        reads ``master_key`` once at startup and this app hands it to the child
        in its environment, so a worker forked by a rolling reload keeps the key
        the old master was started with.  A client presenting the key the bundle
        just installed would then be refused by a proxy that has no such key.
        """

        if master_key_changed:
            self._schedule_service_reload_after_apply(replace_process=True)
            return
        self._schedule_reload_for_applied_domains(("providers_models",), runtime_document_before)

    def _schedule_reload_for_applied_domains(
        self,
        applied: Sequence[str],
        runtime_document_before: str | None,
    ) -> None:
        """Restart the proxy only for a commit it can actually see.

        A providers Apply rewrites ``config.yaml`` whether or not the edit
        reaches the runtime list.  A parked row, a renamed parked row, and the
        credential materialization that writes a relay slot's value all change
        the editor document while leaving the document the proxy loads
        byte-identical.  Each of those used to replace the proxy anyway — a full
        drain, minutes on a machine with long turns in flight — during which
        every window sat at 启动中 for a reload that could not change anything.

        ``runtime`` applies always restart: those settings reach the proxy
        through its process environment, which no document digest covers.
        """

        if "providers_models" not in applied:
            if applied:
                # A ``runtime`` apply reaches the proxy through its *process
                # environment*.  A rolling restart reuses the running master,
                # which can never acquire an environment variable it was not
                # started with (verified: a worker forked after SIGHUP still
                # reads the original value), so only a replaced process can
                # serve one.  Anything else is a document the workers can
                # simply reload.
                self._schedule_service_reload_after_apply(replace_process="runtime" in applied)
            return
        if self._applied_master_key_changed(applied):
            # The proxy key is LiteLLM's ``master_key``, read once at startup.
            # A rolling reload hands the new document to fresh workers of the
            # *same* master, so the process boundary is what decides whether a
            # client presenting the new key is accepted: it takes the same full
            # replacement a runtime environment change takes.
            self._schedule_service_reload_after_apply(replace_process=True)
            return
        stager = self._service_handlers.get("stage_runtime_config")
        try:
            staged = stager("stage_runtime_config") if callable(stager) else None
        except Exception:
            # A config the proxy cannot load is still a commit worth publishing;
            # the restart below reports the failure on the service's own row.
            staged = None
        fingerprint = staged.get("fingerprint") if isinstance(staged, Mapping) else None
        if isinstance(fingerprint, str) and fingerprint == runtime_document_before:
            # The proxy already holds this document; replacing it would spend
            # the whole drain budget to load the bytes it is serving now.
            return
        self._schedule_service_reload_after_apply()

    def _applied_master_key_changed(self, applied: Sequence[str]) -> bool:
        """Whether this commit changed the key the proxy authenticates with.

        The runtime adapter answers it from the Apply it just performed; a
        domain that does not track the key answers ``False``, so this stays a
        fact about the commit rather than a guess about the document.
        """

        if "runtime" not in applied:
            return False
        adapter = self._domains.get("runtime")
        changed = getattr(adapter, "master_key_changed", None)
        return bool(changed()) if callable(changed) else False

    def _schedule_service_reload_after_apply(self, *, replace_process: bool = False) -> None:
        """Queue the post-apply LiteLLM restart without blocking the commit.

        Replacing the proxy takes seconds, so the restart runs on its own
        thread: the committed edit answers immediately and every later IPC
        request stays fast.  A burst of edits coalesces into one final restart
        against the newest configuration.

        ``replace_process`` marks a commit the running process cannot adopt --
        a runtime setting that arrives as an environment variable.  That one
        still takes the full stop/start, because no amount of signalling can
        give a live process a variable it was not started with.
        """

        if self._service_handlers.get("reload") is None:
            _note("an apply wanted a proxy restart, but this app has no proxy to restart")
            return
        with self._service_reload_guard:
            if self._service_reload_stopping:
                return
            # A pending full replacement outranks a rolling one: if any commit
            # in the coalesced burst needs the process gone, the final restart
            # must replace it rather than roll its workers.
            if replace_process:
                self._service_reload_replace_process = True
            thread = self._service_reload_thread
            if thread is not None and thread.is_alive():
                # A restart is already replacing the proxy; it will pick up
                # this commit as its follow-up pass.
                self._service_reload_pending = True
                return
            # A replacement that is still owed outranks the cached service row:
            # the previous pass wrote that row from the failed replacement, so it
            # can read `stopped` while the controller has already recovered.  The
            # worker probes the live state off every write lane before it decides
            # (see ``_reload_service_after_apply``); refusing here would drop the
            # one pass that can still deliver the environment the commit needs.
            if (
                self._service.get("state") not in {"running", "unhealthy"}
                and not self._service_reload_replace_process
            ):
                _note(
                    "an apply wanted a proxy restart, but the service reads %s"
                    % self._service.get("state")
                )
                return
            _note(
                "an apply is replacing the proxy (%s)"
                % ("full replacement" if self._service_reload_replace_process else "rolling reload")
            )
            thread = threading.Thread(
                target=self._service_reload_worker,
                name="litellm-core-service-reload",
                daemon=True,
            )
            self._service_reload_thread = thread
        thread.start()

    def _restore_replace_process_request(self, owed: bool) -> None:
        """Keep owing a full replacement that this pass did not deliver.

        Set again under the scheduler's own lock so a commit that arrived while
        the failed attempt was in flight keeps its own claim too.
        """

        if not owed:
            return
        with self._service_reload_guard:
            self._service_reload_replace_process = True

    def wait_for_service_reload(self, timeout: float = 10.0) -> bool:
        """Wait for a background post-apply restart to finish.

        The native hosts and focused tests use this to observe the restart
        that Apply now performs off the request path.
        """

        thread = self._service_reload_thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _service_reload_worker(self) -> None:
        while True:
            try:
                self._reload_service_after_apply()
            except Exception as exc:
                # A failed background restart must not kill the worker before
                # the controller's real state is projected -- but it must also
                # not be the silence that leaves a host serving nothing with
                # nobody to ask.  The stop has already happened by the time a
                # launch fails here, so this line is the only account of why.
                _note("the proxy was not replaced after an apply", trace=exc)
                self._project_service_reload_failure()
            with self._service_reload_guard:
                if self._service_reload_stopping or not self._service_reload_pending:
                    self._service_reload_thread = None
                    return
                self._service_reload_pending = False

    def _reload_service_after_apply(self) -> None:
        """Reload LiteLLM after settings that shape the proxy are committed.

        Runs only on the reload worker: the controller restart takes seconds,
        and no editor may wait on it.  The pass therefore holds the state lock
        only for its own short transitions and takes the mutation lane only
        for the client-document reconciliation -- a provider Apply that lands
        while the proxy is being replaced must answer immediately and fold
        into one follow-up reload.
        """

        reloader = self._service_handlers.get("reload")
        # The cached row is what the previous pass wrote, and a replacement that
        # did not come up leaves it reading `stopped` even after the controller
        # recovered.  This worker holds no write lane and no request waits on it,
        # so the one real status read that can tell a stale row from a stopped
        # proxy belongs here -- and only while the row says the proxy is down.
        if (
            reloader is not None
            and not self._service_reload_stopping
            and self._service.get("state") not in {"running", "unhealthy"}
        ):
            try:
                self._service_row_is_live()
            except Exception:
                pass
        replace_process = False
        with self._lock:
            if self._service_reload_stopping:
                return
            if self._service.get("state") not in {"running", "unhealthy"}:
                return
            if reloader is None:
                return
            # Project the restart so every open window sees the service leave
            # the running state while the proxy is replaced.  The rest of the
            # record is the service's own, not part of this claim.
            self._publish_service_transition()
            # A runtime setting travels as an environment variable, so the live
            # process cannot adopt it: that commit asks for a replacement
            # rather than the rolling reload a document-only change takes.  The
            # request is taken (and cleared) here, under the same lock the
            # scheduler sets it with: a commit that arrives while this pass runs
            # therefore sets it again and is owed its own pass, instead of being
            # swallowed by the restart already in flight.
            replace_process = self._service_reload_replace_process
            self._service_reload_replace_process = False
        self._request_emit()
        try:
            with self._service_transition_guard:
                if self._service_reload_stopping:
                    return
                service_result = reloader("restart" if replace_process else "reload")
        except Exception as exc:
            _note("restarting the proxy after an apply failed", trace=exc)
            self._restore_replace_process_request(replace_process)
            self._project_service_reload_failure()
            return
        if not isinstance(service_result, Mapping):
            _note("restarting the proxy returned no state: %r" % (service_result,))
            self._restore_replace_process_request(replace_process)
            self._project_service_reload_failure()
            return
        if replace_process and service_result.get("state") != "running":
            # The replacement did not come up.  Said here rather than left to a
            # row nobody is watching: on a host this is the difference between
            # a page that answers and a unit that reports `active` while
            # nothing is listening.
            _note(
                "the replaced proxy did not come up: state=%s port=%s"
                % (service_result.get("state"), service_result.get("port"))
            )
            # The replacement did not come up, so the environment this commit
            # needs is still not in the running process: keep owing it rather
            # than letting a later pass answer it with a rolling reload.
            self._restore_replace_process_request(True)
        with self._lock:
            self._set_service_from_result(service_result, increment=False)
            running = service_result.get("state") == "running"
        self._request_emit()
        if running:
            # This reconciliation reads and rewrites the client documents on
            # disk -- the Codex catalog, the Claude picker, the fetch-server
            # entry -- and it used to run with the store lock held.  Measured on
            # lv: a page poll taken while the proxy was being replaced waited
            # 0.67-0.82 s for that lock, which is a window that feels frozen
            # exactly when a person is watching the reload happen.  It takes
            # the mutation lane (so an Apply cannot rewrite the same files
            # underneath it) and the state lock only for its bookkeeping.
            with self._mutation_scope():
                with self._domain_scope("codex", "claude", "clients"):
                    self._reconcile_client_documents_after_reload()
            self._request_emit()
        if self._service_reload_stopping:
            # The host quit while this restart was in flight; shutdown() may
            # have stopped the previous proxy before this one started.
            self._stop_service_after_reload_shutdown()

    def _reconcile_client_documents_after_reload(self) -> None:
        """Bring the generated client documents up to date with the proxy.

        Called after the proxy answers again, with no store lock held: every
        step here touches a file on disk, and a window polling its snapshot must
        not wait for those writes.  The one part that does move Core's own
        projection -- a catalog observation that changes the revision -- takes
        the lock for that bookkeeping alone.
        """

        codex = self._domains.get("codex")
        refresh_catalog = getattr(codex, "refresh_model_catalog", None)
        if callable(refresh_catalog):
            catalog_revision_before = getattr(codex, "revision", None)
            try:
                refresh_catalog()
            except Exception:
                pass
            if getattr(codex, "revision", None) != catalog_revision_before:
                with self._lock:
                    # A snapshot is memoized on Core's revision; a domain
                    # observation that changed the catalog must invalidate that
                    # memo even though no draft moved.
                    self._advance_revision()
                    self._snapshot_cache = None
                    try:
                        self._persist_metadata()
                    except PersistenceError:
                        pass
        # The caller holds the mutation lane; these rewrite client files and
        # take the state lock only for their own bookkeeping, never across the
        # write.
        self._follow_codex_local_api(codex)
        self.refresh_client_model_lists(refresh_contexts=False)
        # The image-context switch lives in the runtime settings and the fetch
        # server in the client's document.  Reconciling here as well as on Apply
        # is what makes the two agree after a start: a settings file edited
        # while the app was closed, or a client file restored from a backup,
        # would otherwise leave the pass on with no tool to fetch what it
        # omitted.
        self._sync_codex_image_context_mcp(["codex"])

    def _follow_codex_local_api(self, codex: object | None = None) -> None:
        """Keep a Codex client that uses this app's proxy on its current endpoint.

        A restart is where the proxy's port and master key can change, so a
        client that already points at the proxy is rewritten there instead of
        being left on a closed port or a retired key.  The caller holds
        ``self._lock``; the write happens before the revision is published so
        every open window sees the new file and the new state together.
        """

        adapter = codex if codex is not None else self._domains.get("codex")
        follow = getattr(adapter, "follow_local_api_endpoint", None)
        if not callable(follow):
            return
        try:
            changed = bool(follow())
        except Exception:
            # A client file this app cannot rewrite (locked, unreadable, hand
            # edited) must not fail the proxy restart that just succeeded.
            return
        if not changed or adapter is None:
            return
        name = "codex"
        # The applied state is the adapter's draft projection, which is what a
        # normal apply records as the baseline for this domain; recording the
        # written documents here clears the dirty flag the reconcile is not
        # responsible for.
        self._baselines[name] = copy.deepcopy(self._adapter_draft_state(name))
        self._disk[name] = {
            "changed": False,
            "generation": int(self._disk.get(name, {}).get("generation", 0)),
            "keep_draft": False,
        }
        self._disk_identities[name] = self._external_disk_identity(adapter)
        self._advance_revision()
        self._mark_domain(
            name,
            dirty=False,
            validation={"valid": True, "issues": []},
            base_revision=self._revision,
        )
        self._persist_metadata()

    def _project_service_reload_failure(self) -> None:
        """Project the controller's real state after a failed background restart."""

        result: Mapping[str, Any] | None = None
        for operation in ("health", "status"):
            handler = self._service_handlers.get(operation)
            if handler is None:
                continue
            # Both probes may spend an endpoint timeout; neither may do it
            # with the state lock every window polls.
            try:
                candidate = handler(operation)
            except Exception:
                continue
            if isinstance(candidate, Mapping):
                result = candidate
                break
        with self._lock:
            self._set_service_from_result(
                result if result is not None else {"state": "unhealthy"},
                increment=False,
            )
        self._request_emit()

    def supervise_service_once(self) -> bool:
        """Restore an unwatched proxy instead of leaving 不健康 to a click.

        One pass reads the controller's real status and acts only on a state
        this Core can own:

        * ``running``/``starting`` are progress, and they reset the patience.
        * ``unhealthy`` is an owned child that cannot answer its own liveness
          endpoint.  It keeps ``SERVICE_RECOVERY_PATIENCE_SECONDS`` of
          continuous failure first, because a burst of long turns can make one
          0.5 s probe miss while the proxy is still serving them; after that
          the process is replaced.
        * ``stopped`` with the intent still ``running`` is a proxy that died,
          failed to start, or was never launched by a window -- it is started.
        * ``unknown`` is a listener this installation does not own.  It is
          never touched.

        No pass runs while an explicit lifecycle operation or the post-apply
        reload owns the proxy, and none runs after Core began stopping.  The
        recovery takes the same transition guard an explicit operation does,
        so a person's stop and this pass can never both win.

        Returns whether this pass touched the proxy.
        """

        # One pass at a time.  The loop is single-threaded, but a caller may
        # also ask for one pass directly, and two recoveries must not overlap.
        if not self._service_supervision_guard.acquire(blocking=False):
            return False
        try:
            return self._supervise_service_once()
        finally:
            self._service_supervision_guard.release()

    def _supervise_service_once(self) -> bool:
        # A pass can replace the proxy, which takes seconds.  It holds the
        # mutation lane so a pane's edit cannot land mid-replacement, while
        # snapshots keep reading the last committed projection.
        should_emit = False
        with self._mutation_scope():
            with self._lock:
                if self._service_supervision_stopping or self._service_intent != "running":
                    self._service_supervision_unhealthy_since = None
                    return False
                if self._service_proxy_operations > 0:
                    return False
                reload_thread = self._service_reload_thread
                if reload_thread is not None and reload_thread.is_alive():
                    return False
                handlers = dict(self._service_handlers)
            # A forced read: the settled cache would hide the death this pass is
            # looking for until it expired.
            status_handler = handlers.get("health") or handlers.get("status")
            if status_handler is None:
                return False
            try:
                current = status_handler("health" if handlers.get("health") is not None else "status")
            except Exception:
                return False
            if not isinstance(current, Mapping):
                return False
            state = str(current.get("state"))
            now = time.monotonic()
            if state in {"running", "starting"}:
                self._service_supervision_unhealthy_since = None
                self._service_supervision_attempts = 0
                self._service_supervision_retry_at = 0.0
                return False
            if state == "unhealthy":
                if self._service_supervision_unhealthy_since is None:
                    self._service_supervision_unhealthy_since = now
                    return False
                if now - self._service_supervision_unhealthy_since < SERVICE_RECOVERY_PATIENCE_SECONDS:
                    return False
            elif state != "stopped":
                # "unknown" is somebody else's listener on the configured port.
                self._service_supervision_unhealthy_since = None
                return False
            if now < self._service_supervision_retry_at:
                return False
            operation = "restart" if state == "unhealthy" else "start"
            handler = handlers.get(operation)
            if handler is None:
                return False
            # Stated before the attempt, not after: a recovery that hangs or
            # dies while replacing the proxy leaves this line as the only
            # record that anything was being done about it.
            _note("recovering the proxy (%s -> %s)" % (state, operation))
            with self._service_transition_guard:
                published = False
                with self._lock:
                    # Re-read the decision under the guard: a stop, a Core
                    # shutdown, or a person's own start may have landed since
                    # the probe.
                    if self._service_supervision_stopping or self._service_intent != "running":
                        return False
                    if self._service_proxy_operations > 0:
                        return False
                    # Hold the replacement count for this pass exactly as an
                    # explicit operation does: every window must draw 启动中
                    # while the proxy is being replaced, never the failure it
                    # is replacing.
                    self._service_proxy_operations += 1
                    self._publish_service_transition()
                    self._advance_revision()
                    self._persist_metadata()
                    published = True
                if published:
                    # Sent here, not requested: the replacement below may take
                    # minutes, and every window has to see the proxy leave its
                    # state before it starts rather than after it ends.
                    self._emit()
                reconcile = False
                try:
                    try:
                        result = handler(operation)
                    except Exception as exc:
                        _note("the proxy recovery (%s) failed" % operation, trace=exc)
                        result = None
                    with self._lock:
                        if isinstance(result, Mapping):
                            self._set_service_from_result(result, increment=False)
                        recovered = isinstance(result, Mapping) and str(result.get("state")) == "running"
                        if recovered:
                            self._service_supervision_unhealthy_since = None
                            self._service_supervision_attempts = 0
                            self._service_supervision_retry_at = 0.0
                            # A recovery that replaced the process satisfies a
                            # runtime setting that was still owed a
                            # replacement, and it is where a client that points
                            # at this proxy follows a port change.
                            self._service_reload_replace_process = False
                            reconcile = True
                        else:
                            self._service_supervision_attempts += 1
                            self._service_supervision_retry_at = now + _service_recovery_delay(
                                self._service_supervision_attempts
                            )
                            if self._service_supervision_unhealthy_since is not None:
                                # The wedged child was replaced but its
                                # replacement is not healthy yet; afford it its
                                # own patience rather than restarting it on the
                                # very next pass.
                                self._service_supervision_unhealthy_since = now
                finally:
                    with self._lock:
                        self._service_proxy_operations = max(0, self._service_proxy_operations - 1)
                    if self._service_supervision_stopping:
                        # A quit landed while this pass was replacing the
                        # proxy, and its own stop may have run before this
                        # child existed.  End it here rather than leave an
                        # orphan holding the port.  The stop is idempotent: one
                        # that already ran finds no owner.
                        stopper = handlers.get("stop")
                        if stopper is not None:
                            try:
                                stopper("stop")
                            except Exception:
                                pass
                if reconcile:
                    # Disk work: the Codex endpoint file, the generated client
                    # lists, and the image-context MCP entry.  It holds the
                    # document domains it rewrites (nested inside this pass's
                    # transaction lane) and never the state lock.
                    with self._domain_scope("codex", "claude", "clients"):
                        self._follow_codex_local_api()
                        self.refresh_client_model_lists(refresh_contexts=False)
                        self._sync_codex_image_context_mcp(["codex"])
            # ``snapshot`` re-reads the controller once no replacement is in
            # flight, so a failed pass publishes the real state instead of
            # leaving the 启动中 it had to claim while it tried.
            should_emit = True
        if should_emit:
            self._request_emit()
        return True

    def _stop_service_after_reload_shutdown(self) -> None:
        stopper = self._shutdown_handler
        if stopper is not None:
            try:
                with self._service_transition_guard:
                    result = stopper("stop")
                if isinstance(result, Mapping):
                    with self._lock:
                        self._set_service_from_result(result, increment=False)
            except Exception:
                pass
        self._request_emit()

    def validate(self, domain: str | None = None, *, revision: int | None = None, payload: object | None = None) -> dict[str, Any]:
        names = [_canonical_domain(domain)] if domain is not None else self._domain_names()
        with self._domain_scope(*names):
            self._check_revision(revision)
            summaries: dict[str, Any] = {}
            for name in names:
                adapter = self._domains.get(name)
                if adapter is None:
                    raise DomainNotFound(name)
                try:
                    value = adapter.validate(payload if len(names) == 1 else None)
                except Exception as exc:
                    value = {"valid": False, "errors": [safe_exception_message(exc)]}
                summary = _validation_summary(value)
                self._mark_domain(name, validation=summary)
                summaries[name] = summary
            if domain is not None:
                return summaries[names[0]]
            issues: list[dict[str, Any]] = []
            for name, summary in summaries.items():
                for issue in summary["issues"]:
                    issue = dict(issue)
                    issue["path"] = f"{name}.{issue.get('path', '')}".rstrip(".")
                    issues.append(issue)
            return {"valid": not any(issue["severity"] == "error" for issue in issues), "issues": issues}

    @staticmethod
    def _relay_operation_count(relay: DomainAdapter) -> int:
        """Return the public count of remote relay work that remains.

        The relay adapter deliberately owns the journal and its retry state.
        Core only needs the count for the Apply result, and must never copy a
        credential-bearing resource payload into an IPC response.
        """

        try:
            state = relay.snapshot()
        except Exception:
            return 0
        if not isinstance(state, Mapping):
            return 0
        count = state.get("pending_operation_count")
        if type(count) is int and count >= 0:
            return count
        entries = state.get("pending_operations")
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes, bytearray)):
            return 0
        return sum(
            1
            for entry in entries
            if isinstance(entry, Mapping)
            and str(entry.get("status", entry.get("state", "staged"))) != "completed"
        )

    @staticmethod
    def _relay_apply_issues(count: int, *, phase: str = "relay_apply") -> list[dict[str, str]]:
        """Return a stable secret-free issue summary for an Apply result."""

        if count <= 0:
            return []
        return [
            {
                "code": phase,
                "message": "Relay synchronization has work that still needs attention.",
            }
        ]

    @staticmethod
    def _relay_public_issue_count(value: object) -> int:
        """Count relay issues without echoing remote/server text to IPC."""

        if not isinstance(value, Mapping):
            return 0
        issues = value.get("issues")
        if not isinstance(issues, Sequence) or isinstance(issues, (str, bytes, bytearray)):
            return 0
        return sum(1 for item in issues if isinstance(item, (str, Mapping)))

    def _mark_relay_coordinated_applied(self, name: str) -> None:
        """Advance Core's local baseline after one coordinated local commit."""

        self._baselines[name] = self._adapter_draft_state(name)
        self._disk[name] = {
            "changed": False,
            "generation": int(self._disk.get(name, {}).get("generation", 0)),
            "keep_draft": False,
        }
        self._mark_domain(name, dirty=False, validation={"valid": True, "issues": []}, base_revision=self._revision + 1)

    def _apply_relay_coordinated(
        self,
        names: Sequence[str],
        adapters: Mapping[str, DomainAdapter],
        confirm_codes: Sequence[str],
        overwrite_codes: set[str],
        *,
        relay_dependency_only: bool = False,
    ) -> dict[str, Any]:
        """Apply relay and linked model changes around irreversible remote work.

        The normal multi-domain Apply path can restore local files and adapter
        objects. It cannot undo a remote key create, rotation, or deletion.
        This coordinator therefore commits in the one safe ordering:

        * validate and journal first;
        * perform non-destructive remote mutations;
        * reconcile stable resource IDs and materialize linked deployments;
        * save local files and reload LiteLLM;
        * only then delete remote keys.

        Once an operation may have reached the remote service, a later local
        failure becomes a truthful ``partial`` result. The relay journal is
        intentionally retained for a retry instead of attempting a fictional
        rollback.
        """

        relay = adapters.get("relay_accounts")
        if relay is None:
            raise CoreError("relay_apply_unavailable", "Relay synchronization is unavailable")
        prepare = getattr(relay, "prepare_apply", None)
        execute = getattr(relay, "execute_pending_operations", None)
        reconcile = getattr(relay, "reconcile_apply", None)
        commit = getattr(relay, "commit_apply", None)
        finalize = getattr(relay, "finalize_apply", None)
        binding_materials = getattr(relay, "binding_materials", None)
        if not all(callable(method) for method in (prepare, execute, reconcile, commit, finalize, binding_materials)):
            raise CoreError("relay_apply_unavailable", "Relay synchronization is unavailable")
        # The journal runs four passes over the same accounts, and only a pass
        # that actually writes a key changes what the station reports.  Opening
        # one read memo for the whole Apply is what keeps a routine edit from
        # paying a full dashboard round trip plus one gateway catalog per key
        # once per pass, with the store lock held throughout.  The domain drops
        # an account's entry as soon as a write lands on it.
        begin_reads = getattr(relay, "begin_apply_reads", None)
        end_reads = getattr(relay, "end_apply_reads", None)
        try:
            if callable(begin_reads):
                begin_reads()
            return self._apply_relay_transaction(
                names,
                adapters,
                confirm_codes,
                overwrite_codes,
                relay=relay,
                relay_dependency_only=relay_dependency_only,
                prepare=prepare,
                execute=execute,
                reconcile=reconcile,
                commit=commit,
                finalize=finalize,
                binding_materials=binding_materials,
            )
        finally:
            if callable(end_reads):
                end_reads()

    def _apply_relay_transaction(
        self,
        names: Sequence[str],
        adapters: Mapping[str, DomainAdapter],
        confirm_codes: Sequence[str],
        overwrite_codes: set[str],
        *,
        relay: DomainAdapter,
        relay_dependency_only: bool,
        prepare: Any,
        execute: Any,
        reconcile: Any,
        commit: Any,
        finalize: Any,
        binding_materials: Any,
    ) -> dict[str, Any]:
        """The coordinated relay transaction, inside one Apply's read memo."""

        transaction_adapters = dict(adapters)
        adapter_checkpoints = {
            name: _checkpoint_adapter(adapter, error_code="apply_failed")
            for name, adapter in transaction_adapters.items()
        }
        # Read before anything commits: the runtime document still holds the
        # bytes the running proxy was handed, so this is what the proxy is
        # serving now.
        runtime_document_before = self._runtime_document_fingerprint()
        core_checkpoint = {
            "revision": self._revision,
            "drafts": copy.deepcopy(self._drafts),
            "last_actions": copy.deepcopy(self._last_actions),
            "baselines": copy.deepcopy(self._baselines),
            "disk": copy.deepcopy(self._disk),
            "disk_identities": copy.deepcopy(self._disk_identities),
            "service": copy.deepcopy(self._service),
        }
        persistence_paths = [
            path
            for adapter in transaction_adapters.values()
            for path in _adapter_persistence_paths(adapter)
        ]
        if self._metadata_store is not None:
            persistence_paths.append(self._metadata_store.path)
        file_checkpoints = _checkpoint_files(persistence_paths)
        remote_boundary_crossed = False
        provider_locally_applied = False
        relay_locally_applied = False
        applied: list[str] = []
        operation_total = 0

        def restore_local_state() -> None:
            _restore_files(file_checkpoints)
            for adapter_name, adapter in transaction_adapters.items():
                _restore_adapter(adapter, adapter_checkpoints[adapter_name])
            self._restore_revision(core_checkpoint["revision"])
            self._drafts = copy.deepcopy(core_checkpoint["drafts"])
            self._last_actions = copy.deepcopy(core_checkpoint["last_actions"])
            self._baselines = copy.deepcopy(core_checkpoint["baselines"])
            self._disk = copy.deepcopy(core_checkpoint["disk"])
            self._disk_identities = copy.deepcopy(core_checkpoint["disk_identities"])
            self._service = copy.deepcopy(core_checkpoint["service"])

        try:
            for name in names:
                if f"overwrite_external_{name}" not in overwrite_codes:
                    continue
                rebase = getattr(adapters[name], "rebase_external_disk", None)
                if not callable(rebase):
                    raise CoreError("apply_failed", "Settings changed on disk; choose the disk version")
                rebase()
                self._disk[name]["changed"] = False
                self._disk[name]["keep_draft"] = True

            # Validate every participating local domain before a remote API
            # call. Relay's preflight separately validates login/session,
            # operation targets, and deletion policies.
            for name in names:
                if name == "providers_models":
                    # Linked provider keys intentionally have no materialized
                    # value until the relay resolution step below. Validate
                    # the rest of the candidate with a domain-owned placeholder
                    # contract, then run the strict validator after private
                    # material is injected and before any local write.
                    preflight = getattr(adapters[name], "validate_relay_preflight", None)
                    if not callable(preflight) or preflight().get("valid") is not True:
                        raise CoreError("provider_model_invalid", "Fix the provider/model issues")
                    continue
                if relay_dependency_only and name == "relay_accounts":
                    # This Apply uses a linked key; it is not the user's relay
                    # work, so the station's session state and its outstanding
                    # operations are not this edit's validation.
                    continue
                if not self.validate(name)["valid"]:
                    if name == "relay_accounts" and "providers_models" in adapters:
                        # The relay's preflight is the journal: pending key
                        # creates, a session that needs a sign-in, a target
                        # that moved.  None of it is a fact about the provider
                        # document this Apply is writing, and none of it can
                        # refuse that write — the keys it materializes are the
                        # ones this Core already holds.  So a provider edit
                        # leaves the journal exactly where it is, retryable
                        # from the relay's own surface, instead of turning
                        # "add a model" into 中转站还有待处理的操作.
                        relay_dependency_only = True
                        continue
                    raise CoreError("relay_preflight_failed", "Fix the validation errors")
            prepared = {"ready": True, "operations": ()} if relay_dependency_only else prepare()
            if not isinstance(prepared, Mapping) or prepared.get("ready") is not True:
                # The relay is what is not ready here: pending operations, a
                # session that needs a sign-in, or a target that moved.  When
                # this Apply is *also* writing a provider document, that
                # backlog is not the edit's business: the journal belongs to
                # the relay's own surface, and it stays exactly where it is,
                # retryable from there.  Refusing the edit made "add a model"
                # wait on a sign-in the edit never needed and answer
                # 更改未生效 whenever the station could not be reached — the
                # model itself is written from the keys this Core already
                # holds, with no station involved.  A relay-only Apply still
                # says its own code, because there the backlog *is* the work.
                if relay_dependency_only or "providers_models" not in adapters:
                    raise CoreError("relay_not_ready", "Fix the relay connection or binding issues")
                relay_dependency_only = True
                prepared = {"ready": True, "operations": ()}
            operations = prepared.get("operations", ())
            if isinstance(operations, Sequence) and not isinstance(operations, (str, bytes, bytearray)):
                operation_total = len(operations)
            else:
                operations = ()
            # The journal itself is the phase source of truth. It is kept
            # deliberately small and secret-free, so Core can classify work
            # without inspecting an API request body.
            non_destructive = [] if relay_dependency_only else [
                operation
                for operation in operations
                if isinstance(operation, Mapping)
                and operation.get("kind") != "api_key_delete"
                and operation.get("state", operation.get("status", "staged")) == "staged"
            ]
            if non_destructive:
                # We cross the remote boundary immediately before invoking the
                # helper because a timeout is ambiguous: it may have applied
                # the remote mutation even when no response was received.
                remote_boundary_crossed = True
                execute(prepared, phase="non_destructive")

            if not relay_dependency_only:
                reconciled = reconcile(prepared, phase="non_destructive")
                # Transport failures are provisional until the immediate factual
                # refresh. A lost response may still have applied remotely; only
                # unresolved reconciliation issues stop the coordinated Apply —
                # and that is the relay's own outstanding work, which the relay
                # message names, never the edit the user just made.
                if self._relay_public_issue_count(reconciled):
                    if non_destructive or "providers_models" not in adapters:
                        raise CoreError("relay_not_ready", "Fix the relay connection or binding issues")
                    # Nothing this Apply staged was sent to the station: every
                    # unresolved entry is work an earlier attempt left behind,
                    # and the reconciliation could not answer for it because
                    # the session or the station is not there.  That backlog
                    # belongs to the relay's own surface; it must not refuse
                    # the provider document this Apply is writing, whose keys
                    # are already on their slots.  It stays journaled and
                    # retryable where it is.
                    relay_dependency_only = True
                    prepared = {"ready": True, "operations": ()}
                    operations = ()

            providers = adapters.get("providers_models")
            if providers is not None and not relay_dependency_only:
                # The destructive phase below is the one place a key the
                # station still serves is actually removed, so it is the one
                # place that must leave no linked slot or route behind.  A
                # dispatch normally released them when it staged the delete,
                # but the journal outlives that call: a Core that restarted, a
                # delete staged by an older build, or an entry point that
                # reported no dependencies all hand this Apply a staged delete
                # whose rows were never released.  Applying each operation's
                # own recorded policy here — before the provider document is
                # written, and before the station is called — makes the release
                # a property of the journal instead of of whichever caller
                # happened to write it.  Every policy is idempotent, so a
                # delete already released by its dispatch finds nothing left to
                # release here.  ``rebind`` is the exception: it needs the
                # target the caller chose, which only that dispatch carries.
                dependency_handler = getattr(providers, "apply_relay_dependency_policy", None)
                if not callable(dependency_handler):
                    raise CoreError("relay_apply_unavailable", "Linked model dependency handling is unavailable")
                for recorded_policy, retired in _relay_operation_dependencies(operations).items():
                    normalized_policy = _relay_dependency_policy_name(recorded_policy)
                    if normalized_policy == "rebind":
                        continue
                    if not normalized_policy:
                        raise CoreError("relay_apply_unavailable", "Linked model dependency handling is unavailable")
                    released = dependency_handler(retired, normalized_policy)
                    release_issues = released.get("issues") if isinstance(released, Mapping) else None
                    if (
                        isinstance(release_issues, Sequence)
                        and not isinstance(release_issues, (str, bytes, bytearray))
                        and release_issues
                    ):
                        raise CoreError("relay_apply_unavailable", "Linked model dependency handling could not be completed")
            if providers is not None:
                materialize = getattr(providers, "materialize_relay_bindings", None)
                dependency_summary = getattr(providers, "dependency_summary", None)
                if not callable(materialize) or not callable(dependency_summary):
                    raise CoreError("relay_apply_unavailable", "Linked model synchronization is unavailable")
                dependencies = dependency_summary()
                provider_keys = dependencies.get("provider_keys", []) if isinstance(dependencies, Mapping) else []
                # ``None`` means “judge every route”: only a relay's own Apply
                # has the station's answer as its edit.  A dependency-only Apply
                # names the rows it changed below.
                binding_scope: object | None = None
                sources = [
                    item.get("source")
                    for item in provider_keys
                    if isinstance(item, Mapping)
                    and isinstance(item.get("source"), Mapping)
                ]
                if sources:
                    # A relay ProviderKey is itself a dynamic binding, even
                    # before a model selects it.  Resolve every selected key
                    # here so a provider-side key import is fully usable
                    # after Apply; private material remains inside Core.
                    # Resolving a linked key may still read the station — a
                    # key that has never been materialized has to come from
                    # somewhere — but this Apply's *readiness* never depends on
                    # it: the resolution falls back to the credential already
                    # persisted on the slot and to the catalog the last
                    # successful read recorded, so signing in is not a
                    # precondition for managing a model.
                    # The durable half of "a key this Core already holds":
                    # the provider document persists each linked slot's value,
                    # and the relay domain's own cache does not survive a
                    # restart.  Passing it here is what makes a model edit work
                    # on a fresh Core while the station is unreachable.
                    slot_reader = getattr(providers, "relay_slot_credentials", None)
                    slot_credentials = slot_reader() if callable(slot_reader) else None
                    # One model edit names one key.  Resolving every linked key
                    # in the document to write that one row made an unrelated
                    # route's own problem (a stale upstream, a group that moved)
                    # refuse this edit, and it paid a station round trip per key
                    # when a newly bound slot still had to be read.  A
                    # dependency-only Apply therefore resolves the keys its own
                    # edit touches, plus any slot that carries no persisted
                    # credential and so cannot be resolved without the station.
                    # Every other linked route keeps its stored credential and
                    # its applied row exactly as they are.
                    if relay_dependency_only:
                        sources = _relay_dependency_sources(
                            provider_keys,
                            slot_credentials,
                            dependency_baseline=self._baselines.get("providers_models"),
                            dependency_draft=self._adapter_draft_state("providers_models"),
                        )
                        # The rows this Apply may refuse: a dependency-only edit
                        # judges the routes it changed, never a stale row the
                        # station happens to answer about beside them.
                        baseline_projection = set(
                            _relay_binding_projection(self._baselines.get("providers_models"))
                        )
                        binding_scope = [
                            row
                            for row in _relay_binding_projection(
                                self._adapter_draft_state("providers_models")
                            )
                            if row not in baseline_projection
                        ]
                    else:
                        # A relay's own Apply is the read: every route it
                        # materializes is judged against the station's answer.
                        binding_scope = None
                    # A dependency-only Apply carries no relay work of its own:
                    # it needs each key the draft binds, and the document already
                    # persists every one of them.  Asking the station anyway spent
                    # that whole round trip *inside* the store lock — an account
                    # refresh plus one gateway catalog per key, which outlasts the
                    # native hosts' request budget once a station stalls — and the
                    # read could not change the answer, because the resolution
                    # already falls back to the slot's own value.  Resolving
                    # locally is what keeps "add a model" an edit about the model.
                    # A key only a station read could reveal is still reported
                    # unresolved, exactly as it is when the station cannot be
                    # reached at all.
                    materials = binding_materials(
                        {"resources": sources},
                        refresh=True,
                        slot_credentials=slot_credentials,
                    )
                    materialized = materialize(materials, scope=binding_scope)
                    # Whatever this resolution said becomes the pane's own map
                    # of the linked rows: a successful one clears it, a failed
                    # one marks the key or route that refused the write.
                    recorder = getattr(providers, "record_binding_issues", None)
                    if callable(recorder):
                        recorder(materialized.get("issues") if isinstance(materialized, Mapping) else ())
                    if self._relay_public_issue_count(materialized):
                        # A linked key that the relay could not resolve — a moved
                        # or disabled resource, a base-URL conflict, an upstream
                        # model the group does not serve — refused the write, so
                        # the strip names that cause instead of a bare rollback:
                        # the internal step name stays out of the pane.
                        raise CoreError(
                            "relay_binding_failed",
                            "A linked relay binding could not be resolved",
                        )
                if not self.validate("providers_models")["valid"]:
                    raise CoreError("provider_model_invalid", "Fix the linked model issues")
                providers.apply()
                provider_locally_applied = True
                self._mark_relay_coordinated_applied("providers_models")
                applied.append("providers_models")

            # Other local domains can participate in one explicit Apply. They
            # commit after non-destructive relay work but before any remote
            # deletion. A failure is therefore either fully rollbackable
            # (there were no remote mutations) or truthfully partial.
            for name in names:
                if name in {"providers_models", "relay_accounts"}:
                    continue
                adapter = adapters[name]
                domain_confirmations = list(confirm_codes)
                if name == "claude" and any(
                    item in {"accepted", "claude-risk-confirmed"} for item in domain_confirmations
                ):
                    try:
                        from .domains.claude import RISK_CONFIRMATION_CODES
                    except Exception:
                        RISK_CONFIRMATION_CODES = ()
                    domain_confirmations = [str(item) for item in RISK_CONFIRMATION_CODES]
                payload: object | None = None
                if domain_confirmations:
                    payload = {"confirm_risks": domain_confirmations, "confirmation": domain_confirmations}
                if payload is None:
                    adapter.apply()
                else:
                    adapter.apply(payload)
                self._mark_relay_coordinated_applied(name)
                applied.append(name)

            # The relay's durable draft includes operation-journal state. It
            # is committed only after model materialization has reached disk,
            # so a remote create cannot be lost if the app exits now.
            if relay_dependency_only:
                # The relay domain took part — it resolved the keys this edit
                # binds — but it committed no journal work, so its own pending
                # operations stay exactly as they were, retryable from the
                # relay's own surfaces.
                applied.append("relay_accounts")
            else:
                commit()
                relay_locally_applied = True
                self._mark_relay_coordinated_applied("relay_accounts")
                applied.append("relay_accounts")

            if provider_locally_applied:
                self._schedule_reload_for_applied_domains(
                    ("providers_models",),
                    runtime_document_before,
                )

            destructive = [] if relay_dependency_only else [
                operation
                for operation in operations
                if isinstance(operation, Mapping)
                and operation.get("kind") == "api_key_delete"
                and operation.get("state", operation.get("status", "staged")) == "staged"
            ]
            destructive_failed = False
            if destructive:
                # Local configuration is now independent of every key marked
                # for removal; a failed deletion remains journaled for retry.
                remote_boundary_crossed = True
                try:
                    execute(prepared, phase="destructive")
                except Exception:
                    if not provider_locally_applied:
                        raise
                    # The document this Apply is about is written, reloaded, and
                    # live; what failed is the relay's own remote cleanup, and
                    # its journal keeps it for the next attempt.  Reporting the
                    # whole Apply as failed while the model was already serving
                    # is the lie that made a ＋ which had landed read as
                    # 更改未生效.
                    destructive_failed = True
            if relay_dependency_only:
                # This edit carries no relay work of its own, so the journal's
                # remaining phases are not part of its result: every pending
                # operation stays exactly as the user left it, retryable from
                # the relay's own surfaces.
                issue_count = 0
                pending = 0
            else:
                reconciled_after_delete = reconcile(prepared, phase="destructive")
                finalize_result = finalize()
                issue_count = (
                    self._relay_public_issue_count(reconciled_after_delete)
                    + self._relay_public_issue_count(finalize_result)
                    + (1 if destructive_failed else 0)
                )
                pending = self._relay_operation_count(relay)
            if issue_count or pending:
                # The local relay document is authoritative for completed
                # work, while outstanding journal entries keep this domain
                # dirty and retryable.  A dependency-only Apply owns none of
                # that work, so it must not re-dirty the relay either: doing so
                # would put relay_accounts back into the next edit's domain
                # list and drag a model edit straight back into the station's
                # backlog.
                if not relay_dependency_only:
                    self._drafts["relay_accounts"]["dirty"] = True
                self._drafts["relay_accounts"]["validation"] = {"valid": True, "issues": []}
                self._advance_revision()
                self._persist_metadata()
                self._request_emit()
                return {
                    "revision": self._revision,
                    "applied": False,
                    "status": "partial",
                    "domains": applied,
                    "completed_operations": max(operation_total - pending, 0),
                    "pending_operations": pending,
                    "issues": self._relay_apply_issues(issue_count),
                }

            if not relay_dependency_only:
                self._mark_relay_coordinated_applied("relay_accounts")
            self._advance_revision()
            self._persist_metadata()
        except Exception as exc:
            if not remote_boundary_crossed:
                rollback_failed = False
                try:
                    restore_local_state()
                except Exception:
                    rollback_failed = True
                if rollback_failed:
                    raise CoreError("apply_failed", "Settings could not be rolled back") from None
                if isinstance(exc, CoreError):
                    raise
                self._mark_refused_draft(transaction_adapters)
                raise CoreError("apply_failed", safe_exception_message(exc)) from None

            # Remote work may have succeeded even if the network connection
            # timed out. Do not restore its operation journal or local files
            # that have already committed; leave a visible, retryable partial
            # state instead.
            if provider_locally_applied:
                self._mark_relay_coordinated_applied("providers_models")
            if relay_locally_applied:
                self._mark_relay_coordinated_applied("relay_accounts")
            else:
                self._drafts["relay_accounts"]["dirty"] = True
                self._drafts["relay_accounts"]["validation"] = {"valid": True, "issues": []}
            pending = self._relay_operation_count(relay)
            self._advance_revision()
            self._persist_metadata()
            self._request_emit()
            return {
                "revision": self._revision,
                "applied": False,
                "status": "partial",
                "domains": applied,
                "completed_operations": max(operation_total - pending, 0),
                "pending_operations": pending,
                "issues": self._relay_apply_issues(1),
            }

        self._request_emit()
        return {
            "revision": self._revision,
            "applied": True,
            "status": "applied",
            "domains": applied,
            "completed_operations": operation_total,
            "pending_operations": 0,
            "issues": [],
        }

    def _mark_refused_draft(self, adapters: Mapping[str, DomainAdapter]) -> None:
        """Record why a rolled-back draft could not be written, on its own rows.

        A domain that refuses during ``apply`` knows which entry is unusable
        (an order it cannot finish, a name it cannot serve).  That answer never
        reached the window: the Apply rolled back and the strip said 更改未生效,
        so the user was left to find the offending route in a list of dozens.
        Asking each participating domain for its validation after the rollback
        publishes the same coded issues a ``validate`` call would, and the pane
        already points ``alertRowKeys`` at them.

        Runs after the adapters are restored, so it reads the staged draft the
        user still holds rather than the half-written state that refused.
        """

        for name, adapter in adapters.items():
            try:
                summary = _validation_summary(adapter.validate(None))
            except Exception:
                continue
            record = self._drafts.get(name)
            if record is not None and summary.get("valid") is not False:
                # Nothing to point at: the draft validates now, so the refusal
                # was not this row's (a disk change, a confirmation, a remote
                # answer).  Leave the draft's own answer alone.
                continue
            self._drafts.setdefault(name, {})["validation"] = summary

    def _apply_domain_hints(self, domain: str | None, domains: Sequence[str] | None) -> tuple[str, ...]:
        """Which write lanes an Apply may need, from its arguments alone.

        The body can grow its domain set while it runs: a provider draft that
        binds relay material joins the relay transaction, and a runtime or
        provider commit rewrites the Codex MCP entry.  The scope has to declare
        those up front -- a lane discovered halfway through a transaction is a
        lane taken out of order -- so this is deliberately a superset.  An
        argument this cannot read (an unknown domain, an empty list) declares
        everything and lets the body answer with its own error.
        """

        if domain is None and domains is None:
            return tuple(self._domain_names())
        try:
            if domains is not None:
                if isinstance(domains, (str, bytes, bytearray)) or not isinstance(domains, Sequence) or not domains:
                    return tuple(self._domain_names())
                names = {_canonical_domain(name) for name in domains}
            else:
                names = {_canonical_domain(domain)}
        except CoreError:
            return tuple(self._domain_names())
        if names & {"providers_models", "relay_accounts"}:
            names |= {"providers_models", "relay_accounts"}
        if names & {"providers_models", "runtime", "codex"}:
            names.add("codex")
        return tuple(sorted(names))

    def apply(
        self,
        domain: str | None = None,
        *,
        domains: Sequence[str] | None = None,
        revision: int | None = None,
        confirmation: str | Sequence[str] | None = None,
    ) -> dict[str, Any]:
        should_emit = False
        # The commit's own restart decision asks whether the proxy is running,
        # and that answer must be the one observed now, not the row an earlier
        # commit happened to leave behind.  Probe it here -- outside every write
        # lane, so a booting controller costs this call its own probe budget and
        # not a hold on the domains every window edits.
        self._service_row_is_live()
        # The lanes this Apply declares before it starts.  A one-section Apply
        # takes that section's lane plus the domains a coordinated write can
        # pull in (the relay's key material for a provider draft, the other
        # half of the relay transaction), and the Codex MCP entry that follows
        # a runtime or provider commit.  A full Apply -- every dirty draft --
        # is the cross-domain case and names everything.
        with self._domain_scope(*self._apply_domain_hints(domain, domains)):
            # The caller's revision names the projection it read its edit from.
            # A refresh *inside* this Apply can advance the shared revision by
            # adopting a clean draft whose file changed since that read, and
            # that bump is not something the caller did or could see.  Compare
            # against the revision this Apply started from: a caller that was
            # already stale when it arrived still conflicts, while one whose
            # only mismatch is the reload this call just performed does not.
            revision_before_refresh = self._revision
            self._refresh_external_disk_state()
            self._check_revision_before_refresh(revision, revision_before_refresh)
            if domain is not None and domains is not None:
                raise CoreError("invalid_domain", "Choose either one domain or a domain list")
            if domains is not None:
                if not isinstance(domains, Sequence) or isinstance(domains, (str, bytes, bytearray)) or not domains:
                    raise CoreError("invalid_domain", "Choose at least one settings domain")
                names = list(dict.fromkeys(_canonical_domain(name) for name in domains))
            else:
                names = [_canonical_domain(domain)] if domain is not None else [name for name, meta in self._drafts.items() if meta.get("dirty")]
            if not names:
                return {
                    "revision": self._revision,
                    "applied": True,
                    "status": "applied",
                    "domains": [],
                    "completed_operations": 0,
                    "pending_operations": 0,
                    "issues": [],
                }

            # A provider draft containing relay-sourced ProviderKeys must
            # resolve them against the relay domain in the same Apply.
            # Conversely, a relay draft that can affect those keys must bring
            # the provider domain into the coordinated transaction.
            # Independent providers keep the ordinary local-only path.
            relay_candidate = self._domains.get("relay_accounts")
            provider_candidate = self._domains.get("providers_models")
            relay_coordinator_available = callable(getattr(relay_candidate, "prepare_apply", None))
            relay_dependency_only = False
            dependency_summary = getattr(provider_candidate, "dependency_summary", None)
            linked_provider_key_count = 0
            if callable(dependency_summary):
                try:
                    dependency_state = dependency_summary()
                except Exception:
                    dependency_state = None
                if isinstance(dependency_state, Mapping):
                    count = dependency_state.get("provider_key_count")
                    if type(count) is int and count > 0:
                        linked_provider_key_count = count
            # A relay edit can *empty* the provider draft before this point: the
            # dependency policy runs while the relay action is dispatched, and
            # retiring the last linked key leaves nothing for the summary above
            # to count.  The baseline is what asks whether this Apply has relay
            # work at all, so a provider draft the relay just changed is still
            # carried into the coordinated transaction — reading only the draft
            # left ``providers_models`` out of it, and the released slot was
            # never written to disk: the document kept the dangling key while
            # the pane read it from memory as gone.
            provider_draft_bound_relay = linked_provider_key_count > 0 or (
                provider_candidate is not None
                and _relay_binding_projection(self._baselines.get("providers_models"))
            )
            if relay_coordinator_available and provider_draft_bound_relay:
                if "providers_models" in names and "relay_accounts" not in names:
                    # Only a draft that actually binds relay material has relay
                    # work to do.  A plain local edit — a new model on an
                    # independent provider, a rename, an order — applies on its
                    # own: dragging it into the relay transaction would report
                    # the relay's own backlog (a pending key create, an expired
                    # session) as a failure of an edit that has nothing to do
                    # with the relay.
                    baseline = self._baselines.get("providers_models")
                    if baseline is None or _relay_binding_projection(
                        self._adapter_draft_state("providers_models")
                    ) != _relay_binding_projection(baseline):
                        names.append("relay_accounts")
                        # The relay is a *dependency* of this edit, not the
                        # user's own relay work: the edit needs the key it
                        # binds resolved and nothing more.  The station's
                        # session and its unfinished operations belong to the
                        # relay domain's own Apply, so this one never validates
                        # them, never runs the journal, and never reports the
                        # backlog against an edit that did not ask about it.
                        relay_dependency_only = True
                    else:
                        relay_coordinator_available = False
                elif "relay_accounts" in names and "providers_models" not in names:
                    names.append("providers_models")
            adapters: dict[str, DomainAdapter] = {}
            for name in names:
                adapter = self._domains.get(name)
                if adapter is None:
                    raise DomainNotFound(name)
                adapters[name] = adapter

            confirm_codes: list[str] = []
            if isinstance(confirmation, str):
                confirm_codes = [confirmation]
            elif isinstance(confirmation, Sequence) and not isinstance(confirmation, (bytes, bytearray, str)):
                confirm_codes = [str(item) for item in confirmation]
            overwrite_codes = {
                f"overwrite_external_{name}"
                for name in names
                if self._disk.get(name, {}).get("changed")
            }
            missing_overwrite = sorted(overwrite_codes.difference(confirm_codes))
            if missing_overwrite:
                # No persistence checkpoint or rollback is needed: nothing in
                # this Apply attempt has written to disk. In particular, never
                # restore an older checkpoint over the newly detected file.
                raise ConfirmationNeeded(missing_overwrite)

            if "relay_accounts" in adapters and relay_coordinator_available:
                return self._apply_relay_coordinated(
                    names,
                    adapters,
                    confirm_codes,
                    overwrite_codes,
                    relay_dependency_only=relay_dependency_only,
                )

            if "runtime" in names and len(names) > 1:
                # The proxy key is a line of the provider document, and that
                # document is written by the provider domain from the copy its
                # own draft loaded.  Restating the key last means a coordinated
                # commit ends with the key this commit states, instead of with
                # whichever draft happened to write the file.
                names = [name for name in names if name != "runtime"] + ["runtime"]
            transaction_adapters = dict(adapters)
            adapter_checkpoints = {
                name: _checkpoint_adapter(adapter, error_code="apply_failed")
                for name, adapter in transaction_adapters.items()
            }
            # Read before anything commits: the runtime document still holds the
            # bytes the running proxy was handed, so this is what the proxy is
            # serving now.
            runtime_document_before = self._runtime_document_fingerprint()
            core_checkpoint = {
                "revision": self._revision,
                "drafts": copy.deepcopy(self._drafts),
                "last_actions": copy.deepcopy(self._last_actions),
                "baselines": copy.deepcopy(self._baselines),
                "disk": copy.deepcopy(self._disk),
                "disk_identities": copy.deepcopy(self._disk_identities),
            }
            persistence_paths = [
                path
                for adapter in transaction_adapters.values()
                for path in _adapter_persistence_paths(adapter)
            ]
            if self._metadata_store is not None:
                persistence_paths.append(self._metadata_store.path)
            file_checkpoints = _checkpoint_files(persistence_paths)
            applied: list[str] = []
            try:
                for name in names:
                    if f"overwrite_external_{name}" not in overwrite_codes:
                        continue
                    rebase = getattr(adapters[name], "rebase_external_disk", None)
                    if not callable(rebase):
                        raise CoreError("apply_failed", "Settings changed on disk; choose the disk version")
                    rebase()
                    self._disk[name]["changed"] = False
                    self._disk[name]["keep_draft"] = True
                # Validation is complete before any persistence boundary is
                # crossed, so a bad later section cannot partially apply an
                # earlier one.
                #
                # A domain that also validates its own draft while applying is
                # not validated twice: it answers for the same staged draft,
                # and its Apply re-checks it before it writes anything either
                # way.  ``_validates_on_apply`` names those domains, so a new
                # one cannot skip this pass by accident.
                for name in names:
                    if name in _VALIDATES_ON_APPLY:
                        continue
                    if not self.validate(name)["valid"]:
                        raise CoreError("validation_failed", "Fix the validation errors")
                for name in names:
                    adapter = adapters[name]
                    domain_confirmations = list(confirm_codes)
                    if name == "claude" and any(
                        item in {"accepted", "claude-risk-confirmed"} for item in domain_confirmations
                    ):
                        # The UI confirmation acknowledges the complete risk
                        # summary. The Claude adapter still computes and checks
                        # the exact current codes.
                        try:
                            from .domains.claude import RISK_CONFIRMATION_CODES
                        except Exception:
                            RISK_CONFIRMATION_CODES = ()
                        domain_confirmations = [str(item) for item in RISK_CONFIRMATION_CODES]
                    payload: object | None = None
                    if domain_confirmations:
                        payload = {"confirm_risks": domain_confirmations, "confirmation": domain_confirmations}
                    if payload is None:
                        adapter.apply()
                    else:
                        adapter.apply(payload)
                    self._baselines[name] = self._adapter_draft_state(name)
                    self._disk[name] = {
                        "changed": False,
                        "generation": int(self._disk.get(name, {}).get("generation", 0)),
                        "keep_draft": False,
                    }
                    self._mark_domain(name, dirty=False, validation={"valid": True, "issues": []}, base_revision=self._revision + 1)
                    applied.append(name)
                if "providers_models" in applied or "runtime" in applied:
                    # The restart takes seconds; hand it to the background so
                    # the committed edit stays fast and the next request is
                    # not stuck behind the Core lock.  Only a commit that
                    # changed the document the proxy loads needs one.
                    self._schedule_reload_for_applied_domains(applied, runtime_document_before)
                # Codex's own MCP list follows the image-context switch: the two
                # are written together, and the proxy restart above is unrelated.
                self._sync_codex_image_context_mcp(applied)
                self._advance_revision()
                self._persist_metadata()
            except Exception as exc:
                rollback_failed = False
                try:
                    _restore_files(file_checkpoints)
                    for name, adapter in transaction_adapters.items():
                        _restore_adapter(adapter, adapter_checkpoints[name])
                except Exception:
                    rollback_failed = True
                self._restore_revision(core_checkpoint["revision"])
                self._drafts = core_checkpoint["drafts"]
                self._last_actions = core_checkpoint["last_actions"]
                self._baselines = core_checkpoint["baselines"]
                self._disk = core_checkpoint["disk"]
                self._disk_identities = core_checkpoint["disk_identities"]
                if rollback_failed:
                    raise CoreError("apply_failed", "Settings could not be rolled back") from None
                codes = getattr(exc, "codes", None)
                if isinstance(codes, Sequence) and not isinstance(codes, (str, bytes, bytearray)):
                    raise ConfirmationNeeded(str(item) for item in codes) from None
                if isinstance(exc, CoreError):
                    raise
                # A domain that refuses its own draft says so with rows, not
                # with a sentence: the pane marks the route or key that refuses
                # (``alertRowKeys``) and the strip states the outcome once.  A
                # bare rollback reported 更改未生效 with nothing marked, so the
                # row the user had just touched — the ＋ that started this
                # write — looked like the one that failed.
                self._mark_refused_draft(transaction_adapters)
                raise CoreError("apply_failed", safe_exception_message(exc)) from None
            should_emit = True
            response = {
                "revision": self._revision,
                "applied": True,
                "status": "applied",
                "domains": applied,
                "completed_operations": 0,
                "pending_operations": 0,
                "issues": [],
            }
        if should_emit:
            self._request_emit()
        return response

    def _sync_codex_image_context_mcp(self, applied: Sequence[str]) -> None:
        """Keep Codex's fetch server in step with the image-context switch.

        The switch is a runtime setting (that is where an operator turns the
        pass on) and the server entry is a Codex document, so the two live in
        two domains and must move together: a pass that is on with no fetch tool
        leaves the model holding placeholders it can never open, and a fetch
        tool with the pass off is a tool that can never answer.  The caller
        holds ``self._lock``.
        """

        if "codex" not in applied and "runtime" not in applied:
            return
        adapter = self._domains.get("codex")
        sync = getattr(adapter, "sync_image_context_mcp", None)
        if not callable(sync):
            return
        try:
            changed = bool(sync())
        except Exception:
            # A Codex document this app cannot rewrite must not fail the commit
            # that just succeeded: the pass itself is served by the proxy.
            return
        if not changed or adapter is None:
            return
        self._baselines["codex"] = self._adapter_draft_state("codex")
        self._disk["codex"] = {
            "changed": False,
            "generation": int(self._disk.get("codex", {}).get("generation", 0)),
            "keep_draft": False,
        }
        self._disk_identities["codex"] = self._external_disk_identity(adapter)
        self._mark_domain(
            "codex",
            dirty=False,
            validation={"valid": True, "issues": []},
            base_revision=self._revision + 1,
        )

    def reload(self, domain: str | None = None, *, revision: int | None = None) -> dict[str, Any]:
        should_emit = False
        names = [_canonical_domain(domain)] if domain is not None else self._domain_names()
        with self._domain_scope(*names):
            self._check_revision(revision)
            for name in names:
                adapter = self._domains.get(name)
                if adapter is None:
                    raise DomainNotFound(name)
                try:
                    adapter.reload()
                except Exception as exc:
                    raise CoreError("reload_failed", safe_exception_message(exc)) from None
                self._baselines[name] = self._adapter_draft_state(name)
                self._mark_domain(name, dirty=False, validation={"valid": True, "issues": []}, base_revision=self._revision + 1)
                self._disk[name] = {
                    "changed": False,
                    "generation": int(self._disk.get(name, {}).get("generation", 0)),
                    "keep_draft": False,
                }
            self._advance_revision()
            self._persist_metadata()
            should_emit = True
            response = {"revision": self._revision}
        if should_emit:
            self._request_emit()
        return response

    def probe(self, payload: Mapping[str, Any] | None = None, *, domain: str | None = None) -> dict[str, Any]:
        name = _canonical_domain(domain or "providers_models")
        data = dict(payload or {})
        with self._domain_scope(name):
            adapter = self._domains.get(name)
            if adapter is None:
                return {"ok": False, "protocols": [], "detail": "Probe is unavailable"}
            prepare = getattr(adapter, "prepare_probe", None)
            perform = getattr(adapter, "perform_probe", None)
            commit = getattr(adapter, "commit_probe", None)
            if name == "providers_models" and all(callable(method) for method in (prepare, perform, commit)):
                try:
                    prepared = prepare(data)
                except Exception as exc:
                    return {"ok": False, "protocols": [], "detail": safe_exception_message(exc)}
            else:
                prepared = None

        if prepared is not None:
            try:
                value = perform(prepared)
            except Exception as exc:
                return {"ok": False, "protocols": [], "detail": safe_exception_message(exc)}
            with self._domain_scope(name):
                if self._domains.get(name) is not adapter:
                    return {"ok": False, "protocols": [], "detail": "Probe is unavailable"}
                try:
                    value, changed = commit(prepared, value)
                except Exception as exc:
                    return {"ok": False, "protocols": [], "detail": safe_exception_message(exc)}
                should_emit = False
                if changed:
                    self._advance_revision()
                    self._persist_metadata()
                    should_emit = True
                result = _safe_public(value if isinstance(value, Mapping) else {})
                if not isinstance(result, dict):
                    result = {}
                result.setdefault("ok", False)
                result.setdefault("protocols", [])
            if should_emit:
                self._request_emit()
            return result

        should_emit = False
        with self._domain_scope(name):
            method = getattr(adapter, "probe", None)
            if not callable(method):
                return {"ok": False, "protocols": [], "detail": "Probe is unavailable"}
            before = self._adapter_draft_state(name)
            try:
                value = method(data)
            except Exception as exc:
                return {"ok": False, "protocols": [], "detail": safe_exception_message(exc)}
            after = self._adapter_draft_state(name)
            if after != before:
                dirty = after != self._baselines.get(name)
                self._advance_revision()
                self._mark_domain(
                    name,
                    dirty=dirty,
                    base_revision=(
                        self._drafts.get(name, {}).get("base_revision", self._revision)
                        if dirty
                        else self._revision
                    ),
                )
                self._persist_metadata()
                should_emit = True
            result = _safe_public(value if isinstance(value, Mapping) else {})
            if not isinstance(result, dict):
                result = {}
            result.setdefault("ok", False)
            result.setdefault("protocols", [])
            if name == "webdav":
                handler = self._service_handlers.get("status")
                if handler is not None:
                    try:
                        service_result = handler("status")
                    except Exception:
                        service_result = None
                    if isinstance(service_result, Mapping):
                        self._set_service_from_result(service_result, increment=False)
        if should_emit:
            self._request_emit()
        return result

    def export(self, sections: Sequence[str], *, destination_token: str | None = None) -> dict[str, Any]:
        if isinstance(sections, (str, bytes, bytearray)) or not isinstance(sections, Sequence) or not sections:
            raise CoreError("invalid_sections", "Choose at least one configuration section")
        names = [_canonical_domain(section) for section in sections]
        if destination_token is not None and len(names) == 1 and names[0] in {"providers_models", "runtime"}:
            path = self.file_capabilities.resolve(destination_token, "export")
            name = names[0]
            try:
                adapter = self._domains.get(name)
                if adapter is None:
                    raise DomainNotFound(name)
                if any(_same_path(path, source) for source in _adapter_persistence_paths(adapter)):
                    raise CoreError("export_failed", "Choose a file outside the active settings files")
                method = getattr(adapter, "export", None)
                if not callable(method):
                    raise CoreError("export_failed", "The selected settings cannot be exported")
                parameters = inspect.signature(method).parameters
                exported = method(include_sensitive=True) if "include_sensitive" in parameters else method()
                if not isinstance(exported, Mapping):
                    raise CoreError("export_failed", "The selected settings cannot be exported")
                settings = copy.deepcopy(dict(exported))
                settings.pop("domain", None)
                atomic_write_json(
                    path,
                    {
                        "format": DOMAIN_FILE_FORMAT,
                        "version": DOMAIN_FILE_VERSION,
                        "domain": name,
                        "settings": settings,
                    },
                )
            except CoreError:
                raise
            except Exception as exc:
                raise CoreError("export_failed", safe_exception_message(exc)) from None
            return {
                "revision": self._revision,
                "section_count": 1,
                "sections": names,
            }
        if destination_token is not None and set(names).issubset({"providers_models", "runtime"}):
            path = self.file_capabilities.resolve(destination_token, "export")
            try:
                from .domains.providers_models import ProvidersModelsDomain
                from .domains.runtime import RuntimeSettingsDomain
                from .operations import ConfigurationPackageAdapter

                provider = self._domains.get("providers_models")
                runtime = self._domains.get("runtime")
                config_path = getattr(provider, "config_path", None)
                settings_path = getattr(runtime, "settings_path", None)
                if not isinstance(provider, ProvidersModelsDomain) or not isinstance(runtime, RuntimeSettingsDomain):
                    raise CoreError("export_failed", "Configuration package sources are unavailable")
                adapter = ConfigurationPackageAdapter(
                    config_path=Path(config_path),
                    settings_path=Path(settings_path),
                )
                written = adapter.export(sections=names, destination=path)
            except CoreError:
                raise
            except Exception as exc:
                raise CoreError("export_failed", safe_exception_message(exc)) from None
            return {
                "revision": self._revision,
                "section_count": len(written),
                "sections": names,
            }
        package_sections: dict[str, Any] = {}
        safe_package_sections: dict[str, Any] = {}
        with self._domain_scope(*names):
            for name in names:
                adapter = self._domains.get(name)
                if adapter is None:
                    raise DomainNotFound(name)
                method = getattr(adapter, "export", None)
                try:
                    if callable(method):
                        # A trusted, explicitly selected destination may carry
                        # credentials, but the response sent back to RN must
                        # remain redacted.  Domain adapters that support this
                        # opt in with ``include_sensitive=True``; other
                        # adapters keep their safe no-argument export.
                        try:
                            parameters = inspect.signature(method).parameters
                        except (TypeError, ValueError):
                            parameters = {}
                        value = method(include_sensitive=True) if "include_sensitive" in parameters else method()
                    else:
                        value = adapter.snapshot()
                except Exception as exc:
                    raise CoreError("export_failed", safe_exception_message(exc)) from None
                package_sections[name] = copy.deepcopy(value)
                safe_package_sections[name] = _safe_public(value)
            package = {"format": PACKAGE_FORMAT, "version": PACKAGE_VERSION, "sections": package_sections}
            safe_package = {"format": PACKAGE_FORMAT, "version": PACKAGE_VERSION, "sections": safe_package_sections}
            result: dict[str, Any] = {"revision": self._revision, "section_count": len(package_sections), "sections": list(package_sections)}
            if destination_token is not None:
                path = self.file_capabilities.resolve(destination_token, "export")
                try:
                    AtomicJSONStore(path).write(package)
                except PersistenceError as exc:
                    raise CoreError("export_failed", safe_exception_message(exc)) from None
            # ``package`` is retained for previews/tests, but never contains
            # the raw package values that may have been written to disk.
            result["package"] = safe_package
            return result

    def _read_import_package(self, path: Path) -> Mapping[str, Any]:
        """Parse one selected file without using any preselected UI sections."""

        try:
            from .operations import ConfigurationPackageAdapter

            try:
                selected_json: dict[str, Any] | None = read_json(path)
            except PersistenceError:
                selected_json = None

            if selected_json is not None and selected_json.get("format") == DOMAIN_FILE_FORMAT:
                if set(selected_json) != {"format", "version", "domain", "settings"}:
                    raise CoreError("invalid_package", "Settings file has an unsupported shape")
                name = _canonical_domain(selected_json.get("domain"))
                if (
                    selected_json.get("version") != DOMAIN_FILE_VERSION
                    or name not in {"providers_models", "runtime"}
                    or not isinstance(selected_json.get("settings"), Mapping)
                ):
                    raise CoreError("invalid_package", "Settings file version is unsupported")
                return {
                    "format": PACKAGE_FORMAT,
                    "version": PACKAGE_VERSION,
                    "sections": {name: copy.deepcopy(dict(selected_json["settings"]))},
                }

            if selected_json is not None and selected_json.get("format") == PACKAGE_FORMAT:
                return selected_json

            if (
                selected_json is not None
                and selected_json.get("format") == "young-router-configuration-package"
            ):
                from .domains.providers_models import ProvidersModelsDomain
                from .domains.runtime import RuntimeSettingsDomain

                provider = self._domains.get("providers_models")
                runtime = self._domains.get("runtime")
                if not isinstance(provider, ProvidersModelsDomain) or not isinstance(runtime, RuntimeSettingsDomain):
                    raise CoreError("invalid_package", "Configuration package sources are unavailable")
                adapter = ConfigurationPackageAdapter(
                    config_path=Path(provider.config_path),
                    settings_path=Path(runtime.settings_path),
                )
                loaded = adapter.load(path)
                return {
                    "format": PACKAGE_FORMAT,
                    "version": PACKAGE_VERSION,
                    "sections": ConfigurationPackageAdapter.core_sections(loaded, None),
                }

            # All other supported JSON/TOML/YAML/SQL shapes are existing
            # provider/model imports. Detection happens only after the native
            # picker has returned the file; no checkbox hint influences it.
            from . import external_provider_import

            imported = external_provider_import.import_explicit(path)
            providers = imported.get("providers") if isinstance(imported, Mapping) else None
            if not isinstance(providers, list):
                raise CoreError("invalid_package", "Provider configuration could not be imported")
            return {
                "format": PACKAGE_FORMAT,
                "version": PACKAGE_VERSION,
                "sections": {"providers_models": {"providers": copy.deepcopy(providers)}},
            }
        except CoreError:
            raise
        except (PersistenceError, ValueError) as exc:
            raise CoreError("invalid_package", safe_exception_message(exc)) from None
        except Exception as exc:
            raise CoreError("invalid_package", safe_exception_message(exc)) from None

    def _validated_import_package(
        self, package: Mapping[str, Any]
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        if not isinstance(package, Mapping) or set(package) != {"format", "version", "sections"}:
            raise CoreError("invalid_package", "Configuration package has an unsupported shape")
        if (
            package.get("format") != PACKAGE_FORMAT
            or package.get("version") != PACKAGE_VERSION
            or not isinstance(package.get("sections"), Mapping)
        ):
            raise CoreError("invalid_package", "Configuration package version is unsupported")
        raw_sections = package["sections"]
        if not raw_sections:
            raise CoreError("invalid_package", "Configuration package does not contain settings")
        normalized: dict[str, Any] = {}
        for raw_name, payload in raw_sections.items():
            name = _canonical_domain(raw_name)
            if name != raw_name or name not in IMPORTABLE_DOMAINS:
                raise CoreError("invalid_package", "Configuration package contains an unsupported section")
            if name in normalized:
                raise CoreError("invalid_package", "Configuration package contains a repeated section")
            if self._domains.get(name) is None:
                raise DomainNotFound(name)
            normalized[name] = copy.deepcopy(payload)
        return (
            {"format": PACKAGE_FORMAT, "version": PACKAGE_VERSION, "sections": normalized},
            tuple(normalized),
        )

    def prepare_import(
        self,
        *,
        source_token: str,
        revision: int | None = None,
    ) -> PreparedImport:
        """Consume a picker capability and prepare a non-mutating import plan."""

        with self._lock:
            self._check_revision(revision)
        path = self.file_capabilities.resolve(source_token, "import")
        package, detected = self._validated_import_package(self._read_import_package(path))
        with self._lock:
            # Parsing may be expensive. Bind the plan to the exact state whose
            # existing drafts are described by this preview.
            self._check_revision(revision)
            preview = {
                name: {
                    "available": True,
                    "will_replace_draft": bool(self._drafts.get(name, {}).get("dirty")),
                }
                for name in detected
            }
            return PreparedImport(
                package=package,
                detected_sections=detected,
                preview=preview,
                revision=self._revision,
            )

    def import_package(
        self,
        *,
        package: Mapping[str, Any],
        sections: Sequence[str] | None = None,
        revision: int | None = None,
    ) -> dict[str, Any]:
        """Stage a package previously parsed by :meth:`prepare_import`.

        File capabilities are accepted only by ``prepare_import``.  Import
        execution receives the parsed package held by the session-bound IPC
        plan, so a caller cannot bypass preview detection or submit another
        path at staging time.
        """

        package, detected = self._validated_import_package(package)
        if sections is None:
            selected = list(detected)
        else:
            if isinstance(sections, (str, bytes, bytearray)) or not isinstance(sections, Sequence) or not sections:
                raise CoreError("invalid_sections", "Choose at least one detected configuration section")
            requested = list(dict.fromkeys(_canonical_domain(section) for section in sections))
            if len(requested) != len(sections):
                raise CoreError("invalid_sections", "Choose only sections detected in the selected file")
            if not set(requested).issubset(detected):
                raise CoreError("invalid_sections", "Choose only sections detected in the selected file")
            selected = requested
        raw_sections = package["sections"]
        should_emit = False
        with self._domain_scope(*selected):
            self._check_revision(revision)
            pending: list[tuple[str, DomainAdapter, object]] = []
            preview: dict[str, dict[str, bool]] = {}
            for name in selected:
                adapter = self._domains.get(name)
                if adapter is None:
                    raise DomainNotFound(name)
                pending.append((name, adapter, raw_sections[name]))
                preview[name] = {
                    "available": True,
                    "will_replace_draft": bool(self._drafts.get(name, {}).get("dirty")),
                }

            adapter_checkpoints = {
                name: _checkpoint_adapter(adapter)
                for name, adapter, _payload in pending
            }
            core_checkpoint = {
                "revision": self._revision,
                "drafts": copy.deepcopy(self._drafts),
                "last_actions": copy.deepcopy(self._last_actions),
                "baselines": copy.deepcopy(self._baselines),
            }
            staged: list[str] = []
            importing = True
            try:
                for name, adapter, payload in pending:
                    importer = getattr(adapter, "import_package", None)
                    if callable(importer):
                        importer(payload)
                    elif name == "claude" and isinstance(payload, Mapping) and isinstance(payload.get("settings"), Mapping):
                        adapter.dispatch("patch", payload["settings"])
                    elif name == "language" and isinstance(payload, Mapping):
                        adapter.dispatch("set", {"language": payload.get("choice", payload.get("language"))})
                    else:
                        adapter.dispatch("replace", payload)
                    staged.append(name)
                importing = False
                for name in staged:
                    dirty = self._adapter_draft_state(name) != self._baselines.get(name)
                    self._mark_domain(
                        name,
                        dirty=dirty,
                        base_revision=(
                            self._drafts.get(name, {}).get("base_revision", self._revision)
                            if dirty
                            else self._revision
                        ),
                    )
                self._advance_revision()
                self._persist_metadata()
            except Exception as exc:
                for name, adapter, _payload in pending:
                    _restore_adapter(adapter, adapter_checkpoints[name])
                self._restore_revision(core_checkpoint["revision"])
                self._drafts = core_checkpoint["drafts"]
                self._last_actions = core_checkpoint["last_actions"]
                self._baselines = core_checkpoint["baselines"]
                if importing:
                    raise CoreError("import_failed", safe_exception_message(exc)) from None
                raise

            should_emit = True
            response = {"revision": self._revision, "draft_domains": staged, "preview": preview}
        if should_emit:
            self._request_emit()
        return response

    def announce_web_endpoint(self, address: str, port: int) -> None:
        """State where this Core answers the browser page and its API.

        The proxy owns the published port and forwards ``/ui`` and ``/ipc``
        here; it reads that address from its own environment, which Core can
        only write once its listener is up.  A store built without a managed
        proxy (a test, a CLI probe) simply has no handler to tell.
        """

        handler = self._service_handlers.get("set_web_endpoint")
        if callable(handler):
            handler(address, port)

    def set_service_status(
        self,
        state: str,
        *,
        detail: str | None = None,
        pid: int | None = None,
        port: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if state not in SERVICE_STATES:
                raise CoreError("invalid_service_state", "Service state is invalid")
            self._service = {"state": state}
            if detail:
                self._service["detail"] = safe_error_message(detail)
            if type(pid) is int and pid > 0:
                self._service["pid"] = pid
            if state == "running" and type(port) is int and 1 <= port <= 65535:
                self._service["port"] = port
            self._advance_revision()
            self._persist_metadata()
            result = copy.deepcopy(self._service)
        self._request_emit()
        return result

__all__ = [
    "CORE_METADATA_VERSION",
    "CoreError",
    "CoreStore",
    "ConfirmationNeeded",
    "DomainAdapter",
    "DomainNotFound",
    "FileCapability",
    "FileCapabilityRegistry",
    "LOG_TABS",
    "MemoryDomain",
    "PACKAGE_FORMAT",
    "PACKAGE_VERSION",
    "RevisionConflict",
    "SERVICE_INTENTS",
    "SERVICE_PROXY_OPERATIONS",
    "SERVICE_RECOVERY_PATIENCE_SECONDS",
    "SERVICE_RECOVERY_RETRY_SECONDS",
    "SERVICE_STATES",
    "SERVICE_TRANSITION_OPERATIONS",
]
