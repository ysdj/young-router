from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

from hook_test_utils import *


class _MaskedSignatureRefusal(Exception):
    """The station's envelope for an upstream 4xx it does not pass through.

    Measured 2026-11-13 against ``sisct2``: a body carrying a thinking block
    whose signature the route cannot verify answers
    ``502 {"type":"upstream_error","message":"Upstream request failed after
    retries"}`` -- the same envelope it gives for an outage, with no status the
    signature verdict could be read from.
    """

    status_code = 502

    def __init__(self) -> None:
        super().__init__(
            "litellm.BadGatewayError: AnthropicException - BadGatewayError - "
            '{"error":{"message":"Upstream request failed after retries",'
            '"type":"upstream_error"},"type":"error"}'
        )


def _signed_body(signature: str = "CAQS-sig-from-another-channel") -> dict:
    return {
        "model": "claude-fable-5-2",
        "max_tokens": 1,
        "messages": [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hmm", "signature": signature},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}
                ],
            },
            {"role": "system", "content": "Note: a file changed on disk"},
        ],
    }


def _request_kwargs(session: str = "sess-a", deployment: str = "dep-1") -> dict:
    return {
        "model": "claude-fable-5-2",
        "messages": _signed_body()["messages"],
        "model_info": {"id": deployment},
        "litellm_metadata": {"session_id": session},
    }


def _signatures(messages) -> list:
    found = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "thinking":
                found.append(block.get("signature"))
    return found


