"""Catalog lookups answer from indexes, and answer exactly what the scans did.

``ModelContextRegistry._records`` is the model catalog: a public model's
projection asks it for image metadata once per route, and one measured dispatch
made that call 326 times over 2 161 records -- about 100 ms of a 118 ms edit,
spent re-walking the same mapping.  The same ordered walk sat in ``_lookup``,
which every route asks during a projection.

These tests pin the two properties that make replacing a scan with an index
safe: the answers are the ones the scans produced (including which record wins
a tie), and after the index is built a lookup does not walk the records at all.
The second is what a timing test cannot state on a busy machine, and it is the
one that regresses silently when someone adds a read back into the loop.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
import unittest
from unittest import mock

from young_router.core.model_contexts import ModelContextRegistry, RecordLookupIndex, _candidate_ids, _lookup
from young_router.values import positive_int


def _record(metadata: dict[str, Any] | None, priority: int | None = None, window: int = 400_000) -> dict[str, Any]:
    record: dict[str, Any] = {"context_window": window, "max_context_window": window}
    if metadata is not None:
        record["image_model_metadata"] = dict(metadata)
    if priority is not None:
        record["priority"] = priority
    return record


def _naive_image_metadata(records: Mapping[str, Mapping[str, Any]], model_id: str) -> dict[str, Any] | None:
    """What ``image_metadata_for_model_id`` answered before the index existed."""

    candidates = _candidate_ids(model_id)
    matches: list[tuple[int, int, dict[str, Any]]] = []
    for index, (record_id, record) in enumerate(records.items()):
        if not isinstance(record, Mapping) or not isinstance(record.get("image_model_metadata"), Mapping):
            continue
        if any(record_id == candidate or record_id.endswith(f"/{candidate}") for candidate in candidates):
            matches.append((positive_int(record.get("priority")) or 0, -index, dict(record)))
    if not matches:
        return None
    metadata = max(matches, key=lambda item: (item[0], item[1]))[2].get("image_model_metadata")
    return dict(metadata) if isinstance(metadata, Mapping) else None


CATALOG: dict[str, dict[str, Any]] = {
    # The same model under three providers, with different priorities: the
    # highest priority wins, so the answer may not depend on the record order.
    "alpha/gpt-image-1": _record({"type": "image", "publisher": "alpha"}, priority=1, window=0),
    "beta/gpt-image-1": _record({"type": "image", "publisher": "beta"}, priority=3, window=0),
    "gamma/gpt-image-1": _record({"type": "image", "publisher": "gamma"}, priority=3, window=0),
    # Exact multi-segment ids, which the tail index alone cannot answer.
    "openai/gpt-image-public": _record({"type": "image", "publisher": "openai"}, priority=2, window=0),
    # A suffix match on a longer key, and records that are not image models.
    "relay.example.test/openai/gpt-image-1": _record({"type": "image", "publisher": "relay"}, priority=0, window=0),
    "alpha/gpt-5.1": _record(None, priority=9),
    "beta/gpt-5.1": _record(None, priority=9),
    "openai/gpt-5.1": _record(None, priority=2),
}

LOOKUPS = (
    "gpt-image-1",
    "gpt-image-public",
    "openai/gpt-image-1",
    "openai/gpt-image-public",
    "alpha/gpt-image-1",
    "gpt-image-1[1m]",
    "openai.gpt-image-1",
    "gpt-5.1",
    "openai/gpt-5.1",
    "unknown-model",
)


class RecordLookupIndexTests(unittest.TestCase):
    def registry(self, records: Mapping[str, Mapping[str, Any]]) -> ModelContextRegistry:
        registry = ModelContextRegistry()
        registry._sync_cache()
        registry._records = RecordLookupIndex({key: dict(value) for key, value in records.items()})
        return registry

    def test_image_metadata_is_what_the_ordered_scan_answered(self) -> None:
        registry = self.registry(CATALOG)
        for model_id in LOOKUPS:
            with self.subTest(model_id=model_id):
                self.assertEqual(_naive_image_metadata(CATALOG, model_id), registry.image_metadata_for_model_id(model_id))

    def test_equal_priorities_keep_the_record_the_scan_saw_first(self) -> None:
        registry = self.registry(CATALOG)
        # beta and gamma both state priority 3; beta comes first in the map.
        self.assertEqual("beta", registry.image_metadata_for_model_id("gpt-image-1")["publisher"])
        reordered = RecordLookupIndex()
        for key in ("gamma/gpt-image-1", "beta/gpt-image-1", "alpha/gpt-image-1"):
            reordered[key] = dict(CATALOG[key])
        registry._records = reordered
        self.assertEqual("gamma", registry.image_metadata_for_model_id("gpt-image-1")["publisher"])

    def test_a_second_lookup_walks_no_record(self) -> None:
        registry = self.registry(CATALOG)
        first = registry.image_metadata_for_model_id("gpt-image-1")
        records = registry._records
        self.assertIsNotNone(first)
        # Warm every index the lookup reuses, then make walking impossible.
        self.assertIsNotNone(_lookup(records, "gpt-5.1"))
        self.assertIsNotNone(records.image_matches(["gpt-image-1"]))

        def refuse(*_args: object, **_kwargs: object):
            raise AssertionError("the lookup walked the records again")

        with mock.patch.object(RecordLookupIndex, "__iter__", refuse), \
                mock.patch.object(RecordLookupIndex, "items", refuse), \
                mock.patch.object(RecordLookupIndex, "values", refuse):
            self.assertEqual(first, registry.image_metadata_for_model_id("gpt-image-1"))
            self.assertIsNotNone(_lookup(records, "gpt-5.1"))

    def test_a_record_written_into_the_map_is_found_at_once(self) -> None:
        registry = self.registry({"alpha/gpt-5.1": _record(None, priority=1)})
        self.assertIsNone(registry.image_metadata_for_model_id("gpt-image-9"))
        registry._records["alpha/gpt-image-9"] = _record({"type": "image"}, priority=4, window=0)
        self.assertEqual({"type": "image"}, registry.image_metadata_for_model_id("gpt-image-9"))
        del registry._records["alpha/gpt-image-9"]
        self.assertIsNone(registry.image_metadata_for_model_id("gpt-image-9"))

    def test_a_plain_mapping_is_still_answered(self) -> None:
        registry = self.registry({})
        registry._records = {key: dict(value) for key, value in CATALOG.items()}  # type: ignore[assignment]
        for model_id in LOOKUPS:
            with self.subTest(model_id=model_id):
                self.assertEqual(_naive_image_metadata(CATALOG, model_id), registry.image_metadata_for_model_id(model_id))


class LookupOrderTests(unittest.TestCase):
    def test_lookup_answers_what_the_unindexed_mapping_answered(self) -> None:
        """The indexed map and a plain dict of the same records agree."""

        indexed = RecordLookupIndex({key: dict(value) for key, value in CATALOG.items()})
        plain = {key: dict(value) for key, value in CATALOG.items()}
        for model_id in LOOKUPS:
            with self.subTest(model_id=model_id):
                self.assertEqual(_lookup(plain, model_id), _lookup(indexed, model_id))

    def test_lookup_reports_the_highest_priority_the_scan_saw(self) -> None:
        records = RecordLookupIndex({key: dict(value) for key, value in CATALOG.items()})
        self.assertEqual(9, _lookup(records, "gpt-5.1")["priority"])
        self.assertEqual(1, _lookup(records, "alpha/gpt-image-1")["priority"])


if __name__ == "__main__":
    unittest.main()
