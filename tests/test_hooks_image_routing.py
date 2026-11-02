from __future__ import annotations

import ast
import base64

from hook_test_utils import *


class HookImageRoutingTests(HookTestCase):
    def test_image_generation_module_contains_only_imggen_behavior(self) -> None:
        tree = ast.parse((ROOT / "young_router" / "proxy" / "image_generation.py").read_text(encoding="utf-8"))
        definitions = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]

        self.assertTrue(definitions)
        self.assertTrue(all("image_generation" in name for name in definitions), definitions)

    def test_image_generation_result_validation_rejects_text_and_accepts_png(self) -> None:
        hooks, _ = load_hook_module()

        self.assertFalse(hooks._image_generation_result_is_valid("base64-image"))
        self.assertTrue(hooks._image_generation_result_is_valid(VALID_IMAGE_RESULT))

    def test_image_generation_stream_materializes_png_and_attaches_saved_path(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as codex_home:
            self.set_env("CODEX_HOME", codex_home)
            item = {
                "id": "ig_fixture",
                "type": "image_generation_call",
                "status": "completed",
                "result": VALID_IMAGE_RESULT,
            }
            delivered = hooks._image_generation_stream_chunks_for_delivery(
                item,
                {"client_metadata": {"headers": {"session-id": "thread-fixture"}}},
            )
            self.assertEqual(delivered[0]["type"], "image_generation_call")
            saved_path = Path(delivered[0]["saved_path"])
            self.assertEqual(
                saved_path,
                Path(codex_home) / "generated_images" / "thread-fixture" / "ig_fixture.png",
            )
            self.assertTrue(saved_path.is_file())
            self.assertEqual(saved_path.read_bytes(), base64.b64decode(VALID_IMAGE_RESULT))
            self.assertEqual(delivered[0]["saved_path"], str(saved_path))

    def test_image_generation_stream_restates_the_opening_frame_a_done_only_route_omitted(self) -> None:
        """A done-only image item draws nothing until its opening frame exists.

        Codex opens an output item on ``response.output_item.added`` and renders
        the picture into that block.  A relay that answers the whole generation
        in one read sent only ``response.output_item.done``: the rollout kept the
        completed call, the transcript stayed blank, and ``task_complete``
        carried no message.  Observed live 2026-11-02 on ``gpt-image-2.5-flare``.
        """

        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as codex_home:
            self.set_env("CODEX_HOME", codex_home)
            opened: set[str] = set()
            frame = (
                "data: "
                + json.dumps(
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "sequence_number": 7,
                        "item": {
                            "id": "ig_done_only",
                            "type": "image_generation_call",
                            "status": "completed",
                            "result": VALID_IMAGE_RESULT,
                        },
                    }
                )
                + "\n\n"
            )
            delivered = hooks._image_generation_stream_text_chunk_for_delivery(
                frame,
                {"call_type": "aresponses"},
                [],
                opened,
            )
            payloads = [
                json.loads(line[len("data: "):])
                for line in delivered.splitlines()
                if line.startswith("data: ")
            ]

            self.assertEqual(
                ["response.output_item.added", "response.output_item.done"],
                [payload["type"] for payload in payloads],
            )
            opening, done = payloads
            self.assertEqual("in_progress", opening["item"]["status"])
            self.assertNotIn("result", opening["item"])
            self.assertEqual("completed", done["item"]["status"])
            # A sequence number is only valid in non-decreasing order, so the
            # restored frame reuses the one the item already carried.
            self.assertEqual(7, opening["sequence_number"])
            self.assertEqual(7, done["sequence_number"])

            # One item is opened once: a replayed read adds no second block.
            again = hooks._image_generation_stream_text_chunk_for_delivery(
                frame,
                {"call_type": "aresponses"},
                [],
                opened,
            )
            self.assertEqual(
                ["response.output_item.done"],
                [
                    json.loads(line[len("data: "):])["type"]
                    for line in again.splitlines()
                    if line.startswith("data: ")
                ],
            )

    def test_image_generation_stream_keeps_a_real_opening_frame(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as codex_home:
            self.set_env("CODEX_HOME", codex_home)
            opened: set[str] = set()
            chunk = {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"id": "ig_streamed", "type": "image_generation_call", "status": "in_progress"},
            }
            delivered = hooks._image_generation_stream_chunks_for_delivery(
                chunk,
                {"call_type": "aresponses"},
                [],
                opened,
            )
            self.assertEqual([chunk], delivered)

            done = {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": "ig_streamed",
                    "type": "image_generation_call",
                    "status": "completed",
                    "result": VALID_IMAGE_RESULT,
                },
            }
            delivered_done = hooks._image_generation_stream_chunks_for_delivery(
                done,
                {"call_type": "aresponses"},
                [],
                opened,
            )
            # The route already opened this item, so the delivery path leaves it
            # alone instead of drawing a second block.
            self.assertEqual(
                ["response.output_item.added", "response.output_item.done"],
                [item["type"] for item in delivered_done],
            )

    def test_a_stream_chunk_nothing_rewrote_keeps_its_own_wire_form(self) -> None:
        """An untouched chunk must stay the object LiteLLM serializes.

        LiteLLM writes a decoded stream chunk as ``data: {chunk}``.  A plain
        dict there reaches the client as a Python repr instead of JSON, so a
        ``/v1/chat/completions`` stream becomes unparseable the moment the
        delivery pass dumps every chunk it sees.  The pass may only replace a
        chunk it actually rewrote.
        """

        hooks, _ = load_hook_module()

        class DecodedChatChunk:
            """A BaseModel-shaped chunk, as LiteLLM hands one to the hook."""

            def __init__(self) -> None:
                self.payload = {
                    "id": "chatcmpl-proof",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {"content": "hi"}}],
                }

            def model_dump(self) -> dict:
                return dict(self.payload)

        chunk = DecodedChatChunk()

        delivered = hooks._image_generation_stream_chunks_for_delivery(
            chunk,
            {},
            [],
        )

        self.assertEqual([chunk], delivered)
        self.assertIs(chunk, delivered[0])

    def test_image_generation_stream_restates_the_opening_frame_without_client_evidence(self) -> None:
        """The path is this app's addition; the opening frame is not.

        ``codex exec`` proved no client evidence, so the saved path and its
        markdown reference stay off -- but the completion still arrived without
        its opening frame, and the turn finished blank while the rollout held
        the image.  Observed live 2026-11-02 on ``gpt-image-2.5-flare``.
        """

        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as codex_home:
            self.set_env("CODEX_HOME", codex_home)
            delivered = hooks._image_generation_stream_chunks_for_delivery(
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "id": "ig_no_evidence",
                        "type": "image_generation_call",
                        "status": "completed",
                        "result": VALID_IMAGE_RESULT,
                    },
                },
                {"call_type": "aresponses"},
                [],
                set(),
                attach_paths=False,
            )
            self.assertEqual(
                ["response.output_item.added", "response.output_item.done"],
                [item["type"] for item in delivered],
            )
            self.assertEqual("in_progress", delivered[0]["item"]["status"])
            self.assertNotIn("result", delivered[0]["item"])
            # Nothing of this app's own reached the frame.
            self.assertNotIn("saved_path", delivered[-1]["item"])
            self.assertFalse((Path(codex_home) / "generated_images").exists())

    def test_image_generation_stream_does_not_emit_path_for_invalid_result(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as codex_home:
            self.set_env("CODEX_HOME", codex_home)
            item = {
                "id": "ig_invalid",
                "type": "image_generation_call",
                "status": "generating",
                "result": "not-an-image",
            }
            delivered = hooks._image_generation_stream_chunks_for_delivery(
                item,
                {"client_metadata": {"headers": {"session-id": "thread-fixture"}}},
            )
            self.assertEqual(delivered, [item])
            hooks._normalize_image_generation_result_status(item)
            self.assertEqual(item["status"], "generating")
            self.assertFalse((Path(codex_home) / "generated_images").exists())

    def test_image_generation_stream_attaches_path_inside_responses_done_event(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as codex_home:
            self.set_env("CODEX_HOME", codex_home)
            chunk = {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": "ig_nested",
                    "type": "image_generation_call",
                    "status": "completed",
                    "result": VALID_IMAGE_RESULT,
                },
            }
            delivered = hooks._image_generation_stream_chunks_for_delivery(
                chunk,
                {"call_type": "aresponses", "client_metadata": {"headers": {"session-id": "thread-nested"}}},
                [],
                set(),
            )
            # [opening frame, completion] -- the completion is the last one.
            self.assertEqual(
                ["response.output_item.added", "response.output_item.done"],
                [item["type"] for item in delivered],
            )
            saved_path = Path(delivered[-1]["item"]["saved_path"])
            self.assertEqual(
                saved_path,
                Path(codex_home) / "generated_images" / "thread-nested" / "ig_nested.png",
            )
            self.assertEqual(saved_path.read_bytes(), base64.b64decode(VALID_IMAGE_RESULT))

    def test_image_generation_stream_attaches_path_to_sse_json_frame(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as codex_home:
            self.set_env("CODEX_HOME", codex_home)
            frame = (
                "data: "
                + json.dumps(
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "id": "ig_sse",
                            "type": "image_generation_call",
                            "status": "completed",
                            "result": VALID_IMAGE_RESULT,
                        },
                    }
                )
                + "\n\n"
            )
            delivered = hooks._image_generation_stream_chunks_for_delivery(
                frame,
                {"call_type": "aresponses", "client_metadata": {"headers": {"session-id": "thread-sse"}}},
                [],
                set(),
            )
            # The restored opening frame precedes the completion it opens.
            frames = [line for line in delivered[0].splitlines() if line.startswith("data:")]
            self.assertEqual(2, len(frames))
            payload = json.loads(frames[-1].split("data: ", 1)[1])
            saved_path = Path(payload["item"]["saved_path"])
            self.assertEqual(
                saved_path,
                Path(codex_home) / "generated_images" / "thread-sse" / "ig_sse.png",
            )
            self.assertEqual(saved_path.read_bytes(), base64.b64decode(VALID_IMAGE_RESULT))

    def test_image_generation_stream_preserves_multiple_sse_events_when_attaching_path(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as codex_home:
            self.set_env("CODEX_HOME", codex_home)
            frame = (
                "data: "
                + json.dumps(
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "id": "ig_multi",
                            "type": "image_generation_call",
                            "status": "completed",
                            "result": VALID_IMAGE_RESULT,
                        },
                    }
                )
                + "\n\n"
                + "data: "
                + json.dumps({"type": "response.output_text.delta", "delta": "kept"})
                + "\n\n"
            )
            delivered = hooks._image_generation_stream_chunks_for_delivery(
                frame,
                {"call_type": "aresponses", "client_metadata": {"headers": {"session-id": "thread-multi"}}},
                [],
                set(),
            )[0]
            frames = [line for line in delivered.splitlines() if line.startswith("data:")]
            # The route stated the completion alone, so the restored opening
            # frame joins it; the text delta that followed stays where it was.
            self.assertEqual(len(frames), 3)
            self.assertEqual(
                ["response.output_item.added", "response.output_item.done", "response.output_text.delta"],
                [json.loads(frame.split("data: ", 1)[1])["type"] for frame in frames],
            )
            image_payload = json.loads(frames[1].split("data: ", 1)[1])
            text_payload = json.loads(frames[2].split("data: ", 1)[1])
            self.assertEqual("in_progress", json.loads(frames[0].split("data: ", 1)[1])["item"]["status"])
            self.assertTrue(Path(image_payload["item"]["saved_path"]).is_file())
            self.assertEqual(text_payload, {"type": "response.output_text.delta", "delta": "kept"})

    def test_image_generation_stream_rewrites_stale_markdown_path_after_result(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as codex_home:
            self.set_env("CODEX_HOME", codex_home)
            request_data = {
                "call_type": "aresponses",
                "client_metadata": {"headers": {"session-id": "thread-rewrite"}},
            }
            saved_paths: list[str] = []
            opened: set[str] = set()
            image_chunk = hooks._image_generation_stream_chunks_for_delivery(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "id": "ig_rewrite",
                        "type": "image_generation_call",
                        "status": "completed",
                        "result": VALID_IMAGE_RESULT,
                    },
                },
                request_data,
                saved_paths,
                opened,
            )[-1]
            saved_path = image_chunk["item"]["saved_path"]
            stale_link = (
                "![参考图](/synthetic/codex/generated_images/unknown-session/"
                "image-generation.png)"
            )
            text_chunk = hooks._image_generation_stream_chunks_for_delivery(
                {
                    "type": "response.output_text.delta",
                    "delta": stale_link,
                },
                request_data,
                saved_paths,
            )[0]
            self.assertEqual(text_chunk["delta"], f"![参考图]({saved_path})")
            self.assertTrue(Path(saved_path).is_file())

    def test_image_generation_stream_adds_reference_when_message_has_no_image_link(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as codex_home:
            self.set_env("CODEX_HOME", codex_home)
            request_data = {
                "call_type": "aresponses",
                "client_metadata": {"headers": {"session-id": "thread-append"}},
            }
            saved_paths: list[str] = []
            opened: set[str] = set()
            image_chunk = hooks._image_generation_stream_chunks_for_delivery(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "id": "ig_append",
                        "type": "image_generation_call",
                        "status": "completed",
                        "result": VALID_IMAGE_RESULT,
                    },
                },
                request_data,
                saved_paths,
                opened,
            )[-1]
            saved_path = image_chunk["item"]["saved_path"]
            message_chunk = hooks._image_generation_stream_chunks_for_delivery(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "已生成。"}],
                    },
                },
                request_data,
                saved_paths,
            )[0]
            self.assertEqual(
                message_chunk["item"]["content"][0]["text"],
                f"已生成。\n\n![Generated image]({saved_path})",
            )

    def test_image_generation_stream_attaches_path_to_model_dump_chunk(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as codex_home:
            self.set_env("CODEX_HOME", codex_home)
            payload = {
                "type": "response.output_item.done",
                "item": {
                    "id": "ig_model",
                    "type": "image_generation_call",
                    "status": "completed",
                    "result": VALID_IMAGE_RESULT,
                },
            }

            class ModelChunk:
                def model_dump(self):
                    return payload

            delivered = hooks._image_generation_stream_chunks_for_delivery(
                ModelChunk(),
                {"call_type": "aresponses", "client_metadata": {"headers": {"session-id": "thread-model"}}},
                [],
                set(),
            )
            saved_path = Path(delivered[-1]["item"]["saved_path"])
            self.assertEqual(
                saved_path,
                Path(codex_home) / "generated_images" / "thread-model" / "ig_model.png",
            )
            self.assertEqual(saved_path.read_bytes(), base64.b64decode(VALID_IMAGE_RESULT))

    async def test_streaming_hook_delivers_saved_path_for_responses_session(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as codex_home:
            self.set_env("CODEX_HOME", codex_home)
            async def upstream_stream():
                yield {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "id": "ig_hook",
                        "type": "image_generation_call",
                        "status": "completed",
                        "result": VALID_IMAGE_RESULT,
                    },
                }
                yield {
                    "type": "response.output_text.delta",
                    "delta": (
                        "![参考图](/synthetic/codex/generated_images/unknown-session/"
                        "image-generation.png)"
                    ),
                }

            request_data = {
                "call_type": "aresponses",
                "model": "default-chat",
                "input": "generate an image",
                "stream": True,
                "litellm_params": {
                    "metadata": {"headers": {"session-id": "thread-hook"}},
                },
            }
            chunks = [
                chunk
                async for chunk in hooks.YoungRouterHook().async_post_call_streaming_iterator_hook(
                    user_api_key_dict=None,
                    response=upstream_stream(),
                    request_data=request_data,
                )
            ]
            delivered = jsonable_stream_chunk(chunks[0])
            self.assertEqual("response.output_item.added", delivered["type"])
            self.assertEqual("in_progress", delivered["item"]["status"])
            completion = jsonable_stream_chunk(chunks[1])
            saved_path = Path(completion["item"]["saved_path"])
            self.assertEqual(
                saved_path,
                Path(codex_home) / "generated_images" / "thread-hook" / "ig_hook.png",
            )
            self.assertEqual(saved_path.read_bytes(), base64.b64decode(VALID_IMAGE_RESULT))
            self.assertEqual(
                jsonable_stream_chunk(chunks[2])["delta"],
                f"![参考图]({saved_path})",
            )

    async def test_responses_api_image_generation_tool_does_not_use_static_capability_filter(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()

        def aresponses():
            pass

        deployments = [
            {
                "litellm_params": {"api_base": "https://headers.example/v1"},
                "model_info": {"id": "compat_provider-normal", "supports_responses_image_generation_tool": False},
            },
            {
                "litellm_params": {"api_base": "https://headers.example/v1"},
                "model_info": {"id": "compat_provider-pro", "supports_responses_image_generation_tool": True},
            },
            {
                "litellm_params": {"api_base": "https://api.backup.example/v1"},
                "model_info": {"id": "backup_provider", "supports_responses_image_generation_tool": True},
            },
        ]

        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=None,
            request_kwargs={
                "original_generic_function": aresponses,
                "tools": [{"type": "image_generation"}],
            },
        )

        self.assertEqual(filtered, deployments)

    async def test_responses_api_image_generation_edit_keeps_image_input_candidates_for_runtime_probe(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()

        deployments = [
            {
                "litellm_params": {"api_base": "https://provider-a.example/v1"},
                "model_info": {
                    "id": "declared-unsafe",
                    "supports_vision": False,
                    "supports_responses_image_input": False,
                },
            },
            {
                "litellm_params": {"api_base": "https://provider-b.example/v1"},
                "model_info": {"id": "declared-safe", "supports_vision": True},
            },
        ]

        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=None,
            request_kwargs={
                "call_type": "aresponses",
                "tools": [{"type": "image_generation"}],
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "edit this image"},
                            {
                                "type": "input_image",
                                "image_url": "data:image/png;base64,abc",
                            },
                        ],
                    }
                ],
            },
        )

        self.assertEqual(filtered, deployments)

    async def test_responses_api_with_image_input_does_not_use_vision_capability_filter(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()

        def aresponses():
            pass

        deployments = [
            {
                "litellm_params": {"api_base": "https://headers.example/v1"},
                "model_info": {"id": "compat_provider-normal", "supports_vision": True},
            },
            {
                "litellm_params": {"api_base": "https://api.backup.example/v1"},
                "model_info": {"id": "backup_provider", "supports_vision": False},
            },
        ]

        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=None,
            request_kwargs={
                "original_generic_function": aresponses,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "read this"},
                            {
                                "type": "input_image",
                                "image_url": "data:image/jpeg;base64,abc",
                            },
                        ],
                    }
                ],
            },
        )

        self.assertEqual(filtered, deployments)

    async def test_responses_api_with_image_input_skips_declared_unsafe_deployment(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()

        def aresponses():
            pass

        deployments = [
            {
                "litellm_params": {"api_base": "https://provider-a.example/v1"},
                "model_info": {
                    "id": "vision-unsafe",
                    "supports_vision": True,
                    "supports_responses_image_input": False,
                },
            },
            {
                "litellm_params": {"api_base": "https://provider-b.example/v1"},
                "model_info": {"id": "vision-safe", "supports_vision": True},
            },
        ]

        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=None,
            request_kwargs={
                "original_generic_function": aresponses,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "read this"},
                            {
                                "type": "input_image",
                                "image_url": "data:image/png;base64,abc",
                            },
                        ],
                    }
                ],
            },
        )

        self.assertEqual(filtered, [deployments[1]])

    async def test_chat_completions_image_url_keeps_responses_only_unsafe_deployment(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()

        deployments = [
            {
                "litellm_params": {"api_base": "https://provider-a.example/v1"},
                "model_info": {
                    "id": "vision-unsafe-for-responses-only",
                    "supports_vision": True,
                    "supports_responses_image_input": False,
                },
            },
            {
                "litellm_params": {"api_base": "https://provider-b.example/v1"},
                "model_info": {"id": "vision-safe", "supports_vision": True},
            },
        ]

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "read this"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ],
            }
        ]
        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=messages,
            request_kwargs={"messages": messages},
        )

        self.assertEqual(filtered, deployments)

    async def test_chat_completions_image_url_does_not_use_vision_capability_filter(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()

        deployments = [
            {
                "litellm_params": {"api_base": "https://headers.example/v1"},
                "model_info": {"id": "compat_provider-normal", "supports_vision": True},
            },
            {
                "litellm_params": {"api_base": "https://api.backup.example/v1"},
                "model_info": {"id": "backup_provider", "supports_vision": False},
            },
        ]

        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "read this"},
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abc"}},
                    ],
                }
            ],
            request_kwargs={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "read this"},
                            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abc"}},
                        ],
                    }
                ]
            },
        )

        self.assertEqual(filtered, deployments)

    def test_forced_image_generation_payload_uses_internal_metadata_by_default(self) -> None:
        hooks, _ = load_hook_module()

        payload = hooks._build_forced_image_generation_payload(
            {
                "model": "default-chat",
                "input": "draw",
                "tools": [{"type": "image_generation"}],
                "metadata": {"trace_id": "client-trace"},
                "litellm_metadata": {"model_group": "default-chat"},
            },
            stream=True,
        )

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertNotIn("metadata", payload)
        self.assertEqual(payload["litellm_metadata"]["trace_id"], "client-trace")
        self.assertEqual(payload["litellm_metadata"]["model_group"], "default-chat")
        self.assertTrue(payload["litellm_metadata"][hooks._STREAM_FALLBACK_METADATA_KEY])

    def test_forced_image_generation_payload_preserves_metadata_when_opted_in(self) -> None:
        hooks, _ = load_hook_module()

        payload = hooks._build_forced_image_generation_payload(
            {
                "model": "default-chat",
                "input": "draw",
                "tools": [{"type": "image_generation"}],
                "metadata": {"trace_id": "client-trace"},
                "model_info": {"supports_request_metadata": True},
            },
            stream=False,
        )

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["metadata"], {"trace_id": "client-trace"})
        self.assertTrue(payload["litellm_metadata"][hooks._STREAM_FALLBACK_METADATA_KEY])

    async def test_plain_text_request_does_not_retry(self) -> None:
        hooks, proxy_server = load_hook_module()

        class FakeRouter:
            async def aresponses(self, **payload):
                raise AssertionError("plain text requests must not invoke image fallback")

        proxy_server.llm_router = FakeRouter()
        hook = hooks.YoungRouterHook()
        request_data = {"model": "default-chat", "input": "Say pong only."}
        original = {"output_text": "pong"}

        response = await hook.async_post_call_success_deployment_hook(request_data, original, call_type=None)

        self.assertEqual(response, original)

    async def test_structured_image_tool_with_normal_text_response_forces_probe(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                return {
                    "output": [
                        {
                            "type": "image_generation_call",
                            "result": VALID_IMAGE_RESULT,
                        }
                    ]
                }

        proxy_server.llm_router = FakeRouter()
        hook = hooks.YoungRouterHook()
        request_data = {
            "model": "default-chat",
            "input": "Continue the long-running paper translation and report status.",
            "tools": [{"type": "image_generation"}],
            "tool_choice": "auto",
            "stream": False,
        }
        original = {"output_text": "现在这轮已经推进到 39 个 block 了，继续等待收口。"}

        response = await hook.async_post_call_success_deployment_hook(request_data, original, call_type=None)

        self.assertEqual(response["output"][0]["type"], "image_generation_call")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tool_choice"], {"type": "image_generation"})

    async def test_image_policy_refusal_does_not_probe_or_mark_deployment_unsupported(self) -> None:
        hooks, proxy_server = load_hook_module()

        class FakeRouter:
            async def aresponses(self, **payload):
                raise AssertionError("policy refusals must not invoke image fallback")

        proxy_server.llm_router = FakeRouter()
        hook = hooks.YoungRouterHook()
        request_data = {
            "model": "default-chat",
            "input": "make a disallowed image",
            "tools": [{"type": "image_generation"}],
            "tool_choice": "auto",
            "stream": False,
        }
        refusal = {"output_text": "This request violates our content policy."}

        response = await hook.async_post_call_success_deployment_hook(
            request_data,
            refusal,
            call_type=None,
        )

        self.assertIs(response, refusal)
        policy_error = RuntimeError("This request violates our content policy.")
        policy_error.status_code = 400
        self.assertFalse(
            hooks._is_image_generation_tool_capability_error(policy_error)
        )


class ImageDeliveryGuardTests(HookTestCase):
    def test_an_echoed_image_with_a_malformed_type_is_still_stripped(self) -> None:
        """A non-text ``type`` must not raise while stripping echoed images.

        The stripper tests a candidate's type against a small set of image
        item types, so an echoed dict whose type is a list or a dict raised
        TypeError on the delivery path instead of being stripped.
        """

        hooks, _ = load_hook_module()
        request_data = {
            "model": "default-chat",
            "input": [
                {"type": "message", "role": "user", "content": [
                    {"type": "input_image", "image_url": "data:image/png;base64,abc"},
                ]},
            ],
        }
        for malformed in ([], {}, ["input_image"]):
            response = {
                "id": "resp_1",
                "output": [
                    {"type": "message", "content": [
                        {"type": malformed, "image_url": "data:image/png;base64,abc"},
                    ]},
                ],
            }
            with self.subTest(malformed=malformed):
                delivered = hooks._sanitize_response_echoed_request_images_for_delivery(
                    response,
                    request_data,
                )
                self.assertNotIn("data:image", json.dumps(delivered))


if __name__ == "__main__":
    unittest.main()
