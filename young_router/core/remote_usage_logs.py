#!/usr/bin/env python3
"""Fetch the user's own live usage rows from compatible relay control planes.

The reader is deliberately opt-in by response shape: it tries a small set of
documented, read-only endpoints against configured enabled credentials and
emits a sanitized text feed.  It never guesses hosts, persists remote data, or
prints a credential, URL, request body, IP address, or upstream response text.

A relay dashboard's access token is short-lived (a couple of hours) and the
station rotates it on every refresh.  Persisting a copy therefore guarantees a
stale read: the browser keeps itself signed in, while the stored copy expires
and the station starts refusing it.  The token this module uses therefore lives
here, in memory, and is minted from the account's own refresh token on demand —
the same trade the browser makes, minus the disk write.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

from ..config.load import load_config
from young_router.api_base import isolated_http_opener, service_root
from young_router.browser_identity import browser_request_headers


# One target per *account* of a station, not per station: a relay that hosts two
# dashboards (one per login) answers two different usage histories through two
# different keys, and a dropdown that showed the station once could never read
# the second account's rows.  The bound therefore has to clear the accounts a
# user actually configures (a handful of stations times a handful of logins),
# because a station dropped from the list is one the pane cannot select at all.
MAX_TARGETS = 64
MAX_WORKERS = 4
# How many rows one key of one station may contribute. The log view holds up
# to MAX_LINES and states its own limit in the status strip, so a reader cap
# below that would silently drop rows the view was willing to show. It stays
# bounded because the response is third-party and must not grow without limit.
MAX_ROWS_PER_TARGET = 10_000
DEFAULT_TIMEOUT_SECONDS = 4.0
# A relay answers a log page as one JSON document, and a station's own page
# size is often 1000 rows: a real, valid answer is routinely several hundred
# kilobytes, so a small cap rejected the busiest keys as if they were broken.
# The bound still exists, because the response is third-party and must not be
# read without limit; it is stated in rows so it tracks what the reader uses.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class UsageConfigError(ValueError):
    """The current LiteLLM configuration could not be read safely."""

# Which outcome a read ended in. The pane states each one in the user's own
# language, so Core answers with a stable code rather than a sentence that only
# ever reads correctly in English.
USAGE_REASON_CONFIG_UNREADABLE = "config_unreadable"
USAGE_REASON_UNKNOWN_SOURCE = "unknown_source"
USAGE_REASON_NO_CREDENTIAL = "no_credential"
USAGE_REASON_UNSUPPORTED = "unsupported"
USAGE_REASON_NO_ROWS = "no_rows"
USAGE_REASON_UNAVAILABLE = "unavailable"
# The station answered, and its answer was "your dashboard login is no longer
# valid".  That is a fact the user can act on — sign in to the relay again —
# and it is never the same as a station that installs no log feed.
USAGE_REASON_LOGIN_EXPIRED = "login_expired"


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _credential_value(value: Any) -> str:
    text = _string(value)
    if not text.startswith("os.environ/"):
        return "" if "\r" in text or "\n" in text else text
    variable = text.removeprefix("os.environ/").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
        return ""
    resolved = os.environ.get(variable, "").strip()
    return "" if "\r" in resolved or "\n" in resolved else resolved


def _provider_keys(
    provider: dict[str, Any],
) -> tuple[dict[str, str], dict[str, str], list[str], dict[str, str], dict[str, str]]:
    """Index a provider's credentials, keeping the account each one belongs to.

    A key's station account is what a usage read is scoped by: the same station
    can hold one dashboard login per person, and each login answers only the
    traffic its own keys served.  The account is therefore read from the relay
    slot the key was minted from (``source``), which is the only place the
    provider document records it without ever repeating a credential.
    """

    by_name: dict[str, str] = {}
    by_id: dict[str, str] = {}
    account_by_name: dict[str, str] = {}
    account_by_id: dict[str, str] = {}
    ordered: list[str] = []
    raw_keys = provider.get("api_keys")
    if not isinstance(raw_keys, list):
        return by_name, by_id, ordered, account_by_name, account_by_id
    for index, item in enumerate(raw_keys, start=1):
        if not isinstance(item, dict):
            continue
        name = _string(item.get("name")) or f"key-{index}"
        value = _credential_value(item.get("value"))
        if not value or name in by_name:
            continue
        by_name[name] = value
        key_id = _string(item.get("id"))
        if key_id:
            by_id[key_id] = value
        account = _key_account(item)
        if account:
            account_by_name[name] = account
            if key_id:
                account_by_id[key_id] = account
        ordered.append(value)
    return by_name, by_id, ordered, account_by_name, account_by_id


def _key_account(item: dict[str, Any]) -> str:
    """The relay account one configured key was minted from, if any.

    Only a key that came from a dashboard login names one.  A key the user
    pasted by hand belongs to no account, and reading it as somebody else's
    would put a station's newest account label on an unrelated credential.
    """

    source = item.get("source")
    if not isinstance(source, dict) or _string(source.get("kind")) != "relay":
        return ""
    return _string(source.get("account_id"))


def _credential_for_model(provider: dict[str, Any], model: dict[str, Any]) -> str:
    direct = _credential_value(model.get("api_key"))
    if direct:
        return direct
    by_name, by_id, ordered, _account_by_name, _account_by_id = _provider_keys(provider)
    # The slot is the key the route uses, and a name only decides when no slot is
    # stated.  A route that states a key this provider does not carry has no
    # usage to read: answering with the provider's default (or its only key)
    # would present another account's numbers as this route's.
    key_id = _string(model.get("provider_key_id"))
    preferred_name = _string(model.get("api_key_name"))
    if key_id:
        return by_id.get(key_id, "")
    if preferred_name:
        return by_name.get(preferred_name, "")
    if "default" in by_name:
        return by_name["default"]
    return ordered[0] if len(ordered) == 1 else ""


@dataclass(frozen=True)
class UsageTarget:
    """One selectable read scope: one account of one relay station.

    The scope is the *account*, not the station.  A relay that hosts several
    dashboard logins keeps each login's traffic apart, so a station-wide read
    answered with whichever account's key was reached first while the others'
    rows were merged into the same list — two people's usage under one name.
    ``provider`` and ``origin`` still name the station (the row's own column),
    and ``account`` names which of its logins the rows belong to.
    """

    provider: str
    api_base: str
    api_key: str
    # The account whose keys these are, so two logins of one station stay two
    # entries.  Empty for a key the user pasted by hand: it belongs to no
    # login, and claiming one would mislabel the rows it reads.
    account: str = ""
    # The same account's stable id, which is what the source handle is derived
    # from.  It is not the label: a user can rename a login in the relay panes,
    # and a selection stored across that rename must still name one account.
    account_id: str = ""
    # The account's other configured credentials, tried in order when the
    # first cannot read the feed.
    fallback_keys: tuple[str, ...] = ()
    # The station family, so a read asks for the endpoint that station installs
    # before falling back to the other shape.
    station_type: str = ""
    # Dashboard headers for this login, when the app holds a session for it.
    # A Sub2API ledger is only readable through the login that owns it, and the
    # session must never leave this module: it is not part of any record, label,
    # or IPC payload.
    session_headers: tuple[tuple[str, str], ...] = ()
    # The login's refresh token, used to mint a current access token on demand.
    # It is a credential like the headers above and stays inside this module:
    # the token a read authorizes with lives in memory and is never read back
    # from (or written to) the stored session.
    refresh_token: str = ""

    def credentials(self) -> tuple[str, ...]:
        return (self.api_key, *self.fallback_keys) if self.api_key else self.fallback_keys

    def headers(self) -> dict[str, str]:
        """The login's own headers, as a fresh mapping per read."""

        return dict(self.session_headers)

    @property
    def origin(self) -> str:
        """The station's own address, as the relay panes state it."""

        return service_root(self.api_base) or self.api_base

    def display(self) -> str:
        """The relay and the account the rows belong to, as the user knows them.

        ``provider`` is the name the user gave the station when they added it
        (``sisct2``), which is what the relay panes list.  The account is
        appended only when the station actually holds more than one, because
        the common case is one login per station and naming it there would
        spend the column on a word the user already assumes.
        """

        return f"{self.provider} · {self.account}" if self.account else self.provider

    @staticmethod
    def family_label(family: str) -> str:
        """The control plane that answered, named the way the panes name it."""

        if not family:
            return ""
        return "Sub2API" if family == "sub2api" else "NewAPI"

    @property
    def source_id(self) -> str:
        """A stable, non-secret handle for one account of one relay station.

        The id names the station *and* the account, never a credential: the
        control-plane root and the account's stable id are hashed together, so
        every route that points at one login resolves to the same entry and
        the React side hands a selection back without ever seeing the API key
        it selects.  A hand-pasted key carries no account, so it falls back to
        the station alone — which is what it is.
        """

        identity = self.origin
        if self.account_id:
            identity = f"{identity}\x1f{self.account_id}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return digest[:16]


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def default_config_path() -> pathlib.Path:
    from ..config_paths import provider_config_path

    return provider_config_path()


def active_usage_targets(path: pathlib.Path) -> list[UsageTarget]:
    """Return one safe read target for each relay account the app knows.

    The scope is the *account*, not the station.  One relay can host several
    dashboard logins (a shared team station, one login per person), and each
    login answers only the traffic its own keys served; grouping by station
    merged those histories under a single name and could never read the second
    account at all.

    The logins come from the relay panes' own account document, not only from
    the keys currently bound into routes.  A login whose keys have not been
    imported yet is still an account this app holds a session for, and it still
    has usage to read — deriving the list from bound routes made that account
    unreachable in the dropdown, which read as "this app only knows one
    account".  A hand-pasted key belongs to no login and keeps the station
    alone, so a station configured without relay links still lists once.
    """
    try:
        payload = load_config(path)
    except Exception as exc:
        raise UsageConfigError("The LiteLLM configuration could not be read.") from exc
    raw_providers = payload.get("providers")
    if not isinstance(raw_providers, list):
        raise UsageConfigError("The LiteLLM configuration has no provider list.")

    # A station's own root names it, and the account a key was minted from
    # names the login inside it.  Several routes that point at one account (its
    # groups, its models) are one relay account to read and one entry to choose.
    order: list[tuple[str, str]] = []
    roots: dict[tuple[str, str], str] = {}
    labels: dict[tuple[str, str], str] = {}
    # The provider name a station was configured under, so an account of that
    # station is named the way every other surface names it.
    provider_names: dict[str, str] = {}
    credentials: dict[tuple[str, str], list[str]] = {}
    # Every provider's routes are collected first, because whether one is a
    # *relay* is decided by the relay panes' own document, not by the provider
    # entry: the panes record a station (or a login) for each relay they created,
    # and a provider they never created has no entry there at all.  A provider
    # pointed straight at an upstream vendor — a hand-pasted key on
    # `api.deepseek.com` — is exactly that, and listing it offered a choice whose
    # every answer was "unsupported".
    accounts = _relay_accounts(path)
    stations = _relay_stations(path)
    station_roots = set(stations) | {
        service_root(_string(account.get("origin")) + "/v1")
        or _string(account.get("origin")).rstrip("/")
        for account in accounts.values()
        if _string(account.get("origin"))
    }
    for raw_provider in raw_providers:
        if not isinstance(raw_provider, dict):
            continue
        provider_name = _string(raw_provider.get("name")) or "Configured relay"
        provider_base = _string(raw_provider.get("api_base"))
        raw_models = raw_provider.get("models")
        if not isinstance(raw_models, list):
            continue
        for raw_model in raw_models:
            if not isinstance(raw_model, dict):
                continue
            api_base = _string(raw_model.get("api_base")) or provider_base
            api_key = _credential_for_model(raw_provider, raw_model)
            root = service_root(api_base)
            if not root:
                continue
            # A route states its key by slot, and the slot is where the
            # account it belongs to is recorded.  A route that names no
            # readable key still contributes its station, because another
            # route of the same station may name one.
            account_id = _account_for_model(raw_provider, raw_model)
            scope = (root, account_id)
            if scope not in roots:
                order.append(scope)
                roots[scope] = api_base
                labels[scope] = provider_name
                provider_names[scope[0]] = provider_name
                credentials[scope] = []
            if api_key and api_key not in credentials[scope]:
                credentials[scope].append(api_key)

    # A login the relay panes hold is an account with usage to read even when
    # no route carries one of its keys yet: it answers through its own session,
    # and leaving it out made the dropdown name only the logins that had already
    # been imported.
    for account_id, account in accounts.items():
        origin = _string(account.get("origin"))
        has_session = bool(account.get("session"))
        if not account_id or not origin or not has_session:
            continue
        scope = (service_root(origin + "/v1") or origin.rstrip("/"), account_id)
        if scope in roots:
            continue
        order.append(scope)
        roots[scope] = origin
        # The name the relay panes show is the provider name the user gave the
        # station.  An account-only entry used the host instead, so the dropdown
        # listed `relay.example.test · tester` beside `relay` for the same
        # station — the same relay under two names, one of them a URL.
        labels[scope] = (
            provider_names.get(scope[0])
            or _station_label(origin)
            or _string(account.get("label"))
            or origin
        )
        credentials[scope] = []

    # Keep only relay stations.  A hand-pasted key on a vendor's own API
    # (`api.deepseek.com`) cannot be read here at all: there is no dashboard and
    # no usage feed.  A station the panes never registered is dropped with it,
    # because the alternative — offering every provider — is what put a vendor in
    # the dropdown.  The relay document is the only authority on which is which.
    order = [scope for scope in order if scope[0] in station_roots]

    targets: list[UsageTarget] = []
    for scope in order:
        root, account_id = scope
        keys = credentials[scope]
        account = accounts.get(account_id, {})
        # Every account is named: the dropdown is where the login is chosen, so
        # leaving its name off the entry that is *about* that login was what made
        # a station with two logins look like a station with one.
        label = _string(account.get("label")) or account_id
        if not keys and not account.get("session"):
            # No readable credential and no dashboard session: there is nothing
            # this entry could ever answer, and offering it would be a choice
            # that always comes back empty.
            continue
        targets.append(
            UsageTarget(
                provider=labels[scope],
                api_base=roots[scope],
                api_key=keys[0] if keys else "",
                account=label,
                account_id=account_id,
                fallback_keys=tuple(keys[1:]),
                station_type=_string(account.get("type"))
                or _string(stations.get(root, "")),
                session_headers=tuple(_session_headers(account).items()),
                refresh_token=_string((account.get("session") or {}).get("refresh_token"))
                if isinstance(account.get("session"), dict)
                else "",
            )
        )
        if len(targets) >= MAX_TARGETS:
            break
    return targets


def _station_label(origin: str) -> str:
    """The relay's own name for a station, as the panes title it."""

    try:
        parsed = urllib.parse.urlsplit(origin)
    except ValueError:
        return ""
    host = parsed.netloc.strip().lower()
    return host[4:] if host.startswith("www.") else host


def _account_for_model(provider: dict[str, Any], model: dict[str, Any]) -> str:
    """The relay account one configured route's credential was minted from."""

    _by_name, by_id, _ordered, by_account_name, by_account_id = _provider_keys(provider)
    key_id = _string(model.get("provider_key_id"))
    if key_id:
        return by_account_id.get(key_id, "")
    preferred_name = _string(model.get("api_key_name"))
    if preferred_name:
        return by_account_name.get(preferred_name, "")
    return ""


def _relay_document(path: pathlib.Path) -> dict[str, Any]:
    """Read the relay panes' own account document, or an empty one."""

    try:
        from ..webdav.core import relay_accounts_path

        document = json.loads(
            relay_accounts_path(path).read_text(encoding="utf-8", errors="replace")
        )
    except Exception:
        return {}
    return document if isinstance(document, dict) else {}


def _relay_stations(path: pathlib.Path) -> dict[str, str]:
    """The relay stations this app knows: control-plane root -> station type.

    This is what decides whether a provider is a *relay*.  A provider pointed
    straight at an upstream vendor has no entry here, so it is not a station and
    has no usage feed to read however many keys it carries.
    """

    raw_stations = _relay_document(path).get("stations")
    if not isinstance(raw_stations, list):
        return {}
    stations: dict[str, str] = {}
    for raw in raw_stations:
        if not isinstance(raw, dict):
            continue
        origin = _string(raw.get("origin"))
        if not origin:
            continue
        root = service_root(origin + "/v1") or origin.rstrip("/")
        stations[root] = _string(raw.get("type"))
    return stations


def _relay_accounts(path: pathlib.Path) -> dict[str, dict[str, Any]]:
    """The relay logins this app holds, keyed by account id.

    Only the fields a usage read needs travel further: the station the login
    belongs to, its own session, and the name the panes show.  Nothing here is
    returned to the UI — the session stays inside this module and exists for the
    duration of one read.
    """

    document = _relay_document(path)
    raw_accounts = document.get("accounts") if isinstance(document, dict) else None
    if not isinstance(raw_accounts, list):
        return {}
    stations = {}
    raw_stations = document.get("stations")
    if isinstance(raw_stations, list):
        for raw in raw_stations:
            if isinstance(raw, dict) and _string(raw.get("id")):
                stations[_string(raw.get("id"))] = raw
    accounts: dict[str, dict[str, Any]] = {}
    for raw in raw_accounts:
        if not isinstance(raw, dict):
            continue
        account_id = _string(raw.get("id"))
        if not account_id or account_id in accounts:
            continue
        username = _string(raw.get("username"))
        name = username.split("@", 1)[0].strip() or username or _string(raw.get("label"))
        station = stations.get(_string(raw.get("station_id")), {})
        accounts[account_id] = {
            "id": account_id,
            "label": name[:64],
            "origin": _string(raw.get("origin"))
            or _string(station.get("origin")),
            "type": _string(raw.get("type")) or _string(station.get("type")),
            "user_id": raw.get("user_id"),
            "session": raw.get("session") if isinstance(raw.get("session"), dict) else {},
        }
    return accounts


def _control_api_root(api_base: str) -> str | None:
    """Return the installed control-plane prefix without duplicating `/api`.

    A normal OpenAI base URL ends in `/v1`, while a reverse proxy can expose
    the same API below `/api/v1`.  `service_root` deliberately retains that
    deployment prefix, so append the control-plane `/api` segment only when it
    is not already part of the root.
    """

    root = service_root(api_base)
    if root is None:
        return None
    path = urllib.parse.urlsplit(root).path.rstrip("/").lower()
    return root if path.endswith("/api") else f"{root}/api"


def _endpoint_candidates(api_base: str) -> list[tuple[str, str, str]]:
    control_root = _control_api_root(api_base)
    root = service_root(api_base)
    if control_root is None or root is None:
        return []
    return [
        # New API's key-scoped log feed is explicitly read-only and accepts a
        # normal gateway key.  It is the appropriate source for a Menu route,
        # which stores gateway credentials rather than dashboard sessions.
        ("newapi", f"{control_root}/log/token", "bearer"),
        # Sub2API's compatible usage document lives on the API root, not under
        # its control-plane prefix: one answered per model and is stamped with
        # the moment of the read, so it is a summary and never the log feed.
        ("sub2api", f"{root}/v1/usage", "bearer"),
    ]


# The per-request ledger a Sub2API dashboard keeps, read with the account's own
# session rather than a gateway key: the gateway key is scoped to the traffic it
# served, while the dashboard answers the login's whole history.
_SUB2API_LEDGER_PATH = "/api/v1/usage"
# One page of the ledger. The station caps its own page size, and asking for
# more than it offers is answered with the cap instead of an error.
_SUB2API_PAGE_SIZE = 200
# The largest page the reader will ask a station for.  The pane asks for one
# page at a time and the station caps its own size, so this only bounds a
# request; it is never used to walk the history in a single read.
_SUB2API_MAX_PAGE_SIZE = 1_000
# A New API log page shorter than this is the account's whole history, not a
# page of it, so the reader does not follow pagination that cannot exist.
_NEWAPI_MIN_PAGE = 50
_NEWAPI_MAX_PAGES = 100

# The dashboard's own token exchange.  A Sub2API station mints a short-lived
# access token and rotates the refresh token every time it is used; both facts
# come from the station itself in that answer.
_SUB2API_REFRESH_PATH = "/api/v1/auth/refresh"
# How long before expiry a cached token is considered spent.  A token that
# expires while a page of the ledger is in flight is refused mid-read, so the
# margin covers the read it is about to authorize rather than the clock alone.
_TOKEN_EXPIRY_MARGIN_SECONDS = 120


@dataclass
class _CachedToken:
    """One account's live dashboard token, held only in this process.

    The value never reaches disk, a record, a label, an IPC payload, or a log
    line.  It exists to authorize reads for as long as the station honors it;
    when it is spent the station is asked for another one.
    """

    access_token: str
    expires_at: float

    def usable(self, now: float) -> bool:
        return bool(self.access_token) and now + _TOKEN_EXPIRY_MARGIN_SECONDS < self.expires_at


# Keyed by the account's stable id, so two logins of one station never share a
# token and a renamed label cannot resurrect a spent one.
_TOKEN_CACHE: dict[str, _CachedToken] = {}
_TOKEN_CACHE_LOCK = threading.Lock()
# Refresh tokens the station has rotated during this process's life.  The
# station invalidates the previous one, so the current value has to travel with
# the read that produced it; it is kept in memory beside the token rather than
# written back, because a persisted copy is exactly what went stale before.
_REFRESH_CACHE: dict[str, str] = {}
# Called with ``(account_id, refresh_token)`` when the station rotates a refresh
# token.  A rotated token invalidates its predecessor, so a value kept only in
# memory would be lost on the next restart and the account would need signing in
# again.  This module stays free of the relay-account store, so the owner of that
# store installs the sink and decides how a rotated credential is kept.
_ROTATED_REFRESH_SINK: Any = None


def set_rotated_refresh_sink(sink: Any) -> None:
    """Let the relay-account store keep a rotated refresh token durable."""

    global _ROTATED_REFRESH_SINK
    _ROTATED_REFRESH_SINK = sink


def _monotonic() -> float:
    return time.monotonic()


def _remember_session(
    account_id: str,
    access_token: str,
    refresh_token: str,
    expires_in: object,
) -> None:
    """Record the station's newest token pair for one account, in memory."""

    if not account_id:
        return
    try:
        seconds = float(expires_in) if expires_in is not None else 0.0
    except (TypeError, ValueError):
        seconds = 0.0
    if not math.isfinite(seconds) or seconds <= 0:
        seconds = 0.0
    with _TOKEN_CACHE_LOCK:
        _TOKEN_CACHE[account_id] = _CachedToken(
            access_token=access_token,
            expires_at=_monotonic() + seconds,
        )
        previous = _REFRESH_CACHE.get(account_id)
        if refresh_token:
            _REFRESH_CACHE[account_id] = refresh_token
    # A rotated refresh token retires its predecessor, so the new value has to
    # outlive this process: signing in again would otherwise be the only way
    # back after a restart.  The access token itself is never stored.
    if refresh_token and refresh_token != previous and _ROTATED_REFRESH_SINK is not None:
        try:
            _ROTATED_REFRESH_SINK(account_id, refresh_token)
        except Exception:
            # Keeping the credential is best effort: a store that cannot write
            # must not fail the read that already succeeded.
            pass


def _refresh_session(
    account_id: str,
    origin: str,
    refresh_token: str,
    timeout: float,
) -> str:
    """Exchange a refresh token for a live access token.

    Returns the new access token, or an empty string when the station will not
    mint one.  A station that has rotated the refresh token also hands back the
    successor; it is remembered in memory so the next refresh keeps working
    without ever writing a credential to disk.
    """

    if not account_id or not refresh_token:
        return ""
    origin = origin.rstrip("/")
    if not origin:
        return ""
    request = urllib.request.Request(
        f"{origin}{_SUB2API_REFRESH_PATH}",
        data=json.dumps({"refresh_token": refresh_token}).encode("utf-8"),
        headers={
            **browser_request_headers(),
            "Content-Type": "application/json",
            # The station's own dashboard sends this on every API call; without
            # it the same request is refused as an API client.
            "X-User-UI-Request": "1",
        },
        method="POST",
    )
    try:
        with isolated_http_opener().open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", None) or response.getcode())
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, socket.timeout, TimeoutError):
        return ""
    if not 200 <= status < 300 or len(body) > MAX_RESPONSE_BYTES:
        return ""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return ""
    access_token = _string(data.get("access_token"))
    if not access_token:
        return ""
    _remember_session(
        account_id,
        access_token,
        _string(data.get("refresh_token")),
        data.get("expires_in"),
    )
    return access_token


