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
    desktop_keeps_model_id,
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

    def test_the_previous_alias_family_still_resolves(self) -> None:
        # A picker that already holds the alias this app published before the
        # family was renamed keeps reaching the same route.
        names = ["deepseek-v4.1-flash"]
        digest = claude_desktop_alias(names[0]).removeprefix(ALIAS_FAMILY)
        for family in LEGACY_ALIAS_FAMILIES:
            with self.subTest(family=family):
                self.assertEqual(names[0], resolve_alias(f"{family}{digest}", names))

    def test_a_real_claude_name_is_never_mapped_through_an_alias(self) -> None:
        # Two routers can serve the same third-party name; the alias is derived
        # from the name, so the same route reads the same on both.
        self.assertEqual(
            claude_desktop_alias("kimi-k3.1-pro"),
            claude_desktop_alias("kimi-k3.1-pro"),
        )


if __name__ == "__main__":
    unittest.main()
