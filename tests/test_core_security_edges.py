from __future__ import annotations

import unittest

from young_router.core.security import REDACT_TEXT, redact


class RedactPlainStringBoundTests(unittest.TestCase):
    def test_a_long_safe_string_is_cut_the_way_redact_text_cuts_it(self) -> None:
        # The fast path proved a string unchanged without looking at its
        # length, so a field over the cap travelled whole from a snapshot
        # where REDACT_TEXT would have cut it.
        value = "a" * 600

        self.assertEqual(REDACT_TEXT(value), redact(value))
        self.assertEqual(512, len(redact(value)))


if __name__ == "__main__":
    unittest.main()
