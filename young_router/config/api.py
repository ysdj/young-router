from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pathlib
import shutil
import sys
from typing import Any

from .load import (
    CONFIG_DOCUMENT_CONFIG_KEY,
    CONFIG_DOCUMENT_DISABLED_KEY,
    config_document_from_path,
    load_config,
    load_config_document,
    normalize_config_document,
)
from .schema import (
    CONFIG_YAML,
    DISABLED_MODELS_KEY,
    _as_dict,
    _as_list,
    _assert_expected_revision,
    _bool_value,
    _config_revision,
    _disabled_models_path,
    _string_value,
    load_yaml_text,
)


def _assert_unique_deployment_ids(entries: list[dict[str, Any]]) -> None:
    seen: dict[str, int] = {}
    for index, entry in enumerate(entries, start=1):
        model_info = _as_dict(_as_dict(entry).get("model_info"))
        deployment_id = _string_value(model_info.get("id")).strip()
        if not deployment_id:
            continue
        previous_index = seen.get(deployment_id)
        if previous_index is not None:
            raise ValueError(
                f"Duplicate deployment id in generated model entries: {deployment_id} "
                f"(models #{previous_index} and #{index})"
            )
        seen[deployment_id] = index


def _router_settings_restated(
    original_data: object,
    *,
    active_entries: list[dict[str, Any]],
    served: list[str],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Restate ``router_settings`` against the groups this document now serves.

    A public model is a group name several routes share, so renaming one writes
    the new ``model_name`` on every route and leaves ``router_settings`` naming the
    old group.  LiteLLM reads those tables per request, so the leftover name is
    not a stale comment: it is a hop whose group has no deployments, which fails
    the turn and wraps the original upstream failure into a 500 that reports only
    ``Fallback to <gone> also failed``.

    The routes identify a renamed group on their own: a deployment id survives a
    rename, so a group whose ids now carry a different name *is* that group under
    its new name.  Ids are only matched one-to-one, because a group assembled by
    hand from an existing route's ids is not a rename of it.

    Returns the restated ``router_settings`` map -- or ``None`` when no group
    reference moved, so a save that renames nothing leaves the section's own
    spelling, anchors, and comments exactly as they were -- and the group names
    whose reference was dropped because no route serves them any more.
    """

    from .dump import _group_references_restated

    root = _as_dict(original_data)
    settings = _as_dict(root.get("router_settings"))
    if not settings:
        return None, []
    previous_by_id: dict[str, str] = {}
    for entry in _as_list(root.get("model_list")):
        item = _as_dict(entry)
        deployment_id = _string_value(_as_dict(item.get("model_info")).get("id")).strip()
        name = _string_value(item.get("model_name")).strip()
        if deployment_id and name:
            previous_by_id[deployment_id] = name
    current_by_id: dict[str, str] = {}
    for entry in active_entries:
        item = _as_dict(entry)
        deployment_id = _string_value(_as_dict(item.get("model_info")).get("id")).strip()
        name = _string_value(item.get("model_name")).strip()
        if deployment_id and name:
            current_by_id[deployment_id] = name
    # One old name is a rename only when every route it had still exists under
    # one other name; a group that lost or gained a route was edited, not renamed.
    previous_ids_by_name: dict[str, set[str]] = {}
    for deployment_id, name in previous_by_id.items():
        previous_ids_by_name.setdefault(name, set()).add(deployment_id)
    current_ids_by_name: dict[str, set[str]] = {}
    for deployment_id, name in current_by_id.items():
        current_ids_by_name.setdefault(name, set()).add(deployment_id)
    renames: dict[str, str] = {}
    for old_name, ids in previous_ids_by_name.items():
        if old_name in current_ids_by_name:
            continue
        candidates = [
            new_name
            for new_name, new_ids in current_ids_by_name.items()
            if new_ids == ids and new_name not in previous_ids_by_name
        ]
        if len(candidates) == 1:
            renames[old_name] = candidates[0]
    restated, dropped = _group_references_restated(
        settings,
        renames=renames,
        served={str(name).strip() for name in served if str(name).strip()},
    )
    # Leaving ``router_settings`` alone when no reference moved keeps a save that
    # renames nothing from rewriting a section's anchors and comments.
    return (restated if restated is not settings else None), dropped


def save_config(
    providers: list[dict[str, Any]],
    path: pathlib.Path = CONFIG_YAML,
    expected_revision: Any = None,
    document: Any = None,
) -> dict[str, Any]:
    # Loading the editor only needs PyYAML. Import the write path here because
    # it reaches LiteLLM and otherwise adds seconds to every editor open.
    from .dump import (
        _dump_model_list_section,
        _dump_providers_section,
        _dump_section,
        _entry_from_editor,
        _group_references_restated,
        _replace_top_level_sections,
        _unique_model_groups,
        _write_atomic,
    )

    _assert_expected_revision(path, expected_revision)
    source_document = (
        config_document_from_path(path)
        if document is None
        else normalize_config_document(document)
    )

    active_entries: list[dict[str, Any]] = []
    disabled_entries: list[dict[str, Any]] = []
    provider_count = 0
    seen_providers: set[str] = set()
    seen_deployment_ids: set[str] = set()
    model_index = 0

    for provider in providers:
        name = str(provider.get("name", "")).strip()
        if not name:
            raise ValueError("Every provider needs a name")
        if name in seen_providers:
            raise ValueError(f"Duplicate provider name: {name}")
        seen_providers.add(name)
        provider_count += 1
        provider_enabled = _bool_value(provider.get("enabled"), True)
        for model in _as_list(provider.get("models")):
            model_index += 1
            model_dict = _as_dict(model)
            model_enabled = _bool_value(model_dict.get("model_enabled"), _bool_value(model_dict.get("enabled"), True))
            effective_enabled = provider_enabled and model_enabled
            enabled, entry = _entry_from_editor(
                model_dict,
                provider,
                model_index,
                use_provider_aliases=effective_enabled,
                effective_enabled=effective_enabled,
                seen_deployment_ids=seen_deployment_ids,
            )
            if enabled:
                active_entries.append(entry)
            else:
                disabled_entries.append(entry)

    _assert_unique_deployment_ids(active_entries + disabled_entries)

    original = source_document[CONFIG_DOCUMENT_CONFIG_KEY]
    original_data = load_yaml_text(original, pathlib.Path("config.yaml"))
    existing_groups = _as_list(_as_dict(original_data.get("litellm_settings")).get("public_model_groups"))
    settings = dict(_as_dict(original_data.get("litellm_settings")))
    settings["public_model_groups"] = _unique_model_groups(active_entries, existing_groups)
    # A public model is a group name, not a record of its own, so renaming one
    # rewrites the routes and nothing else.  ``router_settings`` still names the
    # old group in its fallback and alias tables, and LiteLLM resolves those per
    # request: a deleted or renamed group is a hop with no deployments, which
    # fails the turn with ``no healthy deployments`` and wraps the original
    # upstream failure into a 500 that hides the cause.  Restate those tables
    # against the groups this document now serves, reading an old name as the one
    # that replaced it by the routes themselves -- the deployment ids are what
    # survive a rename, so they identify a renamed group without the caller
    # having to say what it renamed.
    groups_renamed, groups_dropped = _router_settings_restated(
        original_data,
        active_entries=active_entries,
        served=settings["public_model_groups"],
    )
    next_text = _replace_top_level_sections(
        original,
        {
            "providers": _dump_providers_section(providers),
            "model_list": _dump_model_list_section("model_list", active_entries),
            "litellm_settings": _dump_section("litellm_settings", settings),
            # A document with no ``router_settings`` gains none: the section is
            # only restated where the file already has one.
            **(
                {"router_settings": _dump_section("router_settings", groups_renamed)}
                if groups_renamed
                else {}
            ),
        },
    )

    load_yaml_text(next_text, path)

    next_disabled_text: str | None = None
    if disabled_entries:
        disabled_block = _dump_model_list_section(
            DISABLED_MODELS_KEY, disabled_entries
        )
        original_disabled = source_document[CONFIG_DOCUMENT_DISABLED_KEY]
        next_disabled_text = (
            _replace_top_level_sections(
                original_disabled,
                {DISABLED_MODELS_KEY: disabled_block},
            )
            if original_disabled is not None
            else disabled_block
        )
    if next_disabled_text is not None:
        load_yaml_text(
            next_disabled_text,
            pathlib.Path("config.disabled-models.yaml"),
        )
        next_disabled_text = next_disabled_text.rstrip() + "\n"

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup: pathlib.Path | None = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
    else:
        # A first-run editor starts from a validated in-memory empty document.
        # Create only after the complete candidate above has validated and the
        # missing-file revision has been checked, using the same atomic 0600
        # writer as ordinary saves.
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # `_assert_expected_revision` above observed a missing file. Refuse a
        # concurrent creator (including a symlink) instead of overwriting a
        # configuration that appeared while the candidate was validating.
        if path.exists() or path.is_symlink():
            raise ValueError(
                "config.yaml changed on disk since this editor window loaded. "
                "Close and reopen Providers & Models, then apply your changes again."
            )
    _write_atomic(path, next_text)

    disabled_path = _disabled_models_path(path)
    disabled_backup = ""
    if disabled_entries:
        if disabled_path.exists():
            disabled_backup_path = disabled_path.with_name(f"{disabled_path.name}.bak-{stamp}")
            shutil.copy2(disabled_path, disabled_backup_path)
            os.chmod(disabled_backup_path, 0o600)
            disabled_backup = str(disabled_backup_path)
        _write_atomic(disabled_path, next_disabled_text)
    elif disabled_path.exists():
        disabled_backup_path = disabled_path.with_name(f"{disabled_path.name}.bak-{stamp}")
        shutil.copy2(disabled_path, disabled_backup_path)
        os.chmod(disabled_backup_path, 0o600)
        disabled_backup = str(disabled_backup_path)
        disabled_path.unlink()

    return {
        "providers": provider_count,
        "active": len(active_entries),
        "disabled": len(disabled_entries),
        "backup": str(backup) if backup is not None else "",
        "disabled_path": str(disabled_path) if disabled_entries else "",
        "disabled_backup": disabled_backup,
        "revision": _config_revision(path),
        # Group names a removed route no longer serves, whose fallback or alias
        # reference was dropped from ``router_settings``.  A hop that disappears
        # says so here rather than only in the file it edited.
        "dropped_group_references": sorted(set(groups_dropped)),
        "document": {
            CONFIG_DOCUMENT_CONFIG_KEY: next_text.rstrip() + "\n",
            CONFIG_DOCUMENT_DISABLED_KEY: next_disabled_text,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Load and save Young Router provider configuration.")
    parser.add_argument("command", choices=["load", "save"])
    parser.add_argument("--config", default=str(CONFIG_YAML))
    args = parser.parse_args()
    path = pathlib.Path(args.config).expanduser()

    try:
        if args.command == "load":
            json.dump(load_config(path), sys.stdout, ensure_ascii=False, indent=2)
            print()
            return 0

        if args.command == "save":
            payload = json.load(sys.stdin)
            providers = payload.get("providers") if isinstance(payload, dict) else None
            if not isinstance(providers, list):
                raise ValueError("Save payload must contain a providers list")
            result = save_config(
                providers,
                path,
                payload.get("expected_revision"),
                payload.get("document"),
            )
        else:
            raise ValueError("Unsupported configuration editor command.")
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
