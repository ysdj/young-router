from __future__ import annotations

import json
from pathlib import Path
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


def test_initialized_target_remains_owned_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_LIST_PLUGIN_DATA", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"enabled": False, "initializedTargets": {"codex": True}}))
    assert model_list_plugin.target_initialized("codex")
    assert not model_list_plugin.target_enabled("codex")
    assert not model_list_plugin.target_initialized("claude")


def test_legacy_plugin_configuration_owns_both_targets(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_LIST_PLUGIN_DATA", str(tmp_path))
    config_path = tmp_path / "config.json"
    assert not model_list_plugin.target_initialized("codex")
    config_path.write_text(json.dumps({"enabled": False, "targets": {"codex": True, "claude": True}}))
    assert model_list_plugin.target_initialized("codex")
    assert model_list_plugin.target_initialized("claude")
    assert not model_list_plugin.target_enabled("codex")
    config_path.write_text(json.dumps({"enabled": False, "initializedTargets": None}))
    assert not model_list_plugin.target_initialized("codex")
    config_path.write_text(json.dumps({"enabled": True, "targets": {"codex": True}, "initializedTargets": {}}))
    assert model_list_plugin.target_initialized("codex")


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
