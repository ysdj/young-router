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
import time
import unittest
import zlib
from pathlib import Path
from unittest import mock

from young_router.proxy import context_window as cw
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
                # These cases are about what retirement does at all, so the
                # window is off here; the window's own size has its own case.
                image_context_module._VISIBLE_PICTURES_ENV: "0",
                image_context_module._VISIBLE_TOKENS_ENV: "0",
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

        self.assertEqual([image.image_id for image in found], ["IMG-001", "IMG-002"])
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
            if part.get("type") == "input_text" and "IMG-002" in part.get("text", "")
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
        self.assertIn("图片 IMG-001 已省略", text)
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
        self.assertIn("IMG-001", result["input"][1]["content"][1]["text"])
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
        # naming it -- the content is never lost, only the bytes.  The copy that
        # stays is the oldest, because the newest one moves forward with every
        # step and every placeholder naming it would have to be rewritten, which
        # is exactly the prefix the upstream would then re-read.
        opening = result["input"][0]["content"]
        self.assertTrue(any(part.get("type") == "input_image" for part in opening))
        self.assertTrue(any("IMG-001" in part.get("text", "") for part in opening))
        duplicate = result["input"][2]["content"][1]["text"]
        self.assertIn("图片 IMG-002 未重复提供", duplicate)
        self.assertIn("IMG-001", duplicate)

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
                "unchecked": {"IMG-001": {"id": "IMG-001"}},
            },
        )
        items = [_user_turn("第一轮", _upload_parts(_png(4, 4))), _user_turn("第二轮")]

        result = self.rewritten(items, metadata={"thread_id": thread})

        replaced = [
            item
            for item in result["input"]
            if item.get("role") == "user" and len(item.get("content") or []) == 2
        ][0]
        self.assertIn("IMG-001", replaced["content"][1]["text"])

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
            {"threadId": thread, "auto": True, "pinned": {"IMG-001": {"id": "IMG-001"}}},
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
                    {"type": "input_text", "text": "[图片 IMG-001 取回的原图｜第 1 轮]"},
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
                ("IMG-001", None),
                ("IMG-002", None),
                ("IMG-001", "IMG-001"),
                ("IMG-003", None),
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
                    {"type": "input_text", "text": "[图片 IMG-001 取回的原图｜第 1 轮]"},
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
        self.assertTrue(path.endswith("IMG-001.png"), path)
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

    def test_a_gateway_asks_for_the_original_until_a_client_fetches(self) -> None:
        # A gateway has no client document to edit, so the only fetch tool it
        # can offer is its own endpoint -- and the endpoint is served from the
        # first request, because a placeholder that waits for a client to have
        # already called the tool it never named can never be answered.
        with mock.patch.dict(
            os.environ,
            {
                "HOME": os.path.join(self.directory.name, "gateway-home"),
                image_context_module.IMAGE_CONTEXT_FETCH_ENV: "auto",
            },
        ):
            # No client has ever called the endpoint, so no tool may be named:
            # the ask is the kind, and this proxy answers that ask itself.
            self.assertEqual("ask", image_context_module.fetch_kind())

            image_context_module.mark_endpoint_served()
            self.assertEqual("tool", image_context_module.fetch_kind())

    def test_a_leftover_client_folder_on_a_gateway_is_not_a_client(self) -> None:
        # Running the Codex CLI once on a server leaves ``~/.codex`` behind with
        # no client document in it.  Reading that as "the client is here" handed
        # a remote model this disk's paths and switched the fetch endpoint off.
        home = Path(self.directory.name) / "gateway-home"
        (home / ".codex" / "generated_images").mkdir(parents=True)
        forwarded = {
            "proxy_server_request": {
                "headers": {"x-real-ip": "203.0.113.7"},
            }
        }

        with mock.patch.dict(
            os.environ,
            {
                "HOME": str(home),
                image_context_module.IMAGE_CONTEXT_FETCH_ENV: "auto",
            },
        ):
            self.assertEqual(
                "ask", image_context_module.fetch_kind(request_kwargs=forwarded)
            )

    def test_a_client_document_on_this_host_keeps_the_file_path(self) -> None:
        home = Path(self.directory.name) / "desktop-home"
        (home / ".codex").mkdir(parents=True)
        (home / ".codex" / "config.toml").write_text("", encoding="utf-8")

        with mock.patch.dict(
            os.environ,
            {
                "HOME": str(home),
                image_context_module.IMAGE_CONTEXT_FETCH_ENV: "auto",
            },
        ):
            self.assertEqual("file", image_context_module.fetch_kind())
            self.assertEqual(
                "file",
                image_context_module.fetch_kind(
                    request_kwargs={
                        "proxy_server_request": {"headers": {"host": "127.0.0.1:4401"}}
                    }
                ),
            )

    def test_a_forwarded_request_never_gets_a_path_from_this_disk(self) -> None:
        # Same deployment, somebody else's reader: the file kind is the only one
        # that must not cross a proxy boundary.  A host that owns a client
        # document does not serve the endpoint (the desktop client's stdio
        # server is the tool there), so with no tool to name the placeholder
        # falls back to asking the user rather than promising this disk.
        home = Path(self.directory.name) / "desktop-home"
        (home / ".codex").mkdir(parents=True)
        (home / ".codex" / "config.toml").write_text("", encoding="utf-8")
        forwarded = {
            "proxy_server_request": {
                "headers": {"x-forwarded-for": "203.0.113.7, 10.0.0.1"},
            }
        }

        with mock.patch.dict(
            os.environ,
            {
                "HOME": str(home),
                image_context_module.IMAGE_CONTEXT_FETCH_ENV: "auto",
            },
        ):
            self.assertEqual(
                "ask", image_context_module.fetch_kind(request_kwargs=forwarded)
            )

    def test_forgetting_a_task_removes_its_files_too(self) -> None:
        self.rewrite_in_path_mode()
        folder = os.path.dirname(store_module.paths(self.thread)["IMG-001"])
        self.assertTrue(os.path.isdir(folder))

        self.assertTrue(store_module.forget(self.thread))

        self.assertFalse(os.path.isdir(folder))
        self.assertEqual({}, store_module.paths(self.thread))

    def test_forgetting_a_task_removes_its_thumbnails_too(self) -> None:
        """A thumbnail is cached per task, so it goes with the task.

        One live task carried 183 of them and the cache keeps 32 tasks: leaving
        them behind is a directory that only ever grows.
        """

        items = [
            _user_turn("first", _upload_parts(_png(1200, 900, noisy=True))),
            _user_turn("second"),
        ]
        gateway_home = os.path.join(self.directory.name, "gateway-home")
        os.makedirs(gateway_home, exist_ok=True)
        with mock.patch.dict(
            os.environ,
            {
                "HOME": gateway_home,
                image_context_module.IMAGE_CONTEXT_FETCH_ENV: "auto",
                image_context_module.IMAGE_CONTEXT_PREVIEW_ENV: "auto",
            },
        ):
            self.rewritten(items, metadata={"thread_id": self.thread})

        folder = os.path.join(store_module.previews_dir(), self.thread)
        self.assertTrue(os.path.isdir(folder), folder)
        self.assertTrue(os.listdir(folder))

        self.assertTrue(store_module.forget(self.thread))

        self.assertFalse(os.path.isdir(folder))


