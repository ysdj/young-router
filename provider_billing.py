#!/usr/bin/env python3
"""Read live, credential-scoped billing data for LiteLLM Menu models.

The command intentionally recognizes response shapes instead of provider names or
hosts. It never writes a cache: each invocation is a bounded live refresh, and
the emitted document excludes API bases, credentials, and upstream response text.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import pathlib
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from config_editor_core.load import load_config


SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 4.0
MIN_TIMEOUT_SECONDS = 0.5
MAX_TIMEOUT_SECONDS = 15.0
MAX_ACCOUNTS_PER_REFRESH = 24
MAX_RESPONSE_BYTES = 256 * 1024
MULTIPLIER_UNAVAILABLE_DETAIL = (
    "The billing API does not expose a model multiplier to this credential."
)
UNLIMITED_QUOTA_DETAIL = "The provider reports unlimited quota; no finite balance is available."


class BillingConfigError(ValueError):
    """The current editor configuration could not be read safely."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        fp: Any,
        code: int,
        message: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


HttpOpener = Callable[[urllib.request.Request, float], Any]
BrowserProbe = Callable[..., Any]


def _default_browser_probe(
    api_base: str,
    api_key: str,
    *,
    timeout: float,
) -> tuple[str, int | None, dict[str, Any] | None]:
    """Ask the optional local browser bridge without importing it at startup."""

    try:
        from browser_billing import fetch_browser_token_search
    except (ImportError, OSError):
        return "unavailable", None, None
    return fetch_browser_token_search(api_base, api_key, timeout=timeout)


@dataclass(frozen=True)
class BillingTarget:
    provider: str
    model: str
    upstream_model: str
    deployment_id: str
    api_base: str
    api_key: str


