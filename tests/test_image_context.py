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
from pathlib import Path
from unittest import mock

from young_router.proxy import image_context as image_context_module
from young_router.proxy import image_context_endpoint
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
                # Which promise a placeholder may make depends on the host, so
                # every test states it instead of inheriting the developer's.
                image_context_module.IMAGE_CONTEXT_FETCH_ENV: "off",
                # The replay cache has its own default under the runtime root;
                # a test must never write there.
                store_module.REPLAY_CACHE_DIR_ENV: os.path.join(
                    self.directory.name, "replays"
                ),
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

        # The newest turn keeps its image byte-for-byte, and gains the id label
        # that tells the model which image the placeholder named.  The label is
        # inserted, never substituted: an upload with no Codex tag around it
        # would otherwise be swapped for the label and the image would vanish.
        newest = result["input"][-1]
        images = [part for part in newest["content"] if part.get("type") == "input_image"]
        self.assertEqual(images, [items[1]["content"][2]])
        labels = [
            part["text"]
            for part in newest["content"]
            if part.get("type") == "input_text" and "IMG-0002" in part.get("text", "")
        ]
        self.assertEqual(len(labels), 1)
        self.assertIn("provided", labels[0])

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

    def test_a_signed_item_is_kept_byte_exact_but_history_before_it_is_not(self) -> None:
        # A client with server-side storage off stamps an encrypted reasoning
        # item on every turn, so treating the last one as an immovable boundary
        # froze all of history and nothing was ever omitted.  The signed item
        # itself must survive untouched; the images around it are this pass's
        # own business.
        signed = {"type": "reasoning", "encrypted_content": "opaque"}
        items = [
            _user_turn("第一轮", _upload_parts(_png(120, 90, noisy=True))),
            signed,
            _user_turn("第二轮"),
            _user_turn("第三轮"),
        ]

        result = self.rewritten(items)

        self.assertEqual(result["input"][2], signed)
        self.assertIn("IMG-0001", result["input"][1]["content"][1]["text"])
        # The placeholder is what the note introduced; the note itself sits just
        # before the turn it explains.
        self.assertIn("上下文管理说明", json.dumps(result["input"], ensure_ascii=False))

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
        # The copy that is still sent states its own id, and is still sent.
        sent_content = result["input"][2]["content"]
        self.assertTrue(any(part.get("type") == "input_image" for part in sent_content))
        self.assertTrue(
            any("IMG-0002" in part.get("text", "") for part in sent_content)
        )

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


class ImageContextCopyTests(_ImageContextCase):
    def test_a_fetched_copy_keeps_the_id_it_copies(self) -> None:
        raw = _png(320, 240, noisy=True)
        items = [
            _user_turn("第一轮", _upload_parts(raw)),
            _user_turn("第二轮", _upload_parts(_png(300, 200, noisy=True, seed=2))),
            {
                "type": "function_call_output",
                "call_id": "c9",
                "output": [
                    {"type": "input_text", "text": "[图片 IMG-0001 取回的原图｜第 1 轮]"},
                    {"type": "input_image", "image_url": _data_url(raw)},
                ],
            },
            _user_turn("第三轮", _upload_parts(_png(280, 180, noisy=True, seed=3))),
        ]

        found = image_context_module.scan(items)

        # The copy is not a new occurrence: the numbering does not move, so an id
        # the model already knows still names the same picture after a fetch.
        self.assertEqual(
            [(image.image_id, image.copy_of) for image in found],
            [
                ("IMG-0001", None),
                ("IMG-0002", None),
                ("IMG-0001", "IMG-0001"),
                ("IMG-0003", None),
            ],
        )

    def test_a_copy_from_an_earlier_turn_is_left_out_with_its_own_note(self) -> None:
        raw = _png(320, 240, noisy=True)
        items = [
            _user_turn("第一轮", _upload_parts(raw)),
            _user_turn("第二轮", _upload_parts(_png(300, 200, noisy=True, seed=2))),
            {
                "type": "function_call_output",
                "call_id": "c9",
                "output": [
                    {"type": "input_text", "text": "[图片 IMG-0001 取回的原图｜第 1 轮]"},
                    {"type": "input_image", "image_url": _data_url(raw)},
                ],
            },
            _user_turn("第三轮", _upload_parts(_png(280, 180, noisy=True, seed=3))),
        ]

        result = self.rewritten(items)

        tool_output = [
            item
            for item in result["input"]
            if item.get("type") == "function_call_output"
        ][0]
        self.assertEqual(
            [part["type"] for part in tool_output["output"]], ["input_text", "input_text"]
        )
        self.assertIn("的原图", tool_output["output"][1]["text"])
        # The turn that fetched it keeps its own image.
        newest = result["input"][-1]
        self.assertTrue(
            any(part.get("type") == "input_image" for part in newest["content"])
        )


