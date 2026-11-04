"""The loop that keeps generated client model lists current without a window.

Its contract is what a person sees: an edit anywhere that changes the router's
model names has to reach Codex's catalog without anybody reopening a pane.
Claude Desktop discovers ``/v1/models`` itself, and Claude Code's list follows
the reload this loop asks for, so neither is rewritten on a timer.
"""

from __future__ import annotations

import pathlib
import tempfile
import time
import unittest

from young_router.core.client_model_sync import ClientModelSyncScheduler, attach


def _wait_for(predicate, *, seconds: float = 6.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class ClientModelSyncSchedulerTests(unittest.TestCase):
    def test_an_external_edit_to_the_document_triggers_a_pass(self) -> None:
        # The path this exists for: nobody told this process anything.  The
        # document's own timestamp is the signal, so the lists follow in about a
        # second instead of on a five-minute clock.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        document = pathlib.Path(directory.name) / "config.yaml"
        document.write_text("model_list: []\n", encoding="utf-8")

        calls: list[float] = []
        scheduler = ClientModelSyncScheduler(
            refresh=lambda: calls.append(time.monotonic()),
            watch_paths=[document],
            poll_seconds=0.05,
            settle_seconds=(0.0,),
        )
        scheduler.start()
        self.addCleanup(scheduler.stop)
        self.assertTrue(_wait_for(lambda: calls), "a first pass runs at start")

        before = len(calls)
        document.write_text("model_list: [{model_name: new-route}]\n", encoding="utf-8")
        self.assertTrue(
            _wait_for(lambda: len(calls) > before),
            "an edited document must be picked up without waiting out a clock",
        )

    def test_only_an_observed_change_asks_for_an_adoption(self) -> None:
        # A reload restages the router's own document, and the staged document
        # is often the file watched here.  If every refresh asked for a reload,
        # that reload's restage would read as the next change: the proxy would
        # replace its workers forever.  Adoption therefore belongs to the pass
        # that noticed the document move; the settle retries and the fallback
        # only re-derive the client lists.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        document = pathlib.Path(directory.name) / "config.yaml"
        document.write_text("one\n", encoding="utf-8")

        changes: list[int] = []
        refreshes: list[int] = []
        scheduler = ClientModelSyncScheduler(
            refresh=lambda: refreshes.append(1),
            on_change=lambda: changes.append(1),
            watch_paths=[document],
            poll_seconds=0.02,
            settle_seconds=(0.0, 0.05, 0.1),
            fallback_seconds=0.15,
        )
        scheduler.start()
        self.addCleanup(scheduler.stop)
        self.assertTrue(_wait_for(lambda: changes), "the launch pass adopts once")
        adopted = len(changes)
        refreshed = len(refreshes)

        document.write_text("two\n", encoding="utf-8")
        self.assertTrue(
            _wait_for(lambda: len(refreshes) >= refreshed + 3),
            "the settle ladder still re-derives the lists",
        )
        self.assertEqual(adopted + 1, len(changes), "one adoption for one observed change")

        self.assertTrue(
            _wait_for(lambda: len(refreshes) >= refreshed + 4),
            "the fallback keeps re-deriving the lists",
        )
        self.assertEqual(adopted + 1, len(changes), "a fallback pass reloads nothing")

    def test_a_change_is_followed_by_retries_until_the_router_catches_up(self) -> None:
        # The router serves /v1/models from its running process and reloads its
        # workers on its own schedule, so the first read after a write can still
        # answer the old list.  The settle ladder is what makes that recoverable.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        document = pathlib.Path(directory.name) / "config.yaml"
        document.write_text("one\n", encoding="utf-8")

        calls: list[float] = []
        scheduler = ClientModelSyncScheduler(
            refresh=lambda: calls.append(time.monotonic()),
            watch_paths=[document],
            poll_seconds=0.05,
            settle_seconds=(0.0, 0.3),
        )
        scheduler.start()
        self.addCleanup(scheduler.stop)
        self.assertTrue(_wait_for(lambda: calls))

        before = len(calls)
        document.write_text("two\n", encoding="utf-8")
        self.assertTrue(_wait_for(lambda: len(calls) >= before + 2, seconds=6.0))

    def test_the_background_pass_never_rewrites_claude(self) -> None:
        # The loop still owns Codex's catalog and the reload it asks for; the
        # Claude lists are discovery's and the reload's, not a timer's.
        class Core:
            def __init__(self) -> None:
                self.refreshed: list[bool] = []

            def refresh_client_model_lists(self, *, include_claude: bool = True) -> None:
                self.refreshed.append(include_claude)

            def adopt_external_route_change(self) -> None:
                return None

        core = Core()
        scheduler = attach(core)
        scheduler.start()
        self.addCleanup(scheduler.stop)
        self.assertTrue(_wait_for(lambda: core.refreshed))
        self.assertEqual([False], core.refreshed)

    def test_a_failing_pass_does_not_kill_the_loop(self) -> None:
        calls: list[int] = []

        def refresh() -> None:
            calls.append(1)
            raise RuntimeError("proxy still starting")

        scheduler = ClientModelSyncScheduler(
            refresh=refresh,
            poll_seconds=0.05,
            settle_seconds=(0.05,),
            fallback_seconds=0.1,
        )
        scheduler.start()
        self.addCleanup(scheduler.stop)
        self.assertTrue(_wait_for(lambda: len(calls) >= 2))

    def test_a_document_that_never_moves_still_reconciles_on_the_fallback(self) -> None:
        # An installation whose roster is set once by an older release has no
        # timestamp change to observe, so one slow pass keeps it honest.
        calls: list[int] = []
        scheduler = ClientModelSyncScheduler(
            refresh=lambda: calls.append(1),
            poll_seconds=0.05,
            fallback_seconds=0.2,
        )
        scheduler.start()
        self.addCleanup(scheduler.stop)
        self.assertTrue(_wait_for(lambda: len(calls) >= 2))


if __name__ == "__main__":
    unittest.main()
