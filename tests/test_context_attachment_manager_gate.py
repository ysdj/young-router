"""The build gate that keeps this app's image context aligned with upstream.

Young Router mirrors Context Attachment Manager's placeholder wording and reads
its selection file, so every artifact build resolves that release and refuses one
that moved either.  These tests pin the gate itself: it passes on the release the
pass was written against, and it fails — loudly, naming the file — on a release
that changed one of the mirrored details.
"""

from __future__ import annotations

import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import update_context_attachment_manager as gate  # noqa: E402


_REWRITE = """
// The image sits in the history, so the model concludes it never saw it.
const WORDS = {
  zh: {
    heading: (id, label, state, fields) => `[图片 ${id}${label ? `（${label}）` : ""} ${state}｜${fields.join("｜")}]`,
    plain: (id) => `需要重看时，在回复里写“需要 ${id}”。`,
    autoPlain: (id) => `需要原图时调用 cam_view_image，传入“${id}”。`,
    omitted: "已省略",
    autoNote: ["上下文管理说明（来自用户安装的上下文素材管理工具）：这个任务开着“自动选图”。"],
  },
};
export function rewriteItems(items, describe, unchecked, pixelHash, options = "zh") {
  return { items, report: {} };
}
"""

_SELECTION = """
export type Selection = { threadId: string; unchecked: Record<string, Mark>; auto?: boolean; pinned?: Record<string, Mark> };
export function readSelection(threadId: string, dir: string): Selection { return { threadId, unchecked: {} }; }
"""

_THREAD_INDEX = """
const entry = { id: `IMG-${String(images.length + 1).padStart(3, "0")}`, turn };
"""


def _release_archive(version: str = "0.9.1", *, rewrite: str = _REWRITE) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, text in (
            ("plugin/src/rewrite.ts", rewrite),
            ("plugin/src/selection.ts", _SELECTION),
            ("plugin/src/thread-index.ts", _THREAD_INDEX),
        ):
            payload = text.encode("utf-8")
            info = tarfile.TarInfo(f"context-attachment-manager-{version}/{name}")
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


TAG_COMMIT = "0123456789abcdef0123456789abcdef01234567"
_TAGS = f"{TAG_COMMIT}\trefs/tags/v0.9.0\n{TAG_COMMIT}\trefs/tags/v0.9.1\n"


class ContextAttachmentManagerGateTests(unittest.TestCase):
    def stage(self, *, archive: bytes | None = None, version: str = "0.9.1") -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        output = Path(directory.name) / "staged"
        payload = archive if archive is not None else _release_archive(version)

        def request(url: str, *, timeout: int = 0) -> bytes:
            del timeout, url
            return payload

        with mock.patch.object(gate, "_request", request), mock.patch.object(
            gate, "resolve_release", lambda tags_url=gate.TAGS_URL: {
                "version": version,
                "tag": f"v{version}",
                "commit": TAG_COMMIT,
            }
        ):
            gate.stage(output)
        return output

    def test_the_release_this_pass_mirrors_passes_and_is_recorded(self) -> None:
        output = self.stage()

        recorded = json.loads((output / "release.json").read_text(encoding="utf-8"))
        self.assertEqual(recorded["version"], "0.9.1")
        self.assertEqual(recorded["repository"], gate.REPOSITORY)
        self.assertIn("plugin/src/rewrite.ts", recorded["verified"])

    def test_a_release_that_reworded_the_manual_placeholder_fails_the_build(self) -> None:
        reworded = _REWRITE.replace("需要重看时，在回复里写", "需要看这张图时，回复")

        with self.assertRaises(gate.UpdateError) as raised:
            self.stage(archive=_release_archive(rewrite=reworded))

        message = str(raised.exception)
        self.assertIn("plugin/src/rewrite.ts", message)
        self.assertIn("manual placeholder sentence", message)

    def test_a_release_that_dropped_the_pinned_selection_key_fails_the_build(self) -> None:
        without_pinned = _SELECTION.replace("pinned?: Record<string, Mark>", "")

        def request(url: str, *, timeout: int = 0) -> bytes:
            del timeout, url
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
                for name, text in (
                    ("plugin/src/rewrite.ts", _REWRITE),
                    ("plugin/src/selection.ts", without_pinned),
                    ("plugin/src/thread-index.ts", _THREAD_INDEX),
                ):
                    payload = text.encode("utf-8")
                    info = tarfile.TarInfo(f"context-attachment-manager-0.9.1/{name}")
                    info.size = len(payload)
                    bundle.addfile(info, io.BytesIO(payload))
            return buffer.getvalue()

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with mock.patch.object(gate, "_request", request), mock.patch.object(
            gate, "resolve_release", lambda tags_url=gate.TAGS_URL: {
                "version": "0.9.1",
                "tag": "v0.9.1",
                "commit": TAG_COMMIT,
            }
        ):
            with self.assertRaises(gate.UpdateError) as raised:
                gate.stage(Path(directory.name) / "staged")

        self.assertIn("plugin/src/selection.ts", str(raised.exception))

    def test_a_second_build_reuses_the_verified_release(self) -> None:
        output = self.stage()
        downloads: list[str] = []
        release = {
            "version": "0.9.1",
            "tag": "v0.9.1",
            "commit": TAG_COMMIT,
        }

        def request(url: str, *, timeout: int = 0) -> bytes:
            del timeout
            downloads.append(url)
            raise AssertionError("a verified release must not be downloaded again")

        with mock.patch.object(gate, "_request", request), mock.patch.object(
            gate, "resolve_release", lambda tags_url=gate.TAGS_URL: release
        ):
            gate.stage(output)

        self.assertEqual(downloads, [])

    def test_the_tags_listing_picks_the_newest_version_not_the_last_line(self) -> None:
        listing = (
            "aaaa\trefs/tags/v0.10.0\n"
            "bbbb\trefs/tags/v0.9.9\n"
            "cccc\trefs/tags/v0.2.0\n"
            "dddd\trefs/tags/not-a-version\n"
        )

        def run(command: list[str], **_: object) -> Any:
            class Result:
                returncode = 0
                stdout = listing
                stderr = ""

            self.assertEqual(command[:3], ["git", "ls-remote", "--tags"])
            return Result()

        with mock.patch.object(gate.subprocess, "run", run):
            release = gate.resolve_release()

        self.assertEqual(release["version"], "0.10.0")
        self.assertEqual(release["commit"], "aaaa")

    def test_the_pinned_release_is_the_one_the_scripts_describe(self) -> None:
        # The pin is what a reviewer reads to know which upstream behaviour the
        # mirrored wording came from.
        self.assertRegex(gate.PINNED_RELEASE, r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
