"""The General pane's local API key: one owner, one written line.

The key every client of this machine's proxy presents lives in
``config.yaml``'s ``general_settings.master_key`` -- the field LiteLLM reads and
the field the Codex/Claude editors already resolve -- so the pane edits that
document rather than starting a second store beside it.  These tests pin the
three properties that make that safe: the document keeps every other byte, the
key never travels through a snapshot, and a change asks for a replaced process
because a rolling reload cannot change what the master authenticates with.
"""
from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
import unittest.mock
from pathlib import Path

from young_router.core.domains.providers_models import ProvidersModelsDomain
from young_router.core.domains.runtime import (
    DEFAULT_MASTER_KEY,
    MASTER_KEY_SETTING,
    RuntimeSettingsDomain,
    master_key_from_document,
    master_key_in_document,
)
from young_router.core.service import CoreStore


DOCUMENT = """\
    # hand written: comments and anchors survive a key change
    providers:
      primary: &primary
        api_base: https://example.test/v1
        api_keys:
          - name: default
            value: replace-me
    model_list:
      - model_name: local-chat
        litellm_params:
          model: openai/local-chat
          api_base: *primary
          api_key: replace-me
        model_info:
          id: a1b2c3d4
          provider: primary
    general_settings:
      master_key: sk-young-router
      ui: true
    router_settings:
      routing_strategy: simple-shuffle
"""


class MasterKeyDocumentTests(unittest.TestCase):
    def test_the_published_default_is_what_a_document_without_a_key_reads_as(self) -> None:
        self.assertEqual(DEFAULT_MASTER_KEY, "sk-young-router")
        self.assertEqual(master_key_from_document("providers: {}\n"), DEFAULT_MASTER_KEY)
        self.assertEqual(master_key_from_document(""), DEFAULT_MASTER_KEY)
        self.assertEqual(master_key_from_document("general_settings: {}\n"), DEFAULT_MASTER_KEY)

    def test_a_rewrite_states_the_key_and_changes_nothing_else(self) -> None:
        source = textwrap.dedent(DOCUMENT)

        updated = master_key_in_document(source, "sk-replacement-1")

        self.assertEqual(
            updated,
            source.replace("  master_key: sk-young-router\n", "  master_key: sk-replacement-1\n"),
        )
        # Reading it back is the check the writer performs before it writes.
        self.assertEqual(master_key_from_document(updated), "sk-replacement-1")

    def test_a_document_without_a_section_gains_one(self) -> None:
        updated = master_key_in_document("providers: {}\n", "sk-new")

        self.assertIn("general_settings:\n  master_key: sk-new\n", updated)
        self.assertTrue(updated.startswith("providers: {}\n"))
        self.assertEqual(master_key_from_document(updated), "sk-new")

    def test_a_block_without_the_key_gains_it_at_its_own_indentation(self) -> None:
        source = "general_settings:\n    ui: true\n"

        updated = master_key_in_document(source, "sk-new")

        self.assertEqual(updated, "general_settings:\n    master_key: sk-new\n    ui: true\n")

    def test_a_value_that_would_reshape_the_document_is_refused(self) -> None:
        for candidate in ("", "a b", "sk-key\nother: 1", '"quoted"', "sk-1#2"):
            with self.subTest(candidate=candidate):
                with self.assertRaises(Exception):
                    master_key_in_document("general_settings:\n  master_key: old\n", candidate)


class GeneralApiKeyDomainTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.config = self.root / "config.yaml"
        self.config.write_text(textwrap.dedent(DOCUMENT), encoding="utf-8")
        self.domain = RuntimeSettingsDomain(self.root / "runtime-settings.env")
        self.domain.master_key_path = self.config
        self.domain.reload()

    def tearDown(self) -> None:
        self._directory.cleanup()

    def test_the_pane_reads_the_key_the_proxy_would_accept(self) -> None:
        self.assertTrue(self.domain.secret_present("setting", MASTER_KEY_SETTING))
        self.assertEqual(
            self.domain.trusted_secret_value("setting", MASTER_KEY_SETTING),
            "sk-young-router",
        )

    def test_a_staged_key_is_written_on_apply_and_only_there(self) -> None:
        before = self.config.read_text(encoding="utf-8")

        self.domain.stage_secret("setting", MASTER_KEY_SETTING, "sk-general-1")
        self.assertTrue(self.domain.external_disk_state()["changed"] is False)
        applied = self.domain.apply()

        self.assertTrue(applied["master_key_changed"])
        self.assertTrue(self.domain.master_key_changed())
        self.assertEqual(
            self.config.read_text(encoding="utf-8"),
            before.replace("  master_key: sk-young-router\n", "  master_key: sk-general-1\n"),
        )
        self.assertEqual(
            self.domain.trusted_secret_value("setting", MASTER_KEY_SETTING),
            "sk-general-1",
        )

    def test_an_unchanged_key_is_not_rewritten(self) -> None:
        before = self.config.read_bytes()

        self.domain.stage_secret("setting", MASTER_KEY_SETTING, "sk-young-router")
        self.domain.apply()

        self.assertFalse(self.domain.master_key_changed())
        self.assertEqual(self.config.read_bytes(), before)

    def test_the_empty_and_the_unprintable_key_are_refused(self) -> None:
        for candidate in ("", "   ", "two words", "sk-a\nsk-b"):
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(Exception, "API key"):
                    self.domain.stage_secret("setting", MASTER_KEY_SETTING, candidate)

    def test_a_key_edited_elsewhere_since_the_draft_was_read_is_not_overwritten(self) -> None:
        self.domain.stage_secret("setting", MASTER_KEY_SETTING, "sk-general-2")
        self.config.write_text(
            self.config.read_text(encoding="utf-8").replace(
                "master_key: sk-young-router", "master_key: sk-somebody-else"
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(Exception, "changed on disk"):
            self.domain.apply()

    def test_a_provider_edit_beside_the_key_is_kept(self) -> None:
        """The provider editor writes other lines of the same document.

        A save there must not read as an outside edit of this field, and this
        field's write must not undo it: the two panes own different parts of one
        file, and each keeps the other's work.
        """

        self.domain.stage_secret("setting", MASTER_KEY_SETTING, "sk-general-4")
        self.config.write_text(
            self.config.read_text(encoding="utf-8").replace("ui: true", "ui: false"),
            encoding="utf-8",
        )

        self.assertFalse(self.domain.external_disk_state()["changed"])
        self.domain.apply()

        applied = self.config.read_text(encoding="utf-8")
        self.assertIn("  master_key: sk-general-4\n", applied)
        self.assertIn("  ui: false\n", applied)

    def test_the_field_reports_a_key_staged_from_the_settings_editor(self) -> None:
        # The document is the source of truth, so a hand edit to the key is
        # noticed by this pane instead of being silently replaced by a stale
        # draft on the next Apply.
        self.config.write_text(
            self.config.read_text(encoding="utf-8").replace(
                "master_key: sk-young-router", "master_key: sk-hand-edited"
            ),
            encoding="utf-8",
        )

        self.assertTrue(self.domain.external_disk_state()["changed"])
        self.domain.reload()
        self.assertEqual(
            self.domain.trusted_secret_value("setting", MASTER_KEY_SETTING),
            "sk-hand-edited",
        )

    def test_a_missing_document_keeps_the_default_instead_of_failing_to_load(self) -> None:
        missing = self.root / "absent.yaml"
        domain = RuntimeSettingsDomain(self.root / "other-settings.env")
        domain.master_key_path = missing

        domain.reload()

        self.assertEqual(domain.trusted_secret_value("setting", MASTER_KEY_SETTING), DEFAULT_MASTER_KEY)
        with self.assertRaisesRegex(Exception, "unavailable"):
            domain.stage_secret("setting", MASTER_KEY_SETTING, "sk-x")
            domain.apply()

    def test_no_snapshot_carries_the_key(self) -> None:
        self.domain.stage_secret("setting", MASTER_KEY_SETTING, "sk-never-in-a-snapshot")

        rendered = repr(self.domain.snapshot())

        self.assertNotIn("sk-never-in-a-snapshot", rendered)
        self.assertNotIn("sk-young-router", rendered)


class GeneralApiKeyCoreTests(unittest.TestCase):
    """The Core-level obligations: no snapshot leak, and a replaced process."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.config = self.root / "config.yaml"
        self.config.write_text(textwrap.dedent(DOCUMENT), encoding="utf-8")
        self.operations: list[str] = []

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _store(self) -> CoreStore:
        runtime = RuntimeSettingsDomain(self.root / "runtime-settings.env")
        runtime.master_key_path = self.config
        runtime.reload()
        providers = ProvidersModelsDomain(self.config)

        def reload_service(operation: str) -> dict[str, str]:
            self.operations.append(operation)
            return {"state": "running"}

        def status_service(_operation: str) -> dict[str, str]:
            return {"state": "running"}

        return CoreStore(
            domains=[providers, runtime],
            service_handlers={"status": status_service, "reload": reload_service},
        )

    def test_a_changed_key_is_staged_and_reaches_the_document_on_apply(self) -> None:
        store = self._store()
        try:
            # A credential is read only through the native lease, never through
            # the snapshot a window renders.
            self.assertNotIn("sk-young-router", repr(store.snapshot()))
            descriptor, value = store._trusted_secret_document_read(
                "runtime", "setting", MASTER_KEY_SETTING
            )
            self.assertEqual(value, "sk-young-router")
            self.assertTrue(descriptor["present"])

            staged = store.stage_secret(
                "runtime",
                "setting",
                MASTER_KEY_SETTING,
                "sk-general-3",
                revision=store.revision,
            )
            self.assertTrue(store.snapshot()["drafts"]["runtime"]["dirty"])
            applied = store.apply("runtime", revision=staged["revision"])
            # The restart runs on its own thread so the commit stays fast.
            store.wait_for_service_reload(10.0)
        finally:
            store.shutdown()

        self.assertTrue(applied["applied"])
        self.assertIn("  master_key: sk-general-3\n", self.config.read_text(encoding="utf-8"))
        self.assertEqual(self.operations, ["restart"])

    def test_a_coordinated_commit_that_changes_the_key_replaces_the_process(self) -> None:
        """A provider edit beside the key must not turn the restart into a roll.

        ``providers_models`` and ``runtime`` applied together are a document
        change, which the reload path serves by rolling the workers; but the
        proxy reads its key at startup, so this commit has to replace the
        process or the key it wrote would never be the one it accepts.
        """

        store = self._store()
        try:
            staged = store.dispatch(
                {
                    "domain": "providers_models",
                    "type": "model.patch",
                    "payload": {
                        "provider_id": "primary",
                        "model_id": "a1b2c3d4",
                        "changes": {"upstream_model": "openai/renamed-chat"},
                    },
                },
                expected_revision=store.revision,
            )
            staged = store.stage_secret(
                "runtime",
                "setting",
                MASTER_KEY_SETTING,
                "sk-general-5",
                revision=staged["revision"],
            )
            store.apply(domains=["providers_models", "runtime"], revision=staged["revision"])
            store.wait_for_service_reload(10.0)
        finally:
            store.shutdown()

        applied = self.config.read_text(encoding="utf-8")
        self.assertIn("  master_key: sk-general-5\n", applied)
        self.assertIn("openai/renamed-chat", applied)
        self.assertEqual(self.operations, ["restart"])

    def test_a_runtime_commit_that_keeps_the_key_takes_its_environment_path(self) -> None:
        # Runtime settings reach the proxy through its environment, so any
        # runtime commit already replaces the process; the key never weakens
        # that back to a rolling reload.
        store = self._store()
        try:
            staged = store.dispatch(
                {
                    "domain": "runtime",
                    "type": "set_setting",
                    "payload": {"key": "YOUNG_ROUTER_STALL_TIMEOUT_SECONDS", "value": "90"},
                },
                expected_revision=store.revision,
            )
            store.apply("runtime", revision=staged["revision"])
            store.wait_for_service_reload(10.0)
        finally:
            store.shutdown()

        self.assertEqual(self.operations, ["restart"])
        self.assertIn(
            "YOUNG_ROUTER_STALL_TIMEOUT_SECONDS=90",
            (self.root / "runtime-settings.env").read_text(encoding="utf-8"),
        )

    def test_another_runtime_slot_cannot_borrow_the_plaintext_read(self) -> None:
        store = self._store()
        try:
            with self.assertRaises(Exception):
                store._trusted_secret_document_read("runtime", "setting", "LITELLM_PORT")
        finally:
            store.shutdown()


class ProxyKeyEnvironmentTests(unittest.TestCase):
    """The child hears the same key the document states, before it loads it.

    LiteLLM takes ``LITELLM_MASTER_KEY`` from the environment *before* it reads
    any document and falls back to it only when the document states none, so a
    worker that came up between those two steps served with no key at all and
    answered every client "No connected db." until it was replaced.  Handing it
    the document's own key leaves no such instant.
    """

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        (self.root / ".litellm-runtime").mkdir()
        self.config = self.root / ".litellm-runtime" / "config.yaml"

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _environment(self) -> dict[str, str]:
        from young_router.core.operations import CoreServiceController

        return CoreServiceController(self.root)._runtime_env()

    def test_the_document_key_reaches_the_child_environment(self) -> None:
        self.config.write_text(
            "providers: {}\nmodel_list: []\ngeneral_settings:\n  master_key: sk-own-key\n",
            encoding="utf-8",
        )

        self.assertEqual(self._environment()["LITELLM_MASTER_KEY"], "sk-own-key")

    def test_a_document_without_a_key_states_the_published_default(self) -> None:
        self.config.write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")

        self.assertEqual(self._environment()["LITELLM_MASTER_KEY"], DEFAULT_MASTER_KEY)

    def test_an_environment_reference_is_resolved_not_passed_through(self) -> None:
        # The child would otherwise receive the literal ``os.environ/...`` text
        # as its key, and neither this app nor LiteLLM would accept anything.
        self.config.write_text(
            "providers: {}\nmodel_list: []\ngeneral_settings:\n  master_key: os.environ/KEY_FOR_THE_TEST\n",
            encoding="utf-8",
        )

        with unittest.mock.patch.dict(os.environ, {"KEY_FOR_THE_TEST": "sk-resolved"}, clear=False):
            self.assertEqual(self._environment()["LITELLM_MASTER_KEY"], "sk-resolved")


if __name__ == "__main__":
    unittest.main()
