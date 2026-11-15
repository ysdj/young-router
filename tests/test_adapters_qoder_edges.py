"""Qoder adapter edges a stub worker cannot reach on its own."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from young_router.adapters import qoder


class QoderStopEnvironmentTests(unittest.TestCase):
    """Stopping a worker must leave no address a Core-side read still trusts."""

    def test_stop_forgets_the_published_fragment(self) -> None:
        runtime = qoder.QoderRuntime(tempfile.mkdtemp())
        with mock.patch.dict(qoder._PUBLISHED_ENVIRONMENT, clear=True), mock.patch.dict(os.environ):
            runtime._port = 45454
            runtime._environment = runtime._proxy_environment()
            runtime.configure_environ()
            self.assertTrue(qoder.published_environment())
            runtime.stop()
            self.assertEqual(qoder.published_environment(), {})
            for name in (*qoder.API_BASE_ENV.values(), *qoder.API_KEY_ENV.values()):
                self.assertNotIn(name, os.environ)


if __name__ == "__main__":
    unittest.main()
