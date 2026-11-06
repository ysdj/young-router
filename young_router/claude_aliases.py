"""Claude Desktop's model-picker aliases, derived rather than configured.

Claude Desktop on third-party (3P) builds its picker from ``inferenceModels``
and keeps only entries whose id reads as an Anthropic model, with no
competitor's brand in it.  Everything else is deleted from the picker, with a
warning per entry.  Verified on the app: ``deepseek-v4.1-flash`` and
``claude-gpt-test`` are removed; ``claude-opus-5-5``, ``claude-fast-v3`` and
``anthropic/claude-x`` are kept.

A router serves whatever public names its operator configured, which are
almost never Claude-shaped, so the picker would hold one entry at most.  The
answer every gateway in the wild reaches is the same: expose a Claude-shaped
id and keep the real model behind it (Any2Claude, Claude-Model-Proxy,
ThinkWatch, hanako's router).  This module is that mapping, and it is a
function of the public name rather than a table a person maintains, so a route
added later is covered without anybody editing anything.

The alias is derived for both sides from one function:

* the desktop view of ``/v1/models`` publishes the alias with the public name
  as ``display_name`` (the retired ``inferenceModels`` writer used the same
  alias with ``labelOverride``), which is how the picker reads
  ``deepseek-v4.1-flash``;
* the proxy resolves an incoming ``alias`` back to the public name before
  deployment selection, so the routing table, the canonical ``/v1/models``
  answer, and every other client stay exactly as they were.

Nothing is stored, so nothing can drift: the two sides agree because they
compute the same string.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable, Mapping

#: Brand tokens Claude Desktop refuses inside a model id, even behind a
#: ``claude-`` prefix.  Observed one id at a time: each of these was removed
#: from the picker while the same id with a neutral word in its place was kept.
#: An id it does not mention is assumed acceptable, which is the safe direction:
#: a name that is kept but unrecognised still shows, raw, in the picker.
REFUSED_ALIAS_TOKENS = frozenset(
    {
        "openai",
        "gpt",
        "chatgpt",
        "deepseek",
        "gemini",
        "bard",
        "kimi",
        "moonshot",
        "glm",
        "zhipu",
        "qwen",
        "dashscope",
        "grok",
        "xai",
        "llama",
        "mistral",
        "doubao",
        "bytedance",
        "yi",
        "minimax",
        "ernie",
        "baidu",
        # This app's own adapter prefixes never belong in a client-visible id.
        "anthropic",
    }
)

#: The family every derived alias is named under.  Claude Desktop resolves a
#: model's reasoning control from a small table of exact ids plus one pattern for
#: its future families -- ``/^(?:claude-)?(?:fable|mythos)(?:-|$)/`` -- and a
#: name under that pattern gets the full ``low``..``max`` ladder.  Verified on the
#: app: ``claude-fable-eb1394`` with a ``labelOverride`` showed the label *and* an
#: active Effort control reading ``Max``, while ``claude-sonnet-9-9`` (a made-up
#: version of a real family) showed neither.  Real Claude ids keep their own
#: entries and are never aliased.
#:
#: ``mythos`` rather than ``fable``: the picker prints one hardcoded subtitle per
#: family -- ``fable`` is "For your toughest challenges", ``mythos`` has no entry
#: in that table -- so the second spelling is the one that leaves the row reading
#: just the model's own name.  The reasoning ladder is identical for both.
ALIAS_FAMILY = "claude-mythos-"
ALIAS_PREFIX = ALIAS_FAMILY

#: Aliases this app published before ``mythos``.  A client's request can still
#: name one after an upgrade -- its picker holds the id it was given -- so the
#: resolver accepts the old family too and maps it back to the same route.
LEGACY_ALIAS_FAMILIES = ("claude-fable-",)


def _tokens(value: str) -> list[str]:
    return [token for token in re.split(r"[^a-z0-9]+", value.strip().lower()) if token]


def names_an_anthropic_model(name: object) -> bool:
    """Whether the id reads as an Anthropic model at all."""

    if not isinstance(name, str) or not name.strip():
        return False
    tokens = _tokens(name.rsplit("/", 1)[-1])
    return bool(tokens) and tokens[0] == "claude"


def desktop_keeps_model_id(name: object) -> bool:
    """Whether Claude Desktop keeps this id in its picker as written."""

    if not names_an_anthropic_model(name):
        return False
    assert isinstance(name, str)
    return not (set(_tokens(name.rsplit("/", 1)[-1])) - {"claude"} & REFUSED_ALIAS_TOKENS)


_ALIAS_DIGEST = re.compile(r"[0-9a-f]{6}$")

#: Claude Desktop's own 1M spelling: the picker offers ``<id>[1m]`` beside the
#: plain row, and the id it sends on a turn carries the suffix.  A gateway row
#: that reads as 1M gets it from its ``max_input_tokens``, so this suffix is the
#: client's, never a route the router serves.
#:
#: The desktop view publishes it in front of the client instead (see
#: ``proxy.claude_desktop_listing``): a row whose id is already suffixed does
#: not read as "offers 1M" to the client, so there is no second variant to
#: collapse, and the session still resolves the model at 1M.  Only the browser
#: client's own discovery sees that id; every other caller keeps the public
#: name, and ``context_suffix_free`` is how a request naming it is resolved.
CONTEXT_SUFFIX = "[1m]"
_CONTEXT_SUFFIX = re.compile(r"\[1m\]$", re.IGNORECASE)


def context_suffix_free(name: str) -> str:
    """The id without Claude Desktop's trailing ``[1m]`` marker."""

    return _CONTEXT_SUFFIX.sub("", name.strip())