def _forget_token(account_id: str) -> None:
    """Drop one account's cached access token so the next read mints a new one."""

    if not account_id:
        return
    with _TOKEN_CACHE_LOCK:
        _TOKEN_CACHE.pop(account_id, None)


def _live_access_token(
    target: "UsageTarget",
    timeout: float,
    *,
    skip_stored: bool = False,
) -> str:
    """The account's current access token, minted only when it is really spent.

    The order matters, because signing in is the expensive, side-effecting step
    a station may cap:

    1. a token minted during this process, while the station still honors it;
    2. the token the login already stored, while its own `exp` says it is live —
       a dashboard session routinely outlives the reader, so asking the station
       to mint a replacement for a token that still works is pure cost;
    3. only then an exchange through the refresh token.

    A token is trusted from its `exp` claim when it has one, and is otherwise
    offered to the station once: a refusal is answered by the caller, which is
    cheaper than minting a session the account may not need.
    """

    account_id = target.account_id
    with _TOKEN_CACHE_LOCK:
        cached = _TOKEN_CACHE.get(account_id)
        if cached is not None and cached.usable(_monotonic()):
            return cached.access_token
        refresh_token = _REFRESH_CACHE.get(account_id) or target.refresh_token
    stored = "" if skip_stored else _stored_access_token(target)
    if stored:
        return stored
    return _refresh_session(account_id, target.origin, refresh_token, timeout)


