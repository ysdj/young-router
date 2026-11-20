"""Skip relay deployments whose account has a fresh, negative balance.

The Core owns balance collection.  The proxy only reads the small account
document it shares with Core and treats an observation as authoritative for
one refresh interval.  Any malformed, missing, failed, or expired observation
leaves the deployment eligible so a transient control-plane problem cannot
turn into an outage.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ..config.schema import MENU_PROVIDER_KEY_ID_KEY, MENU_RELAY_KEYS_KEY
from ..config_paths import provider_config_path
from ..core.runtime_settings_schema import (
    RELAY_NEGATIVE_BALANCE_DEFAULT_MINUTES,
    RELAY_NEGATIVE_BALANCE_INTERVAL_KEY,
    relay_balance_should_block,
)
from ..webdav.core import relay_accounts_path
from .reasoning_capability import runtime_config_path


def _config_path() -> Path:
    # The managed Core stages the worker file separately, but ownership and
    # observations belong to the editor document it names here.
    if os.environ.get("LITELLM_CONFIG_FILE", "").strip():
        return provider_config_path()
    configured = runtime_config_path()
    return Path(configured).expanduser() if configured else provider_config_path()


def _version(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_ino


@lru_cache(maxsize=1)
def _negative_observations(path: Path, version: tuple[int, int]) -> dict[str, tuple[object, object]]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    rows = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    # Keep only the fields routing needs, never the account's credentials.
    return {
        account_id: (row.get("balance"), row.get("balance_observed_at"))
        for row in rows
        if isinstance(row, dict) and isinstance((account_id := row.get("id")), str)
    }


@lru_cache(maxsize=1)
def _load_key_sources(path: Path, version: tuple[int, int]) -> dict[tuple[str, str], str]:
    """Map ``(provider, provider-key-id)`` to a relay account id."""

    with path.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    providers = document.get("providers") if isinstance(document, dict) else None
    sources: dict[tuple[str, str], str] = {}
    if not isinstance(providers, dict):
        return sources
    for provider, record in providers.items():
        if not isinstance(provider, str) or not isinstance(record, dict):
            continue
        metadata = record.get(MENU_RELAY_KEYS_KEY)
        slots = metadata.get("slots") if isinstance(metadata, dict) else None
        for slot in slots if isinstance(slots, list) else ():
            if not isinstance(slot, dict):
                continue
            key_id = slot.get("id")
            source = slot.get("source")
            if not isinstance(source, dict) or source.get("kind") != "relay":
                continue
            account_id = source.get("account_id")
            if isinstance(key_id, str) and key_id and isinstance(account_id, str) and account_id:
                sources[(provider, key_id)] = account_id
    return sources


def _deployment_slot(deployment: Mapping[str, Any]) -> tuple[str, str] | None:
    info = deployment.get("model_info")
    if not isinstance(info, Mapping):
        return None
    key_id = info.get(MENU_PROVIDER_KEY_ID_KEY)
    provider = info.get("provider")
    if not isinstance(provider, str) or not isinstance(key_id, str) or not key_id:
        return None
    return provider, key_id


def blocked_slots() -> set[tuple[str, str]]:
    """Provider key slots backed by a current negative balance observation."""

    config = _config_path()
    accounts = relay_accounts_path(config)
    accounts_version = _version(accounts)
    if accounts_version is None:
        return set()
    try:
        interval = os.environ.get(RELAY_NEGATIVE_BALANCE_INTERVAL_KEY, str(RELAY_NEGATIVE_BALANCE_DEFAULT_MINUTES))
        now = datetime.now(timezone.utc)
        blocked_accounts = {
            account_id
            for account_id, (balance, stamp) in _negative_observations(accounts, accounts_version).items()
            if relay_balance_should_block(balance, stamp, interval, now=now)
        }
        if blocked_accounts and (config_version := _version(config)) is not None:
            return {
                slot
                for slot, account_id in _load_key_sources(config, config_version).items()
                if account_id in blocked_accounts
            }
    except (OSError, ValueError, yaml.YAMLError):
        # Neither a malformed observation nor a missing source is evidence
        # that the account has a negative balance.
        pass
    return set()


def blocked_deployment_ids(
    deployments: Callable[[], Iterable[Mapping[str, Any]]],
) -> set[str]:
    slots = blocked_slots()
    if not slots:
        return set()
    return {
        deployment_id
        for deployment in deployments()
        if _deployment_slot(deployment) in slots
        and isinstance((deployment_id := deployment["model_info"].get("id")), str)
    }


def filter_deployments(deployments: list[dict]) -> list[dict]:
    """Remove only deployments owned by accounts with a fresh negative balance."""

    slots = blocked_slots() if deployments else set()
    return [row for row in deployments if _deployment_slot(row) not in slots] if slots else deployments


def clear_cache() -> None:
    _negative_observations.cache_clear()
    _load_key_sources.cache_clear()
