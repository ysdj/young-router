"""The id and behavior Claude Code's picker names for one public route.

Both Claude apps read one id space.  Claude Desktop's discovery publishes a
route under the derived alias (or under its own name when the client already
keeps it) and appends the client's ``[1m]`` spelling for a route published at
1M.  The Code tab launches the same Claude Code with the id it received from
that discovery, so the picker and the discovery have to name a route the same
way.  This module is the one place that decides both; the settings writer (the
app or the model-list plugin) and the discovery view ask it rather than each
re-deriving the suffix or the behavior tier.

Moving this out of ``core.domains.claude`` also lets the proxy publish the
same values on the canonical ``/v1/models`` rows.  The plugin reads those
fields when the provider states them, so its output does not drift from the
picker the app writes.
"""

from __future__ import annotations

from . import claude_aliases

#: The client's 1M behavior entry.  It carries the full low..max effort ladder
#: and states 1M; a route at or above 1M must borrow this entry, not the
#: smaller large-window one, or the tier caps the session below its route.
CODE_BEHAVES_AS_1M = "claude-opus-5-5"

#: The 1M entry with the smaller output claim (64k instead of 128k).  A route
#: between the client's 200k default and a full 1M borrows this one: it is wide
#: enough to cover the route while its output claim stays as small as the tier
#: allows.
CODE_BEHAVES_AS_LARGE = "claude-opus-4-8"

#: The client's own small tier.  A route at or below 200k -- and a route whose
#: provider states no window -- borrows this behavior.
CODE_BEHAVES_AS_DEFAULT = "claude-sonnet-4-6"

#: The width of the client's own small tier.
CODE_DEFAULT_TIER_WINDOW = 200_000


def published_id(public_name: str, max_input_tokens: object = None) -> str:
    """The id both Claude apps address one public route by.

    A public name the client already keeps stays itself; every other route
    gets the derived alias.  A route published at 1M carries the client's
    ``[1m]`` suffix, because that is the id the desktop session actually sends
    and the suffix is how the client reads the row as the large-window entry.
    """

    base = (
        public_name
        if claude_aliases.desktop_keeps_model_id(public_name)
        else claude_aliases.claude_desktop_alias(public_name)
    )
    if (
        isinstance(max_input_tokens, int)
        and not isinstance(max_input_tokens, bool)
        and max_input_tokens >= 1_000_000
    ):
        return f"{base}{claude_aliases.CONTEXT_SUFFIX}"
    return base


def published_behaves_as(public_name: str, max_input_tokens: object) -> str | None:
    """The known Claude Code model an unknown route should behave as.

    A real Claude id is already in the client's catalog and needs no
    substitute.  For every other route the substitute must be at least as wide
    as the route: the client caps a session at the behavior model's window, so
    a 262k route on the 200k tier would read back as 200k.  ``modelSettings``
    then narrows the effective window to the route's exact published number.
    """

    if claude_aliases.desktop_keeps_model_id(public_name):
        return None
    if not isinstance(max_input_tokens, int) or isinstance(max_input_tokens, bool):
        return CODE_BEHAVES_AS_DEFAULT
    if max_input_tokens >= 1_000_000:
        return CODE_BEHAVES_AS_1M
    if max_input_tokens > CODE_DEFAULT_TIER_WINDOW:
        return CODE_BEHAVES_AS_LARGE
    return CODE_BEHAVES_AS_DEFAULT
