"""Serve Codex's standalone web search, the endpoint its Lite lane calls.

Codex's Responses Lite surface never declares the hosted ``web_search`` tool;
it calls ``POST {provider}/alpha/search`` instead and feeds ``output`` back to
the model (``codex-api/src/endpoint/search.rs``, ``ext/web-search``).  Without
this route the request 404s and the agent searches by curling search engines
from the shell -- the failure mode that made every session look oblivious to
web tools.

Only the search itself goes to a backend, and neither borrows another route's
credential:

* A DeepSeek route searches through DeepSeek's own ``/tools/search`` with that
  route's key.
* Any other route forwards the declaration to the gateway it already talks to,
  which may implement ``alpha/search`` itself (several relays do).  A route
  that refuses it is remembered for a short while so the same rejection is not
  paid on every turn.

``open`` and ``find`` are *not* searches: they name a page, so the answer is
that page's text, read here (with a DeepSeek route's ``/tools/fetch`` as the
fallback reader when this host cannot reach it).  Forwarding them to a search
backend is what made ``web.run`` look like it searched the same nothing over
and over -- the relay's owner search answers an ``open`` with the site's whole
navigation tree and answers a ``find`` with that tree again, so the model got a
menu instead of the section it asked for and asked again.  The page reader
below is therefore part of the contract: prose instead of chrome, numbered
lines, ``lineno`` honoured, and every page it hands out addressable by a
``turnNviewM`` reference.

A route that can do neither answers with a *successful* result whose text tells
the model to use the browser it already has.  That is a deliberate shape: an
error makes the agent retry the same tool, while a terminal answer with a
concrete next step is the one thing the model can act on.

The tool's own contract lets one call carry several queries and then ask to
open or search inside the pages it just received.  A gateway keeps no state
this endpoint can reach, so the reference table below keeps every ``ref_id``
either side minted -- a later turn's ``open``/``find`` can still resolve the
page it names instead of silently returning nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
import uuid
from html.parser import HTMLParser
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import quote, urljoin

# ``fastapi`` and ``httpx`` live in the proxy runtime, not in every host that
# imports this module (the test suite runs without them), so they are imported
# where they are used instead of at module scope.
SEARCH_PATHS = ("/v1/alpha/search", "/alpha/search")
DEEPSEEK_HOST = "api.deepseek.com"
ALPHA_SEARCH_UNSUPPORTED_TTL_ENV = "YOUNG_ROUTER_ALPHA_SEARCH_UNSUPPORTED_TTL_SECONDS"
DEFAULT_UNSUPPORTED_TTL_SECONDS = 300.0
MAX_QUERIES = 4
MAX_RESULTS = 20
MAX_FIND_MATCHES = 20
# OpenAI's own answer separates result entries with this rule and nothing else
# -- no blank lines, no per-query headings.  Reproducing it exactly keeps the
# text a model already knows how to read.
RESULT_SEPARATOR = "-" * 80
# A local read is the fallback for a result the search backend cannot fetch
# itself.  It is a plain HTTP GET: no browser, no scripts, no credentials, and it
# presents the same browser identity as every other request the app makes.
LOCAL_FETCH_TIMEOUT_SECONDS = 20.0
LOCAL_FETCH_MAX_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 60.0
PAGE_CONTENT_LIMIT = 8000
# A reference is created by one search and replayed by a later open/find that
# may land on any of the proxy's workers, so it is kept in a small shared file
# next to the other runtime state instead of in this process's memory.
MAX_SESSIONS = 256
MAX_REFERENCES_PER_SESSION = 200
MAX_SHARED_REFERENCES = 4096
SHARED_REFERENCE_TTL_SECONDS = 7 * 24 * 60 * 60
SEARCH_STATE_FILE_ENV = "YOUNG_ROUTER_SEARCH_STATE_FILE"
# ``search`` names a result in a search answer; ``view`` names a page an
# ``open``/``find`` handed back, which is what a later ``find``/``click``
# addresses.  Both are minted here and both must resolve after the turn.
_REFERENCE_RE = re.compile(r"^turn\d+(?:search|view)\d+$", re.IGNORECASE)
_COUNT_BY_RESPONSE_LENGTH = {"short": 5, "medium": 10, "long": 20}
_DEFAULT_COUNT = 10

# (api_base, api_key) -> unix time the "no standalone search" verdict expires.
# A refusal is a property of the endpoint, not of the turn, so it outlives the
# request that observed it; a transient failure is never recorded here.
_unsupported_until: dict[tuple[str, str], float] = {}
_unsupported_lock = asyncio.Lock()

def _ttl_seconds() -> float:
    raw = os.environ.get(ALPHA_SEARCH_UNSUPPORTED_TTL_ENV, "")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_UNSUPPORTED_TTL_SECONDS


async def _remember_unsupported(key: tuple[str, str]) -> None:
    ttl = _ttl_seconds()
    if ttl <= 0:
        return
    async with _unsupported_lock:
        _unsupported_until[key] = time.time() + ttl


def _is_remembered_unsupported(key: tuple[str, str]) -> bool:
    expires = _unsupported_until.get(key)
    if expires is None:
        return False
    if expires <= time.time():
        _unsupported_until.pop(key, None)
        return False
    return True


def reset_unsupported_cache() -> None:
    """Test hook: drop every remembered refusal."""

    _unsupported_until.clear()


# Codex replays a ``ref_id`` for the follow-up ``open``/``find`` commands.
# OpenAI's implementation carries that state in ``encrypted_output``; here the
# response announces the reference and the page it names, per session in this
# process and in the shared file below so any worker can resolve it later.
_sessions: dict[str, dict[str, str]] = {}
_sessions_lock = threading.Lock()


def reset_references() -> None:
    """Test hook: drop every resolved page reference."""

    with _sessions_lock:
        _sessions.clear()


def _session_key(body: Mapping[str, Any]) -> str:
    for key in ("id", "session_id", "thread_id"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "codex-default"


def _session_for(key: str) -> dict[str, str]:
    with _sessions_lock:
        session = _sessions.get(key)
        if session is None:
            if len(_sessions) >= MAX_SESSIONS:
                _sessions.pop(next(iter(_sessions)), None)
            session = {}
            _sessions[key] = session
        return session


def _search_state_file_path() -> Optional[str]:
    configured = os.environ.get(SEARCH_STATE_FILE_ENV, "").strip()
    if configured:
        return configured
    recovery = os.environ.get("YOUNG_ROUTER_ROUTE_RECOVERY_STATE_FILE", "").strip()
    if not recovery:
        return None
    return os.path.join(os.path.dirname(recovery), "web-search-references.json")


def _clean_url(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url.startswith(("http://", "https://")):
        return None
    return url


def _shared_reference_url(ref_id: str) -> Optional[str]:
    path = _search_state_file_path()
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    references = payload.get("references") if isinstance(payload, Mapping) else None
    entry = references.get(ref_id) if isinstance(references, Mapping) else None
    url = entry.get("url") if isinstance(entry, Mapping) else entry
    return _clean_url(url)


def _remember_shared_references(references: Mapping[str, str]) -> None:
    path = _search_state_file_path()
    if not path or not references:
        return
    now = int(time.time())

    def update(payload: dict[str, Any]) -> None:
        stored = payload.get("references")
        stored = stored if isinstance(stored, dict) else {}
        cutoff = now - SHARED_REFERENCE_TTL_SECONDS
        clean: dict[str, dict[str, Any]] = {}
        for ref_id, entry in stored.items():
            if not isinstance(ref_id, str) or not _REFERENCE_RE.fullmatch(ref_id):
                continue
            url = entry.get("url") if isinstance(entry, dict) else entry
            updated_at = entry.get("updated_at") if isinstance(entry, dict) else now
            url = _clean_url(url)
            if not isinstance(updated_at, int) or updated_at < cutoff or not url:
                continue
            clean[ref_id] = {"url": url, "updated_at": updated_at}
        for ref_id, url in references.items():
            clean[ref_id] = {"url": url, "updated_at": now}
        if len(clean) > MAX_SHARED_REFERENCES:
            ordered = sorted(
                clean.items(),
                key=lambda item: int(item[1].get("updated_at") or 0),
                reverse=True,
            )
            clean = dict(ordered[:MAX_SHARED_REFERENCES])
        payload.clear()
        payload.update({"schema_version": 1, "references": clean})

    try:
        from . import state as _state_module

        _state_module._locked_json_state_update(path, update)
    except Exception:
        return


def remember_references(session: dict[str, str], references: Mapping[str, str]) -> None:
    """Publish this response's references to the session and the shared file."""

    for ref_id, url in references.items():
        session[ref_id] = url
    while len(session) > MAX_REFERENCES_PER_SESSION:
        session.pop(next(iter(session)), None)
    _remember_shared_references(references)


