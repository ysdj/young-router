from __future__ import annotations

import http.client
import inspect
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
import urllib.request
import uuid
from unittest import mock

from young_router.core import (
    CoreError,
    CoreIPCClient,
    CoreIPCServer,
    CoreStore,
    IPCError,
    MemoryDomain,
    ProtocolError,
    RequestEnvelope,
    ResponseEnvelope,
    decode_message,
    encode_message,
    load_protocol_schema,
)
from young_router.core import __main__ as core_main
from young_router.core.protocol import validate_method_result
from young_router.core.persistence import AtomicJSONStore, PersistenceError
from young_router.core.security import REDACT_TEXT, redact, safe_error_message


class SnapshotPollCacheTests(unittest.TestCase):
    """A poll of a revision nobody moved is a copy, not a rebuild.

    Measured on lv: one `/ipc` snapshot cost ~80 ms, of which ~14 ms was two
    Python walks over the 273 KB payload -- `ResponseEnvelope` validating it and
    the encoder checking it again.  The window polls the same revision many
    times a second, so those bytes are worth keeping.
    """

    def _server(self):
        with tempfile.TemporaryDirectory() as directory:
            core = CoreStore(domains=[MemoryDomain("probe")])
            server = CoreIPCServer(core)
            server.start()
            self.addCleanup(server.stop)
        return core, server

    def test_the_second_poll_of_a_revision_is_the_same_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            core = CoreStore(domains=[MemoryDomain("probe")])
            server = CoreIPCServer(core)
            server.start()
            self.addCleanup(server.stop)
            token = server._mint_web_session()

            def poll(request_id: str) -> bytes:
                request = RequestEnvelope.from_mapping(
                    {"protocol_version": 1, "request_id": request_id, "method": "snapshot", "params": {}}
                )
                answer = server.handle_request(request, session_token=token)
                if hasattr(answer, "body"):
                    return answer.body
                from young_router.core.ipc import _encode_response_body

                return _encode_response_body(answer.to_mapping())

            first = poll("first")
            expected = json.loads(first)
            self.assertTrue(expected["ok"])
            self.assertEqual("snapshot", "snapshot")
            self.assertEqual(set(expected["result"]), {"snapshot"})
            self.assertIsNotNone(server._snapshot_body)

            second = poll("second")
            self.assertEqual("second", json.loads(second)["request_id"])
            # The one thing that must not change: the payload the window reads.
            self.assertEqual(expected["result"], json.loads(second)["result"])
            # And the cached answer is byte-for-byte what a fresh encode says.
            from young_router.core.ipc import _encode_response_body
            from young_router.core.protocol import ResponseEnvelope

            fresh = _encode_response_body(
                ResponseEnvelope.success("second", expected["result"]).to_mapping()
            )
            self.assertEqual(fresh, second)

            # A revision that moved is a different projection: the cache is
            # dropped with it, never served one revision late.  The revision
            # itself is inside the payload, so that is the field to read.
            core._revision += 1
            third = poll("third")
            self.assertEqual(core.revision, json.loads(third)["result"]["snapshot"]["revision"])
            self.assertEqual(core.revision, server._snapshot_body[0])

    def test_a_cached_poll_is_not_built_by_the_store_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = MemoryDomain("probe")
            core = CoreStore(domains=[domain])
            server = CoreIPCServer(core)
            server.start()
            self.addCleanup(server.stop)
            token = server._mint_web_session()

            calls = []
            original = core.snapshot
            core.snapshot = lambda: (calls.append(1), original())[1]
            for index in range(4):
                request = RequestEnvelope.from_mapping(
                    {"protocol_version": 1, "request_id": f"r{index}", "method": "snapshot", "params": {}}
                )
                server.handle_request(request, session_token=token)
            self.assertEqual(1, len(calls))

