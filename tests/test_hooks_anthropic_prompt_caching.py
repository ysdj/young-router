"""A Responses turn must be able to ask Claude for a prompt cache.

Anthropic caches only what the request marks with ``cache_control``, and the
native Messages surface is the only one that can state the mark.  A Codex
``/v1/responses`` turn has no field for it, so every turn of a thread reached
the route as fresh input: measured 2026-11-05, the same ~7.4k-token prompt
written through ``/v1/messages`` created a 7,360-token cache and read it back,
while three identical ``/v1/responses`` turns reported ``cache_write_tokens:
0`` and ``cached_tokens: 0`` each.

The probe runs in its own interpreter: another suite replaces ``litellm`` in
``sys.modules`` with a stub for the length of the process, and a real Responses
call then fails inside LiteLLM on the stub's non-package ``litellm.proxy``.
This drives the production install path (``patches.install_all``) and the real
LiteLLM against a local Anthropic-shaped endpoint, so it fails if the switch
stops reaching the wire -- not merely if the flag stops being set.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]

PROBE = textwrap.dedent(
    r"""
    import asyncio, json, threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import litellm
    from young_router.proxy import patches

    patches.install_all()

    bodies = []
    paths = []

    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("content-length") or 0))
            paths.append(self.path)
            bodies.append(json.loads(raw))
            payload = json.dumps({
                "id": "msg_young_router_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": "OK"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 2, "output_tokens": 2},
            }).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % server.server_address[1]

    async def main():
        await litellm.aresponses(
            model="anthropic/claude-opus-5-5",
            api_base=base,
            api_key="sk-test-anthropic",
            input=[{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
            max_output_tokens=16,
        )
        await litellm.acompletion(
            model="anthropic/claude-opus-5-5",
            api_base=base,
            api_key="sk-test-anthropic",
            messages=[{"role": "user", "content": [{
                "type": "text",
                "text": "hi",
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }]}],
            max_tokens=16,
        )

    asyncio.run(main())
    server.shutdown()
    print(json.dumps({"paths": paths, "bodies": bodies}))
    """
)


def _cache_marks(body: dict[str, Any]) -> list[Any]:
    marks: list[Any] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "cache_control":
                    marks.append(item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(body.get("system"))
    walk(body.get("messages"))
    return marks


class AnthropicPromptCachingTests(unittest.TestCase):
    def test_a_responses_turn_marks_the_prefix_and_keeps_client_marks(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-c", PROBE],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=180,
            env={
                **os.environ,
                # The bundled runtime puts LiteLLM's site-packages on this path
                # in the launcher; ``sys.executable`` is the interpreter it
                # exec'd, so the child has to inherit that path rather than
                # rebuild it.
                "PYTHONPATH": str(ROOT)
                + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
                "LITELLM_LOCAL_MODEL_COST_MAP": "true",
            },
        )
        self.assertEqual(0, completed.returncode, completed.stderr[-4000:])
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(["/v1/messages", "/v1/messages"], result["paths"])

        responses_body, chat_body = result["bodies"]
        responses_marks = _cache_marks(responses_body)
        self.assertEqual(1, len(responses_marks), responses_body)
        self.assertEqual("ephemeral", responses_marks[0]["type"])

        chat_marks = _cache_marks(chat_body)
        self.assertEqual(1, len(chat_marks), chat_body)
        self.assertEqual({"type": "ephemeral", "ttl": "1h"}, chat_marks[0])


if __name__ == "__main__":
    unittest.main()