def default_config_path() -> pathlib.Path:
    configured = os.environ.get("LITELLM_CONFIG_FILE", "").strip()
    if configured:
        return pathlib.Path(configured).expanduser()
    root = os.environ.get("LITELLM_RUNTIME_ROOT", "").strip() or os.environ.get(
        "LITELLM_MENU_HOME", ""
    ).strip()
    if root:
        return pathlib.Path(root).expanduser() / "config.yaml"
    return pathlib.Path.home() / ".litellm-menu" / "config.yaml"


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _enabled(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _credential_value(value: Any) -> str:
    text = _string(value)
    if not text.startswith("os.environ/"):
        return "" if "\r" in text or "\n" in text else text
    variable = text.removeprefix("os.environ/").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
        return ""
    resolved = os.environ.get(variable, "").strip()
    return "" if "\r" in resolved or "\n" in resolved else resolved


def _provider_keys(provider: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    by_name: dict[str, str] = {}
    ordered: list[str] = []
    raw_keys = provider.get("api_keys")
    if not isinstance(raw_keys, list):
        return by_name, ordered
    for index, item in enumerate(raw_keys, start=1):
        if not isinstance(item, dict):
            continue
        name = _string(item.get("name")) or f"key-{index}"
        value = _credential_value(item.get("value"))
        if not value or name in by_name:
            continue
        by_name[name] = value
        ordered.append(value)
    return by_name, ordered


def _credential_for_model(
    provider: dict[str, Any], model: dict[str, Any]
) -> str:
    direct = _credential_value(model.get("api_key"))
    if direct:
        return direct
    by_name, ordered = _provider_keys(provider)
    preferred_name = _string(model.get("api_key_name"))
    if preferred_name and preferred_name in by_name:
        return by_name[preferred_name]
    if "default" in by_name:
        return by_name["default"]
    return ordered[0] if len(ordered) == 1 else ""


def active_billing_targets(path: pathlib.Path) -> list[BillingTarget]:
    try:
        payload = load_config(path)
    except Exception as exc:
        raise BillingConfigError("The LiteLLM configuration could not be read.") from exc

    raw_providers = payload.get("providers")
    if not isinstance(raw_providers, list):
        raise BillingConfigError("The LiteLLM configuration has no provider list.")

    targets: list[BillingTarget] = []
    for raw_provider in raw_providers:
        if not isinstance(raw_provider, dict):
            continue
        provider_name = _string(raw_provider.get("name")) or "unassigned"
        raw_models = raw_provider.get("models")
        if not isinstance(raw_models, list):
            continue
        provider_base = _string(raw_provider.get("api_base"))
        for raw_model in raw_models:
            if not isinstance(raw_model, dict):
                continue
            model_name = _string(raw_model.get("model_name"))
            if not model_name:
                continue
            targets.append(
                BillingTarget(
                    provider=provider_name,
                    model=model_name,
                    upstream_model=_string(raw_model.get("litellm_model")),
                    deployment_id=_string(raw_model.get("deployment_id")),
                    api_base=_string(raw_model.get("api_base")) or provider_base,
                    api_key=_credential_for_model(raw_provider, raw_model),
                )
            )
    return targets


def _service_root(api_base: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(api_base)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None

    parts = [part for part in parsed.path.split("/") if part]
    lower = [part.lower() for part in parts]
    suffixes = (
        ("v1", "chat", "completions"),
        ("v1", "images", "generations"),
        ("v1", "completions"),
        ("v1", "responses"),
        ("v1", "messages"),
        ("v1", "models"),
        ("chat", "completions"),
        ("images", "generations"),
        ("completions",),
        ("responses",),
        ("messages",),
        ("models",),
        ("v1",),
    )
    for suffix in suffixes:
        if len(lower) >= len(suffix) and tuple(lower[-len(suffix) :]) == suffix:
            parts = parts[: -len(suffix)]
            break
    path = f"/{'/'.join(parts)}" if parts else ""
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


def _endpoint_candidates(api_base: str) -> list[tuple[str, str]]:
    root = _service_root(api_base)
    if root is None:
        return []
    root_path = urllib.parse.urlsplit(root).path.rstrip("/").lower()
    newapi_usage_path = "/usage/token/" if root_path.endswith("/api") else "/api/usage/token/"
    return [
        ("sub2api-v1-usage", f"{root}/v1/usage"),
        ("sub2api-key-billing", f"{root}/v1/sub2api/billing"),
        ("newapi-token-usage", f"{root}{newapi_usage_path}"),
    ]


def _billing_http_opener() -> urllib.request.OpenerDirector:
    """Build an isolated billing opener unless the user explicitly opts in."""
    handlers: list[Any] = [_NoRedirect()]
    if os.environ.get("LITELLM_USE_SYSTEM_PROXIES") != "1":
        # ProxyHandler({}) suppresses both inherited HTTP(S)_PROXY values and
        # platform proxy discovery for this credential-bearing request.
        handlers.insert(0, urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _default_open(request: urllib.request.Request, timeout: float) -> Any:
    return _billing_http_opener().open(request, timeout=timeout)


def _fetch_json(
    url: str,
    api_key: str,
    timeout: float,
    opener: HttpOpener,
) -> tuple[str, int | None, dict[str, Any] | None]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "LiteLLM-Menu-Billing/1",
        },
        method="GET",
    )
    try:
        with opener(request, timeout) as response:
            status_value = getattr(response, "status", None)
            if status_value is None:
                status_value = response.getcode()
            status = int(status_value)
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return "http", int(exc.code), None
    except (socket.timeout, TimeoutError):
        return "timeout", None, None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)) or "timeout" in str(
            exc.reason
        ).lower():
            return "timeout", None, None
        return "network", None, None
    except OSError:
        return "network", None, None

    if status < 200 or status >= 300:
        return "http", status, None
    if len(body) > MAX_RESPONSE_BYTES:
        return "invalid", status, None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "invalid", status, None
    return ("ok", status, payload) if isinstance(payload, dict) else ("invalid", status, None)


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return value if math.isfinite(float(value)) else None
        except OverflowError:
            return None
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def _display_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text or len(text) > 64:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        return None
    return text


def _data_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _browser_fallback_allowed(api_base: str) -> bool:
    """Require an explicit opt-in before invoking any browser automation."""

    setting = os.environ.get("LITELLM_BROWSER_BILLING", "").strip().lower()
    return setting in {"1", "true", "yes", "on", "enabled"}