def _stored_access_token(target: "UsageTarget") -> str:
    """The login's own access token, when its expiry claim says it still works.

    A station that mints 48-hour tokens was being asked for a fresh one on every
    single read, which is a session per refresh for a credential that never
    needed replacing.  The token's own `exp` settles it here without a request.
    A token that carries no readable claim is returned as it stands so the
    station can be the one to judge it.
    """

    header = next(
        (value for key, value in target.session_headers if key.lower() == "authorization"),
        "",
    )
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token:
        return ""
    expiry = _jwt_expiry(token)
    if expiry is None:
        # No readable claim: let the station decide, rather than minting a
        # replacement for a token it may well still accept.
        return token
    return token if time.time() + _TOKEN_EXPIRY_MARGIN_SECONDS < expiry else ""


def _jwt_expiry(token: str) -> float | None:
    """The `exp` claim of a JWT-shaped token, or None when it has none."""

    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    try:
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    expiry = claims.get("exp")
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
        return None
    return float(expiry)


def _session_headers_for(
    target: "UsageTarget",
    timeout: float,
    *,
    skip_stored: bool = False,
) -> dict[str, str]:
    """Dashboard headers for one read, with a token the station currently honors.

    The login's stored token is used while its own expiry claim says it is live;
    a replacement is minted only once it is spent.  ``skip_stored`` is for the
    retry that follows a refusal: that token has just been rejected, so the
    station has to mint one.
    """

    headers = dict(target.session_headers)
    authorization_key = next(
        (key for key in headers if key.lower() == "authorization"),
        "",
    )
    if not authorization_key:
        # No bearer login to renew: a cookie session is sent as it stands.
        return headers
    token = _live_access_token(target, timeout, skip_stored=skip_stored)
    if not token:
        return {}
    headers[authorization_key] = f"Bearer {token}"
    return headers


