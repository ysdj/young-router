from __future__ import annotations

from .base import (
    Any,
    Optional,
    _EXTERNAL_WEB_SEARCH_BACKEND_DEFAULT,
    _EXTERNAL_WEB_SEARCH_BACKEND_ENV,
    _EXTERNAL_WEB_SEARCH_MAX_RESULTS_DEFAULT,
    _EXTERNAL_WEB_SEARCH_MAX_RESULTS_ENV,
    _EXTERNAL_WEB_SEARCH_READ_CHARS_DEFAULT,
    _EXTERNAL_WEB_SEARCH_READ_CHARS_ENV,
    _EXTERNAL_WEB_SEARCH_READ_RESULTS_DEFAULT,
    _EXTERNAL_WEB_SEARCH_READ_RESULTS_ENV,
    _EXTERNAL_WEB_SEARCH_REGION_DEFAULT,
    _EXTERNAL_WEB_SEARCH_REGION_ENV,
    _EXTERNAL_WEB_FETCH_TIMEOUT_DEFAULT,
    _EXTERNAL_WEB_FETCH_TIMEOUT_ENV,
    _FALLBACK_BROWSER_USER_AGENT,
    _SearchResponse,
    _SearchResult,
    _WebSearchTransformation,
    os,
    quote,
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


def _jina_reader_excerpt(url: str, *, timeout: float, max_chars: int) -> str:
    if not url.startswith(("http://", "https://")):
        return ""
    reader_url = "https://r.jina.ai/" + quote(url, safe=":/")
    request = urllib.request.Request(
        reader_url,
        headers={
            "Accept": "text/plain, */*",
            "User-Agent": _FALLBACK_BROWSER_USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read(max_chars * 4).decode("utf-8", "ignore")
    except (urllib.error.URLError, TimeoutError, OSError):
        return ""
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "..."
    return text


def _jina_reader_excerpts(
    urls: list[str],
    *,
    timeout: float,
    max_chars: int,
) -> list[str]:
    if not urls:
        return []
    from concurrent.futures import ThreadPoolExecutor

    workers = min(4, len(urls))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="jina-reader") as executor:
        return list(
            executor.map(
                lambda url: _jina_reader_excerpt(
                    url,
                    timeout=timeout,
                    max_chars=max_chars,
                ),
                urls,
            )
        )


def _ddgs_jina_web_search_sync(query: str) -> tuple[str, Any]:
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
    read_results = _external_web_search_int_env(
        _EXTERNAL_WEB_SEARCH_READ_RESULTS_ENV,
        _EXTERNAL_WEB_SEARCH_READ_RESULTS_DEFAULT,
        0,
        max_results,
    )
    read_chars = _external_web_search_int_env(
        _EXTERNAL_WEB_SEARCH_READ_CHARS_ENV,
        _EXTERNAL_WEB_SEARCH_READ_CHARS_DEFAULT,
        200,
        5000,
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
        if item.strip()
    ] or [_EXTERNAL_WEB_SEARCH_BACKEND_DEFAULT]

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

    readable_urls: list[str] = []
    for raw in raw_results[:read_results]:
        if isinstance(raw, dict):
            readable_urls.append(str(raw.get("href") or raw.get("url") or "").strip())
    excerpts = _jina_reader_excerpts(
        readable_urls,
        timeout=min(timeout, 15.0),
        max_chars=read_chars,
    )

    results: list[Any] = []
    fallback_lines: list[str] = []
    for index, raw in enumerate(raw_results[:max_results]):
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("href") or raw.get("url") or "").strip()
        title = str(raw.get("title") or url or "Untitled result").strip()
        snippet = str(raw.get("body") or raw.get("snippet") or "").strip()
        if index < len(excerpts) and excerpts[index]:
            snippet = f"{snippet}\n\nJina Reader excerpt:\n{excerpts[index]}".strip()
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