def _browser_token_item(payload: dict[str, Any], api_key: str) -> dict[str, Any] | None:
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None
    items = [item for item in data["items"] if isinstance(item, dict)]
    if not items:
        return None
    for item in items:
        for field in ("key", "token", "value"):
            if item.get(field) == api_key:
                return item
    # Never attribute a row to a different credential merely because the server
    # returned one masked or unrelated item.
    if len(items) == 1 and items[0].get("__browser_match") is True:
        return items[0]
    return None


def _recognized_browser_billing(
    payload: dict[str, Any], api_key: str
) -> dict[str, Any] | None:
    item = _browser_token_item(payload, api_key)
    if item is None:
        return None
    # The browser bridge marks an exact key match before redacting the key;
    # never infer a match from an arbitrary singleton response in that path.
    if "__browser_match" in item and item.get("__browser_match") is not True:
        return None
    remain = _number(item.get("remain_quota"))
    used = _number(item.get("used_quota"))
    granted = _number(item.get("total_granted"))
    unlimited = item.get("unlimited_quota") is True
    if not unlimited and remain is None:
        return None

    multiplier = None
    for field in (
        "__browser_multiplier",
        "effective_rate_multiplier",
        "rate_multiplier",
        "multiplier",
    ):
        multiplier = _number(item.get(field))
        if multiplier is not None:
            break
    result: dict[str, Any] = {
        "source": "newapi-browser-token-search",
        "balance": None
        if unlimited
        else {"kind": "remaining_quota", "value": remain, "unit": "quota"},
        "usage": (
            {"used": used, "limit": granted, "unit": "quota"}
            if not unlimited and used is not None and granted is not None
            else None
        ),
        "group": _display_text(item.get("group")),
        "mode": _display_text(item.get("status")),
        "detail": UNLIMITED_QUOTA_DETAIL if unlimited else "Browser session billing data is available.",
    }
    if multiplier is not None:
        result["multiplier"] = {
            "status": "ok",
            "value": multiplier,
            "detail": "Effective model multiplier from the browser keys table.",
        }
    return result


def _recognized_billing(
    endpoint: str, payload: dict[str, Any]
) -> dict[str, Any] | None:
    data = _data_mapping(payload)
    object_name = _string(data.get("object")) or _string(payload.get("object"))
    if endpoint == "sub2api-v1-usage":
        quota = data.get("quota")
        if isinstance(quota, dict):
            limit = _number(quota.get("limit"))
            used = _number(quota.get("used"))
            remaining = _number(quota.get("remaining"))
            unit = _display_text(quota.get("unit")) or "provider_credit"
            if remaining is not None:
                return {
                    "source": "sub2api-v1-usage",
                    "balance": {"kind": "remaining_quota", "value": remaining, "unit": unit},
                    "usage": (
                        {"used": used, "limit": limit, "unit": unit}
                        if used is not None and limit is not None
                        else None
                    ),
                    "group": _display_text(data.get("planName")),
                    "mode": _display_text(data.get("mode")),
                }
        remaining = _number(data.get("remaining"))
        unit = _display_text(data.get("unit")) or "provider_credit"
        if remaining is not None:
            return {
                "source": "sub2api-v1-usage",
                "balance": {"kind": "balance", "value": remaining, "unit": unit},
                "usage": None,
                "group": _display_text(data.get("planName")),
                "mode": _display_text(data.get("mode")),
            }

    if endpoint == "sub2api-key-billing" and object_name == "sub2api.key_billing":
        multiplier = _number(data.get("effective_rate_multiplier"))
        if multiplier is None:
            return None
        return {
            "source": "sub2api-key-billing",
            "balance": None,
            "usage": None,
            "group": None,
            "mode": _display_text(data.get("billing_scope")),
            "multiplier": {
                "status": "ok",
                "value": multiplier,
                "detail": "Effective key billing multiplier.",
            },
        }

    total_granted = _number(data.get("total_granted"))
    total_used = _number(data.get("total_used"))
    total_available = _number(data.get("total_available"))
    if endpoint == "newapi-token-usage" and object_name == "token_usage":
        unlimited = data.get("unlimited_quota") is True
        if not unlimited and total_available is None:
            return None
        return {
            "source": "newapi-token-usage",
            "balance": None if unlimited else {
                "kind": "remaining_quota",
                "value": total_available,
                "unit": "quota",
            } if total_available is not None else None,
            "usage": (
                {"used": total_used, "limit": total_granted, "unit": "quota"}
                if not unlimited and total_used is not None and total_granted is not None
                else None
            ),
            "group": None,
            "mode": None,
            "detail": UNLIMITED_QUOTA_DETAIL if unlimited else None,
        }
    return None


