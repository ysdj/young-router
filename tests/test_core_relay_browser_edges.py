"""How a CDP exchange meets the event loop of the thread that asks for it.

`_run` is the one place the browser protocol is turned into a blocking call.
There is no second loop to fall back on: a thread whose loop is already running
cannot wait on itself, and a coroutine that already raised cannot be awaited a
second time.  A retry there can therefore only ever fail, and it replaced the
exchange's own error with its own -- so these tests pin that the original error
is what a caller sees.
"""

from __future__ import annotations

import asyncio
import unittest

from young_router.core import relay_browser


class RunTests(unittest.TestCase):
    def test_a_runtime_error_from_the_exchange_is_not_masked(self) -> None:
        """A failure inside the exchange must arrive as itself."""

        async def exchange() -> None:
            raise RuntimeError("station closed the socket")

        with self.assertRaises(RuntimeError) as caught:
            relay_browser._run(exchange())
        self.assertEqual("station closed the socket", str(caught.exception))

    def test_a_caller_that_already_owns_a_loop_is_told_so(self) -> None:
        """The message a caller in a loop needs, not one from a second loop."""

        async def exchange() -> str:
            return "answered"

        async def call() -> str:
            coroutine = exchange()
            try:
                with self.assertRaises(RuntimeError) as caught:
                    relay_browser._run(coroutine)
            finally:
                coroutine.close()
            return str(caught.exception)

        message = asyncio.run(call())
        self.assertIn("asyncio.run() cannot be called from a running event loop", message)
