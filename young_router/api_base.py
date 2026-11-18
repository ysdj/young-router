from __future__ import annotations

import os
import ssl
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit
import urllib.request


_KNOWN_API_ENDPOINT_SUFFIXES = (
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
)


#: Services that mount the Anthropic Messages wire on a documented sibling
#: path instead of the OpenAI base most clients are configured with.
#:
#: DeepSeek publishes the same API at two documented bases: the OpenAI one
#: (``https://api.deepseek.com``, or its ``/v1`` compatibility spelling) and
#: the Anthropic one (``https://api.deepseek.com/anthropic``), whose Messages
#: endpoint is ``/anthropic/v1/messages``
#: (https://api-docs.deepseek.com/guides/anthropic_api).  LiteLLM's Anthropic
#: adapter appends ``/v1/messages`` to whatever base it is handed, so deriving
#: the Messages address from the OpenAI base sent every DeepSeek route to
#: ``https://api.deepseek.com/v1/messages`` — a path the service does not
#: mount.  The sibling path is a property of the service, so the table is keyed
#: by exact host: a lookalike host (``api.deepseek.com.example.test``) is a
#: different service, and a base that already names a service path (the
#: Anthropic root, a gateway prefix) is left exactly as configured.
_DOCUMENTED_ANTHROPIC_ROOTS = {
    "api.deepseek.com": "anthropic",
}


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


def trust_context() -> ssl.SSLContext:
    """The trust store every outbound HTTPS request verifies against.

    ``ssl.create_default_context()`` with no argument trusts the bundle the
    interpreter was compiled against, which on macOS is LibreSSL's
    ``/etc/ssl/cert.pem`` -- a store that predates the roots several stations
    now chain to.  Measured live (2026-11-18): ``https://flux-code.cc`` is
    Cloudflare-fronted over ``SSL.com TLS ECC Root CA 2022``, verifies in
    ``curl`` and in ``openssl s_client``, and fails here with "unable to get
    local issuer certificate".  A station read that fails that way is reported
    as an unreachable site, so the saved-session probe, the remembered-password
    sign-in, the key list and 获取模型 all failed at once -- the account could
    not come back, its slots lost the keys that give them their prefix, and a
    route could not be listed from a key the app already held.

    certifi carries the current Mozilla store and is already on Core's path
    (LiteLLM's runtime depends on it); ``SSL_CERT_FILE`` still wins for an
    installation that pins its own bundle, and a machine without certifi keeps
    the interpreter's store rather than losing TLS entirely.
    """

    cafile = os.environ.get("SSL_CERT_FILE", "").strip()
    if not cafile:
        try:
            import certifi  # noqa: PLC0415 - optional dependency of the runtime

            cafile = certifi.where()
        except Exception:
            cafile = ""
    return ssl.create_default_context(cafile=cafile or None)


def isolated_http_opener() -> urllib.request.OpenerDirector:
    """Build an opener that cannot redirect credential-bearing requests.

    The TLS context is stated here as well: an opener built without an
    ``HTTPSHandler`` falls back to urllib's own default context, which is the
    interpreter's stale bundle -- so every credential-bearing read (获取模型,
    probes, account catalogs) verified against yesterday's roots while the
    relay client verified against today's.
    """

    handlers: list[Any] = [_NoRedirect()]
    if os.environ.get("LITELLM_USE_SYSTEM_PROXIES") != "1":
        handlers.insert(0, urllib.request.ProxyHandler({}))
    handlers.append(urllib.request.HTTPSHandler(context=trust_context()))
    return urllib.request.build_opener(*handlers)


def service_root(value: Any) -> Optional[str]:
    """Return the HTTP service root behind a configured API base or endpoint."""

    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None

    parts = [part for part in parsed.path.split("/") if part]
    suffix = _matched_endpoint_suffix(parts)
    if suffix is not None:
        parts = parts[: -len(suffix)]
    elif parts and parts[-1].lower() == "v1":
        parts.pop()
    return _url_with_path(parsed, parts).rstrip("/")


def _split_api_url(value: Any) -> Optional[tuple[Any, list[str]]]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().rstrip("/")
    parsed = urlsplit(text)
    if not parsed.scheme or not parsed.netloc:
        if "://" in text:
            return None
        parsed = urlsplit(f"https://{text}")
    if not parsed.scheme or not parsed.netloc:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    return parsed, parts


def _matched_endpoint_suffix(parts: list[str]) -> Optional[tuple[str, ...]]:
    lowered = [part.lower() for part in parts]
    for suffix in _KNOWN_API_ENDPOINT_SUFFIXES:
        if len(lowered) >= len(suffix) and tuple(lowered[-len(suffix) :]) == suffix:
            return suffix
    return None


