from __future__ import annotations

import json
import pathlib
from typing import Any

from collections.abc import Sequence

from .core import (
    CONFIG_BUNDLE_MAX_BYTES,
    DIGESTS_MAX_BYTES,
    MANIFEST_MAX_BYTES,
    Settings,
    SyncError,
    SyncSnapshot,
    WebDAVClient,
    WebDAVHTTPError,
    _validate_bundle_header,
    archived_bundle_name,
    build_digest_record,
    bundle_snapshot,
    bundle_url,
    collection_url,
    create_bundle,
    digests_url,
    install_bundle,
    local_snapshot,
    manifest_name,
    manifest_url,
    manifests_match,
    read_config_bundle,
    read_digest_record,
    remote_sibling_url,
    save_conflict_record,
    save_sync_state,
    smart_sync_plan,
)

def print_manifest_summary(prefix: str, manifest: dict[str, Any]) -> None:
    summary = manifest.get("summary") if isinstance(manifest.get("summary"), dict) else {}
    print(
        f"{prefix}: providers={summary.get('providers', '?')} "
        f"active_models={summary.get('active_models', '?')} "
        f"disabled_models={summary.get('disabled_models', '?')} "
        f"created_at={manifest.get('created_at', '?')}"
    )




def read_remote_manifest(client: WebDAVClient, settings: Settings) -> dict[str, Any] | None:
    remote_manifest_url = manifest_url(settings)
    try:
        remote_manifest = json.loads(
            client.get(remote_manifest_url, max_bytes=MANIFEST_MAX_BYTES).decode("utf-8")
        )
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


def read_remote_digests(
    client: WebDAVClient,
    settings: Settings,
    remote_manifest: dict[str, Any],
) -> dict[str, str | None] | None:
    """The compared digests that belong to this remote manifest, or ``None``.

    The record is an optimization, never the connection: a server that refuses
    the file, an earlier version that never wrote one, or a record for a
    manifest that has since been replaced all answer ``None``, and the caller
    compares bytes the way every version before this one did.
    """

    try:
        data = client.get(digests_url(settings), max_bytes=DIGESTS_MAX_BYTES)
    except SyncError:
        # Every refusal reads as "no record": a server that does not answer for
        # this file must not turn a sync that worked yesterday into a failure.
        # `WebDAVHTTPError` is one of these.  The manifest and the bundle are
        # still read with the same credentials, so a real connection problem
        # surfaces there, with its own cause.
        return None
    return read_digest_record(data, remote_manifest)


def publish_remote_digests(
    client: WebDAVClient,
    settings: Settings,
    snapshot: SyncSnapshot,
) -> bool:
    """Write the record that lets the next run compare content, not bytes.

    A run that changed nothing still knows something the remote may not: which
    content its manifest holds.  Publishing it there is best effort -- a
    collection that refuses this file keeps the byte comparison it always had,
    and no sync fails over a record that is only an optimization.
    """

    if snapshot.manifest is None or snapshot.digests is None:
        return False
    try:
        client.put(
            digests_url(settings),
            build_digest_record(snapshot.manifest, snapshot.digests),
            "application/json; charset=utf-8",
        )
    except Exception:
        return False
    return True


def remote_snapshot_from_bundle(
    client: WebDAVClient,
    settings: Settings,
    remote_manifest: dict[str, Any],
) -> SyncSnapshot | None:
    """The remote side read from its bundle, when the bundle is that manifest."""

    try:
        bundle_data = client.get(bundle_url(settings), max_bytes=CONFIG_BUNDLE_MAX_BYTES)
        bundle_manifest, files = read_config_bundle(bundle_data)
    except SyncError:
        return None
    if not manifests_match(bundle_manifest, remote_manifest):
        return None
    return bundle_snapshot(bundle_manifest, files)


def remote_snapshot(
    client: WebDAVClient,
    settings: Settings,
    remote_manifest: dict[str, Any] | None,
) -> SyncSnapshot:
    """The remote side, with whatever its digest record says about it.

    One read of the sidecar answers for the whole side; a manifest with no
    record, or one written for another manifest, is a side that compares by
    bytes.  The bundle is not downloaded here: ``decide_sync`` asks for it only
    when the byte comparison would stop the run, which is the only case where
    the answer changes what happens.
    """

    if remote_manifest is None:
        return SyncSnapshot(None)
    return SyncSnapshot(remote_manifest, read_remote_digests(client, settings, remote_manifest))


