"""A sync both ends moved: merge the documents, or leave the choice to a person.

``smart_sync_plan`` answers from snapshots: a side that moved alone is the side
to take, and two sides that both moved is a conflict.  That second answer is
right about the *direction* -- neither copy is a version of the other -- but it
is too pessimistic about the *content*.  Two machines editing one 1,100-line
document usually touch different rows of it: a route added here, a station's
key re-bound there, a model disabled on the other machine.  Stopping for a
choice on every such pair is what makes "both ends changed" mean "sync is
broken until somebody opens the app", and the choice the reader is offered
throws away one of the two edits.

So the same three snapshots are asked a second question, and it is asked of the
bytes: can these two edits be combined?  A three-way merge answers it, one
document at a time, with the last agreed content as the ancestor:

* A file only one side moved is taken from that side -- and a file both sides
  moved to the same content is not a question, exactly as the plan says.
* A configuration document is merged as a *document*, never as text.  Rows are
  matched by their identity (``model_info.id`` for a route, ``name`` for a
  provider's key), mapping entries by their key, ordered lists by the
  ``diff3`` rule.  That is what lets a re-formatted but unchanged row merge
  (the file is rewritten by whichever machine saved it, and a route's
  ``route_key`` is a derived line), and what lets both machines append a route
  to the same list without colliding.
* The relay store is merged by the rule it already owns
  (:func:`young_router.webdav.core._merge_relay_accounts`): stations, sessions
  and removals from both sides, with a tombstone suppressing the row it names.
* Anything that cannot be combined -- both sides changing one field of one row,
  an edit against a removal, a document that is not a mapping, a merge that
  would not validate -- ends the whole merge.  A half-merged document is worse
  than the choice this replaces, so a single collision leaves the sync exactly
  where it was: stopped, with the pane offering both sides.

Nothing here reads a file or touches the server.  The caller holds the three
sides of a stopped sync (``SyncSnapshot.files``: the baseline's saved content,
this machine's files, and the bundle it just downloaded), and gets back the
merged documents or ``None``.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from .core import (
    RELAY_ACCOUNTS_FILE,
    SyncError,
    _expected_bundle_files,
    _merge_relay_accounts,
    content_digest,
    validate_config_bytes,
)

try:  # pragma: no cover - the loader is the same one the config schema takes
    import yaml
except Exception:  # pragma: no cover
    yaml = None

_YAML_LOADER = getattr(yaml, "CSafeLoader", getattr(yaml, "SafeLoader", None))
_YAML_DUMPER = getattr(yaml, "CSafeDumper", getattr(yaml, "SafeDumper", None))


class _Missing:
    """A key or row that one side of the comparison does not have.

    ``None`` cannot play this part: a YAML document may hold a null value, and
    "this row was removed here" is a different fact from "this row is null".
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<missing>"


_MISSING = _Missing()


class _Conflict(Exception):
    """Two sides changed one path of one document to different content."""

    def __init__(self, path: Sequence[str]) -> None:
        self.path = tuple(path)
        super().__init__(" / ".join(self.path) or "document")


def merge_sync_files(
    base: Mapping[str, bytes | None],
    ours: Mapping[str, bytes | None],
    theirs: Mapping[str, bytes | None],
) -> dict[str, bytes] | None:
    """The merged content of every synced file, or ``None`` when any collides.

    ``None`` is the whole answer, not a per-file one: the caller either has a
    document every side can be replaced with, or it has the conflict it already
    knew about.  An absent file (``None``) is content too -- a file one side
    removed and the other left alone stays removed, and a file one side removed
    while the other edited it is a question no merge may answer.
    """

    merged: dict[str, bytes | None] = {}
    for name in sorted(set(base) | set(ours) | set(theirs)):
        base_value = base.get(name)
        our_value = ours.get(name)
        their_value = theirs.get(name)
        if _same_content(name, our_value, their_value):
            merged[name] = our_value
            continue
        if _same_content(name, our_value, base_value):
            merged[name] = their_value
            continue
        if _same_content(name, their_value, base_value):
            merged[name] = our_value
            continue
        combined = _merge_one(name, base_value, our_value, their_value)
        if combined is None:
            return None
        merged[name] = combined
    return {name: value for name, value in merged.items() if value is not None}


