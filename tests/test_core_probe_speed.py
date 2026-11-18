"""The 探测 button's speed reading, and the scan it replaces.

The reading is its own row — the press states it, and *any* route can be asked
for it: the menu row is the statement of intent, so no model name decides
whether the question may be asked.  The streamed turn that produces the number
is also the evidence that the surface serves it, so asking that same surface
again under 「连通性」 would make one press pay twice for one finding.  These
tests pin what those decisions depend on — which single protocol each route is
measured on, what the measurement counts, that the wait for a reasoning route's
first token is reported rather than timed out, and that a press asking for the
reading does not run the availability probe beside it.
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import textwrap

from young_router.core.domains.providers_models import ProvidersModelsDomain


def route(name: str, **extra: object) -> dict[str, object]:
    value: dict[str, object] = {"litellm_model": name, "model_name": name}
    value.update(extra)
    return value


class RouteEligibilityTests(unittest.TestCase):
    def test_any_route_can_be_asked_for_the_reading(self) -> None:
        """The press states the question; a model's name does not gate it.

        The reading used to be offered only to a route whose name carried
        "flash": the menu row was hidden for every other model, which made a
        measurement of the route's own speed a property of the name a provider
        happened to give it.  What decides the reading is that the user asked
        for it, and what it measures is the one protocol the route recommends —
        so a route with a protocol has a reading, whatever it is called.
        """

        for name in (
            "openai/gemini-3.1-flash-lite",
            "openai/glm-5.3-flash",
            "openai/gpt-5",
            "anthropic/claude-opus-4-8",
            "azure/gpt-5.2",
            "deepseek/deepseek-v4",
        ):
            with self.subTest(name=name):
                self.assertTrue(
                    ProvidersModelsDomain._speed_test_protocol(route(name)),
                    name,
                )

        # A protocol this app cannot write a chat request for has no reading,
        # and says so with the empty protocol rather than with a quiet "slow".
        self.assertEqual(
            "",
            ProvidersModelsDomain._speed_test_protocol(
                route("openai/gpt-image-2.6", upstream_url_surface="openai/images")
            ),
        )

    def test_one_protocol_is_the_one_the_route_states(self) -> None:
        """The press measures the route's own recommendation, never a second one.

        ``fixed`` is a route that answers on one wire and names it.  ``fallback``
        is a route that follows the client: its family's wire is what serves it
        first, and the declared one is the backup kept for a refused client.
        Ranking the two the way the availability matrix ranks them is what keeps
        the button and the finding describing one route's protocol instead of
        disagreeing about which one is "the" protocol.
        """

        self.assertEqual(
            "openai/chat",
            ProvidersModelsDomain._speed_test_protocol(
                route(
                    "openai/glm-5.3-flash",
                    upstream_url_surface="openai/chat",
                    upstream_protocol_mode="fixed",
                )
            ),
        )
        # A pinned Messages route is measured on Messages even though its name
        # is not a Claude one: the pin is the user's own answer.
        self.assertEqual(
            "anthropic",
            ProvidersModelsDomain._speed_test_protocol(
                route(
                    "openai/some-flash",
                    upstream_url_surface="anthropic",
                    upstream_protocol_mode="fixed",
                )
            ),
        )
        # A fallback route names its backup protocol, and that is the protocol
        # the measurement speaks: it is the wire the pane draws beside 自动适配
        # and the one the route answers with when the client's own is refused.
        self.assertEqual(
            "anthropic",
            ProvidersModelsDomain._speed_test_protocol(
                route(
                    "openai/some-flash",
                    upstream_url_surface="anthropic",
                    upstream_protocol_mode="fallback",
                )
            ),
        )
        # A route that states nothing is served by its name's family, which is
        # still one protocol and not the three-surface availability matrix.
        self.assertEqual(
            "openai/chat",
            ProvidersModelsDomain._speed_test_protocol(route("openai/glm-5.3-flash")),
        )
        self.assertEqual(
            "openai/responses",
            ProvidersModelsDomain._speed_test_protocol(route("openai/gpt-5-flash")),
        )
        # The Images wire is not a protocol this app can write a chat request
        # for, so the caller keeps the ordinary probe for such a route.
        self.assertEqual(
            "",
            ProvidersModelsDomain._speed_test_protocol(
                route("openai/gpt-image-2.6-flash", upstream_url_surface="openai/images")
            ),
        )

    def test_the_window_stays_inside_the_band(self) -> None:
        """The window is what makes two readings comparable, so it is clamped.

        A host may state its own in the environment, but a value outside the
        band is not a measurement of the same thing as every other host's, and
        obeying it would publish a number no other reading can be compared with.
        """

        original = ProvidersModelsDomain._SPEED_TEST_WINDOW_ENV
        import os

        try:
            for stated, expected in (
                ("", ProvidersModelsDomain._SPEED_TEST_WINDOW_SECONDS),
                ("5", 5.0),
                ("7.5", 7.5),
                ("8", 8.0),
                ("1", ProvidersModelsDomain._SPEED_TEST_MIN_WINDOW_SECONDS),
                ("600", ProvidersModelsDomain._SPEED_TEST_MAX_WINDOW_SECONDS),
                ("nonsense", ProvidersModelsDomain._SPEED_TEST_WINDOW_SECONDS),
            ):
                with self.subTest(stated=stated):
                    if stated:
                        os.environ[original] = stated
                    else:
                        os.environ.pop(original, None)
                    self.assertAlmostEqual(
                        ProvidersModelsDomain._speed_test_window_seconds(), expected
                    )
        finally:
            os.environ.pop(original, None)


class FakeStream(BaseHTTPRequestHandler):
    """One upstream that streams what a test asks it to, and records the asks."""

    protocol_version = "HTTP/1.1"
    requests: list[dict[str, object]] = []
    scenario = "chat"
    pieces = 6
    delay = 0.001

    def do_POST(self) -> None:  # noqa: N802 - the framework's spelling
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        FakeStream.requests.append(
            {"path": self.path, "body": body, "headers": dict(self.headers)}
        )
        if FakeStream.scenario == "rate_limited":
            self.send_response(429)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for _ in range(FakeStream.pieces):
            self._chunk(self._event())
            time.sleep(FakeStream.delay)
        if FakeStream.scenario == "chat":
            self._chunk(
                b"data: "
                + json.dumps(
                    {"choices": [], "usage": {"completion_tokens": FakeStream.pieces * 5}}
                ).encode()
                + b"\n\n"
            )
            self._chunk(b"data: [DONE]\n\n")
        self._chunk(b"")

    def _event(self) -> bytes:
        if FakeStream.scenario == "anthropic":
            payload = {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "streamed words here "},
            }
            return (
                b"event: content_block_delta\ndata: "
                + json.dumps(payload).encode()
                + b"\n\n"
            )
        payload = {"choices": [{"delta": {"content": "streamed words here "}}]}
        return b"data: " + json.dumps(payload).encode() + b"\n\n"

    def _chunk(self, payload: bytes) -> None:
        self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
        self.wfile.flush()

    def log_message(self, *_args: object) -> None:
        return


class SpeedMeasurementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeStream)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        FakeStream.requests = []
        FakeStream.scenario = "chat"
        FakeStream.pieces = 6
        FakeStream.delay = 0.001
        self.domain = ProvidersModelsDomain.__new__(ProvidersModelsDomain)
        # The window is the only slow part of a measurement, and a test that
        # waited six seconds per case would stop being run; the code path is the
        # same one, with the clock's bound stated instead of assumed.
        self.window = ProvidersModelsDomain._SPEED_TEST_WINDOW_ENV
        import os

        os.environ[self.window] = "5"

    def tearDown(self) -> None:
        import os

        os.environ.pop(self.window, None)

    def measure(self, protocol: str = "openai/chat", **kwargs: object) -> dict[str, object]:
        return self.domain._speed_test_run(
            protocol=protocol,
            api_base=self.base,
            credential="sk-test",
            model_name="glm-5.3-flash",
            **kwargs,
        )

    def test_a_streaming_route_reports_the_rate_its_upstream_states(self) -> None:
        """The count is the upstream's own whenever its stream states one.

        Chat states it on the usage chunk and Messages on ``message_delta``;
        this side's count of the text is the fallback, not the authority, so the
        number the route reported is the number the reading publishes.
        """

        result = self.measure()

        self.assertEqual("ok", result["status"])
        # One answer is not the measurement: the fake upstream ends each turn
        # after a few milliseconds, and the run asks for another so the window
        # covers more than the first answer's own span.
        self.assertGreater(result["rounds"], 1)
        self.assertGreater(result["output_token_count_per_second"], 0)
        # Each round stated its own answer's token count: the statement wins
        # over any count of the text, which is what "counted by the upstream"
        # means, and the rounds are summed rather than one of them kept.
        self.assertEqual(
            FakeStream.pieces * 5 * result["rounds"],
            result["output_token_count"],
        )
        self.assertEqual("upstream", result["counted_by"])
        self.assertGreater(result["seconds"], 0)
        self.assertLessEqual(result["window_seconds"], 5.0)
        # The measurement asks for a stream, or the arrival time would be the
        # whole answer's latency rather than the rate it was produced at.
        self.assertTrue(FakeStream.requests[0]["body"]["stream"])
        self.assertIn("/v1/chat/completions", FakeStream.requests[0]["path"])

    def test_a_stream_that_states_no_usage_is_counted_here(self) -> None:
        """A stream without a token count still has a rate to report.

        Relaying one is optional in both wires, and a route that omits it would
        otherwise report no speed at all.  The reading then names the estimate
        as the estimate it is instead of presenting it as the upstream's own.
        """

        FakeStream.scenario = "anthropic"

        result = self.measure("anthropic", )

        self.assertEqual("ok", result["status"])
        self.assertEqual("estimate", result["counted_by"])
        self.assertGreater(result["output_token_count"], 0)
        self.assertGreater(result["output_token_count_per_second"], 0)
        self.assertIn("/v1/messages", FakeStream.requests[0]["path"])

    def test_an_empty_window_is_silence_and_not_a_speed(self) -> None:
        """A route that emits nothing has not been measured at zero tokens a second."""

        FakeStream.pieces = 0

        result = self.measure()

        self.assertNotEqual("ok", result["status"])
        self.assertIsNone(result["output_token_count_per_second"])
        self.assertEqual(0, result["output_token_count"])

    def test_a_rate_limit_keeps_its_own_name(self) -> None:
        """A 429 read the request and refused the pace; it is not silence."""

        FakeStream.scenario = "rate_limited"

        result = self.measure()

        self.assertEqual("rate_limited", result["status"])
        self.assertIsNone(result["output_token_count_per_second"])

    def test_an_unaddressable_route_is_not_reported_as_slow(self) -> None:
        """A route with nothing to ask keeps the availability probe's own word."""

        result = self.domain._speed_test_run(
            protocol="openai/chat",
            api_base="",
            credential="",
            model_name="",
        )

        self.assertEqual("invalid_config", result["status"])
        self.assertIsNone(result["output_token_count_per_second"])
        self.assertEqual([], FakeStream.requests)

    def test_the_wait_for_the_first_token_is_not_the_window(self) -> None:
        """A reasoning route may think longer than the window before it speaks.

        The window is a promise about *generation*: six seconds of output.  It
        used to be pinned to the request, so a route that prefilled for longer
        than the window never produced a token inside it and every press read as
        a timeout — measured on a reasoning route, the first token arrived 15-25
        s after the request.  The wait is bounded by the probe's own first-event
        budget and reported as ``first_token_seconds``; the window opens when
        output does.  This run's wait is deliberately longer than the window, so
        a reading pinned to the request would have no token at all.
        """

        domain = ProvidersModelsDomain.__new__(ProvidersModelsDomain)
        ticks = iter(range(0, 10_000))
        # Two seconds per clock read, and two silent reads before the first
        # piece: the wait is 8 s against a 5 s window.
        clock = lambda: next(ticks) * 2.0

        class Response:
            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> bool:
                return False

        class Opener:
            def open(self, *_args: object, **_kwargs: object) -> Response:
                return Response()

        events = [
            ("", 0),
            ("", 0),
            ("one ", 0),
            ("two ", 0),
            ("three ", 0),
        ]
        from unittest import mock

        with mock.patch(
            "young_router.core.domains.providers_models.isolated_http_opener",
            return_value=Opener(),
        ), mock.patch.object(
            ProvidersModelsDomain, "_speed_test_events", return_value=iter(events)
        ), mock.patch.object(
            ProvidersModelsDomain, "_count_tokens", return_value=3
        ), mock.patch.object(
            ProvidersModelsDomain,
            "_speed_test_request",
            return_value={
                "method": "POST",
                "url": "https://example.test/v1/chat/completions",
                "headers": {},
                "display_headers": {},
                "body": {},
            },
        ), mock.patch(
            "young_router.core.domains.providers_models.time.monotonic", side_effect=clock
        ), mock.patch(
            "young_router.core.domains.providers_models.time.sleep"
        ):
            reading = domain._speed_test_run(
                protocol="openai/chat",
                api_base="https://example.test/v1",
                credential="replace-me-secret",
                model_name="reasoning-route",
            )

        self.assertEqual("ok", reading["status"])
        self.assertGreater(
            reading["first_token_seconds"], reading["window_seconds"]
        )
        self.assertGreater(reading["output_token_count_per_second"], 0.0)
        self.assertGreaterEqual(reading["rounds"], 1)

    def test_a_credential_is_never_part_of_the_reported_request(self) -> None:
        """The pane draws the request that was made, and a key is not that."""

        result = self.measure()

        headers = result["original_request"]["headers"]
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("x-api-key", headers)
        self.assertEqual("text/event-stream", headers["Accept"])


