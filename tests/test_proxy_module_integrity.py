"""Guard the proxy package against the two shapes a bad merge produces.

Both checks here come from one real outage: a refactor moved the pi-web-access
tool declarations into ``adapters/web_search.py`` and rewrote the proxy-side
wrapper, but the edit left the body of ``_responses_external_web_search_bridge_tools``
below the new ``return`` (so it was dead) and dropped the module import the
wrapper now calls.  Nothing failed at import time, so every request that carried
a hosted ``web_search`` declaration reached the fallback helper, raised
``NameError: name '_web_search_module' is not defined``, and the client
reported ``upstream_route_failure`` — "the upstream model route failed before a
final assistant response was available" — against routes that were healthy.

These are static checks on purpose.  A functional test of the bridge passes as
soon as the happy path is exercised again, while both defects stayed invisible
until a request actually took that path.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "young_router"


def _parsed_files():
    for path in sorted(PACKAGE.rglob("*.py")):
        try:
            yield path, ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as error:  # pragma: no cover - a syntax error is its own failure
            raise AssertionError(f"{path} does not parse: {error}") from error


class ProxyModuleIntegrityTests(unittest.TestCase):
    def test_no_client_or_source_states_the_banned_route_failure_sentence(self) -> None:
        """The generic "failed before a final assistant response" line is gone.

        Clients render that sentence as a retry tip and re-send a request the
        router already knows is dead, which is exactly the reconnect loop it
        used to cause: a context-window refusal — the most common real failure it
        covered — is unfixable by resending.  The rule is absolute, so it is
        checked here rather than left to a review: a cause that has a name gets
        its own branch, and the branch for unclassified stops says what happened
        without inviting the client to try the same bytes again.
        """

        banned = "failed before a final assistant response"
        offenders = []
        for path in sorted(PACKAGE.rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace").lower()
            if banned in text:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(
            offenders,
            [],
            "user-visible retry-tip sentence reintroduced in:\n" + "\n".join(offenders),
        )

    def test_no_function_continues_after_an_unconditional_return(self):
        """A ``return`` that ends a function leaves the rest of the body dead.

        The dropped function is not the interesting part; the interesting part
        is that Python accepts it silently, so the code below the ``return``
        only stops running and its callers fail later with a ``NameError`` or a
        ``TypeError`` instead of pointing at the edit.
        """

        offenders = []
        for path, tree in _parsed_files():
            for function in ast.walk(tree):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                body = function.body
                for index, statement in enumerate(body[:-1]):
                    if isinstance(statement, (ast.Return, ast.Raise)):
                        following = body[index + 1]
                        offenders.append(
                            f"{path.relative_to(ROOT)}:{function.name} "
                            f"line {following.lineno} is unreachable after "
                            f"the {type(statement).__name__.lower()} on line "
                            f"{statement.lineno}"
                        )
                        break
        self.assertEqual(offenders, [], "unreachable code after a terminal statement:\n" + "\n".join(offenders))

    def test_every_module_alias_used_in_the_package_is_imported(self):
        """``_foo_module.bar`` must resolve to an import in the same file.

        Reading a module through an alias is how the proxy avoids circular
        imports, and it defeats the usual undefined-name checks: the name is a
        plain global, so a missing import is only a ``NameError`` on the path
        that reaches it.
        """

        offenders = []
        for path, tree in _parsed_files():
            bound = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        bound.add(alias.asname or alias.name.split(".")[0])
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    bound.add(node.name)
                elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                    bound.add(node.id)
                elif isinstance(node, ast.arg):
                    bound.add(node.arg)
                elif isinstance(node, ast.ExceptHandler) and node.name:
                    bound.add(node.name)
                elif isinstance(node, (ast.Global, ast.Nonlocal)):
                    bound.update(node.names)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Name) or not isinstance(node.ctx, ast.Load):
                    continue
                if node.id.startswith("_") and node.id.endswith("_module") and node.id not in bound:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} uses unimported {node.id}")
        self.assertEqual(offenders, [], "module aliases used without an import:\n" + "\n".join(offenders))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
