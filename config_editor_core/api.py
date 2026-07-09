from __future__ import annotations

from .schema import *
from .load import *
from .dump import *


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


def save_config(
    providers: list[dict[str, Any]],
    path: pathlib.Path = CONFIG_YAML,
    expected_revision: Any = None,
) -> dict[str, Any]:
    _assert_expected_revision(path, expected_revision)

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
            key_name = str(model_dict.get("api_key_name", "")).strip()
            key_enabled = _key_enabled(provider, key_name)
            model_enabled = _bool_value(model_dict.get("model_enabled"), _bool_value(model_dict.get("enabled"), True))
            effective_enabled = provider_enabled and key_enabled and model_enabled
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

    original = path.read_text(encoding="utf-8")
    original_data = _load_yaml(path)
    existing_groups = _as_list(_as_dict(original_data.get("litellm_settings")).get("public_model_groups"))
    next_text = _replace_top_level_section(original, "providers", _dump_providers_section(providers))
    next_text = _replace_top_level_section(next_text, "model_list", _dump_model_list_section("model_list", active_entries))
    next_text = _replace_public_model_groups(next_text, _unique_model_groups(active_entries, existing_groups))

    parsed = yaml.safe_load(next_text)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("providers"), dict) or not isinstance(parsed.get("model_list"), list):
        raise ValueError("Refusing to save invalid config.yaml")

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)
    _write_atomic(path, next_text)

    disabled_path = _disabled_models_path(path)
    disabled_backup = ""
    if disabled_entries:
        if disabled_path.exists():
            disabled_backup_path = disabled_path.with_name(f"{disabled_path.name}.bak-{stamp}")
            shutil.copy2(disabled_path, disabled_backup_path)
            disabled_backup = str(disabled_backup_path)
        _write_atomic(disabled_path, _dump_model_list_section(DISABLED_MODELS_KEY, disabled_entries))
    elif disabled_path.exists():
        disabled_backup_path = disabled_path.with_name(f"{disabled_path.name}.bak-{stamp}")
        shutil.copy2(disabled_path, disabled_backup_path)
        disabled_backup = str(disabled_backup_path)
        disabled_path.unlink()

    return {
        "providers": provider_count,
        "active": len(active_entries),
        "disabled": len(disabled_entries),
        "backup": str(backup),
        "disabled_path": str(disabled_path) if disabled_entries else "",
        "disabled_backup": disabled_backup,
        "revision": _config_revision(path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Load and save LiteLLM Menu provider/model config.")
    parser.add_argument("command", choices=["load", "save"])
    parser.add_argument("--config", default=str(CONFIG_YAML))
    args = parser.parse_args()
    path = pathlib.Path(args.config).expanduser()

    try:
        if args.command == "load":
            json.dump(load_config(path), sys.stdout, ensure_ascii=False, indent=2)
            print()
            return 0

        payload = json.load(sys.stdin)
        providers = payload.get("providers") if isinstance(payload, dict) else None
        if not isinstance(providers, list):
            raise ValueError("Save payload must contain a providers list")
        result = save_config(providers, path, payload.get("expected_revision"))
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

__all__ = [name for name in globals() if not name.startswith("__")]
