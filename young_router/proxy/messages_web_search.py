"""Serve Claude Code's web search on the Messages wire.

``WebSearch`` is not a client-executed tool.  A call in Claude Code's main loop
spawns a second Messages request whose whole body is a search: system prompt
``You are an assistant for performing a web search tool use``, one user message
``Perform a web search for the query: <query>``, and one extra declaration
``{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}``
(``src/tools/WebSearchTool/WebSearchTool.ts``).  The provider is expected to run
the search inside that turn and answer with ``server_tool_use`` +
``web_search_tool_result`` blocks; the CLI reads nothing else back
(``makeOutputFromSearchResponse``).  The newer shape -- a client tool literally
named ``WebSearch`` -- still ends in the same side request, so this is the only
place the search can be served.

Codex reaches a search through ``POST {base}/alpha/search``, which
``search_endpoint`` answers from the same backends (a DeepSeek route's own
``/v1/tools/search`` first, then a Claude route's server-side search, then the
gateway's own route).  This module is the Messages half of that route: it takes
the query the client already stated, asks those backends for the pages, hands
them to the model as evidence so the answer is written from something real,
and publishes the two blocks the client's own parser reads.

Three boundaries:

* A route that runs Anthropic's server-side search itself keeps its own answer.
  The declaration is left exactly as the client sent it, and this pass returns
  ``None``; a group whose first deployment is a Claude route is not served
  here.
* The declaration is removed from the request that reaches the model.  A
  Messages request bridged to Chat turns it into a function tool the model may
  call and the client cannot answer, and a Chat validator that requires
  ``input_schema`` refuses the whole body (422 ``tools.0.input_schema: Field
  required``) -- so keeping it would trade a search that runs for a request that
  never does.
* Nothing is invented when the backend answered nothing: the request is left
  alone rather than given a "search" that produced no page.

Whether the client sees the blocks at all is a delivery question: the streaming
wire is rewrapped in ``patches`` (``anthropic_sse``), and a whole-body answer on
a native Anthropic route is assembled by ``anthropic_non_stream``.  A
whole-body answer that a Chat route converted has no Anthropic frames to edit;
the search still ran and the answer still carries the pages.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlsplit

from . import search_endpoint as _search_endpoint_module

#: Anthropic's own declaration names a dated tool: ``web_search_20250305``,
#: ``web_search_20260209``.  The OpenAI-hosted ``web_search_preview`` shares the
#: prefix but is a different declaration on a different wire, so the shape is
#: matched exactly rather than by prefix.
_NATIVE_SEARCH_TOOL_RE = re.compile(r"^web_search_\d{8}$")
#: The sentence the CLI builds around the query it is searching for.  The same
#: prompt is what DeepSeek Harness's search provider sends (its text arrives
#: here through ``_with_dsh_messages_plaintext_content_restored``).
_SIDE_QUERY_RE = re.compile(
    r"web search for the query:\s*(.+)",
    re.IGNORECASE | re.DOTALL,
)
#: The backend's own bound (``/v1/tools/search`` refuses a longer query with
#: HTTP 400); ``search_endpoint`` applies the same one to Codex's queries.
MAX_QUERY_CHARS = 128
#: Top-level marker carrying the blocks a later pass publishes.  The prefix is
#: on ``base._INTERNAL_REQUEST_KEY_PREFIXES``, so the last hook before the
#: provider strips it and no upstream validator ever sees it.
MARKER_KEY = "_young_router_messages_web_search"


def native_declarations(tools: Any) -> list[dict[str, Any]]:
    """The Anthropic-native web-search declarations a Messages body carries."""

    if not isinstance(tools, list):
        return []
    declarations: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        tool_type = tool.get("type")
        if isinstance(tool_type, str) and _NATIVE_SEARCH_TOOL_RE.match(tool_type):
            declarations.append(dict(tool))
    return declarations


def _content_text(content: Any) -> str:
    """Every text piece of one message, in order."""

    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    pieces: list[str] = []
    for block in content:
        if isinstance(block, str):
            pieces.append(block)
            continue
        if not isinstance(block, Mapping):
            continue
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            pieces.append(text)
    return "\n".join(pieces)


def latest_user_text(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, Mapping) and message.get("role") == "user":
            return _content_text(message.get("content"))
    return ""


def query_from_messages(messages: Any) -> Optional[str]:
    """The query the client stated, taken only when it stated one.

    The search side request is generated by the client, so the query is in its
    own sentence.  A request that does not carry that sentence is a main-loop
    turn whose model has not chosen a query yet; guessing one ("the last user
    message") would answer a question the model never asked, so it is left
    alone.
    """

    text = latest_user_text(messages)
    if not text:
        return None
    match = _SIDE_QUERY_RE.search(text)
    if match is None:
        return None
    query = " ".join(match.group(1).split())
    if not query:
        return None
    return query[:MAX_QUERY_CHARS]


def _host(url: Any) -> str:
    if not isinstance(url, str) or not url.strip():
        return ""
    try:
        return (urlsplit(url.strip()).hostname or "").casefold()
    except ValueError:
        return ""


def _matches_domain(host: str, domains: Iterable[str]) -> bool:
    if not host:
        return False
    for domain in domains:
        if not isinstance(domain, str):
            continue
        wanted = domain.strip().casefold().lstrip(".")
        if not wanted:
            continue
        if host == wanted or host.endswith("." + wanted):
            return True
    return False


def _declared_domains(declaration: Mapping[str, Any], key: str) -> list[str]:
    value = declaration.get(key)
    if not isinstance(value, list):
        return []
    return [domain for domain in value if isinstance(domain, str) and domain.strip()]


def filtered_rows(
    rows: Iterable[Mapping[str, Any]], declaration: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """The rows this declaration's domain filter allows.

    Claude Code puts its own ``allowed_domains``/``blocked_domains`` input on
    the declaration and refuses both at once, so one of the two lists is
    applied here.  The search backend has no such parameter: the page list is
    filtered after it answered.  A filter that leaves nothing is a fact the
    model has to be told, not a reason to pretend the search never ran.
    """

    allowed = _declared_domains(declaration, "allowed_domains")
    blocked = _declared_domains(declaration, "blocked_domains")
    kept: list[dict[str, Any]] = []
    for row in rows:
        host = _host(row.get("url"))
        if blocked and _matches_domain(host, blocked):
            continue
        if allowed and not _matches_domain(host, allowed):
            continue
        kept.append(dict(row))
    return kept


def _row_title(row: Mapping[str, Any]) -> str:
    title = str(row.get("title") or "").strip()
    return title or str(row.get("url") or "").strip()


def evidence_text(
    query: str,
    rows: list[dict[str, Any]],
    declaration: Mapping[str, Any],
) -> str:
    """What the model is told before it answers the search side request.

    The pages are listed whole (title, URL, the backend's own snippet) instead
    of being summarised here: the model that reads them is the one that writes
    the answer, and a summary produced by this proxy would be a second model's
    account of pages it never opened.
    """

    lines = [f'The web search for "{query}" already ran before this turn.']
    if not rows:
        allowed = _declared_domains(declaration, "allowed_domains")
        blocked = _declared_domains(declaration, "blocked_domains")
        restriction = ""
        if allowed:
            restriction = f" (only these domains were allowed: {', '.join(allowed)})"
        elif blocked:
            restriction = f" (these domains were excluded: {', '.join(blocked)})"
        lines.append("It returned no usable page" + restriction + ".")
        lines.append(
            "Answer the request above without inventing links, and say plainly "
            "that the search found nothing usable."
        )
        return "\n".join(lines)
    lines.append("These are the pages it returned:")
    lines.append("")
    for index, row in enumerate(rows, start=1):
        lines.append(f"{index}. {_row_title(row)}")
        lines.append(f"   {row.get('url')}")
        snippet = " ".join(str(row.get("snippet") or "").split())
        if snippet:
            lines.append(f"   {snippet}")
    lines.append("")
    lines.append(
        "Answer the request above from these pages. Cite a page as a markdown "
        'link next to the claim it supports, and end the answer with a '
        '"Sources:" section listing the pages you used. Do not search again.'
    )
    return "\n".join(lines)


def result_blocks(
    query: str, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The pair a client reads its sources from.

    ``server_tool_use`` and ``web_search_tool_result`` are one exchange: the
    result names the id of the call that produced it, and Claude Code groups
    its output by that id.  A ``toolu_`` id would be read as a client call the
    CLI is expected to execute, which is why the prefix is ``srvtoolu_``.
    """

    tool_use_id = f"srvtoolu_{uuid.uuid4().hex}"
    return [
        {
            "type": "server_tool_use",
            "id": tool_use_id,
            "name": "web_search",
            "input": {"query": query},
        },
        {
            "type": "web_search_tool_result",
            "tool_use_id": tool_use_id,
            "content": [
                {
                    "type": "web_search_result",
                    "title": _row_title(row),
                    # The client's own reader type keeps both fields on every
                    # result; a missing ``page_age`` is a shape a strict
                    # parser refuses, while null says "the backend did not
                    # state one".
                    "url": str(row.get("url") or ""),
                    "encrypted_content": "",
                    "page_age": None,
                }
                for row in rows
            ],
        },
    ]


def _with_appended_text(content: Any, text: str) -> list[dict[str, Any]]:
    """Put the evidence in the message the query is stated in.

    The evidence joins the last text block instead of opening a second one: the
    client's message is a list of blocks with one of them, and a second text
    block would be a shape this pass invented.  A Chat bridge that reads a
    single text block can send the whole thing as one string, while one that
    reads two has to send parts -- and a relay that only accepts
    ``content: string`` then refuses a body that would otherwise have worked.
    A message that ends in a picture or a tool result has no text block to
    extend, so its evidence is the one block it adds.
    """

    separator = "\n\n"
    blocks: list[dict[str, Any]] = []
    if isinstance(content, str):
        if content.strip():
            blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                blocks.append({"type": "text", "text": block})
            elif isinstance(block, Mapping):
                blocks.append(dict(block))
    for block in reversed(blocks):
        if block.get("type") != "text":
            continue
        existing = block.get("text")
        block["text"] = (
            f"{existing}{separator}{text}"
            if isinstance(existing, str) and existing.strip()
            else text
        )
        return blocks
    blocks.append({"type": "text", "text": text})
    return blocks


def _strip_declarations(
    request_data: Mapping[str, Any], declarations: list[dict[str, Any]]
) -> tuple[Optional[list[Any]], bool]:
    """The tool list without the declarations this pass answered."""

    tools = request_data.get("tools")
    if not isinstance(tools, list):
        return None, False
    removed_types = {
        str(declaration.get("type")) for declaration in declarations
    }
    kept = [
        tool
        for tool in tools
        if not (
            isinstance(tool, Mapping)
            and str(tool.get("type")) in removed_types
        )
    ]
    return kept, len(kept) != len(tools)


def rewritten_request(
    request_data: Mapping[str, Any],
    query: str,
    rows: list[dict[str, Any]],
    declarations: list[dict[str, Any]],
) -> dict[str, Any]:
    """The same turn with the search already done.

    The query stays where the client put it, so a model that answers the side
    request still sees the sentence it was built from; the pages and the
    instruction to use them follow in that same message.  A new user message
    would be a turn the client never wrote.
    """

    messages = request_data.get("messages")
    updated_messages: list[Any] = []
    appended = False
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, Mapping):
                updated_messages.append(dict(message))
            else:
                updated_messages.append(message)
        for message in reversed(updated_messages):
            if not isinstance(message, Mapping) or message.get("role") != "user":
                continue
            message["content"] = _with_appended_text(
                message.get("content"),
                evidence_text(query, rows, declarations[0] if declarations else {}),
            )
            appended = True
            break

    rewritten = dict(request_data)
    if appended:
        rewritten["messages"] = updated_messages
    kept_tools, tools_changed = _strip_declarations(request_data, declarations)
    if tools_changed:
        if kept_tools:
            rewritten["tools"] = kept_tools
        else:
            rewritten.pop("tools", None)
        tool_choice = rewritten.get("tool_choice")
        if isinstance(tool_choice, Mapping):
            chosen = str(tool_choice.get("name") or "")
            if any(str(tool.get("name") or "") == chosen for tool in declarations):
                rewritten.pop("tool_choice", None)
    rewritten[MARKER_KEY] = {
        "query": query,
        "blocks": result_blocks(query, rows),
    }
    return rewritten


def marked_blocks(request_data: Any) -> list[dict[str, Any]]:
    """The blocks a request's marker carries, when it carries any."""

    if not isinstance(request_data, Mapping):
        return []
    marker = request_data.get(MARKER_KEY)
    if not isinstance(marker, Mapping):
        return []
    blocks = marker.get("blocks")
    if not isinstance(blocks, list):
        return []
    return [dict(block) for block in blocks if isinstance(block, Mapping)]


def _served_by_a_claude_route(model: str) -> bool:
    """Whether the route that will answer this group runs the declaration.

    A Claude deployment executes Anthropic's server-side search itself, inside
    the client's own turn.  Replacing that with a second call this proxy makes
    -- and an answer written from pages a different search returned -- would be
    a downgrade, so such a group keeps the declaration untouched.  Anything
    else (a DeepSeek, GPT or Kimi route) cannot run it at all.

    Only the *first* deployment decides.  That is the one the turn goes to;
    reading "any Claude route in the group" instead would disable the search
    for a group whose own first route is the DeepSeek one, and leaving the
    declaration for it is the 422 this pass exists to avoid.
    """

    for entry in _search_endpoint_module._router_entries(model):
        params = _search_endpoint_module._deployment_params(entry)
        return _search_endpoint_module.is_anthropic_search_route(params)
    return False


async def prepare(request_data: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Answer a Messages request's search declaration before it is sent.

    Returns the rewritten body, or ``None`` when this request is not one this
    pass can serve -- no declaration, no stated query, a Claude route that runs
    the search itself, or a search backend that returned nothing.
    """

    if not isinstance(request_data, dict):
        return None
    declarations = native_declarations(request_data.get("tools"))
    if not declarations:
        return None
    query = query_from_messages(request_data.get("messages"))
    if query is None:
        return None
    model = request_data.get("model")
    if not isinstance(model, str) or not model.strip():
        return None
    if _served_by_a_claude_route(model):
        return None
    try:
        rows = await _search_endpoint_module.search_pages(
            model,
            [query],
            question=latest_user_text(request_data.get("messages")),
        )
    except Exception as exc:
        # A search that could not run is not a failed turn: the request goes on
        # with the declaration the client sent, and the route answers it or
        # refuses it exactly as it did before this pass existed.  Raising here
        # would turn a search outage into "this model does not answer".
        _search_endpoint_module._trace(
            "messages_web_search_failed",
            model=model,
            error=type(exc).__name__,
        )
        return None
    if not rows:
        # The backend answered nothing.  A rewritten request would look like a
        # search that ran and found the web silent.
        return None
    allowed = filtered_rows(rows, declarations[0])
    _search_endpoint_module._trace(
        "messages_web_search_served",
        model=model,
        query_chars=len(query),
        rows=len(allowed),
    )
    return rewritten_request(request_data, query, allowed, declarations)


def _search_requests(blocks: list[dict[str, Any]]) -> int:
    return sum(1 for block in blocks if block.get("type") == "web_search_tool_result")


def with_result_blocks(
    message: Any, blocks: list[dict[str, Any]]
) -> Any:
    """Put the search blocks in front of a whole-body Messages answer.

    The turn's usage states how many searches it ran (``server_tool_use``);
    without it a client that reports search counts reports a search that
    answered as zero.
    """

    if not blocks:
        return message
    if isinstance(message, Mapping):
        content = message.get("content")
        existing = list(content) if isinstance(content, list) else []
        updated = dict(message)
        updated["content"] = [*blocks, *existing]
        usage = updated.get("usage")
        if isinstance(usage, Mapping) and "server_tool_use" not in usage:
            updated["usage"] = {
                **usage,
                "server_tool_use": {"web_search_requests": _search_requests(blocks)},
            }
        return updated
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return message
    try:
        setattr(message, "content", [*blocks, *content])
    except (AttributeError, TypeError):
        return message
    return message
