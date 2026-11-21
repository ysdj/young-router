from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from young_router.core import CoreStore, model_list_plugin
from young_router.core.service import CoreError


def test_plugin_action_dispatches_without_a_domain():
    core = CoreStore()
    with mock.patch.object(model_list_plugin, "manage", return_value={"installed": True}) as manage:
        result = core.dispatch({"type": "model_list_plugin", "payload": {"host": "codex", "operation": "status"}})
    assert result["action_summary"] == {"installed": True}
    manage.assert_called_once_with("codex", "status", options={"host": "codex", "operation": "status"})
    with pytest.raises(CoreError):
        core.dispatch({"type": "model_list_plugin", "payload": {"host": "unknown", "operation": "status"}})


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