class CoreProtocolTests(unittest.TestCase):
    def test_ipc_server_uses_a_short_shutdown_poll_interval(self) -> None:
        core = CoreStore(domains=[MemoryDomain("language", {"choice": "system"})])
        server = CoreIPCServer(core)
        served = threading.Event()
        observed: dict[str, float] = {}

        def serve_forever(_server: object, *, poll_interval: float) -> None:
            observed["poll_interval"] = poll_interval
            served.set()

        with (
            mock.patch("young_router.core.ipc.http.server.ThreadingHTTPServer.serve_forever", serve_forever),
            mock.patch("young_router.core.ipc.http.server.ThreadingHTTPServer.shutdown"),
        ):
            server.start()
            self.assertTrue(served.wait(1.0))
            server.stop()

        self.assertEqual(0.05, observed["poll_interval"])

    def test_core_parent_watchdog_stops_after_direct_parent_changes(self) -> None:
        stop = threading.Event()
        with mock.patch.object(core_main.os, "getppid", side_effect=[4321, 9876]):
            core_main._watch_parent(4321, stop, poll_interval=0)

        self.assertTrue(stop.is_set())

    def test_core_parent_watchdog_checks_once_a_second(self) -> None:
        """An idle Core sleeps between parent checks instead of spinning on them."""

        default = inspect.signature(core_main._watch_parent).parameters["poll_interval"].default

        self.assertEqual(default, 1.0)

    def test_core_parent_watchdog_uses_a_process_handle_on_windows(self) -> None:
        stop = threading.Event()
        with (
            mock.patch.object(core_main.os, "name", "nt"),
            mock.patch.object(core_main, "_watch_windows_parent") as watch_windows_parent,
        ):
            core_main._watch_parent(4321, stop, poll_interval=0.5)

        watch_windows_parent.assert_called_once_with(4321, stop, poll_interval=0.5)

    def test_core_parent_pid_must_be_positive(self) -> None:
        parser = core_main.build_parser()
        self.assertEqual(4321, parser.parse_args(["--parent-pid", "4321"]).parent_pid)
        with self.assertRaises(core_main.argparse.ArgumentTypeError):
            core_main._positive_pid("0")

    def test_core_rejects_a_stale_parent_before_initialization(self) -> None:
        with (
            mock.patch.object(core_main.os, "name", "posix"),
            mock.patch.object(core_main.os, "getppid", return_value=9876),
            mock.patch.object(core_main.CoreStore, "with_default_domains") as create_core,
        ):
            self.assertEqual(0, core_main.run(["--parent-pid", "4321"]))

        create_core.assert_not_called()

    def test_core_parent_watchdog_runs_ordered_shutdown(self) -> None:
        events: list[str] = []
        record_states: list[str] = []
        supervisor_head_start: list[float | None] = []
        server_started = threading.Event()

        class FakeCore:
            def webdav_scheduler(self) -> "FakeScheduler":
                return FakeScheduler("scheduler")

            def image_context_sweeper(self) -> "FakeScheduler":
                return FakeScheduler("cleanup")

            def service_supervisor(self, *, initial_delay: float | None = None) -> "FakeScheduler":
                supervisor_head_start.append(initial_delay)
                return FakeScheduler("supervisor")

            def shutdown(self, *, stopped_by: str = "app_exit") -> None:
                # SIGTERM is how a host or a script *replaces* Core; the record
                # must say so rather than read as the user's own Stop.
                record_states.append(stopped_by)
                events.append("core.shutdown")

        class FakeScheduler:
            def __init__(self, name: str) -> None:
                self._name = name

            def start(self) -> None:
                events.append(f"{self._name}.start")

            def stop(self) -> None:
                events.append(f"{self._name}.stop")

        class FakeServer:
            def __init__(self, _core: object, *, address: str, port: int, web_sessions_path: object = None) -> None:
                self.address = address
                self.port = port
                self.web_sessions_path = web_sessions_path

            def start(self) -> None:
                events.append("server.start")
                server_started.set()

            def stop(self) -> None:
                events.append("server.stop")

        def direct_parent() -> int:
            if threading.current_thread().name == "litellm-core-parent-watchdog":
                self.assertTrue(server_started.wait(1.0))
                return 9876
            return 4321

        with (
            mock.patch.object(core_main.os, "getppid", side_effect=direct_parent),
            mock.patch.object(core_main.CoreStore, "with_default_domains", return_value=FakeCore()) as create_core,
            mock.patch.object(core_main, "CoreIPCServer", FakeServer),
            mock.patch.object(core_main.signal, "signal"),
        ):
            self.assertEqual(0, core_main.run(["--parent-pid", "4321"]))

        # Every loop starts with the IPC endpoint; the store stops the ones it
        # owns (the proxy supervisor included) before it shuts the proxy down.
        self.assertEqual(
            [
                "server.start",
                "scheduler.start",
                "cleanup.start",
                "supervisor.start",
                "scheduler.stop",
                "cleanup.stop",
                "core.shutdown",
                "server.stop",
            ],
            events,
        )
        self.assertEqual(["core_replaced"], record_states)
        # A native host is a window: it launches the proxy itself, so the loop
        # keeps its head start instead of racing that launch.
        self.assertEqual([None], supervisor_head_start)
        self.assertTrue(create_core.call_args.kwargs["reset_transient_routing_state"])

    def test_a_server_unit_states_that_it_supervises_the_proxy(self) -> None:
        """A machine with no window still owns the proxy it publishes.

        The proxy follows the app: a gateway under systemd has no window to
        click Start, so the loop that keeps the proxy running has to be entered
        by the unit saying so.  It is stated with a flag rather than by
        inventing the endpoint descriptor a native host would have written --
        and it stays off by default, because a bare CLI Core is a diagnostic
        process that must not spawn a proxy.
        """

        def run_with(argv: list[str]) -> tuple[list[str], list[float | None]]:
            started: list[str] = []
            supervisor_head_start: list[float | None] = []

            class FakeScheduler:
                def __init__(self, name: str) -> None:
                    self._name = name

                def start(self) -> None:
                    started.append(self._name)

                def stop(self) -> None:
                    pass

            class FakeCore:
                def webdav_scheduler(self) -> FakeScheduler:
                    return FakeScheduler("scheduler")

                def image_context_sweeper(self) -> FakeScheduler:
                    return FakeScheduler("cleanup")

                def service_supervisor(self, *, initial_delay: float | None = None) -> FakeScheduler:
                    supervisor_head_start.append(initial_delay)
                    return FakeScheduler("supervisor")

                def shutdown(self, *, stopped_by: str = "app_exit") -> None:
                    pass

            class FakeServer:
                def __init__(self, _core: object, *, address: str, port: int, web_sessions_path: object = None) -> None:
                    self.address = address
                    self.port = port
                    self.web_sessions_path = web_sessions_path

                def start(self) -> None:
                    started.append("server")

                def stop(self) -> None:
                    pass

            class StopAtOnce:
                """The stop event, which answers its own wait.

                ``run`` waits on this event for its whole life, and a bare CLI
                Core has nothing that would ever set it here.  The stand-in
                states "stop" the moment something waits for it, so the startup
                and teardown above still run in order instead of the test
                blocking on a process nobody asked to end.
                """

                def __init__(self) -> None:
                    self._stopped = False

                def is_set(self) -> bool:
                    return self._stopped

                def set(self) -> None:
                    self._stopped = True

                def wait(self, _timeout: object = None) -> bool:
                    self._stopped = True
                    return True

            with (
                mock.patch.object(core_main.os, "getppid", return_value=os.getpid()),
                mock.patch.object(core_main.CoreStore, "with_default_domains", return_value=FakeCore()),
                mock.patch.object(core_main, "CoreIPCServer", FakeServer),
                mock.patch.object(core_main.signal, "signal"),
                mock.patch.object(core_main.threading, "Event", StopAtOnce),
            ):
                self.assertEqual(0, core_main.run(argv))
            return started, supervisor_head_start

        started, head_start = run_with(["--manage-service"])
        self.assertIn("supervisor", started)
        # A unit that said so has no window, so no other starter exists on this
        # machine: the loop's first pass is due at once instead of five seconds
        # into a restart window, spent waiting for a launch that cannot come.
        self.assertEqual([0.0], head_start)
        started, head_start = run_with([])
        self.assertNotIn("supervisor", started)
        self.assertEqual([], head_start)

    def test_shared_schema_matches_python_and_typescript_method_contract(self) -> None:
        schema = load_protocol_schema()
        self.assertEqual(1, schema["protocol_version"])
        self.assertEqual(
            ["snapshot", "disk_state", "logs", "editor", "files", "dispatch", "subscribe", "validate", "apply", "reload", "probe", "export", "import_preview", "import", "web_launch_url"],
            schema["methods"],
        )
        typescript = (
            Path(__file__).resolve().parents[1]
            / "rn"
            / "packages"
            / "shared"
            / "src"
            / "types.ts"
        ).read_text(encoding="utf-8")
        for method in schema["methods"]:
            self.assertIn(f'| "{method}"', typescript)
        self.assertEqual(len(schema["methods"]), len(schema["request"]["allOf"]))
        # The request envelope's own method enum is the same vocabulary: a
        # method that is listed and contracted but missing here would be
        # rejected by any consumer that validates the published request shape,
        # even though Core serves it.
        self.assertEqual(schema["methods"], schema["request"]["properties"]["method"]["enum"])
        contracted = [
            clause["if"]["properties"]["method"]["const"]
            for clause in schema["request"]["allOf"]
        ]
        self.assertEqual(schema["methods"], contracted)
        self.assertFalse(schema["$defs"]["applyParams"]["additionalProperties"])
        self.assertIn("domains", schema["$defs"]["applyParams"]["properties"])
        self.assertEqual(
            {"revision", "applied", "status", "completed_operations", "pending_operations", "issues"},
            set(schema["$defs"]["applyResult"]["required"]),
        )
        self.assertFalse(schema["$defs"]["applyIssue"]["additionalProperties"])
        self.assertNotIn("api_key", schema["$defs"]["applyIssue"]["properties"])

    def test_request_and_response_are_versioned_and_strict(self) -> None:
        request = RequestEnvelope.from_mapping(
            {
                "protocol_version": 1,
                "request_id": "request-1",
                "method": "snapshot",
                "params": {},
            }
        )
        self.assertEqual(request.to_mapping()["protocol_version"], 1)
        self.assertEqual(request.method, "snapshot")
        response = ResponseEnvelope.success(request.request_id, {"snapshot": {}})
        self.assertEqual(ResponseEnvelope.from_mapping(response.to_mapping()).result, {"snapshot": {}})
        with self.assertRaises(ProtocolError):
            RequestEnvelope.from_mapping({**request.to_mapping(), "extra": True})
        with self.assertRaises(ProtocolError):
            decode_message(b'{"protocol_version":1,"request_id":"x","method":"snapshot","params":{},"params":{}}')

    def test_runtime_enforces_every_method_params_schema(self) -> None:
        valid = {
            "snapshot": {},
            "disk_state": {"domains": ["codex", "claude"]},
            "logs": {"tab": "requests"},
            "editor": {"domain": "codex", "document": "config"},
            "files": {},
            "dispatch": {"action": {"type": "set", "domain": "language", "payload": {}}},
            "subscribe": {"topics": ["snapshot"]},
            "validate": {"domain": "language", "revision": 0},
            "apply": {"domain": "language", "revision": 0},
            "reload": {"domain": "language", "revision": 0},
            "probe": {"domain": "providers_models", "provider_id": "primary", "model_id": "default-chat"},
            "export": {"sections": ["language"], "destination_token": "destination"},
            "import_preview": {"source_token": "source", "revision": 0},
            "import": {"import_plan_token": "plan", "sections": ["language"], "revision": 0},
            "web_launch_url": {},
        }
        invalid = {
            "snapshot": {"stale": True},
            "disk_state": {"domains": []},
            "logs": {"tab": "unknown"},
            "editor": {"domain": "runtime", "document": "config"},
            "files": {"stale": True},
            "dispatch": {"action": {"type": "", "unexpected": True}},
            "subscribe": {"topics": ["topic"] * 33},
            "validate": {"domain": "unknown"},
            "apply": {"domain": "language", "domains": ["language"], "revision": 0},
            "reload": {"revision": -1},
            "probe": {"domain": "runtime"},
            "export": {"sections": ["language", "language"], "destination_token": "destination"},
            "import_preview": {"source_token": "source"},
            "import": {"import_plan_token": "plan", "sections": ["language"]},
            "web_launch_url": {"stale": True},
        }

        self.assertEqual(set(valid), set(load_protocol_schema()["methods"]))
        for method, params in valid.items():
            with self.subTest(method=method, case="valid"):
                RequestEnvelope.from_mapping(
                    {"protocol_version": 1, "request_id": f"valid-{method}", "method": method, "params": params}
                )
        RequestEnvelope.from_mapping(
            {
                "protocol_version": 1,
                "request_id": "valid-editor-stage",
                "method": "editor",
                "params": {"editor_token": "token", "text": "{}\n"},
            }
        )
        with self.assertRaisesRegex(ProtocolError, r"^editor params do not match"):
            RequestEnvelope.from_mapping(
                {
                    "protocol_version": 1,
                    "request_id": "invalid-editor-mixed-operation",
                    "method": "editor",
                    "params": {
                        "domain": "codex",
                        "document": "config",
                        "editor_token": "token",
                        "text": "{}\n",
                    },
                }
            )
        for method, params in invalid.items():
            with self.subTest(method=method, case="invalid"):
                with self.assertRaisesRegex(ProtocolError, rf"^{method} params do not match"):
                    RequestEnvelope.from_mapping(
                        {"protocol_version": 1, "request_id": f"invalid-{method}", "method": method, "params": params}
                    )

    def test_runtime_enforces_every_method_result_schema(self) -> None:
        valid = {
            "snapshot": {"snapshot": {}},
            "disk_state": {"revision": 0, "disk": {}},
            "logs": {"changed": True, "revision": 1, "log": {"tab": "requests", "available": False, "paused": False, "line_count": 0, "records": [], "filter": "", "limit": 10000}},
            "editor": {"domain": "codex", "document": "config", "editor_token": "token", "revision": 0, "text": "model = \"example\"\n", "baseline": "model = \"example\"\n"},
            "files": {
                "revision": 0,
                "files": [
                    {
                        "id": "pi_settings",
                        "client": "pi",
                        "domain": "clients",
                        "document": "pi_settings",
                        "name": "settings.json",
                        "path": "/Users/example/.pi/agent/settings.json",
                        "display_path": "/Users/example/.pi/agent/settings.json",
                        "language": "json",
                        "exists": False,
                    }
                ],
            },
            "dispatch": {"revision": 0, "action_summary": {"operation": "fetch_models"}},
            "subscribe": {"subscription_id": "subscription"},
            "validate": {"validate": {}},
            "apply": {
                "revision": 0,
                "applied": True,
                "domains": ["language"],
                "status": "applied",
                "completed_operations": 0,
                "pending_operations": 0,
                "issues": [],
            },
            "reload": {"revision": 0},
            "probe": {"ok": False, "protocols": []},
            "export": {"revision": 0, "section_count": 0, "sections": ["language"]},
            "import_preview": {"revision": 0, "import_plan_token": "plan", "detected_sections": ["language"], "preview": {}},
            "import": {"revision": 0, "draft_domains": ["language"], "preview": {}},
            "web_launch_url": {"url": "http://127.0.0.1:12390/ui/#launch=token"},
        }
        invalid = {
            "snapshot": {"snapshot": []},
            "disk_state": {"revision": -1, "disk": {}},
            "logs": {"changed": "yes", "revision": 1, "log": None},
            "editor": {"domain": "codex", "document": "config", "editor_token": "token", "revision": 0},
            "files": {"revision": 0, "files": [{"id": "pi_settings"}]},
            "dispatch": {"revision": -1, "action_summary": "fetch_models"},
            "subscribe": {"subscription_id": 1},
            "validate": {"validate": []},
            "apply": {
                "revision": 0,
                "applied": False,
                "status": "partial",
                "completed_operations": -1,
                "pending_operations": 1,
                "issues": [],
            },
            "reload": {"revision": "0"},
            "probe": {"ok": True, "protocols": [1]},
            "export": {"revision": 0, "section_count": -1},
            "import_preview": {"revision": 0, "import_plan_token": "plan", "detected_sections": [], "preview": {}},
            "import": {"revision": 0, "draft_domains": ["unknown"], "preview": {}},
            "web_launch_url": {"url": ""},
        }

        self.assertEqual(set(valid), set(load_protocol_schema()["methods"]))
        for method, result in valid.items():
            with self.subTest(method=method, case="valid"):
                validate_method_result(method, result)
        for method, result in invalid.items():
            with self.subTest(method=method, case="invalid"):
                with self.assertRaisesRegex(ProtocolError, rf"^{method} result does not match") as raised:
                    validate_method_result(method, result)
                self.assertEqual("invalid_response", raised.exception.code)
        with self.assertRaisesRegex(ProtocolError, r"^apply result does not match"):
            validate_method_result(
                "apply",
                {
                    "revision": 0,
                    "applied": False,
                    "status": "partial",
                    "completed_operations": 0,
                    "pending_operations": 1,
                    "issues": [{"code": "relay_apply", "message": "Retry required", "api_key": "must-not-leak"}],
                },
            )
        with self.assertRaisesRegex(ProtocolError, r"^reload result does not match"):
            validate_method_result("reload", {"revision": 0, "action_summary": {"operation": "fetch_models"}})
        # The disk map's object-valued `additionalProperties` is what validates
        # each domain's own record, so a partial record is caught here instead
        # of reaching the pane as an undefined generation.
        validate_method_result("disk_state", {"revision": 0, "disk": {"language": {"changed": True, "generation": 3}}})
        for partial in ({"language": {}}, {"language": {"changed": True}}, {"language": {"changed": True, "generation": 3, "extra": 1}}):
            with self.subTest(partial=partial):
                with self.assertRaisesRegex(ProtocolError, r"^disk_state result does not match"):
                    validate_method_result("disk_state", {"revision": 0, "disk": partial})

    def test_snapshot_params_cannot_contain_extra_fields(self) -> None:
        with self.assertRaises(ProtocolError) as raised:
            RequestEnvelope.from_mapping(
                {
                    "protocol_version": 1,
                    "request_id": "snapshot-extra",
                    "method": "snapshot",
                    "params": {"revision": 0},
                }
            )
        self.assertEqual("invalid_request", raised.exception.code)

    def test_protocol_payload_round_trip_rejects_nan(self) -> None:
        with self.assertRaises(ProtocolError):
            encode_message({"protocol_version": 1, "value": float("nan")})

    def test_redaction_removes_credentials_and_private_paths(self) -> None:
        secret = "synthetic-core-secret"
        value = redact(
            {
                "api_key": secret,
                "token": secret,
                "key_name": "primary",
                "config_text": "master_key = 'do-not-return'",
                "path": "/private/user/config.json",
            },
            known_secrets=(secret,),
        )
        self.assertEqual("configured", value["api_key"])
        self.assertEqual("configured", value["token"])
        self.assertEqual("primary", value["key_name"])
        self.assertEqual("configured", value["path"])
        self.assertNotIn(secret, json.dumps(value))
        self.assertNotIn("/private/user", safe_error_message(f"failed at /private/user/config.json: {secret}"))

    def test_redaction_keeps_the_limits_it_must_not_hide(self) -> None:
        """A token *limit* is configuration, not a credential.

        The one ``token`` marker cannot tell a budget from a secret, and reading
        every token-shaped name as a secret replaced the window this app hands
        Codex with the presence marker: ``max_input_tokens`` arrived at the pane
        as the string ``configured``, so the field painted empty and the value
        the user had set was destroyed on the next write.  A name that states a
        quantity therefore survives redaction verbatim, while every name that
        presents a credential stays redacted.
        """

        limits = {
            "max_input_tokens": 372000,
            "model_auto_compact_token_limit": 80000,
            "tool_output_token_limit": 4096,
            "token_budget": 1000,
            "token_count": 12,
        }
        self.assertEqual(limits, redact(limits))
        # A camelCase name is the same key as its snake_case sibling, and the
        # JSON document this app stores spells its budget `maxTokens`.  Reading
        # only the underscore form left that key classified as a credential, so
        # the stored document reached every snapshot rewritten to
        # `"maxTokens":configured` — text that is no longer JSON, which the
        # document's own editor then opened on.
        camel = {
            "maxTokens": 4096,
            "timeoutSeconds": 45,
            "tokenBudget": 1000,
            "localLmStudio": {"enabled": False},
        }
        self.assertEqual(camel, redact(camel))
        document = '{"enabled":true,"maxTokens":4096,"timeoutSeconds":45}'
        self.assertEqual(document, redact(document))
        for key in ("apiKey", "accessToken", "providerAuthToken"):
            self.assertEqual({key: "configured"}, redact({key: "synthetic-credential"}), key)
        for key in (
            "input_tokens",
            "output_tokens",
            "prompt_tokens",
            "completion_tokens",
            "cached_tokens",
            "reasoning_tokens",
            "total_tokens",
            "cache_creation_tokens",
            "cache_read_tokens",
            "codex_compaction_max_output_tokens",
        ):
            self.assertEqual({key: 7}, redact({key: 7}), key)

        # A name that presents a credential is still a secret, including the
        # ones spelled beside the quantity names above.
        for key in (
            "token",
            "tokens",
            "access_token",
            "refresh_token",
            "bootstrap_token",
            "id_token",
            "auth_token",
            "provider_auth_token",
            "refresh_token_expires_at",
            "bootstrap_token_ttl_seconds",
            "file_token",
        ):
            self.assertEqual({key: "configured"}, redact({key: "synthetic-credential"}), key)

    def test_redaction_preserves_urls_but_removes_sensitive_query_values(self) -> None:
        redacted = REDACT_TEXT(
            "probe https://example.test/v1/models?token=synthetic-token&region=us failed at /private/user/config.yaml"
        )

        self.assertIn("https://example.test/v1/models?token=configured&region=us", redacted)
        self.assertIn("<private-path>", redacted)
        self.assertNotIn("synthetic-token", redacted)

    def test_redaction_removes_generic_sensitive_key_value_forms(self) -> None:
        token = "synthetic-token-value"
        password = "synthetic-password-value"
        api_key = "synthetic-api-key-value"
        redacted = REDACT_TEXT(
            f"token={token} password: {password} api_key='{api_key}' "
            f"ANTHROPIC_AUTH_TOKEN={token} token_configured=true"
        )

        self.assertNotIn(token, redacted)
        self.assertNotIn(password, redacted)
        self.assertNotIn(api_key, redacted)
        self.assertIn("token=configured", redacted)
        self.assertIn("password: configured", redacted)
        self.assertIn("api_key=configured", redacted)
        self.assertIn("ANTHROPIC_AUTH_TOKEN=configured", redacted)
        self.assertIn("token_configured=true", redacted)


