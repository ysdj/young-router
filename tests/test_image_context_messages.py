"""Image context on the Messages wire (Claude Code / Claude Desktop).

The same question the Codex pass answers, in the shape a Claude replay uses:
a picture is an ``image`` block with its bytes behind ``source``, and a tool
result is a *user* message.  These tests pin what this wire adds to the shared
rules: status/tool pairing and block order survive, a cache breakpoint follows
its block, a turn is counted by a real user request rather than by every
``role: user`` message, and the fetch is the ask sentence -- no MCP server, no
client configuration, and no file path.
"""

from __future__ import annotations

import base64
import os
import tempfile
import unittest
from unittest import mock

from test_image_context import _png

from young_router.proxy import image_context as image_context_module
from young_router.proxy import image_context_store as store_module


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _image(raw: bytes, *, cache_control: dict | None = None) -> dict:
    block = {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": _b64(raw),
        },
    }
    if cache_control is not None:
        block["cache_control"] = dict(cache_control)
    return block


def _user(text: str, *blocks: dict) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}, *blocks]}


def _assistant(text: str) -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def _tool_result(call_id: str, content) -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": call_id, "content": content}
        ],
    }


def _tool_call(call_id: str) -> dict:
    return {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": call_id,
                "name": "Read",
                "input": {"file_path": f"/tmp/{call_id}.png"},
            }
        ],
    }


def _request(messages: list, **overrides) -> dict:
    request = {
        "model": "claude-sonnet-4-5",
        "call_type": "anthropic_messages",
        "max_tokens": 4096,
        "headers": {"user-agent": "claude-cli/2.1.309 (external, cli)"},
        "messages": messages,
    }
    request.update(overrides)
    return request


def _blocks(result: dict) -> list:
    return [
        block
        for message in result["messages"]
        for block in (message.get("content") or [])
        if isinstance(block, dict)
    ]


def _image_blocks(result: dict) -> list:
    return [block for block in _blocks(result) if block.get("type") == "image"]


def _text_blocks(result: dict) -> list:
    return [block for block in _blocks(result) if block.get("type") == "text"]


