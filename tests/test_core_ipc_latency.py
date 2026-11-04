"""Every Core IPC operation answers within its budget.

The settings window is built from IPC calls: the shell commits as the user
edits (``IMMEDIATE_APPLY_DEBOUNCE_MS``), every commit re-reads a snapshot, and
each pane lists, validates, and applies through the same contract.  A method
that reparses the 50 KB provider document, or walks a two-thousand-record model
catalog, on every call therefore reads as a frozen window rather than a slow
one, and no single call site shows it: the cost is spread over the readers that
each look reasonable on their own.

These tests pin the budget where it is observable, at the contract boundary.
They count parses and record visits first, because a count is deterministic and
names the regression exactly; the wall-clock assertions come last and are
generous, so a slow CI machine does not fail a build over scheduler noise while
a re-introduced half-second parse still does.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from young_router.core.domains.codex import CodexSettingsDomain
from young_router.core.domains.providers_models import ProvidersModelsDomain
from young_router.core.domains.runtime import RuntimeSettingsDomain
from young_router.core.service import CoreStore

#: What one operation may take.  The window commits on a 200 ms debounce and a
#: person waits on the result, so this is the number the shell's own cadence
#: already promises.
#:
#: The budget is measured in *CPU* time, not wall time.  A loaded machine adds
#: tens to hundreds of milliseconds of scheduling to a call that did nothing
#: wrong: the same commit measured 154 ms to 657 ms on this checkout purely from
#: load average, so a wall-clock bound fails for reasons unrelated to the code
#: and passes while a parse hides behind a swap.  CPU time is what this process
#: actually spends, which is the quantity a repeated derivation inflates.
IPC_BUDGET_MS = 200.0
#: Wall time is still bounded, but only as a sanity ceiling: it catches a call
#: that blocks on a socket or a lock, which CPU time cannot see.
WALL_CEILING_MS = 5_000.0

#: A document shaped like a real installation: several providers, anchored
#: values, and enough routes that a Python-side YAML parse is measurable.
PROVIDER_COUNT = 14
ROUTES_PER_PROVIDER = 6


def _provider_document() -> str:
    lines = ["providers:"]
    for provider in range(PROVIDER_COUNT):
        name = f"provider{provider}"
        lines.extend(
            [
                f"  {name}: &{name}_provider",
                f'    api_base: &{name}_base "https://relay{provider}.example.test/v1"',
                "    api_keys:",
            ]
        )
        for key in range(3):
            lines.append(
                f"        - name: key{key}\n"
                f"          value: &{name}_key{key} \"sk-replace-{provider}-{key}\""
            )
    lines.append("model_list:")
    for provider in range(PROVIDER_COUNT):
        name = f"provider{provider}"
        for route in range(ROUTES_PER_PROVIDER):
            lines.extend(
                [
                    f"  - model_name: public-{provider}-{route}",
                    "    litellm_params:",
                    f"      model: openai/upstream-{provider}-{route}",
                    f'      api_base: "https://relay{provider}.example.test/v1"',
                    f'      api_key: "sk-replace-{provider}-0"',
                    "    model_info:",
                    f"      id: {provider:02x}{route:02x}abcd",
                    f"      provider: {name}",
                    "      upstream_url_surface: openai/responses",
                ]
            )
    lines.extend(["general_settings:", "  master_key: sk-replace-master", ""])
    return "\n".join(lines)


class IpcLatencyTests(unittest.TestCase):
    """One Core over a realistic document, driven the way a window drives it."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        root = Path(self._directory.name)
        self.config_path = root / ".litellm-runtime" / "config.yaml"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(_provider_document(), encoding="utf-8")
        self.settings_path = root / "runtime-settings.env"
        self.settings_path.write_text("", encoding="utf-8")
        self.codex_home = root / "codex"
        self.codex_home.mkdir()
        self.root = root
        # ``local_exposed_models`` asks a running proxy for its listing.  These
        # tests are about this process's own work, so the read answers quickly
        # and identically every time instead of depending on a listener.
        self._models = mock.patch(
            "young_router.core.codex_config.local_exposed_models",
            return_value=(
                [f"public-{p}-{r}" for p in range(PROVIDER_COUNT) for r in range(ROUTES_PER_PROVIDER)],
                True,
            ),
        )
        self._models.start()
        self.addCleanup(self._models.stop)
        # ``local_proxy_endpoint`` resolves this installation's root from the
        # process environment, so pin it to this test's own directory.  Without
        # that, a reader that looks for the local endpoint (the Codex pane's
        # refresh, the service row's port) would answer out of whichever
        # installation the runner happens to have configured and this fixture
        # would never be the document under test.
        self._home = mock.patch.dict(
            os.environ,
            {
                "YOUNG_ROUTER_HOME": str(root),
                "LITELLM_RUNTIME_ROOT": str(root),
                "YOUNG_ROUTER_RUNTIME_SETTINGS_FILE": str(self.settings_path),
                "CODEX_HOME": str(self.codex_home),
            },
        )
        self._home.start()
        self.addCleanup(self._home.stop)
        os.environ.pop("LITELLM_CONFIG_FILE", None)
        self.core = CoreStore(
            domains=[
                ProvidersModelsDomain(self.config_path),
                CodexSettingsDomain(self.config_path, codex_home=self.codex_home),
                RuntimeSettingsDomain(self.settings_path),
            ],
        )
        self.core.snapshot()

    def _timed(self, operation, *, repeat: int = 5) -> float:
        """The slowest CPU time of ``repeat`` calls, in milliseconds.

        Wall time is asserted beside it only against ``WALL_CEILING_MS``: it is
        there to catch a call waiting on a socket, never to fail on scheduling.
        """

        operation()
        worst_cpu = 0.0
        worst_wall = 0.0
        for _ in range(repeat):
            wall_started = time.perf_counter()
            cpu_started = time.process_time()
            operation()
            worst_cpu = max(worst_cpu, (time.process_time() - cpu_started) * 1000.0)
            worst_wall = max(worst_wall, (time.perf_counter() - wall_started) * 1000.0)
        self.assertLess(
            worst_wall,
            WALL_CEILING_MS,
            f"an operation waited {worst_wall:.0f} ms of wall time; it is blocked",
        )
        return worst_cpu

    def test_a_snapshot_never_parses_the_provider_document(self) -> None:
        """The most-called method must not read the configuration at all.

        A snapshot has to rebuild the service row, which resolves the configured
        port, which resolved the master key by parsing ``config.yaml``.  Every
        window refresh paid it, three parses deep, on a document the editor had
        already parsed in the same request.
        """

        from young_router.core import codex_config

        with mock.patch.object(codex_config.yaml, "safe_load", wraps=codex_config.yaml.safe_load) as parse:
            self.core.snapshot()
            self.core.snapshot()

        self.assertEqual(0, parse.call_count)

    def test_one_reload_parses_the_provider_document_once(self) -> None:
        """A commit's own reload must share the parse its readers made.

        The Codex editor's runtime context, its saved-route list, the local
        endpoint, the runtime domain's master key, and the service port each
        read the document while one reload runs.  Each used to parse it.
        """

        from young_router.config import schema as config_schema

        self.core.reload()
        # Start from an empty cache, or this passes without the read it measures.
        config_schema._yaml_text_parsed.clear()
        with mock.patch.object(
            config_schema, "_yaml_load", wraps=config_schema._yaml_load
        ) as parse:
            self.core.reload()

        self.assertLessEqual(parse.call_count, 1)

    def test_snapshot_and_reload_stay_inside_the_ipc_budget(self) -> None:
        worst_snapshot = self._timed(self.core.snapshot, repeat=7)
        worst_reload = self._timed(self.core.reload, repeat=7)
        worst_all = self._timed(lambda: self.core.reload(), repeat=3)
        worst_providers = self._timed(lambda: self.core.reload("providers_models"), repeat=5)

        self.assertLess(worst_snapshot, IPC_BUDGET_MS, "snapshot exceeded the IPC budget")
        self.assertLess(worst_reload, IPC_BUDGET_MS, "reload exceeded the IPC budget")
        self.assertLess(worst_all, IPC_BUDGET_MS, "a full reload exceeded the IPC budget")
        self.assertLess(worst_providers, IPC_BUDGET_MS, "a provider reload exceeded the IPC budget")

    def test_validate_and_disk_state_stay_inside_the_ipc_budget(self) -> None:
        worst_validate = self._timed(
            lambda: self.core.validate("providers_models"), repeat=7
        )
        worst_disk = self._timed(
            lambda: self.core.disk_state(["providers_models", "codex", "runtime"]),
            repeat=7,
        )

        self.assertLess(worst_validate, IPC_BUDGET_MS, "validate exceeded the IPC budget")
        self.assertLess(worst_disk, IPC_BUDGET_MS, "disk_state exceeded the IPC budget")

    def test_a_staged_edit_commits_inside_the_ipc_budget(self) -> None:
        """The debounced auto-apply is the path every keystroke ends on."""

        def commit() -> None:
            staged = self.core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.patch",
                    "payload": {
                        "provider_id": "provider0",
                        "changes": {"enabled": True},
                    },
                }
            )
            self.core.apply("providers_models", revision=staged["revision"])

        worst = self._timed(commit, repeat=3)

        self.assertLess(worst, IPC_BUDGET_MS, "a staged commit exceeded the IPC budget")


if __name__ == "__main__":
    unittest.main()
