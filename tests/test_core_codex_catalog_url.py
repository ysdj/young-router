"""URL-first catalog delivery, with the generated file as the fallback.

Codex reads a top-level ``model_catalog_json`` before it ever looks at a
provider row, and an explicit catalog replaces its bundled list rather than
falling back to it.  So the two carriers are one decision: the served address
is written only together with dropping the pointer, and the file is restored
the moment the address stops answering with exactly the list the router owns.
"""

from __future__ import annotations

import http.server
import json
import threading
import tomllib
import unittest
from pathlib import Path
import tempfile
from unittest import mock

from young_router.core.domains.codex import CodexSettingsDomain
from young_router.core.model_catalog import (
    MAX_SERVED_CATALOG_BYTES,
    SERVED_CATALOG_ROUTE,
    served_catalog_body,
)
from young_router.core.service import CoreStore


CONFIG = (
    "providers:\n"
    "  primary:\n"
    "    api_base: https://example.test/v1\n"
    "    api_keys:\n"
    "      - name: default\n"
    "        value: synthetic-key\n"
    "model_list:\n"
    "  - model_name: public-a\n"
    "    litellm_params:\n"
    "      model: openai/upstream-a\n"
    "      api_base: https://example.test/v1\n"
    "      api_key: synthetic-key\n"
    "  - model_name: public-b\n"
    "    litellm_params:\n"
    "      model: openai/upstream-b\n"
    "      api_base: https://example.test/v1\n"
    "      api_key: synthetic-key\n"
    "litellm_settings:\n"
    "  public_model_groups: [public-a, public-b]\n"
    "general_settings:\n"
    "  master_key: sk-synthetic-master\n"
)

#: A row already on this app's proxy: the catalog address is that same origin's
#: ``/v1/models``, which is what makes it recognisable as ours at all.
CLIENT_CONFIG = (
    'model = "public-a"\n'
    'model_provider = "local"\n'
    "\n"
    '[model_providers.local]\n'
    'name = "local"\n'
    'base_url = "http://127.0.0.1:12390/v1"\n'
    'wire_api = "responses"\n'
    'requires_openai_auth = true\n'
)


def client_config(_origin: str = "", *, extra: str = "") -> str:
    """The client document; the listener's own port is irrelevant to it.

    Core's catalog route is one path of its own, so a row is recognised as this
    app's by that path -- not by sharing an origin with a test listener.
    """

    return CLIENT_CONFIG + extra


class _CatalogServer:
    """A loopback answer for the address the domain would serve.

    It stands in for Core's own listener: the domain only has to prove that the
    address answers with the models it owns, and that answer is checked with the
    same credential a client presents.
    """

    def __init__(self, models: list[str], *, status: int = 200) -> None:
        self.models = models
        self.status = status
        self.authorization = ""
        self.query = ""
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_CatalogServer":
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib hook
                owner.authorization = self.headers.get("Authorization", "")
                owner.query = self.path.partition("?")[2]
                if self.path.partition("?")[0].rstrip("/") != SERVED_CATALOG_ROUTE or owner.status != 200:
                    body = b'{"error":{"code":"catalog_unavailable"}}'
                    self.send_response(owner.status if owner.status != 200 else 404)
                else:
                    body = json.dumps(
                        {"models": [{"slug": name} for name in owner.models]}
                    ).encode("utf-8")
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}{SERVED_CATALOG_ROUTE}"


