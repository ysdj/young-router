"""Edge cases for the raw external-client document adapter.

``reload`` reads every registered client file.  Reading a file twice can leave
the text a pane edits and the bytes Apply compares against describing two
different versions of it.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from young_router.core.domains import clients as clients_module
from young_router.core.domains.clients import ClientSettingsDomain


def _client_environment(directory: str) -> dict[str, str]:
    return {
        "PI_CODING_AGENT_DIR": str(Path(directory) / "pi"),
        "DSH_HOME": str(Path(directory) / "dsh"),
        "OPENCODE_CONFIG_DIR": str(Path(directory) / "opencode-config"),
        "OPENCODE_DATA_DIR": str(Path(directory) / "opencode-data"),
        "CODEX_HOME": str(Path(directory) / "codex"),
    }


class ClientSettingsReloadReadTests(unittest.TestCase):
    def test_reload_reads_each_registered_file_once(self) -> None:
        original = clients_module.read_bytes
        reads: list[Path] = []

        def counting_read_bytes(path: Path | str, **kwargs: object) -> bytes | None:
            reads.append(Path(path))
            return original(path, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, _client_environment(directory)):
                with mock.patch.object(clients_module, "read_bytes", counting_read_bytes):
                    domain = ClientSettingsDomain()

        # One read per file: both baselines come from it, so they cannot
        # describe different versions of the same file.
        self.assertEqual(sorted(domain.persistence_paths()), sorted(reads))


if __name__ == "__main__":
    unittest.main()
