"""Image context: which images reach the upstream, and how a fetch answers.

The pass exists because an agent client replays its whole transcript on every
turn, images included.  These tests pin the three properties the rest of the
data plane relies on: the current turn is never touched, a signed prefix is
never touched, and every id the pass omits can be served back.
"""

from __future__ import annotations

import base64
import json
import os
import random
import struct
import tempfile
import unittest
import zlib
from unittest import mock

from young_router.proxy import image_context as image_context_module
from young_router.proxy import image_context_mcp as mcp_module
from young_router.proxy import image_context_store as store_module


def _png(width: int, height: int, noisy: bool = False, seed: int = 1) -> bytes:
    """A real PNG of the requested size; ``noisy`` keeps it above a byte floor."""

    if noisy:
        generator = random.Random(seed)
        rows = b"".join(
            b"\x00" + bytes(generator.randrange(256) for _ in range(width * 3))
            for _ in range(height)
        )
    else:
        rows = b"".join(
            b"\x00" + bytes([seed % 256, 7, 9]) * width for _ in range(height)
        )

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return (
            struct.pack(">I", len(payload))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def _data_url(raw: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def _upload_parts(raw: bytes, name: str = "[Image #1]") -> list:
    """The parts one upload adds, Codex's own ``<image …>`` tags included."""

    return [
        {"type": "input_text", "text": f'<image name={name} path="/tmp/a.png">'},
        {"type": "input_image", "image_url": _data_url(raw)},
        {"type": "input_text", "text": "</image>"},
    ]


def _user_turn(text: str, *groups: list) -> dict:
    content: list = [{"type": "input_text", "text": text}]
    for group in groups:
        content.extend(group)
    return {"type": "message", "role": "user", "content": content}


def _assistant(text: str) -> dict:
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def _tool_image(raw: bytes, call_id: str = "c1") -> dict:
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": [{"type": "input_image", "image_url": _data_url(raw)}],
    }


def _request(items: list, **overrides) -> dict:
    request = {
        "call_type": "aresponses",
        # A real replay always carries Codex's own identification; the pass is
        # deliberately inert without it.
        "headers": {"Originator": "codex_cli_rs"},
        "input": items,
    }
    request.update(overrides)
    return request


class _ImageContextCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.data_dir = os.path.join(self.directory.name, "data")
        os.makedirs(os.path.join(self.data_dir, "selection"), exist_ok=True)
        self.environment = mock.patch.dict(
            os.environ,
            {
                image_context_module.IMAGE_CONTEXT_ENABLED_ENV: "1",
                image_context_module.IMAGE_CONTEXT_DATA_DIR_ENV: self.data_dir,
                image_context_module.IMAGE_CONTEXT_MIN_BYTES_ENV: "1024",
            },
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.directory.cleanup()

    def selection(self, thread_id: str, payload: dict) -> None:
        path = os.path.join(self.data_dir, "selection", f"{thread_id}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def rewritten(self, items: list, **overrides) -> dict:
        result = image_context_module._with_codex_image_context(
            _request(items, **overrides)
        )
        self.assertIsNotNone(result)
        assert result is not None
        return result


class ImageContextScanTests(_ImageContextCase):
    def test_scan_numbers_images_in_wire_order_and_reads_their_size(self) -> None:
        items = [
            _user_turn("first", _upload_parts(_png(9, 7))),
            _assistant("ok"),
            _user_turn("second", _upload_parts(_png(5, 3, seed=4), "[Image #2]")),
        ]

        found = image_context_module.scan(items)

        self.assertEqual([image.image_id for image in found], ["IMG-0001", "IMG-0002"])
        self.assertEqual([image.turn for image in found], [1, 2])
        self.assertEqual((found[0].width, found[0].height), (9, 7))
        self.assertEqual(found[0].kind, "upload")
        self.assertEqual(found[1].kind, "upload")

    def test_a_tool_result_is_its_own_kind_and_shares_its_turn(self) -> None:
        items = [_user_turn("first"), _tool_image(_png(5, 3))]

        found = image_context_module.scan(items)

        self.assertEqual([image.kind for image in found], ["tool"])
        self.assertEqual([image.turn for image in found], [1])


class ImageContextRewriteTests(_ImageContextCase):
    def test_the_current_turn_is_never_rewritten(self) -> None:
        items = [
            _user_turn("first", _upload_parts(_png(120, 90, noisy=True))),
            _user_turn("second", _upload_parts(_png(60, 40, noisy=True, seed=2), "[Image #2]")),
        ]

        result = self.rewritten(items)

        # The newest turn keeps every part, tags included.
        self.assertEqual(result["input"][-1], items[-1])

    def test_an_aged_image_becomes_a_placeholder_that_keeps_its_identity(self) -> None:
        items = [
            _user_turn("第一轮", _upload_parts(_png(120, 90, noisy=True))),
            _user_turn("第二轮"),
            _user_turn("第三轮"),
        ]

        result = self.rewritten(items)

        # The opening text survives, the image and both of its tags are one
        # placeholder, and the turn's own text is untouched.
        replacement = result["input"][1]["content"]
        self.assertEqual(
            [part["type"] for part in replacement], ["input_text", "input_text"]
        )
        self.assertEqual(replacement[0], items[0]["content"][0])
        text = replacement[1]["text"]
        self.assertIn("图片 IMG-0001 已省略", text)
        self.assertIn("第 1 轮", text)
        self.assertIn("120×90", text)
        self.assertIn("[Image #1]", text)

    def test_a_signed_prefix_is_never_rewritten(self) -> None:
        items = [
            {"type": "reasoning", "encrypted_content": "opaque"},
            _user_turn("第一轮", _upload_parts(_png(120, 90, noisy=True))),
            _user_turn("第二轮"),
            _user_turn("第三轮"),
        ]

        result = self.rewritten(items)

        # Everything up to and including the signed item is byte-identical, so
        # the upstream's cached prefix stays the prefix it signed.  The images
        # after it are still bounded, and the note follows the signed prefix.
        self.assertEqual(result["input"][0], items[0])
        self.assertIn("IMG-0001", result["input"][2]["content"][1]["text"])

    def test_the_note_explains_the_placeholders_once_before_their_turn(self) -> None:
        items = [
            _user_turn("first", _upload_parts(_png(120, 90, noisy=True))),
            _user_turn("second"),
            _user_turn("third"),
        ]

        result = self.rewritten(items)

        notes = [
            item
            for item in result["input"]
            if item.get("type") == "message" and item.get("role") == "developer"
        ]
        self.assertEqual(len(notes), 1)
        # It sits before the turn the first omitted image appeared in, never
        # between a call and its output.
        self.assertEqual(result["input"].index(notes[0]), 0)

    def test_small_older_images_stay_inline(self) -> None:
        items = [
            _user_turn("first", _upload_parts(_png(4, 4))),
            _user_turn("second"),
            _user_turn("third"),
        ]

        self.assertIsNone(image_context_module._with_codex_image_context(_request(items)))

    def test_an_identical_image_still_sent_makes_a_duplicate_placeholder(self) -> None:
        raw = _png(120, 90, noisy=True)
        items = [
            _user_turn("第一轮", _upload_parts(raw)),
            _user_turn("第二轮", _upload_parts(raw, "[Image #2]")),
            _user_turn("第三轮"),
        ]

        result = self.rewritten(items)

        # One copy stays sent, so the other becomes the duplicate placeholder
        # naming it -- the content is never lost, only the bytes.
        text = result["input"][1]["content"][1]["text"]
        self.assertIn("图片 IMG-0001 未重复提供", text)
        self.assertIn("IMG-0002", text)
        self.assertEqual(result["input"][2], items[1])

    def test_the_switch_off_means_the_request_is_untouched(self) -> None:
        items = [
            _user_turn("first", _upload_parts(_png(120, 90, noisy=True))),
            _user_turn("second"),
            _user_turn("third"),
        ]
        with mock.patch.dict(
            os.environ, {image_context_module.IMAGE_CONTEXT_ENABLED_ENV: "0"}
        ):
            self.assertIsNone(
                image_context_module._with_codex_image_context(_request(items))
            )

    def test_a_request_without_codex_evidence_is_left_alone(self) -> None:
        items = [
            _user_turn("first", _upload_parts(_png(120, 90, noisy=True))),
            _user_turn("second"),
            _user_turn("third"),
        ]
        request = _request(items)
        request.pop("headers")

        self.assertIsNone(image_context_module._with_codex_image_context(request))

    def test_a_compaction_request_is_replayed_byte_exact(self) -> None:
        items = [
            _user_turn("first", _upload_parts(_png(120, 90, noisy=True))),
            _user_turn("second"),
            {"type": "compaction_trigger"},
        ]
        request = _request(
            items,
            metadata={"x-codex-turn-metadata": '{"request_kind":"compaction"}'},
        )

        self.assertIsNone(image_context_module._with_codex_image_context(request))

    def test_a_route_that_refuses_images_gets_its_history_unchanged(self) -> None:
        items = [
            _user_turn("first", _upload_parts(_png(120, 90, noisy=True))),
            _user_turn("second"),
            _user_turn("third"),
        ]
        request = _request(
            items,
            litellm_params={"model_info": {"supports_vision": False}},
        )

        self.assertIsNone(image_context_module._with_codex_image_context(request))

    def test_manual_mode_sends_everything_the_user_did_not_uncheck(self) -> None:
        thread = "11111111-2222-3333-4444-555555555555"
        self.selection(thread, {"threadId": thread, "auto": False, "unchecked": {}})
        items = [
            _user_turn("first", _upload_parts(_png(120, 90, noisy=True))),
            _user_turn("second"),
            _user_turn("third"),
        ]

        self.assertIsNone(
            image_context_module._with_codex_image_context(
                _request(items, metadata={"thread_id": thread})
            )
        )

    def test_manual_mode_honours_an_id_the_panel_unchecked(self) -> None:
        thread = "11111111-2222-3333-4444-666666666666"
        self.selection(
            thread,
            {
                "threadId": thread,
                "auto": False,
                "unchecked": {"IMG-0001": {"id": "IMG-0001"}},
            },
        )
        items = [_user_turn("第一轮", _upload_parts(_png(4, 4))), _user_turn("第二轮")]

        result = self.rewritten(items, metadata={"thread_id": thread})

        replaced = [
            item
            for item in result["input"]
            if item.get("role") == "user" and len(item.get("content") or []) == 2
        ][0]
        self.assertIn("IMG-0001", replaced["content"][1]["text"])

    def test_the_task_id_is_read_from_the_clients_turn_metadata(self) -> None:
        # Codex carries the task id only inside this JSON header; without it the
        # pass has nowhere to file the replay the fetch tool answers from.
        metadata = json.dumps({"thread_id": "aaaa", "turn_id": "t1"})

        self.assertEqual(
            image_context_module._thread_id(
                {"proxy_server_request": {"headers": {"x-codex-turn-metadata": metadata}}}
            ),
            "aaaa",
        )
        self.assertEqual(
            image_context_module._thread_id(
                {"litellm_metadata": {"X-Codex-Turn-Metadata": metadata}}
            ),
            "aaaa",
        )
        self.assertIsNone(image_context_module._thread_id({}))

    def test_a_pinned_id_is_sent_in_auto_mode(self) -> None:
        thread = "11111111-2222-3333-4444-777777777777"
        self.selection(
            thread,
            {"threadId": thread, "auto": True, "pinned": {"IMG-0001": {"id": "IMG-0001"}}},
        )
        items = [
            _user_turn("first", _upload_parts(_png(120, 90, noisy=True))),
            _user_turn("second"),
            _user_turn("third"),
        ]

        self.assertIsNone(
            image_context_module._with_codex_image_context(
                _request(items, metadata={"thread_id": thread})
            )
        )


class ImageContextFetchTests(_ImageContextCase):
    def test_the_fetch_answers_from_the_request_the_proxy_stored(self) -> None:
        thread = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        raw = _png(120, 90, noisy=True)
        items = [
            _user_turn("first", _upload_parts(raw)),
            _user_turn("second"),
            _user_turn("third"),
        ]

        self.rewritten(items, metadata={"thread_id": thread})

        answer = store_module.image_data_url(thread, "IMG-0001")
        self.assertIsNotNone(answer)
        assert answer is not None
        mime, data_url = answer
        self.assertEqual(mime, "image/png")
        self.assertEqual(base64.b64decode(data_url.split(",", 1)[1]), raw)

    def test_an_id_the_task_no_longer_carries_is_reported_missing(self) -> None:
        thread = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
        items = [_user_turn("first", _upload_parts(_png(4, 4))), _user_turn("second")]
        image_context_module._with_codex_image_context(
            _request(items, metadata={"thread_id": thread})
        )

        self.assertIsNone(store_module.image_data_url(thread, "IMG-0099"))

    def test_the_mcp_server_lists_one_tool_and_answers_with_image_blocks(self) -> None:
        thread = "cccccccc-dddd-eeee-ffff-000000000000"
        items = [_user_turn("first", _upload_parts(_png(4, 4))), _user_turn("second")]
        image_context_module._with_codex_image_context(
            _request(items, metadata={"thread_id": thread})
        )
        with mock.patch.dict(os.environ, {mcp_module.THREAD_ENV: thread}):
            listed = mcp_module.handle(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )
            self.assertEqual(
                [tool["name"] for tool in listed["result"]["tools"]],  # type: ignore[index]
                [mcp_module.TOOL_NAME],
            )
            answer = mcp_module.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": mcp_module.TOOL_NAME,
                        "arguments": {"ids": ["IMG-0001"]},
                    },
                }
            )

        blocks = answer["result"]["content"]  # type: ignore[index]
        self.assertEqual([block["type"] for block in blocks], ["text", "image"])
        self.assertEqual(blocks[1]["mimeType"], "image/png")

    def test_the_initializer_names_the_server_and_its_instructions(self) -> None:
        answer = mcp_module.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})

        assert answer is not None
        result = answer["result"]
        self.assertEqual(result["serverInfo"]["name"], mcp_module.SERVER_NAME)
        self.assertIn("id", result["instructions"])

    def test_a_call_without_ids_is_an_error_the_model_can_read(self) -> None:
        answer = mcp_module.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": mcp_module.TOOL_NAME, "arguments": {}},
            }
        )

        assert answer is not None
        self.assertTrue(answer["result"]["isError"])


class ImageContextStorageTests(_ImageContextCase):
    def test_a_stored_replay_holds_images_and_never_text(self) -> None:
        thread = "dddddddd-eeee-ffff-0000-111111111111"
        items = [
            _user_turn("a secret sentence", _upload_parts(_png(4, 4))),
            _user_turn("another secret sentence"),
        ]

        store_module.store(thread, {"input": items})

        path = os.path.join(
            self.data_dir, "young-router-replays", f"{thread}.json"
        )
        with open(path, encoding="utf-8") as handle:
            written = handle.read()
        self.assertNotIn("secret sentence", written)
        self.assertIn("IMG-0001", written)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