class CatalogUrlDeliveryTests(unittest.TestCase):
    def _domain(self, root: Path, *, document: str = CLIENT_CONFIG) -> CodexSettingsDomain:
        (root / "config.yaml").write_text(CONFIG, encoding="utf-8")
        home = root / "codex"
        home.mkdir()
        (home / "config.toml").write_text(document, encoding="utf-8")
        (home / "auth.json").write_text('{"OPENAI_API_KEY": "sk-synthetic-master"}\n', encoding="utf-8")
        return CodexSettingsDomain(root / "config.yaml", codex_home=home)

    def _resolve(self, domain: CodexSettingsDomain) -> None:
        domain.refresh_local_api_endpoint()

    def _serve(self, url: str) -> None:
        """Point the domain's address at this test's answer, across reloads.

        The address is resolved from the runtime document on every reload, so a
        value set on the instance would be replaced by the next read.
        """

        patcher = mock.patch(
            "young_router.core.domains._shared.local_catalog_url", return_value=url
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self._served_url = url

    @staticmethod
    def _enable(domain: CodexSettingsDomain, names: list[str]) -> None:
        """The menu switch is the explicit statement that a catalog is wanted."""

        with mock.patch(
            "young_router.core.codex_config.local_exposed_models",
            return_value=(names, True),
        ):
            domain.set_model_catalog_enabled_immediately(True)

    @staticmethod
    def _exposed(domain: CodexSettingsDomain, names: list[str]) -> None:
        with mock.patch(
            "young_router.core.codex_config.local_exposed_models",
            return_value=(names, True),
        ):
            domain._catalog_source_checked_at = 0.0
            domain._ensure_model_catalog_current(force_source_refresh=True)

    def _written(self, root: Path) -> tuple[dict, str]:
        text = (root / "codex" / "config.toml").read_text(encoding="utf-8")
        return tomllib.loads(text), text

    def test_a_working_address_replaces_the_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = self._domain(root)
            with _CatalogServer(["public-a", "public-b"]) as server:
                self._serve(server.url)
                self._resolve(domain)
                self._enable(domain, ["public-a", "public-b"])
                with mock.patch(
                    "young_router.core.codex_config.local_exposed_models",
                    return_value=(["public-a", "public-b"], True),
                ):
                    domain.dispatch("use_local_api", {"model": "public-a"})
                    domain.apply()
                    document, _text = self._written(root)
                    provider = document["model_providers"]["local"]
                    self.assertEqual(server.url, provider["model_catalog_url"])
                    # Codex would ignore that URL entirely while the pointer is
                    # present, so the two are written as one decision.
                    self.assertNotIn("model_catalog_json", document)
                    self.assertEqual("sk-synthetic-master", server.authorization.split(" ", 1)[1])

    def test_the_edit_api_form_reports_the_live_catalog(self) -> None:
        """The form opens on the live proxy catalog, not the off backup."""

        direct = (
            'model = "direct-chat"\n'
            'model_provider = "direct"\n'
            "\n"
            '[model_providers.direct]\n'
            'name = "direct"\n'
            'base_url = "https://gateway.example.test/v1"\n'
            'wire_api = "responses"\n'
            'model_catalog_url = "https://gateway.example.test/catalog"\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = self._domain(root, document=direct)
            with _CatalogServer(["public-a", "public-b"]) as server:
                self._serve(server.url)
                self._resolve(domain)
                # Adopt first, then turn the catalog switch on.  The form
                # shows the live row the switch is on, not the direct row the
                # off action will restore.
                domain.dispatch("use_local_api", {"model": "public-a"})
                self._enable(domain, ["public-a", "public-b"])
                snapshot = domain.snapshot()
                self.assertTrue(snapshot["uses_local_api"])
                self.assertEqual("url", snapshot["model_catalog"]["delivery"])
                self.assertEqual(
                    domain._local_api_base_url,
                    snapshot["direct_api"]["base_url"],
                )
                self.assertEqual(
                    server.url,
                    snapshot["direct_api"]["model_catalog_url"],
                )
                self.assertTrue(snapshot["direct_api"]["key_configured"])
                self.assertEqual(
                    "sk-synthetic-master",
                    domain.trusted_secret_value("direct_api_key", "edit-api"),
                )

                domain.dispatch("leave_local_api", {})
                domain.apply()
            document = tomllib.loads((root / "codex" / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(
                "https://gateway.example.test/catalog",
                document["model_providers"]["direct"]["model_catalog_url"],
            )

    def test_the_snapshot_states_which_carrier_is_live(self) -> None:
        """The file exists in both modes, so the window cannot infer this."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = self._domain(root)
            with _CatalogServer(["public-a", "public-b"]) as server:
                self._serve(server.url)
                self._resolve(domain)
                self._enable(domain, ["public-a", "public-b"])
                self.assertEqual("url", domain.snapshot()["model_catalog"]["delivery"])
            self._serve("http://127.0.0.1:9" + SERVED_CATALOG_ROUTE)
            domain.refresh_local_api_endpoint()
            domain._catalog_url_checked_at = 0.0
            with mock.patch(
                "young_router.core.codex_config.local_exposed_models",
                return_value=(["public-a", "public-b"], True),
            ):
                domain._catalog_source_checked_at = 0.0
                domain._ensure_model_catalog_current(force_source_refresh=True)
            self.assertEqual("file", domain.snapshot()["model_catalog"]["delivery"])

    def test_a_silent_address_falls_back_to_the_generated_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = self._domain(root)
            # Nothing listens here: the "cannot fetch" half of the rule.
            self._serve("http://127.0.0.1:9" + SERVED_CATALOG_ROUTE)
            self._resolve(domain)
            with mock.patch(
                "young_router.core.codex_config.local_exposed_models",
                return_value=(["public-a", "public-b"], True),
            ):
                domain.set_model_catalog_enabled_immediately(True)
                document, _text = self._written(root)
                self.assertEqual(
                    str(domain.model_catalog_path), document["model_catalog_json"]
                )
                self.assertNotIn("model_catalog_url", document["model_providers"]["local"])
                self.assertTrue(domain.model_catalog_path.exists())

    def test_the_switch_round_trip_keeps_the_catalog_on_the_proxy_row(self) -> None:
        """Off restores the direct OpenAI row; on returns to the proxy row.

        The desktop owns its top-level config and rewrites it; a catalog URL on
        the proxy provider row is the carrier that survives that write.
        """

        document = 'model = "public-a"\nmodel_provider = "openai"\n'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with _CatalogServer(["public-a", "public-b"]) as server, mock.patch(
                "young_router.core.codex_config.local_exposed_models",
                return_value=(["public-a", "public-b"], True),
            ):
                self._serve(server.url)
                domain = self._domain(root, document=document)
                self._resolve(domain)
                domain.dispatch("use_local_api", {"model": "public-a"})
                domain.apply()
                self.assertTrue(domain.snapshot()["model_catalog"]["enabled"])
                written, _text = self._written(root)
                self.assertEqual("custom", written["model_provider"])
                self.assertEqual(
                    server.url,
                    written["model_providers"]["custom"]["model_catalog_url"],
                )
                self.assertNotIn("model_catalog_json", written)
                domain.dispatch("leave_local_api", {})
                domain.apply()
                left = domain.snapshot()
                self.assertFalse(left["uses_local_api"])
                self.assertFalse(left["model_catalog"]["enabled"])
                written, _text = self._written(root)
                self.assertEqual("openai", written["model_provider"])
                self.assertNotIn("model_catalog_json", written)
                domain.dispatch("use_local_api", {"model": "public-a"})
                domain.apply()
                returned = domain.snapshot()
                self.assertTrue(returned["uses_yrapi_api"])
                self.assertEqual(
                    ["public-a", "public-b"],
                    returned["model_catalog"]["public_models"],
                )
                written, _text = self._written(root)
                self.assertEqual("custom", written["model_provider"])
                self.assertEqual(
                    server.url,
                    written["model_providers"]["custom"]["model_catalog_url"],
                )
                self.assertNotIn("model_catalog_json", written)

    def test_adoption_reuses_a_provider_row_already_on_the_proxy(self) -> None:
        """The switch must not accumulate a second local provider on each on.

        A row already naming this proxy (and its catalog) is the one the client
        should keep; a fresh ``custom`` row is only the fallback.
        """

        document = (
            'model = "public-a"\n'
            'model_provider = "openai"\n'
            '\n'
            '[model_providers.newapi]\n'
            'name = "newapi"\n'
            'base_url = "http://127.0.0.1:12390/v1"\n'
            'wire_api = "responses"\n'
            'requires_openai_auth = true\n'
            'model_catalog_url = "http://127.0.0.1:12390/codex/models"\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with _CatalogServer(["public-a", "public-b"]) as server, mock.patch(
                "young_router.core.codex_config.local_exposed_models",
                return_value=(["public-a", "public-b"], True),
            ):
                self._serve(server.url)
                domain = self._domain(root, document=document)
                self._resolve(domain)
                domain.dispatch("use_local_api", {"model": "public-a"})
                domain.apply()
                written, _text = self._written(root)
                self.assertEqual("newapi", written["model_provider"])
                self.assertNotIn("custom", written.get("model_providers", {}))
                self.assertEqual(
                    server.url,
                    written["model_providers"]["newapi"]["model_catalog_url"],
                )
                self.assertEqual("url", domain.snapshot()["model_catalog"]["delivery"])

    def test_a_builtin_provider_reads_the_generated_file(self) -> None:
        """``openai`` has no provider table, so the URL carrier cannot exist.

        The client must fall back to the managed file even while the catalog
        address is answering; writing a URL nowhere would leave the picker on
        Codex's own list.
        """

        document = (
            'model = "public-a"\n'
            'model_provider = "openai"\n'
            'openai_base_url = "http://127.0.0.1:12390/v1"\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = self._domain(root, document=document)
            with _CatalogServer(["public-a", "public-b"]) as server:
                self._serve(server.url)
                self._resolve(domain)
                self._enable(domain, ["public-a", "public-b"])
            written, _text = self._written(root)
            self.assertEqual(str(domain.model_catalog_path), written["model_catalog_json"])
            self.assertNotIn("model_providers", written)
            self.assertTrue(domain.model_catalog_path.exists())
            snapshot = domain.snapshot()
            self.assertEqual(
                {"enabled": True, "public_models": ["public-a", "public-b"], "delivery": "file"},
                snapshot["model_catalog"],
            )

    def test_adopting_the_api_turns_on_the_catalog_on_the_proxy_row(self) -> None:
        """The on switch carries the public list on a row the desktop keeps.

        This is the state that left a real picker on two models: the API was on
        while the catalog was off, and a built-in provider cannot be handed a
        catalog URL.
        """

        document = 'model = "public-a"\nmodel_provider = "openai"\n'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with _CatalogServer(["public-a", "public-b"]) as server, mock.patch(
                "young_router.core.codex_config.local_exposed_models",
                return_value=(["public-a", "public-b"], True),
            ):
                self._serve(server.url)
                domain = self._domain(root, document=document)
                self._resolve(domain)
                domain.dispatch("use_local_api", {"model": "public-a"})
                domain.apply()
                written, _text = self._written(root)
                self.assertEqual("custom", written["model_provider"])
                custom = written["model_providers"]["custom"]
                self.assertIn("127.0.0.1", custom["base_url"])
                self.assertEqual(server.url, custom["model_catalog_url"])
                self.assertNotIn("model_catalog_json", written)
                self.assertFalse(domain.model_catalog_path.exists())
                snapshot = domain.snapshot()
                self.assertTrue(snapshot["uses_local_api"])
                self.assertTrue(snapshot["uses_yrapi_api"])
                self.assertEqual(
                    ["public-a", "public-b"],
                    snapshot["model_catalog"]["public_models"],
                )
                self.assertEqual("url", snapshot["model_catalog"]["delivery"])

    def test_url_delivery_leaves_no_file_next_to_the_client(self) -> None:
        """The body is served from memory; a second copy would be dead weight.

        A catalog file the client never opens is what made an earlier install
        keep loading a list nobody updated, so the switch removes it -- and the
        address still answers with the same models.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = self._domain(root)
            # A file-mode install: the client reads this file today.
            self._serve("http://127.0.0.1:9" + SERVED_CATALOG_ROUTE)
            self._resolve(domain)
            self._enable(domain, ["public-a", "public-b"])
            self.assertTrue(domain.model_catalog_path.exists())

            with _CatalogServer(["public-a", "public-b"]) as server:
                self._serve(server.url)
                domain.refresh_local_api_endpoint()
                domain._catalog_url_checked_at = 0.0
                with mock.patch(
                    "young_router.core.codex_config.local_exposed_models",
                    return_value=(["public-a", "public-b"], True),
                ):
                    domain._catalog_source_checked_at = 0.0
                    domain._ensure_model_catalog_current(force_source_refresh=True)
                document, _text = self._written(root)
                self.assertIn("model_catalog_url", document["model_providers"]["local"])
                self.assertNotIn("model_catalog_json", document)
                self.assertFalse(domain.model_catalog_path.exists())
                status, body = domain._catalog_response()
                self.assertEqual(200, status)
                self.assertEqual(
                    ["public-a", "public-b"],
                    [model["slug"] for model in json.loads(body)["models"]],
                )

    def test_losing_the_address_moves_the_client_back_to_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = self._domain(root)
            with _CatalogServer(["public-a", "public-b"]) as server:
                self._serve(server.url)
                self._resolve(domain)
                self._enable(domain, ["public-a", "public-b"])
                self.assertNotIn("model_catalog_json", self._written(root)[0])
            # The same document, now with the address gone.
            self._serve("http://127.0.0.1:9" + SERVED_CATALOG_ROUTE)
            self._resolve(domain)
            domain._catalog_url_checked_at = 0.0
            self._exposed(domain, ["public-a", "public-b"])
            document, _text = self._written(root)
            self.assertEqual(str(domain.model_catalog_path), document["model_catalog_json"])
            self.assertNotIn("model_catalog_url", document["model_providers"]["local"])

    def test_an_address_answering_a_different_list_is_not_adopted(self) -> None:
        """A stale address must not decide the client's model list."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = self._domain(root)
            with _CatalogServer(["public-a"]) as server:
                self._serve(server.url)
                self._resolve(domain)
                self._enable(domain, ["public-a", "public-b"])
                document, _text = self._written(root)
                self.assertEqual(
                    str(domain.model_catalog_path), document["model_catalog_json"]
                )

    def test_a_direct_gateways_own_listing_path_is_not_adopted(self) -> None:
        """``/v1/models`` on someone else's gateway is theirs, not this app's."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = (
                'model = "direct-chat"\n'
                'model_provider = "direct"\n'
                "\n"
                '[model_providers.direct]\n'
                'name = "direct"\n'
                'base_url = "https://gateway.example.test/v1"\n'
                'wire_api = "responses"\n'
                'model_catalog_url = "https://gateway.example.test/v1/models"\n'
            )
            domain = self._domain(root, document=document)
            self.assertFalse(domain._is_catalog_enabled(domain._raw))

    def test_a_direct_route_catalog_url_belongs_to_the_operator(self) -> None:
        """Leaving the proxy onto a direct row keeps that row's own catalog URL."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = self._domain(root)
            with mock.patch(
                "young_router.core.codex_config.local_exposed_models",
                return_value=(["public-a", "public-b"], True),
            ):
                domain.set_model_catalog_enabled_immediately(True)
                domain.dispatch("use_local_api", {"model": "public-a"})
                domain.apply()
                domain.dispatch(
                    "use_direct_api",
                    {
                        "base_url": "https://gateway.example.test/v1",
                        "model": "direct-chat",
                        "model_catalog_url": "https://gateway.example.test/catalog",
                        "api_key": "fixture-direct",
                        "clear_key": False,
                    },
                )
                domain.apply()
            document, _text = self._written(root)
            provider = document["model_providers"]["young-router-direct"]
            self.assertEqual(
                "https://gateway.example.test/catalog", provider["model_catalog_url"]
            )


def _enable(domain: CodexSettingsDomain, names: list[str]) -> None:
    with mock.patch(
        "young_router.core.codex_config.local_exposed_models",
        return_value=(names, True),
    ):
        domain.set_model_catalog_enabled_immediately(True)


class CatalogRouteRenameTests(unittest.TestCase):
    """A document still naming a retired spelling is rewritten, not abandoned."""

    def test_a_retired_address_is_migrated_to_the_current_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.yaml").write_text(CONFIG, encoding="utf-8")
            home = root / "codex"
            home.mkdir()
            (home / "config.toml").write_text(
                CLIENT_CONFIG.replace(
                    'requires_openai_auth = true\n',
                    'requires_openai_auth = true\n'
                    'model_catalog_url = "http://127.0.0.1:12390/codex/catalog"\n',
                ),
                encoding="utf-8",
            )
            (home / "auth.json").write_text('{"OPENAI_API_KEY": "sk-synthetic-master"}\n', encoding="utf-8")
            domain = CodexSettingsDomain(root / "config.yaml", codex_home=home)
            # The retired spelling still counts as this app's own catalog.
            self.assertTrue(domain._is_catalog_enabled(domain._raw))
            self.assertEqual("url", domain._catalog_delivery(domain._raw))
            with _CatalogServer(["public-a", "public-b"]) as server:
                patcher = mock.patch(
                    "young_router.core.domains._shared.local_catalog_url", return_value=server.url
                )
                patcher.start()
                self.addCleanup(patcher.stop)
                domain.refresh_local_api_endpoint()
                with mock.patch(
                    "young_router.core.codex_config.local_exposed_models",
                    return_value=(["public-a", "public-b"], True),
                ):
                    domain._catalog_source_checked_at = 0.0
                    _enable(domain, ["public-a", "public-b"])
            document = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
            provider = document["model_providers"]["local"]
            self.assertEqual(server.url, provider["model_catalog_url"])
            self.assertNotIn("model_catalog_json", document)

    def test_the_refresh_lane_migrates_a_retired_address_on_its_own(self) -> None:
        """No toggle or Apply: the background pass is what repairs the document."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.yaml").write_text(CONFIG, encoding="utf-8")
            home = root / "codex"
            home.mkdir()
            (home / "config.toml").write_text(
                CLIENT_CONFIG.replace(
                    'requires_openai_auth = true\n',
                    'requires_openai_auth = true\n'
                    'model_catalog_url = "http://127.0.0.1:12390/codex/catalog"\n',
                ),
                encoding="utf-8",
            )
            (home / "auth.json").write_text('{"OPENAI_API_KEY": "sk-synthetic-master"}\n', encoding="utf-8")
            domain = CodexSettingsDomain(root / "config.yaml", codex_home=home)
            with _CatalogServer(["public-a", "public-b"]) as server:
                patcher = mock.patch(
                    "young_router.core.domains._shared.local_catalog_url", return_value=server.url
                )
                patcher.start()
                self.addCleanup(patcher.stop)
                domain.refresh_local_api_endpoint()
                with mock.patch(
                    "young_router.core.codex_config.local_exposed_models",
                    return_value=(["public-a", "public-b"], True),
                ):
                    domain._catalog_source_checked_at = 0.0
                    domain._ensure_model_catalog_current(force_source_refresh=True)
                    domain._catalog_source_checked_at = 0.0
                    domain._ensure_model_catalog_current(force_source_refresh=True)
            document = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(
                server.url, document["model_providers"]["local"]["model_catalog_url"]
            )


class CatalogAddressTests(unittest.TestCase):
    def test_the_address_follows_the_port_the_proxy_reports(self) -> None:
        """``local_port`` answers with text; the address must still be built from it."""

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            "os.environ", {"LITELLM_PORT": "23456"}
        ):
            from young_router.core.domains._shared import local_catalog_url

            self.assertEqual(
                "http://127.0.0.1:23456" + SERVED_CATALOG_ROUTE,
                local_catalog_url(Path(directory) / "config.yaml"),
            )


class PluginFallbackTests(unittest.TestCase):
    """The plugin maintains the catalog only while the host runs it."""

    def _domain(self, root: Path, *, plugin_enabled: bool) -> tuple[CodexSettingsDomain, Path]:
        (root / "config.yaml").write_text(CONFIG, encoding="utf-8")
        home = root / "codex"
        home.mkdir()
        plugin_catalog = home / "model-list-plugin-catalog.json"
        plugin_catalog.write_text('{"models": [{"slug": "stale-only"}]}', encoding="utf-8")
        plugins = (
            '\n[plugins."model-list-plugin@model-list-plugin"]\n'
            f"enabled = {'true' if plugin_enabled else 'false'}\n"
        )
        (home / "config.toml").write_text(
            'model = "public-a"\n'
            'model_provider = "local"\n'
            f'model_catalog_json = "{plugin_catalog}"\n'
            "\n"
            '[model_providers.local]\n'
            'name = "local"\n'
            'base_url = "http://127.0.0.1:12390/v1"\n'
            'wire_api = "responses"\n'
            'requires_openai_auth = true\n' + plugins,
            encoding="utf-8",
        )
        (home / "auth.json").write_text("{}\n", encoding="utf-8")
        domain = CodexSettingsDomain(root / "config.yaml", codex_home=home)
        # No address answers in this test: this is the fallback case.
        self._serve("http://127.0.0.1:9" + SERVED_CATALOG_ROUTE)
        return domain, plugin_catalog

    def _serve(self, url: str) -> None:
        patcher = mock.patch(
            "young_router.core.domains._shared.local_catalog_url", return_value=url
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self._served_url = url

    def _refresh(self, domain: CodexSettingsDomain) -> None:
        with mock.patch(
            "young_router.core.codex_config.local_exposed_models",
            return_value=(["public-a", "public-b"], True),
        ):
            domain._catalog_source_checked_at = 0.0
            domain._ensure_model_catalog_current(force_source_refresh=True)
            domain._catalog_source_checked_at = 0.0
            domain._ensure_model_catalog_current(force_source_refresh=True)

    def test_a_running_plugin_keeps_its_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain, plugin_catalog = self._domain(root, plugin_enabled=True)
            self._refresh(domain)
            self.assertEqual(plugin_catalog, domain._catalog_pointer(domain._raw))

    def test_a_disabled_plugin_cannot_hold_the_client_on_a_stale_file(self) -> None:
        """Its own config still claims the target; the host's switch is the answer."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain, _plugin_catalog = self._domain(root, plugin_enabled=False)
            self._refresh(domain)
            document = tomllib.loads((root / "codex" / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(str(domain.model_catalog_path), document["model_catalog_json"])


class ServedRouteTests(unittest.TestCase):
    """The route Codex hits: one credential, one list, one bound."""

    def _core(self, root: Path) -> tuple[CoreStore, CodexSettingsDomain]:
        (root / "config.yaml").write_text(CONFIG, encoding="utf-8")
        home = root / "codex"
        home.mkdir()
        (home / "config.toml").write_text(CLIENT_CONFIG, encoding="utf-8")
        (home / "auth.json").write_text('{"OPENAI_API_KEY": "sk-synthetic-master"}\n', encoding="utf-8")
        domain = CodexSettingsDomain(root / "config.yaml", codex_home=home)
        return CoreStore(domains=[domain]), domain

    def _expose(self, domain: CodexSettingsDomain, names: list[str]) -> None:
        with mock.patch(
            "young_router.core.codex_config.local_exposed_models",
            return_value=(names, True),
        ):
            domain._catalog_source_checked_at = 0.0
            domain._refresh_live_catalog_source(force=True)

    def test_only_the_models_that_are_both_configured_and_exposed_are_served(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core, domain = self._core(root)
            domain.model_catalog_path.write_text(
                json.dumps({"models": [{"slug": "public-a"}, {"slug": "removed-model"}]}),
                encoding="utf-8",
            )
            self._expose(domain, ["public-a"])
            status, body = core.codex_catalog_response()
            self.assertEqual(200, status)
            self.assertEqual(["public-a"], [model["slug"] for model in json.loads(body)["models"]])

    def test_a_catalog_with_nothing_catalogued_is_a_failure_not_an_empty_list(self) -> None:
        """Codex drops its cached catalog for an empty 200; a failure keeps it."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core, domain = self._core(root)
            status, body = core.codex_catalog_response()
            self.assertEqual(503, status)
            self.assertNotIn("models", json.loads(body))

    def test_the_route_requires_the_router_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core, domain = self._core(root)
            domain.model_catalog_path.write_text(
                json.dumps({"models": [{"slug": "public-a"}]}), encoding="utf-8"
            )
            self._expose(domain, ["public-a"])
            from young_router.core.ipc import CoreIPCServer
            import http.client

            server = CoreIPCServer(core)
            server.start()
            self.addCleanup(server.stop)
            port = server.port

            def call(key: str | None) -> tuple[int, bytes]:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                headers = {"Accept": "application/json"}
                if key is not None:
                    headers["Authorization"] = f"Bearer {key}"
                connection.request("GET", SERVED_CATALOG_ROUTE, headers=headers)
                answer = connection.getresponse()
                payload = answer.read()
                connection.close()
                return answer.status, payload

            self.assertEqual(401, call(None)[0])
            self.assertEqual(401, call("sk-wrong-key")[0])
            status, body = call("sk-synthetic-master")
            self.assertEqual(200, status)
            self.assertEqual(["public-a"], [model["slug"] for model in json.loads(body)["models"]])


class ServedCatalogBodyTests(unittest.TestCase):
    def test_a_reader_gets_indented_json_and_a_client_gets_the_wire_form(self) -> None:
        payload = {"models": [{"slug": "a", "model_messages": {"instructions_template": "x\ny"}}]}
        readable = served_catalog_body(payload, pretty=True)
        wire = served_catalog_body(payload)
        self.assertIn(b"\n  ", readable)
        self.assertTrue(readable.endswith(b"\n"))
        self.assertEqual(payload, json.loads(readable))
        self.assertNotIn(b"\n", wire)
        self.assertLess(len(wire), len(readable))
        self.assertEqual(payload, json.loads(wire))

    def test_an_oversized_catalog_drops_the_prompt_text_before_the_client_sees_it(self) -> None:
        filler = "x" * (600 * 1024)
        payload = {
            "models": [
                {"slug": "a", "base_instructions": filler, "model_messages": {"x": filler}},
                {"slug": "b", "base_instructions": filler, "model_messages": {"x": filler}},
            ]
        }
        body = served_catalog_body(payload)
        self.assertTrue(body)
        self.assertLessEqual(len(body), MAX_SERVED_CATALOG_BYTES)
        document = json.loads(body)
        self.assertEqual(["a", "b"], [model["slug"] for model in document["models"]])
        self.assertNotIn("base_instructions", document["models"][0])
        self.assertNotIn("model_messages", document["models"][0])


class ForwardedPrefixTests(unittest.TestCase):
    def test_the_catalog_path_is_this_apps_and_the_namespace_is_not(self) -> None:
        from young_router.proxy.web_forward import forwards_path

        self.assertTrue(forwards_path(SERVED_CATALOG_ROUTE))
        self.assertTrue(forwards_path(SERVED_CATALOG_ROUTE + "/"))
        self.assertFalse(forwards_path("/codex/modelsx"))
        self.assertFalse(forwards_path("/codex/other"))
        # LiteLLM's own listing path stays LiteLLM's.
        self.assertFalse(forwards_path("/v1/models"))


if __name__ == "__main__":
    unittest.main()