class CorePersistenceAndStoreTests(unittest.TestCase):
    def test_multidomain_apply_rolls_back_files_state_metadata_and_events(self) -> None:
        class FileDomain(MemoryDomain):
            def __init__(self, name: str, path: Path, *, fail: bool = False):
                super().__init__(name, {"value": "saved"})
                self.settings_path = path
                self.fail = fail

            def apply(self, payload: object | None = None) -> dict[str, object]:
                self.settings_path.write_text(str(self._draft["value"]), encoding="utf-8")
                self._raw = dict(self._draft)
                self.revision += 1
                if self.fail:
                    raise ValueError("synthetic second apply failure")
                return {"applied": True}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_path = root / "first.json"
            second_path = root / "second.json"
            metadata = root / "core-state.json"
            first_path.write_text("first-before", encoding="utf-8")
            second_path.write_text("second-before", encoding="utf-8")
            first = FileDomain("providers_models", first_path)
            second = FileDomain("runtime", second_path, fail=True)
            core = CoreStore(metadata_path=metadata, domains=[first, second])
            core.set_service_status("stopped")
            core.dispatch({"domain": "providers_models", "type": "set", "payload": {"value": "first-after"}})
            core.dispatch({"domain": "runtime", "type": "set", "payload": {"value": "second-after"}})
            before = core.snapshot()
            before_metadata = metadata.read_bytes()
            events: list[dict[str, object]] = []
            unsubscribe = core.subscribe(events.append)
            self.addCleanup(unsubscribe)

            with self.assertRaises(Exception):
                core.apply(domains=["providers_models", "runtime"], revision=core.revision)

            self.assertEqual("first-before", first_path.read_text(encoding="utf-8"))
            self.assertEqual("second-before", second_path.read_text(encoding="utf-8"))
            self.assertEqual(before, core.snapshot())
            self.assertEqual(before_metadata, metadata.read_bytes())
            self.assertEqual([], events)

    def test_multidomain_apply_rolls_back_codex_runtime_context_file(self) -> None:
        class CodexFileDomain(MemoryDomain):
            def __init__(self, runtime_config_path: Path, codex_home: Path):
                super().__init__("codex", {"value": "saved"})
                self.runtime_config_path = runtime_config_path
                self.codex_home = codex_home

            def apply(self, payload: object | None = None) -> dict[str, object]:
                self.runtime_config_path.write_text("runtime-after", encoding="utf-8")
                self.codex_home.mkdir(parents=True, exist_ok=True)
                (self.codex_home / "config.toml").write_text("config-after", encoding="utf-8")
                return {"applied": True}

        class FailingDomain(MemoryDomain):
            def apply(self, payload: object | None = None) -> dict[str, object]:
                raise ValueError("synthetic apply failure")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_config = root / "config.yaml"
            codex_home = root / "codex"
            runtime_config.write_text("runtime-before", encoding="utf-8")
            core = CoreStore(domains=[CodexFileDomain(runtime_config, codex_home), FailingDomain("runtime")])

            with self.assertRaises(Exception):
                core.apply(domains=["codex", "runtime"], revision=core.revision)

            self.assertEqual("runtime-before", runtime_config.read_text(encoding="utf-8"))
            self.assertFalse((codex_home / "config.toml").exists())

    def test_validation_issue_paths_never_expose_local_paths(self) -> None:
        class InvalidDomain(MemoryDomain):
            def validate(self, payload: object | None = None) -> dict[str, object]:
                return {"valid": False, "issues": [{"path": "/private/user/config.yaml", "message": "invalid", "code": "invalid"}]}

        summary = CoreStore(domains=[InvalidDomain("runtime")]).validate("runtime")

        self.assertEqual("configuration", summary["issues"][0]["path"])
        self.assertNotIn("/private", json.dumps(summary))

    def test_atomic_json_store_is_private_and_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "core-state.json"
            AtomicJSONStore(target).write({"version": 1, "value": "ok"})
            self.assertEqual({"version": 1, "value": "ok"}, AtomicJSONStore(target).read())
            self.assertEqual(0o600, stat.S_IMODE(target.stat().st_mode))
            link = Path(directory) / "link.json"
            link.symlink_to(target)
            with self.assertRaises(PersistenceError):
                AtomicJSONStore(link).write({"value": "must-not-follow"})

    def test_store_stages_validates_applies_and_redacts_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "core.json"
            domain = MemoryDomain("language", {"choice": "system", "api_key": "synthetic-secret"})
            core = CoreStore(metadata_path=state_file, domains=[domain])
            initial = core.snapshot()
            self.assertEqual("configured", initial["domains"]["language"]["state"]["api_key"])
            self.assertEqual(0, initial["revision"])
            self.assertEqual(1, core.dispatch({"domain": "language", "type": "set", "payload": {"choice": "en"}})["revision"])
            self.assertTrue(core.snapshot()["drafts"]["language"]["dirty"])
            with self.assertRaises(Exception):
                core.apply("language", revision=0)
            result = core.apply("language", revision=1)
            self.assertTrue(result["applied"])
            self.assertFalse(core.snapshot()["drafts"]["language"]["dirty"])
            self.assertEqual(2, AtomicJSONStore(state_file).read()["revision"])

    def test_unreadable_core_metadata_still_starts_a_fresh_store(self) -> None:
        """Host-passed Core metadata is optional local state, not a boot gate."""

        cases = {
            "corrupt": "{not json",
            "not-an-object": "[]",
            "wrong-version": json.dumps({"version": 999, "revision": 12}),
            "invalid-revision": json.dumps({"version": 1, "revision": "many"}),
        }
        for label, text in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                state_file = Path(directory) / "core-state.json"
                state_file.write_text(text, encoding="utf-8")
                core = CoreStore(metadata_path=state_file, domains=[MemoryDomain("language", {"choice": "system"})])
                self.assertEqual(0, core.revision)
                self.assertEqual(0, core.snapshot()["revision"])
                self.assertEqual(1, core.dispatch({"domain": "language", "type": "set", "payload": {"choice": "en"}})["revision"])
                self.assertEqual(1, AtomicJSONStore(state_file).read()["revision"])

    def test_silent_external_reload_records_its_revision_for_a_replacement_core(self) -> None:
        """A clean-draft external reload bumps the revision without an event.

        The reload is silent, so windows learn that revision from their own
        responses. It must still be recorded, because a replacement Core has to
        resume at or above every revision a window has already observed.
        """

        class DiskDomain(MemoryDomain):
            def __init__(self, path: Path):
                super().__init__("language", {"choice": "system"})
                self.path = path
                self.baseline = path.read_text(encoding="utf-8")

            def external_disk_state(self) -> dict[str, bool]:
                return {"changed": self.path.read_text(encoding="utf-8") != self.baseline, "exists": True}

            def external_disk_identity(self) -> str:
                return "test-disk-domain"

            def reload(self) -> dict[str, object]:
                self.baseline = self.path.read_text(encoding="utf-8")
                self.revision += 1
                return self.snapshot()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_file = root / "core-state.json"
            source = root / "language.json"
            source.write_text('{"choice": "system"}', encoding="utf-8")
            core = CoreStore(metadata_path=state_file, domains=[DiskDomain(source)])
            self.assertEqual(0, core.revision)

            source.write_text('{"choice": "en"}', encoding="utf-8")
            core.snapshot()

            revision = core.revision
            self.assertEqual(1, revision)
            self.assertEqual(revision, AtomicJSONStore(state_file).read()["revision"])
            replacement = CoreStore(metadata_path=state_file, domains=[MemoryDomain("language", {"choice": "en"})])
            self.assertEqual(revision, replacement.revision)

    def test_core_metadata_keeps_the_revision_monotonic_for_a_replacement_core(self) -> None:
        """A host-driven Core replacement must not restart the shared revision."""

        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "core-state.json"
            first = CoreStore(metadata_path=state_file, domains=[MemoryDomain("language", {"choice": "system"})])
            first.dispatch({"domain": "language", "type": "set", "payload": {"choice": "en"}})
            first.apply("language", revision=first.revision)
            revision = first.revision
            self.assertGreater(revision, 0)

            replacement = CoreStore(metadata_path=state_file, domains=[MemoryDomain("language", {"choice": "en"})])
            self.assertEqual(revision, replacement.revision)
            # The replacement keeps answering with the continued revision, so a
            # window that observed the previous Core still sees newer snapshots.
            self.assertEqual(revision, replacement.snapshot()["revision"])
            self.assertEqual(revision + 1, replacement.dispatch({"domain": "language", "type": "set", "payload": {"choice": "system"}})["revision"])

    def test_reverting_a_draft_to_its_baseline_clears_dirty_state(self) -> None:
        core = CoreStore(domains=[MemoryDomain("language", {"choice": "system"})])

        core.dispatch({"domain": "language", "type": "set", "payload": {"choice": "en"}})
        self.assertTrue(core.snapshot()["drafts"]["language"]["dirty"])

        core.dispatch({"domain": "language", "type": "set", "payload": {"choice": "system"}})

        self.assertFalse(core.snapshot()["drafts"]["language"]["dirty"])

    def test_importing_state_equal_to_the_baseline_remains_clean(self) -> None:
        core = CoreStore(domains=[MemoryDomain("runtime", {"port": "4000"})])

        result = core.import_package(
            package={
                "format": "young-router-core-package",
                "version": 1,
                "sections": {"runtime": {"port": "4000"}},
            },
            sections=["runtime"],
            revision=core.revision,
        )

        self.assertEqual(["runtime"], result["draft_domains"])
        self.assertFalse(core.snapshot()["drafts"]["runtime"]["dirty"])

    def test_import_rejects_sections_not_detected_by_preview(self) -> None:
        core = CoreStore(
            domains=[
                MemoryDomain("providers_models", {"value": "saved"}),
                MemoryDomain("runtime", {"value": "saved"}),
            ]
        )
        package = {
            "format": "young-router-core-package",
            "version": 1,
            "sections": {"providers_models": {"value": "imported"}},
        }

        result = core.import_package(package=package, sections=["providers_models"], revision=core.revision)
        self.assertEqual(["providers_models"], result["draft_domains"])
        with self.assertRaises(Exception) as raised:
            core.import_package(
                package=package,
                sections=["runtime", "relay_accounts"],
                revision=core.revision,
            )
        self.assertEqual("invalid_sections", raised.exception.code)

    def test_language_package_export_and_import_are_selectable(self) -> None:
        from young_router.core.domains.language import LanguageSettingsDomain

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "language.json"
            source = LanguageSettingsDomain(path)
            source.dispatch("set", {"language": "zh-Hans"})
            exported = source.export(include_sensitive=True)
            target = LanguageSettingsDomain(Path(directory) / "target-language.json")

            target.import_package(exported)

            self.assertEqual({"domain": "language", "choice": "zh-Hans"}, target.export())

    def test_provider_model_summary_matches_the_typescript_contract(self) -> None:
        class ProviderDomain(MemoryDomain):
            def snapshot(self) -> dict[str, object]:
                return {
                    "domain": "providers_models",
                    "revision": 7,
                    "providers": [
                        {
                            "id": "primary",
                            "name": "Primary",
                            "models": [
                                {
                                    "deployment_id": "deployment-1",
                                    "model_name": "default-chat",
                                    "litellm_model": "openai/upstream-chat",
                                    "model_enabled": False,
                                    "order": "2",
                                    "supported_upstream_url_surfaces": ["openai/responses"],
                                }
                            ],
                        }
                    ],
                }

        model = CoreStore(domains=[ProviderDomain("providers_models")]).snapshot()["providers_models"]["providers"][0]["models"][0]

        self.assertEqual(
            {"id", "display_name", "public_model", "upstream_model", "enabled", "order"},
            set(model),
        )
        self.assertEqual("deployment-1", model["id"])
        self.assertEqual("default-chat", model["display_name"])
        self.assertEqual("default-chat", model["public_model"])
        self.assertEqual("upstream-chat", model["upstream_model"])
        self.assertFalse(model["enabled"])
        self.assertEqual(2, model["order"])
        encoded = json.dumps(model)
        self.assertNotIn("sk-synthetic-summary-secret", encoded)
        self.assertNotIn("/private/user", encoded)
        self.assertNotIn("supported_upstream_url_surfaces", model)

    def test_export_and_import_use_opaque_file_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = MemoryDomain("language", {"choice": "en"})
            core = CoreStore(domains=[domain])
            output = root / "export.json"
            token = core.file_capabilities.register(output, "export")
            result = core.export(["language"], destination_token=token)
            self.assertEqual(1, result["section_count"])
            self.assertEqual(0o600, stat.S_IMODE(output.stat().st_mode))
            source_token = core.file_capabilities.register(output, "import")
            prepared = core.prepare_import(source_token=source_token, revision=core.revision)
            imported = core.import_package(package=prepared.package, sections=["language"], revision=prepared.revision)
            self.assertEqual(["language"], imported["draft_domains"])

    def test_import_preview_reports_only_preexisting_drafts(self) -> None:
        language = MemoryDomain("language", {"choice": "en"})
        runtime = MemoryDomain("runtime", {"port": "4000"})
        core = CoreStore(domains=[language, runtime])
        core.dispatch({"domain": "language", "type": "set", "payload": {"choice": "zh-Hans"}})

        imported = core.import_package(
            package={
                "format": "young-router-core-package",
                "version": 1,
                "sections": {
                    "language": {"state": {"choice": "system"}},
                    "runtime": {"state": {"port": "4100"}},
                },
            },
            sections=["language", "runtime"],
            revision=core.revision,
        )

        self.assertTrue(imported["preview"]["language"]["will_replace_draft"])
        self.assertFalse(imported["preview"]["runtime"]["will_replace_draft"])

    def test_a_dispatch_waiting_on_an_in_flight_apply_reports_a_stale_revision(self) -> None:
        """The revision a client captured can expire while the store lock is busy.

        ``CoreStore.dispatch`` checks the caller revision only once it owns the
        store lock, and a provider Apply holds that lock while it rewrites the
        configuration and reloads the managed proxy. A buffered editor action
        therefore has to rebase once on the authoritative snapshot (see
        ``isRevisionRetryableAction`` in the shared UI) instead of dropping the
        user's edit and reporting a conflict.
        """

        applied = threading.Event()
        release = threading.Event()

        class BlockingApplyDomain(MemoryDomain):
            def apply(self, payload: object | None = None) -> dict[str, object]:
                applied.set()
                release.wait(timeout=5)
                return super().apply(payload)

        providers = BlockingApplyDomain("providers_models", {"value": "staged"})
        core = CoreStore(domains=[MemoryDomain("language", {"choice": "en"}), providers])
        core.dispatch({"domain": "providers_models", "type": "set", "payload": {"value": "staged"}})
        captured_revision = core.revision
        failures: list[Exception] = []

        def apply_staged() -> None:
            try:
                core.apply("providers_models", revision=captured_revision)
            except Exception as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        applier = threading.Thread(target=apply_staged, name="synthetic-apply")
        applier.start()
        self.assertTrue(applied.wait(timeout=5))

        def dispatch_buffered_edit() -> None:
            try:
                core.dispatch(
                    {"domain": "providers_models", "type": "patch", "payload": {"value": "edited"}},
                    expected_revision=captured_revision,
                )
            except Exception as exc:
                failures.append(exc)

        editor = threading.Thread(target=dispatch_buffered_edit, name="synthetic-editor")
        editor.start()
        editor.join(timeout=0.2)
        release.set()
        applier.join(timeout=5)
        editor.join(timeout=5)

        self.assertEqual(["revision_conflict"], [getattr(failure, "code", "") for failure in failures])
        self.assertEqual(captured_revision + 1, core.revision)

    def test_import_rejects_a_stale_revision_before_staging(self) -> None:
        language = MemoryDomain("language", {"choice": "en"})
        core = CoreStore(domains=[language])
        core.dispatch({"domain": "language", "type": "set", "payload": {"choice": "zh-Hans"}})
        before = core.snapshot()

        with self.assertRaises(Exception) as raised:
            core.import_package(
                package={
                    "format": "young-router-core-package",
                    "version": 1,
                    "sections": {"language": {"state": {"choice": "system"}}},
                },
                sections=["language"],
                revision=0,
            )

        self.assertEqual("revision_conflict", raised.exception.code)
        self.assertEqual(before, core.snapshot())

    def test_multisection_import_rolls_back_every_state_when_second_adapter_fails(self) -> None:
        class ImportDomain(MemoryDomain):
            def __init__(self, name: str, *, fail: bool = False):
                super().__init__(name, {"value": "saved"})
                self.fail = fail
                self.internal = {"attempts": []}

            def import_package(self, payload: object) -> None:
                data = dict(payload) if isinstance(payload, dict) else {}
                self._draft = {"value": data.get("value", "imported")}
                self.internal["attempts"].append(self.name)
                self.revision += 1
                if self.fail:
                    raise ValueError("synthetic second-section failure")

        with tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory) / "core-state.json"
            first = ImportDomain("providers_models")
            second = ImportDomain("runtime", fail=True)
            core = CoreStore(metadata_path=metadata, domains=[first, second])
            core.set_service_status("stopped")
            before_snapshot = core.snapshot()
            before_first = json.loads(json.dumps(first.__dict__))
            before_second = json.loads(json.dumps(second.__dict__))
            before_revision = core.revision
            before_drafts = json.loads(json.dumps(core._drafts))
            before_last_actions = json.loads(json.dumps(core._last_actions))
            before_baselines = json.loads(json.dumps(core._baselines))
            before_metadata = metadata.read_bytes()
            events: list[dict[str, object]] = []
            unsubscribe = core.subscribe(events.append)
            self.addCleanup(unsubscribe)

            with self.assertRaises(Exception):
                core.import_package(
                    package={
                        "format": "young-router-core-package",
                        "version": 1,
                        "sections": {
                            "providers_models": {"value": "first-import"},
                            "runtime": {"value": "second-import"},
                        },
                    },
                    sections=["providers_models", "runtime"],
                    revision=core.revision,
                )

            self.assertEqual(before_snapshot, core.snapshot())
            self.assertEqual(before_first, first.__dict__)
            self.assertEqual(before_second, second.__dict__)
            self.assertEqual(before_revision, core.revision)
            self.assertEqual(before_drafts, core._drafts)
            self.assertEqual(before_last_actions, core._last_actions)
            self.assertEqual(before_baselines, core._baselines)
            self.assertEqual(before_metadata, metadata.read_bytes())
            self.assertEqual([], events)

    def test_editor_returns_plaintext_and_stages_through_the_versioned_ipc_method(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_text('{"env":{"ANTHROPIC_AUTH_TOKEN":"synthetic-token"}}\n', encoding="utf-8")
            from young_router.core.domains.claude import ClaudeSettingsDomain

            core = CoreStore(domains=[ClaudeSettingsDomain(settings)])
            snapshot = json.dumps(core.snapshot())
            self.assertNotIn("synthetic-token", snapshot)

            server = CoreIPCServer(core)
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)

            editor = client.call("editor", {"domain": "claude", "document": "settings"})
            self.assertEqual("settings", editor["document"])
            self.assertIn("editor_token", editor)
            self.assertIn("synthetic-token", editor["text"])
            self.assertEqual(editor["text"], editor["baseline"])

            staged = client.call(
                "editor",
                {"editor_token": editor["editor_token"], "text": '{"model":"updated"}\n'},
            )
            self.assertEqual("claude", staged["domain"])
            self.assertEqual("settings", staged["document"])
            self.assertEqual('{"model":"updated"}\n', staged["text"])
            self.assertEqual(editor["baseline"], staged["baseline"])
            self.assertNotEqual(editor["editor_token"], staged["editor_token"])
            self.assertGreater(staged["revision"], editor["revision"])
            self.assertEqual("updated", core.snapshot()["domains"]["claude"]["settings"]["model"])
            with self.assertRaises(Exception):
                client.call("editor", {"editor_token": editor["editor_token"], "text": "{}\n"})

    def test_a_webdav_comparison_reads_both_sides_and_stages_nothing(self) -> None:
        """A stopped sync's two texts open as one document, read-only.

        The comparison is a document read, so it uses the same versioned lease
        every raw editor does -- but there is nothing to write back: a config
        file that arrived through this surface would skip the sync's own
        bundle validation, so a stage attempt is refused.
        """

        from young_router.core.domains.webdav import WebDAVSettingsDomain

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")
            webdav = WebDAVSettingsDomain(
                root / "webdav.json",
                enabled_path=root / "webdav.enabled",
                status_path=root / "webdav-status.json",
                config_path=config,
                state_path=root / "webdav-state.json",
                conflict_path=root / "webdav-conflict.json",
            )
            webdav.stage_secret("password", None, "webdav-secret")
            webdav.dispatch("patch", {"url": "https://webdav.example.test/config/"})
            webdav.apply()
            (root / "webdav-conflict.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "recorded_at": "2026-09-25T10:00:00Z",
                        "remote_created_at": "2026-09-25T09:59:00Z",
                        "files": {"config.yaml": "providers: {}\nmodel_list:\n  - model_name: remote\n"},
                    }
                ),
                encoding="utf-8",
            )

            core = CoreStore(domains=[webdav])
            server = CoreIPCServer(core)
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)

            editor = client.call("editor", {"domain": "webdav", "document": "config"})
            self.assertEqual("webdav", editor["domain"])
            self.assertEqual("providers: {}\nmodel_list: []\n", editor["text"])
            self.assertIn("model_name: remote", editor["baseline"])

            with self.assertRaises(Exception) as refused:
                client.call("editor", {"editor_token": editor["editor_token"], "text": "providers: {}\n"})
            self.assertIn("editor is unavailable", str(refused.exception))
            # A document this domain does not own is not readable either: the
            # pane cannot reach another domain's file by naming it here.
            with self.assertRaises(Exception) as unknown:
                client.call("editor", {"domain": "webdav", "document": "settings"})
            self.assertIn("editor is unavailable", str(unknown.exception))
            self.assertEqual("providers: {}\nmodel_list: []\n", config.read_text(encoding="utf-8"))

    def test_editor_diff_baseline_keeps_structured_staged_changes_visible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_text('{"model":"disk-model"}\n', encoding="utf-8")
            from young_router.core.domains.claude import ClaudeSettingsDomain

            core = CoreStore(domains=[ClaudeSettingsDomain(settings)])
            server = CoreIPCServer(core)
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)

            opened = client.call("editor", {"domain": "claude", "document": "settings"})
            core.dispatch(
                {"domain": "claude", "type": "patch", "payload": {"model": "structured-model"}},
                expected_revision=core.revision,
            )
            staged = client.call("editor", {"domain": "claude", "document": "settings"})

            self.assertIn('"model": "structured-model"', staged["text"])
            self.assertEqual(opened["baseline"], staged["baseline"])
            self.assertNotEqual(staged["text"], staged["baseline"])

            core.apply("claude", revision=core.revision)
            applied = client.call("editor", {"domain": "claude", "document": "settings"})
            self.assertEqual(applied["text"], applied["baseline"])

    def test_claude_desktop_developer_and_code_raw_editors_stage_their_own_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            settings.write_text('{"skipWorkflowUsageWarning":true}\n', encoding="utf-8")
            developer_settings = root / "Claude" / "developer_settings.json"
            developer_settings.parent.mkdir(parents=True)
            developer_settings.write_text('{"allowDevTools":true}\n', encoding="utf-8")
            library = root / "Claude-3p" / "configLibrary"
            library.mkdir(parents=True)
            config_id = "11111111-2222-4333-8444-555555555555"
            (library / "_meta.json").write_text(
                json.dumps({"appliedId": config_id, "entries": [{"id": config_id, "name": "Default"}]}),
                encoding="utf-8",
            )
            (library / f"{config_id}.json").write_text(
                json.dumps(
                    {
                        "inferenceProvider": "gateway",
                        "inferenceGatewayBaseUrl": "http://127.0.0.1:4000",
                        "inferenceGatewayApiKey": "synthetic-desktop-key",
                    }
                ),
                encoding="utf-8",
            )
            from young_router.core.domains.claude import ClaudeSettingsDomain

            core = CoreStore(
                domains=[ClaudeSettingsDomain(
                    settings,
                    desktop_config_library_path=library,
                    developer_settings_path=developer_settings,
                )]
            )
            server = CoreIPCServer(core)
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)

            desktop_editor = client.call("editor", {"domain": "claude", "document": "desktop"})
            developer_editor = client.call("editor", {"domain": "claude", "document": "developer"})
            code_editor = client.call("editor", {"domain": "claude", "document": "settings"})
            self.assertIn("synthetic-desktop-key", desktop_editor["text"])
            self.assertIn("allowDevTools", developer_editor["text"])
            self.assertIn("skipWorkflowUsageWarning", code_editor["text"])
            self.assertNotIn("synthetic-desktop-key", json.dumps(core.snapshot()))

            desktop_staged = client.call(
                "editor",
                {
                    "editor_token": desktop_editor["editor_token"],
                    "text": '{"inferenceProvider":"gateway","inferenceGatewayBaseUrl":"http://127.0.0.1:4100","inferenceGatewayApiKey":"synthetic-desktop-key"}\n',
                },
            )
            developer_staged = client.call(
                "editor",
                {"editor_token": developer_editor["editor_token"], "text": '{"allowDevTools":false}\n'},
            )
            code_staged = client.call(
                "editor",
                {"editor_token": code_editor["editor_token"], "text": '{"model":"claude-code-model"}\n'},
            )
            self.assertNotEqual(desktop_editor["editor_token"], desktop_staged["editor_token"])
            self.assertNotEqual(developer_editor["editor_token"], developer_staged["editor_token"])
            self.assertNotEqual(code_editor["editor_token"], code_staged["editor_token"])
            snapshot = core.snapshot()["domains"]["claude"]
            self.assertEqual("http://127.0.0.1:4100", snapshot["desktop"]["gateway_url"])
            self.assertFalse(snapshot["developer"]["developer_mode_enabled"])
            self.assertEqual("claude-code-model", snapshot["settings"]["model"])
            self.assertNotIn("synthetic-desktop-key", json.dumps(snapshot))

    def test_editor_capability_is_session_bound_and_rejects_a_stale_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            from young_router.core.domains.claude import ClaudeSettingsDomain

            core = CoreStore(domains=[ClaudeSettingsDomain(Path(directory) / "settings.json")])
            server = CoreIPCServer(core)
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)
            editor = client.call("editor", {"domain": "claude", "document": "settings"})

            client.call(
                "dispatch",
                {
                    "action": {
                        "domain": "claude",
                        "type": "patch",
                        "payload": {"model": "newer"},
                    }
                },
            )
            with self.assertRaises(Exception):
                client.call(
                    "editor",
                    {"editor_token": editor["editor_token"], "text": "{}\n"},
                )

    def test_editor_open_remains_stageable_when_an_unrelated_revision_follows_its_read(self) -> None:
        """An editor lease owns an atomic document/revision baseline.

        A status or another settings surface can advance Core immediately
        after the raw document is read. That must not turn an unchanged raw
        editor into a false outside-change conflict.
        """

        with tempfile.TemporaryDirectory() as directory:
            from young_router.core.domains.claude import ClaudeSettingsDomain

            core = CoreStore(
                domains=[
                    ClaudeSettingsDomain(Path(directory) / "settings.json"),
                    MemoryDomain("language", {"choice": "system"}),
                ]
            )
            server = CoreIPCServer(core)
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)

            original_editor_document = core.editor_document
            advanced = False

            def read_then_advance(domain: str, document: str) -> dict[str, object]:
                nonlocal advanced
                descriptor = original_editor_document(domain, document)
                if not advanced:
                    advanced = True
                    core.dispatch(
                        {
                            "domain": "language",
                            "type": "patch",
                            "payload": {"choice": "en"},
                        },
                        expected_revision=core.revision,
                    )
                return descriptor

            with mock.patch.object(core, "editor_document", side_effect=read_then_advance):
                editor = client.call("editor", {"domain": "claude", "document": "settings"})

            staged = client.call(
                "editor",
                {"editor_token": editor["editor_token"], "text": '{"model":"stable"}\n'},
            )
            self.assertEqual('{"model":"stable"}\n', staged["text"])
            self.assertGreater(staged["revision"], editor["revision"])

    def test_editor_stage_rotates_the_capability_for_continuous_editing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            from young_router.core.domains.claude import ClaudeSettingsDomain

            core = CoreStore(domains=[ClaudeSettingsDomain(Path(directory) / "settings.json")])
            server = CoreIPCServer(core)
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)
            editor = client.call("editor", {"domain": "claude", "document": "settings"})
            original_token = editor["editor_token"]
            first = client.call(
                "editor",
                {"editor_token": original_token, "text": '{"model":"first"}\n'},
            )
            self.assertEqual(
                {"domain", "document", "revision", "editor_token", "text", "baseline"}, set(first)
            )
            self.assertEqual("claude", first["domain"])
            self.assertEqual("settings", first["document"])
            self.assertEqual('{"model":"first"}\n', first["text"])
            replacement_token = first["editor_token"]
            self.assertNotEqual(original_token, replacement_token)
            with self.assertRaises(Exception):
                client.call(
                    "editor",
                    {"editor_token": original_token, "text": '{"model":"stale"}\n'},
                )
            second = client.call(
                "editor",
                {"editor_token": replacement_token, "text": '{"model":"second"}\n'},
            )
            self.assertGreater(second["revision"], first["revision"])
            self.assertNotEqual(replacement_token, second["editor_token"])
            self.assertEqual('{"model":"second"}\n', second["text"])
            self.assertEqual(
                "second", core.snapshot()["domains"]["claude"]["settings"]["model"]
            )

    def test_editor_capability_can_be_reacquired_after_core_capability_loss(self) -> None:
        """The React editor can reload a document and continue staging after token loss."""

        with tempfile.TemporaryDirectory() as directory:
            from young_router.core.domains.claude import ClaudeSettingsDomain

            core = CoreStore(domains=[ClaudeSettingsDomain(Path(directory) / "settings.json")])
            server = CoreIPCServer(core)
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)

            original = client.call("editor", {"domain": "claude", "document": "settings"})
            with server._lock:
                server._editor_capabilities.clear()
            with self.assertRaises(Exception):
                client.call(
                    "editor",
                    {"editor_token": original["editor_token"], "text": '{"model":"stale"}\n'},
                )

            replacement = client.call("editor", {"domain": "claude", "document": "settings"})
            self.assertNotEqual(original["editor_token"], replacement["editor_token"])
            self.assertEqual(original["text"], replacement["text"])
            staged = client.call(
                "editor",
                {"editor_token": replacement["editor_token"], "text": '{"model":"recovered"}\n'},
            )
            self.assertEqual('{"model":"recovered"}\n', staged["text"])
            self.assertEqual("recovered", core.snapshot()["domains"]["claude"]["settings"]["model"])

    def test_editor_reacquisition_acknowledges_a_stage_whose_reply_was_lost(self) -> None:
        """A replacement descriptor exposes an edit Core already accepted.

        The editor token is one-use. If native transport loses the successful
        stage response, React retries with its now-invalid original token and
        then reacquires the descriptor. Its text must prove the original edit
        succeeded instead of being treated as a competing outside change.
        """

        with tempfile.TemporaryDirectory() as directory:
            from young_router.core.domains.claude import ClaudeSettingsDomain

            core = CoreStore(domains=[ClaudeSettingsDomain(Path(directory) / "settings.json")])
            server = CoreIPCServer(core)
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)

            original = client.call("editor", {"domain": "claude", "document": "settings"})
            accepted_text = '{\n  "model": "accepted"\n}\n'
            # Model a successful Core write whose IPC response never reaches
            # the embedded editor. The original token has been consumed.
            server.stage_editor_capability(
                original["editor_token"], accepted_text, session_token=client._session_token
            )
            with self.assertRaises(Exception):
                client.call(
                    "editor",
                    {"editor_token": original["editor_token"], "text": accepted_text},
                )

            reacquired = client.call("editor", {"domain": "claude", "document": "settings"})
            self.assertEqual(accepted_text, reacquired["text"])
            continued = client.call(
                "editor",
                {"editor_token": reacquired["editor_token"], "text": '{\n  "model": "continued"\n}\n'},
            )
            self.assertEqual('{\n  "model": "continued"\n}\n', continued["text"])

    def test_codex_raw_editors_remain_valid_when_the_sibling_document_stages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            from young_router.core.domains.codex import CodexSettingsDomain

            root = Path(directory)
            config = root / "config.yaml"
            config.write_text("model_list: []\n", encoding="utf-8")
            codex_home = root / "codex"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text('model = "first"\n', encoding="utf-8")
            (codex_home / "auth.json").write_text('{"kind":"first"}\n', encoding="utf-8")
            core = CoreStore(domains=[CodexSettingsDomain(config, codex_home=codex_home)])
            server = CoreIPCServer(core)
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)

            config_editor = client.call("editor", {"domain": "codex", "document": "config"})
            auth_editor = client.call("editor", {"domain": "codex", "document": "auth"})
            self.assertIn("model", config_editor["text"])
            self.assertIn("kind", auth_editor["text"])

            staged = client.call(
                "editor",
                {"editor_token": config_editor["editor_token"], "text": 'model = "second"\n'},
            )
            self.assertGreater(staged["revision"], config_editor["revision"])
            codex_after_config = core.snapshot()["domains"]["codex"]
            self.assertTrue(codex_after_config["config_exists"])
            self.assertTrue(codex_after_config["auth_file_exists"])
            next_auth = client.call(
                "editor",
                {"editor_token": auth_editor["editor_token"], "text": '{"kind":"second"}\n'},
            )
            self.assertGreater(next_auth["revision"], staged["revision"])
            codex_after_auth = core.snapshot()["domains"]["codex"]
            self.assertTrue(codex_after_auth["config_exists"])
            self.assertTrue(codex_after_auth["auth_file_exists"])
            refreshed_config = client.call("editor", {"domain": "codex", "document": "config"})
            refreshed_auth = client.call("editor", {"domain": "codex", "document": "auth"})
            self.assertIn("second", refreshed_config["text"])
            self.assertIn("second", refreshed_auth["text"])

    def test_claude_plaintext_dispatch_requires_native_capabilities(self) -> None:
        from young_router.core.domains.claude import ClaudeSettingsDomain

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core = CoreStore(domains=[ClaudeSettingsDomain(root / "settings.json")])
            server = CoreIPCServer(core)
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)

            with self.assertRaisesRegex(Exception, "native secure input"):
                client.call(
                    "dispatch",
                    {"action": {"domain": "claude", "type": "patch", "payload": {"env": {"ANTHROPIC_AUTH_TOKEN": "synthetic-token"}}}},
                )
            secret = server.register_secret_capability(
                "claude", "deployment_token", None, session_token=client._session_token
            )
            server.stage_secret_capability(secret["secret_token"], "synthetic-token", session_token=client._session_token)

            with self.assertRaisesRegex(Exception, "native secure input"):
                client.call(
                    "dispatch",
                    {"action": {"domain": "claude", "type": "patch", "payload": {"autoMemoryDirectory": "~/synthetic-memory"}}},
                )
            memory = server.register_secret_capability(
                "claude", "auto_memory_directory", None, session_token=client._session_token
            )
            server.stage_secret_capability(memory["secret_token"], "~/synthetic-memory", session_token=client._session_token)

            snapshot = json.dumps(core.snapshot())
            self.assertNotIn("synthetic-token", snapshot)
            self.assertNotIn("~/synthetic-memory", snapshot)
            self.assertTrue(core.snapshot()["domains"]["claude"]["settings"]["token_configured"])
            self.assertTrue(core.snapshot()["domains"]["claude"]["settings"]["autoMemoryDirectoryConfigured"])

    def test_claude_snapshot_hides_command_and_permission_rule_text_but_editor_ipc_returns_it(self) -> None:
        from young_router.core.domains.claude import ClaudeSettingsDomain

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "permissions": {"allow": ["Read(/private/ipc-synthetic-path/**)"]},
                        "sandbox": {"excludedCommands": ["tool --token=ipc-synthetic-token"]},
                    }
                ),
                encoding="utf-8",
            )
            core = CoreStore(domains=[ClaudeSettingsDomain(path)])

            public_snapshot = json.dumps(core.snapshot())
            self.assertNotIn("/private/ipc-synthetic-path", public_snapshot)
            self.assertNotIn("ipc-synthetic-token", public_snapshot)
            server = CoreIPCServer(core)
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)
            editor = client.call("editor", {"domain": "claude", "document": "settings"})
            self.assertIn("/private/ipc-synthetic-path", editor["text"])
            self.assertIn("ipc-synthetic-token", editor["text"])

    def test_secret_capability_is_allowlisted_session_bound_and_one_time(self) -> None:
        class SecretDomain(MemoryDomain):
            def __init__(self) -> None:
                super().__init__("webdav", {"password_configured": False})
                self.secret = ""

            def secret_present(self, field: str, target: str | None = None) -> bool:
                if field != "password" or target is not None:
                    raise ValueError("unavailable")
                return bool(self.secret)

            def stage_secret(self, field: str, target: str | None, value: str) -> None:
                if field != "password" or target is not None:
                    raise ValueError("unavailable")
                self.secret = value
                self._draft["password_configured"] = bool(value)

        domain = SecretDomain()
        core = CoreStore(domains=[domain])
        server = CoreIPCServer(core)
        endpoint = server.start()
        self.addCleanup(server.stop)
        client = CoreIPCClient(endpoint, server.bootstrap_token)
        self.addCleanup(client.close)
        client.call("snapshot")
        session = client._session_token

        capability = server.register_secret_capability(
            "webdav", "password", None, session_token=session
        )
        self.assertFalse(capability["present"])
        self.assertNotIn("password", capability["secret_token"])
        with self.assertRaises(Exception):
            server.stage_secret_capability(
                capability["secret_token"], "synthetic-secret", session_token="another-session"
            )

        result = server.stage_secret_capability(
            capability["secret_token"], "synthetic-secret", session_token=session
        )
        self.assertEqual("synthetic-secret", domain.secret)
        self.assertEqual({"revision": 1, "present": True}, result)
        self.assertNotIn("synthetic-secret", json.dumps(result))
        with self.assertRaises(Exception):
            server.stage_secret_capability(
                capability["secret_token"], "replacement", session_token=session
            )
        with self.assertRaises(Exception):
            server.register_secret_capability(
                "webdav", "username", None, session_token=session
            )

    def test_secret_capability_rejects_stale_revision_and_http_response_is_presence_only(self) -> None:
        from young_router.core.domains.webdav import WebDAVSettingsDomain

        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "webdav.json"
            core = CoreStore(domains=[WebDAVSettingsDomain(settings)])
            server = CoreIPCServer(core)
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)
            client.call("snapshot")

            status, body, _headers = client._http(
                "/ipc/host/secret/capability",
                payload=encode_message({"domain": "webdav", "field": "password", "purpose": "settings"}),
                token=client._session_token,
            )
            self.assertEqual(200, status)
            capability = decode_message(body)
            core.dispatch({"domain": "webdav", "type": "patch", "payload": {"username": "newer"}})
            with self.assertRaises(Exception):
                server.stage_secret_capability(
                    capability["secret_token"], "must-not-stage", session_token=client._session_token
                )

            fresh = server.register_secret_capability(
                "webdav", "password", None, session_token=client._session_token
            )
            status, body, _headers = client._http(
                "/ipc/host/secret/stage",
                payload=encode_message({"secret_token": fresh["secret_token"], "value": "synthetic-secret"}),
                token=client._session_token,
            )
            self.assertEqual(200, status)
            response = decode_message(body)
            self.assertEqual({"protocol_version", "revision", "present"}, set(response))
            self.assertTrue(response["present"])
            self.assertNotIn("synthetic-secret", body.decode("utf-8"))

    def test_provider_api_key_plaintext_readback_is_narrow_and_read_once(self) -> None:
        from young_router.core.domains.providers_models import ProvidersModelsDomain

        api_key = "test-provider-api-key"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                "\n".join(
                    [
                        "providers:",
                        "  test-provider:",
                        '    api_base: "https://example.test/v1"',
                        "    api_keys:",
                        "      - name: default",
                        f'        value: "{api_key}"',
                        "model_list: []",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            core = CoreStore(domains=[ProvidersModelsDomain(path)])
            server = CoreIPCServer(core)
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)

            public_snapshot = client.call("snapshot")
            self.assertNotIn(api_key, json.dumps(public_snapshot))
            self.assertNotIn(api_key, json.dumps(core.snapshot()))

            session = client._session_token
            status, body, _headers = client._http(
                "/ipc/host/secret/read-capability",
                payload=encode_message(
                    {
                        "domain": "providers_models",
                        "field": "api_key",
                        "target": "test-provider\x1fdefault",
                    }
                ),
                token=session,
            )
            self.assertEqual(200, status)
            read_capability = decode_message(body)
            self.assertEqual(
                {"protocol_version", "secret_read_token", "revision", "present"},
                set(read_capability),
            )
            self.assertTrue(read_capability["present"])
            self.assertNotIn(api_key, body.decode("utf-8"))

            status, body, _headers = client._http(
                "/ipc/host/secret/read",
                payload=encode_message({"secret_read_token": read_capability["secret_read_token"]}),
                token=session,
            )
            self.assertEqual(200, status)
            self.assertEqual(
                {"protocol_version", "value"},
                set(decode_message(body)),
            )
            self.assertEqual(api_key, decode_message(body)["value"])

            # A read lease cannot be replayed, even by the same native host.
            status, body, _headers = client._http(
                "/ipc/host/secret/read",
                payload=encode_message({"secret_read_token": read_capability["secret_read_token"]}),
                token=session,
            )
            self.assertNotEqual(200, status)
            self.assertNotIn(api_key, body.decode("utf-8"))

            # The route is not a generic secret-read API.
            status, body, _headers = client._http(
                "/ipc/host/secret/read-capability",
                payload=encode_message({"domain": "webdav", "field": "password", "target": "x"}),
                token=session,
            )
            self.assertNotEqual(200, status)
            self.assertNotIn(api_key, body.decode("utf-8"))

            writer = server.register_secret_capability(
                "providers_models", "api_key", "test-provider\x1fdefault", session_token=session
            )
            server.stage_secret_capability(
                writer["secret_token"], "unsaved-provider-api-key", session_token=session
            )
            self.assertNotIn("unsaved-provider-api-key", json.dumps(core.snapshot()))
            reader = server.register_secret_read_capability(
                "providers_models", "api_key", "test-provider\x1fdefault", session_token=session
            )
            self.assertEqual(
                "unsaved-provider-api-key",
                server.read_secret_capability(reader["secret_read_token"], session_token=session),
            )
            with self.assertRaises(Exception):
                client.call(
                    "dispatch",
                    {
                        "action": {
                            "domain": "providers_models",
                            "type": "provider.patch",
                            "payload": {"provider_id": "test-provider", "api_key": api_key},
                        }
                    },
                )

    def test_relay_remembered_password_is_served_only_through_the_native_lease(self) -> None:
        """The host reads the remembered password through the same native lease
        a key is read with, and the account's public snapshot never carries it.
        """

        from young_router.core.domains.relay_accounts import RelayAccountsDomain

        password = "replace-relay-password"
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(Path(directory))
            account = domain.dispatch(
                "account.add",
                {
                    "type": "newapi",
                    "label": "Relay",
                    "origin": "https://relay.example.test",
                    "remember_password": True,
                },
            )["accounts"][0]
            domain.accept_login_result(
                account["id"],
                username="sample-user",
                cookie="session=replace-cookie",
                password=password,
            )
            core = CoreStore(domains=[domain])
            server = CoreIPCServer(core)
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)

            self.assertNotIn(password, json.dumps(client.call("snapshot")))
            session = client._session_token
            status, body, _headers = client._http(
                "/ipc/host/secret/read-capability",
                payload=encode_message(
                    {"domain": "relay_accounts", "field": "password", "target": account["id"]}
                ),
                token=session,
            )
            self.assertEqual(200, status)
            read_capability = decode_message(body)
            self.assertTrue(read_capability["present"])
            self.assertNotIn(password, body.decode("utf-8"))

            status, body, _headers = client._http(
                "/ipc/host/secret/read",
                payload=encode_message({"secret_read_token": read_capability["secret_read_token"]}),
                token=session,
            )
            self.assertEqual(200, status)
            self.assertEqual(password, decode_message(body)["value"])

            # A read lease is read once: the same token answers nothing again.
            status, body, _headers = client._http(
                "/ipc/host/secret/read",
                payload=encode_message({"secret_read_token": read_capability["secret_read_token"]}),
                token=session,
            )
            self.assertNotEqual(200, status)

            # Retiring the password stops the read even for the native host.
            domain.accept_login_result(
                account["id"],
                username="sample-user",
                cookie="session=replace-cookie",
                remember_password=False,
            )
            domain.apply()
            status, body, _headers = client._http(
                "/ipc/host/secret/read-capability",
                payload=encode_message(
                    {"domain": "relay_accounts", "field": "password", "target": account["id"]}
                ),
                token=session,
            )
            self.assertNotEqual(200, status)
            self.assertNotIn(password, body.decode("utf-8"))

    def test_codex_and_claude_plaintext_fields_use_native_read_once_capabilities(self) -> None:
        class PlaintextSecretDomain(MemoryDomain):
            def __init__(self, name: str, field: str, secret: str) -> None:
                super().__init__(name, {"configured": True})
                self.field = field
                self.secret = secret

            def secret_present(self, field: str, target: str | None = None) -> bool:
                if field != self.field or target is not None:
                    raise ValueError("unavailable")
                return bool(self.secret)

            def stage_secret(self, field: str, target: str | None, value: str) -> None:
                if field != self.field or target is not None:
                    raise ValueError("unavailable")
                self.secret = value

            def trusted_secret_value(self, field: str, target: str | None = None) -> str:
                if field != self.field or target is not None:
                    raise ValueError("unavailable")
                return self.secret

        cases = (
            ("codex", "api_key", "synthetic-codex-key"),
            ("claude", "deployment_token", "synthetic-claude-code-token"),
            ("claude", "desktop_gateway_api_key", "synthetic-claude-desktop-key"),
        )
        for domain_name, field, secret in cases:
            with self.subTest(domain=domain_name, field=field):
                core = CoreStore(domains=[PlaintextSecretDomain(domain_name, field, secret)])
                server = CoreIPCServer(core)
                endpoint = server.start()
                client = CoreIPCClient(endpoint, server.bootstrap_token)
                try:
                    self.assertNotIn(secret, json.dumps(client.call("snapshot")))
                    session = client._session_token
                    status, body, _headers = client._http(
                        "/ipc/host/secret/read-capability",
                        payload=encode_message(
                            {"domain": domain_name, "field": field, "target": None}
                        ),
                        token=session,
                    )
                    self.assertEqual(200, status)
                    capability = decode_message(body)
                    self.assertTrue(capability["present"])
                    read_status, read_body, _headers = client._http(
                        "/ipc/host/secret/read",
                        payload=encode_message(
                            {"secret_read_token": capability["secret_read_token"]}
                        ),
                        token=session,
                    )
                    self.assertEqual(200, read_status)
                    self.assertEqual(secret, decode_message(read_body)["value"])
                    replay_status, _body, _headers = client._http(
                        "/ipc/host/secret/read",
                        payload=encode_message(
                            {"secret_read_token": capability["secret_read_token"]}
                        ),
                        token=session,
                    )
                    self.assertNotEqual(200, replay_status)
                finally:
                    client.close()
                    server.stop()

