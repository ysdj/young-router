"""One edit must not pay twice for the same work.

The IPC budget in ``test_core_ipc_latency`` says how long an operation may take.
This module says *why* it takes that long, which is the part a timing assertion
cannot tell apart from a busy machine: every defect in this class is one costly
derivation repeated inside a single operation.  Counting is stable under load,
and when the count moves it names the seam that regressed -- the provider
document parsed six times in one reload, a projection built three times where
one answer was read, a catalog walked per lookup.

``ipc_work_ledger`` holds the instrument and the budgets; this module drives it
over the operations the settings window actually performs.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ipc_work_ledger import (
    CATALOG_ROUTE_INDEX,
    CODEX_LOAD_EDITOR,
    EMIT_EVENT,
    OPERATION_BUDGETS,
    PARSE_YAML,
    PER_INPUT_LIMITS,
    PROJECT_DOMAINS,
    PROVIDER_RELOAD,
    PROVIDER_SAVE,
    PROVIDER_SNAPSHOT,
    READ_PROVIDER_DOCUMENT,
    WorkLedger,
    work_ledger,
)

from young_router.core.domains.codex import CodexSettingsDomain
from young_router.core.domains.providers_models import ProvidersModelsDomain
from young_router.core.domains.runtime import RuntimeSettingsDomain
from young_router.core.service import CoreStore


def _provider_document() -> str:
    """A document shaped like an installation: several providers and routes."""

    lines = ["providers:"]
    for provider in range(14):
        name = f"provider{provider}"
        lines.extend(
            [
                f"  {name}: &{name}_provider",
                f'    api_base: &{name}_base "https://relay{provider}.example.test/v1"',
                "    api_keys:",
            ]
        )
        for key in range(3):
            lines.append(
                f"        - name: key{key}\n"
                f"          value: &{name}_key{key} \"sk-replace-{provider}-{key}\""
            )
    lines.append("model_list:")
    for provider in range(14):
        for route in range(6):
            lines.extend(
                [
                    f"  - model_name: public-{provider}-{route}",
                    "    litellm_params:",
                    f"      model: openai/upstream-{provider}-{route}",
                    f'      api_base: "https://relay{provider}.example.test/v1"',
                    f'      api_key: "sk-replace-{provider}-0"',
                    "      order: 0",
                    "    model_info:",
                    f"      id: {provider:02x}{route:02x}abcd",
                    f"      provider: provider{provider}",
                    "      upstream_url_surface: openai/responses",
                ]
            )
    lines.extend(["general_settings:", "  master_key: sk-replace-master", ""])
    return "\n".join(lines)


class WorkLedgerTests(unittest.TestCase):
    def test_the_ledger_reports_what_ran_and_forgets_it_again(self) -> None:
        ledger = WorkLedger()
        ledger.record("seam", "a")
        ledger.record("seam", "a")
        ledger.record("seam", "b")

        self.assertEqual(3, ledger.total("seam"))
        self.assertEqual(2, ledger.worst_input("seam"))

        ledger.reset()

        self.assertEqual(0, ledger.total("seam"))
        self.assertEqual({}, ledger.per_input("seam"))

    def test_the_wrappers_are_removed_when_the_ledger_closes(self) -> None:
        original = CoreStore.snapshot
        with work_ledger():
            self.assertIsNot(CoreStore.snapshot, original)
        self.assertIs(CoreStore.snapshot, original)


class OperationWorkTests(unittest.TestCase):
    """Drive one Core the way the settings shell drives it."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        root = Path(self._directory.name)
        self.config_path = root / ".litellm-runtime" / "config.yaml"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(_provider_document(), encoding="utf-8")
        self.settings_path = root / "runtime-settings.env"
        self.settings_path.write_text("", encoding="utf-8")
        self.codex_home = root / "codex"
        self.codex_home.mkdir()
        self.root = root
        # The listing read is a live proxy's answer; these tests are about this
        # process's own work, so it answers the same way every time.
        models = mock.patch(
            "young_router.core.codex_config.local_exposed_models",
            return_value=(['public-0-0'], True),
        )
        models.start()
        self.addCleanup(models.stop)
        # ``local_proxy_endpoint`` resolves this installation from the process
        # environment, so pin it here and make this fixture the only document
        # any reader can find.
        environment = mock.patch.dict(
            os.environ,
            {
                "YOUNG_ROUTER_HOME": str(root),
                "LITELLM_RUNTIME_ROOT": str(root),
                "YOUNG_ROUTER_RUNTIME_SETTINGS_FILE": str(self.settings_path),
                "CODEX_HOME": str(self.codex_home),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("LITELLM_CONFIG_FILE", None)
        self.core = CoreStore(
            domains=[
                ProvidersModelsDomain(self.config_path),
                CodexSettingsDomain(self.config_path, codex_home=self.codex_home),
                RuntimeSettingsDomain(self.settings_path),
            ],
        )
        # One window is listening, so every event the Core publishes is also
        # built and serialized exactly as a live window would cost it.
        self.core.subscribe(lambda _event: None)
        self.core.snapshot()

    def _measure(self, operation: str, action) -> tuple[WorkLedger, object]:
        with work_ledger() as ledger:
            ledger.reset()
            result = action()
        self._assert_within(operation, ledger)
        return ledger, result

    def _assert_within(self, operation: str, ledger: WorkLedger) -> None:
        budget = OPERATION_BUDGETS[operation]
        detail = f"\nledger for {operation}:\n{ledger.report()}"
        for seam, allowed in budget.items():
            self.assertLessEqual(
                ledger.total(seam),
                allowed,
                f"{operation} derived {seam} more times than it may:"
                f" {ledger.total(seam)} > {allowed}{detail}",
            )
        for seam, allowed in PER_INPUT_LIMITS.items():
            worst = ledger.worst_input(seam)
            self.assertLessEqual(
                worst,
                allowed,
                f"{operation} derived one input through {seam} {worst} times"
                f" (limit {allowed}){detail}",
            )

    def test_a_window_refresh_derives_no_domain_state(self) -> None:
        """The most-called method must be the cheapest one."""

        ledger, _ = self._measure("snapshot", self.core.snapshot)
        self.assertEqual(0, ledger.total(PROJECT_DOMAINS))
        self.assertEqual(0, ledger.total(PARSE_YAML))

    def test_a_commit_with_no_window_open_projects_nothing_for_the_event(self) -> None:
        """A background launch has nobody to publish to.

        The event is built from a full projection, so building one for a fan-out
        with no listeners is work every caller discards.  The commit still
        answers its own caller; what it must not do is project on behalf of a
        window that is not there.
        """

        staged = self.core.dispatch(
            {
                "domain": "providers_models",
                "type": "provider.patch",
                "payload": {"provider_id": "provider0", "changes": {"enabled": True}},
            }
        )
        # Drop every listener: exactly what a closed settings window leaves.
        self.core._subscribers.clear()

        with work_ledger() as ledger:
            ledger.reset()
            self.core.apply("providers_models", revision=staged["revision"])

        # ``_emit`` is still on the commit's path; what must not happen is the
        # projection it exists to publish.
        self.assertEqual(
            0,
            ledger.total(PROJECT_DOMAINS),
            f"a commit projected the configuration with no window listening"
            f"\n{ledger.report()}",
        )

    def test_staging_one_edit_projects_the_draft_once(self) -> None:
        self._measure(
            "dispatch",
            lambda: self.core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.patch",
                    "payload": {"provider_id": "provider0", "changes": {"enabled": True}},
                }
            ),
        )

    def test_committing_one_edit_parses_the_document_once(self) -> None:
        staged = self.core.dispatch(
            {
                "domain": "providers_models",
                "type": "provider.patch",
                "payload": {"provider_id": "provider0", "changes": {"enabled": True}},
            }
        )
        ledger, _ = self._measure(
            "apply",
            lambda: self.core.apply("providers_models", revision=staged["revision"]),
        )
        self.assertLessEqual(ledger.total(PARSE_YAML), 2)

    def test_an_explicit_reload_projects_once(self) -> None:
        ledger, _ = self._measure("reload", lambda: self.core.reload("providers_models"))

        # The projection is built for the answer and again for the event only
        # when a window is listening; anything beyond one per reader is waste.
        self.assertLessEqual(ledger.total(PROJECT_DOMAINS), 1)
        self.assertLessEqual(ledger.total(EMIT_EVENT), 1)

    def test_a_codex_reload_reads_the_provider_document_once(self) -> None:
        """The editor's runtime context and the master key share one read."""

        ledger, _ = self._measure("reload_codex", lambda: self.core.reload("codex"))
        self.assertLessEqual(ledger.total(CODEX_LOAD_EDITOR), 1)
        self.assertLessEqual(ledger.worst_input(PARSE_YAML), 1)

    def test_a_commit_saves_the_document_once(self) -> None:
        staged = self.core.dispatch(
            {
                "domain": "providers_models",
                "type": "provider.patch",
                "payload": {"provider_id": "provider0", "changes": {"enabled": True}},
            }
        )
        ledger, _ = self._measure(
            "apply",
            lambda: self.core.apply("providers_models", revision=staged["revision"]),
        )
        self.assertLessEqual(ledger.total(PROVIDER_SAVE), 2)
        self.assertLessEqual(ledger.total(PROVIDER_RELOAD), 1)
        self.assertLessEqual(ledger.total(CATALOG_ROUTE_INDEX), 1)

    def test_no_operation_derives_one_document_twice_in_a_row(self) -> None:
        """The invariant behind every defect in this class, stated once.

        Whatever an operation does, it must not hand the same bytes to the YAML
        loader twice: the second parse can only answer what the first already
        did.  This is the check that would have caught the six-parse reload
        before anything was profiled.
        """

        def commit() -> None:
            staged = self.core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.patch",
                    "payload": {"provider_id": "provider0", "changes": {"enabled": True}},
                }
            )
            self.core.apply("providers_models", revision=staged["revision"])

        with work_ledger() as ledger:
            for _ in range(3):
                ledger.reset()
                commit()
                for key, count in ledger.per_input(PARSE_YAML).items():
                    self.assertEqual(
                        1,
                        count,
                        f"one commit parsed the document {count} times"
                        f"\nledger:\n{ledger.report()}",
                    )


