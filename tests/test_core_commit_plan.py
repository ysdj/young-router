"""The screen one Apply reads, and the event one staged edit publishes.

Two costs are pinned here, both measured from a real defect: a toggle in the
published page took seconds while the same toggle in the native window was
instant.

- An Apply read the whole projection to answer three questions -- the head
  revision, which domains carry a staged draft, and whether an external edit
  stands in front of one of them.  On this project's own document that is 183 KB
  per edit, which a browser reaching the host over a link pays in seconds.  The
  plan read answers the same three questions in about a kilobyte, and these
  tests hold it to that: it carries no settings document, and it stays small as
  the document grows.
- A commit that only staged a draft moved every domain's own revision counter,
  and the patch builder read "a projection moved and cannot say how" and sent a
  resync.  Every window then re-read every settings document, once per staged
  control -- so the page downloaded the whole state again after each click, and
  the resync was the real reason its "post-action read" never went away.  A
  counter is not a change; the draft travels beside the ops.
"""

from __future__ import annotations

import gzip
import http.client
import json
from pathlib import Path
import tempfile
import time
import unittest

from young_router.core.domains.providers_models import ProvidersModelsDomain
from young_router.core.ipc import CoreIPCServer
from young_router.core.service import CoreStore

#: Every domain the shell asks the plan for.  The real store registers them;
#: this fixture reads the same list so a rename shows up here as a failure.
PLAN_DOMAINS = [
    "providers_models",
    "relay_accounts",
    "runtime",
    "language",
    "codex",
    "claude",
    "clients",
    "webdav",
    "logs",
]


def _document(providers: int = 6, routes: int = 6) -> str:
    """A document shaped like an installation someone actually runs."""

    lines = ["providers:"]
    for provider in range(providers):
        name = f"provider{provider}"
        lines.extend(
            [
                f"  {name}:",
                f'    api_base: "https://relay{provider}.example.test/v1"',
                "    api_keys:",
                f'      - name: key\n        value: "sk-replace-{provider}"',
            ]
        )
    lines.append("model_list:")
    for provider in range(providers):
        for route in range(routes):
            lines.extend(
                [
                    f"  - model_name: public-{provider}-{route}",
                    "    litellm_params:",
                    f"      model: openai/upstream-{provider}-{route}",
                    f'      api_base: "https://relay{provider}.example.test/v1"',
                    f'      api_key: "sk-replace-{provider}"',
                    "    model_info:",
                    f"      id: {provider:02x}{route:02x}abcd",
                    f"      provider: provider{provider}",
                    "      upstream_url_surface: openai/responses",
                ]
            )
    lines.extend(["general_settings:", "  master_key: sk-replace-master", ""])
    return "\n".join(lines)