class ImageContextStoreRetentionTests(_ImageContextCase):
    """The store's own day: pictures do not outlive the replay they answer from."""

    def store_task(self, thread: str, seed: int = 1) -> None:
        items = [_user_turn("first", _upload_parts(_png(40, 30, noisy=True, seed=seed)))]
        self.assertEqual(store_module.store(thread, {"input": items}), 1)

    def backdate(self, path: str, seconds: float) -> None:
        moment = time.time() - seconds
        os.utime(path, (moment, moment))

    def test_a_replay_and_its_file_copies_expire_after_a_day(self) -> None:
        self.store_task("old-thread", 1)
        self.store_task("new-thread", 2)
        old_replay = os.path.join(store_module.cache_dir(), "old-thread.json")
        old_folder = os.path.join(store_module.files_dir(), "old-thread")
        self.backdate(old_replay, 25 * 60 * 60)
        self.backdate(old_folder, 25 * 60 * 60)

        removed = store_module.sweep(now=time.time())

        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(old_replay))
        self.assertFalse(os.path.isdir(old_folder))
        # The fresh task is untouched: its pictures can still answer a fetch.
        self.assertTrue(os.path.isfile(os.path.join(store_module.cache_dir(), "new-thread.json")))
        self.assertTrue(os.path.isdir(os.path.join(store_module.files_dir(), "new-thread")))

    def test_the_task_cap_takes_the_file_copies_with_it(self) -> None:
        # Creating the tasks through the store would sweep the cap after every
        # write, so the oldest would be gone before it could be aged in order.
        with mock.patch.object(store_module, "sweep", return_value=0):
            for index in range(store_module.REPLAY_CACHE_MAX_TASKS + 1):
                self.store_task(f"thread-{index:02d}", index + 1)
        oldest_replay = os.path.join(store_module.cache_dir(), "thread-00.json")
        oldest_folder = os.path.join(store_module.files_dir(), "thread-00")
        self.backdate(oldest_replay, 60 * 60)
        self.backdate(oldest_folder, 60 * 60)

        store_module.sweep(now=time.time())

        self.assertFalse(os.path.exists(oldest_replay))
        self.assertFalse(os.path.isdir(oldest_folder))
        self.assertEqual(
            len([name for name in os.listdir(store_module.cache_dir()) if name.endswith(".json")]),
            store_module.REPLAY_CACHE_MAX_TASKS,
        )

    def test_file_copies_without_a_replay_are_dropped(self) -> None:
        orphan = os.path.join(store_module.files_dir(), "orphan-thread")
        os.makedirs(orphan)
        with open(os.path.join(orphan, "IMG-001.png"), "wb") as handle:
            handle.write(b"png")

        store_module.sweep(now=time.time())

        self.assertFalse(os.path.isdir(orphan))

    def test_reading_a_stale_replay_refuses_to_answer(self) -> None:
        self.store_task("old-thread", 1)
        replay = os.path.join(store_module.cache_dir(), "old-thread.json")
        with open(replay, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["at"] = time.time() - 25 * 60 * 60
        with open(replay, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

        self.assertEqual([], store_module.load("old-thread"))
        # Refusing is not deleting: the sweeper owns the file, and a concurrent
        # write at the same path may have just replaced it.
        self.assertTrue(os.path.exists(replay))


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

        answer = store_module.image_data_url(thread, "IMG-001")
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
                        "arguments": {"ids": ["IMG-001"]},
                    },
                }
            )

        blocks = answer["result"]["content"]  # type: ignore[index]
        self.assertEqual([block["type"] for block in blocks], ["text", "image"])
        self.assertEqual(blocks[1]["mimeType"], "image/png")

    def test_a_fetched_copy_keeps_the_id_its_line_names(self) -> None:
        """The fetch tool's own line is what makes an id mean one picture.

        The pass recognises a copy by reading the line in front of it.  This
        tool used to answer with a line of its own (``[Image IMG-001 | provided
        this turn]``), which that reader does not match: the picture the model
        had just asked for came back as a new occurrence, took the next number,
        and the same image answered two ids for the rest of the task -- so a
        panel selection made on the id the model was told could no longer name
        it.  The line is now the pass's own heading for a fetched image.
        """

        thread = "eeeeeeee-1111-2222-3333-444444444444"
        uploads = _png(120, 90, noisy=True, seed=7)
        first = [
            _user_turn("first", _upload_parts(uploads)),
            _user_turn("second"),
        ]
        image_context_module._with_codex_image_context(
            _request(first, metadata={"thread_id": thread})
        )
        with mock.patch.dict(os.environ, {mcp_module.THREAD_ENV: thread}):
            answer = mcp_module.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": mcp_module.TOOL_NAME,
                        "arguments": {"ids": ["IMG-001"]},
                    },
                }
            )
        line = answer["result"]["content"][0]["text"]  # type: ignore[index]
        self.assertRegex(line, r"^\[(?:图片|Image) IMG-001 (?:取回的原图|fetched original)")
        self.assertIsNotNone(image_context_module._FETCHED_LINE.match(line))

        # Codex carries that call's result into the next replay, exactly as the
        # tool returned it.
        answered = {
            "type": "function_call_output",
            "call_id": "fetch-1",
            "output": [
                {"type": "input_text", "text": line},
                {
                    "type": "input_image",
                    "image_url": answer["result"]["content"][1]["data"],  # type: ignore[index]
                },
            ],
        }
        answered["output"][1]["image_url"] = _data_url(
            base64.b64decode(answered["output"][1]["image_url"])
        )
        replay = first + [answered]
        carried = image_context_module.scan(replay)

        self.assertEqual([image.image_id for image in carried], ["IMG-001", "IMG-001"])
        self.assertEqual(carried[-1].copy_of, "IMG-001")

    def test_a_picture_the_client_viewer_reopened_keeps_its_id(self) -> None:
        """The other way a copy comes back: the client's own viewer, on our file.

        Path mode hands the model a file this pass wrote, and the client's own
        ``view_image`` result carries the image and nothing else -- no tag, no
        id (checked against every such pair in this machine's rollouts).  Read
        as a new occurrence, the picture the model just reopened took the next
        number: one image answered two ids, and the same bytes were cached twice
        under two names.  The id lives on the call, which names the path.
        """

        thread = "abcdefab-1111-2222-3333-444444444444"
        raw = _png(120, 90, noisy=True, seed=5)
        first = [_user_turn("first", _upload_parts(raw)), _user_turn("second")]
        with mock.patch.dict(
            os.environ, {image_context_module.IMAGE_CONTEXT_FETCH_ENV: "path"}
        ):
            self.rewritten(first, metadata={"thread_id": thread})
            stored = store_module.paths(thread)["IMG-001"]
            reopened = [
                {
                    "type": "function_call",
                    "name": "view_image",
                    "namespace": "functions",
                    "call_id": "call_view_1",
                    "arguments": json.dumps({"path": stored, "detail": "original"}),
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_view_1",
                    "output": [{"type": "input_image", "image_url": _data_url(raw)}],
                },
            ]
            replay = first + [_assistant("ok"), _user_turn("third")] + reopened

            found = image_context_module.scan(replay, file_paths=store_module.paths(thread))
            self.assertEqual([image.image_id for image in found], ["IMG-001", "IMG-001"])
            self.assertEqual(found[-1].copy_of, "IMG-001")

            result = self.rewritten(replay, metadata={"thread_id": thread})
            wire = json.dumps(result["input"], ensure_ascii=False)
            self.assertIn("IMG-001", wire)
            self.assertNotIn("IMG-002", wire)
            self.assertEqual(
                [os.path.basename(path) for path in store_module.paths(thread).values()],
                ["IMG-001.png"],
            )

    def test_a_viewer_call_for_a_file_this_pass_did_not_write_is_a_new_picture(self) -> None:
        """Only our own files carry an id; anything else is its own picture."""

        stored = "/Users/somebody/.young-router/image-context/replays/files/t/IMG-001.png"
        replay = [
            _user_turn("first", _upload_parts(_png(4, 4))),
            {
                "type": "function_call",
                "name": "view_image",
                "call_id": "call_view_2",
                "arguments": json.dumps({"path": "/tmp/somebody-else.png"}),
            },
            {
                "type": "function_call_output",
                "call_id": "call_view_2",
                "output": [{"type": "input_image", "image_url": _data_url(_png(120, 90, noisy=True, seed=9))}],
            },
        ]

        found = image_context_module.scan(replay, file_paths={"IMG-001": stored})

        self.assertEqual([image.image_id for image in found], ["IMG-001", "IMG-002"])
        self.assertEqual(found[-1].copy_of, None)

    def test_the_fetched_line_speaks_the_tasks_own_language(self) -> None:
        """The copy's heading reads in the language its placeholders were in.

        The fetch tool is a separate process, so it reads the task's language
        from the stored replay instead of guessing one.
        """

        thread = "ffffffff-1111-2222-3333-444444444444"
        image_context_module._with_codex_image_context(
            _request(
                [
                    _user_turn("第一轮", _upload_parts(_png(120, 90, noisy=True))),
                    _user_turn("第二轮"),
                ],
                metadata={"thread_id": thread},
            )
        )

        record, lang, _mime, _data = store_module.fetched(thread, "IMG-001")

        self.assertEqual(lang, "zh")
        self.assertEqual(record["id"], "IMG-001")
        self.assertIn(
            "取回的原图",
            image_context_module.fetched_label(
                "IMG-001",
                lang=lang,
                name=record.get("name"),
                kind=record.get("kind"),
                turn=record.get("turn"),
                width=record.get("width"),
                height=record.get("height"),
            ),
        )

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

        self.assertIsNotNone(store_module.image_data_url(None, "IMG-001"))
        self.assertIsNotNone(store_module.image_data_url("another-task", "IMG-001"))
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
        # The endpoint is served from the first request and every placeholder
        # names it: a gateway that waited for a client to call a tool it had
        # never named never got a single fetch.
        self.assertTrue(image_context_endpoint.enabled(environment))
        # ``ask`` is a kind this proxy answers itself, so a fetch is available
        # even though no tool can be named yet.
        self.assertEqual("ask", image_context_module.fetch_kind(environment))

    def test_auto_registers_a_stdio_client_where_one_exists(self) -> None:
        home = Path(self.directory.name) / "desktop-home"
        (home / ".codex").mkdir(parents=True)
        (home / ".codex" / "config.toml").write_text("", encoding="utf-8")
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
        self.assertIn("需要 IMG-001", json.dumps(result["input"], ensure_ascii=False))

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
        self.assertIn("IMG-001", written)


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