class ImageContextFileFetchTests(_ImageContextCase):
    """The fetch that changes nothing in the client: a file and its own viewer."""

    def setUp(self) -> None:
        super().setUp()
        self.thread = "thread-file"
        self.metadata = json.dumps({"thread_id": self.thread, "turn_id": "t1"})

    def aged(self) -> list:
        return [
            _user_turn("第一轮", _upload_parts(_png(120, 90, noisy=True))),
            _user_turn("第二轮"),
            _user_turn("第三轮"),
        ]

    def rewrite_in_path_mode(self) -> dict:
        with mock.patch.dict(
            os.environ, {image_context_module.IMAGE_CONTEXT_FETCH_ENV: "path"}
        ):
            return self.rewritten(
                self.aged(),
                headers={
                    "Originator": "codex_cli_rs",
                    "x-codex-turn-metadata": self.metadata,
                },
            )

    def test_the_placeholder_names_a_file_the_client_can_open(self) -> None:
        result = self.rewrite_in_path_mode()

        text = result["input"][1]["content"][1]["text"]
        self.assertIn("view_image", text)
        self.assertNotIn("view_image_by_id", text)
        path = text.split("“")[1].split("”")[0]
        self.assertTrue(path.endswith("IMG-0001.png"), path)
        self.assertTrue(os.path.isfile(path), path)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read()[:8], b"\x89PNG\r\n\x1a\n")

    def test_the_note_names_the_viewer_and_not_a_server(self) -> None:
        result = self.rewrite_in_path_mode()

        note = [
            item["content"][0]["text"]
            for item in result["input"]
            if item.get("role") == "developer"
        ][0]
        self.assertIn("view_image", note)
        self.assertNotIn("view_image_by_id", note)
        self.assertNotIn("需要 IMG-", note)

    def test_a_host_with_no_client_configuration_promises_nothing(self) -> None:
        # A gateway sees no client configuration, so the only evidence that a
        # tool exists out there is that a client has actually used it.
        with mock.patch.dict(
            os.environ,
            {
                "HOME": os.path.join(self.directory.name, "gateway-home"),
                image_context_module.IMAGE_CONTEXT_FETCH_ENV: "auto",
            },
        ):
            self.assertEqual("ask", image_context_module.fetch_kind())
            image_context_module.mark_endpoint_served()
            self.assertEqual("tool", image_context_module.fetch_kind())

    def test_forgetting_a_task_removes_its_files_too(self) -> None:
        self.rewrite_in_path_mode()
        folder = os.path.dirname(store_module.paths(self.thread)["IMG-0001"])
        self.assertTrue(os.path.isdir(folder))

        self.assertTrue(store_module.forget(self.thread))

        self.assertFalse(os.path.isdir(folder))
        self.assertEqual({}, store_module.paths(self.thread))


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

    def test_an_id_is_found_even_when_the_client_names_no_task(self) -> None:
        # Codex starts an MCP server without telling it which task the model is
        # in, so the id has to be resolvable on its own.
        thread = "dddddddd-0000-1111-2222-333333333333"
        image_context_module._with_codex_image_context(
            _request(
                [_user_turn("first", _upload_parts(_png(4, 4))), _user_turn("second")],
                metadata={"thread_id": thread},
            )
        )

        self.assertIsNotNone(store_module.image_data_url(None, "IMG-0001"))
        self.assertIsNotNone(store_module.image_data_url("another-task", "IMG-0001"))
        self.assertIsNone(store_module.image_data_url(None, "IMG-0404"))

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


