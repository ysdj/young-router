from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from litellm_menu import macos_proxy


class MacOSProxyLauncherTests(unittest.TestCase):
    def test_worker_config_preserves_config_only_cli_behavior(self) -> None:
        config = macos_proxy._worker_config(Path("/private/runtime/config.yaml"))

        self.assertEqual("/private/runtime/config.yaml", config["config"])
        self.assertIs(config["telemetry"], False)
        self.assertIsNone(config["request_timeout"])
        self.assertIs(config["drop_params"], False)
        self.assertIs(config["add_function_to_prompt"], False)

    def test_system_proxy_snapshot_prefers_environment_settings(self) -> None:
        with mock.patch.object(
            macos_proxy.urllib.request,
            "getproxies_environment",
            return_value={"https": "http://environment-proxy.example:8080"},
        ), mock.patch.object(
            macos_proxy.urllib.request,
            "getproxies_macosx_sysconf",
        ) as get_system_proxies, mock.patch.object(
            macos_proxy.urllib.request,
            "_get_proxy_settings",
        ) as get_system_settings:
            snapshot = macos_proxy._system_proxy_snapshot()

        self.assertEqual(
            {
                "source": "environment",
                "proxies": {"https": "http://environment-proxy.example:8080"},
            },
            snapshot,
        )
        get_system_proxies.assert_not_called()
        get_system_settings.assert_not_called()

    def test_system_proxy_snapshot_caches_macos_settings_before_fork(self) -> None:
        with mock.patch.object(
            macos_proxy.urllib.request,
            "getproxies_environment",
            return_value={},
        ), mock.patch.object(
            macos_proxy.urllib.request,
            "getproxies_macosx_sysconf",
            return_value={"https": "http://system-proxy.example:8080"},
        ), mock.patch.object(
            macos_proxy.urllib.request,
            "_get_proxy_settings",
            return_value={"exclude_simple": True, "exceptions": ["*.example.test"]},
        ):
            snapshot = macos_proxy._system_proxy_snapshot()

        self.assertEqual("macos", snapshot["source"])
        self.assertEqual(
            {"https": "http://system-proxy.example:8080"},
            snapshot["proxies"],
        )
        self.assertEqual(
            {"exclude_simple": True, "exceptions": ["*.example.test"]},
            snapshot["settings"],
        )

    def test_run_preloads_proxy_once_and_starts_requested_workers(self) -> None:
        uvicorn = SimpleNamespace(run=mock.Mock())
        uvicorn_subprocess = SimpleNamespace(spawn=None)
        forkserver = object()

        def import_module(name: str) -> object:
            if name == "uvicorn":
                return uvicorn
            if name == "uvicorn._subprocess":
                return uvicorn_subprocess
            raise AssertionError(name)

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            multiprocessing, "set_forkserver_preload", create=True
        ) as set_preload, mock.patch.object(
            multiprocessing, "get_context", return_value=forkserver
        ) as get_context, mock.patch.object(
            macos_proxy.importlib, "import_module", side_effect=import_module
        ), mock.patch.object(
            macos_proxy,
            "_system_proxy_snapshot",
            return_value={"source": "macos", "proxies": {}, "settings": {}},
        ):
            result = macos_proxy.run(
                [
                    "--config",
                    "/private/runtime/config.yaml",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "4000",
                    "--workers",
                    "16",
                ]
            )
            worker_config = json.loads(os.environ["WORKER_CONFIG"])
            proxy_snapshot = json.loads(os.environ[macos_proxy.SYSTEM_PROXY_SNAPSHOT_ENV])

        self.assertEqual(0, result)
        self.assertEqual("/private/runtime/config.yaml", worker_config["config"])
        self.assertIs(worker_config["telemetry"], False)
        self.assertEqual(
            {"source": "macos", "proxies": {}, "settings": {}},
            proxy_snapshot,
        )
        set_preload.assert_called_once_with(["litellm.proxy.proxy_server"])
        get_context.assert_called_once_with("forkserver")
        self.assertIs(forkserver, uvicorn_subprocess.spawn)
        uvicorn.run.assert_called_once_with(
            "litellm.proxy.proxy_server:app",
            host="127.0.0.1",
            port=4000,
            workers=16,
            loop="uvloop",
        )


if __name__ == "__main__":
    unittest.main()
