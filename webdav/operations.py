from __future__ import annotations

from .core import *

def print_manifest_summary(prefix: str, manifest: dict[str, Any]) -> None:
    summary = manifest.get("summary") if isinstance(manifest.get("summary"), dict) else {}
    print(
        f"{prefix}: providers={summary.get('providers', '?')} "
        f"active_models={summary.get('active_models', '?')} "
        f"disabled_models={summary.get('disabled_models', '?')} "
        f"created_at={manifest.get('created_at', '?')}"
    )


def create_outbound_bundle(config_path: pathlib.Path, remote_bundle_url: str) -> tuple[bytes, dict[str, Any], str]:
    bundle_data, manifest = create_bundle(config_path)
    bundle_content_type = "application/json; charset=utf-8"
    return bundle_data, manifest, bundle_content_type


def read_remote_manifest(client: WebDAVClient, settings: Settings) -> dict[str, Any] | None:
    remote_manifest_url = manifest_url(settings)
    try:
        remote_manifest = json.loads(client.get(remote_manifest_url).decode("utf-8"))
    except WebDAVHTTPError as exc:
        if exc.code in {403, 404, 405, 500, 502, 503, 504}:
            try:
                return read_manifest_from_remote_bundle(client, settings)
            except WebDAVHTTPError as bundle_exc:
                if bundle_exc.code == 404:
                    return None
                raise
        raise
    except json.JSONDecodeError as exc:
        raise SyncError(f"Remote manifest is not valid JSON: {exc}") from exc
    if not isinstance(remote_manifest, dict):
        raise SyncError("Remote manifest must be a JSON object")
    _validate_bundle_header(remote_manifest)
    return remote_manifest


def read_manifest_from_remote_bundle(client: WebDAVClient, settings: Settings) -> dict[str, Any]:
    remote_bundle_url = bundle_url(settings)
    try:
        manifest, _files = _read_bundle(client.get(remote_bundle_url))
    except WebDAVHTTPError:
        raise
    except SyncError:
        raise
    except Exception as exc:
        raise SyncError(f"Remote bundle manifest could not be read: {exc}") from exc
    _validate_bundle_header(manifest)
    return manifest


def push_bundle(
    client: WebDAVClient,
    settings: Settings,
    config_path: pathlib.Path,
    state_path: pathlib.Path | None = None,
    action: str = "push",
) -> tuple[int, dict[str, Any]]:
    remote_bundle_url = bundle_url(settings)
    bundle_data, manifest, bundle_content_type = create_outbound_bundle(config_path, remote_bundle_url)
    manifest_data = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")

    client.try_mkcol(collection_url(settings))
    client.put(remote_bundle_url, bundle_data, bundle_content_type)
    try:
        client.put(manifest_url(settings), manifest_data, "application/json; charset=utf-8")
    except WebDAVHTTPError as exc:
        if exc.code not in {403, 404, 405, 409, 500, 502, 503, 504}:
            raise
    if state_path is not None:
        save_sync_state(state_path, settings, manifest, action)
    return len(bundle_data), manifest


def pull_bundle(
    client: WebDAVClient,
    settings: Settings,
    config_path: pathlib.Path,
    state_path: pathlib.Path | None = None,
    action: str = "pull",
) -> dict[str, Any]:
    remote_bundle_url = bundle_url(settings)
    bundle_data = client.get(remote_bundle_url)
    result = install_bundle(bundle_data, config_path)
    if state_path is not None:
        save_sync_state(state_path, settings, result["manifest"], action)
    return result

__all__ = [name for name in globals() if not name.startswith("__")]
