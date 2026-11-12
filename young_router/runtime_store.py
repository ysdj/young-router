"""The one store for state the proxy's workers share, in SQLite.

Six documents are written by the sixteen forkserver workers and read by them
and by Core: the concurrency leases, the deployment cooldowns, the route-recovery
records, the session->deployment affinity, the reasoning-replay memory and the
web-search references.  Every write went through one primitive -- open the
sidecar lock file, ``flock`` it, read and parse the whole document, run the
caller's callback, serialise it twice to see whether it moved, then replace it
atomically with an fsync of the file and of its directory.  On one installation
a claim-and-release pair measured 0.5 ms alone and 7.9 ms with sixteen workers
contending for the same lock, and every attempt a request makes pays for one.

The same primitive in SQLite -- one write transaction, no fsync under WAL's
``synchronous=NORMAL`` -- measured 0.02 ms alone and 0.019 ms with sixteen
workers.  It also gives the platform the app runs on second: on Windows the
``flock`` branch is absent, so sixteen workers shared these documents with no
exclusion at all.

What this module is not: a second source of truth for anything else.  A
document here is the same document the JSON file held, under the same name, and
the file is imported once when the store first sees it -- so a downgrade still
finds the state it left, and a test that wrote a fixture by hand still gets it
read back.

The database lives beside the documents (``runtime-state.db`` in their
directory) so that every path a host or a test redirects through an environment
variable keeps its own state, exactly as the file did.  Connections are per
process and per thread, opened lazily: the proxy's workers fork, and a
connection must never cross that boundary.

One deliberate cost stays: a document is still parsed and serialised whole,
because the callbacks that maintain it are written against the document.  The
lease document is small (a few kilobytes) and SQLite removes the fsyncs, the
lock file and the contention; a document that grows large enough for its own
parse to matter gets a table of its own rather than a bigger callback.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable, Mapping

DATABASE_NAME = "runtime-state.db"
BUSY_TIMEOUT_SECONDS = 5.0
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS documents ("
    " name TEXT PRIMARY KEY,"
    " body TEXT NOT NULL,"
    " revision INTEGER NOT NULL,"
    " updated_at REAL NOT NULL"
    ") WITHOUT ROWID"
)

_handles: dict[str, "_Handle"] = {}
_handles_lock = threading.Lock()


class _Handle:
    """One database file: its schema state, and a connection per thread."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._schema_lock = threading.Lock()
        self._schema_ready = False
        self._local = threading.local()

    def connection(self) -> sqlite3.Connection:
        try:
            return self._connection()
        except sqlite3.Error as exc:
            # The callers of this store treat a store they cannot reach the way
            # they treated a file they could not write: ``OSError`` means "this
            # host has no shared state", and every one of them already says so.
            raise OSError(str(exc)) from None

    def _connection(self) -> sqlite3.Connection:
        pid = os.getpid()
        connection = getattr(self._local, "connection", None)
        if connection is not None and getattr(self._local, "pid", None) == pid:
            return connection
        if connection is not None:
            # A forked child inherits the handle but must not use it; the
            # parent's connection is still open there.
            try:
                connection.close()
            except Exception:
                pass
        os.makedirs(os.path.dirname(self.path), exist_ok=True, mode=0o700)
        connection = sqlite3.connect(
            self.path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None, check_same_thread=False
        )
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=%d" % int(BUSY_TIMEOUT_SECONDS * 1000))
        self._local.connection = connection
        self._local.pid = pid
        self._ensure_schema(connection)
        return connection

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            connection.execute(_SCHEMA)
            self._schema_ready = True


def database_path(path: Path | str) -> str:
    """Where the store for a document lives: beside it, one per directory.

    Kept as ``os.path`` strings rather than ``Path`` objects on purpose: the
    ``pathlib`` flavour follows ``os.name`` when a ``Path`` is constructed, and
    a test that states this host is Windows would then build a ``WindowsPath``
    on a POSIX one.
    """

    document = os.path.abspath(os.path.expanduser(os.fspath(path)))
    return os.path.join(os.path.dirname(document), DATABASE_NAME)


def _handle(path: Path | str) -> _Handle:
    key = database_path(path)
    handle = _handles.get(key)
    if handle is not None:
        return handle
    with _handles_lock:
        handle = _handles.get(key)
        if handle is None:
            handle = _Handle(key)
            _handles[key] = handle
        return handle


def _name(path: Path | str) -> str:
    return os.path.basename(os.fspath(path))


