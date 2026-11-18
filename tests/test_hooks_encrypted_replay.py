from __future__ import annotations

import json

from hook_test_utils import *


ENCRYPTED_REFUSAL = (
    "OpenAIException - Encrypted function output content could not be "
    "decrypted or decoded. This error occurs when load balancing Responses API "
    "across deployments with different API keys. Encrypted content is tied to "
    "the organization that created it and cannot be decrypted by other "
    "organizations."
)


def _refusal(status_code=400):
    exception = type("BadRequestError", (Exception,), {})(ENCRYPTED_REFUSAL)
    exception.status_code = status_code
    return exception


def _mixed_history() -> list:
    return [
        {"role": "user", "content": "keep working on the report"},
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [{"type": "summary_text", "text": "reading the file"}],
            "encrypted_content": "opaque-from-another-org",
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "I will read it."}],
        },
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "exec_command",
            "arguments": "{}",
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "the file",
        },
        {
            "type": "agent_message",
            "id": "msg_1",
            "content": [
                {"type": "input_text", "text": "subagent finished"},
                {"type": "encrypted_content", "encrypted_content": "opaque"},
            ],
        },
    ]


class EncryptedReplayStripTests(HookTestCase):
    """A history minted by several accounts is still replayable.

    ``encrypted_content`` is bound to the organization that minted it.  A Codex
    thread served by more than one station carries items no single route can
    decrypt, so every candidate answers the same 400 and the fallback has
    nowhere to go: on 2026-11-18 a Codex thread lost eight turns that way.  The ciphertext is a cache of the model's own thinking, not the
    conversation, so the turn is resent without it.
    """

    def test_the_refusal_drops_only_the_signed_payload(self) -> None:
        hooks, _proxy_server = load_hook_module()
        request = {"model": "gpt-6.2-astra", "input": _mixed_history()}

        self.assertTrue(
            hooks._strip_unreadable_encrypted_replay(request, _refusal())
        )

        items = request["input"]
        self.assertEqual(
            [item.get("type") or item.get("role") for item in items],
            ["user", "message", "function_call", "function_call_output", "agent_message"],
        )
        # The subagent's text survives; only its signed attachment is gone.
        self.assertEqual(
            items[-1]["content"],
            [{"type": "input_text", "text": "subagent finished"}],
        )
        self.assertNotIn("encrypted_content", json.dumps(items))

    def test_the_strip_happens_once_per_turn(self) -> None:
        hooks, _proxy_server = load_hook_module()
        request = {"model": "gpt-6.2-astra", "input": _mixed_history()}

        self.assertTrue(hooks._strip_unreadable_encrypted_replay(request, _refusal()))
        # The same turn advancing to another hop must not rewrite the body a
        # second time: the marker travels with the request.
        self.assertFalse(hooks._strip_unreadable_encrypted_replay(request, _refusal()))
        self.assertTrue(hooks._request_encrypted_replay_was_stripped(request))

    def test_another_refusal_leaves_the_body_alone(self) -> None:
        hooks, _proxy_server = load_hook_module()
        request = {"model": "gpt-6.2-astra", "input": _mixed_history()}
        exception = type("BadRequestError", (Exception,), {})(
            "OpenAIException - context length exceeded"
        )
        exception.status_code = 400

        self.assertFalse(
            hooks._strip_unreadable_encrypted_replay(request, exception)
        )
        self.assertEqual(len(request["input"]), 6)

    def test_a_chat_replay_is_not_rewritten(self) -> None:
        """The Chat wire never carries the blob; nothing there can be dropped."""

        hooks, _proxy_server = load_hook_module()
        request = {
            "model": "gpt-6.2-astra",
            "messages": [{"role": "user", "content": "hi"}],
        }

        self.assertFalse(hooks._strip_unreadable_encrypted_replay(request, _refusal()))

    async def test_the_fallback_resends_the_history_without_ciphertext(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def peer_stream():
            yield {"type": "response.created", "response": {"id": "resp-peer"}}
            yield {"type": "response.output_text.delta", "delta": "peer answered"}
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp-peer",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "peer answered"}
                            ],
                        }
                    ],
                },
            }

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                return peer_stream()

        proxy_server.llm_router = FakeRouter()
        request_data = {
            "model": "gpt-6.2-astra",
            "input": _mixed_history(),
            "stream": True,
            "proxy_server_request": {"path": "/v1/responses"},
            "model_info": {"id": "relay-a", "order": 1},
        }
        exception = _refusal()
        exception.failed_deployment_id = "relay-a"
        exception.failed_deployment_order = 1
        # The failing hop has already been excluded by the failover marking; a
        # repair that changed the body must not spend a hop on a sibling that
        # never saw these bytes, so the route goes back into the pool.
        request_data["_excluded_deployment_ids"] = ["relay-a"]

        chunks = [
            chunk
            async for chunk in hooks._yield_streaming_error_fallback_or_raise(
                request_data,
                exception,
            )
        ]

        self.assertTrue(calls)
        self.assertNotIn("encrypted_content", json.dumps(calls[0]["input"]))
        self.assertNotIn(
            "relay-a", hooks._request_excluded_deployment_ids(request_data)
        )
        self.assertNotIn("encrypted_content", json.dumps(request_data["input"]))
        self.assertTrue(
            any(
                isinstance(chunk, dict)
                and chunk.get("type") == "response.output_text.delta"
                for chunk in chunks
            )
        )

    async def test_a_single_hop_retries_itself_with_the_stripped_history(self) -> None:
        """The non-stream dispatch repairs the body instead of failing the hop."""

        hooks, _proxy_server = load_hook_module()
        from young_router.proxy import reasoning_replay

        kwargs = {"model": "gpt-6.2-astra", "input": _mixed_history()}
        seen = []

        async def call():
            seen.append(list(kwargs["input"]))
            if len(seen) == 1:
                raise _refusal()
            return "answered"

        result = await reasoning_replay.call_with_replay_repair(call, kwargs)

        self.assertEqual("answered", result)
        self.assertEqual(2, len(seen))
        self.assertIn("encrypted_content", json.dumps(seen[0]))
        self.assertNotIn("encrypted_content", json.dumps(seen[1]))
        self.assertTrue(hooks._request_encrypted_replay_was_stripped(kwargs))