def merge_document(
    name: str,
    base: str,
    ours: str,
    theirs: str,
) -> str | None:
    """One configuration document merged, or ``None`` when its edits collide."""

    combined = _merge_yaml(name, base.encode("utf-8"), ours.encode("utf-8"), theirs.encode("utf-8"))
    if combined is None:
        return None
    try:
        return combined.decode("utf-8")
    except UnicodeDecodeError:  # pragma: no cover - the dump is UTF-8 by construction
        return None


def _same_content(name: str, left: bytes | None, right: bytes | None) -> bool:
    """Whether two copies of one file say the same thing.

    The compared digest, not the bytes: the relay store carries observations
    its own poller writes, and a file that is not there is the same absence
    only with another absence.
    """

    if left is None or right is None:
        return left is None and right is None
    return content_digest(name, left) == content_digest(name, right)


def _merge_one(
    name: str,
    base: bytes | None,
    ours: bytes | None,
    theirs: bytes | None,
) -> bytes | None:
    if ours is None or theirs is None:
        # One side removed a file the other changed.  Installing the survivor
        # would undo a deletion, deleting it would undo an edit.
        return None
    if name == RELAY_ACCOUNTS_FILE:
        combined = _merge_relay_accounts(ours, theirs)
        return theirs if combined is None else combined
    return _merge_yaml(name, base, ours, theirs)


def _merge_yaml(
    name: str,
    base: bytes | None,
    ours: bytes | None,
    theirs: bytes | None,
) -> bytes | None:
    if yaml is None or _YAML_LOADER is None or _YAML_DUMPER is None:  # pragma: no cover
        return None
    documents: list[Any] = []
    for data in (base, ours, theirs):
        # A file neither side had when the last sync recorded the baseline has
        # an empty ancestor: both copies are additions, and the merge answers
        # for every key they share.
        if data is None:
            documents.append({})
            continue
        parsed = _parse_document(data)
        if parsed is None:
            return None
        documents.append(parsed)
    try:
        merged = _merge_value((), documents[0], documents[1], documents[2])
    except _Conflict:
        return None
    if not isinstance(merged, Mapping):
        return None
    try:
        text = yaml.dump(
            dict(merged),
            Dumper=_YAML_DUMPER,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=1000,
        )
    except Exception:  # pragma: no cover - a tree this dumper refuses is unreadable anyway
        return None
    data = text.encode("utf-8")
    required_key = _expected_bundle_files().get(name, (False, ""))[1]
    try:
        # The same validator the bundle and the editor use.  A merge that
        # produces a document this app would refuse to apply is not a merge:
        # the reader's choice is better than a file that fails to load.
        validate_config_bytes(name, data, required_key)
    except SyncError:
        return None
    return data


def _parse_document(data: bytes) -> Any:
    try:
        parsed = yaml.load(data.decode("utf-8"), Loader=_YAML_LOADER)
    except Exception:
        return None
    return parsed


def _merge_value(path: tuple[str, ...], base: Any, ours: Any, theirs: Any) -> Any:
    """One node of the document, merged; ``_MISSING`` when both sides removed it.

    A node one side did not touch is the other side's answer.  Only content both
    sides changed is merged, and only structure that can be merged node by node
    (mappings by key, rows by identity, lists by the diff3 rule) gets that far.
    """

    if base is _MISSING:
        # The ancestor never had this node, so neither side can have removed
        # it: whatever is here was added since the last agreement.
        if ours is _MISSING:
            return theirs
        if theirs is _MISSING:
            return ours
        if _value_equal(ours, theirs):
            return ours
        if isinstance(ours, Mapping) and isinstance(theirs, Mapping):
            return _merge_mapping(path, base, ours, theirs)
        if isinstance(ours, list) and isinstance(theirs, list):
            return _merge_list(path, base, ours, theirs)
        raise _Conflict(path)
    if ours is _MISSING and theirs is _MISSING:
        return _MISSING
    if ours is _MISSING or theirs is _MISSING:
        survivor = theirs if ours is _MISSING else ours
        if _value_equal(survivor, base):
            # The side that still has it did not change it: the other side's
            # removal is the whole edit.
            return _MISSING
        raise _Conflict(path)
    if _value_equal(ours, theirs):
        return ours
    if _value_equal(ours, base):
        return theirs
    if _value_equal(theirs, base):
        return ours
    if isinstance(ours, Mapping) and isinstance(theirs, Mapping):
        return _merge_mapping(path, base, ours, theirs)
    if isinstance(ours, list) and isinstance(theirs, list):
        return _merge_list(path, base, ours, theirs)
    raise _Conflict(path)


