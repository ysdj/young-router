from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from young_router.webdav import core as webdav_core  # noqa: E402
from young_router.webdav import merge as webdav_merge  # noqa: E402
from young_router.webdav import operations as webdav_operations  # noqa: E402


CONFIG = textwrap.dedent(
    """
    providers:
      local:
        api_base: "https://example.com/v1"
        api_keys:
          - name: default
            value: "sk-test"
    model_list:
      - model_name: default-chat
        litellm_params:
          model: openai/default-chat
          api_base: "https://example.com/v1"
          api_key: "sk-test"
        model_info:
          id: "00000001"
          provider: local
          upstream_url_surface: openai/responses
      - model_name: second-chat
        litellm_params:
          model: openai/second-chat
          api_base: "https://example.com/v1"
          api_key: "sk-test"
          order: 1
        model_info:
          id: "00000002"
          provider: local
          upstream_url_surface: openai/responses
    litellm_settings:
      public_model_groups:
        - default-chat
        - second-chat
    """
).lstrip()


def dump(document: dict) -> str:
    return yaml.safe_dump(
        document,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=1000,
    )


def add_route(text: str, name: str, deployment_id: str) -> str:
    document = yaml.safe_load(text)
    document["model_list"] = [
        *document["model_list"],
        {
            "model_name": name,
            "litellm_params": {"model": f"openai/{name}", "api_base": "https://example.com/v1", "api_key": "sk-test"},
            "model_info": {"id": deployment_id, "provider": "local", "upstream_url_surface": "openai/responses"},
        },
    ]
    document["litellm_settings"]["public_model_groups"] = [
        *document["litellm_settings"]["public_model_groups"],
        name,
    ]
    return dump(document)


def route_names(text: str) -> list[str]:
    return [row["model_name"] for row in (yaml.safe_load(text)["model_list"] or [])]


def set_order(text: str, deployment_id: str, order: float) -> str:
    document = yaml.safe_load(text)
    for row in document["model_list"]:
        if row["model_info"]["id"] == deployment_id:
            row["litellm_params"]["order"] = order
    return dump(document)


def route_order(text: str, deployment_id: str) -> float | None:
    for row in yaml.safe_load(text)["model_list"]:
        if row["model_info"]["id"] == deployment_id:
            return row["litellm_params"].get("order")
    return None


def remove_route(text: str, deployment_id: str) -> str:
    document = yaml.safe_load(text)
    document["model_list"] = [
        row for row in document["model_list"] if row["model_info"]["id"] != deployment_id
    ]
    document["litellm_settings"]["public_model_groups"] = [
        row["model_name"] for row in document["model_list"]
    ]
    return dump(document)


