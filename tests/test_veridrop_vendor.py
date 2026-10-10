"""Veridrop staging and in-process plumbing for the authenticity deep test."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import textwrap
import unittest
from unittest import mock

from young_router.adapters import veridrop


ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts" / "update_veridrop.py"

FIXTURE_REVISION = "0123456789abcdef0123456789abcdef01234567"

FIXTURE_PYPROJECT = textwrap.dedent(
    """
    [project]
    name = "relay-detector"
    version = "0.1.0"
    license = "AGPL-3.0-or-later"
    dependencies = ["httpx>=0.27", "typer>=0.12", "rapidfuzz>=3.6"]

    [project.scripts]
    relay-detector = "relay_detector.cli:app"
    veridrop = "relay_detector.cli:app"
    """
)

# The fixture mirrors the shapes the staging guard and the in-process call
# exercise, so both run against real modules instead of mocks.  The credential
# is echoed back through the report's identity field, which is how a test
# proves the suite ran inside this interpreter.
FIXTURE_PROTOCOL_CHOICES = {
    "anthropic": "MODELS",
    "openai": "OPENAI_MODEL_CHOICES",
    "gemini": "GEMINI_MODEL_CHOICES",
}
FIXTURE_CLI = textwrap.dedent(
    '''
    """Fixture entry point for the staged program."""

    async def _run_detect(protocol, base_url, api_key, model, config, output_path) -> None:
        raise NotImplementedError
    '''
)

FIXTURE_MODELS = textwrap.dedent(
    '''
    """Fixture execution models."""

    from enum import Enum


    class Mode(str, Enum):
        QUICK = "quick"
        STANDARD = "standard"


    class Protocol(str, Enum):
        ANTHROPIC = "anthropic"
        OPENAI = "openai"
        GEMINI = "gemini"


    class ExecutionConfig:
        def __init__(self, mode, overall_timeout_s=60.0, max_concurrent=3, request_timeout_s=30.0):
            self.mode = mode
            self.overall_timeout_s = overall_timeout_s
            self.max_concurrent = max_concurrent
            self.request_timeout_s = request_timeout_s

        @classmethod
        def for_mode(cls, mode, **overrides):
            return cls(mode, **overrides)


    class _Report:
        """Stands in for upstream's pydantic report model."""

        def __init__(self, **fields):
            self.fields = fields

        def model_dump_json(self):
            import json
            fields = dict(self.fields)
            fields["protocol"] = getattr(fields.get("protocol"), "value", fields.get("protocol"))
            fields["mode"] = getattr(fields.get("mode"), "value", fields.get("mode"))
            # The real model serializes the results it was handed; the fixture
            # keeps only the fields the adapter reads back.
            fields["results"] = [
                {
                    "name": r.name,
                    "status": r.status,
                    "score": r.score,
                    "error": getattr(r, "error", None),
                }
                for r in fields.get("results") or []
            ]
            return json.dumps(fields, default=str)


    def mask_api_key(key):
        return key[:3] + "••••" if key else ""


    class DetectionReport(_Report):
        pass
    '''
)

# The scorer the adapter builds each report with, mirroring upstream's
# weighted average and verdict thresholds (DESIGN.md 5.2 / 5.3).
FIXTURE_SCORER = textwrap.dedent(
    '''
    """Fixture scorer."""


    def compute_total(results):
        valid = [r for r in results if r.status != "skip"]
        weights = sum(r.weight for r in valid)
        if not valid or weights <= 0:
            return 0.0
        return sum(r.score * r.weight for r in valid) / weights


    def effective_verdict(score, results):
        return "passed" if score >= 70 else "marginal" if score >= 50 else "failed"


    def fatal_run_error(results):
        return None


    def summary_text(score, verdict):
        if verdict == "passed" and score >= 85:
            return "优秀"
        if verdict == "passed":
            return "通过"
        if verdict == "marginal":
            return "基本合格"
        return "未达标"
    '''
)

# One fixture protocol module per protocol: the three entry points the adapter
# calls, plus the result objects the report is built from.
FIXTURE_PROTOCOL_MODULE = textwrap.dedent(
    '''
    """Fixture {protocol} suite entry points."""


    class _Result:
        def __init__(self, name, status, score):
            self.name = name
            self.status = status
            self.score = score
            self.weight = 1.0
            self.details = {{}}
            self.error = None

        def skip(self, reason):
            return _Result(self.name, "skip", 0.0)


    class _Client:
        def __init__(self, base_url, api_key, timeout):
            self.base_url = base_url
            self.api_key = api_key
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False


    class _Runner:
        def __init__(self, client, detectors, config):
            self.client = client
            self.detectors = detectors
            self.config = config

        async def run(self, model):
            import json

            class _Outcome:
                pass

            outcome = _Outcome()
            # The credential is echoed into a field the test reads back, which
            # is how it proves the suite ran in this process.
            outcome.results = [
                _Result("identity", "pass", 100.0),
                _Result("protocol", "fail", 0.0),
                _Result("message_id", "skip", 0.0),
            ]
            outcome.results[0].name = "identity"
            outcome.results[0].details = {{"response_text": self.client.api_key}}
            outcome.performance = {{"base_url": self.client.base_url}}
            return outcome


    def build_detectors(mode=None):
        return ["fixture-detector"]


    def build_runner(client, detectors, config):
        return _Runner(client, detectors, config)


    def make_client(base_url, api_key, timeout):
        return _Client(base_url, api_key, timeout)
    '''
)

FIXTURE_REPORT = 'def write_json(report, path) -> None:\n    raise NotImplementedError\n'

# The adapter imports this shim and the scorer behind it; the fixture mirrors
# upstream's layout so a rename is caught by the staging guard.
FIXTURE_SCORER_SHIM = 'from .core.scorer import *  # noqa: F403\n'
FIXTURE_CORE_SCORER = FIXTURE_SCORER

FIXTURE_PROTOCOL_INIT = textwrap.dedent(
    '''
    """Fixture protocol package."""

    from .config import {choices}
    from .detectors import build_all


    def build_detectors(mode=None):
        return build_all()


    def build_runner(client, detectors, config):
        from .runner import Runner

        return Runner(client, detectors, config)


    def make_client(base_url, api_key, timeout):
        from .client import Client

        return Client(base_url, api_key, timeout)
    '''
)

FIXTURE_PROTOCOL_RUNNER = textwrap.dedent(
    '''
    """Fixture protocol runner."""


    class _Result:
        def __init__(self, name, status, score, details=None):
            self.name = name
            self.status = status
            self.score = score
            self.weight = 1.0
            self.details = details or {}
            self.error = None


    class _Outcome:
        def __init__(self, client):
            self.client = client

        @property
        def results(self):
            return [
                _Result(
                    "identity",
                    "pass",
                    100.0,
                    # The credential is echoed back through the report's own
                    # identity field: a test reads it there to prove the suite
                    # ran inside this interpreter, with the route's credential.
                    {
                        "response_text": self.client.api_key,
                        "detected_non_anthropic_brands": ["Amazon Q"],
                    },
                ),
                _Result("protocol", "fail", 0.0),
                _Result("message_id", "skip", 0.0),
            ]


    class Runner:
        # The most recent runner, so a test can read back the config the
        # adapter passed without the adapter exposing it in the finding.
        last = None

        def __init__(self, client, detectors, config):
            self.client = client
            self.detectors = detectors
            self.config = config
            Runner.last = self

        async def run(self, model):
            outcome = _Outcome(self.client)
            outcome.performance = {"model": model}
            return outcome
    '''
)

FIXTURE_PROTOCOL_CLIENT = textwrap.dedent(
    '''
    """Fixture protocol client."""


    class Client:
        # The most recent client, so a test can prove the adapter handed the
        # suite the route's own address and credential.
        last = None

        def __init__(self, base_url, api_key, timeout):
            self.base_url = base_url
            self.api_key = api_key
            self.timeout = timeout
            Client.last = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False
    '''
)
FIXTURE_ANTHROPIC_CONFIG = textwrap.dedent(
    '''
    from dataclasses import dataclass


    @dataclass(frozen=True)
    class ModelInfo:
        alias: str


    MODELS = {
        "claude-opus-4-8": ModelInfo(alias="claude-opus-4-8"),
        "claude-haiku-4-5": ModelInfo(alias="claude-haiku-4-5"),
    }
    '''
)

FIXTURE_OPENAI_CONFIG = 'OPENAI_MODEL_CHOICES = [\n    "gpt-5.5",\n    "gpt-5.4-mini",\n]\n'
FIXTURE_GEMINI_CONFIG = 'GEMINI_MODEL_CHOICES = [\n    "gemini-3-pro-preview",\n]\n'

FIXTURE_LICENSE = "GNU AFFERO GENERAL PUBLIC LICENSE\nVersion 3, 19 November 2007\n"


def fixture_archive(
    *, with_models: bool = True, cli: str = FIXTURE_CLI, scorer: str | None = None
) -> bytes:
    """A minimal upstream archive with the same layout the updater reads."""

    files = {
        "src/relay_detector/__init__.py": '"""Fixture package."""\n',
        "src/relay_detector/cli.py": cli,
        "src/relay_detector/models.py": 'from .core.models import *  # noqa: F403\n',
        "src/relay_detector/core/models.py": FIXTURE_MODELS,
        "src/relay_detector/scorer.py": FIXTURE_SCORER_SHIM,
        "src/relay_detector/core/scorer.py": FIXTURE_CORE_SCORER if scorer is None else scorer,
        "src/relay_detector/report.py": FIXTURE_REPORT,
        "pyproject.toml": FIXTURE_PYPROJECT,
        "LICENSE": FIXTURE_LICENSE,
        "src/relay_detector/protocols/anthropic/data/test_document.pdf": "not a pdf",
    }
    for protocol in FIXTURE_PROTOCOL_CHOICES:
        base = f"src/relay_detector/protocols/{protocol}"
        files[f"{base}/__init__.py"] = FIXTURE_PROTOCOL_INIT.format(
            choices=FIXTURE_PROTOCOL_CHOICES[protocol]
        )
        files[f"{base}/runner.py"] = FIXTURE_PROTOCOL_RUNNER
        files[f"{base}/client.py"] = FIXTURE_PROTOCOL_CLIENT
        files[f"{base}/detectors/__init__.py"] = "def build_all():\n    return []\n"
    if with_models:
        files["src/relay_detector/protocols/anthropic/config.py"] = FIXTURE_ANTHROPIC_CONFIG
        files["src/relay_detector/protocols/openai/config.py"] = FIXTURE_OPENAI_CONFIG
        files["src/relay_detector/protocols/gemini/config.py"] = FIXTURE_GEMINI_CONFIG
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, payload in files.items():
            encoded = payload.encode("utf-8")
            info = tarfile.TarInfo(f"veridrop-{FIXTURE_REVISION}/{name}")
            info.size = len(encoded)
            archive.addfile(info, io.BytesIO(encoded))
    return buffer.getvalue()


def write_fixture_archive(directory: Path, **kwargs: object) -> Path:
    path = directory / "veridrop.tar.gz"
    path.write_bytes(fixture_archive(**kwargs))
    return path


def veridrop_update():
    """The staging module, imported from its script path."""

    import importlib.util

    spec = importlib.util.spec_from_file_location("update_veridrop_under_test", UPDATER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class StagingTests(unittest.TestCase):
    def test_extract_keeps_the_package_metadata_and_license(self) -> None:
        files = veridrop_update().extract(fixture_archive())
        self.assertIn("src/relay_detector/cli.py", files)
        self.assertIn("src/relay_detector/protocols/anthropic/data/test_document.pdf", files)
        self.assertIn("pyproject.toml", files)
        self.assertIn("LICENSE", files)
        # Nothing outside the package and its metadata is staged.
        self.assertFalse([name for name in files if name.startswith("tests/")])

    def test_supported_models_come_from_the_upstream_tables(self) -> None:
        update = veridrop_update()
        models = update.supported_models(update.extract(fixture_archive()))
        self.assertEqual(["claude-opus-4-8", "claude-haiku-4-5"], list(models["anthropic"]))
        self.assertEqual(["gpt-5.5", "gpt-5.4-mini"], list(models["openai"]))
        self.assertEqual(["gemini-3-pro-preview"], list(models["gemini"]))

    def test_a_restructured_table_fails_the_build(self) -> None:
        update = veridrop_update()
        files = update.extract(fixture_archive(with_models=False))
        with self.assertRaises(update.UpdateError):
            update.supported_models(files)

    def test_a_renamed_entry_point_fails_the_build(self) -> None:
        """The adapter drives these objects, so a rename is a build failure."""

        update = veridrop_update()
        for name, replacement, path in (
            ("async def _run_detect(", "async def run_scan(", "src/relay_detector/cli.py"),
            ("class ExecutionConfig", "class ScanConfig", "src/relay_detector/core/models.py"),
            ("class DetectionReport", "class ScanReport", "src/relay_detector/core/models.py"),
            ("def mask_api_key(", "def hide_key(", "src/relay_detector/core/models.py"),
            ("core.models", "another.models", "src/relay_detector/models.py"),
            ("def compute_total(", "def total_for(", "src/relay_detector/core/scorer.py"),
            ("def effective_verdict(", "def verdict_for_final(", "src/relay_detector/core/scorer.py"),
            ("def fatal_run_error(", "def blocking_error(", "src/relay_detector/core/scorer.py"),
            ("def summary_text(", "def summary_for(", "src/relay_detector/core/scorer.py"),
            ("core.scorer", "other.scorer", "src/relay_detector/scorer.py"),
        ):
            with self.subTest(marker=name):
                files = update.extract(fixture_archive())
                files[path] = files[path].replace(name.encode(), replacement.encode())
                with self.assertRaises(update.UpdateError):
                    update.validate_entry_points(files)

    def test_a_protocol_that_drops_a_suite_entry_point_fails_the_build(self) -> None:
        """Each protocol module must keep build_detectors/build_runner/make_client."""

        update = veridrop_update()
        for protocol in ("anthropic", "openai", "gemini"):
            for entry_point in ("build_detectors", "build_runner", "make_client"):
                with self.subTest(protocol=protocol, entry_point=entry_point):
                    files = update.extract(fixture_archive())
                    path = f"src/relay_detector/protocols/{protocol}/__init__.py"
                    files[path] = files[path].replace(
                        f"def {entry_point}(".encode(), f"def legacy_{entry_point}(".encode()
                    )
                    with self.assertRaises(update.UpdateError):
                        update.validate_entry_points(files)

    def test_the_entry_point_must_still_be_published(self) -> None:
        update = veridrop_update()
        files = update.extract(fixture_archive())
        files["pyproject.toml"] = b'[project]\nname = "relay-detector"\ndependencies = []\n'
        with self.assertRaises(update.UpdateError):
            update.entry_point(files)

    def test_staging_writes_the_source_the_manifest_and_the_license(self) -> None:
        update = veridrop_update()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = write_fixture_archive(root)
            output = root / "veridrop"
            code = update.main(["--archive-url", str(archive), "--output", str(output)])
            self.assertEqual(0, code)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue((output / "src" / "relay_detector" / "cli.py").is_file())
            self.assertEqual(FIXTURE_LICENSE, (output / "LICENSE").read_text(encoding="utf-8"))
            # Source only: the Core imports the package into its own interpreter.
            self.assertFalse((output / "deps").exists())
            self.assertFalse((output / "run.py").exists())
            self.assertEqual(FIXTURE_REVISION, manifest["revision"])
            self.assertEqual("AGPL-3.0-or-later", manifest["license"])
            self.assertEqual("relay_detector.cli:app", manifest["entry_point"])
            self.assertEqual("src", manifest["source_path"])
            self.assertEqual(
                ["httpx>=0.27", "typer>=0.12", "rapidfuzz>=3.6"], manifest["dependencies"]
            )
            self.assertEqual(
                {
                    "anthropic": ["claude-opus-4-8", "claude-haiku-4-5"],
                    "openai": ["gpt-5.5", "gpt-5.4-mini"],
                    "gemini": ["gemini-3-pro-preview"],
                },
                manifest["supported_models"],
            )
            self.assertIn("src/relay_detector/cli.py", manifest["files"])

    def test_missing_dependencies_exclude_what_the_runtime_already_carries(self) -> None:
        update = veridrop_update()
        with tempfile.TemporaryDirectory() as directory:
            site_packages = Path(directory) / "site-packages"
            (site_packages / "httpx-0.28.1.dist-info").mkdir(parents=True)
            (site_packages / "typer.py").write_text("", encoding="utf-8")
            missing = update.missing_dependencies(
                ["httpx>=0.27", "typer>=0.12", "rapidfuzz>=3.6", "rich>=13.7"], site_packages
            )
            self.assertEqual(["rapidfuzz", "rich"], missing)
            # An absent directory is simply an empty runtime, never an error.
            self.assertEqual(
                ["httpx", "typer"],
                update.missing_dependencies(["httpx>=0.27", "typer>=0.12"], Path(directory) / "nope"),
            )

    def test_completing_a_runtime_keeps_the_distributions_it_already_has(self) -> None:
        update = veridrop_update()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resolved = root / "resolved"
            destination = root / "site-packages"
            for name in ("rich", "typer"):
                (resolved / name).mkdir(parents=True)
                (resolved / f"{name}-9.9.9.dist-info").mkdir()
                (resolved / f"{name}-9.9.9.dist-info" / "METADATA").write_text("", encoding="utf-8")
            (resolved / "shellingham.py").write_text("", encoding="utf-8")
            (resolved / ".lock").write_text("", encoding="utf-8")
            destination.mkdir()
            (destination / "rich-13.7.1.dist-info").mkdir()
            (destination / "rich").mkdir()
            added = update.copy_absent(resolved, destination)
            # The runtime's own rich keeps both its code and its record, while
            # everything it never had is added.
            self.assertEqual(["shellingham.py", "typer", "typer-9.9.9.dist-info"], added)
            self.assertFalse((destination / "rich-9.9.9.dist-info").exists())
            self.assertFalse((destination / ".lock").exists())

    def test_the_missing_deps_mode_reports_the_staged_revision(self) -> None:
        update = veridrop_update()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = write_fixture_archive(root)
            output = root / "veridrop"
            update.main(["--archive-url", str(archive), "--output", str(output)])
            site_packages = root / "site-packages"
            (site_packages / "httpx-0.28.1.dist-info").mkdir(parents=True)
            (site_packages / "typer-0.19.0.dist-info").mkdir(parents=True)
            printed = io.StringIO()
            with mock.patch("sys.stdout", new=printed):
                self.assertEqual(
                    0,
                    update.main(["--output", str(output), "--missing-deps", str(site_packages)]),
                )
            self.assertEqual("rapidfuzz", printed.getvalue().strip())


class AdapterTests(unittest.TestCase):
    """The adapter imports the staged package into this interpreter."""

    MANIFEST = {
        "name": "Veridrop",
        "source": "https://github.com/canarybyte/veridrop",
        "revision": "abc123",
        "staged_at": "2026-01-01T00:00:00Z",
        "license": "AGPL-3.0-or-later",
        "source_path": "src",
        "supported_models": {
            "anthropic": ["claude-opus-4-8"],
            "openai": ["gpt-5.5"],
            "gemini": ["gemini-3-pro-preview"],
        },
    }

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = self.stage(Path(self._directory.name), self.MANIFEST)
        self.addCleanup(self._forget_upstream)

    def tearDown(self) -> None:
        self._forget_upstream()

    def _forget_upstream(self) -> None:
        """Drop the imported fixture so the next test stages a fresh one."""

        source = str(self.root / "src")
        if source in sys.path:
            sys.path.remove(source)
        for name in [name for name in sys.modules if name.split(".")[0] == "relay_detector"]:
            del sys.modules[name]

    def stage(self, directory: Path, manifest: dict[str, object], **kwargs: object) -> Path:
        """Write the fixture archive's payload as a staged tree."""

        import importlib.util

        spec = importlib.util.spec_from_file_location("update_veridrop_under_test", UPDATER)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        root = directory / "veridrop"
        module.stage(root, module.extract(fixture_archive(**kwargs)))
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return root

    def test_target_resolves_every_supported_protocol_and_normalizes_the_name(self) -> None:
        with mock.patch.dict(os.environ, {veridrop._VERIDROP_DIR_ENV: str(self.root)}):
            self.assertEqual(
                {"model": "claude-opus-4-8", "protocol": "anthropic"},
                veridrop.target("anthropic/claude-opus-4-8"),
            )
            self.assertEqual(
                {"model": "claude-opus-4-8", "protocol": "anthropic"},
                veridrop.target("[1m]claude-opus-4-8"),
            )
            self.assertEqual({"model": "gpt-5.5", "protocol": "openai"}, veridrop.target("gpt-5.5"))
            self.assertEqual(
                {"model": "gemini-3-pro-preview", "protocol": "gemini"},
                veridrop.target("gemini-3-pro-preview"),
            )
            self.assertIsNone(veridrop.target("gpt-6-astra"))
            self.assertIsNone(veridrop.target(None))
            self.assertTrue(veridrop.available())
            engine = veridrop.engine()
            self.assertEqual("Veridrop", engine["name"])
            self.assertEqual("quick", engine["mode"])
            self.assertEqual("AGPL-3.0-or-later", engine["license"])
            self.assertIn("anthropic:claude-opus-4-8", engine["targets"])

    def test_an_unstaged_checkout_reports_itself_instead_of_raising(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(
                os.environ, {veridrop._VERIDROP_DIR_ENV: str(Path(directory) / "missing")}
            ):
                self.assertFalse(veridrop.available())
                self.assertEqual({}, veridrop.supported_models())
                self.assertIsNone(veridrop.target("claude-opus-4-8"))
                self.assertFalse(veridrop.engine()["available"])
                with self.assertRaises(veridrop.VeridropUnavailable):
                    veridrop.run_quick(
                        base_url="https://api.example.test",
                        api_key="secret",
                        model="claude-opus-4-8",
                        protocol="anthropic",
                    )

    def test_a_package_that_cannot_be_imported_reads_as_unavailable(self) -> None:
        """A missing dependency is a skipped probe, not a crashed Core."""

        broken = self.stage(
            Path(tempfile.mkdtemp()),
            self.MANIFEST,
            scorer="import a_dependency_that_is_not_installed\n",
        )
        self.addCleanup(lambda: __import__("shutil").rmtree(broken.parent, ignore_errors=True))
        with mock.patch.dict(os.environ, {veridrop._VERIDROP_DIR_ENV: str(broken)}):
            self.assertFalse(veridrop.available())
            with self.assertRaises(veridrop.VeridropUnavailable):
                veridrop.run_quick(
                    base_url="https://api.example.test",
                    api_key="secret",
                    model="claude-opus-4-8",
                    protocol="anthropic",
                )

    def test_run_quick_drives_the_staged_suite_in_this_interpreter(self) -> None:
        """The adapter drives upstream's library entry points, not its CLI.

        The CLI renders a terminal report between the run and the JSON write,
        so a route that fails a detector loses the whole finding to a
        rendering error.  Driving the same objects upstream's own web
        application drives is what keeps a real mismatch reportable.
        """

        with mock.patch.dict(
            os.environ,
            {
                veridrop._VERIDROP_DIR_ENV: str(self.root),
                veridrop._VERIDROP_TIMEOUT_ENV: "42",
            },
        ):
            result = veridrop.run_quick(
                base_url="https://relay.example.test",
                api_key="secret-value",
                model="claude-opus-4-8",
                protocol="anthropic",
            )
            sys.path.insert(0, str(self.root / "src"))
            from relay_detector.protocols.anthropic.runner import Runner
            from relay_detector.protocols.anthropic.client import Client

        self.assertEqual("anthropic", result["protocol"])
        self.assertEqual("claude-opus-4-8", result["model"])
        # The suite received the route's own address, credential and timeout.
        self.assertEqual("https://relay.example.test", Client.last.base_url)
        self.assertEqual("secret-value", Client.last.api_key)
        self.assertEqual(42.0, Runner.last.config.overall_timeout_s)
        # 100 for identity and 0 for protocol, message_id skipped.
        self.assertEqual(50.0, result["score"])
        self.assertEqual("marginal", result["verdict"])
        self.assertEqual(["Amazon Q"], result["brands"])
        self.assertEqual(["message_id"], result["skipped"])
        self.assertEqual(["protocol"], result["failed"])
        self.assertEqual(3, len(result["detectors"]))

    def test_an_out_of_range_verdict_reads_as_inconclusive(self) -> None:
        normalized = veridrop._normalize_report({"verdict": "wat", "total_score": 10})
        self.assertEqual("marginal", normalized["verdict"])


if __name__ == "__main__":
    unittest.main()