def resolve_reference(ref_id: str, session: Optional[dict[str, str]] = None) -> Optional[str]:
    url = _clean_url(ref_id)
    if url:
        return url
    ref = (ref_id or "").strip()
    if not ref:
        return None
    if session is not None and ref in session:
        return session[ref]
    if not _REFERENCE_RE.fullmatch(ref):
        return None
    shared = _shared_reference_url(ref)
    if shared and session is not None:
        session[ref] = shared
    return shared


def master_key(settings: Mapping[str, Any]) -> str:
    """The key this proxy hands its clients, resolved the way it is written."""

    value = settings.get("master_key") if isinstance(settings, Mapping) else None
    if isinstance(value, str) and value.startswith("os.environ/"):
        variable = value.removeprefix("os.environ/").strip()
        value = os.environ.get(variable) if variable else None
    return str(value or os.environ.get("LITELLM_MASTER_KEY") or "").strip()


def proxy_general_settings() -> Mapping[str, Any]:
    """LiteLLM's loaded settings, or an empty mapping outside its process.

    This module is imported by the test suite and by hosts that never start the
    proxy, so a missing LiteLLM must not turn an import into a crash.
    """

    try:
        from litellm.proxy import proxy_server
    except Exception:
        return {}
    settings = getattr(proxy_server, "general_settings", {}) or {}
    return settings if isinstance(settings, Mapping) else {}


def authorized(request: Any, settings: Mapping[str, Any]) -> bool:
    expected = master_key(settings)
    if not expected:
        return True
    header = request.headers.get("authorization") or ""
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return presented.strip() == expected


def deployment_api_base(params: Mapping[str, Any]) -> str:
    return str(params.get("api_base") or "").rstrip("/")


def deployment_api_key(params: Mapping[str, Any]) -> str:
    return str(params.get("api_key") or "")


def upstream_model_name(params: Mapping[str, Any]) -> str:
    """The model id this deployment sends, without LiteLLM's provider prefix."""

    value = str(params.get("model") or "")
    return value.split("/", 1)[-1] if "/" in value else value


def is_deepseek_deployment(params: Mapping[str, Any]) -> bool:
    base = deployment_api_base(params)
    return DEEPSEEK_HOST in base


def _commands(body: Mapping[str, Any]) -> Mapping[str, Any]:
    commands = body.get("commands")
    return commands if isinstance(commands, Mapping) else {}


def search_queries(body: Mapping[str, Any]) -> list[str]:
    """The queries Codex asked for, bounded the way the tool documents them."""

    raw = _commands(body).get("search_query")
    queries: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            text = item.get("q")
            if isinstance(text, str) and text.strip():
                queries.append(text.strip())
            if len(queries) >= MAX_QUERIES:
                break
    if queries:
        return queries
    commands = _commands(body)
    if commands.get("open") or commands.get("find"):
        # A follow-up turn opens or searches inside pages it already has; its
        # `input` is the user's question, not a new search term.
        return []
    # A turn may carry the query only as the request input.
    question = body.get("input")
    if isinstance(question, str) and question.strip():
        return [question.strip()[:128]]
    return []


# The tool's schema declares more commands than this endpoint answers.  Naming
# them back is what stops a model from believing a finance or screenshot call
# silently succeeded.
_SERVED_COMMANDS = {"search_query", "open", "find", "response_length"}
_UNSERVED_NOTES = {
    "image_query": "图片搜索没有执行，请改用文本搜索。",
    "click": "click 没有执行（本端点不维护页面内的编号链接），请直接 open 目标 URL。",
    "screenshot": "screenshot 没有执行（本端点不渲染 PDF），请直接 open 该页面读取文本。",
    "finance": "finance 没有执行（本端点没有行情数据）。",
    "weather": "weather 没有执行（本端点没有天气数据）。",
    "sports": "sports 没有执行（本端点没有赛程数据）。",
    "time": "time 没有执行（本端点不换算时间）。",
}