def _multiplier_unavailable() -> dict[str, Any]:
    return {
        "status": "unavailable",
        "value": None,
        "detail": MULTIPLIER_UNAVAILABLE_DETAIL,
    }


def _account_result(
    status: str,
    detail: str,
    *,
    source: str | None = None,
    balance: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    group: str | None = None,
    mode: str | None = None,
    multiplier: dict[str, Any] | None = None,
    http_status: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "detail": detail,
        "source": source,
        "balance": balance,
        "usage": usage,
        "group": group,
        "mode": mode,
        "multiplier": multiplier or _multiplier_unavailable(),
    }
    if http_status is not None:
        result["http_status"] = http_status
    return result


def _failure_result(attempts: list[tuple[str, str, int | None]]) -> dict[str, Any]:
    if attempts and all(
        kind in {"unsupported", "http"} and http_status in {404, 405}
        for _, kind, http_status in attempts
    ):
        return _account_result(
            "unsupported",
            "This provider does not expose a supported billing endpoint.",
        )
    priority = (
        ("auth", "auth_error", "The provider rejected the configured credential."),
        ("rate", "rate_limited", "The provider rate-limited the billing request."),
        ("timeout", "timeout", "The provider did not answer before the billing timeout."),
        ("network", "network_error", "The provider could not be reached for billing."),
        ("http", "http_error", "The provider returned an HTTP error for billing."),
    )
    for needle, status, detail in priority:
        for endpoint, kind, http_status in reversed(attempts):
            if (
                needle == "auth"
                and kind == "http"
                and http_status in {401, 403}
            ):
                return _account_result(status, detail, http_status=http_status)
            if needle == "rate" and kind == "http" and http_status == 429:
                return _account_result(status, detail, http_status=http_status)
            if kind == needle:
                return _account_result(status, detail, http_status=http_status)
    return _account_result(
        "unsupported",
        "No supported billing response was available from this provider.",
    )