def is_derived_alias(name: object) -> bool:
    """Whether an id is one of the aliases this app derives, old or current.

    The digest is six lowercase hexadecimal characters, so a real Claude id
    (``claude-fable-5``) does not match.  The Desktop migration reads this as
    the mark of a list this app wrote when its bookkeeping record is missing.
    The picker's 1M spelling counts too: it is the same published id and the
    same mark.
    """

    if not isinstance(name, str):
        return False
    text = context_suffix_free(name).lower()
    for family in (ALIAS_FAMILY, *LEGACY_ALIAS_FAMILIES):
        if text.startswith(family) and _ALIAS_DIGEST.fullmatch(text[len(family):]):
            return True
    return False


def claude_desktop_alias(public_name: str) -> str:
    """The id Claude Desktop sees for one public model name.

    Deterministic in the public name and injective in practice: the digest is
    taken over the name, so two routes cannot collide.  The id is a bare
    hexadecimal tail, which is also what keeps it free of every refused brand --
    and the picker never shows it, because the listing carries the public name
    as ``display_name``.
    """

    text = str(public_name or "").strip()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:6]
    return f"{ALIAS_FAMILY}{digest}"


#: Alias index for one observation of the router's model list.  The table is
#: rebuilt only when that list changes, because this runs on every request and
#: a served name would otherwise cost a digest per route per call.
_INDEX_CACHE: tuple[tuple[str, ...], dict[str, str]] | None = None
_LEGACY_INDEX_CACHE: tuple[tuple[str, ...], dict[str, str]] | None = None


def alias_index(public_names: Iterable[str]) -> dict[str, str]:
    """Alias -> public name for every public model that needs one.

    A public name Desktop already keeps is not aliased: written as itself it
    renders with the model's own display name and Effort control, which is
    better than any label this app could supply.
    """

    global _INDEX_CACHE
    names = tuple(str(name or "").strip() for name in public_names)
    if _INDEX_CACHE is not None and _INDEX_CACHE[0] == names:
        return _INDEX_CACHE[1]
    index: dict[str, str] = {}
    for name in names:
        if not name or desktop_keeps_model_id(name):
            continue
        index[claude_desktop_alias(name)] = name
    _INDEX_CACHE = (names, index)
    return index