class ThinkingSignatureReplayTests(HookTestCase):
    def setUp(self) -> None:
        super().setUp()
        hooks, _ = load_hook_module()
        self.hooks = hooks
        self.module = hooks._thinking_signature_replay_module
        directory = tempfile.mkdtemp()
        self.state_path = Path(directory) / "routing.json"
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FILE_ENV, str(self.state_path))
        self.module._REQUIREMENTS.clear()
        self.module._REQUIREMENTS_LOADED_AT = 0.0
        self.addCleanup(self.module._REQUIREMENTS.clear)

    def test_a_replayed_signature_is_blanked_and_nothing_else_changes(self) -> None:
        """The rewrite is one field, so an upstream's cached prefix stays put.

        A signature that verifies is proof the route is the right one for this
        history; the repair exists only for a history whose signatures came
        from somewhere else.  Everything the upstream caches -- the thinking
        text, the tool pairing, the trailing system note, the message order --
        has to survive the rewrite, or the prefix the retry proves is not the
        prefix the next turn sends.
        """

        body = _signed_body()
        repaired = self.module.with_blanked_thinking_signatures(body)

        self.assertIsNotNone(repaired)
        self.assertEqual([""], _signatures(repaired["messages"]))
        # Only the signature differs.
        expected = copy.deepcopy(body)
        expected["messages"][1]["content"][0]["signature"] = ""
        self.assertEqual(json.dumps(expected, sort_keys=True), json.dumps(repaired, sort_keys=True))
        # The original body is untouched: the same dict is what the route was
        # called with, and a caller must be able to compare against it.
        self.assertEqual(
            "CAQS-sig-from-another-channel",
            body["messages"][1]["content"][0]["signature"],
        )
        # Canonical: the same body rewritten twice is the same bytes, which is
        # what keeps the prompt cache from being rebuilt on every turn.
        again = self.module.with_blanked_thinking_signatures(_signed_body())
        self.assertEqual(
            json.dumps(repaired, sort_keys=True), json.dumps(again, sort_keys=True)
        )
        # Nothing left to rewrite.
        self.assertIsNone(self.module.with_blanked_thinking_signatures(repaired))

    def test_only_a_body_that_replays_a_signed_thinking_is_retried(self) -> None:
        """The gate is the body, because the refusal carries no status.

        The station answers every upstream 4xx as its own 5xx envelope, so a
        text or status gate would never fire -- which is how one thread replayed
        a 924,938-token history nine times.  What is excluded here is the answer
        that cannot be about a signature: this proxy's own refusals, a body its
        own route counted as too large, and a throttle.
        """

        masked = _MaskedSignatureRefusal()
        self.assertTrue(self.module.retry_request_kwargs(masked, _request_kwargs()))

        unsigned = _request_kwargs()
        unsigned["messages"][1]["content"][0]["signature"] = ""
        self.assertIsNone(self.module.retry_request_kwargs(masked, unsigned))

        no_thinking = _request_kwargs()
        no_thinking["messages"] = [{"role": "user", "content": "hi"}]
        self.assertIsNone(self.module.retry_request_kwargs(masked, no_thinking))

        # Already the repaired attempt: a second refusal is about something else.
        attempted = self.module.retry_request_kwargs(masked, _request_kwargs())
        self.assertIsNone(self.module.retry_request_kwargs(masked, attempted))

        # This proxy's own cap: no route was asked, and the bytes are not the
        # subject of the answer.
        hooks = self.hooks
        cap = RuntimeError(
            "litellm.RateLimitError: concurrency limit reached for the model group"
        )
        cap.status_code = 429
        self.assertIsNone(self.module.retry_request_kwargs(cap, _request_kwargs()))

        # A prompt the route's own count refused is a size answer.
        too_long = RuntimeError(
            "litellm.ContextWindowExceededError: prompt is too long: 1048577 tokens"
        )
        too_long.status_code = 400
        self.assertIsNone(self.module.retry_request_kwargs(too_long, _request_kwargs()))

    async def test_a_rewritten_session_is_rewritten_before_the_next_turn_is_sent(
        self,
    ) -> None:
        """Learn once, then stop paying the refused round trip.

        Measured live: the same 924,938-token history answered 200 with its
        signatures blanked and 502 with them intact.  A session that proved it
        needs the rewrite must therefore be rewritten *before* it is sent, or
        every later turn pays one more refused attempt -- and the rewrite has to
        be the same bytes, so the upstream's cached prefix survives.
        """

        module = self.module
        kwargs = _request_kwargs()
        sent: list = []

        async def call() -> str:
            sent.append(copy.deepcopy(kwargs["messages"]))
            if len(sent) == 1:
                raise _MaskedSignatureRefusal()
            return "ok"

        self.assertEqual("ok", await module.call_with_signature_repair(call, kwargs))
        self.assertEqual(2, len(sent))
        self.assertEqual(["CAQS-sig-from-another-channel"], _signatures(sent[0]))
        self.assertEqual([""], _signatures(sent[1]))
        # Repaired in place: the caller keeps one kwargs dict per attempt.
        self.assertEqual([""], _signatures(kwargs["messages"]))

        # The fact is shared, not held per worker: sixteen workers must not each
        # pay the same refused round trip to learn it.  Dropping the in-process
        # copy makes the next answer come from the shared document.
        module._REQUIREMENTS.clear()
        module._REQUIREMENTS_LOADED_AT = 0.0
        self.assertTrue(module._recorded_repair_requirement(_request_kwargs()))
        self.assertFalse(module._recorded_repair_requirement(_request_kwargs(session="sess-b")))
        self.assertFalse(module._recorded_repair_requirement(_request_kwargs(deployment="dep-2")))

        # Next turn, same session and route: one call, already rewritten.
        later = _request_kwargs()
        sent.clear()

        async def call_again() -> str:
            sent.append(copy.deepcopy(later["messages"]))
            return "ok"

        self.assertEqual("ok", await module.call_with_signature_repair(call_again, later))
        self.assertEqual(1, len(sent))
        self.assertEqual([""], _signatures(sent[0]))

    async def test_another_sessions_history_is_left_signed(self) -> None:
        """A requirement belongs to one history, not to the route.

        The session that had to be rewritten says nothing about a session whose
        signatures this route issued itself: rewriting that one would discard a
        verifying signature and rebuild its prompt cache for no reason.
        """

        module = self.module
        self.module._REQUIREMENTS["session:sess-a:route:dep-1"] = {
            "status": "unsigned",
            "expires_at": 9_999_999_999.0,
        }
        other = _request_kwargs(session="sess-b")
        sent: list = []

        async def call() -> str:
            sent.append(copy.deepcopy(other["messages"]))
            return "ok"

        self.assertEqual("ok", await module.call_with_signature_repair(call, other))
        self.assertEqual(1, len(sent))
        self.assertEqual(["CAQS-sig-from-another-channel"], _signatures(sent[0]))

    async def test_a_route_that_refuses_the_rewrite_is_not_asked_twice(self) -> None:
        """An outage must not buy one extra attempt on every turn.

        The masked envelope is also what a dead channel says, so the rewrite
        cannot be trusted to be the answer.  One attempt decides it; a second
        refusal is remembered briefly, and the next turn is sent once, as the
        client wrote it.
        """

        module = self.module
        kwargs = _request_kwargs()
        sent: list = []

        async def always_refuses() -> str:
            sent.append(copy.deepcopy(kwargs["messages"]))
            raise _MaskedSignatureRefusal()

        with self.assertRaises(_MaskedSignatureRefusal):
            await module.call_with_signature_repair(always_refuses, kwargs)
        self.assertEqual(2, len(sent))
        self.assertEqual([""], _signatures(sent[1]))

        module._REQUIREMENTS.clear()
        module._REQUIREMENTS_LOADED_AT = 0.0
        self.assertTrue(self.module._record_declined_requirement(_request_kwargs()))
        self.assertFalse(self.module._recorded_repair_requirement(_request_kwargs()))

        # The next turn pays exactly one attempt and keeps its signatures.
        later = _request_kwargs()
        sent.clear()

        async def fails_once() -> str:
            sent.append(copy.deepcopy(later["messages"]))
            raise _MaskedSignatureRefusal()

        with self.assertRaises(_MaskedSignatureRefusal):
            await module.call_with_signature_repair(fails_once, later)
        self.assertEqual(1, len(sent))
        self.assertEqual(["CAQS-sig-from-another-channel"], _signatures(sent[0]))
