from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tomllib
from unittest import mock

import pytest

from young_router.core import CoreStore, model_list_plugin
from young_router.core.domains.claude import ClaudeSettingsDomain
from young_router.core.domains.codex import CodexSettingsDomain
from young_router.core.service import CoreError


def test_plugin_action_dispatches_without_a_domain():
    core = CoreStore()
    with mock.patch.object(model_list_plugin, "manage", return_value={"installed": True}) as manage:
        result = core.dispatch({"type": "model_list_plugin", "payload": {"host": "codex", "operation": "status"}})
    assert result["action_summary"] == {"installed": True}
    manage.assert_called_once_with("codex", "status")
    with pytest.raises(CoreError):
        core.dispatch({"type": "model_list_plugin", "payload": {"host": "unknown", "operation": "status"}})
    with pytest.raises(CoreError):
        core.dispatch({"type": "model_list_plugin", "payload": {"host": "claude", "operation": "configure"}})


def test_install_reenables_host_and_keeps_installation_after_sync_error():
    row = {"version": "0.2.0", "enabled": False}
    commands = []

    def run(executable, args):
        commands.append((executable, args))
        if args[0] == "sync":
            raise RuntimeError("provider unavailable")
        return ""

    with mock.patch.object(model_list_plugin, "_executable", return_value="codex"), \
         mock.patch.object(model_list_plugin, "_row", return_value=row), \
         mock.patch.object(model_list_plugin, "_binary", return_value=Path("/plugin")), \
         mock.patch.object(model_list_plugin, "_run", side_effect=run), \
         mock.patch.object(model_list_plugin, "status", return_value={"installed": True, "last_error": "provider unavailable"}):
        result = model_list_plugin.manage("codex", "install")

    assert ("codex", ["plugin", "add", model_list_plugin.PLUGIN_ID]) in commands
    assert ("/plugin", ["enable", "--host", "codex", "--defer"]) in commands
    assert result == {"installed": True, "last_error": "provider unavailable"}