class _MessagesImageContextCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(
            os.environ,
            {
                image_context_module.CLAUDE_IMAGE_CONTEXT_ENABLED_ENV: "1",
                image_context_module.IMAGE_CONTEXT_MIN_BYTES_ENV: "1024",
                image_context_module._VISIBLE_PICTURES_ENV: "0",
                image_context_module._VISIBLE_TOKENS_ENV: "0",
                store_module.REPLAY_CACHE_DIR_ENV: os.path.join(
                    self.directory.name, "replays"
                ),
            },
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.directory.cleanup()

    def rewritten(self, messages: list, **overrides) -> dict:
        result = image_context_module._with_claude_image_context(
            _request(messages, **overrides)
        )
        self.assertIsNotNone(result)
        assert result is not None
        return result


class MessagesScanTests(_MessagesImageContextCase):
    def test_a_tool_result_is_a_user_message_but_not_a_new_turn(self) -> None:
        # The lifted shape: the picture sits beside the result, in the user
        # message the result arrived in.  ``tool_result_images`` produces this
        # before the pass runs; a picture still nested in the result is the
        # lift's business, not the scan's (see the rewrite case below).
        messages = [
            _user("look at this", _image(_png(120, 90, noisy=True))),
            _assistant("looking"),
            _tool_call("t1"),
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
                    {"type": "text", "text": "The image the tool returned:"},
                    _image(_png(60, 40, noisy=True, seed=2)),
                ],
            },
        ]

        found = image_context_module.scan_messages(messages)

        self.assertEqual(["IMG-001", "IMG-002"], [image.image_id for image in found])
        self.assertEqual([1, 1], [image.turn for image in found])
        self.assertEqual(["upload", "tool"], [image.kind for image in found])
        self.assertTrue(found[1].unseen, "the tool result arrived after the last answer")

    def test_the_next_user_prompt_starts_the_next_turn(self) -> None:
        messages = [
            _user("first"),
            _tool_call("t1"),
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
                    {"type": "text", "text": "The image the tool returned:"},
                    _image(_png(60, 40, noisy=True, seed=2)),
                ],
            },
            _assistant("ok"),
            _user("second"),
        ]

        found = image_context_module.scan_messages(messages)

        self.assertEqual([1], [image.turn for image in found])
        self.assertEqual(
            2, image_context_module._messages_turn_count(messages)
        )

    def test_a_picture_ahead_of_a_tool_result_is_left_alone(self) -> None:
        # The lift puts pictures behind the run of results.  A body that still
        # has one in front of a result is not "labelled" into an ordering the
        # Messages wire refuses; it is left exactly as it arrived.
        messages = [
            {
                "role": "user",
                "content": [
                    _image(_png(60, 40, noisy=True)),
                    {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
                ],
            }
        ]

        self.assertEqual([], image_context_module.scan_messages(messages))

    def test_a_url_source_is_not_bytes_this_pass_can_omit(self) -> None:
        messages = [
            _user("remote", {"type": "image", "source": {"type": "url", "url": "https://x/a.png"}}),
        ]

        self.assertEqual([], image_context_module.scan_messages(messages))


class MessagesRewriteTests(_MessagesImageContextCase):
    def test_an_earlier_picture_becomes_a_placeholder_and_the_newest_turn_stays(self) -> None:
        old_raw = _png(120, 90, noisy=True)
        new_raw = _png(60, 40, noisy=True, seed=2)
        messages = [
            _user("第一轮看这张图", _image(old_raw)),
            _assistant("看到了"),
            _user("第二轮", _image(new_raw)),
        ]

        result = self.rewritten(messages)

        first = result["messages"][0]["content"]
        self.assertEqual(["text", "text", "text"], [block["type"] for block in first])
        self.assertIn("图片 IMG-001 已省略", first[1]["text"])
        self.assertIn("第 1 轮", first[1]["text"])
        self.assertIn("120×90", first[1]["text"])
        self.assertNotIn("base64", str(first))

        newest = result["messages"][2]["content"]
        self.assertEqual(["text", "text", "image"], [block["type"] for block in newest])
        self.assertIn("图片 IMG-002 已提供", newest[1]["text"])
        self.assertEqual(messages[2]["content"][1], newest[2])

        # The note lives inside the opener of the first affected turn, never
        # as a new message.  Here that is turn 1's own user message, so the
        # newest prompt is untouched.
        self.assertEqual(len(messages), len(result["messages"]))
        self.assertIn("上下文管理说明", first[-1]["text"])
        self.assertIn("需要 IMG-xxx", first[-1]["text"])
        self.assertNotIn("上下文管理说明", newest[0]["text"])

    def test_tool_results_keep_their_pairing_and_their_first_position(self) -> None:
        messages = [
            _user("第一轮看这张图", _image(_png(120, 90, noisy=True))),
            _tool_call("t1"),
            _tool_result("t1", [_image(_png(80, 60, noisy=True, seed=3))]),
            _assistant("第二轮"),
            _user("继续"),
        ]

        result = self.rewritten(messages)

        for message in result["messages"]:
            for block in message.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                self.assertEqual("t1", block["tool_use_id"])
                self.assertEqual(0, message["content"].index(block))
        self.assertIn(
            "图片 IMG-",
            " ".join(block.get("text", "") for block in _text_blocks(result)),
        )

    def test_a_tool_result_picture_is_lifted_even_when_the_pass_is_called_directly(self) -> None:
        # The pre-call hook lifts this shape on the real path; a direct caller
        # gets the repair here, because a pass that could not see it would leave
        # the largest block of a Claude Code replay untouched.
        messages = [
            _user("第一轮"),
            _tool_call("t1"),
            _tool_result("t1", [_image(_png(120, 90, noisy=True))]),
            _assistant("答"),
            _user("继续"),
        ]

        result = self.rewritten(messages)

        lifted = [
            block
            for block in result["messages"][2]["content"]
            if isinstance(block, dict) and block.get("type") == "image"
        ]
        self.assertEqual([], lifted)
        self.assertEqual(
            "t1", result["messages"][2]["content"][0]["tool_use_id"]
        )

    def test_the_ask_is_honoured_without_a_tool(self) -> None:
        messages = [_user("第一轮", _image(_png(900, 700, noisy=True)))]
        for seed in range(3, 7):
            messages.extend(
                [
                    _tool_call(f"t{seed}"),
                    _tool_result(
                        f"t{seed}", [_image(_png(1000, 800, noisy=True, seed=seed))]
                    ),
                    _assistant("ok"),
                ]
            )
        messages.append(_assistant("需要 IMG-002"))
        messages.append(_user("继续"))

        result = self.rewritten(messages)

        self.assertEqual(1, len(_image_blocks(result)))

    def test_a_cache_breakpoint_survives_its_block(self) -> None:
        messages = [
            _user(
                "第一轮",
                _image(
                    _png(120, 90, noisy=True),
                    cache_control={"type": "ephemeral"},
                ),
            ),
            _assistant("看到了"),
            _user("第二轮"),
        ]

        result = self.rewritten(messages)

        placeholder = result["messages"][0]["content"][1]
        self.assertEqual("text", placeholder["type"])
        self.assertEqual({"type": "ephemeral"}, placeholder["cache_control"])

    def test_thinking_and_signature_blocks_are_left_byte_exact(self) -> None:
        thinking = [
            {"type": "thinking", "thinking": "weighing", "signature": "sig"},
            {"type": "redacted_thinking", "data": "opaque"},
        ]
        messages = [
            _user("第一轮", _image(_png(120, 90, noisy=True))),
            {"role": "assistant", "content": [*thinking, {"type": "text", "text": "ok"}]},
            _user("第二轮"),
        ]

        result = self.rewritten(messages)

        self.assertEqual(
            thinking, result["messages"][1]["content"][:2]
        )

    def test_a_replay_from_a_non_claude_messages_client_is_not_rewritten(self) -> None:
        messages = [
            _user("first", _image(_png(120, 90, noisy=True))),
            _assistant("ok"),
            _user("second"),
        ]
        request = _request(
            messages, headers={"user-agent": "python-anthropic/0.49.0"}
        )

        self.assertIsNone(image_context_module._with_claude_image_context(request))

    def test_the_claude_system_prompt_is_evidence_when_the_agent_is_stripped(self) -> None:
        messages = [
            _user("first", _image(_png(120, 90, noisy=True))),
            _assistant("ok"),
            _user("second"),
        ]
        request = _request(
            messages,
            headers={},
            system=[
                {
                    "type": "text",
                    "text": "You are Claude Code, Anthropic's official CLI for Claude.",
                }
            ],
        )

        result = image_context_module._with_claude_image_context(request)

        assert result is not None
        self.assertEqual([], _image_blocks(result))
        self.assertIn(
            "omitted",
            " ".join(block.get("text", "") for block in _text_blocks(result)),
        )

    def test_a_chat_completions_body_is_not_a_messages_request(self) -> None:
        request = {
            "model": "gpt-6",
            "call_type": "acompletion",
            "headers": {"user-agent": "claude-cli/2.1.0"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "hi"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AAAA"},
                        },
                    ],
                }
            ],
        }

        self.assertFalse(image_context_module._request_is_messages_wire(request))
        self.assertIsNone(image_context_module._with_claude_image_context(request))

    def test_the_setting_off_leaves_the_body_exactly_as_sent(self) -> None:
        messages = [
            _user("first", _image(_png(120, 90, noisy=True))),
            _assistant("ok"),
            _user("second"),
        ]
        request = _request(messages)

        with mock.patch.dict(
            os.environ,
            {image_context_module.CLAUDE_IMAGE_CONTEXT_ENABLED_ENV: "0"},
        ):
            self.assertIsNone(
                image_context_module._with_claude_image_context(request)
            )
        self.assertEqual(messages, request["messages"])

    def test_a_route_that_refuses_images_gets_its_replay_unchanged(self) -> None:
        messages = [
            _user("first", _image(_png(120, 90, noisy=True))),
            _assistant("ok"),
            _user("second"),
        ]
        request = _request(
            messages,
            litellm_params={"model_info": {"supports_vision": False}},
        )

        self.assertIsNone(image_context_module._with_claude_image_context(request))


