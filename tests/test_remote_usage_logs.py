from __future__ import annotations

import io
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
        return path

    def test_newapi_logs_are_sanitized_and_do_not_print_path_or_credential(self) -> None:
        path = self.write_config()
        seen: list[str] = []

        def opener(request, timeout):
            seen.append(request.full_url)
            self.assertEqual(request.get_header("Authorization"), "Bearer replace-me")
            return FakeResponse(
                {
                    "success": True,
                    "data": {
                        "items": [
                            {
                                "created_at": "2026-07-18T10:00:00Z",
                                "model_name": "example-model",
                                "status": "success",
                                "total_tokens": 42,
                                "request_body": "SECRET_REQUEST_BODY",
                                "path": "/private/secret.txt",
                            }
                        ]
                    },
                }
            )

        with patch("young_router.core.remote_usage_logs.isolated_http_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = opener
            output = remote_usage_logs.render(path, 1)

        self.assertEqual(seen, ["https://relay.example.test/api/log/token"])
        # The station is named by its own identity, not by the alias the
        # user gave the route: the same words the relay panes show.
        self.assertIn("relay (NewAPI)", output)
        self.assertIn("example-model", output)
        self.assertIn("tokens=42", output)
        self.assertRegex(
            output.splitlines()[0],
            r"^Updated \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
        )
        self.assertNotIn("replace-me", output)
        self.assertNotIn("SECRET_REQUEST_BODY", output)
        self.assertNotIn("/private/secret.txt", output)

    def test_unsupported_endpoints_do_not_expose_remote_error_text(self) -> None:
        path = self.write_config()

        def opener(request, timeout):
            raise HTTPError(request.full_url, 404, "not found", {}, io.BytesIO())

        with patch("young_router.core.remote_usage_logs.isolated_http_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = opener
            output = remote_usage_logs.render(path, 1)

        # A code, not a sentence: the pane states each outcome in the user's
        # language, and an error text from the station never reaches it.
        self.assertEqual(output, remote_usage_logs.USAGE_REASON_UNSUPPORTED)

    def test_gateway_key_log_feed_is_used_directly(self) -> None:
        path = self.write_config()
        seen: list[tuple[str, str | None]] = []

        def opener(request, timeout):
            seen.append((request.full_url, request.get_header("Authorization")))
            self.assertTrue(request.full_url.endswith("/log/token"))
            return FakeResponse(
                {
                    "success": True,
                    "data": [
                        {
                            "created_at": 1784368800,
                            "model_name": "gateway-model",
                            "prompt_tokens": 3,
                            "completion_tokens": 5,
                            "content": "SECRET_RESPONSE_TEXT",
                            "ip": "203.0.113.9",
                        }
                    ],
                }
            )

        with patch("young_router.core.remote_usage_logs.isolated_http_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = opener
            output = remote_usage_logs.render(path, 1)

        self.assertEqual(
            seen,
            [
                ("https://relay.example.test/api/log/token", "Bearer replace-me"),
            ],
        )
        # The station is named by its own identity, not by the alias the
        # user gave the route: the same words the relay panes show.
        self.assertIn("relay (NewAPI)", output)
        self.assertIn("gateway-model", output)
        self.assertIn("tokens=8", output)
        self.assertRegex(output, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z  gateway-model")
        self.assertNotIn("replace-me", output)
        self.assertNotIn("SECRET_RESPONSE_TEXT", output)
        self.assertNotIn("203.0.113.9", output)

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

    def test_sub2api_credential_usage_summary_is_sanitized(self) -> None:
        target = remote_usage_logs.UsageTarget(
            provider="relay",
            api_base="https://relay.example.test/v1",
            api_key="replace-me",
        )
        seen: list[tuple[str, str | None]] = []

        def opener(request, timeout):
            seen.append((request.full_url, request.get_header("Authorization")))
            if request.full_url.endswith("/v1/usage"):
                return FakeResponse(
                    {
                        "mode": "unrestricted",
                        "model_stats": [
                            {
                                "model": "sub2api-model",
                                "input_tokens": 7,
                                "output_tokens": 11,
                                "request_id": "private-request-id",
                                "ip_address": "203.0.113.10",
                            }
                        ],
                    }
                )
            raise HTTPError(request.full_url, 404, "not found", {}, io.BytesIO())

        with patch("young_router.core.remote_usage_logs.isolated_http_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = opener
            source, rows = remote_usage_logs.fetch_target(target, 1)

        self.assertEqual("sub2api", source)
        self.assertEqual(1, len(rows))
        self.assertEqual("sub2api-model", rows[0]["model"])
        self.assertEqual("18", rows[0]["total_tokens"])
        # The row carries only the fields the log view renders: a request id or
        # an address the station also returned must not reach it.
        self.assertNotIn("private-request-id", str(rows[0]))
        self.assertNotIn("203.0.113.10", str(rows[0]))
        self.assertEqual(
            seen,
            [
                ("https://relay.example.test/api/log/token", "Bearer replace-me"),
                ("https://relay.example.test/v1/usage", "Bearer replace-me"),
            ],
        )


    def test_one_station_is_one_target_however_many_keys_it_carries(self) -> None:
        path = self.write_config()

        # Four routes reaching the same control plane are one relay to read.
        sources = remote_usage_logs.usage_sources(path)

        self.assertEqual(1, len(sources))
        target = remote_usage_logs.active_usage_targets(path)[0]
        self.assertEqual(1, len(target.credentials()))

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

        target = remote_usage_logs.active_usage_targets(path)[0]

        self.assertEqual("replace-me-first", target.api_key)
        self.assertEqual(("replace-me-second",), target.fallback_keys)
        self.assertEqual(
            ("replace-me-first", "replace-me-second"),
            target.credentials(),
        )

    def test_a_target_reads_with_its_first_working_credential(self) -> None:
        path = self.write_config()
        target = remote_usage_logs.active_usage_targets(path)[0]
        # The first key cannot read the feed; the second can.
        refused = remote_usage_logs.UsageTarget(
            provider=target.provider,
            api_base=target.api_base,
            api_key="replace-me-first",
            fallback_keys=("replace-me-second",),
        )
        seen: list[str] = []

        def opener(request, timeout):
            authorization = request.get_header("Authorization")
            seen.append(authorization)
            if authorization == "Bearer replace-me-first":
                raise HTTPError(request.full_url, 401, "unauthorized", {}, io.BytesIO())
            return FakeResponse(
                {
                    "success": True,
                    "data": {"items": [{"model_name": "fallback-model", "total_tokens": 5}]},
                }
            )

        with patch("young_router.core.remote_usage_logs.isolated_http_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = opener
            source, records = remote_usage_logs.fetch_target(refused, 1)

        self.assertEqual("newapi", source)
        self.assertEqual("fallback-model", records[0]["model"])
        self.assertIn("Bearer replace-me-second", seen)

    def test_usage_sources_name_each_relay_without_exposing_a_credential(self) -> None:
        path = self.write_config()

        sources = remote_usage_logs.usage_sources(path)

        # The dropdown names the relay by the name the user gave the station,
        # never by its URL and never by the credential.
        self.assertEqual(["relay"], [source["label"] for source in sources])
        self.assertTrue(sources[0]["id"])
        self.assertNotIn("replace-me", sources[0]["id"])

    def test_a_usage_row_carries_its_relays_full_name_and_its_own_fields(self) -> None:
        path = self.write_config()

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
            records, status = remote_usage_logs.collect(path, 1)

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
        self.assertEqual("1.85", record["duration"])
        self.assertEqual("1234", record["cost"])
        # The record is exactly the fields the log view renders.
        self.assertNotIn("203.0.113.9", str(record))
        self.assertNotIn("SECRET_RESPONSE_TEXT", str(record))

    def test_every_key_of_a_station_is_read_not_just_the_first_that_answers(self) -> None:
        path = self.write_config()
        target = remote_usage_logs.active_usage_targets(path)[0]
        multi = remote_usage_logs.UsageTarget(
            provider=target.provider,
            api_base=target.api_base,
            api_key="replace-me-quiet",
            fallback_keys=("replace-me-busy",),
        )
        seen: list[str] = []

        def opener(request, timeout):
            authorization = request.get_header("Authorization")
            seen.append(authorization)
            # The first key is valid and answers zero rows; the second is the
            # one that served the traffic. Stopping at the first would report
            # "no usage" for a station that was actively serving requests.
            if authorization == "Bearer replace-me-quiet":
                return FakeResponse({"success": True, "data": {"items": []}})
            return FakeResponse(
                {
                    "success": True,
                    "data": {
                        "items": [
                            {"model_name": "busy-model", "total_tokens": 9, "token_name": "busy"}
                        ]
                    },
                }
            )

        with patch("young_router.core.remote_usage_logs.isolated_http_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = opener
            source, records = remote_usage_logs.fetch_target(multi, 1)

        self.assertEqual("newapi", source)
        self.assertEqual(["busy-model"], [record["model"] for record in records])
        self.assertIn("Bearer replace-me-busy", seen)

    def test_a_row_reported_through_two_keys_is_counted_once(self) -> None:
        path = self.write_config()
        target = remote_usage_logs.active_usage_targets(path)[0]
        shared = remote_usage_logs.UsageTarget(
            provider=target.provider,
            api_base=target.api_base,
            api_key="replace-me-one",
            fallback_keys=("replace-me-two",),
        )
        row = {
            "created_at": "2026-07-18T10:00:00Z",
            "model_name": "same-model",
            "total_tokens": 7,
            "token_name": "same",
        }

        def opener(request, timeout):
            return FakeResponse({"success": True, "data": {"items": [dict(row)]}})

        with patch("young_router.core.remote_usage_logs.isolated_http_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = opener
            _source, records = remote_usage_logs.fetch_target(shared, 1)

        self.assertEqual(1, len(records))

    def test_a_large_but_valid_log_page_is_not_rejected_as_unsupported(self) -> None:
        path = self.write_config()
        target = remote_usage_logs.active_usage_targets(path)[0]
        # A station's own page size is often a thousand rows, which is a
        # several-hundred-kilobyte document. A cap below that rejected the
        # busiest keys as if their endpoint were unsupported.
        items = [
            {
                "model_name": f"model-{index}",
                "total_tokens": index,
                "token_name": "k",
                "created_at": index,
            }
            for index in range(1000)
        ]

        def opener(request, timeout):
            return FakeResponse({"success": True, "data": {"items": items}})

        with patch("young_router.core.remote_usage_logs.isolated_http_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = opener
            source, records = remote_usage_logs.fetch_target(target, 1)

        self.assertEqual("newapi", source)
        self.assertEqual(1000, len(records))

    def test_a_scoped_collect_reads_only_the_named_relay(self) -> None:
        path = self.write_config()
        target = remote_usage_logs.active_usage_targets(path)[0]
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
        path = self.write_config()
        seen: list[str] = []

        def opener(request, timeout):
            seen.append(request.full_url)
            return FakeResponse({"success": True, "data": {"items": []}})

        with patch("young_router.core.remote_usage_logs.isolated_http_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = opener
            output = remote_usage_logs.render(path, 1, "0" * 16)

        self.assertEqual([], seen)
        self.assertEqual(remote_usage_logs.USAGE_REASON_UNKNOWN_SOURCE, output)


if __name__ == "__main__":
    unittest.main()