def _session_headers(account: dict[str, Any]) -> dict[str, str]:
    """Dashboard headers for one relay account, from its own stored session.

    A Sub2API login can carry a bearer token, a cookie, or both; the station
    accepts either.  ``New-Api-User`` belongs to the New API family alone and
    is never sent to a Sub2API station.  Nothing here is logged or returned:
    the headers exist only for the duration of one read.
    """

    session = account.get("session")
    if not isinstance(session, dict):
        return {}
    headers: dict[str, str] = {}
    cookie = _string(session.get("cookie"))
    token = _string(session.get("access_token"))
    if cookie:
        headers["Cookie"] = cookie
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if headers and _string(account.get("type")) == "newapi":
        user_id = _string(account.get("user_id"))
        if user_id.isdigit():
            headers["New-Api-User"] = user_id
    return headers


def _fetch_json_with_headers(
    url: str,
    headers: dict[str, str],
    timeout: float,
) -> tuple[str, dict[str, Any] | None]:
    """Read one dashboard document with already-resolved headers."""

    request = urllib.request.Request(
        url,
        headers={**browser_request_headers(), **headers},
        method="GET",
    )
    try:
        with isolated_http_opener().open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", None) or response.getcode())
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            return "refused", None
        return "unsupported", None
    except (socket.timeout, TimeoutError):
        return "timeout", None
    except (urllib.error.URLError, OSError):
        return "network", None
    if not 200 <= status < 300 or len(body) > MAX_RESPONSE_BYTES:
        return "unsupported", None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "unsupported", None
    if not isinstance(payload, dict):
        return "unsupported", None
    if payload.get("success") is False:
        return "unsupported", None
    code = payload.get("code")
    if isinstance(code, int) and not isinstance(code, bool) and code != 0:
        return "unsupported", None
    return "ok", payload


