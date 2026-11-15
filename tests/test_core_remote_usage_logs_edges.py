from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))
from young_router.core import remote_usage_logs  # noqa: E402


class RemoteUsageLogEdgesTests(unittest.TestCase):
    def test_a_non_finite_duration_does_not_discard_the_row(self) -> None:
        """A third-party row may spell a duration as bare NaN or Infinity.

        Such a value is not a duration, so that column stays empty; raising
        here would drop the whole relay's page instead of one field.
        """

        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.assertEqual(
                    {"model": "m"},
                    remote_usage_logs._row_details({"model": "m", "use_time": value}),
                )
                self.assertEqual(
                    "",
                    remote_usage_logs._duration_milliseconds({"duration": value}),
                )