class ImageContextDeploymentTests(_ImageContextCase):
    """A gateway on a server has no client to configure, so it promises nothing."""

    def test_auto_serves_http_where_no_client_configuration_exists(self) -> None:
        environment = {"HOME": str(Path(self.directory.name) / "server-home")}

        self.assertEqual(
            image_context_module.fetch_mode(environment), "auto"
        )
        self.assertFalse(image_context_module.fetch_available(environment))
        self.assertTrue(image_context_endpoint.enabled(environment))

    def test_auto_registers_a_stdio_client_where_one_exists(self) -> None:
        home = Path(self.directory.name) / "desktop-home"
        (home / ".codex").mkdir(parents=True)
        environment = {"HOME": str(home)}

        self.assertTrue(image_context_module.fetch_available(environment))
        self.assertFalse(image_context_endpoint.enabled(environment))

    def test_off_promises_no_tool_and_opens_no_port(self) -> None:
        environment = {"YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_FETCH": "off"}

        self.assertFalse(image_context_module.fetch_available(environment))
        self.assertFalse(image_context_endpoint.enabled(environment))

    def test_a_gateway_without_a_tool_asks_the_user_instead(self) -> None:
        items = [
            _user_turn("第一轮", _upload_parts(_png(320, 240, noisy=True))),
            _user_turn("第二轮"),
        ]
        with mock.patch.dict(
            os.environ,
            {
                image_context_module.IMAGE_CONTEXT_FETCH_ENV: "off",
            },
        ):
            result = self.rewritten(items)

        note = [
            item["content"][0]["text"]
            for item in result["input"]
            if item.get("role") == "developer"
        ][0]
        self.assertNotIn("view_image_by_id", note)
        self.assertIn("需要 IMG-", note)
        self.assertIn("需要 IMG-0001", json.dumps(result["input"], ensure_ascii=False))

    def test_the_endpoint_answers_only_with_the_installations_token(self) -> None:
        environment = {"LITELLM_MASTER_KEY": "sk-test"}
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        ).encode("utf-8")

        self.assertTrue(
            image_context_endpoint.authorized("Bearer sk-test", environment)
        )
        self.assertFalse(
            image_context_endpoint.authorized("Bearer sk-other", environment)
        )
        self.assertFalse(image_context_endpoint.authorized(None, environment))
        # No token configured is not an open door.
        self.assertFalse(image_context_endpoint.authorized("Bearer anything", {}))
        status, payload = image_context_endpoint.answer(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["tools"][0]["name"], mcp_module.TOOL_NAME)


class ImageContextStorageTests(_ImageContextCase):
    def test_a_stored_replay_holds_images_and_never_text(self) -> None:
        thread = "dddddddd-eeee-ffff-0000-111111111111"
        items = [
            _user_turn("a secret sentence", _upload_parts(_png(4, 4))),
            _user_turn("another secret sentence"),
        ]

        store_module.store(thread, {"input": items})

        path = os.path.join(store_module.cache_dir(), f"{thread}.json")
        with open(path, encoding="utf-8") as handle:
            written = handle.read()
        self.assertNotIn("secret sentence", written)
        self.assertIn("IMG-0001", written)


class ImageContextDeclineTests(_ImageContextCase):
    """A pass that declines silently is indistinguishable from one that is off."""

    def decline_reasons(self) -> list:
        from young_router.proxy import trace as trace_module

        calls = []
        original = trace_module._route_trace

        def capture(event, **fields):
            if event == "codex_image_context_declined":
                calls.append(fields.get("reason"))
            return original(event, **fields)

        self.enterContext(mock.patch.object(trace_module, "_route_trace", capture))
        return calls

    def test_a_disabled_setting_says_so(self) -> None:
        reasons = self.decline_reasons()
        items = [_user_turn("first", _upload_parts(_png(120, 90, noisy=True))), _user_turn("second")]
        with mock.patch.dict(
            os.environ, {image_context_module.IMAGE_CONTEXT_ENABLED_ENV: "0"}
        ):
            self.assertIsNone(
                image_context_module._with_codex_image_context(_request(items))
            )
        self.assertEqual(reasons, ["setting_disabled"])

    def test_images_only_in_the_current_turn_say_so(self) -> None:
        # The case that produced the bug report: every image belonged to the
        # turn being answered, so no rewrite was legal -- and nothing said that.
        reasons = self.decline_reasons()
        items = [
            _user_turn("earlier"),
            _user_turn("look", _upload_parts(_png(120, 90, noisy=True))),
        ]
        with mock.patch.dict(
            os.environ,
            {
                image_context_module.IMAGE_CONTEXT_ENABLED_ENV: "1",
                image_context_module.IMAGE_CONTEXT_FETCH_ENV: "path",
            },
        ):
            self.assertIsNone(
                image_context_module._with_codex_image_context(_request(items))
            )
        self.assertEqual(reasons, ["no_image_old_enough"])

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