def unsupported_commands(body: Mapping[str, Any]) -> list[str]:
    """Commands in this call this endpoint cannot serve, in schema order."""

    commands = _commands(body)
    return [
        name
        for name in _UNSERVED_NOTES
        if name in commands and commands.get(name)
    ]


def result_count(body: Mapping[str, Any]) -> int:
    length = str(_commands(body).get("response_length") or "").strip().lower()
    return _COUNT_BY_RESPONSE_LENGTH.get(length, _DEFAULT_COUNT)


def _reference_requests(
    body: Mapping[str, Any], key: str, session: Optional[dict[str, str]] = None
) -> list[dict[str, str]]:
    """``open``/``find`` entries, each resolved to a URL or marked as missing.

    A bare ``ref_id`` is only usable while this process still remembers it; the
    caller reports that plainly instead of returning an empty page, because a
    silent miss looks like "the page had nothing" to the model.
    """

    raw = _commands(body).get(key)
    wanted: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return wanted
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        ref = str(item.get("ref_id") or "").strip()
        pattern = str(item.get("pattern") or "")
        if not ref:
            continue
        extra: dict[str, Any] = {}
        if key == "find":
            extra["pattern"] = pattern
        else:
            # ``lineno`` is where the model wants the page opened: the line the
            # previous excerpt ended at, or the section it came back for.  A
            # reader that ignores it always hands back the top of the page, so
            # the model never reaches what it asked to see.
            lineno = item.get("lineno")
            if isinstance(lineno, int) and not isinstance(lineno, bool) and lineno > 0:
                extra["lineno"] = lineno
        if ref.startswith("http://") or ref.startswith("https://"):
            wanted.append({"url": ref, **extra})
            continue
        url = resolve_reference(ref, session)
        if url is None:
            wanted.append({"missing_ref": ref})
            continue
        wanted.append({"url": url, **extra})
    return wanted


def open_requests(
    body: Mapping[str, Any], session: Optional[dict[str, str]] = None
) -> list[dict[str, str]]:
    return _reference_requests(body, "open", session)


def find_requests(
    body: Mapping[str, Any], session: Optional[dict[str, str]] = None
) -> list[dict[str, str]]:
    return _reference_requests(body, "find", session)


