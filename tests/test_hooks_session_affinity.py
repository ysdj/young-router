from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from hook_test_utils import *


def _deployment(deployment_id: str, order: float = 0.1) -> dict:
    return {
        "model_name": "default-chat",
        "litellm_params": {
            "model": "openai/vendor-chat",
            "api_base": f"https://{deployment_id}.example.test/v1",
            "order": order,
        },
        "model_info": {
            "id": deployment_id,
            "provider": "unknown-provider",
            "api_key_name": "r-cheap",
        },
        "default_params": {"model": "openai/vendor-chat"},
    }


class SessionAffinityIdTests(HookTestCase):
    def test_extracts_first_session_like_id(self) -> None:
        hooks, _ = load_hook_module()
        request = {
            "proxy_server_request": {"headers": {"session_id": "sess-123"}},
            "input": "hi",
        }
        self.assertEqual(
            hooks._request_session_affinity_id(request),
            "sess-123",
        )
        metadata_request = {
            "litellm_metadata": {"thread_id": "thread-abc"},
        }
        self.assertEqual(
            hooks._request_session_affinity_id(metadata_request),
            "thread-abc",
        )
        self.assertIsNone(hooks._request_session_affinity_id({"model": "m"}))
        self.assertIsNone(hooks._request_session_affinity_id(None))