class MergeDocumentTests(unittest.TestCase):
    """The rules that let a stopped sync settle itself.

    Every case here is one document, three copies: the content the last sync
    agreed on and the two edits made since.  What the merge may never do is
    answer ``None`` for edits that fit together, or invent a document for edits
    that do not.
    """

    def merge(self, base: str, ours: str, theirs: str) -> str | None:
        return webdav_merge.merge_document("config.yaml", base, ours, theirs)

    def test_two_machines_adding_different_routes_merge(self) -> None:
        """The case the sync used to stop on: both sides appended a route.

        Rows are matched by deployment id, so two additions are two additions.
        Matching by position (or by model name) would report one collision for
        the pair and make the reader choose which route to lose.
        """

        ours = add_route(CONFIG, "added-here", "0000aaaa")
        theirs = add_route(CONFIG, "added-there", "0000bbbb")

        merged = self.merge(CONFIG, ours, theirs)

        assert merged is not None
        self.assertEqual(
            ["default-chat", "second-chat", "added-here", "added-there"],
            route_names(merged),
        )
        self.assertEqual(
            ["default-chat", "second-chat", "added-here", "added-there"],
            yaml.safe_load(merged)["litellm_settings"]["public_model_groups"],
        )

    def test_one_side_editing_a_row_and_the_other_adding_one_merge(self) -> None:
        ours = add_route(CONFIG, "added-here", "0000aaaa")
        theirs = set_order(CONFIG, "00000002", 3)

        merged = self.merge(CONFIG, ours, theirs)

        assert merged is not None
        self.assertEqual(3, route_order(merged, "00000002"))
        self.assertIn("added-here", route_names(merged))

    def test_a_rewritten_document_is_not_an_edit(self) -> None:
        """Whichever machine saved the file wrote it back in its own shape.

        A row that only moved in the text is still the row the baseline held,
        so a side that merely re-formatted the document does not compete with
        the side that changed it -- and the merge keeps the changed content
        instead of colliding on the formatting.
        """

        reformatted = dump(yaml.safe_load(CONFIG))
        theirs = set_order(CONFIG, "00000002", 4)

        merged = self.merge(CONFIG, reformatted, theirs)

        assert merged is not None
        self.assertEqual(4, route_order(merged, "00000002"))

    def test_a_key_added_on_both_sides_merges(self) -> None:
        """A provider's keys are rows too, identified by the name given them."""

        def add_key(text: str, name: str) -> str:
            document = yaml.safe_load(text)
            document["providers"]["local"]["api_keys"].append({"name": name, "value": f"sk-{name}"})
            return dump(document)

        merged = self.merge(CONFIG, add_key(CONFIG, "here"), add_key(CONFIG, "there"))

        assert merged is not None
        names = [row["name"] for row in yaml.safe_load(merged)["providers"]["local"]["api_keys"]]
        self.assertEqual(["default", "here", "there"], names)

    def test_the_same_field_of_one_route_changed_on_both_sides_collides(self) -> None:
        """Neither copy is a version of the other: this stays the reader's."""

        self.assertIsNone(
            self.merge(CONFIG, set_order(CONFIG, "00000002", 2), set_order(CONFIG, "00000002", 9))
        )

    def test_a_route_removed_here_and_edited_there_collides(self) -> None:
        self.assertIsNone(
            self.merge(CONFIG, remove_route(CONFIG, "00000002"), set_order(CONFIG, "00000002", 5))
        )

    def test_a_route_removed_here_and_untouched_there_stays_removed(self) -> None:
        theirs = add_route(CONFIG, "added-there", "0000bbbb")

        merged = self.merge(CONFIG, remove_route(CONFIG, "00000002"), theirs)

        assert merged is not None
        self.assertEqual(["default-chat", "added-there"], route_names(merged))

    def test_two_models_added_under_one_name_are_two_rows(self) -> None:
        """A model name is a group: two deployments of it are not one row."""

        def add_deployment(text: str, deployment_id: str) -> str:
            document = yaml.safe_load(text)
            row = dict(document["model_list"][0])
            row["litellm_params"] = dict(row["litellm_params"], order=2)
            row["model_info"] = dict(row["model_info"], id=deployment_id)
            document["model_list"] = [*document["model_list"], row]
            return dump(document)

        merged = self.merge(CONFIG, add_deployment(CONFIG, "0000cccc"), add_deployment(CONFIG, "0000dddd"))

        assert merged is not None
        self.assertEqual(
            ["00000001", "00000002", "0000cccc", "0000dddd"],
            [row["model_info"]["id"] for row in yaml.safe_load(merged)["model_list"]],
        )

    def test_a_row_pair_the_app_would_refuse_never_reaches_the_disk(self) -> None:
        """Row-by-row merging can produce a document no single side would write.

        A provider's relay slots name the API key they answer with, and the
        schema refuses a slot naming a key that is not there.  One machine
        renaming that key while the other adds a slot for it is therefore a
        real collision -- even though neither side touched the other's row --
        and the validator is what turns it back into the reader's choice.
        """

        base = textwrap.dedent(
            """
            providers:
              local:
                api_base: "https://example.com/v1"
                api_keys:
                  - name: default
                    value: "sk-test"
                  - name: second
                    value: "sk-second"
                x-young-router-relay-keys:
                  version: 1
                  slots:
                    - id: provider-slot-aaaa
                      api_key_name: default
                      source: {kind: independent}
            model_list: []
            """
        ).lstrip()

        renamed = yaml.safe_load(base)
        renamed["providers"]["local"]["api_keys"][0]["name"] = "renamed"
        renamed["providers"]["local"]["x-young-router-relay-keys"]["slots"][0]["api_key_name"] = "renamed"

        added_slot = yaml.safe_load(base)
        added_slot["providers"]["local"]["x-young-router-relay-keys"]["slots"].append(
            {"id": "provider-slot-bbbb", "api_key_name": "default", "source": {"kind": "independent"}}
        )

        self.assertIsNone(self.merge(base, dump(renamed), dump(added_slot)))

    def test_a_merged_document_that_would_not_load_is_refused(self) -> None:
        """A merge this app cannot apply is not a merge.

        The result is validated with the loader the editor and the bundle use,
        so a combination that only makes sense row by row -- and would be
        refused at load -- falls back to the reader's choice.
        """

        def rename_model(text: str) -> str:
            document = yaml.safe_load(text)
            document["model_list"] = "not a list"
            return dump(document)

        self.assertIsNone(self.merge(CONFIG, rename_model(CONFIG), add_route(CONFIG, "added", "0000eeee")))