def probe_account(
    api_base: str,
    api_key: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener: HttpOpener = _default_open,
    browser_probe: BrowserProbe | None = _default_browser_probe,
) -> dict[str, Any]:
    if not api_base:
        return _account_result(
            "invalid_config", "The configured model has no provider API base."
        )
    if not api_key:
        return _account_result(
            "credential_unavailable",
            "The configured model has no usable provider credential in this environment.",
        )
    endpoints = _endpoint_candidates(api_base)
    if not endpoints:
        return _account_result(
            "invalid_config", "The configured model has an unsupported provider API base."
        )

    # The configured timeout is an account-wide budget. Unsupported endpoints
    # must not turn one UI refresh into several consecutive network timeouts.
    deadline = time.monotonic() + timeout
    attempts: list[tuple[str, str, int | None]] = []
    billing_multiplier: dict[str, Any] | None = None
    billing_result: dict[str, Any] | None = None
    for endpoint, url in endpoints:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            attempts.append((endpoint, "timeout", None))
            break
        kind, http_status, payload = _fetch_json(
            url,
            api_key,
            min(timeout, remaining),
            opener,
        )
        if kind == "ok" and payload is not None:
            recognized = _recognized_billing(endpoint, payload)
            if recognized is not None:
                if recognized["source"] == "sub2api-key-billing":
                    billing_multiplier = recognized.get("multiplier")
                else:
                    billing_result = recognized
                if endpoint == "newapi-token-usage":
                    break
            else:
                attempts.append((endpoint, "unsupported", http_status))
        else:
            attempts.append((endpoint, kind, http_status))

        # A recognized Sub2API balance is authoritative for this credential.
        # Its one companion endpoint is the documented multiplier source; a
        # New API endpoint cannot add useful data after that probe.
        if (
            endpoint == "sub2api-key-billing"
            and billing_result is not None
            and billing_result["source"] == "sub2api-v1-usage"
        ):
            break
    browser_allowed = browser_probe is not None and _browser_fallback_allowed(api_base)
    # Do not touch Chrome when the normal provider endpoint already supplied a
    # finite balance. The browser path is strictly a balance-recovery fallback.
    browser_needed = billing_result is None or billing_result.get("balance") is None
    if browser_allowed and browser_needed:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            try:
                kind, http_status, browser_payload = browser_probe(
                    api_base,
                    api_key,
                    timeout=min(timeout, remaining),
                )
            except Exception:
                kind, http_status, browser_payload = "unavailable", None, None
            if kind == "ok" and browser_payload is not None:
                recognized = _recognized_browser_billing(browser_payload, api_key)
                if recognized is not None:
                    if billing_result is None:
                        billing_result = recognized
                        billing_multiplier = recognized.get("multiplier")
                    else:
                        if billing_result.get("balance") is None:
                            billing_result["balance"] = recognized.get("balance")
                        if billing_result.get("usage") is None:
                            billing_result["usage"] = recognized.get("usage")
                        if billing_result.get("group") is None:
                            billing_result["group"] = recognized.get("group")
                        if billing_result.get("mode") is None:
                            billing_result["mode"] = recognized.get("mode")
                        if billing_multiplier is None:
                            billing_multiplier = recognized.get("multiplier")
                        billing_result["source"] = recognized.get(
                            "source", billing_result["source"]
                        )
                        if recognized.get("detail"):
                            billing_result["detail"] = recognized["detail"]

    if billing_result is not None:
        return _account_result(
            "ok",
            str(billing_result.get("detail") or "Live provider billing data is available."),
            source=str(billing_result["source"]),
            balance=billing_result["balance"],
            usage=billing_result["usage"],
            group=billing_result["group"],
            mode=billing_result["mode"],
            multiplier=billing_multiplier,
        )
    return _failure_result(attempts)


def _model_identity(target: BillingTarget) -> dict[str, str]:
    return {
        "name": target.model,
        "upstream_model": target.upstream_model,
        "deployment_id": target.deployment_id,
    }


def _model_billing(target: BillingTarget, account: dict[str, Any]) -> dict[str, Any]:
    model = _model_identity(target)
    model.update(
        {
            "status": account["status"],
            "detail": account["detail"],
            "source": account["source"],
            "balance": account["balance"],
            "usage": account["usage"],
            "multiplier": account["multiplier"],
        }
    )
    return model


def _provider_status(accounts: list[dict[str, Any]]) -> str:
    statuses = {str(account.get("status", "unsupported")) for account in accounts}
    if statuses == {"ok"}:
        return "ok"
    if "ok" in statuses:
        return "partial"
    for status in (
        "auth_error",
        "rate_limited",
        "timeout",
        "network_error",
        "http_error",
        "permission_required",
        "credential_unavailable",
        "invalid_config",
    ):
        if status in statuses:
            return status
    return "unsupported"