def decide_sync(
    client: WebDAVClient,
    settings: Settings,
    local: SyncSnapshot,
    remote: SyncSnapshot,
    base: SyncSnapshot,
) -> tuple[str, SyncSnapshot]:
    """The plan for these sides, and the remote side that plan was made on.

    The remote's digest record is an optimization the server may not have: a
    collection that refuses the file, a bundle published before the record
    existed.  Without it the comparison falls back to bytes, which reads the
    relay store's own poll as an edit on both ends and would stop a run where
    only one side moved.  So a plan that stops is asked again once with the
    digests the bundle itself proves -- that bundle is what the conflict branch
    downloads anyway, and the caller gets back the side the plan was made on so
    the baseline it saves describes the content it actually compared.
    """

    plan = smart_sync_plan(local, remote, base)
    if plan != "conflict" or remote.digests is not None or remote.manifest is None:
        return plan, remote
    from_bundle = remote_snapshot_from_bundle(client, settings, remote.manifest)
    if from_bundle is None:
        return plan, remote
    return smart_sync_plan(local, from_bundle, base), from_bundle


def read_manifest_from_remote_bundle(client: WebDAVClient, settings: Settings) -> dict[str, Any]:
    remote_bundle_url = bundle_url(settings)
    try:
        manifest, _files = read_config_bundle(
            client.get(remote_bundle_url, max_bytes=CONFIG_BUNDLE_MAX_BYTES)
        )
    except WebDAVHTTPError:
        raise
    except SyncError:
        raise
    except Exception as exc:
        raise SyncError(f"Remote bundle manifest could not be read: {exc}") from exc
    _validate_bundle_header(manifest)
    return manifest


def adopt_renamed_remote_bundle(
    client: WebDAVClient,
    settings: Settings,
    legacy_names: Sequence[str],
) -> dict[str, Any] | None:
    """Move a bundle left under an earlier default name to this one.

    Renaming the setting alone would leave the previous file behind as a stale
    copy that still answers to the misleading name.  The bundle and its sidecar
    manifest are copied together, and nothing is removed before the new file
    exists.  A WebDAV server may refuse DELETE and MOVE outright (this app's own
    server answers 403 to both), so the removals are best-effort: the copy is
    what matters, and the caller states the leftover when the server keeps it.
    """
    target_url = bundle_url(settings)
    if _remote_file_exists(client, target_url):
        return None
    for name in legacy_names:
        if name == settings.remote_name:
            continue
        source_url = remote_sibling_url(settings, name)
        if not _remote_file_exists(client, source_url):
            continue
        client.put(target_url, client.get(source_url, max_bytes=CONFIG_BUNDLE_MAX_BYTES), "application/json; charset=utf-8")
        removals = [source_url]
        source_manifest_url = remote_sibling_url(settings, manifest_name(name))
        if _remote_file_exists(client, source_manifest_url):
            client.put(
                remote_sibling_url(settings, manifest_name(settings.remote_name)),
                client.get(source_manifest_url, max_bytes=MANIFEST_MAX_BYTES),
                "application/json; charset=utf-8",
            )
            removals.insert(0, source_manifest_url)
        removed = True
        for url in removals:
            try:
                client.delete(url)
            except Exception:
                removed = False
        return {"name": name, "removed": removed}
    return None


def _remote_file_exists(client: WebDAVClient, url: str) -> bool:
    try:
        client.head(url)
        return True
    except WebDAVHTTPError as exc:
        if exc.code == 404:
            return False
        raise


def remember_remote_bundle(
    client: WebDAVClient,
    settings: Settings,
    conflict_path: pathlib.Path,
) -> None:
    """Keep the remote side of a stopped sync for the reader to compare.

    The run that detects a conflict has only compared manifests; the texts the
    comparison shows are inside the bundle, and the window that shows them
    usually opens after that run is over.  Reading the bundle once here -- while
    the run already holds the connection -- is what lets the pane answer
    later without touching the server again.
    """

    bundle_data = client.get(bundle_url(settings), max_bytes=CONFIG_BUNDLE_MAX_BYTES)
    manifest, files = read_config_bundle(bundle_data)
    created_at = manifest.get("created_at")
    save_conflict_record(
        conflict_path,
        files,
        created_at if isinstance(created_at, str) else None,
    )


