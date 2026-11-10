"""What a staged write is compared against.

The shared revision counts everything a window can observe, and most of what
moves it is not a settings edit: the proxy leaves and re-enters ``running``
once per Apply, a login observation settles, a client model list is
republished, a probe answers.  A write arriving just after one of those was
refused as ``revision_conflict`` even though the draft it carried was the
newest one there was, and the pane reported a save that had happened as
更改未生效.  Only a settings change the caller has not seen may refuse it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from young_router.core.domains.language import LanguageSettingsDomain
from young_router.core.service import CoreStore, RevisionConflict


class RevisionScopeTests(unittest.TestCase):
    def _core(self, root: Path) -> CoreStore:
        return CoreStore(domains=[LanguageSettingsDomain(root / "language.json")])

    def _staged(self, core: CoreStore) -> int:
        """Stage one settings edit the way a pane does, and return its revision.

        A pane applies with the revision its own dispatch answered, not the one
        it held before the edit: the draft it is committing is the one that
        dispatch wrote.
        """

        core.dispatch({"domain": "language", "type": "set", "payload": {"language": "en"}})
        return core.revision

    def test_a_projection_advance_does_not_make_a_staged_write_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            core = self._core(Path(directory))
            revision = self._staged(core)

            # The proxy moving between states, a login settling, a client model
            # list republished: none of it is in the draft the caller staged.
            core._advance_projection_revision()
            core._advance_projection_revision()

            applied = core.apply("language", revision=revision)

            self.assertTrue(applied["applied"])
            self.assertEqual("en", core.snapshot()["language"])

    def test_a_settings_change_after_the_callers_revision_still_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            core = self._core(Path(directory))
            revision = self._staged(core)

            # Another writer changes the settings document this caller staged
            # over: that is the draft it has not seen, and the revision's whole
            # purpose is to refuse it.
            core.dispatch({"domain": "language", "type": "set", "payload": {"language": "zh-Hans"}})
            with self.assertRaises(RevisionConflict):
                core.apply("language", revision=revision)

    def test_a_projection_between_two_settings_writes_is_still_ordered(self) -> None:
        """The projection advance keeps the ordering a reader depends on."""

        with tempfile.TemporaryDirectory() as directory:
            core = self._core(Path(directory))
            before = core.revision
            core._advance_projection_revision()
            self.assertGreater(core.revision, before)
            self.assertEqual(core.snapshot()["revision"], core.revision)


if __name__ == "__main__":
    unittest.main()