def collect_billing(
    path: pathlib.Path | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener: HttpOpener = _default_open,
    browser_probe: BrowserProbe | None = _default_browser_probe,
) -> dict[str, Any]:
    timeout = float(timeout)
    if not math.isfinite(timeout):
        raise ValueError("Billing timeout must be finite")
    timeout = min(MAX_TIMEOUT_SECONDS, max(MIN_TIMEOUT_SECONDS, timeout))
    if path is None:
        path = default_config_path()
    targets = active_billing_targets(path)
    by_provider: dict[str, list[BillingTarget]] = {}
    for target in targets:
        by_provider.setdefault(target.provider, []).append(target)

    provider_accounts: dict[str, list[list[BillingTarget]]] = {}
    probe_plan: list[tuple[str, int, BillingTarget]] = []
    for provider_name, provider_targets in by_provider.items():
        by_account: dict[tuple[str, str], list[BillingTarget]] = {}
        for target in provider_targets:
            by_account.setdefault((target.api_base, target.api_key), []).append(target)
        account_groups = list(by_account.values())
        provider_accounts[provider_name] = account_groups
        for account_index, account_targets in enumerate(account_groups):
            probe_plan.append((provider_name, account_index, account_targets[0]))

    deadline = time.monotonic() + timeout
    account_results: dict[tuple[str, int], dict[str, Any]] = {}
    limited_plan = probe_plan[:MAX_ACCOUNTS_PER_REFRESH]
    skipped_plan = probe_plan[MAX_ACCOUNTS_PER_REFRESH:]

    # Credential accounts are independent. Serial probing lets one slow provider
    # spend the whole UI refresh window and falsely time out every other account.
    # The account limit also bounds the number of concurrent credential-bearing
    # requests in a refresh.
    condition = threading.Condition()
    pending: set[tuple[str, int]] = set()

    def run_probe(key: tuple[str, int], representative: BillingTarget, request_timeout: float) -> None:
        try:
            result = probe_account(
                representative.api_base,
                representative.api_key,
                timeout=request_timeout,
                opener=opener,
                browser_probe=browser_probe,
            )
        except Exception:
            result = _account_result(
                "network_error",
                "The provider could not be reached for billing.",
            )
        with condition:
            if key in pending:
                account_results[key] = result
                pending.remove(key)
                condition.notify_all()

    if limited_plan:
        for provider_name, account_index, representative in limited_plan:
            remaining = deadline - time.monotonic()
            key = (provider_name, account_index)
            if remaining <= 0:
                account_results[key] = _account_result(
                    "timeout",
                    "Billing refresh reached its bounded account or time limit.",
                )
                continue
            with condition:
                pending.add(key)
            threading.Thread(
                target=run_probe,
                args=(key, representative, min(timeout, remaining)),
                name="litellm-menu-billing-probe",
                daemon=True,
            ).start()

        with condition:
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                condition.wait(timeout=remaining)
            timed_out = tuple(pending)
            pending.clear()
        for key in timed_out:
            account_results[key] = _account_result(
                "timeout",
                "Billing refresh reached its bounded account or time limit.",
            )

    for provider_name, account_index, _ in skipped_plan:
        account_results[(provider_name, account_index)] = _account_result(
            "timeout",
            "Billing refresh reached its bounded account or time limit.",
        )

    providers: list[dict[str, Any]] = []
    available_models = 0
    for provider_name, account_groups in provider_accounts.items():
        accounts: list[dict[str, Any]] = []
        models: list[dict[str, Any]] = []
        for account_index, account_targets in enumerate(account_groups):
            account = account_results[(provider_name, account_index)]
            account["models"] = [_model_identity(target) for target in account_targets]
            accounts.append(account)
            models.extend(_model_billing(target, account) for target in account_targets)
            available_models += sum(1 for target in account_targets if account["status"] == "ok")

        providers.append(
            {
                "name": provider_name,
                "status": _provider_status(accounts),
                "accounts": accounts,
                "models": models,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "providers": providers,
        "summary": {
            "providers": len(providers),
            "models": len(targets),
            "available_models": available_models,
            "unavailable_models": len(targets) - available_models,
        },
    }


def _error_payload() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "providers": [],
        "summary": {
            "providers": 0,
            "models": 0,
            "available_models": 0,
            "unavailable_models": 0,
        },
        "status": "invalid_config",
        "detail": "The LiteLLM configuration could not be read.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print sanitized live billing data for LiteLLM Menu models."
    )
    parser.add_argument("--config", default=str(default_config_path()))
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()
    try:
        payload = collect_billing(pathlib.Path(args.config).expanduser(), timeout=args.timeout)
    except (BillingConfigError, ValueError, TypeError):
        payload = _error_payload()
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
