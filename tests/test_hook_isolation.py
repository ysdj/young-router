"""The hook suite's LiteLLM stub must not starve the process-wide module.

``hook_test_utils.load_hook_module`` installs a stub ``litellm`` so the hook
package can be imported without the real package, and that stub stays in
``sys.modules`` for the whole test that loaded it.  Anything the rest of the
suite reads from ``litellm`` has to keep answering - the model catalog and the
context registry read ``model_cost`` - or a later, unrelated test fails with a
symptom that looks like a bug in the code under test.
"""
from __future__ import annotations

import sys
import unittest
from unittest import mock

from hook_test_utils import load_hook_module


class HookStubIsolationTests(unittest.TestCase):
    def test_the_stub_answers_for_the_installed_package(self) -> None:
        hooks, _ = load_hook_module()
        import litellm

        # The stub is what the hook modules imported, and it is what the
        # process-wide module table holds.
        self.assertIs(hooks.litellm, litellm)
        self.assertIs(sys.modules["litellm"], litellm)
        # The names the stub overrides stay its own.
        self.assertTrue(issubclass(litellm.InternalServerError, Exception))
        # Everything else falls through to the installed package, so a later
        # test that reads the cost map, or patches it, still finds it.
        self.assertIsInstance(litellm.model_cost, dict)
        with mock.patch("litellm.model_cost", {}):
            self.assertEqual({}, litellm.model_cost)
        self.assertIsInstance(litellm.model_cost, dict)


if __name__ == "__main__":
    unittest.main()
