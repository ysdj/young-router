"""Count the expensive derivations one Core operation actually performs.

Every latency defect this project has found by hand was the same shape: one
user action paid for the same costly derivation twice, three times, or several
hundred times.  A snapshot reparsed the provider document; a reload parsed it
six times; a provider Apply built its projection three times, twice for an
answer nobody read; a catalog build re-read and re-hashed the same 33 KB file
five hundred times.

A wall-clock assertion cannot catch that class reliably.  On this machine the
same commit measured between 154 ms and 657 ms purely from load average, so a
timing test fails for reasons that have nothing to do with the regression, and
passes while a second parse hides behind a slow disk.  A *count* is the stable
question: how many times did this operation parse that document, project that
domain, probe that socket?  It is the same answer on an idle machine and a
loaded one, and when it changes it names the seam that regressed.

The instrument wraps the seams, not the call sites, so it sees any new caller.
Each seam is recorded with a key naming its input, which is what turns "this ran
four times" into "these bytes were parsed four times": a seam allowed to run
once per input is caught the moment one input is derived twice.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import hashlib
import threading
from typing import Any, Callable, Iterator, Mapping
from unittest import mock

#: Seam identifiers.  A seam is one place where a costly derivation happens, and
#: counting it answers "how much work did this operation actually do".
PARSE_YAML = "yaml.parse"
READ_PROVIDER_DOCUMENT = "provider.read"
CORE_SNAPSHOT = "core.snapshot"
PROJECT_DOMAINS = "core.project_domains"
ASSEMBLE_SNAPSHOT = "core.assemble_snapshot"
EMIT_EVENT = "core.emit"
PROVIDER_SNAPSHOT = "providers.snapshot"
PROVIDER_RELOAD = "providers.reload"
PROVIDER_SAVE = "providers.save"
CATALOG_ROUTE_INDEX = "catalog.route_index"
CATALOG_IMAGE_LOOKUP = "catalog.image_lookup"
HEALTH_PROBE = "service.health_probe"
CODEX_LOAD_EDITOR = "codex.load_editor"

#: The text of the one document a settings operation repeatedly derives from.
#: Keying by content is what makes "parsed twice" mean "these same bytes were
#: parsed twice" rather than "two documents were parsed".
PROVIDER_DOCUMENT_KEY = "provider-document"


def _document_key(text: object) -> str:
    if not isinstance(text, str):
        return PROVIDER_DOCUMENT_KEY
    return f"doc:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}"


#: For a seam that answers from its own memo, this answers the state that memo
#: is keyed on.  A call that leaves it unchanged derived nothing, so it is not
#: counted: the instrument measures work performed, never calls made.
_MEMO_STATE: Mapping[str, Callable[[Any], Any]] = {
    # The route index is keyed on the document's identity.
    "_route_index": lambda registry: registry._route_index_key,
}


class WorkLedger:
    """Record every wrapped seam call for the duration of one measurement.

    ``as`` a context manager it installs the wrappers and removes them again, so
    a test never leaks instrumentation into another.  ``counts`` answers per
    seam, and ``first_offender`` answers the one sentence a failing test needs:
    which seam ran more times on one input than the budget allows.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counts: Counter[tuple[str, str | None]] = Counter()
        self._patches: list[Any] = []

    # -- recording ---------------------------------------------------------

    def record(self, seam: str, key: str | None = None) -> None:
        with self._lock:
            self.counts[(seam, key)] += 1

    def reset(self) -> None:
        with self._lock:
            self.counts.clear()

    # -- reading -----------------------------------------------------------

    def total(self, seam: str) -> int:
        """Every call to one seam, whatever its input."""

        with self._lock:
            return sum(value for (name, _key), value in self.counts.items() if name == seam)

    def per_input(self, seam: str) -> dict[str | None, int]:
        with self._lock:
            return {key: value for (name, key), value in self.counts.items() if name == seam}

    def worst_input(self, seam: str) -> int:
        """The most times one single input was derived through this seam."""

        values = self.per_input(seam)
        return max(values.values()) if values else 0

    def report(self) -> str:
        with self._lock:
            rows = sorted(self.counts.items(), key=lambda item: (-item[1], item[0][0]))
        if not rows:
            return "  (no instrumented work ran)"
        return "\n".join(
            f"  {count:5d}x  {seam}{'' if key is None else f'  [{key}]'}"
            for (seam, key), count in rows
        )

    # -- wrapping ----------------------------------------------------------

    def wrap(
        self,
        seam: str,
        target: Any,
        attribute: str,
        *,
        key: Callable[..., str | None] | None = None,
        worked: bool = False,
    ) -> None:
        """Count ``target.attribute`` and call through unchanged.

        ``worked`` marks a seam that answers from its own memo: a hit is not
        work, so only the calls that actually derived something are counted.
        Without that distinction a correctly cached reader would look like it
        ran once per lookup, which is the number this instrument exists to
        tell apart from real repeated work.
        """

        original = getattr(target, attribute)
        memo = _MEMO_STATE.get(attribute) if worked else None

        def counted(*args: Any, **kwargs: Any) -> Any:
            before = memo(target) if memo is not None else None
            outcome = original(*args, **kwargs)
            if memo is None or memo(target) != before:
                self.record(seam, key(*args, **kwargs) if key else None)
            return outcome

        patch = mock.patch.object(target, attribute, counted)
        patch.start()
        self._patches.append(patch)

    def __enter__(self) -> "WorkLedger":
        return self

    def __exit__(self, *_exc: object) -> None:
        for patch in reversed(self._patches):
            patch.stop()
        self._patches.clear()


