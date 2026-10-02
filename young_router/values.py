"""Small value coercions shared by the config editor, Core, and the hook.

The functions here accept the shapes user configuration and learned model
metadata actually carry (ints, integral floats, digit strings, and the usual
boolean spellings), and they answer ``None`` for anything they cannot read, so
a caller decides what a missing value means.  The module is dependency-free on
purpose: ``base`` re-exports these names for the proxy's own modules.
"""

from __future__ import annotations

import re
from typing import Any, Optional

_TRUE_SPELLINGS = frozenset({"1", "true", "yes", "on", "enabled"})
_FALSE_SPELLINGS = frozenset({"0", "false", "no", "off", "disabled"})


def int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def explicit_bool(value: Any) -> Optional[bool]:
    """Normalize an explicitly supplied boolean without inventing a default."""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_SPELLINGS:
            return True
        if normalized in _FALSE_SPELLINGS:
            return False
    return None


def positive_int(value: Any, *, allow_grouped_digits: bool = False) -> Optional[int]:
    """Read a positive integer, or ``None`` when the value is not one.

    ``allow_grouped_digits`` additionally tolerates thousands separators
    (``"1,000"``), which is the shape a hand-written configuration file may
    use; learned model metadata is read strictly.
    """

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value > 0 else None
    text = str(value).strip()
    if allow_grouped_digits:
        text = text.replace(",", "")
    if not re.fullmatch(r"[0-9]+", text):
        return None
    number = int(text)
    return number if number > 0 else None


__all__ = ["explicit_bool", "int_or_none", "positive_int"]
