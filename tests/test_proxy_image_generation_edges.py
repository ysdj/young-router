from __future__ import annotations

import base64
from unittest import mock

from hook_test_utils import *


class ProxyImageGenerationEdgeTests(HookTestCase):
    def test_image_generation_result_payload_accepts_sse_text(self) -> None:
        hooks, _ = load_hook_module()
        frame = (
            "data: "
            + json.dumps(
                {
                    "type": "image_generation_call",
                    "result": VALID_IMAGE_RESULT,
                }
            )
            + "\n\n"
        )

        self.assertTrue(hooks._response_has_image_generation_result_payload(frame))

    def test_image_generation_materialize_decodes_result_once(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as codex_home:
            self.set_env("CODEX_HOME", codex_home)
            payload = {
                "type": "image_generation_call",
                "id": "ig_once",
                "result": VALID_IMAGE_RESULT,
            }
            original_decode = base64.b64decode
            decodes: list[object] = []

            def counting_decode(*args, **kwargs):
                decodes.append(args[0])
                return original_decode(*args, **kwargs)

            with mock.patch.object(base64, "b64decode", counting_decode):
                changed = hooks._image_generation_attach_paths_to_payload(payload, {})
            self.assertTrue(changed)
            self.assertEqual(len(decodes), 1)

    def test_image_generation_current_reference_rejects_another_saved_image(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as codex_home:
            old_path = Path(codex_home) / "generated_images" / "old.png"
            old_path.parent.mkdir(parents=True)
            old_path.write_bytes(b"x" * 300)
            current_path = Path(codex_home) / "generated_images" / "current.png"
            payload = {
                "type": "message",
                "content": f"![Generated image]({old_path})",
            }
            self.assertTrue(
                hooks._image_generation_path_is_usable(str(old_path), str(current_path))
            )
            self.assertFalse(
                hooks._image_generation_payload_has_current_reference(
                    payload,
                    [str(current_path)],
                )
            )

    def test_image_generation_path_probe_reads_only_the_header(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as codex_home:
            path = Path(codex_home) / "generated_images" / "probe.png"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"x" * 300)
            with mock.patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("whole file read"),
            ):
                usable = hooks._image_generation_path_is_usable(str(path), "")

        self.assertTrue(usable)
