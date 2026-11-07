from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

from .core import (
    BUNDLE_MISSING_STATUS,
    CONFIG_BUNDLE_MAX_BYTES,
    Settings,
    SyncError,
    WebDAVClient,
    WebDAVHTTPError,
    _redact_url,
    _settings_from_raw,
    baseline_manifest,
    build_manifest,
    bundle_url,
    clear_conflict_record,
    collection_url,
    default_config_yaml,
    default_conflict_file,
    default_settings_file,
    default_state_file,
    default_status_file,
    load_settings,
    manifest_url,
    save_settings,
    save_sync_status,
    save_sync_state,
    smart_sync_plan,
)
from .operations import (
    print_manifest_summary,
    pull_bundle,
    push_bundle,
    read_remote_manifest,
    remember_remote_bundle,
)

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
    # An absent password keeps the stored one.  A payload that names it owns the
    # value, empty string included -- that is how a form clears a password it no
    # longer wants, and a later branch that also kept the stored one made the
    # two cases indistinguishable.
    raw["password"] = payload["password"] if "password" in payload else existing.password
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

    print(f"Pushed Young Router config to {_redact_url(remote_bundle_url)}")
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

    print(f"Pulled Young Router config from {_redact_url(remote_bundle_url)}")
    print_manifest_summary("Remote snapshot", manifest)
    if not (result["installed"] or result["removed"] or result["backups"]):
        # The remote snapshot is exactly what this machine already holds.  Say
        # that instead of reporting a pull with no files under it, so an
        # interval pull reads the same whether it transferred or stopped at the
        # manifest.
        print("WebDAV pull: already up to date")
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
    plan = smart_sync_plan(local_manifest, remote_manifest, base_manifest)
    if plan == "unchanged":
        save_sync_state(args.state, settings, local_manifest, "sync")
        clear_conflict_record(default_conflict_file())
        print("WebDAV sync: already up to date")
        return 0

    if plan == "push":
        bundle_size, pushed_manifest = push_bundle(client, settings, config_path, args.state, "sync-push")
        print(f"WebDAV sync: took this machine's newer snapshot; pushed to {_redact_url(bundle_url(settings))}")
        print(f"Bundle bytes: {bundle_size}")
        print_manifest_summary("Synced snapshot", pushed_manifest)
        clear_conflict_record(default_conflict_file())
        return 0

    if plan == "conflict":
        # Two sides that both moved cannot be settled here: the CLI has no reader
        # to compare them.  Keep the remote side for the pane that does, and name
        # the choice the reader has to make.
        conflict_path = default_conflict_file()
        try:
            remember_remote_bundle(client, settings, conflict_path)
        except Exception:
            clear_conflict_record(conflict_path)
        raise SyncError(
            "WebDAV sync stopped because both ends changed; compare them in the app "
            "and keep one side",
            "webdav_sync_conflict",
        )

    result = pull_bundle(client, settings, config_path, args.state, "sync-pull", skip_when_unchanged=False)
    print(f"WebDAV sync: took the remote's newer snapshot; pulled from {_redact_url(bundle_url(settings))}")
    print_manifest_summary("Synced snapshot", result["manifest"])
    for path in result["installed"]:
        print(f"Installed: {path}")
    for path in result["removed"]:
        print(f"Removed: {path}")
    for path in result["backups"]:
        print(f"Backup: {path}")
    clear_conflict_record(default_conflict_file())
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
                # What a sync would do with these three snapshots: the same one
                # comparison the unattended loop asks.
                print(f"Sync plan: {smart_sync_plan(local_manifest, remote_manifest, base)}")
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
    try:
        access = client.collection_access()
    except WebDAVHTTPError as exc:
        # A server that does not answer PROPFIND still answers the HEAD below;
        # what it will not do is tell us whether the collection is writable.
        if exc.code not in {403, 405, 501}:
            raise
        access = "unknown"
    collection = _redact_url(collection_url(settings))
    if access == "read_only":
        print(f"Collection is read-only: {collection}")
        print("Push and Sync cannot write here; point the URL at a writable folder.")
    elif access == "missing":
        print(f"Collection is not there yet: {collection} (the first push creates it)")
    else:
        print(f"Collection: {access} {collection}")
    remote_bundle_url = bundle_url(settings)
    try:
        status, headers = client.head(remote_bundle_url)
    except WebDAVHTTPError as exc:
        # Some servers refuse HEAD and only answer GET; every other missing
        # answer (409 included: the collection is not there yet) is simply
        # "no bundle yet", which is not a failed connection.
        if exc.code in {403, 405, 501}:
            try:
                bundle_data = client.get(remote_bundle_url, max_bytes=CONFIG_BUNDLE_MAX_BYTES)
            except WebDAVHTTPError as inner:
                if inner.code not in BUNDLE_MISSING_STATUS:
                    raise
            else:
                print(f"WebDAV probe OK via configured remote file: {_redact_url(remote_bundle_url)}")
                print(f"Remote bundle HEAD unavailable: HTTP {exc.code}; verified with GET instead")
                print(f"Remote bundle GET: bytes={len(bundle_data)}")
                print("Test did not create, upload, or delete anything.")
                return 0
        if exc.code not in BUNDLE_MISSING_STATUS:
            raise
        print(f"WebDAV probe OK: {_redact_url(collection_url(settings))}")
        print(f"Remote bundle: missing at {_redact_url(remote_bundle_url)}")
        print("Test did not create, upload, or delete anything. Use Push or Sync to create the configured remote file.")
        return 0

    size = headers.get("Content-Length") or headers.get("content-length") or "?"
    print(f"WebDAV probe OK via configured remote file: {_redact_url(remote_bundle_url)}")
    print(f"Remote bundle HEAD: status={status} bytes={size}")
    print("Test did not create a collection, upload, or delete anything.")
    return 0


def _run_recorded_command(args: argparse.Namespace) -> int:
    command = str(args.command)
    handlers = {
        "probe": command_probe,
        "push": command_push,
        "pull": command_pull,
        "sync": command_sync,
    }
    handler = handlers[command]
    try:
        result = handler(args)
    except Exception:
        save_sync_status(args.status, command, False)
        raise
    save_sync_status(args.status, command, result == 0)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync Young Router config.yaml over WebDAV.")
    parser.add_argument("command", choices=["settings", "configure", "probe", "push", "pull", "sync", "status"])
    parser.add_argument("--config", type=pathlib.Path, default=default_config_yaml())
    parser.add_argument("--settings", type=pathlib.Path, default=default_settings_file())
    parser.add_argument("--state", type=pathlib.Path, default=default_state_file())
    parser.add_argument("--status", type=pathlib.Path, default=default_status_file())
    parser.add_argument("--stdin-settings", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.config = args.config.expanduser()
    args.settings = args.settings.expanduser()
    args.state = args.state.expanduser()
    args.status = args.status.expanduser()

    try:
        if args.command == "settings":
            return command_settings(args)
        if args.command == "configure":
            return command_configure(args)
        if args.command == "probe":
            return _run_recorded_command(args)
        if args.command == "push":
            return _run_recorded_command(args)
        if args.command == "pull":
            return _run_recorded_command(args)
        if args.command == "sync":
            return _run_recorded_command(args)
        if args.command == "status":
            return command_status(args)
    except SyncError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