class MessagesSizeRepairTests(_MessagesImageContextCase):
    def test_size_mode_repairs_a_messages_replay_although_the_setting_is_off(self) -> None:
        messages = [
            _user("first", _image(_png(120, 90, noisy=True))),
            _assistant("ok"),
            _user("second"),
        ]
        request = _request(messages)

        with mock.patch.dict(
            os.environ,
            {image_context_module.CLAUDE_IMAGE_CONTEXT_ENABLED_ENV: "0"},
        ):
            result = image_context_module.rewrite_for_size(request, keep=0)

        assert result is not None
        self.assertEqual([], _image_blocks(result))
        self.assertIn(
            'need IMG-001',
            " ".join(block.get("text", "") for block in _text_blocks(result)),
        )


class MessagesHookIntegrationTests(unittest.TestCase):
    def test_the_deployment_hook_runs_the_claude_step(self) -> None:
        from hook_test_utils import load_hook_module

        hooks, _ = load_hook_module()
        raw = hooks._image_context_module
        messages = [
            _user("first", _image(_png(120, 90, noisy=True))),
            _assistant("ok"),
            _user("second"),
        ]
        request = _request(messages, api_key="k", api_base="https://api.anthropic.com")

        with mock.patch.dict(
            os.environ,
            {
                raw.CLAUDE_IMAGE_CONTEXT_ENABLED_ENV: "1",
                raw.IMAGE_CONTEXT_MIN_BYTES_ENV: "1024",
                raw._VISIBLE_PICTURES_ENV: "0",
                raw._VISIBLE_TOKENS_ENV: "0",
            },
        ):
            import asyncio

            modified = asyncio.run(
                hooks.YoungRouterHook().async_pre_call_deployment_hook(
                    request, call_type="anthropic_messages"
                )
            )

        assert modified is not None
        self.assertEqual([], _image_blocks(modified))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
