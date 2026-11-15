"""The request Core is handed is the one the browser sent.

``/ui`` and ``/ipc`` are Core's own protocol, so the courier may not reshape the
request on the way: a header stated twice -- by the browser, or by a reverse
proxy in front of this port -- arrives twice rather than being collapsed.
"""

from __future__ import annotations

import unittest
from unittest import mock

from young_router.proxy import web_forward


class _Answer:
    status = 204

    def read(self) -> bytes:
        return b""

    def getheaders(self) -> list[tuple[str, str]]:
        return []


class _Connection:
    """A stand-in for ``http.client`` that keeps the header lines it is given."""

    opened: list["_Connection"] = []

    def __init__(self, address: str, port: int, timeout: float) -> None:
        self.pairs: list[tuple[str, str]] = []
        type(self).opened.append(self)

    def putrequest(self, method: str, path: str, **options: object) -> None:
        return

    def putheader(self, name: str, value: str) -> None:
        self.pairs.append((name, value))

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        # A mapping holds a name once, so this is where a second copy of the
        # same header would be lost on the way to the server.
        for name, value in (headers or {}).items():
            self.putheader(name, value)

    def endheaders(self, body: bytes | None = None) -> None:
        return

    def getresponse(self) -> _Answer:
        return _Answer()

    def close(self) -> None:
        return


class RepeatedHeaderTests(unittest.TestCase):
    def test_a_header_stated_twice_reaches_core_twice(self) -> None:
        # A reverse proxy in front of this port may send a second `Cookie`;
        # keeping only the last value hands Core a different session.
        with mock.patch.object(web_forward.http.client, "HTTPConnection", _Connection):
            web_forward._exchange(
                ("127.0.0.1", 9),
                "GET",
                "/ui/",
                [(b"cookie", b"a=1"), (b"cookie", b"b=2")],
                b"",
                5.0,
            )
        pairs = _Connection.opened[-1].pairs
        self.assertEqual(
            ["a=1", "b=2"], [value for name, value in pairs if name.lower() == "cookie"]
        )


if __name__ == "__main__":
    unittest.main()
