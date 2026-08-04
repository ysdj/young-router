#!/usr/bin/env python3
"""Read live, credential-scoped multipliers for LiteLLM Menu models.

The command intentionally recognizes response shapes instead of provider names or
hosts. It never writes a cache: each invocation is a bounded live read, and
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
MAX_CREDENTIALS_PER_REFRESH = 24
MAX_RESPONSE_BYTES = 256 * 1024
MULTIPLIER_UNAVAILABLE_DETAIL = "The provider does not expose a model multiplier to this credential."


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


def _multiplier_endpoint(api_base: str) -> str | None:
    root = _service_root(api_base)
    if root is None:
        return None
    return f"{root}/v1/sub2api/billing"


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
            "User-Agent": "LiteLLM-Menu-Multiplier/1",
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


def _multiplier_unavailable() -> dict[str, Any]:
    return {
        "status": "unavailable",
        "value": None,
        "detail": MULTIPLIER_UNAVAILABLE_DETAIL,
    }


def _multiplier_result(
    status: str,
    detail: str,
    *,
    source: str | None = None,
    multiplier: dict[str, Any] | None = None,
    http_status: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "detail": detail,
        "source": source,
        "multiplier": multiplier or _multiplier_unavailable(),
    }
    if http_status is not None:
        result["http_status"] = http_status
    return result


def _failure_result(kind: str, http_status: int | None) -> dict[str, Any]:
    if kind == "http" and http_status in {404, 405}:
        return _multiplier_result(
            "unsupported",
            "This provider does not expose an effective multiplier endpoint.",
        )
    priority = (
        ("auth", "auth_error", "The provider rejected the configured credential."),
        ("rate", "rate_limited", "The provider rate-limited the billing request."),
        ("timeout", "timeout", "The provider did not answer before the multiplier timeout."),
        ("network", "network_error", "The provider could not be reached for the multiplier."),
        ("http", "http_error", "The provider returned an HTTP error for the multiplier."),
    )
    for needle, status, detail in priority:
        if needle == "auth" and kind == "http" and http_status in {401, 403}:
            return _multiplier_result(status, detail, http_status=http_status)
        if needle == "rate" and kind == "http" and http_status == 429:
            return _multiplier_result(status, detail, http_status=http_status)
        if kind == needle:
            return _multiplier_result(status, detail, http_status=http_status)
    return _multiplier_result(
        "unsupported",
        "No supported effective multiplier response was available from this provider.",
    )


def _key_multiplier(payload: dict[str, Any]) -> int | float | None:
    if _string(payload.get("object")) != "sub2api.key_billing":
        return None
    if _string(payload.get("billing_scope")) != "token":
        return None
    return _number(payload.get("effective_rate_multiplier"))


def probe_model(
    target: BillingTarget,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener: HttpOpener = _default_open,
) -> dict[str, Any]:
    if not target.api_base:
        return _multiplier_result(
            "invalid_config", "The configured model has no provider API base."
        )
    if not target.api_key:
        return _multiplier_result(
            "credential_unavailable",
            "The configured model has no usable provider credential in this environment.",
        )
    endpoint = _multiplier_endpoint(target.api_base)
    if endpoint is None:
        return _multiplier_result(
            "invalid_config", "The configured model has an unsupported provider API base."
        )
    kind, http_status, payload = _fetch_json(endpoint, target.api_key, timeout, opener)
    if kind != "ok" or payload is None:
        return _failure_result(kind, http_status)
    multiplier = _key_multiplier(payload)
    if multiplier is None:
        return _multiplier_result(
            "unsupported",
            "This credential does not expose an effective key multiplier.",
        )
    return _multiplier_result(
        "ok",
        "Live effective key multiplier is available.",
        source="sub2api-key-billing",
        multiplier={
            "status": "ok",
            "value": multiplier,
            "detail": "Effective key billing multiplier.",
        },
    )


def _model_identity(target: BillingTarget) -> dict[str, str]:
    return {
        "name": target.model,
        "upstream_model": target.upstream_model,
        "deployment_id": target.deployment_id,
    }


def _model_billing(target: BillingTarget, result: dict[str, Any]) -> dict[str, Any]:
    model = _model_identity(target)
    model.update(
        {
            "status": result["status"],
            "detail": result["detail"],
            "source": result["source"],
            "multiplier": result["multiplier"],
        }
    )
    if "http_status" in result:
        model["http_status"] = result["http_status"]
    return model


def _multiplier_identity(target: BillingTarget) -> tuple[str, str]:
    """Return the credential-scoped endpoint identity for a model target."""

    return (_multiplier_endpoint(target.api_base) or target.api_base, target.api_key)


def _provider_status(models: list[dict[str, Any]]) -> str:
    statuses = {str(model.get("status", "unsupported")) for model in models}
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

    target_identities: dict[BillingTarget, tuple[str, str]] = {}
    probe_targets: dict[tuple[str, str], BillingTarget] = {}
    for provider_targets in by_provider.values():
        for target in provider_targets:
            identity = _multiplier_identity(target)
            target_identities[target] = identity
            probe_targets.setdefault(identity, target)

    deadline = time.monotonic() + timeout
    results: dict[tuple[str, str], dict[str, Any]] = {}
    probe_plan = list(probe_targets.items())
    limited_plan = probe_plan[:MAX_CREDENTIALS_PER_REFRESH]
    skipped_plan = probe_plan[MAX_CREDENTIALS_PER_REFRESH:]

    # Multiplier reads are independent. Serial probing lets one slow provider
    # spend the whole UI refresh window and falsely time out every other
    # credential. Each credential is queried once, regardless of model count.
    condition = threading.Condition()
    pending: set[tuple[str, str]] = set()

    def run_probe(
        identity: tuple[str, str], target: BillingTarget, request_timeout: float
    ) -> None:
        try:
            result = probe_model(
                target,
                timeout=request_timeout,
                opener=opener,
            )
        except Exception:
            result = _multiplier_result(
                "network_error",
                "The provider could not be reached for the multiplier.",
            )
        with condition:
            if identity in pending:
                results[identity] = result
                pending.remove(identity)
                condition.notify_all()

    if limited_plan:
        for identity, target in limited_plan:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                results[identity] = _multiplier_result(
                    "timeout",
                    "Multiplier refresh reached its bounded credential or time limit.",
                )
                continue
            with condition:
                pending.add(identity)
            threading.Thread(
                target=run_probe,
                args=(identity, target, min(timeout, remaining)),
                name="litellm-menu-multiplier-probe",
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
        for identity in timed_out:
            results[identity] = _multiplier_result(
                "timeout",
                "Multiplier refresh reached its bounded credential or time limit.",
            )

    for identity, _ in skipped_plan:
        results[identity] = _multiplier_result(
            "timeout",
            "Multiplier refresh reached its bounded credential or time limit.",
        )

    providers: list[dict[str, Any]] = []
    available_models = 0
    for provider_name, provider_targets in by_provider.items():
        models: list[dict[str, Any]] = []
        for target in provider_targets:
            result = results[target_identities[target]]
            models.append(_model_billing(target, result))
            available_models += int(result["status"] == "ok")

        providers.append(
            {
                "name": provider_name,
                "status": _provider_status(models),
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
        description="Print sanitized live multipliers for LiteLLM Menu models."
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
