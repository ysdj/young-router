from __future__ import annotations

import os
import tempfile
import unittest

from litellm_menu import log_rotation


class LogRotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.log_path = os.path.join(self._tmpdir.name, "menu-server.log")
        for key in (
            log_rotation.LOG_MAX_BYTES_ENV,
            log_rotation.LOG_BACKUP_SEGMENTS_ENV,
            "LITELLM_MENU_SERVICE_LOG",
        ):
            os.environ.pop(key, None)
        self.addCleanup(lambda: os.environ.pop(key, None))

    def _rotate_times(self, times: int, *, maximum: int = 400) -> None:
        for index in range(times):
            payload = f"segment-{index}-" + ("x" * 200) + "\n"
            log_rotation.append_bounded_log(
                self.log_path,
                payload.encode("utf-8"),
                maximum_bytes=maximum,
            )
            # Keep the main file under the cap: append_bounded_log truncates
            # when the write would exceed it, which rotates the tail.
            size = os.path.getsize(self.log_path)
            if size > maximum:
                log_rotation.append_bounded_log(
                    self.log_path,
                    b"",
                    maximum_bytes=maximum,
                )

    def test_rotation_keeps_multiple_backup_segments(self) -> None:
        maximum = 400
        payload = "x" * 300
        for index in range(4):
            log_rotation.append_bounded_log(
                self.log_path,
                f"tail-{index}\n".encode("utf-8") + payload.encode("utf-8"),
                maximum_bytes=maximum,
            )
        self.assertTrue(os.path.exists(self.log_path + ".1"))
        self.assertTrue(os.path.exists(self.log_path + ".2"))
        self.assertFalse(os.path.exists(self.log_path + ".3"))
        backup_2 = open(self.log_path + ".2", "rb").read().decode("utf-8", "replace")
        backup_1 = open(self.log_path + ".1", "rb").read().decode("utf-8", "replace")
        # .2 holds an older tail than .1; both contain rotation content.
        self.assertIn("tail-", backup_1)
        self.assertIn("tail-", backup_2)
        self.assertNotEqual(backup_1, backup_2)

    def test_backup_segment_count_env_is_honored(self) -> None:
        os.environ[log_rotation.LOG_BACKUP_SEGMENTS_ENV] = "3"
        maximum = 400
        payload = "x" * 300
        for index in range(5):
            log_rotation.append_bounded_log(
                self.log_path,
                f"tail-{index}\n".encode("utf-8") + payload.encode("utf-8"),
                maximum_bytes=maximum,
            )
        self.assertTrue(os.path.exists(self.log_path + ".3"))
        self.assertFalse(os.path.exists(self.log_path + ".4"))

    def test_write_bounded_stream_rotates_managed_log(self) -> None:
        os.environ["LITELLM_MENU_SERVICE_LOG"] = self.log_path
        maximum = 400
        with open(self.log_path, "w", encoding="utf-8") as handle:
            handle.write("x" * 350)
            handle.flush()
            log_rotation.write_bounded_stream(handle, "y" * 100, maximum_bytes=maximum)
            handle.flush()
        # The write overflowed the cap: the previous tail moved to .1 and the
        # main file now holds the newest fragment.
        current = open(self.log_path, "rb").read().decode("utf-8", "replace")
        self.assertIn("y" * 100, current)
        backup = open(self.log_path + ".1", "rb").read().decode("utf-8", "replace")
        self.assertIn("x" * 350, backup)
        self.assertLessEqual(os.path.getsize(self.log_path), maximum)

    def test_backup_segments_clamped_to_sane_bounds(self) -> None:
        os.environ[log_rotation.LOG_BACKUP_SEGMENTS_ENV] = "99"
        self.assertEqual(log_rotation.log_backup_segments(), 8)
        os.environ[log_rotation.LOG_BACKUP_SEGMENTS_ENV] = "0"
        self.assertEqual(log_rotation.log_backup_segments(), 1)
        os.environ[log_rotation.LOG_BACKUP_SEGMENTS_ENV] = "abc"
        self.assertEqual(log_rotation.log_backup_segments(), 2)


if __name__ == "__main__":
    unittest.main()
