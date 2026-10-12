from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HYGIENE_SCRIPT = ROOT / "scripts" / "check-repo-hygiene.py"


def _load_hygiene_module():
    """Load the gate the way the commit hook and CI invoke it."""

    spec = importlib.util.spec_from_file_location(
        "young_router_repo_hygiene",
        HYGIENE_SCRIPT,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


hygiene = _load_hygiene_module()


def _join(*parts: str) -> str:
    """Assemble a value the gate must refuse from pieces.

    Every refused value is built here rather than written out, so this file does
    not itself contain the thing it proves is refused: the gate reads the bytes
    of the test file too, and a test that trips the gate would make the gate
    unusable.
    """

    return "".join(parts)


# Values assembled from parts; each is shaped like the real thing.
PRIVATE_KEY = _join("-----BEGIN RSA ", "PRIVATE KEY-----", "\nMIIEow==\n")
OPENAI_KEY = _join("sk-proj-", "T3BlbkFJq8xVn2mKpLd9wRzYcHaB4eGtUiOaS6")
GITHUB_TOKEN = _join("ghp_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8")
AWS_KEY = _join("AKIA", "IOSFODNN7EXAMPLE")
GOOGLE_KEY = _join("AIza", "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q")
JWT = _join("eyJhbGciOiJIUzI1NiJ9", ".", "eyJzdWIiOiIxIn0", ".", "abcdefghijklmnop")
BEARER = _join("Bearer ", "A" * 48)
MACHINE_ADDRESS = _join("mba@", "mbadeMacBook-Air", ".local")
HOME_PATH = _join("/Users/", "mba", "/.young-router/image-cache/x.jpg")
PERSONAL_ADDRESS = _join("someone@", "qq.com")


class SecretPatternTests(unittest.TestCase):
    """A reading that must not depend on a fixture's wording.

    The gate is only useful if it separates a real credential from the readable
    words this repository writes as fixtures, so both halves are asserted.
    """

    def _findings(self, text: str, *, name: str = "some/file.py"):
        return hygiene._scan_bytes(
            text.encode(),
            name,
            local_facts_allowed=hygiene._local_facts_allowed(name),
        )

    def test_a_real_provider_key_is_refused(self) -> None:
        findings = self._findings(f'api_key = "{OPENAI_KEY}"')

        self.assertEqual(["secret"], [finding.kind for finding in findings])
        self.assertIn("OpenAI", findings[0].detail)

    def test_a_github_and_cloud_key_are_refused(self) -> None:
        for text, label in (
            (f"token: {GITHUB_TOKEN}", "GitHub"),
            (f"id: {AWS_KEY}", "AWS"),
            (f"key: {GOOGLE_KEY}", "Google"),
        ):
            with self.subTest(label=label):
                findings = self._findings(text)
                self.assertEqual(["secret"], [finding.kind for finding in findings])

    def test_a_private_key_a_token_and_a_bearer_credential_are_refused(self) -> None:
        for text in (
            PRIVATE_KEY,
            f"Authorization: Bearer {JWT}",
            f"Authorization: {BEARER}",
        ):
            with self.subTest(text=text[:32]):
                findings = self._findings(text)
                self.assertTrue(
                    any(finding.kind == "secret" for finding in findings),
                    findings,
                )

    def test_the_repositories_own_fixtures_are_not_refused(self) -> None:
        """A gate that fails on the tree it guards is a gate nobody runs.

        Every value here is one a committed test really writes, and each is a
        readable word rather than the opaque run a real credential carries.
        """

        for text in (
            'api_key = "sk-replace-selected-key"',
            'api_key = "sk-synthetic-summary-secret"',
            'api_key = "sk-persisted-on-the-slot"',
            'headers = {"Authorization": "Bearer replace-me-secondary-secret"}',
            'token = "sk-private-provider-token"',
        ):
            with self.subTest(text=text):
                self.assertEqual([], self._findings(text))


class LocalFactPatternTests(unittest.TestCase):
    def _findings(self, text: str, *, name: str = "young_router/core/service.py"):
        return hygiene._scan_bytes(
            text.encode(),
            name,
            local_facts_allowed=hygiene._local_facts_allowed(name),
        )

    def test_a_developer_home_directory_is_refused_outside_the_tests(self) -> None:
        findings = self._findings(f"path = '{HOME_PATH}'")

        self.assertEqual(["local fact"], [finding.kind for finding in findings])

    def test_a_home_directory_is_allowed_inside_the_tests(self) -> None:
        """The tests spell a path out to prove the router never learns it."""

        findings = self._findings(
            f"path = '{HOME_PATH}'",
            name="tests/test_hooks_responses_request_prep.py",
        )

        self.assertEqual([], findings)

    def test_the_documentation_placeholder_stays_usable(self) -> None:
        for name in ("tests/test_api_base.py", "young_router/core/service.py"):
            with self.subTest(name=name):
                findings = self._findings(
                    'path = "/Users/example/.pi/agent/settings.json"',
                    name=name,
                )
                self.assertEqual([], findings)

    def test_a_machine_name_is_refused_but_an_attribute_is_not(self) -> None:
        refused = self._findings(f"commit as {MACHINE_ADDRESS}")
        for allowed in (
            "self._cache = threading.local()",
            "rn/apps/macos/macos/.xcode.env.local",
        ):
            with self.subTest(allowed=allowed):
                self.assertEqual([], self._findings(allowed))
        # A personal hostname states two facts -- the machine and the address
        # that names it -- and both are reported.
        self.assertEqual(
            ["local fact", "local fact"],
            [finding.kind for finding in refused],
        )

    def test_a_private_endpoint_is_refused_but_an_sdk_version_is_not(self) -> None:
        refused = self._findings('proxy = "http://192.168.1.14:7890"')
        for allowed in (
            "WindowsTargetPlatformVersion = '10.0.22621.0'",
            'MinVersion="10.0.17763.0"',
        ):
            with self.subTest(allowed=allowed):
                self.assertEqual([], self._findings(allowed))
        self.assertEqual(["local fact"], [finding.kind for finding in refused])

    def test_a_personal_address_is_refused_and_a_fixture_is_not(self) -> None:
        refused = self._findings(f'author_email = "{PERSONAL_ADDRESS}"')
        for allowed in (
            'username = "person@example.test"',
            'email = "noreply@users.noreply.github.com"',
            "status_icon@2x.png",
        ):
            with self.subTest(allowed=allowed):
                self.assertEqual([], self._findings(allowed))
        self.assertEqual(["local fact"], [finding.kind for finding in refused])


class StagedScanTests(unittest.TestCase):
    """The gate as the commit path really runs it, against the real repository."""

    def test_the_committed_tree_is_clean(self) -> None:
        self.assertEqual([], hygiene.scan_tree("HEAD"))

    def test_the_gate_refuses_a_staged_leak(self) -> None:
        """A staged blob carrying a private value fails the commit."""

        findings = hygiene._scan_bytes(
            b'base_url = "https://api.vendor.example/v1"\n',
            "young_router/config/providers.yaml",
            local_facts_allowed=False,
        )
        self.assertEqual([], findings)

        findings = hygiene._scan_bytes(
            f'home = "{HOME_PATH}"\n'.encode(),
            "young_router/config/providers.yaml",
            local_facts_allowed=False,
        )
        self.assertEqual(["local fact"], [finding.kind for finding in findings])

    def test_a_runtime_document_is_refused_by_name(self) -> None:
        """The files this repository never publishes are refused on sight."""

        for name in (
            "config.yaml",
            ".env",
            "young_router/service.log",
            "core-state.db",
            "young.router.proxy.runtime.plist",
        ):
            with self.subTest(name=name):
                matched = any(
                    pattern.search(name)
                    for _, pattern in hygiene.SENSITIVE_FILE_PATTERNS
                )
                self.assertTrue(matched, name)

        for name in (
            "config.example.yaml",
            "young_router/core/service.py",
            ".env.example",
        ):
            with self.subTest(name=name):
                matched = any(
                    pattern.search(name)
                    for _, pattern in hygiene.SENSITIVE_FILE_PATTERNS
                )
                self.assertFalse(matched, name)

    def test_the_upstream_allowlist_is_honoured(self) -> None:
        """A lockfile records maintainer addresses by design."""

        findings = hygiene.scan_tree("HEAD")
        rendered = [finding.render() for finding in findings]

        self.assertNotIn("rn/pnpm-lock.yaml", " ".join(rendered))


if __name__ == "__main__":
    unittest.main()