def _merge_mapping(path: tuple[str, ...], base: Any, ours: Mapping, theirs: Mapping) -> dict:
    ancestor = base if isinstance(base, Mapping) else {}
    order = list(ancestor)
    order.extend(key for key in ours if key not in ancestor)
    order.extend(key for key in theirs if key not in ancestor and key not in ours)
    merged: dict = {}
    for key in order:
        value = _merge_value(
            (*path, str(key)),
            ancestor.get(key, _MISSING),
            ours.get(key, _MISSING),
            theirs.get(key, _MISSING),
        )
        if value is not _MISSING:
            merged[key] = value
    return merged


def _merge_list(path: tuple[str, ...], base: Any, ours: list, theirs: list) -> list:
    ancestor = base if isinstance(base, list) else []
    key = _shared_identity(ancestor, ours, theirs)
    if key is not None:
        return _merge_rows(path, key, ancestor, ours, theirs)
    return _merge_sequence(path, ancestor, ours, theirs)


#: The names a row of a synced document carries its identity under, most
#: specific first.  A route is identified by the deployment id the router
#: assigned it (``model_info.id``), not by its model name: two deployments of
#: one model are two rows, and merging them by name would fuse two routes into
#: one.  A provider's key is identified by the name the reader gave it; the
#: station slot catalog uses ``id`` for the same reason.
_ROW_IDENTITY_KEYS = ("id", "name")


def _row_identity(row: Any) -> tuple[str, str] | None:
    if not isinstance(row, Mapping):
        return None
    info = row.get("model_info")
    if isinstance(info, Mapping):
        deployment_id = info.get("id")
        if isinstance(deployment_id, str) and deployment_id:
            return ("model_info.id", deployment_id)
    for key in _ROW_IDENTITY_KEYS:
        value = row.get(key)
        if isinstance(value, str) and value:
            return (key, value)
    return None


def _shared_identity(*rows: Sequence[Any]) -> str | None:
    """The identity field every row of these lists carries, or ``None``.

    All or nothing: rows identified in one list and not in another cannot be
    matched, and a field that repeats within one list does not identify.  A
    list that fails this test is merged as a sequence instead, which is
    stricter -- an insertion at the same place is still merged, an edit of the
    same position is not.
    """

    key: str | None = None
    for values in rows:
        seen: set[str] = set()
        for row in values:
            identity = _row_identity(row)
            if identity is None:
                return None
            name, value = identity
            if key is None:
                key = name
            elif name != key:
                return None
            if value in seen:
                return None
            seen.add(value)
    return key


def _rows_by_identity(rows: Sequence[Any]) -> dict[str, Any]:
    indexed: dict[str, Any] = {}
    for row in rows:
        identity = _row_identity(row)
        if identity is not None:
            indexed[identity[1]] = row
    return indexed


def _merge_rows(
    path: tuple[str, ...],
    key: str,
    base: list,
    ours: list,
    theirs: list,
) -> list:
    """Rows matched by identity: added, removed and edited rows all survive.

    The order is the ancestor's, then the rows each side added in its own
    order.  Two machines appending a route to the end of ``model_list`` are
    therefore two additions, not one collision -- which is the case that made
    this file the reason a sync stopped.
    """

    ancestor = _rows_by_identity(base)
    our_rows = _rows_by_identity(ours)
    their_rows = _rows_by_identity(theirs)
    order = list(ancestor)
    order.extend(name for name in our_rows if name not in ancestor)
    order.extend(name for name in their_rows if name not in ancestor and name not in our_rows)
    merged: list = []
    for name in order:
        row = _merge_value(
            (*path, f"{key}={name}"),
            ancestor.get(name, _MISSING),
            our_rows.get(name, _MISSING),
            their_rows.get(name, _MISSING),
        )
        if row is not _MISSING:
            merged.append(row)
    return merged


