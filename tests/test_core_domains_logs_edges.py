from __future__ import annotations

import tempfile
import unittest

from young_router.core.domains.logs import LogsDomain


class _SwitchingUsageReader:
    """A first relay with history, and a second that cannot answer."""

    def __init__(self, failure: str) -> None:
        self.failure = failure

    def refresh(
        self,
        source_id: str | None = None,
        *,
        page: int = 1,
        rows_per_page: int | None = None,
    ) -> object:
        if source_id == "one":
            return [{"timestamp": "2026-01-01T00:00:00Z", "model": "old"}], "", 1
        if self.failure == "raise":
            raise RuntimeError("relay went away")
        return [], "unsupported"


class LogsDomainEdgeTests(unittest.TestCase):
    def test_switching_to_an_unsupported_relay_drops_the_previous_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = LogsDomain(directory, online_usage_reader=_SwitchingUsageReader("unsupported"))
            domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage", "source": "one"})
            self.assertEqual(1, domain.view("online-usage")["log"]["line_count"])

            domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage", "source": "two"})
            tab = domain.view("online-usage")["log"]

            self.assertEqual("two", tab["source_id"])
            self.assertEqual([], tab["records"])
            self.assertEqual(0, tab["line_count"])
            self.assertEqual(0, tab["total"])
            self.assertEqual("unsupported", tab["reason"])

    def test_a_failed_switch_does_not_show_the_previous_relays_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = LogsDomain(directory, online_usage_reader=_SwitchingUsageReader("raise"))
            domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage", "source": "one"})
            self.assertEqual(1, domain.view("online-usage")["log"]["total"])

            domain.dispatch("logs.refresh_online_usage", {"tab": "online-usage", "source": "two"})
            tab = domain.view("online-usage")["log"]

            self.assertEqual("two", tab["source_id"])
            self.assertEqual([], tab["records"])
            self.assertEqual(0, tab["line_count"])
            self.assertEqual(0, tab["total"])
