from __future__ import annotations

import hashlib
import math
import os
import pathlib
import re
import secrets
import tempfile
from typing import Any
from urllib.parse import urlparse

import yaml

from young_router.api_base import normalize_configured_api_base

from .schema import (
    DEFAULT_API_KEY_NAME,
    MENU_API_KEY_NAME_KEY,
    MENU_KEY_BINDING_KEY,
    MENU_MANUAL_ORDER_KEY,
    MENU_MODEL_ENABLED_KEY,
    MENU_ORDER_MODE_KEY,
    MENU_PROVIDER_KEY_ID_KEY,
    MENU_PROVIDER_AUTH_KEY,
    MENU_PROVIDER_SOURCE_KEY,
    MENU_RELAY_CATALOG_MODE_KEY,
    MENU_RELAY_KEYS_KEY,
    MENU_RELAY_KEYS_VERSION,
    MENU_RELAY_SOURCE_MODEL_KEY,
    MENU_ROUTE_KEY,
    MODEL_ORDER_MODES,
    PUBLIC_MODEL_CONTEXT_KEY,
    RANDOM_DEPLOYMENT_ID_RE,
    UPSTREAM_PROTOCOL_MODE_KEY,
    UPSTREAM_URL_SURFACE_KEY,
    _as_dict,
    _as_list,
    _bool_value,
    _positive_int,
    _provider_key_id,
    _provider_auth,
    _provider_source,
    _relay_source,
    _stable_provider_key_id,
    _string_value,
    _upstream_protocol_mode,
    _upstream_url_surface,
    canonical_litellm_model,
    infer_upstream_fallback_surface,
)

