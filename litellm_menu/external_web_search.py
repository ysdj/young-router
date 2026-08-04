from __future__ import annotations

from html.parser import HTMLParser

from .base import (
    Any,
    Optional,
    _EXTERNAL_WEB_SEARCH_BACKEND_DEFAULT,
    _EXTERNAL_WEB_SEARCH_BACKEND_ENV,
    _EXTERNAL_WEB_SEARCH_MAX_RESULTS_DEFAULT,
    _EXTERNAL_WEB_SEARCH_MAX_RESULTS_ENV,
    _EXTERNAL_WEB_SEARCH_REGION_DEFAULT,
    _EXTERNAL_WEB_SEARCH_REGION_ENV,
    _EXTERNAL_WEB_FETCH_TIMEOUT_DEFAULT,
    _EXTERNAL_WEB_FETCH_TIMEOUT_ENV,
    _SearchResponse,
    _SearchResult,
    _WebSearchTransformation,
    os,
    re,
    urllib,
)


def _external_web_search_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _external_web_search_float_env(
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


class _ExternalWebSearchHTMLTextExtractor(HTMLParser):
    _BLOCK_TAGS = {
        "article",
        "aside",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "p",
        "section",
        "table",
        "td",
        "th",
        "tr",
    }
    _IGNORED_TAGS = {"noscript", "script", "style", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        if normalized in self._IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if normalized in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in self._IGNORED_TAGS:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if normalized in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self.parts.append(data)


def _external_web_search_normalize_page_text(text: str, *, max_chars: int) -> str:
    text = re.sub(r"[\t\r\f\v ]+", " ", text)
    text = re.sub(r"\n[ ]*\n+", "\n", text).strip()
    if len(text) > max_chars:
        return text[:max_chars].rstrip() + "..."
    return text


def _web_page_excerpt(url: str, *, timeout: float, max_chars: int) -> str:
    if not url.startswith(("http://", "https://")):
        return ""
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
            "User-Agent": "LiteLLM-Menu/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(max_chars * 16)
            content_type = str(response.headers.get("Content-Type") or "").lower()
    except (urllib.error.URLError, TimeoutError, OSError):
        return ""
    text = raw.decode("utf-8", "ignore")
    if "html" in content_type or "<html" in text[:1024].lower():
        parser = _ExternalWebSearchHTMLTextExtractor()
        parser.feed(text)
        text = "".join(parser.parts)
    return _external_web_search_normalize_page_text(text, max_chars=max_chars)


def _ddgs_jina_web_search_sync(query: str, *, page: int = 1) -> tuple[str, Any]:
    try:
        from ddgs import DDGS
    except ImportError as exc:
        raise RuntimeError(
            "ddgs package is required for no-key web_search bridge"
        ) from exc

    max_results = _external_web_search_int_env(
        _EXTERNAL_WEB_SEARCH_MAX_RESULTS_ENV,
        _EXTERNAL_WEB_SEARCH_MAX_RESULTS_DEFAULT,
        1,
        20,
    )
    timeout = _external_web_search_float_env(
        _EXTERNAL_WEB_FETCH_TIMEOUT_ENV,
        _EXTERNAL_WEB_FETCH_TIMEOUT_DEFAULT,
        3.0,
        60.0,
    )
    region = os.environ.get(
        _EXTERNAL_WEB_SEARCH_REGION_ENV,
        _EXTERNAL_WEB_SEARCH_REGION_DEFAULT,
    )
    backend = os.environ.get(
        _EXTERNAL_WEB_SEARCH_BACKEND_ENV,
        _EXTERNAL_WEB_SEARCH_BACKEND_DEFAULT,
    )
    backends = [
        item.strip()
        for item in re.split(r"[, ]+", backend)
        if item.strip() and item.strip().lower() != "auto"
    ]
    if not backends:
        backends = [
            item.strip()
            for item in re.split(r"[, ]+", _EXTERNAL_WEB_SEARCH_BACKEND_DEFAULT)
            if item.strip()
        ]

    raw_results: list[dict[str, Any]] = []
    last_exception: Optional[Exception] = None
    for backend_name in backends:
        try:
            with DDGS(timeout=timeout) as ddgs:
                backend_results = list(
                    ddgs.text(
                        query,
                        max_results=max_results,
                        region=region,
                        safesearch="off",
                        page=page,
                        backend=backend_name,
                    )
                )
        except Exception as exc:
            last_exception = exc
            continue

        for raw in backend_results:
            if not isinstance(raw, dict):
                continue
            url = str(raw.get("href") or raw.get("url") or "").strip()
            title = str(raw.get("title") or "").strip()
            snippet = str(raw.get("body") or raw.get("snippet") or "").strip()
            dedupe_key = url or title
            if not dedupe_key:
                continue
            if any(
                dedupe_key
                == (
                    str(existing.get("href") or existing.get("url") or "").strip()
                    or str(existing.get("title") or "").strip()
                )
                for existing in raw_results
                if isinstance(existing, dict)
            ):
                continue
            raw_results.append(raw)
            if len(raw_results) >= max_results:
                break
        if len(raw_results) >= max_results:
            break
    if not raw_results and last_exception is not None:
        raise last_exception

    results: list[Any] = []
    fallback_lines: list[str] = []
    for index, raw in enumerate(raw_results[:max_results]):
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("href") or raw.get("url") or "").strip()
        title = str(raw.get("title") or url or "Untitled result").strip()
        snippet = str(raw.get("body") or raw.get("snippet") or "").strip()
        fallback_lines.append(f"Title: {title}\nURL: {url}\nSnippet: {snippet}")
        if _SearchResult is not None:
            results.append(
                _SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet,
                    date=None,
                    last_updated=None,
                )
            )

    if _SearchResponse is None or _WebSearchTransformation is None:
        return "\n\n".join(fallback_lines), None

    response = _SearchResponse(results=results, object="search")
    return _WebSearchTransformation.format_search_response(response), response