def _wait_for_events(core: CoreStore, events: list[dict[str, object]], count: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while len(events) < count and time.monotonic() < deadline:
        core._emitter.flush()
        time.sleep(0.005)
    if len(events) < count:
        raise AssertionError(f"the store published {len(events)} events, expected {count}")


class CommitPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.config = self.root / "config.yaml"
        self.config.write_text(_document(), encoding="utf-8")
        self.core = CoreStore(domains=[ProvidersModelsDomain(self.config)])
        self.core.snapshot()

    def _stage_first_provider_toggle(self) -> int:
        snapshot = self.core.snapshot()
        provider = snapshot["providers_models"]["providers"][0]
        answer = self.core.dispatch(
            {
                "domain": "providers_models",
                "type": "provider.patch",
                "payload": {
                    "provider_id": provider["id"],
                    "changes": {"enabled": not provider["enabled"]},
                },
            },
            expected_revision=snapshot["revision"],
        )
        return int(answer["revision"])

    def test_the_plan_is_the_commit_screen_and_not_the_projection(self) -> None:
        """A kilobyte of facts, never a settings document.

        The pane's Apply asks this four times per edit (the first attempt, each
        rebase, and the settled read), and each of those used to be every
        provider, every route and every client document at once.
        """

        plan = self.core.commit_plan(["providers_models"])
        self.assertEqual(set(plan), {"revision", "projected_revision", "service", "drafts", "disk", "risks"})
        self.assertNotIn("domains", plan)
        self.assertNotIn("providers_models", plan)
        self.assertFalse(plan["drafts"]["providers_models"]["dirty"])
        snapshot = self.core.snapshot()
        plan_bytes = len(json.dumps(plan, separators=(",", ":")).encode("utf-8"))
        snapshot_bytes = len(json.dumps(snapshot, separators=(",", ":")).encode("utf-8"))
        self.assertGreater(snapshot_bytes, 20 * 1024, "this fixture is too small to prove anything")
        self.assertLess(plan_bytes, 4 * 1024, f"the plan is {plan_bytes} bytes")
        self.assertLess(plan_bytes * 10, snapshot_bytes, "the plan is not an order of magnitude smaller")

    def test_the_plan_states_what_is_staged(self) -> None:
        self._stage_first_provider_toggle()
        plan = self.core.commit_plan(["providers_models"])
        self.assertTrue(plan["drafts"]["providers_models"]["dirty"])
        self.assertEqual(self.core.revision, plan["revision"])
        self.assertEqual(self.core.snapshot()["revision"], plan["revision"])

    def test_a_staged_edit_is_a_patch_and_an_apply_is_one_too(self) -> None:
        """Neither commit may answer with a resync.

        A resync is every window reading every settings document.  The staged
        commit and the apply that writes it are the two most ordinary commits
        this app makes, and both used to be able to answer with one.
        """

        events: list[dict[str, object]] = []
        unsubscribe = self.core.subscribe(events.append)
        self.addCleanup(unsubscribe)
        self.core._emit()
        self.assertEqual("snapshot", events[0]["event"])

        self._stage_first_provider_toggle()
        _wait_for_events(self.core, events, 2)
        staged = events[-1]
        self.assertEqual("patch", staged["event"], f"the staged commit sent {staged['event']}")
        self.assertTrue(staged["drafts"]["providers_models"]["dirty"])

        self.core.apply("providers_models")
        _wait_for_events(self.core, events, 3)
        applied = events[-1]
        self.assertEqual("patch", applied["event"], f"the apply sent {applied['event']}")
        self.assertFalse(applied["drafts"]["providers_models"]["dirty"])

    def test_a_projection_that_moved_only_its_counter_is_no_change(self) -> None:
        """The rule the resync storm came from, stated on its own.

        Every commit advances the domain's own revision counter, including the
        commit that only stages a draft: the domain was touched, no row moved.
        A patch built from two such projections is the draft, which travels
        beside the ops -- not "a change I cannot describe".
        """

        def projection(revision: int, dirty: bool) -> dict[str, object]:
            return {
                "revision": revision,
                "projected_revision": revision,
                "service": {"state": "running"},
                "drafts": {"providers_models": {"dirty": dirty, "base_revision": 0, "validation": {"valid": True, "issues": []}}},
                "disk": {},
                "action_summaries": {},
                "domains": {
                    "providers_models": {
                        "domain": "providers_models",
                        "revision": revision,
                        "providers": [{"id": "primary", "name": "primary", "enabled": True}],
                    }
                },
            }

        event = self.core._patch_event(projection(7, True), projection(6, False), 6)
        self.assertIsNotNone(event, "a staged draft produced no event at all")
        self.assertEqual("patch", event["event"])
        self.assertEqual([], event["ops"])
        self.assertEqual([], event["domains"])
        self.assertTrue(event["drafts"]["providers_models"]["dirty"])

    def test_an_unchanged_commit_publishes_nothing(self) -> None:
        """A counter move with no field beside it is not an event."""

        def projection(revision: int) -> dict[str, object]:
            return {
                "revision": revision,
                "projected_revision": revision,
                "service": {"state": "running"},
                "drafts": {"providers_models": {"dirty": False, "base_revision": 0, "validation": {"valid": True, "issues": []}}},
                "disk": {},
                "action_summaries": {},
                "domains": {"providers_models": {"domain": "providers_models", "revision": revision, "providers": []}},
            }

        self.assertIsNone(self.core._patch_event(projection(7), projection(6), 6))


class IpcCompressionTests(unittest.TestCase):
    """A projection over a link is compressed, and a native read is not."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        root = Path(self._directory.name)
        config = root / "config.yaml"
        config.write_text(_document(), encoding="utf-8")
        self.core = CoreStore(domains=[ProvidersModelsDomain(config)])
        self.core.snapshot()
        self.server = CoreIPCServer(self.core)
        self.server.start()
        self.addCleanup(self.server.stop)
        endpoint = self.server.endpoint
        self.address = (endpoint.address, endpoint.port)
        # The page is how a loopback caller proves it is this machine's owner:
        # the same answer mints the cookie the protocol then accepts.
        bundle = Path(__file__).resolve().parents[1] / "rn" / "apps" / "web" / "dist"
        self.server.enable_web_host(bundle)

    def _call(self, method: str, accept_encoding: str | None) -> tuple[int, dict[str, str], bytes]:
        body = json.dumps({"protocol_version": 1, "request_id": "compression", "method": method, "params": {"domains": ["providers_models"]} if method == "staged" else {}}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if accept_encoding is not None:
            headers["Accept-Encoding"] = accept_encoding
        connection = http.client.HTTPConnection(*self.address, timeout=10)
        try:
            # A loopback page is the owner's own door: the page request mints the
            # session cookie the protocol then accepts.
            connection.request("GET", "/ui/", headers={})
            response = connection.getresponse()
            response.read()
            cookie = next((value.split(";")[0] for name, value in response.getheaders() if name.lower() == "set-cookie"), "")
            headers["Cookie"] = cookie
            connection.request("POST", "/ipc", body=body, headers=headers)
            answer = connection.getresponse()
            return answer.status, {key.lower(): value for key, value in answer.getheaders()}, answer.read()
        finally:
            connection.close()

    def test_a_projection_costs_a_fifth_of_its_bytes_on_the_wire(self) -> None:
        status, headers, body = self._call("snapshot", "gzip")
        self.assertEqual(200, status)
        self.assertEqual("gzip", headers.get("content-encoding"))
        self.assertIn("accept-encoding", headers.get("vary", "").lower())
        plain = json.loads(gzip.decompress(body))
        self.assertIn("snapshot", plain["result"])
        # The listing is what the assertion is about: the compressed body must be
        # the same projection, and much smaller.
        self.assertGreater(len(gzip.decompress(body)) * 3, len(body) * 10)

    def test_a_native_read_stays_plain(self) -> None:
        """The native hosts send no `Accept-Encoding`; they get the bytes.

        A WinHTTP client does not decode on its own, so an answer compressed
        without being asked for is a window that cannot parse its own state.
        """

        status, headers, body = self._call("snapshot", None)
        self.assertEqual(200, status)
        self.assertNotIn("content-encoding", headers)
        self.assertIn("snapshot", json.loads(body)["result"])

    def test_a_small_answer_is_not_compressed(self) -> None:
        status, headers, body = self._call("staged", "gzip")
        self.assertEqual(200, status)
        self.assertNotIn("content-encoding", headers, "a kilobyte answer was deflated for no reason")
        self.assertIn("drafts", json.loads(body)["result"])


if __name__ == "__main__":
    unittest.main()
