"""A pull reloads the runtime pane under the runtime domain's own write lane."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Callable
from unittest import mock

from young_router.core import CoreStore
from young_router.core.domains.providers_models import ProvidersModelsDomain
from young_router.core.domains.relay_accounts import RelayAccountsDomain
from young_router.core.domains.webdav import WebDAVSettingsDomain
from young_router.core.service import MemoryDomain
from young_router.webdav import core as webdav_core


CONFIG = """providers:
  primary:
    api_base: "https://example.test/v1"
    api_keys:
      - name: default
        value: "replace-me-secret"
model_list: []
"""


class _RuntimePane(MemoryDomain):
    """A runtime pane that records whether its lane was held when it reloaded."""

    def __init__(self, lane_held: Callable[[], bool]) -> None:
        super().__init__("runtime")
        self._lane_held = lane_held
        self.held: list[bool] = []

    def reload(self):
        self.held.append(self._lane_held())
        return super().reload()


class CoreServiceEdgesTests(unittest.TestCase):
    @staticmethod
    def _webdav(root: Path, config: Path) -> WebDAVSettingsDomain:
        domain = WebDAVSettingsDomain(
            root / "webdav.json",
            enabled_path=root / "webdav.enabled",
            status_path=root / "webdav-status.json",
            config_path=config,
            state_path=root / "webdav-state.json",
            conflict_path=root / "webdav-conflict.json",
        )
        domain.stage_secret("password", None, "replace-webdav-password")
        domain.dispatch(
            "patch",
            {"url": "https://webdav.example.test/config/", "username": "person", "remote_name": "menu-config.json"},
        )
        domain.apply()
        return domain

    def test_a_pull_reloads_the_runtime_pane_under_its_own_lane(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text(CONFIG, encoding="utf-8")
            providers = ProvidersModelsDomain(config)
            relay = RelayAccountsDomain(storage_path=root / ".litellm-runtime" / "relay-accounts.json")
            core = CoreStore(domains=[providers, relay, self._webdav(root, config)])
            runtime = _RuntimePane(lambda: core._domain_lock("runtime")._is_owned())
            core.register_domain(runtime)
            keys = ["sk-before", "sk-after"]
            bundle = webdav_core.bundle_from_files({"config.yaml": CONFIG.replace("primary:", "arrived:").encode()})
            with mock.patch("young_router.core.host_profile.active", return_value=False), mock.patch(
                "young_router.core.service._document_master_key",
                side_effect=lambda _path: keys.pop(0) if len(keys) > 1 else keys[0],
            ), mock.patch.object(webdav_core.WebDAVClient, "get", return_value=bundle), mock.patch(
                "young_router.webdav.operations.publish_remote_digests", return_value=True
            ):
                core.dispatch(
                    {"domain": "webdav", "type": "pull", "payload": {"sections": ["providers_models", "relay_accounts"]}}
                )

            self.assertEqual([True], runtime.held)


if __name__ == "__main__":
    unittest.main()
