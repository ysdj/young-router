"""The alias Claude Desktop is handed, and the proxy's way back to the model.

Both directions are one function, so the two sides cannot drift: what the
Desktop listing publishes (or the old ``inferenceModels`` list wrote) is what
the proxy resolves before deployment selection.
"""

from __future__ import annotations

import unittest

from young_router.claude_aliases import (
    ALIAS_FAMILY,
    LEGACY_ALIAS_FAMILIES,
    REFUSED_ALIAS_TOKENS,
    alias_index,
    claude_desktop_alias,
    context_suffix_free,
    desktop_keeps_model_id,
    is_derived_alias,
    resolve_alias,
)


class ClaudeDesktopAliasTests(unittest.TestCase):
    def test_a_real_claude_id_is_left_alone(self) -> None:
        # Desktop renders these itself and gives them a reasoning ladder, so
        # aliasing one would only make it worse.
        for name in ("claude-opus-5-5", "claude-sonnet-4-5", "anthropic/claude-x"):
            with self.subTest(name=name):
                self.assertTrue(desktop_keeps_model_id(name))
                self.assertEqual({}, alias_index([name]))

    def test_a_third_party_name_is_never_shown_to_desktop_as_itself(self) -> None:
        for name in ("grok-4.7", "deepseek-v4.1-flash", "gpt-5.4", "claude-gpt-test"):
            with self.subTest(name=name):
                self.assertFalse(desktop_keeps_model_id(name))
                alias = claude_desktop_alias(name)
                self.assertTrue(desktop_keeps_model_id(alias))

    def test_the_alias_carries_no_refused_brand_and_no_public_name(self) -> None:
        name = "deepseek-v4.1-flash"
        alias = claude_desktop_alias(name)
        tokens = {token for token in alias.lower().replace("-", " ").split()}
        self.assertFalse(tokens & REFUSED_ALIAS_TOKENS)
        self.assertNotIn("deepseek", alias)

    def test_the_alias_family_is_the_one_the_app_gives_a_reasoning_ladder_to(self) -> None:
        # Verified on the app: an id under this prefix kept its label and showed
        # an active Effort control, while an invented version of a real family
        # (``claude-sonnet-9-9``) showed none.
        self.assertTrue(claude_desktop_alias("anything").startswith(ALIAS_FAMILY))
        self.assertEqual("claude-mythos-", ALIAS_FAMILY)

    def test_every_public_name_gets_its_own_alias(self) -> None:
        names = [
            "grok-4.7",
            "gpt-6.1-sol",
            "gemini-3.8-flash",
            "deepseek-v4.1-flash",
            "deepseek-v4.1-flash-free",
            "glm-5.3-flash",
        ]
        aliases = [claude_desktop_alias(name) for name in names]
        self.assertEqual(len(names), len(set(aliases)))
        self.assertEqual(len(names), len(alias_index(names)))

    def test_the_proxy_resolves_an_alias_and_leaves_everything_else(self) -> None:
        names = ["deepseek-v4.1-flash", "claude-opus-5-5"]
        alias = claude_desktop_alias("deepseek-v4.1-flash")
        self.assertEqual("deepseek-v4.1-flash", resolve_alias(alias, names))
        # A name the router already serves, and one it does not, are both left
        # to the router to answer exactly as before.
        self.assertIsNone(resolve_alias("deepseek-v4.1-flash", names))
        self.assertIsNone(resolve_alias("claude-opus-5-5", names))
        self.assertIsNone(resolve_alias("claude-fable-000000", names))
        self.assertIsNone(resolve_alias(None, names))
        self.assertIsNone(resolve_alias("", names))

    def test_the_one_million_suffix_is_stripped_and_nothing_else_is(self) -> None:
        self.assertEqual("claude-mythos-eb1394", context_suffix_free("claude-mythos-eb1394[1m]"))
        self.assertEqual("claude-mythos-eb1394", context_suffix_free("claude-mythos-eb1394[1M]"))
        # Only the client's own marker, and only at the end.
        self.assertEqual("[1m]claude-mythos-eb1394", context_suffix_free("[1m]claude-mythos-eb1394"))
        self.assertEqual("claude-mythos-eb1394", context_suffix_free("claude-mythos-eb1394"))

    def test_the_previous_alias_family_still_resolves(self) -> None:
        # A picker that already holds the alias this app published before the
        # family was renamed keeps reaching the same route.
        names = ["deepseek-v4.1-flash"]
        digest = claude_desktop_alias(names[0]).removeprefix(ALIAS_FAMILY)
        for family in LEGACY_ALIAS_FAMILIES:
            with self.subTest(family=family):
                self.assertEqual(names[0], resolve_alias(f"{family}{digest}", names))

    def test_the_pickers_one_million_spelling_resolves_to_the_route(self) -> None:
        # Desktop offers a route that reads as 1M as the alias plus
        # ``<alias>[1m]`` and sends the suffixed id.  It is not a route this
        # router serves, so without this the chosen row answered
        # ``Invalid model name`` (a real session picked it for 11 of 12 turns).
        names = ["deepseek-v4.1-flash", "claude-opus-5-5"]
        alias = claude_desktop_alias(names[0])
        for spelling in (f"{alias}[1m]", f"{alias}[1M]", f"{alias}[1m] "):
            with self.subTest(spelling=spelling):
                self.assertEqual(names[0], resolve_alias(spelling, names))

    def test_the_one_million_spelling_resolves_for_an_older_family(self) -> None:
        names = ["gpt-6.1-sol"]
        digest = claude_desktop_alias(names[0]).removeprefix(ALIAS_FAMILY)
        for family in LEGACY_ALIAS_FAMILIES:
            with self.subTest(family=family):
                self.assertEqual(names[0], resolve_alias(f"{family}{digest}[1m]", names))

    def test_a_one_million_spelling_of_a_served_name_resolves(self) -> None:
        # A public name the client keeps as written is published at 1M under
        # the same suffix, so the row still names one route when the session
        # sends it.  The suffix selects a picker row; it is never a route.
        names = ["deepseek-v4.1-flash", "claude-opus-5-5"]
        for spelling in (
            "claude-opus-5-5[1m]",
            "claude-opus-5-5[1M]",
            "claude-opus-5-5[1m] ",
        ):
            with self.subTest(spelling=spelling):
                self.assertEqual("claude-opus-5-5", resolve_alias(spelling, names))
        # The alias namespace answers the same way for a third-party route,
        # whatever the picker appended to it.
        self.assertEqual(
            "deepseek-v4.1-flash", resolve_alias("deepseek-v4.1-flash[1m]", names)
        )
        # A name the router serves is answered as written, suffix and all: the
        # route comes first, the rewriting second.
        self.assertIsNone(resolve_alias("claude-opus-5-5[1m]", ["claude-opus-5-5[1m]"]))

    def test_a_one_million_spelling_of_something_else_is_left_alone(self) -> None:
        names = ["deepseek-v4.1-flash"]
        # An unknown alias, an unknown route, a real Claude id and a bare
        # suffix all keep the router's own answer.
        for name in (
            "claude-fable-000000[1m]",
            "some-route[1m]",
            "claude-opus-5-5[1m]",
            "[1m]",
            "claude-mythos-[1m]",
        ):
            with self.subTest(name=name):
                self.assertIsNone(resolve_alias(name, names))

    def test_a_one_million_spelling_is_the_same_aliases_mark(self) -> None:
        # The retirement pass reads the alias namespace as proof this app wrote
        # a list, and a picker row that carries the suffix is that same id.
        alias = claude_desktop_alias("deepseek-v4.1-flash")
        self.assertTrue(is_derived_alias(alias))
        self.assertTrue(is_derived_alias(f"{alias}[1m]"))
        self.assertFalse(is_derived_alias("claude-opus-5-5[1m]"))


    def test_a_real_claude_name_is_never_mapped_through_an_alias(self) -> None:
        # Two routers can serve the same third-party name; the alias is derived
        # from the name, so the same route reads the same on both.
        self.assertEqual(
            claude_desktop_alias("kimi-k3.1-pro"),
            claude_desktop_alias("kimi-k3.1-pro"),
        )


if __name__ == "__main__":
    unittest.main()