class MergeFileSetTests(unittest.TestCase):
    """The whole side, not one document: absence and the relay store."""

    def test_a_file_one_side_removed_and_the_other_left_alone_stays_removed(self) -> None:
        merged = webdav_merge.merge_sync_files(
            {"config.yaml": CONFIG.encode(), "config.disabled-models.yaml": b"disabled_model_list: []\n"},
            {"config.yaml": CONFIG.encode()},
            {"config.yaml": add_route(CONFIG, "added", "0000aaaa").encode()},
        )

        assert merged is not None
        self.assertEqual({"config.yaml"}, set(merged))

    def test_a_file_one_side_removed_and_the_other_edited_collides(self) -> None:
        merged = webdav_merge.merge_sync_files(
            {"config.yaml": CONFIG.encode(), "config.disabled-models.yaml": b"disabled_model_list: []\n"},
            {"config.yaml": CONFIG.encode()},
            {"config.yaml": CONFIG.encode(), "config.disabled-models.yaml": b"disabled_model_list:\n  - model_name: gone\n"},
        )

        self.assertIsNone(merged)

    def test_a_relay_store_changed_on_both_sides_merges(self) -> None:
        """The store's own rule already answers this: both sides, minus removals."""

        def store(station: str, *, removed: str | None = None) -> bytes:
            document = {
                "version": 3,
                "stations": [{"id": station, "name": station, "origin": f"https://{station}.example.test", "type": "newapi"}],
                "accounts": [
                    {
                        "id": f"account-{station}",
                        "station_id": station,
                        "label": station,
                        "username": "person",
                        "password": "replace-password",
                    }
                ],
                "pending_credential_cleanups": [],
            }
            if removed is not None:
                document["removed_stations"] = [{"id": removed}]
                document["removed_accounts"] = [{"id": f"account-{removed}"}]
            return json.dumps(document).encode()

        merged = webdav_merge.merge_sync_files(
            {webdav_core.RELAY_ACCOUNTS_FILE: store("base")},
            {webdav_core.RELAY_ACCOUNTS_FILE: store("here")},
            {webdav_core.RELAY_ACCOUNTS_FILE: store("there", removed="here")},
        )

        assert merged is not None
        document = json.loads(merged[webdav_core.RELAY_ACCOUNTS_FILE])
        self.assertEqual(["there"], [row["id"] for row in document["stations"]])
        self.assertIn("here", [row["id"] for row in document["removed_stations"]])


