"""Adapters and worker scripts for the third-party integrations this app
bundles (pi-web-access, Veridrop, WorkBuddy, dsh-vision-router).

A staged worker that serves a model endpoint publishes the base URL it is
reachable at as an ``os.environ/`` *reference*, never as a literal URL: the
port is chosen per process, and only the Core that owns the worker can resolve
it.  ``managed_base_references`` is how that fact is asked for without naming
any particular integration, so a rule that reasons about "an address this
deployment serves itself" has one place to look and a second worker only has to
add itself here.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping


# The bundled workers that serve a model endpoint, by adapter module name.  A
# worker adds itself here; nothing else in the app has to name it.
_MANAGED_ENDPOINT_MODULES = ("workbuddy",)


def _managed_modules() -> list[Any]:
    """Import each registered worker adapter, skipping one that is not staged.

    Imported lazily so that importing :mod:`young_router.adapters` never starts
    a worker and never fails because a staged integration is absent.
    """

    modules: list[Any] = []
    for module_name in _MANAGED_ENDPOINT_MODULES:
        try:
            modules.append(__import__(f"{__name__}.{module_name}", fromlist=["x"]))
        except Exception:
            continue
    return modules


def managed_base_references() -> frozenset[str]:
    """Every ``os.environ/`` base reference a bundled worker publishes."""

    references: set[str] = set()
    for module in _managed_modules():
        for name in _base_env_names(getattr(module, "API_BASE_ENV", None)):
            references.add(f"os.environ/{name}")
    return frozenset(references)


def managed_base_urls() -> tuple[str, ...]:
    """The addresses those references resolve to, as this process published them.

    A worker publishes the base URL it is reachable at, and a Core-side read
    has to be able to recognize that address even when it was resolved rather
    than passed as a reference.  The worker's own published map is the record,
    not the process environment: a test (or a restart) may have cleared the
    variable while the address is still the one this deployment serves.
    """

    urls: list[str] = []
    for module in _managed_modules():
        published = getattr(module, "published_environment", None)
        if not callable(published):
            continue
        try:
            values = published()
        except Exception:
            continue
        if not isinstance(values, dict):
            continue
        for name in _base_env_names(getattr(module, "API_BASE_ENV", None)):
            candidate = values.get(name)
            if isinstance(candidate, str) and candidate.strip():
                urls.append(candidate.strip())
    return tuple(urls)


def rate_provider_id(auth_kind: str) -> str:
    """The catalog id one rate-publishing account kind is read through.

    The worker's own document addresses its variants by id (``workbuddy``,
    ``workbuddy-ai``), which is not the same string as the auth kind the
    configuration stores.  The adapter that owns that protocol states the
    mapping, so a caller asks for it instead of naming the product.
    """

    for module in _managed_modules():
        mapping = getattr(module, "RATE_AUTH_KIND_TO_PROVIDER", None)
        if not isinstance(mapping, Mapping):
            continue
        candidate = mapping.get(str(auth_kind).strip())
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def rate_auth_kind_for_provider_id(provider_id: str) -> str:
    """The inverse: the auth kind a catalog id belongs to, or ``""``."""

    for module in _managed_modules():
        mapping = getattr(module, "RATE_AUTH_KIND_TO_PROVIDER", None)
        if not isinstance(mapping, Mapping):
            continue
        for auth_kind, candidate in mapping.items():
            if isinstance(candidate, str) and candidate.strip() == str(provider_id).strip():
                return str(auth_kind).strip()
    return ""


def model_rate(entry: object) -> float | None:
    """The rate one catalog entry bills at, or ``None`` when it states none.

    A catalog entry is a worker's own document shape, so interpreting it
    belongs to the adapter that owns that protocol rather than to each caller.
    The first registered worker that recognizes the entry answers.
    """

    for module in _managed_modules():
        reader = getattr(module, "model_rate", None)
        if not callable(reader):
            continue
        try:
            rate = reader(entry)
        except Exception:
            continue
        if rate is not None:
            return rate
    return None


def _base_env_names(value: object) -> Iterable[str]:
    if isinstance(value, dict):
        for item in value.values():
            if isinstance(item, str) and item.strip():
                yield item.strip()


def rate_publishing_auth_kinds() -> frozenset[str]:
    """Auth kinds whose account publishes a per-model rate for its routes.

    A rate is a property of the *account*, not of one integration: an account
    that states what each model costs lets a route follow that number the same
    way a relay group's multiplier is followed.  A worker that reads such a
    catalog declares the auth kinds here, and the order rules ask this question
    instead of naming a product.
    """

    kinds: set[str] = set()
    for module in _managed_modules():
        declared = getattr(module, "RATE_AUTH_KINDS", None)
        if isinstance(declared, (set, frozenset, tuple, list)):
            for kind in declared:
                if isinstance(kind, str) and kind.strip():
                    kinds.add(kind.strip())
    return frozenset(kinds)
