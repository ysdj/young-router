"""Exercise the pinned Router with HTTP, outside the hook test doubles."""

from pathlib import Path
import os
import subprocess
import sys
import textwrap
import unittest


class RouterRuntimeIntegrationTests(unittest.TestCase):
    def test_real_router_enforces_and_releases_deployment_concurrency(self) -> None:
        script = textwrap.dedent(
            '''
            import asyncio
            from datetime import datetime, timezone
            from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
            import json
            import os
            import tempfile
            import threading
            import time

            with tempfile.TemporaryDirectory(prefix="young-router-cap-") as root:
                os.environ.update({
                    "LITELLM_RUNTIME_ROOT": root,
                    "YOUNG_ROUTER_HOME": root,
                    "YOUNG_ROUTER_DEPLOYMENT_CONCURRENCY_FILE": root + "/concurrency.json",
                    "LITELLM_RECENT_REQUESTS_LOG": root + "/recent.jsonl",
                })
                import litellm
                from litellm import Router
                from young_router.callbacks import YoungRouterHook
                from young_router.proxy import routing

                class Upstream(BaseHTTPRequestHandler):
                    active = 0
                    peak = 0
                    calls = 0
                    lock = threading.Lock()

                    def log_message(self, *args):
                        pass

                    def do_POST(self):
                        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                        assert not any(key.startswith("_young_router") for key in payload), payload
                        with self.lock:
                            type(self).active += 1
                            type(self).calls += 1
                            type(self).peak = max(self.peak, self.active)
                        time.sleep(0.3)
                        response = json.dumps({
                            "id": "chatcmpl-local", "object": "chat.completion",
                            "created": 1, "model": "audit",
                            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                        }).encode()
                        with self.lock:
                            type(self).active -= 1
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(response)))
                        self.end_headers()
                        self.wfile.write(response)

                server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                hook = YoungRouterHook()
                litellm.callbacks = [hook]
                router = Router(model_list=[{
                    "model_name": "audit",
                    "litellm_params": {"model": "openai/audit", "api_key": "local-only", "api_base": f"http://127.0.0.1:{server.server_port}/v1"},
                    "model_info": {"id": "audit-route", "max_concurrency": 1},
                }], num_retries=0, disable_cooldowns=True)

                async def request():
                    data = {"model": "audit", "messages": [{"role": "user", "content": "hello"}]}
                    changed = await hook.async_pre_call_hook(None, None, data, "acompletion")
                    data = changed or data
                    started = datetime.now(timezone.utc)
                    try:
                        return await router.acompletion(**data)
                    except Exception as exc:
                        # End the rejected turn through the terminal callback,
                        # withdrawing its recovery queue ticket as the proxy does.
                        await hook.async_log_failure_event(data, exc, started, datetime.now(timezone.utc))
                        raise

                async def main():
                    results = await asyncio.gather(*(request() for _ in range(3)), return_exceptions=True)
                    successes = [result for result in results if not isinstance(result, Exception)]
                    assert len(successes) == 1, results
                    assert all(routing._is_model_concurrency_rejection_error(result) for result in results if isinstance(result, Exception)), results
                    # LiteLLM schedules completion callbacks on background tasks.
                    deadline = time.monotonic() + 3
                    while routing._deployment_in_flight_count("id:audit-route") and time.monotonic() < deadline:
                        await asyncio.sleep(0.01)
                    assert routing._deployment_in_flight_count("id:audit-route") == 0
                    response = await request()
                    assert response.choices[0].message.content == "ok"
                    assert Upstream.peak == 1, Upstream.peak
                    assert Upstream.calls == 2, Upstream.calls

                try:
                    asyncio.run(main())
                finally:
                    server.shutdown()
                    server.server_close()
                    worker.join()
            '''
        )
        result = subprocess.run(
            [os.environ.get("LITELLM_TEST_PYTHON", sys.executable), "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