PROVIDER_CONFIG = """
providers:
  primary:
    api_base: "https://example.test/v1"
    api_keys:
      - name: default
        value: "replace-me-secret"
model_list:
  - model_name: glm-5.3-flash
    litellm_params:
      model: openai/glm-5.3-flash
      api_base: "https://example.test/v1"
      api_key: "replace-me-secret"
    model_info:
      id: "00000071"
      provider: primary
      upstream_url_surface: openai/chat
      upstream_protocol_mode: fixed
"""


class SpeedReplacesTheAvailabilityProbeTests(unittest.TestCase):
    """One press, one measurement: the protocol measured is not asked twice."""

    def probe(self, *, surfaces: str = "openai/chat") -> tuple[dict[str, object], list[str]]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                textwrap.dedent(PROVIDER_CONFIG).lstrip().replace(
                    "upstream_url_surface: openai/chat",
                    f"upstream_url_surface: {surfaces}",
                ),
                encoding="utf-8",
            )
            domain = ProvidersModelsDomain(path)
            asked: list[str] = []

            def surface_probe(*, surface: str, **_kwargs: object) -> dict[str, object]:
                asked.append(surface)
                return {"surface": surface, "available": True, "status": "ok"}

            def speed(**kwargs: object) -> dict[str, object]:
                asked.append(f"speed:{kwargs['protocol']}")
                return {
                    "status": "ok",
                    "protocol": str(kwargs["protocol"]),
                    "output_token_count": 120,
                    "seconds": 5.0,
                    "output_token_count_per_second": 24.0,
                    "counted_by": "upstream",
                    "original_request": {"method": "POST", "url": "", "headers": {}, "body": {}},
                }

            from unittest import mock

            with mock.patch.object(
                ProvidersModelsDomain, "_surface_probe", side_effect=surface_probe
            ), mock.patch.object(
                ProvidersModelsDomain, "_speed_test_run", side_effect=speed
            ):
                result = domain.probe(
                    {
                        # The speed reading is the row the pane offers, so the
                        # press states it; 「连通性」 no longer measures a route
                        # just because its name says flash.
                        "kind": "speed",
                        "provider_id": domain.snapshot()["providers"][0]["id"],
                        "model_id": domain.snapshot()["providers"][0]["models"][0]["editor_id"],
                    }
                )
            return result, asked

    def test_the_measured_protocol_is_not_probed_again(self) -> None:
        """The streamed turn answered the availability question on that surface.

        The other surfaces keep their own probes — a recommendation compares
        their answers — but the one the reading speaks must not be asked twice:
        that is the duplicate press this reading replaced.
        """

        result, asked = self.probe()

        self.assertIn("speed:openai/chat", asked)
        self.assertNotIn("openai/chat", asked)
        self.assertEqual("ok", result["speed_test"]["status"])
        self.assertEqual(24.0, result["speed_test"]["output_token_count_per_second"])

    def test_the_reading_is_stored_with_the_finding(self) -> None:
        """A window that reopens reads the number the last press measured."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8"
            )
            domain = ProvidersModelsDomain(path)
            provider_id = domain.snapshot()["providers"][0]["id"]
            model_id = domain.snapshot()["providers"][0]["models"][0]["editor_id"]
            from unittest import mock

            with mock.patch.object(
                ProvidersModelsDomain,
                "_speed_test_run",
                return_value={
                    "status": "ok",
                    "protocol": "openai/chat",
                    "output_token_count": 300,
                    "seconds": 5.0,
                    "output_token_count_per_second": 60.0,
                    "counted_by": "upstream",
                    "original_request": {"method": "POST", "url": "", "headers": {}, "body": {}},
                },
            ):
                domain.probe(
                    {
                        "kind": "speed",
                        "provider_id": provider_id,
                        "model_id": model_id,
                    }
                )

            stored = domain.snapshot()["providers"][0]["models"][0]["probe"]
            self.assertEqual(60.0, stored["speed_test"]["output_token_count_per_second"])

    def test_the_deep_plan_describes_the_deep_test_and_nothing_else(self) -> None:
        """A flash name no longer shortens the plan.

        The plan used to drop its scan when the route's name said "flash",
        because that route's press was a speed measurement on the same wire.
        Speed is its own row now, so the plan states what the deep test runs and
        lets the press's own layers decide whether both are asked: a "full"
        press asks for the scan *and* the reading, and reading the name here
        would make the deep test a property of a name instead of a protocol.
        """

        model = {
            "litellm_model": "openai/glm-5.3-flash",
            "model_name": "glm-5.3-flash",
            "upstream_url_surface": "openai/chat",
            "upstream_protocol_mode": "fixed",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            from unittest import mock

            # The quick suite scans the surface the degradation requirements
            # name; state which one that is instead of reaching for the
            # adapter that computes it.  A staged baseline makes the plan
            # reachable, so this case reads the surface rule itself.
            with mock.patch.object(
                ProvidersModelsDomain,
                "_degradation_requirements",
                return_value="openai/chat",
            ), mock.patch.object(
                ProvidersModelsDomain,
                "_reference_fingerprint",
                return_value={"fingerprint": "reference"},
            ):
                plan = domain._deep_probe_plan(model)
            self.assertTrue(plan["includes_degradation"])
            self.assertEqual("openai/chat", plan["surface"])

            # A route the table cannot speak still says so: the reason is the
            # protocol, not the model's name.
            with mock.patch.object(
                ProvidersModelsDomain,
                "_degradation_requirements",
                return_value="",
            ), mock.patch.object(
                ProvidersModelsDomain,
                "_reference_fingerprint",
                return_value={"fingerprint": "reference"},
            ):
                plan = domain._deep_probe_plan(model)
            self.assertTrue(plan["includes_degradation"])

            # A route the table speaks on another surface keeps its scan only
            # when this press can actually ask that surface: the plan answers
            # "will the deep test run", not "would it run for some other press".
            with mock.patch.object(
                ProvidersModelsDomain,
                "_degradation_requirements",
                return_value="anthropic",
            ), mock.patch.object(
                ProvidersModelsDomain,
                "_reference_fingerprint",
                return_value={"fingerprint": "reference"},
            ):
                plan = domain._deep_probe_plan(model, surfaces=["openai/chat"])
                self.assertFalse(plan["includes_degradation"])
                self.assertEqual("", plan["surface"])
                plan = domain._deep_probe_plan(
                    model, surfaces=["openai/chat", "anthropic"]
                )
                self.assertTrue(plan["includes_degradation"])
                self.assertEqual("anthropic", plan["surface"])


class AModelNameDoesNotDecideTests(unittest.TestCase):
    def test_a_model_that_is_not_flash_still_reports_a_reading(self) -> None:
        """The menu offers the reading for every route, so a press gets one.

        A route whose name carries no "flash" used to be refused the press:
        the pane hid the row, and a press that slipped through measured nothing.
        The row is the user's statement of intent now, so this counts the
        reading a non-flash route's speed press actually produces.
        """

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                textwrap.dedent(PROVIDER_CONFIG)
                .lstrip()
                .replace("glm-5.3-flash", "gpt-5")
                .replace("openai/chat", "openai/responses"),
                encoding="utf-8",
            )
            domain = ProvidersModelsDomain(path)
            from unittest import mock

            def surface_probe(*, surface: str, **_kwargs: object) -> dict[str, object]:
                return {"surface": surface, "available": True, "status": "ok"}

            with mock.patch.object(
                ProvidersModelsDomain, "_surface_probe", side_effect=surface_probe
            ), mock.patch.object(
                ProvidersModelsDomain,
                "_speed_test_run",
                return_value={
                    "status": "ok",
                    "protocol": "openai/responses",
                    "output_token_count": 90,
                    "seconds": 3.0,
                    "output_token_count_per_second": 30.0,
                    "first_token_seconds": 1.5,
                    "counted_by": "upstream",
                },
            ) as run:
                result = domain.probe(
                    {
                        "kind": "speed",
                        "provider_id": domain.snapshot()["providers"][0]["id"],
                        "model_id": domain.snapshot()["providers"][0]["models"][0]["editor_id"],
                    }
                )

            run.assert_called_once()
            self.assertEqual("openai/responses", run.call_args.kwargs["protocol"])
            self.assertEqual("ok", result["speed_test"]["status"])
            self.assertEqual(30.0, result["speed_test"]["output_token_count_per_second"])

    def test_a_route_with_no_chat_wire_says_so_instead_of_saying_nothing(self) -> None:
        """An image route cannot be streamed, and the press still answers.

        "No chat wire" is a finding about the reading, not a reason to answer a
        speed press with the availability matrix and leave the rate unmentioned.
        """

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                textwrap.dedent(PROVIDER_CONFIG)
                .lstrip()
                .replace("glm-5.3-flash", "gpt-image-2.6"),
                encoding="utf-8",
            )
            domain = ProvidersModelsDomain(path)
            from unittest import mock

            def surface_probe(*, surface: str, **_kwargs: object) -> dict[str, object]:
                return {"surface": surface, "available": True, "status": "ok"}

            # An Images route is one this domain must find behaviourally: the
            # bundled cost map is what answers ``serves_images``, and this test
            # is about the reading's wording rather than that lookup.  The empty
            # protocol is what ``_speed_test_protocol`` returns for it.
            with mock.patch.object(
                ProvidersModelsDomain, "_surface_probe", side_effect=surface_probe
            ), mock.patch.object(
                ProvidersModelsDomain,
                "_speed_test_protocol",
                return_value="",
            ):
                result = domain.probe(
                    {
                        "kind": "speed",
                        "provider_id": domain.snapshot()["providers"][0]["id"],
                        "model_id": domain.snapshot()["providers"][0]["models"][0]["editor_id"],
                    }
                )

            self.assertIsNotNone(result["speed_test"])
            self.assertEqual("invalid_config", result["speed_test"]["status"])
            self.assertEqual("", result["speed_test"]["protocol"])
            self.assertIn("chat wire", result["speed_test"]["detail"])


class NonFlashRouteTests(unittest.TestCase):
    def test_a_press_that_did_not_ask_for_the_reading_keeps_the_probe(self) -> None:
        """The reading is its own row; 连通性 still asks the matrix.

        A default press on any route — flash-named or not — keeps the
        three-surface matrix it always ran.  The flash name only changes the
        deep plan, which drops a scan the reading already answered.
        """

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                textwrap.dedent(PROVIDER_CONFIG)
                .lstrip()
                .replace("glm-5.3-flash", "gpt-5")
                .replace("openai/chat", "openai/responses"),
                encoding="utf-8",
            )
            domain = ProvidersModelsDomain(path)
            asked: list[str] = []

            def surface_probe(*, surface: str, **_kwargs: object) -> dict[str, object]:
                asked.append(surface)
                return {"surface": surface, "available": True, "status": "ok"}

            def speed(**_kwargs: object) -> dict[str, object]:
                asked.append("speed")
                return {"status": "ok", "output_token_count_per_second": 1.0}

            from unittest import mock

            with mock.patch.object(
                ProvidersModelsDomain, "_surface_probe", side_effect=surface_probe
            ), mock.patch.object(
                ProvidersModelsDomain, "_speed_test_run", side_effect=speed
            ):
                result = domain.probe(
                    {
                        "provider_id": domain.snapshot()["providers"][0]["id"],
                        "model_id": domain.snapshot()["providers"][0]["models"][0]["editor_id"],
                    }
                )

            self.assertNotIn("speed", asked)
            self.assertIsNone(result["speed_test"])
            self.assertEqual({"openai/responses", "openai/chat", "anthropic"}, set(asked))


if __name__ == "__main__":
    unittest.main()