@contextmanager
def work_ledger() -> Iterator[WorkLedger]:
    """Instrument one Core's seams, then remove every wrapper again."""

    from young_router.config import schema as config_schema
    from young_router.core import codex_config
    from young_router.core.domains.providers_models import ProvidersModelsDomain
    from young_router.core.model_contexts import ModelContextRegistry
    from young_router.core.operations import CoreServiceController
    from young_router.core.service import CoreStore

    ledger = WorkLedger()
    try:
        # One seam per derivation, not per call site: a new caller is counted
        # without touching this file.  ``yaml.load`` is the lowest level every
        # parse goes through -- ``safe_load`` is a thin call to it -- so wrapping
        # it counts each parse exactly once however a caller spells the call.
        ledger.wrap(PARSE_YAML, config_schema.yaml, "load", key=lambda text, *a, **k: _document_key(text))
        ledger.wrap(
            READ_PROVIDER_DOCUMENT,
            codex_config,
            "_read_yaml_text",
            key=lambda path: str(path),
        )
        ledger.wrap(CORE_SNAPSHOT, CoreStore, "snapshot")
        ledger.wrap(PROJECT_DOMAINS, CoreStore, "_project_domains")
        ledger.wrap(ASSEMBLE_SNAPSHOT, CoreStore, "_assemble_snapshot")
        ledger.wrap(EMIT_EVENT, CoreStore, "_emit")
        ledger.wrap(PROVIDER_SNAPSHOT, ProvidersModelsDomain, "snapshot")
        ledger.wrap(PROVIDER_RELOAD, ProvidersModelsDomain, "reload")
        ledger.wrap(CATALOG_ROUTE_INDEX, ModelContextRegistry, "_route_index", worked=True)
        ledger.wrap(CATALOG_IMAGE_LOOKUP, ModelContextRegistry, "image_metadata_for_model_id")
        ledger.wrap(HEALTH_PROBE, CoreServiceController, "_health")
        ledger.wrap(CODEX_LOAD_EDITOR, codex_config, "load_editor")
        yield ledger
    finally:
        for patch in reversed(ledger._patches):
            patch.stop()
        ledger._patches.clear()


#: What each operation may do, per seam.  ``None`` means "not budgeted": the
#: operation is not the one this seam's cost belongs to.
#:
#: A row states the work one operation is allowed to perform, never how long it
#: took.  The numbers are the minimum a correct implementation needs, which is
#: why they are so small: a snapshot projects nothing (its projection is
#: memoized on the revision), a commit projects the configuration once, and one
#: document is parsed at most once per operation that reads it at all.
#:
#: ``per_input`` is the rule that catches double work: the same bytes, the same
#: file, or the same record must not be derived twice inside one operation even
#: when the seam's total stays under a larger number.
OPERATION_BUDGETS: Mapping[str, Mapping[str, int]] = {
    # A window's own refresh is the most frequent call in the app.  Its whole
    # cost is the memoized projection plus the live service row, so it derives
    # no domain state and reads no document.  This row is the one that fails
    # the moment a projection stops being memoized.
    "snapshot": {
        PARSE_YAML: 0,
        READ_PROVIDER_DOCUMENT: 0,
        PROJECT_DOMAINS: 0,
        PROVIDER_SNAPSHOT: 0,
        CATALOG_ROUTE_INDEX: 0,
    },
    # Staging one edit republishes the staged draft for the windows, which is
    # one projection and the provider domain's own snapshot in it.
    "dispatch": {
        PARSE_YAML: 1,
        READ_PROVIDER_DOCUMENT: 1,
        PROJECT_DOMAINS: 1,
        PROVIDER_SNAPSHOT: 2,
    },
    # Committing that edit projects the configuration once for the event, and
    # re-reads the document it just wrote exactly once.
    "apply": {
        PARSE_YAML: 1,
        READ_PROVIDER_DOCUMENT: 1,
        PROJECT_DOMAINS: 1,
        # Exactly one: the projection inside the event every listening window
        # receives.  A second one inside the domain's own Apply is a projection
        # built for an answer nothing reads, which is the defect this pins.
        PROVIDER_SNAPSHOT: 1,
    },
    # An explicit reload re-reads the document once and projects it once.
    "reload": {
        PARSE_YAML: 1,
        READ_PROVIDER_DOCUMENT: 1,
        PROJECT_DOMAINS: 1,
        PROVIDER_SNAPSHOT: 2,
    },
    # The Codex domain's own reload reads the editor document, and the runtime
    # domain reads the master key out of the same bytes rather than reparsing.
    "reload_codex": {
        PARSE_YAML: 1,
        READ_PROVIDER_DOCUMENT: 2,
        PROJECT_DOMAINS: 1,
        PROVIDER_SNAPSHOT: 2,
    },
}

#: The budget from one side: an operation that may only parse once must not
#: have parsed the same bytes twice.  Stated separately because a seam's total
#: can stay under a multi-input budget while one document is derived repeatedly.
PER_INPUT_LIMITS: Mapping[str, int] = {
    PARSE_YAML: 1,
    READ_PROVIDER_DOCUMENT: 2,
}