class _MemoryWebDAV:
    """A WebDAV collection held in memory: PUT stores, GET answers or 404s."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def try_mkcol(self, url: str) -> None:
        return None

    def put(self, url: str, data: bytes, content_type: str) -> None:
        self.files[url] = data

    def get(self, url: str, max_bytes: int | None = None) -> bytes:
        if url not in self.files:
            raise webdav_core.WebDAVHTTPError("GET", url, 404, "Not Found")
        return self.files[url]


class SyncMergeTests(unittest.TestCase):
    """Two machines, one collection, the calls the app itself makes."""

    settings = webdav_core.Settings(
        url="https://dav.example.test/young-router/",
        remote_name="config.json",
    )

    def sync(self, server: _MemoryWebDAV, config_path: Path, state_path: Path) -> str:
        local = webdav_core.local_snapshot(config_path)
        remote = webdav_operations.remote_snapshot(
            server, self.settings, webdav_operations.read_remote_manifest(server, self.settings)
        )
        if remote.manifest is None:
            webdav_operations.push_bundle(server, self.settings, config_path, state_path, "sync-push")
            return "push"
        base = webdav_core.baseline_snapshot(state_path)
        plan, remote = webdav_operations.decide_sync(server, self.settings, local, remote, base)
        if plan == "unchanged":
            agreed = webdav_core.SyncSnapshot(
                remote.manifest,
                remote.digests or local.digests,
                local.files if local.files is not None else remote.files,
            )
            webdav_core.save_sync_state(state_path, self.settings, agreed, "sync")
            return "unchanged"
        if plan == "push":
            webdav_operations.push_bundle(server, self.settings, config_path, state_path, "sync-push")
            return "push"
        if plan == "conflict":
            merged = webdav_operations.conflict_merge(server, self.settings, local, remote, base)
            if merged is None:
                return "conflict"
            webdav_operations.install_merged_files(server, self.settings, config_path, state_path, merged)
            return "merge"
        webdav_operations.pull_bundle(
            server, self.settings, config_path, state_path, "sync-pull", skip_when_unchanged=False
        )
        return "pull"

    def machines(self, root: Path) -> tuple[Path, Path, Path, Path]:
        (root / "mac").mkdir()
        (root / "peer").mkdir()
        mac_config = root / "mac" / "config.yaml"
        peer_config = root / "peer" / "config.yaml"
        for path in (mac_config, peer_config):
            path.write_text(CONFIG, encoding="utf-8")
        return mac_config, root / "mac" / "state.json", peer_config, root / "peer" / "state.json"

    def test_a_rejected_manifest_update_cannot_hide_the_new_bundle(self) -> None:
        class PartialPublication(_MemoryWebDAV):
            reject_manifest = False

            def put(self, url: str, data: bytes, content_type: str) -> None:
                if self.reject_manifest and url.endswith(".manifest.json"):
                    raise webdav_core.WebDAVHTTPError("PUT", url, 403, "Forbidden")
                super().put(url, data, content_type)

        with tempfile.TemporaryDirectory() as directory:
            sender, sender_state, peer, peer_state = self.machines(Path(directory))
            server = PartialPublication()
            self.assertEqual("push", self.sync(server, sender, sender_state))
            self.assertEqual("unchanged", self.sync(server, peer, peer_state))
            old_manifest = server.files[webdav_core.manifest_url(self.settings)]
            sender.write_text(CONFIG.replace("order: 1", "order: 2"), encoding="utf-8")
            server.reject_manifest = True
            self.assertEqual("push", self.sync(server, sender, sender_state))
            self.assertEqual(old_manifest, server.files[webdav_core.manifest_url(self.settings)])

            self.assertEqual("pull", self.sync(server, peer, peer_state))
            self.assertEqual(sender.read_bytes(), peer.read_bytes())
            # Explicit pull has the same authority rule as automatic sync.
            peer.write_text(CONFIG, encoding="utf-8")
            result = webdav_operations.pull_bundle(server, self.settings, peer)
            self.assertEqual([str(peer)], result["installed"])
            self.assertEqual(sender.read_bytes(), peer.read_bytes())

    def test_explicit_bundle_urls_keep_their_own_sidecars(self) -> None:
        a = webdav_core._settings_from_raw({"url": "https://dav.example.test/dav/a%20one.json?token=x"})
        b = webdav_core._settings_from_raw({"url": "https://dav.example.test/dav/b.json?token=x"})
        self.assertEqual("https://dav.example.test/dav/a%20one.manifest.json?token=x", webdav_core.manifest_url(a))
        self.assertEqual("https://dav.example.test/dav/a%20one.digests.json?token=x", webdav_core.digests_url(a))
        self.assertNotEqual(webdav_core.manifest_url(a), webdav_core.manifest_url(b))
        self.assertNotEqual(webdav_core.digests_url(a), webdav_core.digests_url(b))
        with tempfile.TemporaryDirectory() as directory:
            sender, _, peer, _ = self.machines(Path(directory))
            server = _MemoryWebDAV()
            sender.write_text(CONFIG.replace("order: 1", "order: 2"), encoding="utf-8")
            webdav_operations.push_bundle(server, a, sender)
            webdav_operations.push_bundle(server, b, peer)
            result = webdav_operations.pull_bundle(server, a, peer)
            self.assertEqual([str(peer)], result["installed"])
            self.assertEqual(sender.read_bytes(), peer.read_bytes())

    def test_both_machines_editing_one_document_settles_itself(self) -> None:
        """The whole failure, end to end, through the real sync entry points.

        Neither machine is wrong: this one added a route, the other added a
        route and changed one row's order.  The merge installs both edits,
        publishes the result, and the peer's next run takes it -- no reader, no
        comparison window, and nothing dropped.
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            mac_config, mac_state, peer_config, peer_state = self.machines(Path(temp_dir))
            server = _MemoryWebDAV()

            self.assertEqual("push", self.sync(server, mac_config, mac_state))
            self.assertEqual("unchanged", self.sync(server, peer_config, peer_state))
            # The baseline keeps the documents, which is what makes the merge
            # below possible at all.
            self.assertEqual({"config.yaml"}, set(webdav_core.baseline_snapshot(peer_state).files or {}))

            mac_config.write_text(add_route(CONFIG, "added-here", "0000aaaa"), encoding="utf-8")
            self.assertEqual("push", self.sync(server, mac_config, mac_state))

            peer_config.write_text(
                set_order(add_route(CONFIG, "added-there", "0000bbbb"), "00000002", 5),
                encoding="utf-8",
            )
            self.assertEqual("merge", self.sync(server, peer_config, peer_state))
            merged = peer_config.read_text(encoding="utf-8")
            self.assertEqual(
                ["default-chat", "second-chat", "added-there", "added-here"],
                route_names(merged),
            )
            self.assertEqual(5, route_order(merged, "00000002"))

            # The merge was published, so the other machine converges on it
            # rather than pushing its own copy back over it.
            self.assertEqual("pull", self.sync(server, mac_config, mac_state))
            self.assertEqual(route_names(merged), route_names(mac_config.read_text(encoding="utf-8")))
            self.assertEqual("unchanged", self.sync(server, peer_config, peer_state))
            self.assertEqual("unchanged", self.sync(server, mac_config, mac_state))

    def test_the_same_field_of_one_route_still_stops_for_a_choice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mac_config, mac_state, peer_config, peer_state = self.machines(Path(temp_dir))
            server = _MemoryWebDAV()

            self.assertEqual("push", self.sync(server, mac_config, mac_state))
            self.assertEqual("unchanged", self.sync(server, peer_config, peer_state))
            mac_config.write_text(set_order(CONFIG, "00000002", 2), encoding="utf-8")
            self.assertEqual("push", self.sync(server, mac_config, mac_state))
            peer_config.write_text(set_order(CONFIG, "00000002", 9), encoding="utf-8")

            self.assertEqual("conflict", self.sync(server, peer_config, peer_state))
            # The run wrote nothing: the copy the reader compares is the one
            # this machine already had.
            self.assertEqual(9, route_order(peer_config.read_text(encoding="utf-8"), "00000002"))

    def test_a_baseline_without_documents_keeps_the_choice(self) -> None:
        """A state file written before the merge existed asks, as it always did."""

        with tempfile.TemporaryDirectory() as temp_dir:
            mac_config, mac_state, peer_config, peer_state = self.machines(Path(temp_dir))
            server = _MemoryWebDAV()

            self.assertEqual("push", self.sync(server, mac_config, mac_state))
            self.assertEqual("unchanged", self.sync(server, peer_config, peer_state))
            mac_config.write_text(add_route(CONFIG, "added-here", "0000aaaa"), encoding="utf-8")
            self.assertEqual("push", self.sync(server, mac_config, mac_state))
            peer_config.write_text(add_route(CONFIG, "added-there", "0000bbbb"), encoding="utf-8")

            # An earlier version's state: the manifest and digests, no content.
            state = json.loads(peer_state.read_text(encoding="utf-8"))
            state.pop("contents", None)
            peer_state.write_text(json.dumps(state), encoding="utf-8")

            self.assertEqual("conflict", self.sync(server, peer_config, peer_state))