def _merge_sequence(path: tuple[str, ...], base: list, ours: list, theirs: list) -> list:
    """An ordered list with no identity: merge the edits each side made.

    A list of plain values (the public model group order, a callback list) has
    no key to match rows by, so positions are compared instead.  A region only
    one side touched is that side's; a region both sides replaced with the same
    values is one edit; a region both sides added to at the same position keeps
    both insertions; a region both sides replaced differently is a conflict.
    """

    our_changes = _changes(ours, base)
    their_changes = _changes(theirs, base)
    merged: list = []
    position = 0
    for start, end in _clusters([*our_changes, *their_changes]):
        if start < position:  # pragma: no cover - clusters are disjoint and sorted
            raise _Conflict(path)
        merged.extend(base[position:start])
        ours_span = _side_span(base, our_changes, start, end)
        theirs_span = _side_span(base, their_changes, start, end)
        ancestor_span = base[start:end]
        if _value_equal(ours_span, theirs_span):
            merged.extend(ours_span)
        elif _value_equal(ours_span, ancestor_span):
            merged.extend(theirs_span)
        elif _value_equal(theirs_span, ancestor_span):
            merged.extend(ours_span)
        elif start == end:
            # Both sides inserted different values at one position: keeping
            # both is the only answer that loses neither edit.
            merged.extend(ours_span)
            merged.extend(theirs_span)
        else:
            raise _Conflict(path)
        position = end
    merged.extend(base[position:])
    return merged


def _changes(side: list, base: list) -> list[tuple[int, int, list]]:
    """What one side did to the base, as (start, end, replacement) spans."""

    matcher = difflib.SequenceMatcher(
        a=[_sequence_key(value) for value in base],
        b=[_sequence_key(value) for value in side],
        autojunk=False,
    )
    changes: list[tuple[int, int, list]] = []
    for tag, start, end, side_start, side_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        changes.append((start, end, list(side[side_start:side_end])))
    return changes


def _clusters(changes: Sequence[tuple[int, int, list]]) -> list[tuple[int, int]]:
    """The base ranges two sides' edits share, as the regions to decide on.

    Overlapping and same-start edits are one region: the second side's answer
    for a range the first side already rewrote cannot be applied beside it.
    Two edits that merely touch (one ends where the other begins) stay
    separate, so deleting a row and rewriting the row after it still merge.
    """

    spans = sorted((start, end) for start, end, _replacement in changes)
    clusters: list[tuple[int, int]] = []
    for start, end in spans:
        if clusters and (start < clusters[-1][1] or start == clusters[-1][0]):
            previous_start, previous_end = clusters[-1]
            clusters[-1] = (previous_start, max(previous_end, end))
        else:
            clusters.append((start, end))
    return clusters


def _side_span(
    base: list,
    changes: Sequence[tuple[int, int, list]],
    start: int,
    end: int,
) -> list:
    """One side's content for the base range a cluster covers."""

    span: list = []
    position = start
    for change_start, change_end, replacement in changes:
        if change_start == change_end:
            inside = start <= change_start < end or (start == end == change_start)
        else:
            inside = change_start >= start and change_end <= end
        if not inside:
            continue
        if change_start > position:
            span.extend(base[position:change_start])
        span.extend(replacement)
        position = max(position, change_end)
    if position < end:
        span.extend(base[position:end])
    return span


def _sequence_key(value: Any) -> str:
    """One list element as something difflib can compare.

    The elements of a merged list are documents, not scalars, so equality is
    the JSON they render to: two rows with the same keys in the same order are
    the same row, and a re-ordered row is a changed one.
    """

    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - parsed YAML is serializable
        return repr(value)


def _value_equal(left: Any, right: Any) -> bool:
    if left is _MISSING or right is _MISSING:
        return left is _MISSING and right is _MISSING
    if isinstance(left, bool) or isinstance(right, bool):
        # YAML's ``true`` and its ``1`` are different content even though
        # Python compares them equal.
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    return left == right