def legacy_alias_index(public_names: Iterable[str]) -> dict[str, str]:
    """Alias -> public name for the families earlier releases published.

    The digest is the same one ``claude_desktop_alias`` derives; only the
    prefix differs, so a picker that still holds ``claude-mythos-<digest>``
    reaches the route it named before this app renamed the family.
    """

    global _LEGACY_INDEX_CACHE
    names = tuple(str(name or "").strip() for name in public_names)
    if _LEGACY_INDEX_CACHE is not None and _LEGACY_INDEX_CACHE[0] == names:
        return _LEGACY_INDEX_CACHE[1]
    index: dict[str, str] = {}
    for name in names:
        if not name or desktop_keeps_model_id(name):
            continue
        digest = claude_desktop_alias(name).removeprefix(ALIAS_FAMILY)
        for family in LEGACY_ALIAS_FAMILIES:
            index[f"{family}{digest}"] = name
    _LEGACY_INDEX_CACHE = (names, index)
    return index


def resolve_alias(model: object, public_names: Iterable[str]) -> str | None:
    """The public model an incoming ``model`` names, or ``None``.

    Only an id that is exactly one derived alias resolves.  Anything else --
    a real public name, a deployment id, a name this router does not serve --
    is left for the router to answer as it always has.

    The picker's 1M spelling resolves too.  Desktop offers a route that reads
    as 1M as ``<alias>`` plus ``<alias>[1m]`` and sends the suffixed id, which
    is not a route this router serves: without this the chosen row answered
    ``Invalid model name`` (one real session picked the 1M row for 11 of its 12
    turns).  The suffix only selects a row in the client; it is stripped, so
    the public name reaches the router and the upstream exactly as before.
    """

    if not isinstance(model, str) or not model.strip():
        return None
    names = tuple(str(name or "").strip() for name in public_names)
    index = alias_index(names)
    legacy = legacy_alias_index(names)
    for text in (model.strip(), context_suffix_free(model)):
        if not text:
            continue
        resolved = index.get(text) or legacy.get(text)
        if resolved is not None:
            return resolved
    return None


def rewrite_request_model(data: Mapping[str, Any]) -> str | None:
    """Point a Claude Desktop alias at the public model it names.

    Returns the public name when the request was rewritten, so the caller can
    trace it; ``None`` means the request was already addressed by a name the
    router serves.
    """

    try:
        from litellm.proxy import proxy_server as _proxy_server_module

        router = getattr(_proxy_server_module, "llm_router", None)
    except Exception:
        return None
    if router is None:
        return None
    model = data.get("model")
    if not isinstance(model, str) or not model.strip():
        return None
    resolved = resolve_alias(model, public_model_names(router))
    if resolved is None:
        return None
    # LiteLLM accepts a mutable mapping here and reads ``model`` from it after
    # this hook returns, so the swap has to be written back rather than
    # returned as a new object.
    try:
        data["model"] = resolved  # type: ignore[index]
    except TypeError:
        return None
    return resolved


def public_model_names(router: object) -> list[str]:
    """The model names the running router serves, from its own table."""

    try:
        entries = getattr(router, "model_list", None)
    except Exception:
        return []
    if not isinstance(entries, list):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        name = entry.get("model_name") if isinstance(entry, Mapping) else None
        if isinstance(name, str) and name.strip() and name.strip() not in seen:
            seen.add(name.strip())
            names.append(name.strip())
    return names


__all__ = [
    "ALIAS_FAMILY",
    "ALIAS_PREFIX",
    "LEGACY_ALIAS_FAMILIES",
    "REFUSED_ALIAS_TOKENS",
    "alias_index",
    "claude_desktop_alias",
    "context_suffix_free",
    "desktop_keeps_model_id",
    "is_derived_alias",
    "legacy_alias_index",
    "names_an_anthropic_model",
    "public_model_names",
    "rewrite_request_model",
    "resolve_alias",
]