class ImageContextSizeModeTests(_ImageContextCase):
    """A turn-only pass cannot shorten a task, and a Codex task is one turn.

    Real thread ``01a19000``: one user message, then ~800 items of work, every
    ``view_image`` result of it staying inside that same turn.  The replay
    reached 50.4 MB of base64 pictures, WorkBuddy refused it with its own
    ``prompt is too long`` after 29 s, and both size repairs declined for the
    same reason -- the pictures were not old enough, because the only turn was
    the current one.  The turn ended as "Codex ran out of room".
    """

    def one_turn_task(self, pictures: int) -> list:
        items: list = [_user_turn("look at these and report")]
        for index in range(pictures):
            items.append(
                {
                    "type": "function_call",
                    "call_id": f"call_{index}",
                    "name": "view_image",
                    "arguments": "{}",
                }
            )
            items.append(
                _tool_image(
                    _png(120, 90, noisy=True, seed=index + 1), call_id=f"call_{index}"
                )
            )
            items.append(_assistant(f"page {index} looks fine"))
        return items

    def test_the_kept_picture_is_the_same_one_on_every_step(self) -> None:
        # The cache is the reason this is not "the newest picture": keeping the
        # newest moves the kept item forward on every step, which rewrites an
        # item already on the wire and makes the upstream re-read everything
        # after it.  Measured on a real 20-image replay, that cost 14% of the
        # prefix on every step that added a result.
        kept = []
        for pictures in (2, 3, 4, 5):
            items = self.one_turn_task(pictures)
            result = self.rewritten(items)
            kept.append([image.image_id for image in image_context_module.scan(result["input"])])
        self.assertEqual(kept[0], kept[-1])
        # One result of the turn is still sent, so the model is never left with
        # nothing but placeholders for the pages it just walked.
        self.assertEqual(len(kept[-1]), 1)

    def test_a_new_result_never_rewrites_the_ones_already_sent(self) -> None:
        # Every item a previous step already put on the wire must come back
        # byte-identical, or the prefix after it is read again.
        def wire(pictures: int) -> list:
            return self.rewritten(self.one_turn_task(pictures))["input"]

        previous = wire(3)
        current = wire(4)
        for index, item in enumerate(previous):
            self.assertEqual(item, current[index], f"item {index} drifted")

    def batch_of_views(self, pictures: int) -> list:
        """One step of a task that renders a dozen pages and looks at them all.

        A step that asks for many pictures puts every ``function_call`` first and
        the results after them, all of them after the last thing the assistant
        said -- so no earlier request of this turn can have carried their bytes,
        and the model is looking at every one of them for the first time.
        """
        items: list = [_user_turn("look at these and report")]
        for index in range(pictures):
            items.append(
                {
                    "type": "function_call",
                    "call_id": f"call_{index}",
                    "name": "view_image",
                    "arguments": "{}",
                }
            )
        for index in range(pictures):
            items.append(
                _tool_image(
                    _png(120, 90, noisy=True, seed=index + 1), call_id=f"call_{index}"
                )
            )
        return items

    def test_the_turn_keeps_its_last_pictures_in_front_of_the_model(self) -> None:
        # Retiring a picture on the next request made the model work from
        # placeholders: real thread ``01a19633`` said "we have many images as
        # placeholders", stitched contact sheets of its own and asked for one
        # page per step.  A window keeps the last pictures of the turn visible.
        items = self.batch_of_views(14)
        items.append(_assistant("page 1 is a cover"))
        items.append(
            {
                "type": "function_call",
                "call_id": "next",
                "name": "exec_command",
                "arguments": "{}",
            }
        )
        items.append(_tool_image(_png(120, 90, noisy=True, seed=999), call_id="next"))
        with mock.patch.dict(
            os.environ,
            {
                image_context_module._VISIBLE_PICTURES_ENV: "24",
                image_context_module._VISIBLE_TOKENS_ENV: "1000",
            },
        ):
            result = self.rewritten(items)
        visible = len(image_context_module.scan(result["input"]))
        # Ten newest of the turn, plus the turn's first picture as the anchor.
        self.assertEqual(visible, 11)
        # The oldest picture stays as well: it is the anchor every later request
        # shares a prefix with, so retiring it would rewrite the whole request.
        wire = json.dumps(result["input"], ensure_ascii=False)
        self.assertIn("IMG-001 provided", wire)
        self.assertIn("IMG-015 provided", wire)
        self.assertIn("IMG-002 omitted", wire)

    def test_a_batch_of_views_reaches_the_model_whole(self) -> None:
        # Real thread ``01a195f0``: a step that asked for several pages at once
        # had all but one replaced by placeholders on the very request that
        # carried them, so the model - which had never seen any of them -
        # fetched them again one at a time.  A many-picture step became many
        # single-picture steps, and the task dribbled through the work.
        for pictures in (3, 12):
            items = self.batch_of_views(pictures)
            result = image_context_module._with_codex_image_context(
                _request(items)
            )
            # A pass with nothing to say leaves the request untouched, which is
            # the outcome this case wants: every picture of the batch still
            # travels as its own bytes.
            carried = (result or {}).get("input", items)
            self.assertEqual(len(image_context_module.scan(carried)), pictures)

    def test_a_view_the_model_has_already_seen_may_become_a_placeholder(self) -> None:
        # The point of the pass survives the rule above: once the assistant has
        # answered after those pictures, they are ones the model has seen, and
        # the next request may name them instead of re-sending them.
        items = self.batch_of_views(4)
        items.append(_assistant("page 1 is a cover"))
        items.append(
            {
                "type": "function_call",
                "call_id": "next",
                "name": "view_image",
                "arguments": "{}",
            }
        )
        items.append(_tool_image(_png(120, 90, noisy=True, seed=99), call_id="next"))
        result = self.rewritten(items)
        carried = image_context_module.scan(result["input"])
        # The turn's first picture (the cache anchor) and the one it just asked
        # for; the four it has already read are named instead.  Scanning the
        # result renumbers what survived, so the count is what can be asserted.
        self.assertEqual(len(carried), 2)
        self.assertEqual(carried[-1].raw, image_context_module.scan(items)[-1].raw)
        wire = json.dumps(result["input"], ensure_ascii=False)
        for named in ("IMG-002", "IMG-003", "IMG-004"):
            self.assertIn(f"{named} omitted", wire)
        self.assertIn("IMG-005 provided", wire)

    def test_size_mode_omits_pictures_older_than_the_newest_result(self) -> None:
        items = self.one_turn_task(4)
        before = cw._inline_image_bytes(items)

        result = image_context_module.rewrite_for_size(_request(items), keep=0)

        self.assertIsNotNone(result)
        assert result is not None
        after = cw._inline_image_bytes(result["input"])
        self.assertLess(after, before / 2)
        # The newest result's picture is the one the model just asked to see.
        self.assertEqual(after, cw._inline_image_bytes([items[-2]]))
        # Every omitted picture is still named, so it can be fetched back.
        self.assertIn("IMG-001", json.dumps(result["input"], ensure_ascii=False))

    def test_the_setting_reaches_the_turn_being_answered(self) -> None:
        # The task that produced this pass's own bug report is one user message
        # followed by hundreds of steps: with the pass stopping at the turn
        # boundary it never ran on that thread at all, and every step of the
        # task re-uploaded the same pictures.
        items = self.one_turn_task(4)
        before = cw._inline_image_bytes(items)

        result = self.rewritten(items)

        after = cw._inline_image_bytes(result["input"])
        self.assertLess(after, before / 2)
        # The newest result's picture is the one the model just asked to see.
        self.assertEqual(after, cw._inline_image_bytes([items[-2]]))
        self.assertIn("IMG-001", json.dumps(result["input"], ensure_ascii=False))

    def test_the_turns_own_upload_is_never_touched(self) -> None:
        # The picture the user attached is what the task is about, and the note
        # says it is provided as usual; a placeholder for it on the first
        # request would be an answer about an image the model never saw.
        upload = _png(120, 90, noisy=True, seed=9)
        items = [_user_turn("look at this and at the pages", _upload_parts(upload))]
        for index in range(3):
            items.append(
                {
                    "type": "function_call",
                    "call_id": f"call_{index}",
                    "name": "view_image",
                    "arguments": "{}",
                }
            )
            items.append(
                _tool_image(
                    _png(120, 90, noisy=True, seed=index + 1), call_id=f"call_{index}"
                )
            )

        result = self.rewritten(items)

        # The developer note precedes the turn, so the upload's own message is
        # the first user item after it.
        opening = next(
            item
            for item in result["input"]
            if item.get("type") == "message" and item.get("role") == "user"
        )
        uploads = [
            part for part in opening["content"] if part.get("type") == "input_image"
        ]
        self.assertEqual(uploads, [items[0]["content"][2]])
        # Its own id label is inserted before it, so a placeholder elsewhere can
        # be matched to the picture in front of the model.
        self.assertTrue(
            any("IMG-001" in part.get("text", "") for part in opening["content"])
        )
        # The older tool results are what was left out.
        self.assertIn("IMG-002", json.dumps(result["input"], ensure_ascii=False))

    def test_a_lone_tool_picture_is_kept_because_nothing_is_newer(self) -> None:
        items = self.one_turn_task(1)

        self.assertIsNone(
            image_context_module._with_codex_image_context(_request(items))
        )

    def test_history_mode_keeps_the_turn_being_answered_whole(self) -> None:
        items = self.one_turn_task(4)
        with mock.patch.dict(
            os.environ,
            {image_context_module.IMAGE_CONTEXT_INSIDE_TURN_ENV: "history"},
        ):
            self.assertIsNone(
                image_context_module._with_codex_image_context(_request(items))
            )

    def test_size_mode_frees_nothing_from_a_single_picture(self) -> None:
        items = [_user_turn("look")] + self.one_turn_task(1)[1:]

        self.assertIsNone(
            image_context_module.rewrite_for_size(_request(items), keep=0)
        )

