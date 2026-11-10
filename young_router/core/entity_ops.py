"""Entity-level changes between two projections of one domain.

A state event used to be the whole projection, so every edit replayed ten
domain snapshots to every open window.  Most edits touch one row: a provider
was added, a route was disabled, a relay account was removed.  An ``EntityOp``
is that row named directly, and a domain that can name its rows answers a
patch instead of the whole projection.

A domain opts in by implementing ``diff(previous, current)``.  A domain that
does not is published as one ``replace`` op, which is the whole projection:
correct, just heavier.  That fallback is what keeps this from becoming a
requirement every adapter has to grow before the smaller event can ship.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: How many entity ops one patch may carry before a full projection is the
#: cheaper answer.  A patch is only worth its own framing while it is smaller
#: than the snapshot it replaces; several hundred ops means a bulk import or a
#: reload touched nearly everything.
MAX_PATCH_OPS = 256

_OP_KINDS = frozenset({"upsert", "remove", "replace"})


@dataclass(frozen=True)
class EntityOp:
    """One entity-level change inside a domain projection.

    ``remove`` names only an ``id``: the entity is gone, so there is no value
    to carry.  ``upsert`` replaces the row at that id (or inserts it).
    ``replace`` is the whole-domain fallback and carries the projection itself.
    """

    domain: str
    kind: str
    id: str | None = None
    value: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.domain, str) or not self.domain:
            raise ValueError("an entity op must name its domain")
        if self.kind not in _OP_KINDS:
            raise ValueError("an entity op must be an upsert, a remove, or a replace")
        if self.kind == "remove" and (not isinstance(self.id, str) or not self.id):
            raise ValueError("a remove op must name the entity it removes")

    def to_mapping(self) -> dict[str, Any]:
        """The wire shape of this op, matching the event contract exactly."""

        payload: dict[str, Any] = {"domain": self.domain, "kind": self.kind}
        if self.id is not None:
            payload["id"] = self.id
        if self.kind != "remove":
            payload["value"] = self.value
        return payload


def domain_diff(adapter: object, previous: object, current: object) -> list[EntityOp]:
    """Return the entity changes ``adapter`` went through between projections.

    ``previous`` is the projection the last published event carried, so a
    window that applied that event holds it.  A domain that cannot describe the
    difference answers with one ``replace`` op; a domain that can is trusted to
    name every row it changed, which is why the caller treats an empty answer
    for a projection that did move as "read the whole state again" rather than
    as "nothing happened".
    """

    domain = str(getattr(adapter, "name", "") or "")
    if previous is None:
        return [EntityOp(domain=domain, kind="replace", value=current)]
    diff = getattr(adapter, "diff", None)
    if not callable(diff):
        return [EntityOp(domain=domain, kind="replace", value=current)]
    result = diff(previous, current)
    if result is None:
        return []
    ops: list[EntityOp] = []
    for op in result:
        if not isinstance(op, EntityOp):
            raise TypeError("a domain diff must return EntityOp values")
        if op.domain != domain:
            raise ValueError("a domain diff named another domain")
        ops.append(op)
    return ops


def index_entities(items: object, *identities: str) -> dict[str, Mapping[str, Any]]:
    """Index projected rows by the first identity field each one states.

    A projection's own fields decide its id: the first non-empty one of
    ``identities``.  Rows that state none are skipped rather than guessed at,
    because a guessed id is a patch that renames a row the store never changed.
    """

    index: dict[str, Mapping[str, Any]] = {}
    if not isinstance(items, (list, tuple)):
        return index
    for item in items:
        if not isinstance(item, Mapping):
            continue
        key = entity_key(item, *identities)
        if key:
            index[key] = item
    return index


def entity_key(item: Mapping[str, Any], *identities: str) -> str:
    """The first non-empty identity field of one projected row."""

    for identity in identities:
        value = item.get(identity)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