class SyncStateContentsTests(unittest.TestCase):
    """What the baseline keeps, and what it refuses to read as a baseline."""

    settings = webdav_core.Settings(url="https://dav.example.test/young-router/", remote_name="config.json")

    def test_the_saved_baseline_carries_the_documents_it_agreed_on(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            snapshot = webdav_core.SyncSnapshot(
                {"files": [{"path": "config.yaml", "present": True}]},
                {"config.yaml": "abc"},
                {"config.yaml": CONFIG.encode()},
            )

            webdav_core.save_sync_state(path, self.settings, snapshot, "sync")
            restored = webdav_core.baseline_snapshot(path)

        self.assertEqual({"config.yaml": CONFIG.encode()}, restored.files)

    def test_content_that_does_not_describe_the_manifest_is_dropped(self) -> None:
        """A file the record leaves out would read as a deletion.

        So an ancestor that does not name every file the manifest names is no
        ancestor at all: the sync then stops and offers the comparison rather
        than merging against a document one side never had.
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "version": webdav_core.SYNC_STATE_VERSION,
                        "manifest": {
                            "files": [
                                {"path": "config.yaml", "present": True},
                                {"path": "config.disabled-models.yaml", "present": True},
                            ]
                        },
                        "digests": {"config.yaml": "abc", "config.disabled-models.yaml": "def"},
                        "contents": {"config.yaml": CONFIG},
                    }
                ),
                encoding="utf-8",
            )

            restored = webdav_core.baseline_snapshot(path)

        self.assertIsNone(restored.files)
        self.assertEqual({"config.yaml": "abc", "config.disabled-models.yaml": "def"}, restored.digests)


if __name__ == "__main__":
    unittest.main()