def _legacy_payload(path: Path | str) -> dict[str, Any] | None:
    """The JSON file this document lived in before the store, if it is there."""

    target = os.path.expanduser(os.fspath(path))
    try:
        details = os.lstat(target)
    except OSError:
        return None
    import stat as _stat

    if _stat.S_ISLNK(details.st_mode) or not _stat.S_ISREG(details.st_mode):
        return None
    try:
        with open(target, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _encoded(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"


def _row(connection: sqlite3.Connection, name: str) -> tuple[str, int] | None:
    row = connection.execute("SELECT body, revision FROM documents WHERE name=?", (name,)).fetchone()
    return (row[0], int(row[1])) if row is not None else None


def _import(connection: sqlite3.Connection, path: Path | str, name: str) -> tuple[str, int] | None:
    """Adopt a document the file-based build left behind, once."""

    payload = _legacy_payload(path)
    if payload is None:
        return None
    connection.execute(
        "INSERT OR IGNORE INTO documents(name, body, revision, updated_at) VALUES(?,?,?,?)",
        (name, _encoded(payload), 1, time.time()),
    )
    return _row(connection, name)


def read_document(path: Path | str, *, default: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The document as it stands, from a snapshot read (no write lock)."""

    name = _name(path)
    connection = _handle(path).connection()
    row = _row(connection, name)
    if row is None:
        # Importing is a write; take the write lock only when it is needed.
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = _row(connection, name) or _import(connection, path, name)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    if row is None:
        return dict(default or {})
    try:
        payload = json.loads(row[0])
    except ValueError:
        return dict(default or {})
    return payload if isinstance(payload, dict) else dict(default or {})


def read_text(path: Path | str) -> str | None:
    """The document's canonical text, or ``None`` when it does not exist."""

    name = _name(path)
    if not has_document(path):
        return None
    connection = _handle(path).connection()
    row = _row(connection, name)
    return row[0] if row is not None else None


def document_version(path: Path | str) -> int | None:
    """A number that moves on every committed change (a cursor, not a clock)."""

    connection = _handle(path).connection()
    row = _row(connection, _name(path))
    return row[1] if row is not None else None


def has_document(path: Path | str) -> bool:
    connection = _handle(path).connection()
    row = _row(connection, _name(path))
    if row is not None:
        return True
    return _legacy_payload(path) is not None


def update_document(
    path: Path | str,
    callback: Callable[[dict[str, Any]], Any],
    *,
    read_only: bool = False,
) -> Any:
    """Run ``callback`` over the document and commit what it changed.

    ``read_only`` answers from a snapshot and throws the callback's writes
    away.  A caller that only counts or filters uses it: under the file
    primitive even a read took the exclusive lock and parsed the whole
    document, which made every read a writer as far as sixteen workers were
    concerned.
    """

    name = _name(path)
    connection = _handle(path).connection()
    if read_only:
        row = _row(connection, name)
        if row is None:
            payload = _legacy_payload(path)
            if payload is None:
                return callback({})
            return callback(dict(payload))
        try:
            payload = json.loads(row[0])
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return callback(payload)

    connection.execute("BEGIN IMMEDIATE")
    try:
        row = _row(connection, name)
        if row is None:
            row = _import(connection, path, name)
        if row is None:
            payload: dict[str, Any] = {}
            before = None
        else:
            try:
                loaded = json.loads(row[0])
            except ValueError:
                loaded = {}
            payload = loaded if isinstance(loaded, dict) else {}
            before = _canonical(payload)
        result = callback(payload)
        after = _canonical(payload)
        if before is None or after != before:
            connection.execute(
                "INSERT INTO documents(name, body, revision, updated_at) VALUES(?,?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET"
                "  body=excluded.body,"
                "  revision=documents.revision+1,"
                "  updated_at=excluded.updated_at",
                (name, _encoded(payload), (row[1] if row is not None else 0) + 1, time.time()),
            )
        connection.execute("COMMIT")
        return result
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def delete_document(path: Path | str) -> bool:
    """Remove the document from the store, and the file it came from, if any."""

    connection = _handle(path).connection()
    connection.execute("BEGIN IMMEDIATE")
    try:
        removed = connection.execute("DELETE FROM documents WHERE name=?", (_name(path),)).rowcount
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    try:
        os.unlink(os.path.expanduser(os.fspath(path)))
        removed = True
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return bool(removed)


__all__ = [
    "DATABASE_NAME",
    "database_path",
    "delete_document",
    "document_version",
    "has_document",
    "read_document",
    "read_text",
    "update_document",
]