def deepseek_search_body(query: str, count: int, question: Optional[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {"query": query, "count": count}
    if question:
        payload["ori_question"] = question[:128]
    return payload


class _TextExtractor(HTMLParser):
    """Turn one HTML document into the prose a reader would actually read.

    Page chrome is most of a documentation page: npm's ``npm-prune`` renders
    343 lines, roughly 260 of them a navigation tree, so a model that opens the
    page to read one paragraph pays for the whole menu -- and then asks
    ``find`` for a heading it already had, because the first read looked empty.
    So the menu is not text here:

    * an ``<article>``/``<main>`` bounds the document when the page has one;
    * blocks whose text is almost entirely link labels are dropped;
    * a block repeated verbatim at the end is a table of contents drawn twice.
    """

    _SKIP = {
        "script", "style", "noscript", "template", "svg", "iframe", "nav",
        "header", "footer", "aside", "menu", "form", "button", "select",
        "dialog", "canvas", "video", "audio", "object",
    }
    # Only real block boundaries end a line.  Inline markup -- ``code``,
    # ``strong``, a heading's own anchor -- is part of the sentence around it;
    # breaking there turns "the node_modules folder" into three "lines" that
    # read as a broken page.  Table cells separate with a space instead.
    _BLOCK = {
        "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "article", "main", "pre", "table", "blockquote", "ul",
        "ol", "dd", "dt", "figcaption", "hr",
    }
    _CELL = {"td", "th"}
    _HEADING = {"h1", "h2", "h3", "h4", "h5", "h6"}
    # A block this short that is this much link label is a menu entry, not
    # prose.  Long blocks are kept even when they are links: a reference list
    # or a table of API names is content the reader asked for.
    _MENU_LINK_RATIO = 0.8
    _MENU_MAX_CHARS = 200

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self.title = ""
        self._scope: Optional[str] = None
        self._scope_depth = 0
        self._scoped = False
        self._parts: list[str] = []
        self._blocks: list[str] = []
        self._link_depth = 0
        self._link_chars = 0
        self._block_chars = 0
        self._heading_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if self._skip_depth:
            return
        if not self._scoped and tag in {"article", "main"}:
            self._scope = tag
            self._scope_depth = 1
            self._scoped = True
        elif self._scope is not None:
            self._scope_depth += 1
        if tag == "a":
            # Adjacent links are separate words ("jsoref Eomm lukekarrys"),
            # and an inline link never glues itself to the word before it.
            if self._parts and self._parts[-1][-1:] not in (" ", "\n"):
                self._parts.append(" ")
            self._link_depth += 1
        if tag in self._HEADING:
            self._heading_depth += 1
        if tag in self._BLOCK:
            self._flush()
        elif tag in self._CELL:
            self._parts.append(" ")

    def handle_startendtag(self, tag, attrs):
        if tag in self._BLOCK and not self._skip_depth:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
            return
        if self._skip_depth:
            return
        if tag == "a":
            self._link_depth = max(0, self._link_depth - 1)
        if tag in self._HEADING:
            self._heading_depth = max(0, self._heading_depth - 1)
        if tag in self._BLOCK:
            self._flush()
        elif tag in self._CELL:
            self._parts.append(" ")
        if self._scope is not None:
            self._scope_depth -= 1
            if self._scope_depth <= 0:
                self._scope = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._skip_depth:
            return
        # Before the page's own content starts, only the title is kept: the
        # banner and menus above it are chrome, and once an article/main is
        # seen the document ends with it.
        if self._scoped and self._scope is None:
            return
        self._parts.append(data)
        visible = len(data.strip())
        self._block_chars += visible
        # A heading usually wraps its own anchor link, so its text is all link
        # label while still being the one line that names the section.
        if self._link_depth and not self._heading_depth:
            self._link_chars += visible

    def _flush(self) -> None:
        raw = "".join(self._parts)
        self._parts = []
        link_chars, block_chars = self._link_chars, self._block_chars
        self._link_chars = self._block_chars = 0
        text = " ".join(raw.split())
        if not text:
            return
        if (
            block_chars
            and len(text) < self._MENU_MAX_CHARS
            and link_chars / block_chars > self._MENU_LINK_RATIO
        ):
            return
        self._blocks.append(text)

    def text(self) -> str:
        self._flush()
        lines: list[str] = []
        for block in self._blocks:
            for line in block.splitlines():
                line = " ".join(line.split())
                if not line or (lines and lines[-1] == line):
                    continue
                lines.append(line)
        return "\n".join(_drop_repeated_trailer(lines))


def _drop_repeated_trailer(lines: list[str]) -> list[str]:
    """Drop a block of lines the page draws twice, such as its own outline.

    Documentation sites print a table of contents under the title and again at
    the end; the second copy is the same lines in the same order, and keeping
    it doubles the cost of every page read without adding a single fact.
    """

    count = len(lines)
    for size in range(min(60, count // 2), 4, -1):
        trailer = lines[count - size:]
        for start in range(count - size):
            if lines[start:start + size] == trailer:
                return lines[:count - size]
    return lines


_META_REFRESH_RE = re.compile(
    r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]*?"""
    r"""content=["'][^"']*?url\s*=\s*([^"\s>]+)""",
    re.IGNORECASE,
)


def _meta_refresh_target(html: str) -> Optional[str]:
    """The URL an HTML ``<meta http-equiv=refresh>`` redirects to."""

    match = _META_REFRESH_RE.search(html)
    if not match:
        return None
    target = match.group(1).strip("'\"")
    return target or None


async def local_page(url: str, client: Any) -> Optional[tuple[str, str]]:
    """Read one page over plain HTTP, for a result the backend could not serve.

    Only readable text is returned: scripts never run, so a page that renders
    its content in the browser still has nothing to give -- the caller says so
    rather than pretending the page was empty.
    """

    from ..browser_identity import browser_request_headers

    try:
        response = await client.get(
            url,
            headers=browser_request_headers(
                accept="text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"
            ),
            follow_redirects=True,
        )
    except Exception:
        return None
    if response.status_code != 200:
        return None
    body = response.content[:LOCAL_FETCH_MAX_BYTES]
    charset = response.encoding or "utf-8"
    text = body.decode(charset, "replace")
    content_type = str(response.headers.get("content-type") or "").lower()
    if "html" in content_type or text.lstrip()[:1] == "<":
        meta = _meta_refresh_target(text)
        if meta:
            # A meta refresh is an HTML redirect: following it is the
            # difference between npm's canonical CLI URL (a 77-byte stub that
            # only refreshes to /cli/v12/...) reading as an empty page and
            # reading as the documentation it points at.
            followed = await local_page(urljoin(url, meta), client)
            if followed is not None:
                return followed
        extractor = _TextExtractor()
        try:
            extractor.feed(text)
        except Exception:
            return None
        title = extractor.title.strip()
        body = extractor.text()
        if not body.strip():
            # A document whose content is drawn by scripts has no prose to
            # give.  Handing the model the raw HTML instead (inline scripts and
            # all) would be the same "opened the page and learned nothing" that
            # this reader exists to avoid, only larger.
            return (title or url, "（这个页面用脚本渲染正文，纯文本读取拿不到内容。）")
        return (title or url, body)
    return (url, text)


def _domain(url: str) -> str:
    without_scheme = url.split("://", 1)[-1]
    return without_scheme.split("/", 1)[0]


def _published_label(value: Any) -> str:
    """The publish date a result declares, reduced to its calendar day."""

    if not isinstance(value, str) or not value.strip():
        return ""
    text = value.strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return ""


def render_groups(
    groups: Iterable[tuple[Optional[str], list[dict[str, Any]]]],
    session: Optional[dict[str, str]] = None,
) -> tuple[str, list[dict[str, Any]]]:
    """The text Codex shows the model, grouped by the query that produced it.

    Every query this endpoint executed is named in the text.  The upstream's own
    answer is one flat list, which leaves a reader unable to tell which terms
    were actually searched; naming the group fixes that without hiding any
    result.  ``ref_id`` stays scoped to one response and is remembered for the
    follow-up ``open``/``find``, and each entry keeps its URL because that is
    what survives into a later turn.
    """

    prepared: list[tuple[Optional[str], list[dict[str, Any]]]] = []
    for query, rows in groups:
        usable = [dict(row) for row in rows if str(row.get("url") or "").strip()]
        if usable:
            prepared.append((query, usable))

    digest = hashlib.sha256(
        "\n".join(
            f"{query or ''} {row.get('url') or ''}" for query, rows in prepared for row in rows
        ).encode("utf-8")
    ).digest()[:5]
    turn_id = int.from_bytes(digest, "big")

    remembered: dict[str, str] = {}
    structured: list[dict[str, Any]] = []
    lines: list[str] = []
    index = 0
    for group_number, (query, rows) in enumerate(prepared, start=1):
        if lines:
            lines.append(RESULT_SEPARATOR)
        if query:
            lines.append(f"查询 {group_number}：{query}")
        for offset, result in enumerate(rows):
            if offset:
                lines.append(RESULT_SEPARATOR)
            index += 1
            url = str(result.get("url") or "").strip()
            title = str(result.get("title") or "").strip() or url
            text = str(result.get("summary") or result.get("snippet") or "").strip()
            ref_id = f"turn{turn_id}search{index - 1}"
            remembered[ref_id] = url
            published = _published_label(result.get("date"))
            # ``wordlim`` is prose for the model, not a protocol field: Codex
            # never parses it.  OpenAI prints it because it truncates each
            # result; this backend hands the summary back whole, so declaring a
            # limit here would describe a cut that never happened.
            marker = f" Published: {published};" if published else ""
            lines.append(f"{title} ({url})")
            lines.append(f" cite {ref_id}{marker}")
            if text:
                lines.append(text)
            structured.append(
                {
                    "type": "text_result",
                    "domain": _domain(url),
                    "ref_id": ref_id,
                    "title": title,
                    "url": url,
                    "snippet": str(result.get("snippet") or text)[:400],
                }
            )
    if remembered:
        remember_references(
            session if session is not None else _session_for("codex-default"),
            remembered,
        )
    return "\n".join(lines).strip(), structured


def render_results(
    results: Iterable[Mapping[str, Any]],
    session: Optional[dict[str, str]] = None,
) -> tuple[str, list[dict[str, Any]]]:
    """One unnamed group; kept for callers that have no query to name."""

    return render_groups([(None, [dict(row) for row in results])], session)


def _browser_urls(queries: list[str]) -> list[str]:
    if not queries:
        return []
    encoded = quote(queries[0])
    return [
        f"https://www.bing.com/search?q={encoded}",
        f"https://www.baidu.com/s?wd={encoded}",
    ]


def tips_output(model: str, queries: list[str], reason: str) -> str:
    """The terminal answer a route without a search backend gives the model."""

    lines = [
        f"搜索没有执行：{reason}（模型 {model or '未知'}）。",
        "不要再次调用 web.run 重试，也不要用 shell / curl / requests 抓搜索结果页——"
        "这些站点会返回反爬页面，白白消耗上下文。",
        "请改用本机已有的浏览器工具，一步到位：",
        "  1) 内置浏览器：调用 cua_repl 的 js，执行 "
        'await cua.createBrowserTab("iab", "<下面的地址>"); 然后读取页面文本。',
        '  2) 终端 Playwright：npx playwright-cli open "<下面的地址>"。',
    ]
    for url in _browser_urls(queries):
        lines.append(f"  入口：{url}")
    return "\n".join(lines)


def _payload(output: str, results: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    return {"encrypted_output": None, "output": output, "results": results or []}


def tips_payload(model: str, body: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return _payload(tips_output(model, search_queries(body), reason))


def _deployment_params(entry: Mapping[str, Any]) -> dict[str, Any]:
    params = entry.get("litellm_params")
    return dict(params) if isinstance(params, Mapping) else {}


def _router_entries(model: str) -> list[dict[str, Any]]:
    """The deployments this public model can be served by, in router order."""

    try:
        from litellm.proxy import proxy_server

        router = getattr(proxy_server, "llm_router", None)
        if router is None:
            return []
        entries = router.get_model_list(model_name=model) or []
    except Exception:
        return []
    return [dict(entry) for entry in entries if isinstance(entry, Mapping)]


async def _post_json(
    url: str, key: str, payload: Mapping[str, Any], client: Any
) -> tuple[int, Any]:
    response = await client.post(
        url,
        json=dict(payload),
        headers={"authorization": f"Bearer {key}", "content-type": "application/json"},
    )
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, None


def _number_lines(text: str) -> list[str]:
    """One page line per ``L<n>:`` entry, the shape the model reads back.

    ``OpenOperation.lineno`` addresses a line of this rendering, so the numbers
    have to be the page's own and stable across reads of the same page: a find
    answer that names ``L264`` must be addressable by the next ``open``.
    """

    return [f"L{index}: {line}" for index, line in enumerate(text.splitlines())]


def _page_excerpt(content: str, lineno: Optional[int] = None) -> str:
    """The part of a page a model asked to see, still line-addressed.

    Without ``lineno`` that is the head of the page.  With it, the window starts
    where the model pointed -- the tail of the previous excerpt, or the section
    it came back for -- so a second read costs new text instead of the same
    navigation tree again.
    """

    numbered = _number_lines(content)
    start = 0
    if lineno is not None:
        # ``lineno`` is the offset from the top of the page, so a value inside
        # the excerpt is already reachable; clamp rather than refuse.
        start = max(0, min(lineno, max(0, len(numbered) - 1)))
    excerpt: list[str] = []
    budget = PAGE_CONTENT_LIMIT
    for line in numbered[start:]:
        if excerpt and len(line) + 1 > budget:
            break
        excerpt.append(line)
        budget -= len(line) + 1
    remaining = len(numbered) - start - len(excerpt)
    if remaining > 0:
        # Say where this window stopped and how to ask for the rest: a reader
        # that does not know the page continues will search again instead.
        excerpt.append(
            f"（本页共 {len(numbered)} 行，本次到 L{start + len(excerpt) - 1} 结束，"
            f"还剩 {remaining} 行；需要后面的内容请带 lineno={start + len(excerpt)} 再 open。）"
        )
    return "\n".join(excerpt)


def _find_hits(content: str, pattern: str) -> list[tuple[int, str]]:
    """Matching page lines with the line number an ``open`` can address."""

    if not pattern:
        return []
    wanted = pattern.casefold()
    hits: list[tuple[int, str]] = []
    for index, line in enumerate(content.splitlines()):
        if wanted in line.casefold():
            hits.append((index, line.strip()))
            if len(hits) >= MAX_FIND_MATCHES:
                break
    return hits


def _remember_page_view(
    session: dict[str, str], url: str, content: str
) -> str:
    """Publish a page handed back by ``open``/``find`` under its own reference.

    Codex's ``find`` and ``click`` address a page view, not a search result, so
    a referenced ``open`` has to mint ``turnNviewM`` or the follow-up cannot be
    expressed at all -- which is why the model used to fall back to opening the
    same URL again and again.
    """

    digest = hashlib.sha256(f"{url}\n{len(content)}".encode("utf-8")).digest()[:5]
    ref_id = f"turn{int.from_bytes(digest, 'big')}view0"
    remember_references(session, {ref_id: url})
    return ref_id


def _remember_answer_references(
    payload: Mapping[str, Any], session: dict[str, str]
) -> None:
    """Record the references a gateway's own search answer minted.

    The gateway scopes ``turnNsearchM`` to its own state, which a later
    ``open``/``find`` no longer reaches now that those are answered from the
    page here.  Keeping the mapping it printed is what lets the model still
    address a result by the reference it was just given.
    """

    results = payload.get("results")
    if not isinstance(results, list):
        return
    remembered: dict[str, str] = {}
    for result in results:
        if not isinstance(result, Mapping):
            continue
        ref_id = str(result.get("ref_id") or "").strip()
        url = _clean_url(result.get("url"))
        if ref_id and url and _REFERENCE_RE.fullmatch(ref_id):
            remembered[ref_id] = url
    if remembered:
        remember_references(session, remembered)


def page_fetcher(client: Any, params: Optional[Mapping[str, Any]] = None) -> Any:
    """How one route reads a page for ``open``/``find``.

    A DeepSeek route has its own ``/tools/fetch``, which reaches pages a plain
    HTTP read cannot; every other route is read by this host.  Either way the
    text comes from a page reader, never from a search engine.
    """

    params = params if isinstance(params, Mapping) else {}
    base = deployment_api_base(params)
    key = deployment_api_key(params)
    cache: dict[str, Optional[tuple[str, str]]] = {}

    async def fetch(url: str) -> Optional[tuple[str, str]]:
        if url in cache:
            return cache[url]
        # This host reads first: a vendor "fetch" answers in Markdown with
        # every punctuation character backslash-escaped, which is noise in
        # code and command names.  The vendor is the fallback for pages a plain
        # HTTP read cannot reach (anti-bot, paywall), not the default reader.
        entry = await local_page(url, client)
        if entry is None and base and key:
            status, payload = await _post_json(
                f"{base}/tools/fetch", key, {"url": url}, client
            )
            if status == 200 and isinstance(payload, Mapping):
                entry = (
                    str(payload.get("title") or url),
                    str(payload.get("content") or ""),
                )
            _trace(
                "alpha_search_fetch_fallback",
                url=url,
                backend_status=status,
                local=bool(entry),
            )
        cache[url] = entry
        return entry

    return fetch


_ANSWER_LINE_RE = re.compile(r"(?m)^L(\d+):")
_ANSWER_TOTAL_RE = re.compile(r"Total lines:\s*(\d+)")
_ANSWER_NO_MATCH_RE = re.compile(r'No matching text found for "([^"]*)"', re.IGNORECASE)


def _answer_body(payload: Mapping[str, Any]) -> str:
    """The gateway's own prose, without the envelope it wraps around it.

    The first lines are bookkeeping -- a citation marker and a ``Source:
    find({"pattern": "..."}) ... Total lines: N`` line that echoes the request.
    Comparing a pattern against those would always succeed, because the request
    already contains it, so they are dropped before any judgement is made.
    """

    kept: list[str] = []
    for line in str(payload.get("output") or "").splitlines():
        if "Source:" in line and ("Total lines:" in line or "Crawled:" in line):
            continue
        kept.append(line)
    return "\n".join(kept).replace("\\", "")


def _answer_line_numbers(payload: Mapping[str, Any]) -> list[int]:
    return [int(match.group(1)) for match in _ANSWER_LINE_RE.finditer(_answer_body(payload))]


def _unserved_page_requests(
    body: Mapping[str, Any], payload: Mapping[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    """Page commands whose answer proves the gateway did not serve them.

    The rule is deliberately narrow: an answer is replaced only when it says so
    itself.  ``find`` whose pattern is nowhere in what came back (and which was
    not reported as a genuine miss), ``open`` whose requested ``lineno`` is not
    in the window it returned, and any page answer that declares zero lines.
    Everything else -- including a window that starts at the top of the page --
    is the upstream's own answer and is left alone.
    """

    text = _answer_body(payload)
    total = _ANSWER_TOTAL_RE.search(text)
    nothing_read = total is not None and int(total.group(1)) == 0
    reported_misses = {match.group(1) for match in _ANSWER_NO_MATCH_RE.finditer(text)}
    folded = text.casefold()
    served_lines = _answer_line_numbers(payload)

    unserved: dict[str, list[dict[str, Any]]] = {}
    for request in open_requests(body):
        if "missing_ref" in request:
            continue
        lineno = request.get("lineno")
        if nothing_read or not served_lines:
            unserved.setdefault("open", []).append(request)
        elif isinstance(lineno, int) and lineno not in served_lines:
            # The model asked to continue from a line the answer never
            # reached, so the answer is the page's head again.
            unserved.setdefault("open", []).append(request)
    for request in find_requests(body):
        if "missing_ref" in request:
            continue
        pattern = request.get("pattern") or ""
        if not pattern or pattern in reported_misses:
            continue
        if nothing_read or pattern.casefold() not in folded:
            unserved.setdefault("find", []).append(request)
    return unserved


_ANSWER_SOURCE_RE = re.compile(r"Source:\s*(open|find)\((\{.*?\})\)", re.DOTALL)


def _answer_segments(output: str) -> list[str]:
    """Split an upstream answer into one segment per result/command."""

    segments: list[list[str]] = [[]]
    for line in output.splitlines():
        if line.strip() == RESULT_SEPARATOR:
            segments.append([])
            continue
        segments[-1].append(line)
    return ["\n".join(segment) for segment in segments]


def _segment_request(segment: str) -> Optional[dict[str, Any]]:
    """The ``open``/``find`` request an upstream answer segment reports."""

    match = _ANSWER_SOURCE_RE.search(segment)
    if not match:
        return None
    try:
        arguments = json.loads(match.group(2))
    except ValueError:
        return None
    if not isinstance(arguments, Mapping):
        return None
    request: dict[str, Any] = {"command": match.group(1)}
    url = resolve_reference(str(arguments.get("ref_id") or ""))
    if url is None:
        return None
    request["url"] = url
    if match.group(1) == "find":
        request["pattern"] = str(arguments.get("pattern") or "")
    else:
        lineno = arguments.get("lineno")
        if isinstance(lineno, int) and not isinstance(lineno, bool) and lineno > 0:
            request["lineno"] = lineno
    return request


def _drop_repaired_segments(
    output: str, unserved: Mapping[str, list[dict[str, Any]]]
) -> str:
    """Remove the upstream text of the commands being answered here instead.

    Appending a local answer to the upstream's broken one would still make the
    model pay for the broken one -- the point is to replace it.
    """

    wanted: list[dict[str, Any]] = [
        *unserved.get("open", []),
        *unserved.get("find", []),
    ]
    if not wanted:
        return output
    kept: list[str] = []
    for segment in _answer_segments(output):
        request = _segment_request(segment)
        if request is not None and any(
            request.get("url") == item.get("url")
            and request.get("pattern") == item.get("pattern")
            and request.get("lineno") == item.get("lineno")
            for item in wanted
        ):
            continue
        kept.append(segment)
    return ("\n" + RESULT_SEPARATOR + "\n").join(kept).strip()


async def _page_answers(
    body: Mapping[str, Any],
    session: dict[str, str],
    fetch: Any,
    only: Optional[Mapping[str, list[dict[str, Any]]]] = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Answer ``open``/``find`` from the page itself.

    Used for the page commands the upstream did not answer usefully, and for a
    route with no page backend at all.  ``only`` restricts the work to the
    specific requests being repaired, so a call that mixes a good upstream
    answer with one bad one keeps the upstream's own text for the good one.
    """

    opens = open_requests(body, session)
    finds = find_requests(body, session)
    if only is not None:
        opens = [request for request in only.get("open", []) if request in opens]
        finds = [request for request in only.get("find", []) if request in finds]

    lines: list[str] = []
    results: list[dict[str, Any]] = []
    for request in opens:
        if "missing_ref" in request:
            lines.append(
                f"引用 {request['missing_ref']} 已失效（它只在产生它的那次搜索里有效），"
                "请改用 URL 重新 open。"
            )
            continue
        url = request["url"]
        fetched = await fetch(url)
        if fetched is None:
            lines.append(f"打开失败：{url}（目标站点不可用或被策略拒绝）。")
            continue
        title, content = fetched
        ref_id = _remember_page_view(session, url, content)
        lines.append(
            f"{title} ({url})\n cite {ref_id}\n"
            f"{_page_excerpt(content, request.get('lineno'))}"
        )
        results.append(_page_result(ref_id, title, url, content))

    for request in finds:
        if "missing_ref" in request:
            lines.append(f"引用 {request['missing_ref']} 已失效，请改用 URL 重新 find。")
            continue
        url = request["url"]
        pattern = request.get("pattern") or ""
        fetched = await fetch(url)
        if fetched is None:
            lines.append(f"查找失败：{url}（目标站点不可用）。")
            continue
        title, content = fetched
        ref_id = _remember_page_view(session, url, content)
        if not content.strip():
            lines.append(f"{title} ({url})\n cite {ref_id}\n页面没有可读文本，无法 find。")
            continue
        hits = _find_hits(content, pattern)
        if hits:
            lines.append(
                f"{title} ({url})\n cite {ref_id}\n匹配 “{pattern}” 的行：\n"
                + "\n".join(f"L{index}: {text}" for index, text in hits)
            )
            results.append(
                _page_result(
                    ref_id, title, url,
                    "\n".join(f"L{index}: {text}" for index, text in hits),
                    match_count=len(hits),
                )
            )
        else:
            lines.append(f"{title} ({url})\n cite {ref_id}\n没有匹配 “{pattern}” 的行。")
    for name in unsupported_commands(body):
        lines.append(_UNSERVED_NOTES[name])
    return "\n\n".join(lines), results


def _page_result(
    ref_id: str, title: str, url: str, text: str, *, match_count: Optional[int] = None
) -> dict[str, Any]:
    """One page as the structured result the client renders beside the text."""

    snippet = " ".join(text.split())[:400]
    result: dict[str, Any] = {
        "type": "text_result",
        "domain": _domain(url),
        "ref_id": ref_id,
        "title": title,
        "url": url,
        "snippet": snippet,
    }
    if match_count is not None:
        result["match_count"] = match_count
    return result


async def _deepseek_search(
    params: Mapping[str, Any], body: Mapping[str, Any], client: Any
) -> tuple[Optional[dict[str, Any]], str]:
    base = deployment_api_base(params)
    key = deployment_api_key(params)
    if not base or not key:
        return None, "DeepSeek 路线缺少搜索参数"
    session = _session_for(_session_key(body))
    queries = search_queries(body)
    if not queries:
        return None, "DeepSeek 路线缺少搜索参数"
    question = body.get("input") if isinstance(body.get("input"), str) else None
    count = result_count(body)

    # One tool call may carry up to four queries; they are independent, so they
    # are issued together instead of paying each round trip in sequence.
    responses = await asyncio.gather(
        *[
            _post_json(
                f"{base}/tools/search",
                key,
                deepseek_search_body(query, count, question),
                client,
            )
            for query in queries
        ],
        return_exceptions=True,
    )
    payloads: list[Any] = []
    for response in responses:
        if isinstance(response, Exception):
            return None, "DeepSeek 搜索接口不可用"
        status, payload = response
        if status != 200:
            return None, "DeepSeek 搜索接口不可用"
        payloads.append(payload)

    # Group by the query that returned each result: a turn that asked several
    # questions has to show which terms were searched, and which answer came
    # from which.  A URL two queries both returned is attributed once, to the
    # query that returned it first.
    grouped: list[tuple[Optional[str], list[dict[str, Any]]]] = []
    claimed: set[str] = set()
    for query, payload in zip(queries, payloads):
        rows: list[dict[str, Any]] = []
        for result in (payload.get("results") if isinstance(payload, Mapping) else None) or []:
            if not isinstance(result, Mapping):
                continue
            url = str(result.get("url") or "").strip()
            if not url or url in claimed:
                continue
            claimed.add(url)
            rows.append(dict(result))
            if len(claimed) >= MAX_RESULTS:
                break
        grouped.append((query, rows))
        if len(claimed) >= MAX_RESULTS:
            break
    output, structured = render_groups(grouped, session)
    return _payload(output, structured), ""


def _with_unsupported_notes(
    payload: dict[str, Any], body: Mapping[str, Any]
) -> dict[str, Any]:
    """Say plainly which requested commands this endpoint cannot serve."""

    notes = unsupported_commands(body)
    if not notes:
        return payload
    said = [line for line in notes if line not in str(payload.get("output") or "")]
    if not said:
        return payload
    merged = dict(payload)
    merged["output"] = "\n\n".join(
        part for part in (str(payload.get("output") or ""), *said) if part
    )
    return merged


def _has_content(payload: Mapping[str, Any]) -> bool:
    return bool(str(payload.get("output") or "").strip())


def _trace(event: str, **fields: Any) -> None:
    """One diagnostic line per attempt, on the same channel as route traces."""

    try:
        from . import trace as _trace_module

        _trace_module._route_trace(event, **fields)
    except Exception:
        pass


async def _alpha_search_forward(
    params: Mapping[str, Any], body: Mapping[str, Any], client: Any
) -> tuple[str, Optional[dict[str, Any]]]:
    """Forward the declaration to the gateway this deployment already talks to.

    ``unsupported`` means the gateway answered deterministically that it has no
    such route; anything else (timeout, 5xx) is reported as a transient failure
    so the caller can try the next deployment without poisoning the cache.
    """

    base = deployment_api_base(params)
    key = deployment_api_key(params)
    if not base or not key:
        return "unsupported", None
    cache_key = (base, key)
    if _is_remembered_unsupported(cache_key):
        return "unsupported", None

    forwarded = dict(body)
    # The gateway validates the declared model against the key's own group, so
    # the search must name a model this exact route serves.  It also requires a
    # request id; Codex always sends one, and a caller that did not is given a
    # fresh one instead of being refused by an upstream 400.
    forwarded["model"] = upstream_model_name(params)
    forwarded.setdefault("id", f"search-{uuid.uuid4()}")
    status, payload = await _post_json(f"{base}/alpha/search", key, forwarded, client)
    if status == 200 and isinstance(payload, Mapping):
        return "ok", dict(payload)
    if status in {404, 501}:
        await _remember_unsupported(cache_key)
        return "unsupported", None
    _trace(
        "alpha_search_rejected",
        base=base,
        status=status,
        model=forwarded.get("model"),
        body=str(payload)[:200],
    )
    return f"failed:{status}", None


async def _search(body: Mapping[str, Any], model: str) -> dict[str, Any]:
    import httpx

    session = _session_for(_session_key(body))
    queries = search_queries(body)
    commands = _commands(body)
    wants_pages = bool(commands.get("open") or commands.get("find"))
    if not queries and not wants_pages:
        return tips_payload(model, body, "这次调用没有可执行的指令")
    entries = _router_entries(model)
    tried: list[str] = []
    # The proxy process carries the machine's HTTP(S)_PROXY snapshot for its
    # upstream calls; the search backends are reached directly, so the client
    # must not inherit that environment or the request dies in the tunnel.
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS, trust_env=False
    ) as client:
        reader = next(
            (
                _deployment_params(entry)
                for entry in entries
                if is_deepseek_deployment(_deployment_params(entry))
            ),
            None,
        )
        payload: Optional[dict[str, Any]] = None
        for entry in entries:
            params = _deployment_params(entry)
            base = deployment_api_base(params)
            if is_deepseek_deployment(params):
                try:
                    served, reason = await _deepseek_search(params, body, client)
                except Exception as exc:
                    served, reason = None, type(exc).__name__
                if served is not None and _has_content(served):
                    _trace(
                        "alpha_search_served",
                        model=model,
                        base=base,
                        backend="deepseek",
                        queries=len(queries),
                    )
                    payload = served
                    break
                tried.append(f"{base or '?'}:deepseek:{reason or 'empty'}")
                continue
            try:
                state, served = await _alpha_search_forward(params, body, client)
            except Exception as exc:
                state, served = f"failed:{type(exc).__name__}", None
            if state == "ok" and served is not None and _has_content(served):
                _trace("alpha_search_served", model=model, base=base, backend="gateway")
                _remember_answer_references(served, session)
                payload = served
                break
            tried.append(f"{base or '?'}:{state}")

        if payload is None:
            # No route serves searches.  Page commands still have an answer --
            # the page itself -- so they are read here rather than tipped.
            page_text, page_results = ("", [])
            if wants_pages:
                page_text, page_results = await _page_answers(
                    body, session, page_fetcher(client, reader)
                )
            if page_text:
                _trace("alpha_search_served", model=model, base="-", backend="pages")
                return _with_unsupported_notes(_payload(page_text, page_results), body)
            _trace(
                "alpha_search_fallback",
                model=model,
                deployments=len(entries),
                tried=tried,
            )
            reason = "这条路线没有可用的搜索后端"
            if not entries:
                reason = "这个公开模型没有任何可路由的部署"
            return tips_payload(model, body, reason)

        if not wants_pages:
            return _with_unsupported_notes(payload, body)

        # The upstream answered; it keeps every page command whose answer
        # stands on its own.  Only the ones whose answer contradicts itself --
        # a find whose pattern is absent from what came back, an open whose
        # requested lineno was never reached, a page reported as empty -- are
        # answered here instead.
        unserved = _unserved_page_requests(body, payload)
        if not unserved:
            return _with_unsupported_notes(payload, body)
        _trace(
            "alpha_search_page_repaired",
            model=model,
            open=len(unserved.get("open", [])),
            find=len(unserved.get("find", [])),
        )
        page_text, page_results = await _page_answers(
            body, session, page_fetcher(client, reader), only=unserved
        )
        if not page_text:
            return _with_unsupported_notes(payload, body)
        merged = dict(payload)
        merged["output"] = "\n\n".join(
            part
            for part in (
                _drop_repaired_segments(str(payload.get("output") or ""), unserved),
                page_text,
            )
            if part
        )
        merged["results"] = [*(payload.get("results") or []), *page_results]
        return _with_unsupported_notes(merged, body)


def _error(response_cls: Any, message: str, status: int) -> Any:
    return response_cls({"error": {"message": message, "code": status}}, status_code=status)


async def alpha_search(request: Any, response_cls: Any) -> Any:
    """Answer one standalone search. ``response_cls`` keeps FastAPI optional."""

    if not authorized(request, proxy_general_settings()):
        return _error(response_cls, "Authentication Error", 401)
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        return _error(response_cls, "invalid JSON body", 400)
    if not isinstance(body, Mapping):
        return _error(response_cls, "body must be an object", 400)
    model = str(body.get("model") or "").strip()
    if not model:
        return _error(response_cls, "model is required", 400)
    return response_cls(await _search(body, model))


def register(app: Any) -> None:
    """Add the route to a running app.

    The handler is defined here so its annotations resolve against the real
    ``Request`` class at import time; the class itself stays out of this
    module's scope so hosts without FastAPI can still import it.
    """

    from fastapi import Request
    from fastapi.responses import JSONResponse

    async def handler(request: Request) -> JSONResponse:
        return await alpha_search(request, JSONResponse)

    # ``from __future__ import annotations`` turns these into strings that
    # FastAPI resolves against *module* globals, where these classes are
    # deliberately absent; bind the real classes so the body is parsed as a
    # request instead of being looked for as a query parameter.
    handler.__annotations__ = {"request": Request, "return": JSONResponse}

    for path in SEARCH_PATHS:
        app.add_api_route(path, handler, methods=["POST"], include_in_schema=False)