def _parse_scalar(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return yaml.safe_load(stripped)
    except Exception:
        return stripped


def _numeric_order(value: Any) -> int | float:
    text = str(value).strip()
    if not text:
        # The app's own automatic order starts at 0: the first route of a group
        # is 0 everywhere else this app writes one (a new model, a renumbered
        # group), so a value the file states nowhere keeps that same first slot.
        return 0
    parsed = _parse_scalar(text)
    if isinstance(parsed, bool) or not isinstance(parsed, (int, float)):
        raise ValueError(f"Invalid route order: {text}")
    if isinstance(parsed, float) and not math.isfinite(parsed):
        raise ValueError(f"Invalid route order: {text}")
    return parsed


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
            "id": _provider_key_id(item_dict.get("id"))
            or _stable_provider_key_id(provider.get("name"), key_name),
            "name": key_name,
            "value": key_value,
            "source": _relay_source(item_dict.get("source")),
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
    """The one key this name identifies, or ``None`` when no key carries it.

    An empty name is not a lookup: a model that names no key follows the
    provider's default and the caller states that itself.  A name that matches
    nothing is a dangling reference, and resolving it to the provider's first
    key would silently move the route to another credential — the same key the
    next model switch would report as its own.
    """

    if not key_name:
        return None
    for item in _normalized_api_keys(provider):
        if item["name"] == key_name:
            return item
    return None


def _api_key_by_id(provider: dict[str, Any], provider_key_id: str) -> dict[str, Any] | None:
    if not provider_key_id:
        return None
    for item in _normalized_api_keys(provider):
        if item["id"] == provider_key_id:
            return item
    return None


def _provider_key_metadata(keys: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": MENU_RELAY_KEYS_VERSION,
        "slots": [
            {
                "id": item["id"],
                "api_key_name": item["name"],
                "source": item["source"],
            }
            for item in keys
        ],
    }


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
        api_base = normalize_configured_api_base(provider.get("api_base", ""))
        if api_base:
            lines.append(f"    api_base: {_anchor_scalar(api_base, base_anchor)}")
        if keys:
            lines.append("    api_keys:")
            for item in keys:
                lines.append(f"      - name: {_plain_scalar(item['name'])}")
                lines.append(f"        value: {_anchor_scalar(item['value'], _provider_key_anchor(name, item['name']))}")
        extra = dict(_as_dict(provider.get("extra")))
        extra[MENU_RELAY_KEYS_KEY] = _provider_key_metadata(keys)
        raw_source = extra.get(MENU_PROVIDER_SOURCE_KEY)
        provider_type = _string_value(provider.get("provider_type")).strip()
        station_id = _string_value(provider.get("relay_station_id")).strip()
        if provider_type or station_id:
            raw_source = {
                "kind": provider_type or "custom",
                **({"station_id": station_id} if station_id else {}),
            }
        source = _provider_source(raw_source)
        extra[MENU_PROVIDER_SOURCE_KEY] = source
        raw_auth = extra.get(MENU_PROVIDER_AUTH_KEY)
        auth_kind = _string_value(provider.get("auth_kind")).strip()
        credential_ref = _string_value(provider.get("auth_credential_ref")).strip()
        if auth_kind or credential_ref:
            raw_auth = {
                "kind": auth_kind or "api_key",
                **({"credential_ref": credential_ref} if credential_ref else {}),
            }
        auth = _provider_auth(raw_auth)
        if auth["kind"] == "api_key":
            extra.pop(MENU_PROVIDER_AUTH_KEY, None)
        else:
            extra[MENU_PROVIDER_AUTH_KEY] = auth
        for key, value in extra.items():
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
    raw_upstream_url_surface = model.get("upstream_url_surface")
    upstream_url_surface = (
        _upstream_url_surface(raw_upstream_url_surface)
        if raw_upstream_url_surface is not None
        else infer_upstream_fallback_surface(model.get("litellm_model"))
    )
    upstream_protocol_mode = _upstream_protocol_mode(
        model.get("upstream_protocol_mode")
    )
    auth = _provider_auth(
        {
            "kind": provider.get("auth_kind", "api_key"),
            **(
                {"credential_ref": provider.get("auth_credential_ref")}
                if provider.get("auth_credential_ref")
                else {}
            ),
        }
    )
    adapter = (
        "chatgpt"
        if auth["kind"] == "openai_login"
        else "anthropic"
        if auth["kind"] == "claude_login"
        else None
    )
    litellm_model = canonical_litellm_model(
        model.get("litellm_model", ""),
        upstream_url_surface,
        adapter,
    )

    if not provider_name:
        raise ValueError(f"Provider for model #{index + 1} has no name")
    if enabled and not model_name:
        raise ValueError(f"Model #{index + 1} is enabled but has no model_name")
    if enabled and not litellm_model:
        raise ValueError(f"Model #{index + 1} is enabled but has no provider model")

    entry = dict(_as_dict(model.get("entry_extra")))
    params = dict(_as_dict(model.get("litellm_extra")))
    model_info = dict(_as_dict(model.get("model_info_extra")))
    # The route's key binding is decided below from what the route itself says;
    # a marker read back from an earlier write must not survive as stale text.
    model_info.pop(MENU_KEY_BINDING_KEY, None)
    _set_if_text(entry, "model_name", model_name)
    _set_if_text(params, "model", litellm_model)
    api_base = normalize_configured_api_base(provider.get("api_base", ""))
    provider_key_id = _provider_key_id(model.get("provider_key_id"))
    key_name = str(model.get("api_key_name", "")).strip()
    api_key_item = (
        _api_key_by_id(provider, provider_key_id)
        if provider_key_id
        else _api_key_by_name(provider, key_name)
    )
    api_key = ""
    resolved_key_name = api_key_item["name"] if api_key_item else ""
    if api_key_item is None and (key_name or provider_key_id):
        # The model names a key — or a slot — this provider does not offer.  An
        # enabled route is refused by name instead of falling back to another
        # slot: a route that answered with the wrong credential is worse than a
        # route the pane reports as unfinished.  A disabled route is not served
        # at all, so its companion entry keeps the reference it had — re-enabling
        # it asks for that key again rather than silently borrowing another
        # slot's credential.
        if enabled:
            raise ValueError(
                f"Provider key {key_name or provider_key_id} for model {model_name or f'#{index + 1}'} is unavailable"
            )
        api_key_name = key_name
    elif not key_name and not provider_key_id:
        # A route that names no key is the pane's own “no key” choice: it answers
        # with the provider's default credential (the credential the entry
        # already carries wins, so a hand-written route keeps its own), and it
        # claims no key — neither a name nor a slot id is written.  What the pane
        # shows before Apply is then what the next read still shows: the row
        # stays where the user left it, no slot the user did not pick owns it,
        # a relay slot never adopts it, and deleting a key never deletes it.
        keys = _normalized_api_keys(provider)
        api_key = str(model.get("api_key", "")).strip() or (keys[0]["value"] if keys else "")
        api_key_name = ""
        provider_key_id = ""
        model_info[MENU_KEY_BINDING_KEY] = "unbound"
        # Reference the key that carries this credential when one does, so a
        # rotated credential keeps flowing into an unbound route too; a
        # credential no key carries stays literal.
        resolved_key_name = next(
            (item["name"] for item in keys if api_key and item["value"] == api_key),
            "",
        )
    else:
        api_key = api_key_item["value"] if api_key_item else ""
        api_key_name = api_key_item["name"] if api_key_item else ""
        provider_key_id = api_key_item["id"] if api_key_item else provider_key_id
    # A route that states its own base keeps it: the provider's base is what a
    # route without one inherits, and flattening an override onto it pointed the
    # route at a different host than the file named.
    model_api_base = normalize_configured_api_base(model.get("api_base", ""))
    if model_api_base and model_api_base != api_base:
        params["api_base"] = model_api_base
    elif api_base:
        params["api_base"] = {"__alias__": _make_anchor_name(provider_name, "api_base")} if use_provider_aliases else api_base
    if api_key:
        params["api_key"] = (
            {"__alias__": _provider_key_anchor(provider_name, resolved_key_name)}
            if use_provider_aliases and resolved_key_name
            else api_key
        )

    order_mode = str(model.get("order_mode", "manual")).strip() or "manual"
    if order_mode not in MODEL_ORDER_MODES:
        raise ValueError(f"Invalid route order mode: {order_mode}")
    manual_order = _numeric_order(model.get("manual_order", model.get("order", "")))
    if order_mode == "relay_multiplier":
        effective_order_value = model.get("effective_order")
        if effective_order_value is None or str(effective_order_value).strip() == "":
            raise ValueError(
                f"Relay multiplier is unavailable for model {model_name or f'#{index + 1}'}"
            )
        order = _numeric_order(effective_order_value)
    else:
        order = manual_order
    params["order"] = order
    # The route's transport trust is part of the entry, not of the editor's own
    # fields: the loader lifts `ssl_verify` out of the extras bucket and the
    # pane reads it, so a save has to put it back or an Apply silently
    # re-enables TLS verification for a route configured against a self-signed
    # or local upstream.  A boolean stays a boolean and any other value is the
    # CA bundle path the proxy reads.
    if model.get("ssl_verify_present") is True:
        ssl_verify = str(model.get("ssl_verify", "")).strip()
        if ssl_verify.lower() in {"true", "false"}:
            params["ssl_verify"] = ssl_verify.lower() == "true"
        elif ssl_verify:
            params["ssl_verify"] = ssl_verify
        else:
            params.pop("ssl_verify", None)

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
    if provider_key_id:
        model_info[MENU_PROVIDER_KEY_ID_KEY] = provider_key_id
    # Relay ownership is persisted once on the ProviderKey slot.  Older
    # model-level relay fields are accepted by the loader but deliberately
    # removed on the next save so they cannot drift from the selected key.
    model_info.pop(MENU_RELAY_CATALOG_MODE_KEY, None)
    model_info.pop(MENU_RELAY_SOURCE_MODEL_KEY, None)
    model_info[MENU_ORDER_MODE_KEY] = order_mode
    model_info[MENU_MANUAL_ORDER_KEY] = manual_order
    # Keep the model's own switch explicit even when the provider disables the
    # effective route. Re-enabling a provider must restore each prior model
    # state instead of treating every companion-file entry as model-disabled.
    model_info[MENU_MODEL_ENABLED_KEY] = model_enabled
    supports_responses_image_tool = bool(
        model.get("supports_responses_image_generation_tool", False)
    )
    supports_responses_image_tool_present = bool(
        model.get("supports_responses_image_generation_tool_present", False)
    )
    if supports_responses_image_tool_present or supports_responses_image_tool:
        model_info["supports_responses_image_generation_tool"] = supports_responses_image_tool
    model_info[UPSTREAM_URL_SURFACE_KEY] = upstream_url_surface
    model_info[UPSTREAM_PROTOCOL_MODE_KEY] = upstream_protocol_mode
    # The public model's custom context window is written only when the user
    # set it: an absent key keeps the metadata its client resolves, and the
    # Codex catalog falls back to the resolved default.
    limit = _positive_int(model.get("max_input_tokens"))
    if limit is None:
        model_info.pop(PUBLIC_MODEL_CONTEXT_KEY, None)
    else:
        model_info[PUBLIC_MODEL_CONTEXT_KEY] = limit

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
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(text.rstrip() + "\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
