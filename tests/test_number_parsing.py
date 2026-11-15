"""Two readers that answer "cannot read" with a value, not with a crash."""

from __future__ import annotations

import unittest

from young_router.config import schema
from young_router.values import int_or_none


class IntOrNoneTests(unittest.TestCase):
    def test_what_it_can_read(self) -> None:
        self.assertEqual(12, int_or_none(12))
        self.assertEqual(12, int_or_none(12.0))
        self.assertEqual(12, int_or_none(" 12 "))

    def test_what_it_cannot_read_is_none(self) -> None:
        for value in (None, True, 1.5, "", "1.5", "-3", "twelve"):
            with self.subTest(value=value):
                self.assertIsNone(int_or_none(value))

    def test_a_digit_like_character_int_refuses_is_none_too(self) -> None:
        # ``"²".isdigit()`` is true and ``int("²")`` raises: the test that
        # guards the conversion has to be the one ``int`` itself applies.
        for value in ("²", "1²", "①"):
            with self.subTest(value=value):
                self.assertIsNone(int_or_none(value))


class MenuOrderTests(unittest.TestCase):
    def test_orders_it_can_read(self) -> None:
        self.assertEqual(2, schema._menu_order(2, "order"))
        self.assertEqual(2, schema._menu_order("2.0", "order"))
        self.assertEqual(1.5, schema._menu_order(1.5, "order"))

    def test_a_non_finite_order_is_invalid(self) -> None:
        for value in (float("inf"), float("nan"), "1e400", True, "", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    schema._menu_order(value, "order")

    def test_an_integer_too_large_for_a_float_is_invalid_not_an_overflow(self) -> None:
        # YAML reads a long digit literal as an int.  ``math.isfinite`` raises
        # ``OverflowError`` for one beyond a double, and every caller catches
        # only ``ValueError`` -- so the document failed with a traceback instead
        # of "is invalid".
        with self.assertRaises(ValueError):
            schema._menu_order(10**400, "order")


if __name__ == "__main__":
    unittest.main()
