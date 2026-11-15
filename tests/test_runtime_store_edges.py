"""Edge cases for the shared-state store: legacy documents and log imports."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from young_router import runtime_store


class RuntimeStoreEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_read_text_adopts_a_document_that_exists_only_as_a_legacy_file(self) -> None:
        path = Path(self.directory.name) / "deployment-concurrency.json"
        path.write_text(json.dumps({"leases": {"old": {"pid": 1}}}), encoding="utf-8")

        self.assertTrue(runtime_store.has_document(path))
        self.assertEqual(
            {"leases": {"old": {"pid": 1}}},
            json.loads(runtime_store.read_text(path) or "{}"),
        )

    def test_a_first_small_read_does_not_truncate_the_adopted_log(self) -> None:
        log = Path(self.directory.name) / "recent-requests.jsonl"
        log.write_text(
            "".join(json.dumps({"request_id": f"old-{index}"}) + "\n" for index in range(3)),
            encoding="utf-8",
        )

        self.assertEqual(1, len(runtime_store.read_records(log, limit=1)))
        self.assertEqual(3, runtime_store.record_count(log))

    def test_a_first_small_records_after_read_does_not_truncate_the_adopted_log(self) -> None:
        log = Path(self.directory.name) / "recent-requests.jsonl"
        log.write_text(
            "".join(json.dumps({"request_id": f"old-{index}"}) + "\n" for index in range(3)),
            encoding="utf-8",
        )

        self.assertEqual(1, len(runtime_store.records_after(log, 0, limit=1)))
        self.assertEqual(3, runtime_store.record_count(log))

    def test_a_delete_that_cannot_remove_the_legacy_file_reports_the_failure(self) -> None:
        # The row used to go first and the unlink failure was dropped, so the
        # next read imported the "deleted" document back out of the old file.
        path = Path(self.directory.name) / "deployment-concurrency.json"
        path.write_text(json.dumps({"leases": {"a": {}}}), encoding="utf-8")
        runtime_store.read_document(path)
        version = runtime_store.document_version(path)

        with mock.patch("os.unlink", side_effect=PermissionError("read-only directory")):
            with self.assertRaises(OSError):
                runtime_store.delete_document(path)
            self.assertEqual(version, runtime_store.document_version(path))

        self.assertEqual({"leases": {"a": {}}}, runtime_store.read_document(path))


if __name__ == "__main__":
    unittest.main()