if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class ImageContextPreviewTests(_ImageContextCase):
    """What a reader sees when its client has no fetch tool at all.

    A gateway cannot edit the client's MCP configuration, so a placeholder there
    named a tool the model could not call: three hours of one live task omitted
    73 images per request and fetched none.  A bounded thumbnail keeps the picture
    visible at a fraction of the bytes, which is the only path that needs no
    client change.
    """

    def setUp(self) -> None:
        super().setUp()
        self.environment.stop()
        # A gateway: no client documents on this host, so the fetch kind is the
        # endpoint nobody has configured a client to call.
        self.gateway_home = os.path.join(self.directory.name, "gateway-home")
        os.makedirs(self.gateway_home, exist_ok=True)
        self.environment.start()
        # After the base env is applied: its own update would otherwise win.
        os.environ["HOME"] = self.gateway_home
        os.environ[image_context_module.IMAGE_CONTEXT_FETCH_ENV] = "auto"
        os.environ[image_context_module.IMAGE_CONTEXT_PREVIEW_ENV] = "auto"  # opt in: the default is id

    def aged_replay(self) -> list:
        return [
            _user_turn("第一轮", _upload_parts(_png(1200, 900, noisy=True))),
            _assistant("看过了"),
            _tool_image(_png(1000, 800, noisy=True, seed=7)),
            _user_turn("第二轮"),
        ]

    def images_in(self, result: dict) -> list:
        found = []
        for item in result["input"]:
            for part in item.get("content") or item.get("output") or []:
                if isinstance(part, dict) and part.get("type") == "input_image":
                    found.append(part)
        return found

    def test_a_gateway_sends_a_thumbnail_instead_of_nothing(self) -> None:
        result = self.rewritten(self.aged_replay())

        images = self.images_in(result)
        self.assertTrue(images, "no thumbnail was attached")
        url = images[0]["image_url"]
        self.assertTrue(url.startswith("data:image/jpeg;base64,"), url[:32])
        # A thumbnail, not the original: well under the uploaded bytes.
        self.assertLess(len(url), len(base64.b64encode(_png(1200, 900, noisy=True))))

    def test_the_caption_still_names_the_id_and_says_it_is_a_thumbnail(self) -> None:
        result = self.rewritten(self.aged_replay())

        texts = [
            part.get("text") or ""
            for item in result["input"]
            for part in (item.get("content") or [])
            if isinstance(part, dict) and part.get("type") == "input_text"
        ]
        joined = "\n".join(texts)
        self.assertIn("IMG-001", joined)
        self.assertIn("此处为缩略图", joined)
        # The caption names the ask, not a tool: this reader has none.
        self.assertIn("需要 IMG-001", joined)
        self.assertNotIn("view_image_by_id", joined)

    def test_the_note_says_the_thumbnails_are_there(self) -> None:
        result = self.rewritten(self.aged_replay())

        notes = [
            item["content"][0]["text"]
            for item in result["input"]
            if item.get("role") == "developer"
        ]
        self.assertTrue(any("缩略图" in note for note in notes), notes)

    def test_a_desktop_client_keeps_the_path_and_no_thumbnail(self) -> None:
        # The client here can open a path itself, so a second copy of the same
        # picture would only cost bytes.
        home = Path(self.directory.name) / "desktop-home"
        (home / ".codex").mkdir(parents=True)
        (home / ".codex" / "config.toml").write_text("", encoding="utf-8")
        os.environ["HOME"] = str(home)
        os.environ[image_context_module.IMAGE_CONTEXT_FETCH_ENV] = "auto"
        result = self.rewritten(self.aged_replay())

        self.assertEqual([], self.images_in(result))

    def test_off_keeps_the_bare_placeholder(self) -> None:
        os.environ[image_context_module.IMAGE_CONTEXT_PREVIEW_ENV] = "off"

        result = self.rewritten(self.aged_replay())

        self.assertEqual([], self.images_in(result))

    def test_the_budget_bounds_how_many_travel(self) -> None:
        os.environ[image_context_module.IMAGE_CONTEXT_PREVIEW_TOKENS_ENV] = "500"

        result = self.rewritten(self.aged_replay())

        # One 512px thumbnail is ~400 tokens, so a 500-token budget allows one.
        self.assertLessEqual(len(self.images_in(result)), 1)

    def test_a_picture_that_travelled_once_keeps_travelling(self) -> None:
        """The set must not move as the replay grows.

        A thumbnail that appears or disappears between turns rewrites an item the
        upstream has already read, and every step re-reads the bytes after it.
        """

        first = self.rewritten(self.aged_replay())
        first_urls = [image["image_url"] for image in self.images_in(first)]
        self.assertTrue(first_urls)

        grown = self.aged_replay() + [
            _assistant("又一个结果"),
            _tool_image(_png(1000, 800, noisy=True, seed=11)),
            _user_turn("第三轮"),
        ]
        second = self.rewritten(grown)
        second_urls = [image["image_url"] for image in self.images_in(second)]

        # Everything that travelled before still travels, with the same bytes.
        self.assertEqual(first_urls[: len(second_urls)], second_urls[: len(first_urls)])


