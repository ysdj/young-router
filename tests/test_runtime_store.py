"""The shared-state store: one document, one write transaction, no file.

The proxy's sixteen workers used to share six small documents through a sidecar
lock file, a blocking ``flock``, a whole-document parse, two serialisations to
see whether the document moved, and an atomic replace with two fsyncs -- on
every request attempt, on the event loop.  What these tests pin is the contract
the callers were written against: the same document, the same callback, a
commit only when the callback changed something, a read that does not take the
write lock, a legacy JSON file adopted once, and no file written beside it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from young_router import runtime_store


class RuntimeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "deployment-concurrency.json"

    def test_the_document_is_read_back_and_no_file_is_written(self) -> None:
        runtime_store.update_document(self.path, lambda payload: payload.setdefault("leases", {}).update({"a": 1}))
        self.assertEqual({"leases": {"a": 1}}, runtime_store.read_document(self.path))
        self.assertEqual({"leases": {"a": 1}}, json.loads(runtime_store.read_text(self.path) or "{}"))
        self.assertFalse(self.path.exists(), "the document was written as a file again")
        self.assertTrue((Path(self.directory.name) / runtime_store.DATABASE_NAME).exists())

    def test_a_legacy_file_is_adopted_once(self) -> None:
        self.path.write_text(json.dumps({"leases": {"old": {"pid": 1}}}), encoding="utf-8")
        self.assertEqual({"leases": {"old": {"pid": 1}}}, runtime_store.read_document(self.path))
        # The file the previous build wrote is not a second writer: once the
        # store holds the document, the file beside it is history.
        self.path.write_text(json.dumps({"leases": {"stale": {}}}), encoding="utf-8")
        self.assertEqual({"leases": {"old": {"pid": 1}}}, runtime_store.read_document(self.path))

    def test_the_version_moves_only_when_the_document_does(self) -> None:
        runtime_store.update_document(self.path, lambda payload: payload.update({"leases": {}}))
        first = runtime_store.document_version(self.path)
        self.assertEqual(1, runtime_store.update_document(self.path, lambda payload: len(payload)))
        self.assertEqual(first, runtime_store.document_version(self.path))
        runtime_store.update_document(self.path, lambda payload: payload["leases"].update({"b": 2}))
        self.assertNotEqual(first, runtime_store.document_version(self.path))

    def test_a_read_only_update_answers_from_a_snapshot(self) -> None:
        runtime_store.update_document(self.path, lambda payload: payload.update({"leases": {"a": {}}}))
        version = runtime_store.document_version(self.path)

        def prune_and_count(payload: dict) -> int:
            payload["leases"].clear()  # a read-only caller's write is dropped
            return 1

        self.assertEqual(1, runtime_store.update_document(self.path, prune_and_count, read_only=True))
        self.assertEqual(version, runtime_store.document_version(self.path))
        self.assertEqual({"leases": {"a": {}}}, runtime_store.read_document(self.path))

    def test_another_connection_sees_a_committed_change(self) -> None:
        runtime_store.update_document(self.path, lambda payload: payload.update({"leases": {}}))
        seen: list[dict] = []

        def read_in_another_thread() -> None:
            seen.append(runtime_store.read_document(self.path))
            runtime_store.update_document(self.path, lambda payload: payload["leases"].update({"t": 1}))

        thread = threading.Thread(target=read_in_another_thread)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual({"leases": {}}, seen[0])
        self.assertEqual({"leases": {"t": 1}}, runtime_store.read_document(self.path))

    def test_delete_removes_the_document_and_the_file_it_came_from(self) -> None:
        self.path.write_text(json.dumps({"recoveries": {"a": {}}}), encoding="utf-8")
        self.assertTrue(runtime_store.has_document(self.path))
        self.assertTrue(runtime_store.delete_document(self.path))
        self.assertFalse(runtime_store.has_document(self.path))
        self.assertFalse(self.path.exists())
        self.assertEqual({}, runtime_store.read_document(self.path))

    def test_a_legacy_file_that_is_not_utf8_is_no_document(self) -> None:
        # A decode error is a ``ValueError``: it left through neither the
        # ``OSError`` handler around the read nor the one around the parse.
        self.path.write_bytes(b'{"leases": "\xff\xfe"}')
        self.assertEqual({}, runtime_store.read_document(self.path))
        self.assertFalse(runtime_store.has_document(self.path))

    def test_a_schema_that_failed_once_is_built_on_the_next_use(self) -> None:
        # The connection used to be cached before the schema existed, so one
        # failure (another process still creating the file) left this thread a
        # connection that every later call returned as ready.
        handle = runtime_store._handle(self.path)
        original = handle._ensure_schema
        calls = []

        def fail_once(connection):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return original(connection)

        handle._ensure_schema = fail_once  # type: ignore[method-assign]
        self.addCleanup(setattr, handle, "_ensure_schema", original)
        with self.assertRaises(OSError):
            runtime_store.read_document(self.path)

        runtime_store.update_document(self.path, lambda payload: payload.update({"ok": True}))
        self.assertEqual({"ok": True}, runtime_store.read_document(self.path))

    def test_a_legacy_log_that_could_not_be_read_is_adopted_on_a_later_call(self) -> None:
        # A lock or a permission is not "there is no legacy log".  The import
        # used to be recorded as done either way, which left the history on
        # disk and never read it again.
        log = Path(self.directory.name) / "recent-requests.jsonl"
        log.write_text(json.dumps({"request_id": "old"}) + "\n", encoding="utf-8")
        real_open = open

        def locked(file, mode="r", *args, **kwargs):
            if os.fspath(file) == str(log) and "b" in mode:
                raise PermissionError("locked")
            return real_open(file, mode, *args, **kwargs)

        with mock.patch("builtins.open", locked):
            runtime_store.append_record(log, json.dumps({"request_id": "new-1"}))
        runtime_store.append_record(log, json.dumps({"request_id": "new-2"}))

        bodies = [json.loads(body)["request_id"] for body in runtime_store.read_records(log, limit=10)]
        self.assertEqual({"old", "new-1", "new-2"}, set(bodies))
        self.assertEqual(3, len(bodies), "the adopted history was imported more than once")

    def test_a_log_with_no_legacy_file_is_marked_imported(self) -> None:
        log = Path(self.directory.name) / "recent-requests.jsonl"
        runtime_store.append_record(log, json.dumps({"request_id": "first"}))
        # A file that appears afterwards is not a history this log ever had.
        log.write_text(json.dumps({"request_id": "stray"}) + "\n", encoding="utf-8")
        runtime_store.append_record(log, json.dumps({"request_id": "second"}))
        bodies = [json.loads(body)["request_id"] for body in runtime_store.read_records(log, limit=10)]
        self.assertEqual(["first", "second"], bodies)

    def test_a_store_that_cannot_be_reached_is_an_oserror(self) -> None:
        blocker = Path(self.directory.name) / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        unreachable = blocker / "deployment-cooldowns.json"
        with self.assertRaises(OSError):
            runtime_store.read_document(unreachable)
        with self.assertRaises(OSError):
            runtime_store.update_document(unreachable, lambda payload: None)


if __name__ == "__main__":
    unittest.main()