def _newapi_self_page(
    target: "UsageTarget",
    timeout: float,
    page: int,
    size: int,
) -> tuple[list[dict[str, Any]] | None, bool, int, int]:
    """Read one page of the account's own log feed, the way its dashboard does.

    ``/log/self`` is the endpoint the dashboard itself calls: it answers the
    *login's* history, so the pane and the station show the same rows.  The
    per-key feed beside it is scoped to the traffic one key served, and this
    account's six keys proved that is not the same history — their newest rows
    were 32 hours apart, and the same request answered 42, 53, 447 and 1000 rows
    depending on which key was sent.  Reading by key was therefore both slower
    (one request per key, merged) and wrong (a partial history presented as the
    account's).

    The second value says the station refused the login, the third is the total
    the station states, and the fourth is the page size it actually applied: a
    feed that caps its own page answered 100 rows for a request of 300, and a
    pager built on the request would promise pages it cannot produce.

    ``p`` is one-based here: the feed answered page one for both ``p=0`` and
    ``p=1``, and every page after that follows its own numbering.
    """

    control_root = _control_api_root(target.api_base)
    if not control_root:
        return None, False, 0, 0
    query = urllib.parse.urlencode({"p": max(1, page), "page_size": max(1, size)})
    result, payload = _fetch_json_with_headers(
        f"{control_root}/log/self?{query}", target.headers(), timeout
    )
    if result != "ok" or payload is None:
        return None, result == "refused", 0, 0
    data = payload.get("data") if isinstance(payload, dict) else None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None, False, 0, 0
    total = data.get("total") if isinstance(data, dict) else None
    total_rows = (
        total if isinstance(total, int) and not isinstance(total, bool) and total > 0 else 0
    )
    applied = data.get("page_size") if isinstance(data, dict) else None
    applied_rows = (
        applied if isinstance(applied, int) and not isinstance(applied, bool) and applied > 0 else 0
    )
    return [item for item in items if isinstance(item, dict)], False, total_rows, applied_rows


def _sub2api_ledger_rows(
    headers: dict[str, str],
    origin: str,
    timeout: float,
    *,
    page: int = 1,
    rows_per_page: int | None = None,
) -> tuple[list[dict[str, Any]] | None, bool, int, int]:
    """Read one page of the account's per-request usage ledger.

    The dashboard reports each request with its own model, token counts,
    duration, and timestamp, which is what the pane's columns and its time
    filter actually need.  The compatible ``/v1/usage`` summary beside it has
    none of those: it answers one always-current row per model, so a range
    filter can never select anything and the table can only ever show as many
    rows as the account has models.  Reading the ledger is what makes both
    honest.

    One page per read, positioned by ``page``.  The station's own pagination is
    the only way to answer an account with tens of thousands of rows: walking
    every page before answering anything took 28 s on a real account, far past
    the moment the pane stops waiting, and the table sat empty behind a spinner
    while the whole history was fetched.  The total the station reports is
    returned beside the rows, so the pane can say how much history exists
    without reading any of it.

    The second value says the station *refused* the read, which is what an
    expired dashboard login answers.  A refusal is not an empty ledger: the
    account has to be signed in again, and the pane has to be able to say so.
    The third value is the account's total row count as the station reports it,
    and the fourth is the page size it actually applied.  A station that is asked
    for an out-of-range page does not cap it — it silently answers its own
    default (20 rows for a request of 2000) — so a pager built on the request
    would promise pages the ledger never returns.
    """

    if not headers:
        return None, False, 0, 0
    origin = origin.rstrip("/")
    if not origin:
        return None, False, 0, 0

    size = rows_per_page or _SUB2API_PAGE_SIZE
    size = max(1, min(size, _SUB2API_MAX_PAGE_SIZE))
    # The ledger's own query, as the station's dashboard sends it: the list is
    # ordered server-side, and a request that omits the ordering is answered
    # with an arbitrary window of the account's history rather than its newest
    # page.  The dashboard has always sent these params, so they are the
    # endpoint's contract, not a preference.
    query = urllib.parse.urlencode(
        {
            "page": max(1, page),
            "page_size": size,
            "sort_by": "created_at",
            "sort_order": "desc",
        }
    )
    result, payload = _fetch_json_with_headers(
        f"{origin}{_SUB2API_LEDGER_PATH}?{query}", headers, timeout
    )
    if result != "ok" or payload is None:
        # A refusal (401/403) is the station rejecting this login, which is what
        # an expired dashboard session answers.  Every other failure says
        # nothing about the login.
        return None, result == "refused", 0, 0
    data = payload.get("data")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None, False, 0, 0
    total = data.get("total") if isinstance(data, dict) else None
    total_rows = (
        total
        if isinstance(total, int) and not isinstance(total, bool) and total > 0
        else 0
    )
    echoed = data.get("page_size") if isinstance(data, dict) else None
    applied_rows = (
        echoed
        if isinstance(echoed, int) and not isinstance(echoed, bool) and echoed > 0
        else size
    )
    return [item for item in items if isinstance(item, dict)], False, total_rows, applied_rows


def _fetch_json(
    url: str,
    api_key: str,
    timeout: float,
    authorization_scheme: str,
) -> tuple[str, dict[str, Any] | None]:
    authorization = api_key if authorization_scheme == "raw" else f"Bearer {api_key}"
    request = urllib.request.Request(
        url,
        headers={
            **browser_request_headers(),
            "Authorization": authorization,
        },
        method="GET",
    )
    try:
        with isolated_http_opener().open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", None) or response.getcode())
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        # A refusal is not the same fact as a missing route: 401/403 says the
        # endpoint exists and this credential may not read it, while 404 says
        # the station does not install it.  The difference decides whether the
        # next credential is worth trying.
        if exc.code in {401, 403}:
            return "refused", None
        return "unsupported", None
    except (socket.timeout, TimeoutError):
        return "timeout", None
    except (urllib.error.URLError, OSError):
        return "network", None
    if not 200 <= status < 300 or len(body) > MAX_RESPONSE_BYTES:
        return "unsupported", None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "unsupported", None
    if not isinstance(payload, dict):
        return "unsupported", None
    if payload.get("success") is False:
        return "unsupported", None
    code = payload.get("code")
    if isinstance(code, int) and not isinstance(code, bool) and code != 0:
        return "unsupported", None
    return "ok", payload