class ImageContextIdFetchTests(_ImageContextCase):
    """The fetch that needs no tool: the model writes the id, the next turn has it.

    A gateway cannot edit the client's MCP configuration, and a thumbnail costs
    bytes on every turn. A placeholder's id is enough if this proxy reads the
    model's own "需要 IMG-004" back out of its newest message and puts that
    original in the next request -- one turn later, at full size, and only when
    it was actually asked for.
    """

    def setUp(self) -> None:
        super().setUp()
        self.environment.stop()
        self.gateway_home = os.path.join(self.directory.name, "gateway-home")
        os.makedirs(self.gateway_home, exist_ok=True)
        self.environment.start()
        os.environ["HOME"] = self.gateway_home
        os.environ[image_context_module.IMAGE_CONTEXT_FETCH_ENV] = "auto"
        os.environ[image_context_module.IMAGE_CONTEXT_PREVIEW_ENV] = "id"

    def payloads(self, result: dict) -> dict:
        """``id -> (mime, bytes)`` for every image the request carries."""

        found = {}
        for item in result["input"]:
            for part in item.get("content") or item.get("output") or []:
                if isinstance(part, dict) and part.get("type") == "input_image":
                    url = part.get("image_url") or ""
                    header, _, data = url.partition(",")
                    found.setdefault(header, []).append(len(data) * 3 // 4)
        return found

    def image_bytes(self, result: dict) -> list:
        sizes = []
        for item in result["input"]:
            for part in item.get("content") or item.get("output") or []:
                if isinstance(part, dict) and part.get("type") == "input_image":
                    sizes.append(len(part.get("image_url") or ""))
        return sizes

    def replay(self, ask: str | None) -> list:
        items = [
            _user_turn("第一轮", _upload_parts(_png(900, 700, noisy=True))),
            _assistant("看过这页了"),
            _tool_image(_png(1000, 800, noisy=True, seed=5)),
        ]
        if ask is not None:
            items.append(_assistant(ask))
        items.append(_user_turn("继续"))
        return items

    def test_an_id_the_model_asked_for_comes_back_at_full_size(self) -> None:
        quiet = self.rewritten(self.replay(None))
        self.assertEqual([], self.image_bytes(quiet), "nothing should travel yet")

        asked = self.rewritten(self.replay("我还想再看一遍。\n需要 IMG-002"))
        sizes = self.image_bytes(asked)
        self.assertEqual(1, len(sizes), sizes)
        # Full size, not a thumbnail: the original is the answer to the question.
        self.assertGreater(sizes[0], 200_000)

    def test_the_picture_carries_its_id_so_the_model_knows_which_it_got(self) -> None:
        asked = self.rewritten(self.replay("需要 IMG-002"))

        labels = [
            part.get("text") or ""
            for item in asked["input"]
            for part in (item.get("content") or item.get("output") or [])
            if isinstance(part, dict) and part.get("type") == "input_text"
        ]
        self.assertTrue(any("IMG-002" in label and "已提供" in label for label in labels), labels)

    def test_an_older_ask_is_not_honoured_again(self) -> None:
        # The ask lives in the assistant message; a later answer of the model's
        # own is newer, so the original is not re-sent for ever.
        items = [
            _user_turn("第一轮", _upload_parts(_png(900, 700, noisy=True))),
            _assistant("需要 IMG-001"),
            _user_turn("第二轮"),
            _assistant("这是我的结论"),
            _user_turn("继续"),
        ]

        result = self.rewritten(items)

        self.assertEqual([], self.image_bytes(result))

    def test_only_the_ids_asked_for_travel_and_only_a_few(self) -> None:
        items = [_user_turn("第一轮", _upload_parts(_png(900, 700, noisy=True)))]
        for seed in range(3, 10):
            items.extend([_assistant("ok"), _tool_image(_png(1000, 800, noisy=True, seed=seed))])
        items.pop()
        items.append(_assistant("需要 IMG-001、IMG-002、IMG-003、IMG-004、IMG-005、IMG-006"))
        items.append(_user_turn("继续"))

        result = self.rewritten(items)

        self.assertEqual(4, len(self.image_bytes(result)))

    def test_every_mirrored_sentence_is_still_the_upstream_one(self) -> None:
        """The fetch must not cost a word of prompt.

        These sentences are copied verbatim from Context Attachment Manager
        because the model was tuned against them.  New delivery (an id the router
        answers itself) must not edit one, and must not name a tool the client
        does not have.
        """

        words = image_context_module._WORDS
        zh, en = words[image_context_module._LANG_ZH], words[image_context_module._LANG_EN]

        self.assertEqual("已省略", zh["omitted"])
        self.assertEqual("已提供", zh["included"])
        self.assertEqual("未重复提供", zh["not_repeated"])
        self.assertEqual("omitted", en["omitted"])
        self.assertEqual("provided", en["included"])
        self.assertEqual("not repeated", en["not_repeated"])
        self.assertTrue(zh["plain"]("IMG-007").startswith("需要重看时，在回复里写“需要 IMG-007”"))
        self.assertIn("需要原图时调用 view_image_by_id", zh["auto_plain"]("IMG-007"))
        self.assertIn("需要原图时调用 view_image", zh["file_plain"]("p.png"))
        self.assertTrue(en["plain"]("IMG-007").startswith('To look again, reply "need IMG-007"'))
        self.assertIn('call view_image_by_id with "IMG-007"', en["auto_plain"]("IMG-007"))
        self.assertIn("上下文管理说明", zh["auto_ask_note"])
        self.assertIn("需要 IMG-xxx", zh["auto_ask_note"])
        self.assertIn("that is how the user knows which one to send again", en["auto_ask_note"])

    def test_the_prompt_is_the_mirrored_one_and_no_tool_is_named(self) -> None:
        """The fetch works without a word of new prompt.

        ``ask`` is the wording the upstream already ships for a host with no
        tool: it tells the model to write "需要 IMG-xxx".  This proxy answers
        that ask itself, so nothing about the sentence has to change -- and it
        must not start naming a tool the client cannot call.
        """

        result = self.rewritten(self.replay(None))

        notes = [
            item["content"][0]["text"]
            for item in result["input"]
            if item.get("role") == "developer"
        ]
        self.assertEqual(
            [image_context_module._WORDS[image_context_module._LANG_ZH]["auto_ask_note"]],
            notes,
        )
        self.assertNotIn("view_image_by_id", notes[0])
        self.assertIn("“需要 IMG-xxx”", notes[0])
