from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from young_router.core import CoreStore
from young_router.core.image_context_cleanup import ImageContextSweeper
from young_router.core.scheduled_tasks import IMAGE_CONTEXT_CLEANUP, WEBDAV_SYNC, ScheduledTasks
from young_router.core.webdav_scheduler import CONFLICT_CODE, WebDAVSyncScheduler


def task_row(tasks: ScheduledTasks, task_id: str) -> dict:
    return next(row for row in tasks.view(None)[1] if row["id"] == task_id)


class ScheduledTaskTests(unittest.TestCase):
    def test_history_survives_a_restart_but_live_state_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            tasks = ScheduledTasks(path)
            tasks.plan(IMAGE_CONTEXT_CLEANUP, interval_seconds=3600, delay_seconds=0)
            for _ in range(22):
                tasks.begin(IMAGE_CONTEXT_CLEANUP)
                tasks.finish(IMAGE_CONTEXT_CLEANUP)
            tasks.begin(IMAGE_CONTEXT_CLEANUP)
            tasks.finish(IMAGE_CONTEXT_CLEANUP, "failed")
            before = task_row(tasks, IMAGE_CONTEXT_CLEANUP)

            core = CoreStore(scheduled_tasks_path=path)
            view = core.log_view("scheduled-tasks")
            after = next(row for row in view["log"]["records"] if row["id"] == IMAGE_CONTEXT_CLEANUP)
            self.assertEqual(before["history"], after["history"])
            self.assertEqual(20, len(after["history"]))
            self.assertEqual("failed", after["history"][0]["outcome"])
            self.assertEqual("idle", after["state"])
            self.assertIsNone(after["next_run_at"])
            self.assertFalse(core.log_view("scheduled-tasks", view["revision"])["changed"])

    def test_webdav_reports_the_real_deadline_and_conflict(self):
        now = 1_700_000_000.0
        tasks = ScheduledTasks()
        state = {"enabled": True, "configured": True, "sync_interval_minutes": 30}
        scheduler = WebDAVSyncScheduler(
            snapshot=lambda: state, sync=lambda: CONFLICT_CODE, initial_delay=1200,
            clock=lambda: now, tasks=tasks,
        )
        with mock.patch("young_router.core.scheduled_tasks.time.time", return_value=now):
            self.assertEqual((300, False), scheduler.next_run())
            planned = task_row(tasks, WEBDAV_SYNC)
        self.assertEqual("waiting", planned["state"])
        self.assertEqual(now + 1200, datetime.fromisoformat(planned["next_run_at"]).timestamp())

        now += 1200
        self.assertEqual((0, True), scheduler.next_run())
        scheduler._run()
        self.assertEqual((None, False), scheduler.next_run())
        blocked = task_row(tasks, WEBDAV_SYNC)
        self.assertEqual(("blocked", None), (blocked["state"], blocked["next_run_at"]))
        self.assertEqual("failed", blocked["history"][0]["outcome"])

    def test_failure_detail_is_redacted_before_persistence(self):
        def fail():
            raise PermissionError("Failed to remove the cached image: token=fixture-only-token")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            sweeper = ImageContextSweeper(sweep=fail, tasks=ScheduledTasks(path))
            with mock.patch("young_router.core.scheduled_tasks.time.time", side_effect=(1_700_000_000.0, 1_700_000_000.125)):
                sweeper.run_once()

            run = task_row(ScheduledTasks(path), IMAGE_CONTEXT_CLEANUP)["history"][0]
            self.assertEqual("failed", run["outcome"])
            self.assertEqual(125, run["duration_ms"])
            self.assertEqual("sweep", run["detail"]["phase"])
            self.assertEqual("PermissionError", run["detail"]["error_type"])
            self.assertIn("Failed to remove the cached image", run["detail"]["message"])
            self.assertIn("token=configured", run["detail"]["message"])
            self.assertNotIn("fixture-only-token", path.read_text())

    def test_old_failure_has_a_duration_but_no_invented_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            path.write_text(json.dumps({IMAGE_CONTEXT_CLEANUP: [{
                "started_at": "2026-01-01T00:00:00.000+00:00",
                "finished_at": "2026-01-01T00:00:00.125+00:00",
                "outcome": "failed",
            }]}))
            run = task_row(ScheduledTasks(path), IMAGE_CONTEXT_CLEANUP)["history"][0]
            self.assertEqual(125, run["duration_ms"])
            self.assertNotIn("detail", run)
            self.assertNotIn("issues", run)