def _items(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Return a recognized paginated item list, including an empty one.

    Empty usage history is a valid response.  Keeping it distinct from an
    unrecognized JSON object lets the UI say that a relay has no recent rows
    instead of falsely claiming that no supported endpoint exists.
    """

    for container in (payload.get("data"), payload):
        if isinstance(container, dict):
            for key in ("items", "data", "list", "logs"):
                value = container.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        elif isinstance(container, list):
            return [item for item in container if isinstance(item, dict)]
    return None


def _named_text(value: Any, limit: int = 64) -> str:
    """The name a station states for something, whether it is a string or an object.

    A Sub2API ledger spells a grouping as a whole object (``{"id": 21, "name":
    "混合分组", …}``), not as a string.  Reading only strings dropped that name
    everywhere it was nested, so the pane had a 分组 column with nothing in it.
    """

    if isinstance(value, str):
        return _safe_text(value, limit)
    if isinstance(value, Mapping):
        for key in ("name", "title", "label", "group_name", "model_name"):
            text = _safe_text(value.get(key), limit)
            if text:
                return text
    return ""


def _safe_text(value: Any, limit: int = 120) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text[:limit]


def _safe_number(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(number):
        return ""
    return str(int(number)) if number.is_integer() else f"{number:.4g}"


def _safe_timestamp(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            return ""
        if not math.isfinite(timestamp):
            return ""
        # Relay log APIs commonly use Unix seconds.  Accept milliseconds too,
        # but reject implausible values instead of rendering arbitrary numbers.
        if timestamp > 10_000_000_000:
            timestamp /= 1_000
        if not 946_684_800 <= timestamp <= 4_102_444_800:
            return ""
        try:
            return (
                dt.datetime.fromtimestamp(timestamp, dt.timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except (OverflowError, OSError, ValueError):
            return ""
    text = _safe_text(value, 40)
    if not text:
        return ""
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _total_tokens(row: dict[str, Any]) -> str:
    for key in ("total_tokens", "token_count", "tokens"):
        value = _safe_number(row.get(key))
        if value:
            return value

    parts = (
        "prompt_tokens",
        "completion_tokens",
        "input_tokens",
        "output_tokens",
        "cache_creation_tokens",
        "cache_read_tokens",
    )
    total = 0.0
    found = False
    for key in parts:
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if not math.isfinite(number):
            continue
        total += number
        found = True
    return _safe_number(total) if found else ""


def _summary_rows(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Project a credential-scoped usage summary into safe model rows."""

    raw_models: Any = payload.get("model_stats")
    if not isinstance(raw_models, list):
        data = payload.get("data")
        raw_models = data.get("model_stats") if isinstance(data, dict) else None
    if not isinstance(raw_models, list):
        return None
    rows: list[dict[str, Any]] = []
    for raw_model in raw_models:
        if not isinstance(raw_model, dict):
            continue
        model = _safe_text(raw_model.get("model") or raw_model.get("model_name"), 96)
        if not model:
            continue
        row: dict[str, Any] = {"model": model}
        for key in (
            "total_tokens",
            "token_count",
            "tokens",
            "input_tokens",
            "output_tokens",
            "prompt_tokens",
            "completion_tokens",
            "cache_creation_tokens",
            "cache_read_tokens",
        ):
            if key in raw_model:
                row[key] = raw_model[key]
        rows.append(row)
    return rows


def _duration_milliseconds(row: Mapping[str, Any]) -> str:
    """One request's duration in milliseconds, whichever spelling states it.

    Two families answer here with different units: a Sub2API ledger records
    `duration_ms` (already milliseconds), while a New API log records `use_time`
    in seconds.  Reading them as one unit rendered every New API row as `0`s —
    eight seconds became eight milliseconds — so the unit is decided by the key
    that carried the value.
    """

    milliseconds = row.get("duration_ms")
    if milliseconds is None:
        milliseconds = row.get("latency_ms")
    if milliseconds is not None:
        return _safe_number(milliseconds)
    seconds = row.get("use_time")
    if seconds is None:
        seconds = row.get("duration")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return _safe_number(seconds)
    return _safe_number(round(float(seconds) * 1000))


def _row_details(row: dict[str, Any]) -> dict[str, str]:
    """Project one usage row into the fields the log view renders.

    Only documented, presentational keys survive: a relay's response is
    third-party data, and the pane must never show a request body, an IP, a
    path, or anything else the station happened to include.  Every value is a
    string so the record is the same shape whichever endpoint answered.
    """

    fields = {
        "model": _safe_text(
            row.get("model_name") or row.get("model") or row.get("requested_model"),
            96,
        ),
        # A Sub2API ledger states no per-request outcome at all: it records
        # `request_type` (stream / sync), which is the shape of the call, not
        # whether it succeeded.  Reading that as a status labelled every
        # historical row as streaming, so the column reported the transport as
        # if it were the result.  Only a real status field is a status.
        "status": _safe_text(row.get("status"), 32),
        "channel": _safe_text(row.get("channel_name") or row.get("channel"), 64),
        # The grouping decides which rate a request was billed at, so it is a
        # fact about the row and not decoration: a station sends it as an object
        # with its own name.
        "group": _named_text(row.get("group") or row.get("group_name") or row.get("group_title"), 64),
        "key_name": _named_text(
            row.get("token_name") or row.get("key_name") or row.get("api_key"), 64
        ),
        "duration": _duration_milliseconds(row),
        "prompt_tokens": _safe_number(row.get("prompt_tokens") or row.get("input_tokens")),
        "completion_tokens": _safe_number(
            row.get("completion_tokens") or row.get("output_tokens")
        ),
        "cache_tokens": _safe_number(
            row.get("cache_read_tokens") or row.get("cache_creation_tokens")
        ),
        "total_tokens": _total_tokens(row),
        "cost": _safe_number(row.get("quota") or row.get("cost") or row.get("amount")),
    }
    return {key: value for key, value in fields.items() if value}


def _row_text(record: dict[str, Any], fallback_timestamp: str) -> str:
    timestamp = record.get("timestamp") or fallback_timestamp
    model = record.get("model") or ""
    status = record.get("status") or ""
    tokens = record.get("total_tokens") or ""
    pieces = [
        part
        for part in [timestamp, model, status, f"tokens={tokens}" if tokens else ""]
        if part
    ]
    return "  ".join(pieces)


def _row_record(
    raw: dict[str, Any],
    target: UsageTarget,
    family: str,
    fallback_timestamp: str,
) -> dict[str, Any] | None:
    """Build one usage record carrying its relay's own name and its fields.

    The record names the *account* as well as the station: a station with two
    logins shows two rows histories in one table, and the relay column is how
    the user tells whose traffic a row is.
    """

    timestamp = _safe_timestamp(
        raw.get("created_at", raw.get("createdAt", raw.get("timestamp")))
    ) or fallback_timestamp
    details = _row_details(raw)
    if not details.get("model") and not details.get("total_tokens"):
        # A row that names neither a model nor a token count is the station's
        # own placeholder; rendering it would be a blank line with a relay name.
        return None
    record: dict[str, Any] = {
        "timestamp": timestamp,
        "source": family,
        "source_id": target.source_id,
        "source_label": target.provider,
        "source_account": target.account,
        "account_id": target.account_id,
        "source_family": target.family_label(family),
    }
    record.update(details)
    return record


def fetch_target(
    target: UsageTarget,
    timeout: float,
    fallback_timestamp: str | None = None,
    *,
    page: int = 1,
    rows_per_page: int | None = None,
) -> tuple[str, list[dict[str, Any]], str, int, int]:
    """Read every one of the account's keys and merge what they report.

    The feed is scoped per key, so one key answers for the traffic it served
    and nothing else: the key an account happens to list first can legitimately
    answer zero rows while another is the busiest.  Every key of *this account*
    is therefore read and their rows merged.  A station's other logins are
    other targets with their own keys, so this read never answers for a login
    the user did not select.

    Which endpoint shape a station installs is a property of the *station*,
    not of a key, so it is settled once and the keys are then read against
    that one shape.  Probing every shape with every key multiplied the
    requests a refresh costs by the number of keys, and each request is the
    slow part.
    """

    fallback_timestamp = fallback_timestamp or _utc_now_iso()
    # A Sub2API station keeps its per-request ledger behind the dashboard login,
    # which is the only shape that carries a timestamp, a duration, and a row
    # per request.  Reading the compatible summary document instead answered one
    # always-current row per model, so the pane could never show more rows than
    # the account had models and no time filter could select anything.
    #
    # The ledger is therefore the account's only log feed: when the dashboard
    # login it needs has expired, the summary document beside it is *not* a
    # fallback.  Falling through to it answered one row per model stamped with
    # the moment of the read, so a signed-out account showed "9 rows" over nine
    # models and read as a complete, up-to-date history.  An empty answer here
    # is the honest one: the pane states that the relay reported nothing rather
    # than presenting a per-model total as per-request traffic.
    if target.station_type == "sub2api" and target.session_headers:
        # The read uses the token this login already holds while it is live, and
        # only mints another one once that is spent.  A dashboard session
        # routinely outlives the pane, and asking the station to replace a
        # working token costs a session each time — which is exactly what a
        # station with a concurrent-session cap makes expensive.
        headers = _session_headers_for(target, timeout)
        if not headers:
            # Neither the stored token nor an exchange produced one, so this
            # login really is spent.  The caller may be able to renew it; it is
            # the one that knows whether a sign-in is allowed.
            return "", [], USAGE_REASON_LOGIN_EXPIRED, 0, 0
        ledger, refused, station_total, applied_rows = _sub2api_ledger_rows(
            headers, target.origin, timeout, page=page, rows_per_page=rows_per_page
        )
        if refused:
            # The station refused the token it was given.  One renewal is tried,
            # and only one: a station that refuses two tokens in a row is not
            # answering a rotation, and repeating the exchange would be the
            # refresh loop this read exists to avoid.
            _forget_token(target.account_id)
            headers = _session_headers_for(target, timeout, skip_stored=True)
            if not headers:
                return "", [], USAGE_REASON_LOGIN_EXPIRED, 0, 0
            ledger, refused, station_total, applied_rows = _sub2api_ledger_rows(
                headers, target.origin, timeout, page=page, rows_per_page=rows_per_page
            )
        if ledger is not None:
            # An empty list is the station stating that this login has served
            # nothing: a real answer, and the pane says so.  `None` is a read
            # that never came back, which is a different fact below.  The kind
            # travels even with no rows, because it says the station was read.
            return (
                "sub2api",
                _deduplicated_records(
                    _records_from(ledger, target, "sub2api", fallback_timestamp)
                ),
                "",
                station_total,
                applied_rows,
            )
        # The dashboard login is the only thing that can read this station's
        # ledger, so a refused read means the login no longer answers and the
        # account has to be signed in again.  That is reported as its own
        # reason: an expired login is something the user can fix, and it is not
        # the same fact as a station that installs no log feed.
        if refused:
            return "", [], USAGE_REASON_LOGIN_EXPIRED, 0, 0
        return "", [], "", 0, 0
    # Every usage read goes through the account's own dashboard session: the
    # per-key feed beside it answers only the traffic one key served, so a
    # reader that walked the keys reported a partial history as the account's —
    # one request per key, and pages of 42, 53, 447 and 1000 rows for the same
    # question.  A relay with no dashboard login therefore has no readable
    # history here, and saying so is more useful than drawing another list.
    size = max(1, min(rows_per_page or _SUB2API_PAGE_SIZE, MAX_ROWS_PER_TARGET))
    if target.station_type == "newapi" and target.session_headers:
        rows, refused, station_total, applied_rows = _newapi_self_page(
            target, timeout, page, size
        )
        if rows is not None:
            return (
                "newapi",
                _deduplicated_records(
                    _records_from(rows, target, "newapi", fallback_timestamp)
                ),
                "",
                station_total,
                applied_rows,
            )
        if refused:
            return "", [], USAGE_REASON_LOGIN_EXPIRED, 0, 0
        return "", [], "", 0, 0
    if not target.session_headers:
        return "", [], USAGE_REASON_NO_CREDENTIAL, 0, 0
    return "", [], "", 0, 0


def _fetch_key(
    api_key: str,
    probe: tuple[str, str, str],
    timeout: float,
    kind: str,
) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]] | None]:
    """Read one key's page: its result, the payload, and the recognized rows.

    New API's log feed is paginated and the first page is a single page, not the
    account's history: reading only that page reported the busiest account's
    rows as the few hundred a default page happens to carry.  The remaining
    pages are read while the station says more exist.
    """

    _kind, endpoint, authorization_scheme = probe
    result, payload = _fetch_json(endpoint, api_key, timeout, authorization_scheme)
    if result != "ok" or payload is None:
        return result, payload, None
    rows = _items(payload)
    if rows is None and kind == "sub2api":
        rows = _summary_rows(payload)
    if rows is None:
        return result, payload, None
    if kind == "newapi":
        rows = _newapi_all_pages(rows, endpoint, payload, api_key, timeout, authorization_scheme)
    return result, payload, rows


