from __future__ import annotations

from .core import *
from .operations import *

def command_settings(args: argparse.Namespace) -> int:
    settings = load_settings(args.settings)
    print(json.dumps(settings.sanitized(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _settings_from_payload(payload: dict[str, Any], existing: Settings) -> Settings:
    raw = {
        "url": payload.get("url", existing.url),
        "username": payload.get("username", existing.username),
        "remote_name": payload.get("remote_name", existing.remote_name),
        "sync_interval_minutes": payload.get("sync_interval_minutes", existing.sync_interval_minutes),
        "timeout_seconds": payload.get("timeout_seconds", existing.timeout_seconds),
    }
    if "password" in payload:
        raw["password"] = payload.get("password", "")
    elif payload.get("keep_password"):
        raw["password"] = existing.password
    else:
        raw["password"] = existing.password
    return _settings_from_raw(raw)


def _read_payload_from_stdin() -> dict[str, Any]:
    try:
        text = sys.stdin.read()
    except OSError as exc:
        raise SyncError(f"Could not read WebDAV settings JSON from stdin: {exc}") from exc
    if not text.strip():
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SyncError(f"Expected WebDAV settings JSON on stdin: {exc}") from exc
    if not isinstance(payload, dict):
        raise SyncError("WebDAV settings payload must be a JSON object")
    return payload


def command_configure(args: argparse.Namespace) -> int:
    existing = load_settings(args.settings)
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise SyncError(f"Expected WebDAV settings JSON on stdin: {exc}") from exc
    if not isinstance(payload, dict):
        raise SyncError("WebDAV settings payload must be a JSON object")

    settings = _settings_from_payload(payload, existing)
    if not settings.url:
        raise SyncError("WebDAV URL is required")
    save_settings(args.settings, settings)
    print(f"WebDAV sync configured: {_redact_url(settings.url)}")
    print(f"Remote bundle: {_redact_url(bundle_url(settings))}")
    print(f"Settings file: {args.settings}")
    return 0


def command_push(args: argparse.Namespace) -> int:
    config_path = args.config.expanduser()
    settings = load_settings(args.settings)
    client = WebDAVClient(settings)
    remote_bundle_url = bundle_url(settings)
    bundle_size, manifest = push_bundle(client, settings, config_path, args.state, "push")

    print(f"Pushed LiteLLM Menu config to {_redact_url(remote_bundle_url)}")
    print(f"Bundle bytes: {bundle_size}")
    print_manifest_summary("Local snapshot", manifest)
    return 0


def command_pull(args: argparse.Namespace) -> int:
    config_path = args.config.expanduser()
    settings = load_settings(args.settings)
    client = WebDAVClient(settings)
    remote_bundle_url = bundle_url(settings)
    result = pull_bundle(client, settings, config_path, args.state, "pull")
    manifest = result["manifest"]

    print(f"Pulled LiteLLM Menu config from {_redact_url(remote_bundle_url)}")
    print_manifest_summary("Remote snapshot", manifest)
    for path in result["installed"]:
        print(f"Installed: {path}")
    for path in result["removed"]:
        print(f"Removed: {path}")
    for path in result["backups"]:
        print(f"Backup: {path}")
    return 0


def command_sync(args: argparse.Namespace) -> int:
    config_path = args.config.expanduser()
    settings = load_settings(args.settings)
    client = WebDAVClient(settings)
    local_manifest = build_manifest(config_path)
    remote_manifest = read_remote_manifest(client, settings)
    base_manifest = baseline_manifest(args.state)

    print_manifest_summary("Local snapshot", local_manifest)
    if remote_manifest is None:
        bundle_size, pushed_manifest = push_bundle(client, settings, config_path, args.state, "sync-push")
        print(f"Remote snapshot: missing manifest; pushed local config to {_redact_url(bundle_url(settings))}")
        print(f"Bundle bytes: {bundle_size}")
        print_manifest_summary("Synced snapshot", pushed_manifest)
        return 0

    print_manifest_summary("Remote snapshot", remote_manifest)
    if manifests_match(local_manifest, remote_manifest):
        save_sync_state(args.state, settings, local_manifest, "sync")
        print("WebDAV sync: already up to date")
        return 0

    if base_manifest is None:
        raise SyncError(
            "WebDAV sync conflict: remote config exists and no previous sync baseline is recorded. "
            "Use webdav-pull to accept remote, or webdav-push to overwrite remote."
        )

    local_changed = not manifests_match(local_manifest, base_manifest)
    remote_changed = not manifests_match(remote_manifest, base_manifest)

    if local_changed and not remote_changed:
        bundle_size, pushed_manifest = push_bundle(client, settings, config_path, args.state, "sync-push")
        print(f"WebDAV sync: local changed; pushed to {_redact_url(bundle_url(settings))}")
        print(f"Bundle bytes: {bundle_size}")
        print_manifest_summary("Synced snapshot", pushed_manifest)
        return 0

    if remote_changed and not local_changed:
        result = pull_bundle(client, settings, config_path, args.state, "sync-pull")
        print(f"WebDAV sync: remote changed; pulled from {_redact_url(bundle_url(settings))}")
        print_manifest_summary("Synced snapshot", result["manifest"])
        for path in result["installed"]:
            print(f"Installed: {path}")
        for path in result["removed"]:
            print(f"Removed: {path}")
        for path in result["backups"]:
            print(f"Backup: {path}")
        return 0

    if local_changed and remote_changed:
        raise SyncError(
            "WebDAV sync conflict: local and remote configs both changed since the last successful sync. "
            "Use webdav-status to inspect, then webdav-pull or webdav-push to choose a side."
        )

    save_sync_state(args.state, settings, local_manifest, "sync")
    print("WebDAV sync: refreshed baseline")
    return 0


def command_status(args: argparse.Namespace) -> int:
    config_path = args.config.expanduser()
    settings = load_settings(args.settings)
    if not settings.configured:
        raise SyncError("WebDAV sync is not configured")

    print(f"Settings file: {args.settings}")
    print(f"WebDAV URL: {_redact_url(settings.url)}")
    print(f"Remote bundle: {_redact_url(bundle_url(settings))}")
    print(f"Username: {settings.username or '(none)'}")
    print(f"Password: {'set' if settings.password else '(none)'}")
    print(f"Sync interval: {settings.sync_interval_minutes} minutes")
    print(f"HTTP timeout: {settings.timeout_seconds:g} seconds")
    print(f"Local config: {config_path}")
    local_manifest = None
    if config_path.exists():
        local_manifest = build_manifest(config_path)
        print_manifest_summary("Local snapshot", local_manifest)
    else:
        print("Local snapshot: missing config.yaml")

    client = WebDAVClient(settings)
    remote_manifest = None
    try:
        remote_manifest = read_remote_manifest(client, settings)
        if remote_manifest is None:
            print(f"Remote snapshot: missing manifest at {_redact_url(manifest_url(settings))}")
        else:
            print_manifest_summary("Remote snapshot", remote_manifest)
    except Exception as exc:
        print(f"Remote snapshot: could not read manifest: {exc}")

    try:
        base = baseline_manifest(args.state)
        if base is None:
            print("Sync baseline: missing")
        else:
            print_manifest_summary("Sync baseline", base)
            if local_manifest is not None and remote_manifest is not None:
                local_changed = not manifests_match(local_manifest, base)
                remote_changed = not manifests_match(remote_manifest, base)
                print(
                    "Sync state: "
                    f"local_changed={'yes' if local_changed else 'no'} "
                    f"remote_changed={'yes' if remote_changed else 'no'}"
                )
    except Exception as exc:
        print(f"Sync baseline: could not read state: {exc}")

    try:
        _status, headers = client.head(bundle_url(settings))
        size = headers.get("Content-Length") or headers.get("content-length") or "?"
        modified = headers.get("Last-Modified") or headers.get("last-modified") or "?"
        print(f"Remote bundle: present bytes={size} modified={modified}")
    except WebDAVHTTPError as exc:
        if exc.code == 404:
            print("Remote bundle: missing")
        else:
            raise
    return 0


def command_probe(args: argparse.Namespace) -> int:
    existing = load_settings(args.settings)
    payload = _read_payload_from_stdin() if args.stdin_settings else {}
    settings = _settings_from_payload(payload, existing) if payload else existing
    if not settings.url:
        raise SyncError("WebDAV URL is required")

    client = WebDAVClient(settings)
    client.try_mkcol(collection_url(settings))
    remote_bundle_url = bundle_url(settings)
    try:
        status, headers = client.head(remote_bundle_url)
    except WebDAVHTTPError as exc:
        if exc.code == 404:
            print(f"WebDAV probe OK: {_redact_url(collection_url(settings))}")
            print(f"Remote bundle: missing at {_redact_url(remote_bundle_url)}")
            print("Test did not upload a temporary file. Use Push or Sync to create the configured remote file.")
            return 0
        if exc.code in {403, 405, 501}:
            bundle_data = client.get(remote_bundle_url)
            print(f"WebDAV probe OK via configured remote file: {_redact_url(remote_bundle_url)}")
            print(f"Remote bundle HEAD unavailable: HTTP {exc.code}; verified with GET instead")
            print(f"Remote bundle GET: bytes={len(bundle_data)}")
            print("Test did not upload a temporary file.")
            return 0
        raise

    size = headers.get("Content-Length") or headers.get("content-length") or "?"
    print(f"WebDAV probe OK via configured remote file: {_redact_url(remote_bundle_url)}")
    print(f"Remote bundle HEAD: status={status} bytes={size}")
    print("Test did not upload a temporary file.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync LiteLLM Menu config.yaml over WebDAV.")
    parser.add_argument("command", choices=["settings", "configure", "probe", "push", "pull", "sync", "status"])
    parser.add_argument("--config", type=pathlib.Path, default=default_config_yaml())
    parser.add_argument("--settings", type=pathlib.Path, default=default_settings_file())
    parser.add_argument("--state", type=pathlib.Path, default=default_state_file())
    parser.add_argument("--stdin-settings", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.config = args.config.expanduser()
    args.settings = args.settings.expanduser()
    args.state = args.state.expanduser()

    try:
        if args.command == "settings":
            return command_settings(args)
        if args.command == "configure":
            return command_configure(args)
        if args.command == "probe":
            return command_probe(args)
        if args.command == "push":
            return command_push(args)
        if args.command == "pull":
            return command_pull(args)
        if args.command == "sync":
            return command_sync(args)
        if args.command == "status":
            return command_status(args)
    except SyncError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 64


if __name__ == "__main__":
    raise SystemExit(main())

__all__ = [name for name in globals() if not name.startswith("__")]