class LedgerCatchesRegressionsTests(OperationWorkTests):
    """The instrument must fail on the defects it was written for.

    An instrument nobody has seen fail is indistinguishable from one that cannot
    fail.  Each case reintroduces one real defect that this session found by
    hand -- through the production call path, not around it -- and asserts that
    the budget rejects it.  If a future change makes the ledger stop observing a
    seam, these tests fail even though every performance number looks fine.
    """

    def test_it_rejects_a_projection_built_on_every_snapshot(self) -> None:
        """The defect that made the most-called method the slowest."""

        from young_router.core.service import CoreStore

        real_snapshot = CoreStore.snapshot

        def old_snapshot(self) -> dict:
            # The pre-fix behaviour: the projection was never memoized.
            self._snapshot_cache = None
            return real_snapshot(self)

        with work_ledger() as ledger:
            ledger.reset()
            with mock.patch.object(CoreStore, "snapshot", old_snapshot):
                for _ in range(3):
                    self.core.snapshot()

        self.assertGreater(
            ledger.total(PROJECT_DOMAINS),
            OPERATION_BUDGETS["snapshot"][PROJECT_DOMAINS],
            "a snapshot that reprojects every call was not noticed",
        )

    def test_it_rejects_a_reload_that_parses_per_reader(self) -> None:
        """The defect that cost six YAML parses of one document per reload."""

        from young_router.config import schema as config_schema

        def no_cache(text: str):
            # The pre-fix reader: derive the same bytes again, every time.
            return config_schema._yaml_load(text)

        with work_ledger() as ledger:
            ledger.reset()
            with mock.patch.object(config_schema, "_yaml_text_parse", no_cache):
                self.core.reload("providers_models")

        self.assertGreater(
            ledger.worst_input(PARSE_YAML),
            PER_INPUT_LIMITS[PARSE_YAML],
            "a reload that reparsed one document per reader was not noticed",
        )

    def test_it_rejects_a_domain_apply_that_builds_a_dropped_projection(self) -> None:
        """The defect that built the provider projection three times per click."""

        staged = self.core.dispatch(
            {
                "domain": "providers_models",
                "type": "provider.patch",
                "payload": {"provider_id": "provider0", "changes": {"enabled": True}},
            }
        )
        provider = self.core._domains["providers_models"]
        real_apply = provider.apply

        def old_apply(payload: object | None = None) -> dict:
            result = real_apply(payload)
            # The pre-fix answer: a projection nobody reads.
            return {"applied": True, **provider.snapshot(), **result}

        with work_ledger() as ledger:
            ledger.reset()
            with mock.patch.object(provider, "apply", old_apply):
                self.core.apply("providers_models", revision=staged["revision"])

        self.assertGreater(
            ledger.total(PROVIDER_SNAPSHOT),
            OPERATION_BUDGETS["apply"][PROVIDER_SNAPSHOT],
            "an Apply that projects its domain for a discarded answer was not noticed",
        )


if __name__ == "__main__":
    unittest.main()
