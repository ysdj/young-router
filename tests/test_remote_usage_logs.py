from __future__ import annotations

import io
import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))
from young_router.core import remote_usage_logs  # noqa: E402


class FakeResponse:
    def __init__(self, payload: object) -> None:
        import json

        self.body = json.dumps(payload).encode("utf-8")
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def getcode(self) -> int:
        return self.status

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]


class RemoteUsageLogsTests(unittest.TestCase):
    def setUp(self) -> None:
        # A minted dashboard token is cached in memory per account, so one
        # test's token must never authorize another test's read.
        remote_usage_logs._TOKEN_CACHE.clear()
        remote_usage_logs._REFRESH_CACHE.clear()
        remote_usage_logs.set_rotated_refresh_sink(None)
        self.addCleanup(remote_usage_logs._TOKEN_CACHE.clear)
        self.addCleanup(remote_usage_logs._REFRESH_CACHE.clear)
        self.addCleanup(remote_usage_logs.set_rotated_refresh_sink, None)

    @staticmethod
    def write_relay_station(path: Path, origin: str, station_type: str = "newapi", account_id: str = "") -> None:
        """Register a relay station the way the relay panes record one.

        The usage tab lists relay _stations_, and the relay panes' own document
        is what records them.  A provider with no entry there is a plain
        provider pointed at a vendor's API, which is what the reader must not
        offer.
        """

        document = Path(path).parent / ".litellm-runtime" / "relay-accounts.json"
        document.parent.mkdir(parents=True, exist_ok=True)
        station_id = "station-test"
        document.write_text(
            json.dumps(
                {
                    "version": 3,
                    "stations": [
                        {
                            "id": station_id,
                            "name": "relay",
                            "origin": origin,
                            "type": station_type,
                        }
                    ],
                    "accounts": (
                        [
                            {
                                "id": account_id,
                                "station_id": station_id,
                                "type": station_type,
                                "label": "tester",
                                "origin": origin,
                                "username": "tester@example.test",
                                "session": {"access_token": "session-token"},
                                "resources": [],
                            }
                        ]
                        if account_id
                        else []
                    ),
                    "pending_credential_cleanups": [],
                }
            ),
            encoding="utf-8",
        )

    def write_signed_in_config(self) -> Path:
        """A relay the app holds a dashboard login for.

        A usage read goes through the account's own dashboard session — the
        per-key feed beside it answers only that key's traffic — so a fixture
        that exercises a read has to record a login, exactly as the relay panes
        do when the user signs in.
        """

        path = self.write_config()
        self.write_relay_station(path, "https://relay.example.test", account_id="login-test")
        return path

    def write_config(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.yaml"
        path.write_text(
            textwrap.dedent(
                """
                providers:
                  relay:
                    api_base: https://relay.example.test/v1
                    api_keys:
                      - name: default
                        value: replace-me
                model_list:
                  - model_name: default-chat
                    litellm_params:
                      model: openai/default-chat
                      api_base: https://relay.example.test/v1
                    model_info:
                      id: a1b2c3d4
                      provider: relay
                      api_key_name: default
                      upstream_url_surface: openai/responses
                      supported_upstream_url_surfaces: [openai/responses]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        self.write_relay_station(path, "https://relay.example.test")
        return path

    def write_two_account_config(self) -> Path:
        """One station reached through two dashboard logins."""

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.yaml"
        path.write_text(
            textwrap.dedent(
                """
                providers:
                  relay:
                    api_base: https://relay.example.test/v1
                    api_keys:
                      - id: slot-a
                        name: a
                        value: replace-me-a
                      - id: slot-b
                        name: b
                        value: replace-me-b
                    x-young-router-relay-keys:
                      version: 1
                      slots:
                        - id: slot-a
                          api_key_name: a
                          source:
                            kind: relay
                            station_id: station-one
                            account_id: login-a
                            resource_id: newapi-1
                        - id: slot-b
                          api_key_name: b
                          source:
                            kind: relay
                            station_id: station-one
                            account_id: login-b
                            resource_id: newapi-2
                model_list:
                  - model_name: one
                    litellm_params:
                      model: openai/one
                      api_base: https://relay.example.test/v1
                    model_info:
                      id: a1b2c3d4
                      provider: relay
                      api_key_name: a
                      x-young-router-provider-key-id: slot-a
                  - model_name: two
                    litellm_params:
                      model: openai/two
                      api_base: https://relay.example.test/v1
                    model_info:
                      id: e5f6a7b8
                      provider: relay
                      api_key_name: b
                      x-young-router-provider-key-id: slot-b
                """
            ).lstrip(),
            encoding="utf-8",
        )
        self.write_relay_station(path, "https://relay.example.test")
        return path

    def test_a_route_reads_usage_with_the_slot_it_names(self) -> None:
        """The slot decides, and a slot the provider does not carry reads nothing.

        Falling back to the provider's `default` key (or its only key) for a route
        that named another one presents that account's usage as this route's; the
        slot id the route carries is the answer.
        """

        provider = {
            "api_base": "https://relay.example.test/v1",
            "api_keys": [
                {"id": "slot-default", "name": "default", "value": "replace-default"},
                {"id": "slot-bob", "name": "bob", "value": "replace-bob"},
            ],
        }
        self.assertEqual(
            "replace-bob",
            remote_usage_logs._credential_for_model(
                provider, {"api_key_name": "bob", "provider_key_id": "slot-bob"}
            ),
        )
        # A stale name does not decide once a slot is stated.
        self.assertEqual(
            "replace-default",
            remote_usage_logs._credential_for_model(
                provider, {"api_key_name": "bob", "provider_key_id": "slot-default"}
            ),
        )
        self.assertEqual(
            "",
            remote_usage_logs._credential_for_model(
                provider,
                {"api_key_name": "bob", "provider_key_id": "provider-slot-00000000000000000000000000000000"},
            ),
        )
        self.assertEqual(
            "",
            remote_usage_logs._credential_for_model(provider, {"api_key_name": "gone"}),
        )
        # A route that names no key still follows the provider's own default.
        self.assertEqual(
            "replace-default",
            remote_usage_logs._credential_for_model(provider, {}),
        )

    def test_keys_of_one_account_are_one_target(self) -> None:
        path = self.write_config()

        # Four routes reaching the same control plane are one relay to read.
        sources = remote_usage_logs.usage_sources(path)

        self.assertEqual(1, len(sources))
        target = remote_usage_logs.active_usage_targets(path)[0]
        self.assertEqual(1, len(target.credentials()))

    def test_one_station_with_two_accounts_is_two_targets(self) -> None:
        """A station's logins are separate read scopes, not one history.

        Each dashboard login answers only the traffic its own keys served, so a
        target per station merged two people's usage under one name and could
        never read the second login at all.
        """

        path = self.write_two_account_config()

        targets = remote_usage_logs.active_usage_targets(path)

        self.assertEqual(2, len(targets))
        # Each target reads only its own account's credential, and the two
        # handles the dropdown hands back are distinct.
        self.assertEqual(
            [("login-a", "replace-me-a"), ("login-b", "replace-me-b")],
            [(target.account_id, target.api_key) for target in targets],
        )
        self.assertNotEqual(targets[0].source_id, targets[1].source_id)
        # The station stays the relay's name; the account is what the dropdown
        # entry adds, so the source column still reads as the relay.
        self.assertEqual("relay", targets[0].provider)
        self.assertIn("login-a", targets[0].display())

    def test_a_stations_other_credentials_are_kept_as_fallbacks(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.yaml"
        path.write_text(
            textwrap.dedent(
                """
                providers:
                  relay:
                    api_base: https://relay.example.test/v1
                    api_keys:
                      - name: first
                        value: replace-me-first
                      - name: second
                        value: replace-me-second
                model_list:
                  - model_name: one
                    litellm_params:
                      model: openai/one
                      api_base: https://relay.example.test/v1
                    model_info:
                      id: a1b2c3d4
                      provider: relay
                      api_key_name: first
                  - model_name: two
                    litellm_params:
                      model: openai/two
                      api_base: https://relay.example.test/v1
                    model_info:
                      id: e5f6a7b8
                      provider: relay
                      api_key_name: second
                """
            ).lstrip(),
            encoding="utf-8",
        )
        self.write_relay_station(path, "https://relay.example.test")

        target = remote_usage_logs.active_usage_targets(path)[0]

        self.assertEqual("replace-me-first", target.api_key)
        # Both keys were pasted by hand, so neither belongs to a relay account:
        # they stay one target's credentials instead of one target per key.
        self.assertEqual("", target.account_id)
        self.assertEqual(("replace-me-second",), target.fallback_keys)
        self.assertEqual(
            ("replace-me-first", "replace-me-second"),
            target.credentials(),
        )

    def test_usage_sources_name_each_relay_without_exposing_a_credential(self) -> None:
        path = self.write_config()

        sources = remote_usage_logs.usage_sources(path)

        # The dropdown names the relay by the name the user gave the station,
        # never by its URL and never by the credential.
        self.assertEqual(["relay"], [source["label"] for source in sources])
        self.assertTrue(sources[0]["id"])
        self.assertNotIn("replace-me", sources[0]["id"])

    def test_a_usage_row_carries_its_relays_full_name_and_its_own_fields(self) -> None:
        path = self.write_signed_in_config()
        target = next(
            t for t in remote_usage_logs.active_usage_targets(path) if t.session_headers
        )

        def opener(request, timeout):
            return FakeResponse(
                {
                    "success": True,
                    "data": {
                        "items": [
                            {
                                "created_at": "2026-07-18T10:00:00Z",
                                "model_name": "row-model",
                                "status": "success",
                                "prompt_tokens": 100,
                                "completion_tokens": 200,
                                "total_tokens": 300,
                                "channel_name": "channel-one",
                                "group_name": "group-one",
                                "token_name": "prod-key",
                                "use_time": 1.85,
                                "quota": 1234,
                                # Neither of these belongs in a rendered row.
                                "ip": "203.0.113.9",
                                "content": "SECRET_RESPONSE_TEXT",
                            }
                        ]
                    },
                }
            )

        with patch("young_router.core.remote_usage_logs.isolated_http_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = opener
            _kind, records, status, _total, _applied = remote_usage_logs.fetch_target(target, 1)

        self.assertEqual("", status)
        self.assertEqual(1, len(records))
        record = records[0]
        # The relay is named by its address and the family that answered, which
        # is the identity the relay panes and the station's own pages carry.
        # The relay's name and the control plane that answered are two facts:
        # the name is the source column and the family is its own type column.
        self.assertEqual("relay", record["source_label"])
        self.assertEqual("NewAPI", record["source_family"])
        self.assertEqual("2026-07-18T10:00:00Z", record["timestamp"])
        self.assertEqual("row-model", record["model"])
        self.assertEqual("success", record["status"])
        self.assertEqual("300", record["total_tokens"])
        self.assertEqual("100", record["prompt_tokens"])
        self.assertEqual("200", record["completion_tokens"])
        self.assertEqual("channel-one", record["channel"])
        self.assertEqual("group-one", record["group"])
        self.assertEqual("prod-key", record["key_name"])
        # `use_time` is seconds; the record carries milliseconds so every family
        # lands on one unit and the column's own `(s)` label is applied once.
        self.assertEqual("1850", record["duration"])
        self.assertEqual("1234", record["cost"])
        # The record is exactly the fields the log view renders.
        self.assertNotIn("203.0.113.9", str(record))
        self.assertNotIn("SECRET_RESPONSE_TEXT", str(record))

    def test_a_scoped_collect_reads_only_the_named_relay(self) -> None:
        path = self.write_signed_in_config()
        # The account's own target is the one that carries a dashboard login; the
        # provider-derived entry has none and is not readable.
        target = next(
            t for t in remote_usage_logs.active_usage_targets(path) if t.session_headers
        )
        seen: list[str] = []

        def opener(request, timeout):
            seen.append(request.full_url)
            return FakeResponse(
                {
                    "success": True,
                    "data": {"items": [{"model_name": "scoped-model", "total_tokens": 7}]},
                }
            )

        with patch("young_router.core.remote_usage_logs.isolated_http_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = opener
            output = remote_usage_logs.render(path, 1, target.source_id)

        self.assertIn("scoped-model", output)
        self.assertTrue(seen)

    def test_an_unknown_source_reads_no_relay_at_all(self) -> None:
        path = self.write_signed_in_config()
        seen: list[str] = []

        def opener(request, timeout):
            seen.append(request.full_url)
            return FakeResponse({"success": True, "data": {"items": []}})

        with patch("young_router.core.remote_usage_logs.isolated_http_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = opener
            output = remote_usage_logs.render(path, 1, "0" * 16)

        self.assertEqual([], seen)
        self.assertEqual(remote_usage_logs.USAGE_REASON_UNKNOWN_SOURCE, output)

    def _session_target(self, provider: str = "relay", station_type: str = "newapi") -> object:
        """A relay account the app holds a dashboard login for."""

        return remote_usage_logs.UsageTarget(
            provider=provider,
            api_base="https://relay.example.test/v1",
            api_key="replace-me",
            account="tester",
            account_id="login-tester",
            station_type=station_type,
            session_headers=(("Authorization", "Bearer session-token"),),
        )

    def test_newapi_usage_is_read_through_the_accounts_own_feed(self) -> None:
        """The dashboard's endpoint, one request, and nothing private leaks.

        ``/log/self`` answers the login's whole history.  The per-key feed beside
        it answers only the traffic that key served, so it is never asked.
        """

        target = self._session_target()
        seen: list[str] = []

        def opener(request, timeout):
            seen.append(request.full_url)
            self.assertEqual(request.get_header("Authorization"), "Bearer session-token")
            return FakeResponse(
                {
                    "success": True,
                    "data": {
                        "page": 1,
                        "page_size": 100,
                        "total": 4242,
                        "items": [
                            {
                                "created_at": 1792867428,
                                "model_name": "example-model",
                                "use_time": 8,
                                "prompt_tokens": 12,
                                "completion_tokens": 3,
                                "group": {"id": 7, "name": "混合分组"},
                                "token_name": "secret-key-name",
                                "ip": "203.0.113.9",
                                "content": "SECRET_REQUEST_BODY",
                            }
                        ],
                    },
                }
            )

        with patch("young_router.core.remote_usage_logs.isolated_http_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = opener
            kind, records, reason, total, applied = remote_usage_logs.fetch_target(target, 1)

        self.assertEqual(1, len(seen))
        self.assertIn("/api/log/self?", seen[0])
        self.assertNotIn("/log/token", seen[0])
        self.assertEqual("newapi", kind)
        self.assertEqual("", reason)
        self.assertEqual(4242, total)
        self.assertEqual(100, applied)
        self.assertEqual("example-model", records[0]["model"])
        self.assertEqual("混合分组", records[0]["group"])
        self.assertEqual("8000", records[0]["duration"])
        flat = json.dumps(records)
        self.assertNotIn("SECRET_REQUEST_BODY", flat)
        self.assertNotIn("203.0.113.9", flat)
        self.assertNotIn("session-token", flat)

    def test_a_duration_keeps_its_units_across_both_station_families(self) -> None:
        """Seconds and milliseconds must not be read as one unit.

        A New API log states `use_time` in seconds; a Sub2API ledger states
        `duration_ms` in milliseconds.  Treating both as milliseconds rendered
        every New API row as `0`.
        """

        self.assertEqual(
            {"model": "m", "duration": "8000"},
            remote_usage_logs._row_details({"use_time": 8, "model": "m"}),
        )
        self.assertEqual(
            {"model": "m", "duration": "3159"},
            remote_usage_logs._row_details({"duration_ms": 3159, "model": "m"}),
        )

    def test_a_usage_read_never_uses_a_gateway_key(self) -> None:
        """No session, no history: a key's own feed is not the account's.

        One station's six keys answered the same request with 42, 53, 447 and
        1000 rows and newest rows 32 hours apart.  Reading them one by one and
        merging was slower than the pane could wait and still showed a partial
        history as the account's, so a relay with no dashboard login now states
        that instead.
        """

        target = remote_usage_logs.UsageTarget(
            provider="relay",
            api_base="https://relay.example.test/v1",
            api_key="replace-me",
            account="tester",
            account_id="login-tester",
            station_type="newapi",
        )
        seen: list[str] = []

        def opener(request, timeout):
            seen.append(request.full_url)
            return FakeResponse({"success": True, "data": {"items": []}})

        with patch("young_router.core.remote_usage_logs.isolated_http_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = opener
            kind, records, reason, _total, _applied = remote_usage_logs.fetch_target(target, 1)

        self.assertEqual([], seen)
        self.assertEqual("", kind)
        self.assertEqual([], records)
        self.assertEqual(remote_usage_logs.USAGE_REASON_NO_CREDENTIAL, reason)

    def test_a_newapi_page_turn_is_one_request(self) -> None:
        """One page, one request, and the feed's own numbering.

        The feed answered page one for both ``p=0`` and ``p=1``, so the reader
        sends its one-based page directly.  Walking the history before answering
        cost 146 requests and 118 seconds for a single page turn.
        """

        target = self._session_target()
        seen: list[str] = []

        def opener(request, timeout):
            seen.append(request.full_url)
            return FakeResponse(
                {
                    "success": True,
                    "data": {
                        "page": 2,
                        "page_size": 100,
                        "total": 4242,
                        "items": [
                            {"created_at": 1792867000 + index, "model_name": f"m{index}"}
                            for index in range(100)
                        ],
                    },
                }
            )

        with patch("young_router.core.remote_usage_logs.isolated_http_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = opener
            kind, records, reason, total, applied = remote_usage_logs.fetch_target(
                target, 1, page=2, rows_per_page=300
            )

        self.assertEqual(1, len(seen))
        self.assertIn("p=2", seen[0])
        self.assertIn("page_size=300", seen[0])
        self.assertEqual(100, len(records))
        self.assertEqual((4242, 100), (total, applied))
        self.assertEqual("", reason)

    def test_a_refused_usage_login_is_its_own_reason(self) -> None:
        target = self._session_target()

        def opener(request, timeout):
            raise HTTPError(request.full_url, 401, "unauthorized", {}, io.BytesIO())

        with patch("young_router.core.remote_usage_logs.isolated_http_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = opener
            _kind, records, reason, _total, _applied = remote_usage_logs.fetch_target(target, 1)

        self.assertEqual([], records)
        self.assertEqual(remote_usage_logs.USAGE_REASON_LOGIN_EXPIRED, reason)

    def test_a_sub2api_account_reads_its_per_request_ledger_not_the_summary(self) -> None:
        """The ledger is the only shape that can fill a log table.

        The compatible ``/v1/usage`` document answers one always-current row per
        model, so a station with four models showed four rows whatever its
        history held, and no time filter could select anything.  The dashboard
        ledger holds one row per request with its own timestamp, which is what
        the columns and the range filter actually read.
        """

        target = remote_usage_logs.UsageTarget(
            provider="relay",
            api_base="https://relay.example.test/v1",
            api_key="replace-me",
            account="someone",
            account_id="login-one",
            station_type="sub2api",
            session_headers=(("Authorization", "Bearer session-token"),),
            refresh_token="rt_ledger",
        )
        seen: list[str] = []

        def opener(request, timeout):
            seen.append(request.full_url)
            if "/api/v1/auth/refresh" in request.full_url:
                return FakeResponse(
                    {"code": 0, "data": {"access_token": "live-token", "expires_in": 3600}}
                )
            if "/api/v1/usage" in request.full_url:
                return FakeResponse(
                    {
                        "code": 0,
                        "data": {
                            "page": 1,
                            "pages": 1,
                            "page_size": 200,
                            "total": 3,
                            "items": [
                                {
                                    "model": f"ledger-model-{index}",
                                    "input_tokens": 10 + index,
                                    "output_tokens": 2,
                                    "created_at": f"2026-10-0{index + 1}T10:00:00Z",
                                }
                                for index in range(3)
                            ],
                        },
                    }
                )
            raise HTTPError(request.full_url, 404, "not found", {}, io.BytesIO())

        with patch("young_router.core.remote_usage_logs.isolated_http_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = opener
            kind, records, _reason, _total, _applied = remote_usage_logs.fetch_target(target, 1)

        self.assertEqual("sub2api", kind)
        self.assertEqual(
            ["ledger-model-0", "ledger-model-1", "ledger-model-2"],
            [record["model"] for record in records],
        )
        # Three distinct request instants, not one stamp shared by every row.
        self.assertEqual(
            ["2026-10-01T10:00:00Z", "2026-10-02T10:00:00Z", "2026-10-03T10:00:00Z"],
            [record["timestamp"] for record in records],
        )
        self.assertIn("/api/v1/usage", seen[0])
        # The station orders the ledger server-side, so the reader sends the
        # same ordering its dashboard sends instead of taking an arbitrary
        # window of the account's history.
        self.assertIn("sort_by=created_at", seen[0])
        self.assertIn("sort_order=desc", seen[0])

    def test_a_refused_sub2api_ledger_never_falls_back_to_the_per_model_summary(self) -> None:
        """A signed-out dashboard has no log feed, and the summary is not one.

        ``/v1/usage`` answers one always-current row per model stamped with the
        moment of the read.  When the dashboard login that reads the ledger had
        expired, the reader used to fall through to that document, so the pane
        showed as many rows as the account had models, all sharing one
        timestamp, and read as a complete up-to-date history: nine models
        rendered as "9 rows" no matter how much traffic the account had served.
        A refused ledger is therefore an empty read, not a summary one.
        """

        target = remote_usage_logs.UsageTarget(
            provider="relay",
            api_base="https://relay.example.test/v1",
            api_key="replace-me",
            account="someone",
            account_id="login-one",
            station_type="sub2api",
            session_headers=(("Authorization", "Bearer expired-token"),),
            refresh_token="rt_spent",
        )
        seen: list[str] = []

        def opener(request, timeout):
            seen.append(request.full_url)
            if "/api/v1/auth/refresh" in request.full_url:
                # The refresh token is spent too, so no live token can be
                # minted and the ledger is never even asked.
                raise HTTPError(request.full_url, 401, "unauthorized", {}, io.BytesIO())
            if "/api/v1/usage" in request.full_url:
                raise HTTPError(request.full_url, 401, "unauthorized", {}, io.BytesIO())
            # The summary document is reachable and would answer nine models.
            return FakeResponse(
                {
                    "success": True,
                    "data": {
                        "model_stats": [
                            {"model": f"summary-model-{index}", "total_tokens": 100 + index}
                            for index in range(9)
                        ]
                    },
                }
            )

        with patch("young_router.core.remote_usage_logs.isolated_http_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = opener
            kind, records, reason, _total, _applied = remote_usage_logs.fetch_target(target, 1)

        self.assertEqual("", kind)
        self.assertEqual([], records)
        # A spent login is its own reported outcome, not "no rows" and not
        # "this station installs no feed": signing in again is the fix.
        self.assertEqual(remote_usage_logs.USAGE_REASON_LOGIN_EXPIRED, reason)
        # The summary document was never asked, so it could not answer rows.
        # The per-model summary lives at `<root>/v1/usage`; the ledger is the
        # different path `/api/v1/usage`.  Only the ledger may be asked.
        self.assertFalse(
            [
                url
                for url in seen
                if "/v1/usage" in url and "/api/v1/usage" not in url
            ],
            "the per-model summary must never be read as a log feed",
        )

    def test_a_usage_read_mints_a_token_from_the_refresh_token(self) -> None:
        """A refused stored token is replaced through the refresh token.

        A spent dashboard token is the signal to exchange the refresh token, and
        the exchange rotates it: the successor is handed to the store, because a
        refresh token kept only in memory dies with the process and the account
        would then have to be signed in again.
        """

        target = remote_usage_logs.UsageTarget(
            provider="relay",
            api_base="https://relay.example.test/v1",
            api_key="replace-me",
            account="someone",
            account_id="login-fresh",
            station_type="sub2api",
            session_headers=(("Authorization", "Bearer stale-token"),),
            refresh_token="rt_current",
        )
        calls: list[tuple[str, str]] = []

        def opener(request, timeout):
            calls.append((request.full_url, request.get_header("Authorization") or ""))
            if "/api/v1/auth/refresh" in request.full_url:
                self.assertEqual("rt_current", json.loads(request.data)["refresh_token"])
                return FakeResponse(
                    {
                        "code": 0,
                        "data": {
                            "access_token": "fresh-token",
                            "refresh_token": "rt_rotated",
                            "expires_in": 172800,
                            "token_type": "Bearer",
                        },
                    }
                )
            # The station refuses the token the login still stores, which is
            # what a spent dashboard token answers.
            if request.get_header("Authorization") == "Bearer stale-token":
                raise HTTPError(request.full_url, 401, "unauthorized", {}, io.BytesIO())
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "page": 1,
                        "pages": 1,
                        "page_size": 200,
                        "total": 1,
                        "items": [
                            {
                                "model": "ledger-model",
                                "input_tokens": 10,
                                "output_tokens": 2,
                                "created_at": "2026-10-01T10:00:00Z",
                            }
                        ],
                    },
                }
            )

        rotated: list[tuple[str, str]] = []
        remote_usage_logs.set_rotated_refresh_sink(lambda account_id, token: rotated.append((account_id, token)))
        try:
            with patch("young_router.core.remote_usage_logs.isolated_http_opener") as opener_factory:
                opener_factory.return_value.open.side_effect = opener
                kind, records, reason, _total, _applied = remote_usage_logs.fetch_target(target, 1)
        finally:
            remote_usage_logs.set_rotated_refresh_sink(None)

        self.assertEqual("sub2api", kind)
        self.assertEqual("", reason)
        self.assertEqual(["ledger-model"], [record["model"] for record in records])
        # A token with no readable expiry is offered to the station first: a
        # dashboard session routinely outlives the pane, and minting a
        # replacement for a token that still works costs a session every read.
        ledger_calls = [headers for url, headers in calls if "/api/v1/usage" in url]
        # The stored token is offered first, refused, and only then replaced.
        self.assertEqual(["Bearer stale-token", "Bearer fresh-token"], ledger_calls)
        # It was refused, so exactly one renewal happened — and it did happen,
        # because a refusal is the one signal that the token is really spent.
        self.assertEqual(
            1,
            len([url for url, _ in calls if "/api/v1/auth/refresh" in url]),
        )
        # The rotated refresh token is handed to the store that can keep it,
        # because a refresh token kept only in memory dies with the process.
        self.assertEqual([("login-fresh", "rt_rotated")], rotated)

    def test_a_live_stored_token_is_never_refreshed(self) -> None:
        """A working dashboard token is used as it stands.

        Every refresh is a session at the station, and stations cap concurrent
        sessions.  A token whose own `exp` says it is live therefore authorizes
        the read directly: no exchange, no sign-in, no session.
        """

        import base64 as _b64
        import time as _time

        claims = _b64.urlsafe_b64encode(
            json.dumps({"exp": int(_time.time()) + 3600}).encode()
        ).decode().rstrip("=")
        live = f"header.{claims}.signature"

        target = remote_usage_logs.UsageTarget(
            provider="relay",
            api_base="https://relay.example.test/v1",
            api_key="replace-me",
            account="someone",
            account_id="login-live",
            station_type="sub2api",
            session_headers=(("Authorization", f"Bearer {live}"),),
            refresh_token="rt_live",
        )
        calls: list[str] = []

        def opener(request, timeout):
            calls.append(request.full_url)
            if "/api/v1/auth/refresh" in request.full_url:
                raise AssertionError("a live token must not be refreshed")
            return FakeResponse(
                {
                    "code": 0,
                    "data": {"page": 1, "pages": 1, "page_size": 200, "total": 0, "items": []},
                }
            )

        with patch("young_router.core.remote_usage_logs.isolated_http_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = opener
            kind, records, reason, _total, _applied = remote_usage_logs.fetch_target(target, 1)

        self.assertEqual("sub2api", kind)
        self.assertEqual([], records)
        self.assertEqual("", reason)
        # Exactly one request: the ledger, with the token the login already had.
        self.assertEqual(1, len(calls))
        self.assertIn("/api/v1/usage", calls[0])

    def test_an_empty_ledger_says_no_rows_not_unsupported(self) -> None:
        """An account that has served nothing is not a station without a feed.

        The two read the same in the table — nothing to list — but only one of
        them is something the user can act on, so the pane must be told which.
        """

        target = remote_usage_logs.UsageTarget(
            provider="relay",
            api_base="https://relay.example.test/v1",
            api_key="replace-me",
            account="someone",
            account_id="login-empty",
            station_type="sub2api",
            session_headers=(("Authorization", "Bearer session-token"),),
            refresh_token="rt_empty-ledger",
        )

        def opener(request, timeout):
            if "/api/v1/auth/refresh" in request.full_url:
                return FakeResponse(
                    {"code": 0, "data": {"access_token": "live-token", "expires_in": 3600}}
                )
            if "/api/v1/usage" in request.full_url:
                return FakeResponse(
                    {"code": 0, "data": {"items": [], "total": 0, "page": 1, "pages": 1}}
                )
            raise HTTPError(request.full_url, 404, "not found", {}, io.BytesIO())

        with patch("young_router.core.remote_usage_logs.isolated_http_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = opener
            kind, rows, reason, _total, _applied = remote_usage_logs.fetch_target(target, 1)

        self.assertEqual([], rows)
        # The station answered and has served nothing: that is its own state,
        # not a login that stopped working.
        self.assertEqual("sub2api", kind)
        self.assertEqual("", reason)

    def test_a_provider_that_is_not_a_relay_is_never_offered(self) -> None:
        """The dropdown lists relay stations, not every provider.

        A provider pointed straight at an upstream vendor (a hand-pasted key on
        `api.deepseek.com`) has no dashboard, so there is nothing this reader
        could ask it.  Listing it offered a choice whose every answer was
        "unsupported", and it crowded the relays the user actually meant.
        """

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        path = root / "config.yaml"
        path.write_text(
            textwrap.dedent(
                """
                providers:
                  relay:
                    api_base: https://relay.example.test/v1
                    api_keys:
                      - name: default
                        value: replace-me-relay
                  vendor:
                    api_base: https://api.vendor.example.test/v1
                    api_keys:
                      - name: default
                        value: replace-me-vendor
                model_list:
                  - model_name: relay-chat
                    litellm_params:
                      model: openai/relay-chat
                      api_base: https://relay.example.test/v1
                    model_info:
                      id: a1b2c3d4
                      provider: relay
                      api_key_name: default
                  - model_name: vendor-chat
                    litellm_params:
                      model: openai/vendor-chat
                      api_base: https://api.vendor.example.test/v1
                    model_info:
                      id: e5f6a7b8
                      provider: vendor
                      api_key_name: default
                """
            ).lstrip(),
            encoding="utf-8",
        )
        # Only the relay is a station; the vendor provider is not.
        relay_doc = root / ".litellm-runtime" / "relay-accounts.json"
        relay_doc.parent.mkdir(parents=True, exist_ok=True)
        relay_doc.write_text(
            json.dumps(
                {
                    "version": 3,
                    "stations": [
                        {
                            "id": "station-relay",
                            "name": "relay",
                            "origin": "https://relay.example.test",
                            "type": "newapi",
                        }
                    ],
                    "accounts": [],
                    "pending_credential_cleanups": [],
                }
            ),
            encoding="utf-8",
        )

        sources = remote_usage_logs.usage_sources(path)

        self.assertEqual(["relay"], [source["label"] for source in sources])


if __name__ == "__main__":
    unittest.main()