def _newapi_all_pages(
    first_rows: list[dict[str, Any]],
    endpoint: str,
    payload: dict[str, Any],
    api_key: str,
    timeout: float,
    authorization_scheme: str,
) -> list[dict[str, Any]]:
    """Follow a New API log feed's own pagination up to the reader's bound."""

    rows = list(first_rows)
    page_size = len(first_rows)
    if page_size < _NEWAPI_MIN_PAGE or len(rows) >= MAX_ROWS_PER_TARGET:
        return rows
    # New API answers ``p`` as a zero-based page and ``page_size`` as the page's
    # own length; reuse the length the station just returned rather than
    # guessing a size it may reject.
    for page in range(1, _NEWAPI_MAX_PAGES + 1):
        if len(rows) >= MAX_ROWS_PER_TARGET:
            break
        separator = "&" if "?" in endpoint else "?"
        url = f"{endpoint}{separator}p={page}&page_size={page_size}"
        result, more = _fetch_json(url, api_key, timeout, authorization_scheme)
        if result != "ok" or more is None:
            break
        page_rows = _items(more)
        if not page_rows:
            break
        rows.extend(page_rows)
        if len(page_rows) < page_size:
            break
    return rows


def _records_from(
    rows: list[dict[str, Any]],
    target: UsageTarget,
    kind: str,
    fallback_timestamp: str,
) -> list[dict[str, Any]]:
    return [
        record
        for record in (
            _row_record(row, target, kind, fallback_timestamp)
            for row in rows[:MAX_ROWS_PER_TARGET]
        )
        if record
    ]


