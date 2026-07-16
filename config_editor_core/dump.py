from __future__ import annotations

from .schema import *
from .load import *
from urllib.parse import urlparse


_KNOWN_API_ENDPOINT_SUFFIXES = (
    ("v1", "chat", "completions"),
    ("v1", "chat", "completion"),
    ("v1", "images", "generations"),
    ("v1", "images", "generation"),
    ("v1", "completions"),
    ("v1", "completion"),
    ("v1", "complete"),
    ("v1", "responses"),
    ("v1", "response"),
    ("v1", "messages"),
    ("v1", "message"),
    ("v1", "models"),
    ("v1", "model"),
    ("v1", "chat"),
    ("v1", "images"),
    ("chat", "completions"),
    ("chat", "completion"),
    ("images", "generations"),
    ("images", "generation"),
    ("completions",),
    ("completion",),
    ("complete",),
    ("responses",),
    ("response",),
    ("messages",),
    ("message",),
    ("models",),
    ("model",),
    ("chat",),
    ("images",),
)


def _normalize_configured_api_base(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        if "://" in text:
            return text
        parsed = urlparse(f"https://{text}")
    if not parsed.scheme or not parsed.netloc:
        return text
    parts = [part for part in parsed.path.split("/") if part]
    lowered = [part.lower() for part in parts]
    for suffix in _KNOWN_API_ENDPOINT_SUFFIXES:
        if len(lowered) >= len(suffix) and tuple(lowered[-len(suffix) :]) == suffix:
            break
    else:
        if not parts or parts[-1].lower() != "v1":
            parts.append("v1")
    path = f"/{'/'.join(parts)}" if parts else ""
    return parsed._replace(path=path, params="", query="", fragment="").geturl().rstrip("/")

def _parse_scalar(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return yaml.safe_load(stripped)
    except Exception:
        return stripped


def _set_if_text(target: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    text = str(value)
    if text != "":
        target[key] = text


def _anchor_part(value: str, fallback: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip())
    base = re.sub(r"_+", "_", base).strip("_")
    if not base:
        base = fallback
    if not re.match(r"^[A-Za-z_]", base):
        base = f"p_{base}"
    return base


def _make_anchor_name(provider_name: str, suffix: str) -> str:
    base = _anchor_part(provider_name, "provider")
    suffix_part = _anchor_part(suffix, "value")
    return f"{base}_{suffix_part}"


def _provider_key_anchor(provider_name: str, key_name: str) -> str:
    if key_name.strip() == DEFAULT_API_KEY_NAME:
        return _make_anchor_name(provider_name, "api_key")
    return _make_anchor_name(provider_name, f"api_key_{key_name}")


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


def _deployment_route_key(
    *,
    model_name: str = "",
    litellm_model: str,
    provider_name: str,
    api_base: str = "",
    api_key_name: str = "",
    order: Any = None,
) -> str:
    parts = []
    public_model = str(model_name).strip()
    if public_model:
        parts.append(f"model={public_model}")
    parts.extend([
        f"provider={str(provider_name).strip() or 'unknown-provider'}",
        f"upstream={str(litellm_model).strip() or 'unknown-model'}",
    ])
    host = _api_base_host(api_base)
    if host:
        parts.append(f"host={host}")
    key_part = str(api_key_name).strip()
    if key_part:
        parts.append(f"key={key_part}")
    if order is not None and str(order).strip():
        parts.append(f"order={str(order).strip()}")
    return " / ".join(parts)


def _api_base_host(api_base: str) -> str:
    if not api_base:
        return ""
    parsed = urlparse(api_base if "://" in api_base else f"https://{api_base}")
    return (parsed.hostname or "").lower()


def _random_deployment_id(seen: set[str] | None = None) -> str:
    if seen is None:
        seen = set()
    for _ in range(128):
        deployment_id = hashlib.md5(secrets.token_bytes(32), usedforsecurity=False).hexdigest()[:8]
        if deployment_id not in seen:
            seen.add(deployment_id)
            return deployment_id
    raise RuntimeError("Could not generate a unique deployment token")


def _plain_scalar(value: Any) -> str:
    text = yaml.safe_dump(
        value,
        allow_unicode=True,
        default_flow_style=True,
        sort_keys=False,
        width=1000,
    ).strip()
    if text.endswith("\n..."):
        text = text[:-4].strip()
    elif text == "...":
        text = ""
    return text


def _anchor_scalar(value: Any, anchor: str) -> str:
    if isinstance(value, str) and value:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'&{anchor} "{escaped}"'
    return f"&{anchor} {_plain_scalar(value)}"


def _alias_scalar(anchor: str) -> str:
    return f"*{anchor}"


def _normalized_api_keys(provider: dict[str, Any]) -> list[dict[str, Any]]:
    raw_keys = _as_list(provider.get("api_keys"))
    keys: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    for index, item in enumerate(raw_keys, start=1):
        item_dict = _as_dict(item)
        key_name = _string_value(item_dict.get("name")).strip() or f"key-{index}"
        key_value = _string_value(item_dict.get("value"))
        if not key_value:
            continue
        if key_name in seen_names:
            raise ValueError(f"Duplicate API key label in provider {provider.get('name', '')}: {key_name}")
        keys.append({
            "name": key_name,
            "value": key_value,
            "enabled": True,
        })
        seen_names.add(key_name)

    seen_anchors: set[str] = set()
    provider_name = str(provider.get("name", "")).strip()
    for item in keys:
        anchor = _provider_key_anchor(provider_name, item["name"])
        if anchor in seen_anchors:
            raise ValueError(f"API key labels in provider {provider_name} produce duplicate YAML anchors")
        seen_anchors.add(anchor)

    return keys


def _primary_api_key(keys: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in keys:
        if item["name"] == DEFAULT_API_KEY_NAME:
            return item
    return keys[0] if keys else None


def _api_key_by_name(provider: dict[str, Any], key_name: str) -> dict[str, Any] | None:
    keys = _normalized_api_keys(provider)
    if key_name:
        for item in keys:
            if item["name"] == key_name:
                return item
    return _primary_api_key(keys)


def _dump_providers_section(providers: list[dict[str, Any]]) -> str:
    lines = ["providers:"]
    seen: set[str] = set()
    for provider in providers:
        name = str(provider.get("name", "")).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        provider_anchor = _make_anchor_name(name, "provider")
        base_anchor = _make_anchor_name(name, "api_base")
        keys = _normalized_api_keys(provider)
        lines.append(f"  {name}: &{provider_anchor}")
        if not _bool_value(provider.get("enabled"), True):
            lines.append("    enabled: false")
        api_base = _normalize_configured_api_base(provider.get("api_base", ""))
        if api_base:
            lines.append(f"    api_base: {_anchor_scalar(api_base, base_anchor)}")
        if keys:
            lines.append("    api_keys:")
            for item in keys:
                lines.append(f"      - name: {_plain_scalar(item['name'])}")
                lines.append(f"        value: {_anchor_scalar(item['value'], _provider_key_anchor(name, item['name']))}")
        for key, value in _as_dict(provider.get("extra")).items():
            lines.append(f"    {key}: {_plain_scalar(value)}")
    if len(lines) == 1:
        return "providers: {}\n"
    return "\n".join(lines).rstrip() + "\n"


def _entry_from_editor(
    model: dict[str, Any],
    provider: dict[str, Any],
    index: int,
    use_provider_aliases: bool,
    effective_enabled: bool,
    seen_deployment_ids: set[str] | None = None,
) -> tuple[bool, dict[str, Any]]:
    model_enabled = _bool_value(model.get("model_enabled"), _bool_value(model.get("enabled"), True))
    enabled = effective_enabled
    provider_name = str(provider.get("name", "")).strip()
    model_name = str(model.get("model_name", "")).strip()
    litellm_model = str(model.get("litellm_model", "")).strip()

    if not provider_name:
        raise ValueError(f"Provider for model #{index + 1} has no name")
    if enabled and not model_name:
        raise ValueError(f"Model #{index + 1} is enabled but has no model_name")
    if enabled and not litellm_model:
        raise ValueError(f"Model #{index + 1} is enabled but has no provider model")

    entry = dict(_as_dict(model.get("entry_extra")))
    params = dict(_as_dict(model.get("litellm_extra")))
    model_info = dict(_as_dict(model.get("model_info_extra")))
    model_info.pop("supports_vision", None)
    _set_if_text(entry, "model_name", model_name)
    _set_if_text(params, "model", litellm_model)
    api_base = _normalize_configured_api_base(provider.get("api_base", ""))
    key_name = str(model.get("api_key_name", "")).strip()
    if not key_name:
        model_api_key = str(model.get("api_key", "")).strip()
        for item in _normalized_api_keys(provider):
            if item["value"] == model_api_key:
                key_name = item["name"]
                break
    api_key_item = _api_key_by_name(provider, key_name)
    api_key = api_key_item["value"] if api_key_item else ""
    api_key_name = api_key_item["name"] if api_key_item else ""
    if api_base:
        params["api_base"] = {"__alias__": _make_anchor_name(provider_name, "api_base")} if use_provider_aliases else api_base
    if api_key:
        params["api_key"] = {"__alias__": _provider_key_anchor(provider_name, api_key_name)} if use_provider_aliases else api_key

    order = _parse_scalar(str(model.get("order", "")).strip())
    if order is None:
        order = 1
    params["order"] = order

    deployment_id = str(model.get("deployment_id", "")).strip().lower()
    if not RANDOM_DEPLOYMENT_ID_RE.fullmatch(deployment_id):
        deployment_id = ""
    if deployment_id and seen_deployment_ids is not None:
        if deployment_id in seen_deployment_ids:
            deployment_id = ""
        else:
            seen_deployment_ids.add(deployment_id)
    if not deployment_id:
        deployment_id = _random_deployment_id(seen_deployment_ids)
    _set_if_text(model_info, "id", deployment_id)
    model_info["provider"] = provider_name
    model_info[MENU_ROUTE_KEY] = _deployment_route_key(
        model_name=model_name,
        litellm_model=litellm_model,
        provider_name=provider_name,
        api_base=api_base,
        api_key_name=api_key_name,
        order=order,
    )
    if api_key_name:
        model_info[MENU_API_KEY_NAME_KEY] = api_key_name
    if not model_enabled:
        model_info[MENU_MODEL_ENABLED_KEY] = False
    supports_responses_image_tool = bool(
        model.get("supports_responses_image_generation_tool", False)
    )
    supports_responses_image_tool_present = bool(
        model.get("supports_responses_image_generation_tool_present", False)
    )
    if supports_responses_image_tool_present or supports_responses_image_tool:
        model_info["supports_responses_image_generation_tool"] = supports_responses_image_tool
    supported_upstream_url_surfaces = _upstream_url_surfaces(
        model.get("supported_upstream_url_surfaces")
    )
    upstream_url_surface = supported_upstream_url_surfaces[0]
    model_info[UPSTREAM_URL_SURFACE_KEY] = upstream_url_surface
    model_info[SUPPORTED_UPSTREAM_URL_SURFACES_KEY] = supported_upstream_url_surfaces

    if params:
        entry["litellm_params"] = params
    if model_info:
        entry["model_info"] = model_info

    return enabled, entry


def _dump_yaml_value(value: Any, indent: int) -> list[str]:
    if isinstance(value, dict) and set(value.keys()) == {"__alias__"}:
        return [_alias_scalar(str(value["__alias__"]))]
    if not isinstance(value, (dict, list)):
        return [_plain_scalar(value)]
    dumped = yaml.safe_dump(
        value,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=1000,
    ).rstrip().splitlines()
    if isinstance(value, list) and value:
        return [""] + [(" " * indent) + line for line in dumped]
    if len(dumped) == 1:
        return [dumped[0]]
    return [""] + [(" " * indent) + line for line in dumped]


def _dump_mapping(lines: list[str], mapping: dict[str, Any], indent: int) -> None:
    prefix = " " * indent
    for key, value in mapping.items():
        dumped = _dump_yaml_value(value, indent + 2)
        if len(dumped) == 1 and dumped[0] != "":
            lines.append(f"{prefix}{key}: {dumped[0]}")
        else:
            lines.append(f"{prefix}{key}:")
            lines.extend(dumped[1:])


def _dump_model_list_section(key: str, entries: list[dict[str, Any]]) -> str:
    lines = [f"{key}:"]
    if not entries:
        return f"{key}: []\n"

    for entry in entries:
        items = list(entry.items())
        first_key, first_value = items[0]
        first_dump = _dump_yaml_value(first_value, 4)
        if len(first_dump) == 1 and first_dump[0] != "":
            lines.append(f"  - {first_key}: {first_dump[0]}")
        else:
            lines.append(f"  - {first_key}:")
            lines.extend(first_dump[1:])

        for key_name, value in items[1:]:
            if isinstance(value, dict):
                lines.append(f"    {key_name}:")
                _dump_mapping(lines, value, 6)
            else:
                dumped = _dump_yaml_value(value, 6)
                if len(dumped) == 1 and dumped[0] != "":
                    lines.append(f"    {key_name}: {dumped[0]}")
                else:
                    lines.append(f"    {key_name}:")
                    lines.extend(dumped[1:])
    return "\n".join(lines).rstrip() + "\n"


def _dump_section(key: str, value: Any) -> str:
    return yaml.safe_dump(
        {key: value},
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=1000,
    ).rstrip() + "\n"


def _find_top_level_section(text: str, key: str) -> tuple[int, int] | None:
    document = yaml.compose(text)
    if not isinstance(document, yaml.nodes.MappingNode):
        return None

    entries = document.value
    for index, (key_node, _value_node) in enumerate(entries):
        if not isinstance(key_node, yaml.nodes.ScalarNode) or key_node.value != key:
            continue
        end = (
            entries[index + 1][0].start_mark.index
            if index + 1 < len(entries)
            else len(text)
        )
        return key_node.start_mark.index, end
    return None


def _replace_top_level_section(text: str, key: str, block: str) -> str:
    section = _find_top_level_section(text, key)
    if section is None:
        suffix = "" if text.endswith("\n") else "\n"
        return f"{text}{suffix}\n{block}\n"
    start, end = section
    before = text[:start].rstrip()
    after = text[end:].lstrip("\n")
    prefix = f"{before}\n\n" if before else ""
    suffix = f"\n{after}" if after else ""
    return f"{prefix}{block.rstrip()}\n{suffix}"


def _replace_top_level_sections(text: str, blocks: dict[str, str]) -> str:
    document = yaml.compose(text)
    if not isinstance(document, yaml.nodes.MappingNode):
        raise ValueError("config.yaml must be a YAML mapping")

    entries = document.value
    sections: dict[str, tuple[int, int]] = {}
    for index, (key_node, _value_node) in enumerate(entries):
        if not isinstance(key_node, yaml.nodes.ScalarNode):
            continue
        end = (
            entries[index + 1][0].start_mark.index
            if index + 1 < len(entries)
            else len(text)
        )
        sections[key_node.value] = (key_node.start_mark.index, end)

    updated = text
    replacements = [
        (sections[key][0], sections[key][1], block.rstrip() + "\n")
        for key, block in blocks.items()
        if key in sections
    ]
    for start, end, block in sorted(replacements, reverse=True):
        updated = updated[:start] + block + updated[end:]

    missing = [block.rstrip() for key, block in blocks.items() if key not in sections]
    if missing:
        updated = updated.rstrip() + "\n\n" + "\n\n".join(missing) + "\n"
    return updated


def _unique_model_groups(active_entries: list[dict[str, Any]], existing_groups: list[Any] | None = None) -> list[str]:
    active_names: list[str] = []
    active_seen: set[str] = set()
    for entry in active_entries:
        name = str(entry.get("model_name", "")).strip()
        if name and name not in active_seen:
            active_names.append(name)
            active_seen.add(name)

    groups: list[str] = []
    seen: set[str] = set()
    for group in existing_groups or []:
        name = str(group).strip()
        if name and name in active_seen and name not in seen:
            groups.append(name)
            seen.add(name)
    for name in active_names:
        if name not in seen:
            groups.append(name)
            seen.add(name)
    return groups


def _write_atomic(path: pathlib.Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(text.rstrip() + "\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)

__all__ = [name for name in globals() if not name.startswith("__")]
