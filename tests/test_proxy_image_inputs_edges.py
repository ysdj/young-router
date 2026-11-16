from __future__ import annotations

import base64

from hook_test_utils import *


def _inline_image(byte_count: int) -> dict:
    encoded = base64.b64encode(b"\x00" * byte_count).decode("ascii")
    return {"type": "input_image", "image_url": f"data:image/png;base64,{encoded}"}


def _messages_image(byte_count: int) -> dict:
    encoded = base64.b64encode(b"\x00" * byte_count).decode("ascii")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": encoded,
        },
    }


class ImageInputBudgetEdgesTests(HookTestCase):
    def test_largest_inline_image_is_a_maximum_across_both_request_keys(self) -> None:
        """One image is 300 bytes and the other 1200: no image is 1500."""

        hooks, _ = load_hook_module()
        budget = hooks._image_input_budget(
            {
                "input": [_inline_image(300)],
                "messages": [_inline_image(1200)],
            }
        )
        self.assertEqual(budget["inline_image_bytes"], 1500)
        self.assertEqual(budget["largest_inline_image_bytes"], 1200)
        self.assertEqual(budget["new_suffix_largest_inline_image_bytes"], 1200)

    def test_a_messages_picture_counts_toward_the_budget(self) -> None:
        """The Messages spelling carries bare base64 behind ``source``."""

        hooks, _ = load_hook_module()
        budget = hooks._image_input_budget(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "look"},
                            _messages_image(1200),
                        ],
                    }
                ]
            }
        )
        self.assertEqual(budget["image_count"], 1)
        self.assertEqual(budget["inline_image_count"], 1)
        self.assertEqual(budget["inline_image_bytes"], 1200)

    def test_largest_inline_image_is_a_maximum_across_the_encrypted_boundary(self) -> None:
        """The signed item splits 900 and 600 byte images; neither is 1500."""

        hooks, _ = load_hook_module()
        budget = hooks._image_input_budget(
            {
                "input": [
                    _inline_image(900),
                    {"type": "reasoning", "encrypted_content": "signed"},
                    _inline_image(600),
                ],
            }
        )
        self.assertEqual(budget["image_count"], 2)
        self.assertEqual(budget["largest_inline_image_bytes"], 900)
        self.assertEqual(budget["encrypted_prefix_largest_inline_image_bytes"], 900)
        self.assertEqual(budget["new_suffix_largest_inline_image_bytes"], 600)