def _deduplicated_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop rows the station reported through more than one key.

    A key-scoped feed can overlap, and the same request appearing twice would
    read as two requests the user made.  Rows are identified by what the
    station stated about them, so a genuine repeat (same model, same status,
    same counts at the same instant) is the only thing collapsed.  The account
    is part of the identity: two logins of one relay can serve the same model
    at the same second, and those are two people's requests.
    """

    seen: set[tuple[Any, ...]] = set()
    unique: list[dict[str, Any]] = []
    for record in records:
        identity = (
            record.get("account_id"),
            record.get("timestamp"),
            record.get("model"),
            record.get("status"),
            record.get("prompt_tokens"),
            record.get("completion_tokens"),
            record.get("total_tokens"),
            record.get("key_name"),
        )
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(record)
    return unique


def _fetch_target_safely(
    target: UsageTarget,
    timeout: float,
    fallback_timestamp: str,
    *,
    page: int = 1,
    rows_per_page: int | None = None,
) -> tuple[str, list[dict[str, Any]], str, int, int]:
    try:
        return fetch_target(
            target,
            timeout,
            fallback_timestamp,
            page=page,
            rows_per_page=rows_per_page,
        )
    except Exception:
        # A malformed third-party response must not prevent other configured
        # relays from refreshing, and the underlying exception may include a
        # host or remote text that the native window must never display.
        return "", [], "", 0, 0


def usage_sources(path: pathlib.Path) -> list[dict[str, str]]:
    """List the selectable read targets without exposing any credential.

    The dropdown that names them is what decides the scope of a refresh, so the
    list must be safe to render: an id and the station's own name.  A source
    that yields no usable credential is simply absent, which is how the UI says
    there is nothing to read.
    """

    try:
        targets = active_usage_targets(path)
    except UsageConfigError:
        return []
    return [
        {"id": target.source_id, "label": target.display()}
        for target in targets
    ]


def collect(
    path: pathlib.Path,
    timeout: float,
    source_id: str | None = None,
    relogin: Any = None,
    *,
    page: int = 1,
    rows_per_page: int | None = None,
) -> tuple[list[dict[str, Any]], str, int, int]:
    """Read one page of a relay's usage rows.

    Returns the records, a status sentence for the view to show when there is
    nothing to list, the account's total row count as the station reports it,
    and the page size the feed actually applied.  The total is what lets the pane say "48 345 rows" without reading any
    of them: fetching a whole history before drawing anything is what left the
    tab spinning, since a real account with 48 000 rows took 28 s.

    The caller renders the rows; this never formats the station's data into
    prose, because the pane owns how a row looks.

    ``relogin`` is called with an account id that the station has stopped
    accepting.  A relay account commonly has its password remembered, and a
    spent dashboard login is exactly the case that can renew itself: reporting
    it as expired without asking is telling the user to do work the app can do.
    The callback renews the stored login; this reader then reads again.  Only a
    renewal that also fails ends as ``login_expired``, which is the one case the
    user can still act on.
    """

    try:
        targets = active_usage_targets(path)
    except UsageConfigError:
        return [], USAGE_REASON_CONFIG_UNREADABLE, 0, 0
    if source_id:
        # An unknown id is not an error the user can fix: the configuration may
        # have moved under a dropdown whose selection is one refresh old. Read
        # nothing rather than silently answering for a different relay.
        targets = [target for target in targets if target.source_id == source_id]
        if not targets:
            return [], USAGE_REASON_UNKNOWN_SOURCE, 0, 0
    if not targets:
        return [], USAGE_REASON_NO_CREDENTIAL, 0, 0

    read = _read_targets(targets, timeout, page=page, rows_per_page=rows_per_page)
    if read.expired_login and callable(relogin):
        # The login is spent, and a remembered password can replace it.  The
        # renewals run first and the read repeats, so a relay that signs itself
        # back in never appears to the user as an expired login at all.
        renewed = False
        for target in targets:
            if target.account_id and _renew_login(relogin, target.account_id):
                renewed = True
        if renewed:
            # The station has new material, so the cached tokens for these
            # accounts are spent by definition.
            for target in targets:
                _forget_token(target.account_id)
            try:
                targets = [
                    t
                    for t in active_usage_targets(path)
                    if not source_id or t.source_id == source_id
                ]
            except UsageConfigError:
                targets = []
            read = _read_targets(targets, timeout, page=page, rows_per_page=rows_per_page)
    if not read.readable:
        # A relay whose dashboard login the station no longer accepts is not a
        # relay that installs no feed: signing in again is the fix, so the pane
        # says that instead of "unsupported".
        reason = (
            USAGE_REASON_LOGIN_EXPIRED if read.expired_login else USAGE_REASON_UNSUPPORTED
        )
        return [], reason, read.total, read.page_rows
    if not read.records and read.answered_empty:
        return [], USAGE_REASON_NO_ROWS, read.total, read.page_rows
    return read.records, "", read.total, read.page_rows


def _renew_login(relogin: Any, account_id: str) -> bool:
    """Ask the store to sign one account in again, keeping every failure quiet.

    A station that refuses the sign-in, caps its sessions, or cannot be reached
    is not an error this reader reports: the read that follows states what the
    relay's usage actually is, and the login itself is the relay panes' story.
    """

    try:
        renewed = relogin(account_id)
    except Exception:
        return False
    return bool(renewed)


@dataclass
class _TargetRead:
    """One page read from every selected relay, folded into what the pane needs."""

    records: list[dict[str, Any]]
    readable: bool
    answered_empty: bool
    expired_login: bool
    # The account's total row count as the station reports it, which is what
    # the pager counts pages from without reading the rows.
    total: int
    # The page size the feed actually applied, which is what the pager counts
    # from: a feed that caps its own page answered 100 rows for a request of 300.
    page_rows: int


def _read_targets(
    targets: list[UsageTarget],
    timeout: float,
    *,
    page: int = 1,
    rows_per_page: int | None = None,
) -> _TargetRead:
    """Read one page from every target and fold the answers into one result."""

    if not targets:
        return _TargetRead([], False, False, False, 0, 0)
    observed_at = _utc_now_iso()
    workers = max(1, min(MAX_WORKERS, len(targets)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                _fetch_target_safely,
                target,
                timeout,
                observed_at,
                page=page,
                rows_per_page=rows_per_page,
            )
            for target in targets
        ]
        results = [future.result() for future in futures]

    records: list[dict[str, Any]] = []
    readable = False
    answered_empty = False
    expired_login = False
    total = 0
    page_rows = 0
    for source, rows, reason, station_total, applied_rows in results:
        if applied_rows > 0:
            page_rows = applied_rows
        if reason == USAGE_REASON_LOGIN_EXPIRED:
            expired_login = True
        # One selected relay answers for one page, so its own total is the
        # count the pager needs.  Several accounts of one station are several
        # lists, and their totals add up.
        if station_total > 0:
            total += station_total
        if not source:
            continue
        readable = True
        if not rows:
            # The station answered and its ledger is empty: an account whose keys
            # have not served anything yet is a different fact from a station
            # that installs no feed, and the pane says which.
            answered_empty = True
        records.extend(rows)
    return _TargetRead(records, readable, answered_empty, expired_login, total, page_rows)


def render(
    path: pathlib.Path,
    timeout: float,
    source_id: str | None = None,
) -> str:
    """The CLI's own plain-text presentation of the same records."""

    records, status, _total, _page_rows = collect(path, timeout, source_id)
    if status:
        return status
    if not records:
        return "No recent usage rows."
    groups: dict[str, list[str]] = {}
    for record in records:
        label = str(record.get("source_label") or "Relay")
        account = str(record.get("source_account") or "")
        kind = str(record.get("source_family") or "")
        heading = f"{label} · {account}" if account else label
        if kind:
            heading = f"{heading} ({kind})"
        groups.setdefault(heading, []).append(_row_text(record, _utc_now_iso()))
    sections = [f"{heading}\n" + "\n".join(rows) for heading, rows in groups.items()]
    return f"Updated {_utc_now_iso()}\n\n" + "\n\n".join(sections)


def main() -> int:
    parser = argparse.ArgumentParser(description="Print sanitized online relay usage logs.")
    parser.add_argument("--config", default=str(default_config_path()))
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--source", default="", help="Read only the relay whose source id this is.")
    args = parser.parse_args()
    timeout = max(0.5, min(float(args.timeout), 15.0))
    print(
        render(
            pathlib.Path(args.config).expanduser(),
            timeout,
            _string(args.source) or None,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
