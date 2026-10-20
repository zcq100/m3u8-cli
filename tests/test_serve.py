import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from m3u8_cli.errors import DownloadCancelled
from m3u8_cli.serve import TaskConflict, TaskManager, TaskNotFound


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def submit(manager, output, fmt="hls", overwrite=False):
    return manager.submit(
        url="https://example.com/a.m3u8",
        fmt=fmt,
        output=output,
        jobs=1,
        retries=3,
        timeout=30,
        overwrite=overwrite,
        headers={},
    )


class TaskManagerTests(unittest.TestCase):
    def test_success_updates_state_and_progress(self):
        def runner(options, on_progress=None, cancel=None):
            on_progress(1, 2)
            on_progress(2, 2)
            return options.output

        with tempfile.TemporaryDirectory() as tmp:
            manager = TaskManager(tmp, max_tasks=1, runner=runner)
            record = submit(manager, "movie")

            self.assertTrue(wait_for(
                lambda: manager.get(record["id"])["state"]
                == "succeeded"
            ))
            task = manager.get(record["id"])
            self.assertEqual(task["completed"], 2)
            self.assertEqual(task["total"], 2)
            self.assertEqual(
                task["final_path"], str(Path(tmp) / "movie")
            )
            manager.close()

    def test_queue_respects_max_tasks(self):
        gate = threading.Event()

        def runner(options, on_progress=None, cancel=None):
            gate.wait(2)
            return options.output

        with tempfile.TemporaryDirectory() as tmp:
            manager = TaskManager(tmp, max_tasks=1, runner=runner)
            first = submit(manager, "one")
            second = submit(manager, "two")

            self.assertTrue(wait_for(
                lambda: manager.get(first["id"])["state"] == "running"
            ))
            self.assertEqual(
                manager.get(second["id"])["state"], "queued"
            )

            gate.set()
            self.assertTrue(wait_for(
                lambda: manager.get(second["id"])["state"]
                == "succeeded"
            ))
            manager.close()

    def test_cancel_queued_task(self):
        def runner(options, on_progress=None, cancel=None):
            if wait_for(cancel.is_set, timeout=2):
                raise DownloadCancelled("cancelled")
            raise AssertionError("未被取消")

        with tempfile.TemporaryDirectory() as tmp:
            manager = TaskManager(tmp, max_tasks=1, runner=runner)
            first = submit(manager, "one")
            second = submit(manager, "two")
            self.assertTrue(wait_for(
                lambda: manager.get(first["id"])["state"] == "running"
            ))

            manager.cancel(second["id"])
            self.assertEqual(
                manager.get(second["id"])["state"], "cancelled"
            )
            manager.cancel(first["id"])
            self.assertTrue(wait_for(
                lambda: manager.get(first["id"])["state"]
                == "cancelled"
            ))
            manager.close()

    def test_cancel_running_task(self):
        started = threading.Event()

        def runner(options, on_progress=None, cancel=None):
            started.set()
            if wait_for(cancel.is_set, timeout=2):
                raise DownloadCancelled("cancelled")
            raise AssertionError("未被取消")

        with tempfile.TemporaryDirectory() as tmp:
            manager = TaskManager(tmp, max_tasks=1, runner=runner)
            record = submit(manager, "movie")
            self.assertTrue(started.wait(1))

            manager.cancel(record["id"])
            self.assertTrue(wait_for(
                lambda: manager.get(record["id"])["state"]
                == "cancelled"
            ))
            manager.close()

    def test_duplicate_output_conflicts(self):
        gate = threading.Event()

        def runner(options, on_progress=None, cancel=None):
            gate.wait(2)
            return options.output

        with tempfile.TemporaryDirectory() as tmp:
            manager = TaskManager(tmp, max_tasks=1, runner=runner)
            submit(manager, "movie")
            with self.assertRaises(TaskConflict):
                submit(manager, "movie")
            gate.set()
            manager.close()

    def test_existing_target_conflicts_without_overwrite(self):
        def runner(options, on_progress=None, cancel=None):
            return options.output

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "movie"
            target.mkdir()
            (target / "playlist.m3u8").write_text("old")

            manager = TaskManager(tmp, max_tasks=1, runner=runner)
            with self.assertRaises(TaskConflict):
                submit(manager, "movie")
            manager.close()

    def test_reload_marks_unfinished_tasks_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(json.dumps({
                "version": 1,
                "tasks": [{
                    "id": "abc",
                    "url": "https://example.com/a.m3u8",
                    "format": "mp4",
                    "output": "output.mp4",
                    "state": "running",
                }],
            }))

            manager = TaskManager(
                tmp,
                max_tasks=1,
                state_file=state,
                runner=lambda *a, **k: None,
            )
            self.assertEqual(
                manager.get("abc")["state"], "interrupted"
            )
            manager.close()


if __name__ == "__main__":
    unittest.main()