class CoreIPCKeepAliveTests(unittest.TestCase):
    """One loopback socket must carry every request a window makes.

    A settings control is one round trip and a typed field issues several, so
    the handler keeps the connection open.  The stdlib only hands the next
    request to the handler if the previous one consumed its body: an unread body
    stays in the buffer and its bytes become the next request line.  That failed
    two ways before it was pinned here -- ``{}`` read back as the method
    ``{}POST`` (answered 501), and a body drained *after* the reply, which left
    both sides waiting on each other until the client timed out.
    """

    def _connection(self, server: CoreIPCServer) -> tuple[http.client.HTTPConnection, str]:
        connection = http.client.HTTPConnection(server.address, server.port, timeout=10)
        connection.request(
            "POST",
            "/ipc/hello",
            body=b"{}",
            headers={"Authorization": f"Bearer {server.bootstrap_token}", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        token = response.headers.get("X-LiteLLM-Core-Session") or ""
        response.read()
        self.assertEqual(200, response.status)
        self.assertTrue(token)
        return connection, token

    def test_a_kept_alive_socket_carries_sequential_requests(self) -> None:
        core = CoreStore(domains=[MemoryDomain("language", {"choice": "system"})])
        server = CoreIPCServer(core)
        server.start()
        try:
            connection, token = self._connection(server)
            for _ in range(3):
                body = json.dumps(
                    {
                        "protocol_version": 1,
                        "request_id": uuid.uuid4().hex,
                        "method": "snapshot",
                        "params": {},
                    }
                ).encode()
                connection.request(
                    "POST",
                    "/ipc",
                    body=body,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                )
                response = connection.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(200, response.status, payload)
                self.assertTrue(payload["ok"])
            connection.close()
        finally:
            server.stop()

    def test_a_route_that_never_reads_its_body_still_leaves_a_reusable_socket(self) -> None:
        # ``/ipc/hello`` answers without parsing anything: it is the route whose
        # unread body corrupted the next request line.
        core = CoreStore(domains=[MemoryDomain("language", {"choice": "system"})])
        server = CoreIPCServer(core)
        server.start()
        try:
            connection, token = self._connection(server)
            body = json.dumps(
                {"protocol_version": 1, "request_id": uuid.uuid4().hex, "method": "snapshot", "params": {}}
            ).encode()
            connection.request(
                "POST",
                "/ipc",
                body=body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertEqual(200, response.status)
            self.assertTrue(json.loads(response.read())["ok"])
            connection.close()
        finally:
            server.stop()


class CoreIPCTests(unittest.TestCase):
    def test_dispatch_returns_the_fetch_summary_through_authenticated_ipc(self) -> None:
        from young_router.core.domains.providers_models import ProvidersModelsDomain

        class ModelListResponse:
            status = 200

            def read(self, _limit: int) -> bytes:
                return b'{"data":[{"id":"model-a"}]}'

            def __enter__(self) -> "ModelListResponse":
                return self

            def __exit__(self, _type: object, _value: object, _traceback: object) -> bool:
                return False

        class ModelListOpener:
            def open(self, _request: object, *, timeout: float) -> ModelListResponse:
                self.timeout = timeout
                return ModelListResponse()

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text(
                "providers:\n"
                "  openrouter:\n"
                "    api_base: https://openrouter.ai/api/v1\n"
                "    api_keys:\n"
                "      - name: r\n"
                "        value: synthetic-openrouter-key\n"
                "model_list: []\n",
                encoding="utf-8",
            )
            providers = ProvidersModelsDomain(config)
            core = CoreStore(domains=[providers])
            server = CoreIPCServer(core)
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)

            snapshot = client.call("snapshot")["snapshot"]
            provider_id = snapshot["domains"]["providers_models"]["providers"][0]["id"]
            opener = ModelListOpener()
            with mock.patch(
                "young_router.core.domains.providers_models.isolated_http_opener",
                return_value=opener,
            ):
                staged = client.call(
                    "dispatch",
                    {
                        "action": {
                            "domain": "providers_models",
                            "type": "providers.fetch_models",
                            "payload": {"provider_id": provider_id, "api_key_name": "r"},
                        },
                        "revision": core.revision,
                    },
                )

            self.assertEqual(5.0, opener.timeout)
            self.assertEqual("fetch_models", staged["action_summary"]["operation"])
            self.assertEqual(["model-a"], staged["action_summary"]["models"])
            self.assertNotIn("synthetic-openrouter-key", json.dumps(staged))

    def test_import_preview_is_non_mutating_and_plan_is_one_use(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package_path = Path(directory) / "configuration.json"
            source = CoreStore(
                domains=[
                    MemoryDomain("language", {"choice": "zh-Hans"}),
                    MemoryDomain("runtime", {"port": "4100"}),
                ]
            )
            source.export(
                ["language", "runtime"],
                destination_token=source.file_capabilities.register(package_path, "export"),
            )
            language = MemoryDomain("language", {"choice": "system"})
            runtime = MemoryDomain("runtime", {"port": "4000"})
            target = CoreStore(domains=[language, runtime])
            target.dispatch({"domain": "language", "type": "set", "payload": {"choice": "en"}})
            server = CoreIPCServer(target)
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)

            source_token = client.register_file_capability(str(package_path), "import")
            before = target.snapshot()
            preview = client.call("import_preview", {"source_token": source_token, "revision": target.revision})

            self.assertEqual(before, target.snapshot())
            self.assertEqual(["language", "runtime"], preview["detected_sections"])
            self.assertTrue(preview["preview"]["language"]["will_replace_draft"])
            self.assertFalse(preview["preview"]["runtime"]["will_replace_draft"])

            # A different authenticated session must not be able to consume
            # the owner's one-use plan merely by presenting its token.
            with self.assertRaises(Exception) as crossed:
                server._consume_import_plan(preview["import_plan_token"], session_token="synthetic-other-session")
            self.assertEqual("invalid_import_plan", crossed.exception.code)

            # A cross-session presentation must not consume the owner's plan.
            self.assertIn(preview["import_plan_token"], server._import_plans)

            # A request outside the detected set is invalid without consuming
            # the plan, so the owner may correct its selection.
            with self.assertRaises(Exception) as invalid_subset:
                server._consume_import_plan(
                    preview["import_plan_token"],
                    session_token=client._session_token,
                    expected_revision=preview["revision"],
                    sections=["codex"],
                )
            self.assertEqual("invalid_sections", invalid_subset.exception.code)
            self.assertIn(preview["import_plan_token"], server._import_plans)

            # Even if Core has not changed, an execution request carrying a
            # different revision cannot consume a preview-bound plan.
            with self.assertRaises(Exception) as wrong_revision:
                server._consume_import_plan(
                    preview["import_plan_token"],
                    session_token=client._session_token,
                    expected_revision=preview["revision"] + 1,
                    sections=["runtime"],
                )
            self.assertEqual("revision_conflict", wrong_revision.exception.code)
            self.assertIn(preview["import_plan_token"], server._import_plans)

            staged = client.call(
                "import",
                {
                    "import_plan_token": preview["import_plan_token"],
                    "sections": ["runtime"],
                    "revision": preview["revision"],
                },
            )
            self.assertEqual(["runtime"], staged["draft_domains"])
            self.assertEqual("en", language.draft_state()["choice"])
            self.assertEqual("4100", runtime.draft_state()["port"])
            with self.assertRaises(IPCError):
                client.call(
                    "import",
                    {
                        "import_plan_token": preview["import_plan_token"],
                        "sections": ["runtime"],
                        "revision": staged["revision"],
                    },
                )

    def test_import_plan_rejects_state_changes_after_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package_path = Path(directory) / "configuration.json"
            source = CoreStore(domains=[MemoryDomain("language", {"choice": "system"})])
            source.export(
                ["language"],
                destination_token=source.file_capabilities.register(package_path, "export"),
            )
            language = MemoryDomain("language", {"choice": "en"})
            target = CoreStore(domains=[language])
            server = CoreIPCServer(target)
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)

            source_token = client.register_file_capability(str(package_path), "import")
            preview = client.call("import_preview", {"source_token": source_token, "revision": target.revision})
            target.dispatch({"domain": "language", "type": "set", "payload": {"choice": "zh-Hans"}})
            before = target.snapshot()

            with self.assertRaises(IPCError):
                client.call(
                    "import",
                    {
                        "import_plan_token": preview["import_plan_token"],
                        "sections": ["language"],
                        "revision": target.revision,
                    },
                )

            self.assertEqual(before, target.snapshot())
            self.assertEqual("zh-Hans", language.draft_state()["choice"])

            # Revision validation precedes one-use consumption. The stale
            # attempt leaves the owner's plan available for a fresh preview or
            # a retry if the caller still has the matching revision.
            self.assertIn(preview["import_plan_token"], server._import_plans)

    def test_import_plan_expiry_consumes_only_the_expired_lease(self) -> None:
        from young_router.core.service import PreparedImport

        target = CoreStore(domains=[MemoryDomain("language", {"choice": "system"})])
        server = CoreIPCServer(target)
        prepared = PreparedImport(
            package={
                "format": "young-router-core-package",
                "version": 1,
                "sections": {"language": {"state": {"choice": "en"}}},
            },
            detected_sections=("language",),
            preview={"language": {"available": True, "will_replace_draft": False}},
            revision=target.revision,
        )
        with mock.patch("young_router.core.ipc.IMPORT_PLAN_TTL_SECONDS", -1):
            token = server._register_import_plan(prepared, session_token="owner-session")

        with self.assertRaises(Exception) as expired:
            server._consume_import_plan(token, session_token="owner-session")

        self.assertEqual("invalid_import_plan", expired.exception.code)
        self.assertNotIn(token, server._import_plans)

    def test_invalid_core_result_becomes_a_safe_failure_response(self) -> None:
        secret = "synthetic-invalid-result-secret"
        core = CoreStore(domains=[MemoryDomain("language", {"choice": "system"})])
        core.snapshot = lambda: [secret]  # type: ignore[method-assign]
        server = CoreIPCServer(core)
        endpoint = server.start()
        self.addCleanup(server.stop)
        client = CoreIPCClient(endpoint, server.bootstrap_token)
        self.addCleanup(client.close)
        client._ensure_session()
        request = RequestEnvelope(request_id="invalid-result", method="snapshot", params={})

        status, body, _headers = client._http(
            "/ipc", payload=encode_message(request), token=client._session_token
        )
        response = ResponseEnvelope.from_mapping(decode_message(body))

        self.assertEqual(400, status)
        self.assertFalse(response.ok)
        self.assertIsNone(response.result)
        self.assertEqual(
            {
                "code": "invalid_response",
                "message": "snapshot result does not match the Core IPC contract",
                "retryable": False,
            },
            response.error,
        )
        self.assertNotIn(secret, body.decode("utf-8"))

    def test_a_safe_error_message_fits_the_field_it_crosses(self) -> None:
        """One bound is in characters and the field's is in bytes.

        A sentence of CJK characters passes the redactor's 512-character limit
        and is still wider than the 512 bytes the error field allows, so the
        envelope would refuse the very message meant to make it acceptable.
        """

        message = safe_error_message("远端 " * 400)
        self.assertLessEqual(len(message.encode("utf-8")), 512)
        self.assertNotIn("\n", message)
        self.assertEqual("Core operation failed", safe_error_message("  \n  "))

    def test_a_failure_the_wire_cannot_carry_is_still_answered(self) -> None:
        """A refused reply is not a Core that went away.

        The envelope validates its own code and message; a failure raised with
        an HTTP status as its ``code`` (an int) or with a multi-line sentence
        made that validation raise *inside* the handler's ``except`` block, so
        ``do_POST`` wrote no reply at all and the socket closed.  The native
        bridge can only read that as 本地 Core 不可用, which is how a WebDAV 404
        was reported to a reader while this Core was answering every other
        request.  Whatever a domain reports, the handler owes the peer an
        answer it can carry.
        """

        core = CoreStore(domains=[MemoryDomain("language", {"choice": "system"})])
        server = CoreIPCServer(core)
        endpoint = server.start()
        self.addCleanup(server.stop)
        client = CoreIPCClient(endpoint, server.bootstrap_token)
        self.addCleanup(client.close)
        client._ensure_session()

        def ask(request_id: str) -> tuple[int, ResponseEnvelope]:
            request = RequestEnvelope(
                request_id=request_id,
                method="dispatch",
                params={"action": {"domain": "webdav", "type": "sync"}},
            )
            status, body, _headers = client._http(
                "/ipc", payload=encode_message(request), token=client._session_token
            )
            return status, ResponseEnvelope.from_mapping(decode_message(body))

        with mock.patch.object(
            core,
            "dispatch",
            side_effect=CoreError(404, "GET https://webdav.example.test/menu-config.json failed: HTTP 404 Not Found"),
        ):
            status, response = ask("status-code-as-code")
        self.assertEqual(400, status)
        self.assertFalse(response.ok)
        self.assertEqual("core_error", (response.error or {})["code"])
        self.assertIn("HTTP 404", str((response.error or {})["message"]))

        with mock.patch.object(
            core,
            "dispatch",
            side_effect=CoreError("webdav_sync_failed", "PROPFIND failed: HTTP 403 Forbidden\nVercel protection rejected the request"),
        ):
            status, response = ask("multiline-message")
        self.assertEqual(400, status)
        self.assertFalse(response.ok)
        self.assertEqual("webdav_sync_failed", (response.error or {})["code"])
        message = str((response.error or {})["message"])
        self.assertNotIn("\n", message)
        self.assertIn("Vercel protection rejected the request", message)

    def test_apply_response_keeps_the_complete_result_contract(self) -> None:
        core = CoreStore(domains=[MemoryDomain("language", {"choice": "system"})])
        server = CoreIPCServer(core)
        endpoint = server.start()
        self.addCleanup(server.stop)
        client = CoreIPCClient(endpoint, server.bootstrap_token)
        self.addCleanup(client.close)

        staged = client.call(
            "dispatch",
            {
                "action": {"domain": "language", "type": "set", "payload": {"choice": "en"}},
                "revision": core.revision,
            },
        )
        result = client.call("apply", {"domain": "language", "revision": staged["revision"]})

        self.assertTrue(result["applied"])
        self.assertEqual("applied", result["status"])
        self.assertEqual(0, result["completed_operations"])
        self.assertEqual(0, result["pending_operations"])
        self.assertEqual([], result["issues"])
        self.assertEqual(["language"], result["domains"])

    def test_provider_apply_with_auth_manager_survives_transaction_checkpoint(self) -> None:
        from young_router.core.domains.providers_models import ProvidersModelsDomain
        from young_router.core.provider_auth import ProviderAuthManager

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            config_path.write_text(
                "providers:\n"
                "  primary:\n"
                "    api_base: https://example.test/v1\n"
                "    api_keys:\n"
                "      - name: default\n"
                "        value: replace-me\n"
                "model_list: []\n",
                encoding="utf-8",
            )
            provider = ProvidersModelsDomain(
                config_path,
                auth_manager=ProviderAuthManager(root),
            )
            reloads: list[str] = []
            core = CoreStore(
                domains=[provider],
                service_handlers={
                    "status": lambda _operation: {"state": "running"},
                    "reload": lambda operation: (reloads.append(operation) or {"state": "running"}),
                },
            )
            server = CoreIPCServer(core)
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)

            snapshot = client.call("snapshot")
            provider_id = snapshot["snapshot"]["domains"]["providers_models"]["providers"][0]["id"]
            staged = client.call(
                "dispatch",
                {
                    "action": {
                        "domain": "providers_models",
                        "type": "provider.patch",
                        "payload": {"provider_id": provider_id, "changes": {"name": "renamed"}},
                    },
                    "revision": core.revision,
                },
            )
            result = client.call("apply", {"domain": "providers_models", "revision": staged["revision"]})

            self.assertTrue(result["applied"])
            self.assertEqual("applied", result["status"])
            # The restart is a background task now; wait for it so the
            # controller call list is complete.
            self.assertTrue(core.wait_for_service_reload(5.0))
            self.assertEqual(["reload"], reloads)
            self.assertIn("renamed", config_path.read_text(encoding="utf-8"))

    def test_the_record_names_who_stopped_the_service(self) -> None:
        """``stopped`` alone does not say who stopped it, and that was the bug.

        A host replaces Core with SIGTERM (a failed session renewal, a request
        that outlived its budget).  That stop is identical to the app quitting
        or to the user pressing 停止服务, so a record holding only ``stopped``
        was a decision nobody made: the replacement came up reading it, and the
        window it handed over said 已停止 over a router that was serving a
        second earlier.  The record now names the actor beside the state, and an
        ordinary write drops that key so a stale reason cannot outlive the stop
        it described.
        """

        def service_handler(operation: str) -> dict[str, str]:
            return {"state": "stopped" if operation == "stop" else "running", "port": 49173}

        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "core-state.json"
            AtomicJSONStore(state_file).write(
                {"version": 1, "revision": 7, "service": {"state": "running", "port": 49173}}
            )
            replacement = CoreStore(
                metadata_path=state_file,
                domains=[MemoryDomain("language", {"choice": "system"})],
                service_handlers={"stop": service_handler},
            )
            replacement.shutdown(stopped_by="core_replaced")
            recorded = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual("stopped", recorded["service"]["state"])
            self.assertEqual("core_replaced", recorded["stopped_by"])
            self.assertEqual(7, recorded["revision"])

            app_exit = CoreStore(
                metadata_path=state_file,
                domains=[MemoryDomain("language", {"choice": "system"})],
                service_handlers={"stop": service_handler},
            )
            app_exit.shutdown(stopped_by="app_exit")
            recorded = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual("stopped", recorded["service"]["state"])
            self.assertEqual("app_exit", recorded["stopped_by"])

            # A record written by a running Core carries no actor: the key can
            # never survive into a later stop and be read as that stop's cause.
            running = CoreStore(
                metadata_path=state_file,
                domains=[MemoryDomain("language", {"choice": "system"})],
                service_handlers={"stop": service_handler, "status": lambda _op: {"state": "running", "pid": 1, "port": 49173}},
            )
            running.snapshot()
            running._persist_metadata()
            recorded = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertNotIn("stopped_by", recorded)

    def test_authenticated_host_shutdown_stops_the_owned_service(self) -> None:
        calls: list[str] = []

        def service_handler(operation: str) -> dict[str, str]:
            calls.append(operation)
            return {"state": "stopped"}

        core = CoreStore(
            domains=[MemoryDomain("language", {"choice": "system"})],
            service_handlers={"stop": service_handler},
        )
        server = CoreIPCServer(core)
        endpoint = server.start()
        self.addCleanup(server.stop)
        client = CoreIPCClient(endpoint, server.bootstrap_token)
        self.addCleanup(client.close)
        client.call("snapshot")

        status, body, _headers = client._http(
            "/ipc/host/shutdown",
            payload=b"{}",
            token=client._session_token,
        )

        self.assertEqual(200, status)
        self.assertEqual({"protocol_version": 1, "stopped": True}, decode_message(body))
        self.assertEqual(["stop"], calls)

    def test_host_shutdown_rejects_unauthenticated_call(self) -> None:
        core = CoreStore(domains=[MemoryDomain("language", {"choice": "system"})])
        server = CoreIPCServer(core)
        endpoint = server.start()
        self.addCleanup(server.stop)
        request = urllib.request.Request(
            f"http://{endpoint.address}:{endpoint.port}/ipc/host/shutdown",
            data=b"{}",
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(401, raised.exception.code)

    def test_loopback_server_requires_one_time_bootstrap_and_serves_core(self) -> None:
        core = CoreStore(domains=[MemoryDomain("language", {"choice": "system"})])
        server = CoreIPCServer(core)
        endpoint = server.start()
        self.addCleanup(server.stop)
        self.assertNotEqual(0, endpoint.port)
        self.assertTrue(endpoint.one_time_auth)
        self.assertNotIn("bootstrap_token", endpoint.to_mapping())

        # The bootstrap credential is intentionally one-shot.
        client = CoreIPCClient(endpoint, server.bootstrap_token)
        self.assertEqual("system", client.call("snapshot")["snapshot"]["language"])
        disk_state = client.call("disk_state", {"domains": ["language"]})
        self.assertEqual({"language"}, set(disk_state["disk"]))
        second = CoreIPCClient(endpoint, server.bootstrap_token)
        with self.assertRaises(Exception):
            second.call("snapshot")
        client.close()
        second.close()

    def test_loopback_server_renews_a_valid_session_without_restarting_core(self) -> None:
        core = CoreStore(domains=[MemoryDomain("language", {"choice": "system"})])
        server = CoreIPCServer(core)
        endpoint = server.start()
        self.addCleanup(server.stop)
        client = CoreIPCClient(endpoint, server.bootstrap_token)
        self.addCleanup(client.close)

        self.assertEqual("system", client.call("snapshot")["snapshot"]["language"])
        session = client._session_token
        with server._lock:
            server._sessions[session].expires_at = time.monotonic() + 0.01

        # The native host's quiet event poll is authenticated activity. It
        # must keep its Core session alive instead of later forcing a Core
        # replacement while the LiteLLM proxy is serving remote streams.
        status, _body, _headers = client._http(
            "/ipc/events?subscription_id=missing&timeout=0",
            token=session,
        )
        self.assertEqual(200, status)
        with server._lock:
            self.assertGreater(server._sessions[session].expires_at, time.monotonic() + 60)

        client.renew_session()

        self.assertEqual(session, client._session_token)
        self.assertTrue(server._valid_session(session))
        self.assertEqual("system", client.call("snapshot")["snapshot"]["language"])
        with server._lock:
            server._sessions[session].expires_at = time.monotonic() - 1

        request = {"protocol_version": 1, "request_id": "expired-session", "method": "snapshot", "params": {}}
        status, _body, _headers = client._http("/ipc", payload=encode_message(request), token=session)
        self.assertEqual(401, status)

    def test_subscribe_receives_versioned_snapshot_event(self) -> None:
        core = CoreStore(domains=[MemoryDomain("language", {"choice": "system"})])
        server = CoreIPCServer(core)
        endpoint = server.start()
        self.addCleanup(server.stop)
        client = CoreIPCClient(endpoint, server.bootstrap_token)
        self.addCleanup(client.close)
        received: list[dict[str, object]] = []
        ready = threading.Event()

        def on_event(event: dict[str, object]) -> None:
            received.append(event)
            ready.set()

        unsubscribe = client.subscribe(on_event)
        self.addCleanup(unsubscribe)
        client.call("dispatch", {"action": {"domain": "language", "type": "set", "payload": {"choice": "en"}}})
        self.assertTrue(ready.wait(2.0))
        self.assertEqual(1, received[-1]["protocol_version"])
        self.assertEqual("snapshot", received[-1]["event"])

    def test_a_subscription_is_sent_only_the_topics_it_asked_for(self) -> None:
        """A subscription's topics decide which events reach it.

        The field used to be validated and then discarded, so a client that
        asked for one topic was sent every event.  A topic is the event's own
        name, an absent list means every event, and an empty list means none.
        """

        core = CoreStore(domains=[MemoryDomain("language", {"choice": "system"})])
        server = CoreIPCServer(core)
        endpoint = server.start()
        self.addCleanup(server.stop)
        client = CoreIPCClient(endpoint, server.bootstrap_token)
        self.addCleanup(client.close)
        ask = {"action": {"domain": "language", "type": "set", "payload": {"choice": "en"}}}

        asked: list[dict[str, object]] = []
        asked_ready = threading.Event()

        def on_asked(event: dict[str, object]) -> None:
            asked.append(event)
            asked_ready.set()

        unsubscribe = client.subscribe(on_asked, topics=["snapshot"])
        self.addCleanup(unsubscribe)
        client.call("dispatch", ask)
        self.assertTrue(asked_ready.wait(2.0))
        self.assertEqual("snapshot", asked[-1]["event"])

        nothing: list[dict[str, object]] = []
        unsubscribe_none = client.subscribe(nothing.append, topics=[])
        self.addCleanup(unsubscribe_none)
        client.call("dispatch", {"action": {"domain": "language", "type": "set", "payload": {"choice": "system"}}})
        time.sleep(0.5)
        self.assertEqual([], nothing)

    def test_authenticated_host_exchanges_file_path_for_opaque_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / "selected.json"
            selected.write_text("{}", encoding="utf-8")
            core = CoreStore(domains=[MemoryDomain("language", {"choice": "system"})])
            server = CoreIPCServer(core)
            endpoint = server.start()
            self.addCleanup(server.stop)
            client = CoreIPCClient(endpoint, server.bootstrap_token)
            self.addCleanup(client.close)

            capability = client.register_file_capability(str(selected), "import")

            self.assertNotIn(str(selected), capability)
            self.assertEqual(selected, core.file_capabilities.resolve(capability, "import"))

    def test_a_dead_session_takes_its_subscriptions_with_it(self) -> None:
        """An expired session's subscriptions are swept with its capabilities.

        Nothing ever removed them, so every relaunched host left subscriptions
        (and the events queued in them) behind for the Core's lifetime, and a
        poll reusing the id read as a normal quiet heartbeat.
        """

        core = CoreStore(domains=[MemoryDomain("language", {"choice": "system"})])
        server = CoreIPCServer(core)
        endpoint = server.start()
        self.addCleanup(server.stop)
        client = CoreIPCClient(endpoint, server.bootstrap_token)
        self.addCleanup(client.close)

        subscription_id = client.call("subscribe", {})["subscription_id"]
        with server._lock:
            session = next(iter(server._sessions.values()))
            session.expires_at = 0.0

        self.assertFalse(server._valid_session(session.token))
        with server._lock:
            self.assertNotIn(subscription_id, server._subscriptions)

    def test_subscription_closes_cleanly_after_server_stops(self) -> None:
        core = CoreStore(domains=[MemoryDomain("language", {"choice": "system"})])
        server = CoreIPCServer(core)
        endpoint = server.start()
        client = CoreIPCClient(endpoint, server.bootstrap_token)
        unsubscribe = client.subscribe(lambda _event: None)

        server.stop()
        time.sleep(0.25)
        unsubscribe()
        client.close()


if __name__ == "__main__":
    unittest.main()