def test_target_enabled_is_the_ownership_the_legacy_generator_yields_to(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_LIST_PLUGIN_DATA", str(tmp_path))
    config_path = tmp_path / "config.json"
    assert not model_list_plugin.target_enabled("codex")
    # A host that was initialized and then disabled is not owned: the legacy
    # generator may maintain the list it restored instead of leaving it stale.
    config_path.write_text(json.dumps({
        "enabled": False,
        "targets": {"codex": True, "claude": True},
        "initializedTargets": {"codex": True, "claude": True},
    }))
    assert not model_list_plugin.target_enabled("codex")
    assert not model_list_plugin.target_enabled("claude")
    # Only the plugin's own on state makes it the owner of one host.
    config_path.write_text(json.dumps({
        "enabled": True,
        "targets": {"codex": True, "claude": False},
        "initializedTargets": {"codex": True, "claude": True},
    }))
    assert model_list_plugin.target_enabled("codex")
    assert not model_list_plugin.target_enabled("claude")
    # Configurations from the previous plugin version name their targets the
    # same way; they no longer own a host merely by having been initialized.
    config_path.write_text(json.dumps({"enabled": False, "targets": {"codex": True, "claude": True}}))
    assert not model_list_plugin.target_enabled("codex")
    assert not model_list_plugin.target_enabled("claude")


def test_status_keeps_each_hosts_sync_error_apart():
    row = {"version": "0.2.10", "enabled": True}
    plugin = {
        "enabled": True,
        "targets": {"codex": True, "claude": True},
        "lastError": "codex: boom",
        "lastErrorByHost": {"codex": "codex: boom"},
        "lastSyncAtByTarget": {"claude": "2026-01-01T00:00:00Z"},
        "lastUpdateErrorByHost": {},
    }
    with mock.patch.object(model_list_plugin, "_executable", return_value="claude"), \
         mock.patch.object(model_list_plugin, "_row", return_value=row), \
         mock.patch.object(model_list_plugin, "_binary", return_value=Path("/plugin")), \
         mock.patch.object(model_list_plugin, "_json", return_value=plugin):
        state = model_list_plugin.status("claude")
    assert state["last_error"] == ""
    assert state["last_sync"] == "2026-01-01T00:00:00Z"

    # A plugin that predates the per-host map still reports its one global
    # error, which is the only attribution available for it.
    legacy = {key: value for key, value in plugin.items() if key != "lastErrorByHost"}
    with mock.patch.object(model_list_plugin, "_executable", return_value="claude"), \
         mock.patch.object(model_list_plugin, "_row", return_value=row), \
         mock.patch.object(model_list_plugin, "_binary", return_value=Path("/plugin")), \
         mock.patch.object(model_list_plugin, "_json", return_value=legacy):
        state = model_list_plugin.status("claude")
    assert state["last_error"] == "codex: boom"


def test_desktop_bundled_cli_is_found_newest_first(tmp_path):
    """An app-only machine's Claude Code lives inside Claude Desktop."""

    if sys.platform != "darwin" and os.name != "nt":
        pytest.skip("Claude Desktop keeps its bundled CLI on macOS and Windows only")

    def bundle(version: str, digest: str) -> Path:
        if sys.platform == "darwin":
            path = tmp_path / version / digest / "claude.app" / "Contents" / "MacOS" / "claude"
        else:
            path = tmp_path / version / digest / "claude.exe"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    older = bundle("2.1.0", "older")
    newer = bundle("2.2.0", "newer")
    os.utime(tmp_path / "2.1.0", (1_000_000, 1_000_000))
    os.utime(tmp_path / "2.2.0", (2_000_000, 2_000_000))
    assert model_list_plugin._desktop_claude_cli([tmp_path]) == str(newer)

    if sys.platform == "darwin":
        # A newer version whose binary is not executable is not a command.
        blocked = bundle("2.3.0", "blocked")
        blocked.chmod(0o644)
        os.utime(tmp_path / "2.3.0", (3_000_000, 3_000_000))
        assert model_list_plugin._desktop_claude_cli([tmp_path]) == str(newer)

    assert model_list_plugin._desktop_claude_cli([tmp_path / "missing"]) is None


def test_uninstall_restores_every_host_before_the_plugin_is_removed():
    """Removing the plugin must not leave the other host's catalog pointer behind.

    Young Router is asked to remove the plugin for Claude, but the plugin is
    one installation serving both hosts.  The host's own removal runs after the
    plugin's uninstall, which restores both targets and deletes the local
    config and state that would otherwise keep claiming ownership.
    """

    row = {"version": "0.2.9", "enabled": True}
    commands = []

    def run(executable, args):
        commands.append((executable, args))
        return ""

    with mock.patch.object(model_list_plugin, "_executable", return_value="claude"), \
         mock.patch.object(model_list_plugin, "_row", return_value=row), \
         mock.patch.object(model_list_plugin, "_binary", return_value=Path("/plugin")), \
         mock.patch.object(model_list_plugin, "_run", side_effect=run), \
         mock.patch.object(model_list_plugin, "status", return_value={"installed": False}):
        model_list_plugin.manage("claude", "uninstall")

    assert ("claude", ["plugin", "uninstall", model_list_plugin.PLUGIN_ID]) in commands
    assert ("/plugin", ["uninstall"]) in commands
    assert ("/plugin", ["disable", "--host", "claude"]) not in commands


@pytest.mark.parametrize("host", ["codex", "claude"])
def test_direct_api_commits_the_client_then_syncs(tmp_path, monkeypatch, host):
    monkeypatch.setenv("LITELLM_PORT", "4000")
    home = tmp_path / "codex"
    home.mkdir()
    runtime = tmp_path / "config.yaml"
    runtime.write_text('model_list: []\ngeneral_settings: {master_key: fixture-router-key}\n')
    if host == "codex":
        path = home / "config.toml"
        path.write_text('model_provider = "custom"\n[model_providers.custom]\nbase_url = "http://127.0.0.1:4000/v1"\n')
        adapter = CodexSettingsDomain(runtime, codex_home=home)
        options = {"model_catalog_url": "https://gateway.example.test/catalog"}
    else:
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:4000", "ANTHROPIC_AUTH_TOKEN": "fixture-router-key", "KEEP": "me"}}))
        adapter = ClaudeSettingsDomain(path, runtime_config_path=runtime, runtime_root=tmp_path)
        options = {"auth_mode": "x-api-key"}
    core = CoreStore(domains=[adapter])
    original = path.read_bytes()
    key = "fixture-direct-api-key"
    token = "direct-api-form-token"
    staged = core.stage_secret(host, "direct_api_key", token, key, revision=core.snapshot()["revision"])
    assert staged["present"]
    assert original == path.read_bytes()
    assert key not in json.dumps(core.snapshot())

    payload = {"base_url": "https://gateway.example.test/v1", "model": "example-model", "key_token": token, **options}
    action = {"domain": host, "type": "use_direct_api", "payload": payload}
    with mock.patch.object(model_list_plugin, "sync_if_enabled") as sync:
        with pytest.raises(CoreError):
            core.dispatch({**action, "payload": {**payload, "base_url": "invalid-url"}})
        with mock.patch.object(adapter, "apply", side_effect=RuntimeError("disk write failed")):
            with pytest.raises(CoreError, match="disk write failed"):
                core.dispatch(action)
        assert path.read_bytes() == original
        sync.assert_not_called()

        def synced(client):
            assert client == host and path.read_bytes() != original
            return {"enabled": True, "last_sync": "now"}

        sync.side_effect = synced
        result = core.dispatch(action, expected_revision=core.snapshot()["revision"])
        assert set(result) == {"revision", "action_summary"}
        assert result["action_summary"]["last_sync"] == "now"
        sync.assert_called_once_with(host)
    assert key not in json.dumps(core.snapshot())
    assert not core.secret_descriptor(host, "direct_api_key", token)["present"]
    descriptor = core.trusted_secret_read_descriptor(host, "direct_api_key", token)
    assert descriptor["present"]
    assert core.trusted_secret_value(host, "direct_api_key", token, revision=descriptor["revision"]) == key
    if host == "codex":
        content = tomllib.loads(path.read_text())
        provider = content["model_providers"][content["model_provider"]]
        assert provider["base_url"] == payload["base_url"]
        assert provider["model_catalog_url"] == options["model_catalog_url"]
        assert provider["experimental_bearer_token"] == key
        assert content["model"] == "example-model"
    else:
        content = json.loads(path.read_text())
        assert content["env"] == {"ANTHROPIC_BASE_URL": "https://gateway.example.test", "ANTHROPIC_API_KEY": key, "KEEP": "me"}
        assert content["model"] == "example-model"

    with mock.patch.object(model_list_plugin, "sync_if_enabled") as sync:
        core.dispatch({**action, "payload": {**payload, "key_token": "", "clear_key": True}})
        sync.assert_called_once_with(host)
    assert key not in path.read_text()
    assert not core.trusted_secret_read_descriptor(host, "direct_api_key", token)["present"]


def test_saving_the_yrapi_triplet_from_the_form_is_the_on_direction(tmp_path, monkeypatch):
    """The form and the switch are one state: its full triplet adopts the proxy."""

    monkeypatch.setenv("LITELLM_PORT", "4000")
    home = tmp_path / "codex"
    home.mkdir()
    runtime = tmp_path / "config.yaml"
    runtime.write_text('model_list: []\ngeneral_settings: {master_key: fixture-router-key}\n')
    path = home / "config.toml"
    path.write_text(
        'model_provider = "custom"\n'
        "\n"
        '[model_providers.custom]\n'
        'base_url = "http://127.0.0.1:4000/v1"\n'
        'requires_openai_auth = true\n'
    )
    (home / "auth.json").write_text('{"OPENAI_API_KEY": "fixture-router-key"}\n')
    adapter = CodexSettingsDomain(runtime, codex_home=home)
    core = CoreStore(domains=[adapter])
    key = "fixture-router-key"
    token = "direct-api-form-token"
    core.stage_secret("codex", "direct_api_key", token, key, revision=core.snapshot()["revision"])
    payload = {
        "base_url": "http://127.0.0.1:4000/v1",
        "model": "example-model",
        "model_catalog_url": "",
        "key_token": token,
    }
    with mock.patch.object(model_list_plugin, "sync_if_enabled", return_value={}) as sync:
        core.dispatch({"domain": "codex", "type": "use_direct_api", "payload": payload})
        sync.assert_called_once_with("codex")
    assert adapter.snapshot()["uses_yrapi_api"]
    document = tomllib.loads(path.read_text())
    assert document["model_provider"] == "custom"
    assert "young-router-direct" not in document["model_providers"]
    assert document["model_providers"]["custom"]["base_url"] == "http://127.0.0.1:4000/v1"