def archive_remote_bundle(client: WebDAVClient, settings: Settings) -> str:
    """Copy a remote bundle this version cannot read to a dated sibling name.

    Smart sync replaces such a file with this Mac's state.  The file is copied
    aside first, so replacing a bundle written by another app identity or an
    older archive version never destroys what was there.
    """
    data = client.get(bundle_url(settings), max_bytes=CONFIG_BUNDLE_MAX_BYTES)
    name = archived_bundle_name(settings.remote_name)
    client.put(remote_sibling_url(settings, name), data, "application/json; charset=utf-8")
    return name


def push_bundle(
    client: WebDAVClient,
    settings: Settings,
    config_path: pathlib.Path,
    state_path: pathlib.Path | None = None,
    action: str = "push",
) -> tuple[int, dict[str, Any]]:
    remote_bundle_url = bundle_url(settings)
    bundle_data, snapshot = create_bundle(config_path)
    manifest = snapshot.manifest or {}
    manifest_data = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    digest_data = build_digest_record(manifest, snapshot.digests or {})

    client.try_mkcol(collection_url(settings))
    client.put(remote_bundle_url, bundle_data, "application/json; charset=utf-8")
    try:
        client.put(manifest_url(settings), manifest_data, "application/json; charset=utf-8")
    except WebDAVHTTPError as exc:
        if exc.code not in {403, 404, 405, 409, 500, 502, 503, 504}:
            raise
    else:
        # The record is bound to the manifest that was just written.  A server
        # that refuses either one therefore leaves the other stale by identity,
        # which the next read treats as "no record" instead of as an answer
        # about content this upload never carried.
        try:
            client.put(digests_url(settings), digest_data, "application/json; charset=utf-8")
        except WebDAVHTTPError as exc:
            if exc.code not in {403, 404, 405, 409, 500, 502, 503, 504}:
                raise
    if state_path is not None:
        save_sync_state(state_path, settings, snapshot, action)
    return len(bundle_data), manifest


def pull_bundle(
    client: WebDAVClient,
    settings: Settings,
    config_path: pathlib.Path,
    state_path: pathlib.Path | None = None,
    action: str = "pull",
    *,
    skip_when_unchanged: bool = True,
) -> dict[str, Any]:
    """Install the remote bundle, or state that there is nothing to install.

    ``skip_when_unchanged`` reads the sidecar manifest (a few hundred bytes)
    before the bundle, and stops there when its fingerprints already match the
    files on disk.  A keep-in-sync host pulls on an interval: without this the
    whole bundle is downloaded every tick to discover it is the one already
    installed.  The answer is the same either way -- nothing is written -- so
    the comparison can only save the transfer, never change the outcome, and an
    unreadable side (a corrupt local config, a refused manifest) falls back to
    the ordinary whole-bundle pull.
    """

    remote_bundle_url = bundle_url(settings)
    if skip_when_unchanged and config_path.exists():
        try:
            local = local_snapshot(config_path)
            remote = remote_snapshot(client, settings, read_remote_manifest(client, settings))
        except SyncError:
            remote = SyncSnapshot(None)
        else:
            if remote.manifest is not None and local.matches(remote):
                # The bytes matched, so this side's digests describe the remote
                # too.  Stating them for the run keeps the next comparison off
                # the raw bytes, and publishing them keeps the other machine's
                # next run off them as well.
                agreed = SyncSnapshot(remote.manifest, remote.digests or local.digests)
                if state_path is not None:
                    save_sync_state(state_path, settings, agreed, action)
                if remote.digests is None:
                    publish_remote_digests(client, settings, agreed)
                return {
                    "manifest": agreed.manifest,
                    "digests": agreed.digests,
                    "snapshot": agreed,
                    "installed": [],
                    "removed": [],
                    "backups": [],
                }
    bundle_data = client.get(remote_bundle_url, max_bytes=CONFIG_BUNDLE_MAX_BYTES)
    result = install_bundle(bundle_data, config_path)
    snapshot = result["snapshot"]
    if state_path is not None:
        save_sync_state(state_path, settings, snapshot, action)
    # A pull has the bundle in hand, so the record can always be stated for the
    # manifest it came with -- even when this run installed nothing new.
    publish_remote_digests(client, settings, snapshot)
    return result