class SessionAffinityRecordingTests(HookTestCase):
    def test_record_and_lookup_via_shared_file(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session-deployment-affinity.json"
            self.set_env(hooks._SESSION_DEPLOYMENT_AFFINITY_FILE_ENV, str(path))

            request = {
                "proxy_server_request": {"headers": {"session_id": "sess-123"}},
                "model_info": {"id": "dep-a"},
                "metadata": {"model_group": "default-chat"},
            }
            hooks._record_session_deployment_affinity(request)

            self.assertTrue(path.is_file())
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(payload), 1)
            entry = next(iter(payload.values()))
            self.assertEqual(entry["deployment_id"], "dep-a")

            self.assertEqual(
                hooks._session_affinity_deployment_id(request, "default-chat"),
                "dep-a",
            )

    def test_record_skips_without_session_or_deployment(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session-deployment-affinity.json"
            self.set_env(hooks._SESSION_DEPLOYMENT_AFFINITY_FILE_ENV, str(path))

            hooks._record_session_deployment_affinity(
                {"model_info": {"id": "dep-a"}, "metadata": {"model_group": "g"}}
            )
            hooks._record_session_deployment_affinity(
                {"proxy_server_request": {"headers": {"session_id": "s"}}, "metadata": {"model_group": "g"}}
            )
            self.assertFalse(path.exists())

    def test_lookup_is_scoped_to_model_group(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session-deployment-affinity.json"
            self.set_env(hooks._SESSION_DEPLOYMENT_AFFINITY_FILE_ENV, str(path))

            request = {
                "proxy_server_request": {"headers": {"session_id": "sess-123"}},
                "model_info": {"id": "dep-a"},
                "metadata": {"model_group": "default-chat"},
            }
            hooks._record_session_deployment_affinity(request)

            self.assertIsNone(
                hooks._session_affinity_deployment_id(request, "other-model")
            )

    def test_pruning_drops_stale_entries(self) -> None:
        hooks, _ = load_hook_module()
        now = 1000.0
        entries = {
            "old": {"deployment_id": "dep-old", "updated_at": now - hooks._SESSION_DEPLOYMENT_AFFINITY_TTL_SECONDS - 1},
            "fresh": {"deployment_id": "dep-fresh", "updated_at": now - 10},
        }
        pruned = hooks._prune_session_affinity_entries(entries, now)
        self.assertEqual(set(pruned), {"fresh"})

        cap = hooks._SESSION_DEPLOYMENT_AFFINITY_MAX_ENTRIES
        capped = {
            f"k{i}": {"deployment_id": f"dep-{i}", "updated_at": float(i)}
            for i in range(cap + 10)
        }
        pruned = hooks._prune_session_affinity_entries(capped, now)
        self.assertEqual(len(pruned), cap)
        self.assertIn(f"k{cap + 9}", pruned)
        self.assertNotIn("k0", pruned)


class SessionAffinityFilterTests(HookTestCase):
    def _request(self) -> dict:
        return {
            "proxy_server_request": {"headers": {"session_id": "sess-123"}},
            "model": "default-chat",
            "stream": True,
        }

    def test_narrows_candidates_to_sticky_deployment(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session-deployment-affinity.json"
            self.set_env(hooks._SESSION_DEPLOYMENT_AFFINITY_FILE_ENV, str(path))
            hooks._record_session_deployment_affinity(
                {
                    "proxy_server_request": {"headers": {"session_id": "sess-123"}},
                    "model_info": {"id": "dep-b"},
                    "metadata": {"model_group": "default-chat"},
                }
            )

            candidates, sticky, applied = hooks._with_session_deployment_affinity(
                [_deployment("dep-a"), _deployment("dep-b"), _deployment("dep-c")],
                self._request(),
                "default-chat",
            )
            self.assertTrue(applied)
            self.assertEqual(sticky, "dep-b")
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["model_info"]["id"], "dep-b")

    def test_no_sticky_or_absent_sticky_keeps_candidates(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as directory:
            self.set_env(
                hooks._SESSION_DEPLOYMENT_AFFINITY_FILE_ENV,
                str(Path(directory) / "session-deployment-affinity.json"),
            )
            original = [_deployment("dep-a"), _deployment("dep-b")]

            candidates, sticky, applied = hooks._with_session_deployment_affinity(
                original,
                self._request(),
                "default-chat",
            )
            self.assertFalse(applied)
            self.assertIsNone(sticky)
            self.assertEqual(candidates, original)

            hooks._record_session_deployment_affinity(
                {
                    "proxy_server_request": {"headers": {"session_id": "sess-123"}},
                    "model_info": {"id": "dep-gone"},
                    "metadata": {"model_group": "default-chat"},
                }
            )
            candidates, sticky, applied = hooks._with_session_deployment_affinity(
                original,
                self._request(),
                "default-chat",
            )
            self.assertFalse(applied)
            self.assertEqual(sticky, "dep-gone")
            self.assertEqual(candidates, original)

    def test_disabled_env_disables_narrowing(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session-deployment-affinity.json"
            self.set_env(hooks._SESSION_DEPLOYMENT_AFFINITY_FILE_ENV, str(path))
            self.set_env(hooks._SESSION_DEPLOYMENT_AFFINITY_ENV, "0")
            hooks._record_session_deployment_affinity(
                {
                    "proxy_server_request": {"headers": {"session_id": "sess-123"}},
                    "model_info": {"id": "dep-b"},
                    "metadata": {"model_group": "default-chat"},
                }
            )

            candidates, sticky, applied = hooks._with_session_deployment_affinity(
                [_deployment("dep-a"), _deployment("dep-b")],
                self._request(),
                "default-chat",
            )
            self.assertFalse(applied)
            self.assertIsNone(sticky)
            self.assertEqual(len(candidates), 2)

    def test_single_candidate_is_unchanged(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as directory:
            self.set_env(
                hooks._SESSION_DEPLOYMENT_AFFINITY_FILE_ENV,
                str(Path(directory) / "session-deployment-affinity.json"),
            )
            candidates, _sticky, applied = hooks._with_session_deployment_affinity(
                [_deployment("dep-a")],
                self._request(),
                "default-chat",
            )
            self.assertFalse(applied)
            self.assertEqual(len(candidates), 1)


class HookFilterSessionAffinityTests(HookTestCase):
    async def test_filter_deployments_applies_affinity_last(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        with tempfile.TemporaryDirectory() as directory:
            self.set_env(
                hooks._SESSION_DEPLOYMENT_AFFINITY_FILE_ENV,
                str(Path(directory) / "session-deployment-affinity.json"),
            )
            request_kwargs = {
                "proxy_server_request": {"headers": {"session_id": "sess-123"}},
                "model": "default-chat",
                "stream": True,
                "input": "hi",
            }
            hooks._record_session_deployment_affinity(
                {
                    "proxy_server_request": {"headers": {"session_id": "sess-123"}},
                    "model_info": {"id": "dep-b"},
                    "metadata": {"model_group": "default-chat"},
                }
            )

            filtered = await hook.async_filter_deployments(
                model="default-chat",
                healthy_deployments=[_deployment("dep-a"), _deployment("dep-b")],
                messages=None,
                request_kwargs=request_kwargs,
            )
            self.assertEqual(len(filtered), 1)
            self.assertEqual(filtered[0]["model_info"]["id"], "dep-b")

    async def test_sticky_deployment_in_cooldown_falls_back_to_peer(self) -> None:
        """Affinity never forces a cooled deployment: the peer wins instead."""

        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        with tempfile.TemporaryDirectory() as directory:
            self.set_env(
                hooks._DEPLOYMENT_COOLDOWN_FILE_ENV,
                str(Path(directory) / "deployment-cooldowns.json"),
            )
            self.set_env(
                hooks._SESSION_DEPLOYMENT_AFFINITY_FILE_ENV,
                str(Path(directory) / "session-deployment-affinity.json"),
            )
            session_headers = {"headers": {"session_id": "sess-123"}}

            # The session is sticky on dep-b, then dep-b starts failing.
            hooks._record_session_deployment_affinity(
                {
                    "proxy_server_request": session_headers,
                    "model_info": {"id": "dep-b"},
                    "metadata": {"model_group": "default-chat"},
                }
            )
            failure = RuntimeError("upstream peer failure")
            failure.status_code = 503
            hooks._mark_exception_for_deployment_failover(
                failure,
                {
                    "proxy_server_request": session_headers,
                    "model": "default-chat",
                    "litellm_params": _deployment("dep-b")["litellm_params"],
                    "model_info": {"id": "dep-b"},
                },
            )

            filtered = await hook.async_filter_deployments(
                model="default-chat",
                healthy_deployments=[_deployment("dep-a"), _deployment("dep-b")],
                messages=None,
                request_kwargs={
                    "proxy_server_request": session_headers,
                    "model": "default-chat",
                    "stream": True,
                    "input": "hi",
                },
            )
            self.assertEqual(len(filtered), 1)
            self.assertEqual(filtered[0]["model_info"]["id"], "dep-a")

            # Once the peer serves the session, affinity re-points to it, so
            # the next request sticks to the new warm cache, not the dead one.
            hooks._record_deployment_success_for_cooldown(
                {
                    "proxy_server_request": session_headers,
                    "model_info": {"id": "dep-a"},
                    "metadata": {"model_group": "default-chat"},
                }
            )
            candidates, sticky, applied = hooks._with_session_deployment_affinity(
                [_deployment("dep-a"), _deployment("dep-b")],
                {
                    "proxy_server_request": session_headers,
                    "model": "default-chat",
                    "stream": True,
                },
                "default-chat",
            )
            self.assertTrue(applied)
            self.assertEqual(sticky, "dep-a")
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["model_info"]["id"], "dep-a")

    async def test_filter_deployments_without_affinity_keeps_all(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        with tempfile.TemporaryDirectory() as directory:
            self.set_env(
                hooks._SESSION_DEPLOYMENT_AFFINITY_FILE_ENV,
                str(Path(directory) / "session-deployment-affinity.json"),
            )
            filtered = await hook.async_filter_deployments(
                model="default-chat",
                healthy_deployments=[_deployment("dep-a"), _deployment("dep-b")],
                messages=None,
                request_kwargs={
                    "proxy_server_request": {"headers": {"session_id": "sess-new"}},
                    "model": "default-chat",
                    "stream": True,
                },
            )
            self.assertEqual(len(filtered), 2)

    async def test_success_recording_updates_affinity(self) -> None:
        hooks, _ = load_hook_module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session-deployment-affinity.json"
            self.set_env(hooks._SESSION_DEPLOYMENT_AFFINITY_FILE_ENV, str(path))

            hooks._record_deployment_success_for_cooldown(
                {
                    "proxy_server_request": {"headers": {"session_id": "sess-123"}},
                    "model_info": {"id": "dep-b"},
                    "metadata": {"model_group": "default-chat"},
                }
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            entry = next(iter(payload.values()))
            self.assertEqual(entry["deployment_id"], "dep-b")


class AffinityTransientStateResetTests(HookTestCase):
    def test_reset_transient_routing_state_removes_affinity_file(self) -> None:
        from litellm_menu.core.operations import CoreServiceController

        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            affinity_path = controller.paths.affinity
            affinity_path.parent.mkdir(parents=True, exist_ok=True)
            affinity_path.write_text("{}", encoding="utf-8")

            controller.reset_transient_routing_state()
            self.assertFalse(affinity_path.exists())

    def test_runtime_env_exports_affinity_file(self) -> None:
        from litellm_menu.core.operations import CoreServiceController

        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            environment = controller._runtime_env()
            self.assertEqual(
                str(controller.paths.affinity),
                environment["LITELLM_MENU_SESSION_DEPLOYMENT_AFFINITY_FILE"],
            )
