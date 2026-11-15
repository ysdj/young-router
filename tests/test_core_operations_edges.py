from __future__ import annotations

import unittest
from unittest import mock

from young_router.core.operations import CoreServiceController


class CoreServiceControllerEdgeTests(unittest.TestCase):
    def test_stop_ends_a_verified_orphan_of_a_dead_core(self) -> None:
        controller = CoreServiceController("/tmp/unused-runtime")
        with mock.patch.object(controller, "_pid", return_value=None), mock.patch.object(
            controller, "_recorded_proxy_is_orphaned", return_value=4242
        ), mock.patch.object(controller, "_stop_process_group") as stop_group, mock.patch.object(
            controller, "_remove_owner_files"
        ) as remove, mock.patch.object(
            controller, "_invalidate_status_cache"
        ), mock.patch.object(
            controller, "status", return_value={"state": "stopped"}
        ):
            result = controller.stop()

        self.assertEqual({"state": "stopped"}, result)
        stop_group.assert_called_once_with(4242)
        remove.assert_called_once_with()

    def test_reload_drops_a_health_answer_memoized_before_the_roll(self) -> None:
        controller = CoreServiceController("/tmp/unused-runtime")
        controller._health_cache = (12390, 0.0, True)
        seen: list[object] = []

        def health(*_args: object) -> bool:
            seen.append(controller._health_cache)
            return True

        with mock.patch.object(controller, "_pid", return_value=4811), mock.patch.object(
            controller, "_rolling_restart", return_value=True
        ), mock.patch.object(controller, "_health", side_effect=health), mock.patch.object(
            controller, "_stage_runtime_config"
        ), mock.patch.object(
            controller, "reset_transient_routing_state"
        ), mock.patch.object(
            controller, "status", return_value={"state": "running"}
        ):
            controller.reload()

        self.assertEqual([None], seen)

    def test_stopped_status_reuses_the_resolved_configured_port(self) -> None:
        controller = CoreServiceController("/tmp/unused-runtime")
        with mock.patch.object(controller, "_pid", return_value=None), mock.patch.object(
            controller, "_health", return_value=False
        ), mock.patch.object(
            controller, "_runtime_env", return_value={"LITELLM_PORT": "4321"}
        ) as runtime_env:
            first = controller.status(force=False)
            second = controller.status(force=False)

        self.assertEqual("stopped", first["state"])
        self.assertEqual("stopped", second["state"])
        runtime_env.assert_called_once_with(strict=False)


if __name__ == "__main__":
    unittest.main()
