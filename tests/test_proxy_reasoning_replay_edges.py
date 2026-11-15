from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path
from unittest import mock

from hook_test_utils import *

from young_router import runtime_store


class ReasoningReplayStoreFailureTests(HookTestCase):
    def setUp(self) -> None:
        super().setUp()
        hooks, _ = load_hook_module()
        self.module = hooks._reasoning_replay_module
        directory = tempfile.mkdtemp()
        self.state_path = Path(directory) / "routing.json"
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FILE_ENV, str(self.state_path))
        self.module._REQUIREMENTS.clear()
        self.module._REQUIREMENTS_LOADED_AT = 0.0
        self.addCleanup(self.module._REQUIREMENTS.clear)

    def test_a_recording_transaction_failure_does_not_fail_the_answer(self) -> None:
        failure = sqlite3.OperationalError("database is locked")
        with mock.patch.object(runtime_store, "update_document", side_effect=failure):
            self.module._record_replay_requirement(
                {"model_info": {"id": "route-a"}, "input": []}
            )
        self.assertIn("id:route-a:responses", self.module._REQUIREMENTS)

    def test_a_refresh_read_failure_keeps_the_in_process_copy(self) -> None:
        key = "id:route-a:responses"
        self.module._REQUIREMENTS[key] = time.time() + 60.0
        failure = sqlite3.OperationalError("database is locked")
        with mock.patch.object(runtime_store, "read_document", side_effect=failure):
            self.module._refresh_requirements_from_shared(time.time(), force=True)
        self.assertIn(key, self.module._REQUIREMENTS)
