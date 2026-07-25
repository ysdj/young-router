#!/usr/bin/env python3
"""Read a provider token's quota through an already-authenticated browser.

Some New API deployments expose token quota only to their web console.  This
module deliberately does not read browser profile, cookie, or storage files.  It
asks the user's running Chrome tab to perform the same-origin request, so the
browser supplies its own session credentials. Chrome must have
"Allow JavaScript from Apple Events" enabled for this bridge; when that is not
available the caller receives a bounded, classified failure and can keep the
normal HTTP billing result.
"""

from __future__ import annotations

import json
import math
import subprocess
import threading
import urllib.parse
from typing import Any, Callable


DEFAULT_TIMEOUT_SECONDS = 4.0
MAX_TIMEOUT_SECONDS = 15.0
MIN_TIMEOUT_SECONDS = 0.5
MAX_OUTPUT_BYTES = 256 * 1024
DEFAULT_BROWSER_APP = "Google Chrome"
_BRIDGE_LOCK = threading.Lock()
_SAFE_ITEM_FIELDS = (
    "status",
    "remain_quota",
    "unlimited_quota",
    "used_quota",
    "total_granted",
    "group",
    "__browser_multiplier",
    "__browser_match",
)


class BrowserBillingError(RuntimeError):
    """The local browser bridge could not produce a billing response."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _service_origin(api_base: str) -> str | None:
    """Return a safe origin for the configured API base."""

    try:
        parsed = urllib.parse.urlsplit(api_base)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.hostname is None:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    # A billing page must not be selected from an embedded path or query.  The
    # path is intentionally discarded: the token endpoint is rooted at /api.
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def _page_expression(origin: str, token: str) -> str:
    """Return a JSON-quoted, same-origin page expression."""

    return r'''(() => {
  const origin = %ORIGIN%;
  const token = %TOKEN%;
  const url = origin + "/api/token/search?token=" + encodeURIComponent(token) + "&p=1&size=20";
  const request = new XMLHttpRequest();
  request.open("GET", url, false);
  request.withCredentials = true;
  request.setRequestHeader("Accept", "application/json");
  try {
    request.send(null);
  } catch (_) {
    return JSON.stringify({"__browser_billing_error":"network"});
  }
  if (request.status < 200 || request.status >= 300) {
    return JSON.stringify({"__browser_billing_http_status":request.status});
  }
  let payload;
  try {
    payload = JSON.parse(request.responseText || "{}");
  } catch (_) {
    return request.responseText || "";
  }
  const items = payload && payload.data && Array.isArray(payload.data.items)
    ? payload.data.items : [];
  const item = items.find(value => value && value.key === token) || null;
  // The token-search API has the quota; the keys table can expose the group
  // multiplier. Read only the matching row and return the numeric multiplier.
  if (item && typeof item === "object") {
    const needles = [item.name, item.key, item.group]
      .filter(value => typeof value === "string" && value);
    const rows = Array.from(document.querySelectorAll("tr,[role=\"row\"]"));
    for (const row of rows) {
      const text = String(row.innerText || "");
      if (!needles.some(needle => text.includes(needle))) continue;
      const match = text.match(/(?:^|\s)([0-9]+(?:\.[0-9]+)?)x(?:\s|$)/i);
      if (match) item.__browser_multiplier = Number(match[1]);
      break;
    }
  }
  const matchedItem = items.find(candidate => candidate && candidate.key === token);
  const safeFields = %SAFE_FIELDS%;
  const safeItem = {};
  if (matchedItem && typeof matchedItem === "object") {
    matchedItem.__browser_match = true;
    for (const field of safeFields) {
      if (Object.prototype.hasOwnProperty.call(matchedItem, field)) {
        safeItem[field] = matchedItem[field];
      }
    }
  }
  return JSON.stringify({data: {items: Object.keys(safeItem).length ? [safeItem] : []}});
})()'''.replace("%ORIGIN%", json.dumps(origin)).replace(
    "%TOKEN%", json.dumps(token)
).replace("%SAFE_FIELDS%", json.dumps(_SAFE_ITEM_FIELDS))


def _apple_script(origin: str, token: str, browser_app: str) -> str:
    """Build AppleScript with a short-lived page expression.

    ``osascript`` does not provide a way for an AppleScript handler to substitute
    an argv value into Chrome's JavaScript execution API.  The page expression
    therefore contains JSON-quoted values, while the source is never written to
    disk or logged.  ``json.dumps`` prevents credential punctuation from
    changing the JavaScript syntax.
    """

    expression = _page_expression(origin, token)
    app_literal = json.dumps(browser_app)
    source = '''on run argv
  if (count of argv) < 1 then error "browser billing arguments are missing"
  set targetOrigin to item 1 of argv
  if not (application %APP_LITERAL% is running) then error "The browser is not running"
  tell application %APP_LITERAL%
    if (count of windows) = 0 then error "The browser has no open window"
    set workingTab to missing value
    -- Prefer an already-open keys page, without selecting or navigating it.
    repeat with candidateWindow in windows
      repeat with candidateTab in (tabs of candidateWindow)
        try
          set candidateURL to URL of candidateTab
          if my isKeysPage(candidateURL, targetOrigin) then
            set workingTab to candidateTab
            exit repeat
          end if
        end try
      end repeat
      if workingTab is not missing value then exit repeat
    end repeat
    -- If /keys is not open, use any existing page on the same origin.
    if workingTab is missing value then
      repeat with candidateWindow in windows
        repeat with candidateTab in (tabs of candidateWindow)
          try
            set candidateURL to URL of candidateTab
            if my isSameOriginPage(candidateURL, targetOrigin) then
              set workingTab to candidateTab
              exit repeat
            end if
          end try
        end repeat
        if workingTab is not missing value then exit repeat
      end repeat
    end if
    if workingTab is missing value then error "No existing browser tab matches the provider origin"
    set pageExpression to %PAGE_EXPRESSION%
    tell workingTab
      set responseText to execute javascript pageExpression
    end tell
    return responseText
  end tell
end run

on isSameOriginPage(candidateURL, targetOrigin)
  return candidateURL is targetOrigin or candidateURL starts with (targetOrigin & "/")
end isSameOriginPage

on isKeysPage(candidateURL, targetOrigin)
  set keysURL to targetOrigin & "/keys"
  return candidateURL is keysURL or candidateURL starts with (keysURL & "/") or candidateURL starts with (keysURL & "?") or candidateURL starts with (keysURL & "#")
end isKeysPage'''
    # AppleScript strings use backslash escapes, while JSON uses the same
    # escape character for quotes and control bytes. Keep the expression as a
    # single-line JSON string so AppleScript never has to parse its braces.
    expression_literal = json.dumps(expression)
    return source.replace("%APP_LITERAL%", app_literal).replace(
        "%PAGE_EXPRESSION%", expression_literal
    )


def _default_runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(*args, **kwargs)


def fetch_browser_token_search(
    api_base: str,
    api_key: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    browser_app: str = DEFAULT_BROWSER_APP,
    runner: Runner = _default_runner,
) -> tuple[str, int | None, dict[str, Any] | None]:
    """Run the browser-side token search.

    The return convention mirrors ``provider_billing._fetch_json``:
    ``("ok", status, payload)`` on a JSON response, or a classified failure
    such as ``("unavailable", None, None)``.  The API key is never included in
    the returned payload or exception text.
    """

    if not isinstance(api_key, str) or not api_key or "\n" in api_key or "\r" in api_key:
        return "invalid", None, None
    origin = _service_origin(api_base)
    if origin is None:
        return "invalid", None, None
    try:
        bounded_timeout = float(timeout)
    except (TypeError, ValueError):
        return "invalid", None, None
    if not math.isfinite(bounded_timeout):
        return "invalid", None, None
    bounded_timeout = min(MAX_TIMEOUT_SECONDS, max(MIN_TIMEOUT_SECONDS, bounded_timeout))

    try:
        script = _apple_script(origin, api_key, browser_app)
    except (TypeError, ValueError):
        return "invalid", None, None
    # Passing the script on stdin keeps the credential out of the process
    # argument vector. The page expression still exists only for this one
    # short-lived subprocess and is never written to disk or emitted in a result.
    command: list[str] = ["osascript", "-", origin]
    try:
        with _BRIDGE_LOCK:
            completed = runner(
                command,
                input=script,
                capture_output=True,
                text=True,
                timeout=bounded_timeout,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return "timeout", None, None
    except (OSError, ValueError):
        return "unavailable", None, None
    if completed.returncode != 0:
        # Chrome emits a stable error when Apple-event JavaScript is disabled;
        # treat all bridge execution failures as unavailable without surfacing
        # browser diagnostics that could contain page or credential text.
        return "unavailable", None, None
    output = completed.stdout or ""
    if len(output.encode("utf-8", "ignore")) > MAX_OUTPUT_BYTES:
        return "invalid", None, None
    try:
        payload = json.loads(output.strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return "invalid", None, None
    if not isinstance(payload, dict):
        return "invalid", None, None
    status_marker = payload.get("__browser_billing_http_status")
    if isinstance(status_marker, int):
        return "http", status_marker, None
    if payload.get("__browser_billing_error"):
        return "network", None, None
    return "ok", 200, payload


__all__ = [
    "BrowserBillingError",
    "DEFAULT_BROWSER_APP",
    "fetch_browser_token_search",
]