def _api_root_and_version(value: Any) -> Optional[tuple[Any, list[str], Optional[bool]]]:
    split = _split_api_url(value)
    if split is None:
        return None
    parsed, parts = split
    suffix = _matched_endpoint_suffix(parts)
    if suffix is None:
        return parsed, parts, None
    return parsed, parts[: -len(suffix)], suffix[0] == "v1"


def _url_with_path(parsed: Any, parts: list[str]) -> str:
    path = f"/{'/'.join(parts)}" if parts else ""
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _documented_anthropic_root(parsed: Any, root_parts: list[str]) -> Optional[str]:
    """The sibling Messages root for a documented service, if this base lacks a path.

    Only the service's OpenAI base spellings count: its bare root and the
    ``/v1`` compatibility root that is still just a path segment (a complete
    endpoint's version segment has already been stripped).  A configured path
    is the user's statement about where that service is mounted (a gateway
    prefix, the Anthropic root itself, or its full endpoint), so it is never
    rewritten from the table.
    """

    segment = _DOCUMENTED_ANTHROPIC_ROOTS.get((parsed.hostname or "").lower())
    if segment is None:
        return None
    if [part.lower() for part in root_parts] not in ([], ["v1"]):
        return None
    return segment


def normalize_configured_api_base(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("os.environ/"):
        # An environment reference resolves to a complete base URL in the
        # process that owns it (the WorkBuddy worker's loopback origin drives
        # both of its variants).  It is never a host to guess a scheme for.
        return text
    split = _split_api_url(value)
    if split is None:
        return str(value or "").strip().rstrip("/")
    parsed, parts = split
    if _matched_endpoint_suffix(parts) is not None:
        # An explicit endpoint records whether its server is versioned. Keep
        # that distinction until the selected protocol maps it to a request.
        return _url_with_path(parsed, parts).rstrip("/")
    if not parts or parts[-1].lower() != "v1":
        parts.append("v1")
    return _url_with_path(parsed, parts).rstrip("/")


def api_base_for_surface(value: Any, surface: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("os.environ/"):
        # See ``normalize_configured_api_base``: the resolved value already
        # carries the endpoint the surface adapter needs.
        return text
    root = _api_root_and_version(value)
    if root is None:
        return str(value or "").strip().rstrip("/")
    parsed, root_parts, endpoint_was_versioned = root
    surface_text = str(surface or "").strip().lower()

    if root_parts and root_parts[-1].lower() == "v1":
        versioned_parts = root_parts
    elif endpoint_was_versioned is False:
        versioned_parts = root_parts
    else:
        versioned_parts = [*root_parts, "v1"]

    if surface_text == "anthropic":
        # LiteLLM's Anthropic client appends `/v1/messages` unless that complete
        # endpoint is already present. Supplying the final endpoint prevents a
        # configured `/v1` base from becoming `/v1/v1/messages`.
        documented_root = _documented_anthropic_root(parsed, root_parts)
        if documented_root is not None:
            # The service documents its Messages wire under a sibling path, so
            # the OpenAI base is not the root to append to (DeepSeek answers
            # `/v1/messages` with 404 and serves `/anthropic/v1/messages`).
            return _url_with_path(
                parsed, [documented_root, "v1", "messages"]
            ).rstrip("/")
        anthropic_parts = [*versioned_parts]
        if endpoint_was_versioned is not False and (
            not anthropic_parts or anthropic_parts[-1].lower() != "v1"
        ):
            anthropic_parts.append("v1")
        anthropic_parts.append("messages")
        return _url_with_path(parsed, anthropic_parts).rstrip("/")
    if surface_text in {"openai/chat", "openai/responses"}:
        return _url_with_path(parsed, versioned_parts).rstrip("/")
    return normalize_configured_api_base(value)


def is_unversioned_anthropic_messages_endpoint(value: Any) -> bool:
    split = _split_api_url(value)
    if split is None:
        return False
    _, parts = split
    return bool(parts) and parts[-1].lower() == "messages" and (
        len(parts) == 1 or parts[-2].lower() != "v1"
    )


def apply_surface_api_base(request_kwargs: dict, surface: Any) -> bool:
    litellm_params = request_kwargs.get("litellm_params")
    if not isinstance(litellm_params, dict):
        return False
    configured = litellm_params.get("api_base")
    if not isinstance(configured, str) or not configured.strip():
        return False
    normalized = api_base_for_surface(configured, surface)
    if not normalized:
        return False
    updated = litellm_params.copy()
    updated["api_base"] = normalized
    request_kwargs["litellm_params"] = updated
    changed = normalized != configured or request_kwargs.get("api_base") != normalized
    request_kwargs["api_base"] = normalized
    metadata = request_kwargs.get("litellm_metadata")
    if isinstance(metadata, dict):
        updated_metadata = metadata.copy()
        updated_metadata["api_base"] = normalized
        request_kwargs["litellm_metadata"] = updated_metadata
        changed = changed or metadata.get("api_base") != normalized
    return changed
