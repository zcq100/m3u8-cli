import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from m3u8_cli.errors import DownloadCancelled
from m3u8_cli.serve import (
    RequestError,
    TaskConflict,
    TaskManager,
    TaskNotFound,
)


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
        started = threading.Event()

        def runner(options, on_progress=None, cancel=None):
            started.set()
            if wait_for(cancel.is_set, timeout=2):
                raise DownloadCancelled("cancelled")
            raise AssertionError("未被取消")

        with tempfile.TemporaryDirectory() as tmp:
            manager = TaskManager(tmp, max_tasks=1, runner=runner)
            first = submit(manager, "one")
            self.assertTrue(started.wait(1))
            second = submit(manager, "two")
            self.assertEqual(
                manager.get(second["id"])["state"], "queued"
            )

            manager.cancel(second["id"])
            self.assertEqual(
                manager.get(second["id"])["state"], "cancelled"
            )
            self.assertNotIn(second["id"], manager._params)

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
                "tasks": [
                    {
                        "id": "abc",
                        "url": "https://example.com/a.m3u8",
                        "format": "mp4",
                        "output": "output.mp4",
                        "state": "running",
                    },
                    {
                        "id": "def",
                        "url": "https://example.com/b.m3u8",
                        "format": "hls",
                        "output": "other",
                        "state": "queued",
                    },
                ],
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
            self.assertEqual(
                manager.get("def")["state"], "interrupted"
            )
            manager.close()

    def test_invalid_output_name_rejected(self):
        def runner(options, on_progress=None, cancel=None):
            return options.output

        with tempfile.TemporaryDirectory() as tmp:
            manager = TaskManager(tmp, max_tasks=1, runner=runner)
            for output in (
                "../evil", "a/b", "..", "", "a\\b", "a\x00b"
            ):
                with self.assertRaises(RequestError):
                    submit(manager, output)
            manager.close()

    def test_submit_after_close_rejected(self):
        def runner(options, on_progress=None, cancel=None):
            return options.output

        with tempfile.TemporaryDirectory() as tmp:
            manager = TaskManager(tmp, max_tasks=1, runner=runner)
            manager.close()
            with self.assertRaises(RequestError):
                submit(manager, "movie")

    def test_close_marks_unfinished_tasks_interrupted(self):
        def runner(options, on_progress=None, cancel=None):
            wait_for(cancel.is_set, timeout=2)
            return options.output

        with tempfile.TemporaryDirectory() as tmp:
            manager = TaskManager(tmp, max_tasks=1, runner=runner)
            record = submit(manager, "movie")
            self.assertTrue(wait_for(
                lambda: manager.get(record["id"])["state"]
                == "running"
            ))
            manager.close(timeout=0.1)
            self.assertEqual(
                manager.get(record["id"])["state"], "interrupted"
            )

    def test_unknown_task_raises(self):
        def runner(options, on_progress=None, cancel=None):
            return options.output

        with tempfile.TemporaryDirectory() as tmp:
            manager = TaskManager(tmp, max_tasks=1, runner=runner)
            with self.assertRaises(TaskNotFound):
                manager.get("missing")
            with self.assertRaises(TaskNotFound):
                manager.cancel("missing")
            manager.close()

    def test_mp4_and_hls_share_task_dir_collision(self):
        gate = threading.Event()

        def runner(options, on_progress=None, cancel=None):
            gate.wait(2)
            return options.output

        with tempfile.TemporaryDirectory() as tmp:
            manager = TaskManager(tmp, max_tasks=1, runner=runner)
            submit(manager, "movie", fmt="hls")
            with self.assertRaises(TaskConflict):
                submit(manager, "movie.mp4", fmt="mp4")
            gate.set()
            manager.close()

    def test_existing_mp4_target_conflicts_without_overwrite(self):
        def runner(options, on_progress=None, cancel=None):
            return options.output

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "movie"
            target.mkdir()
            (target / "movie.mp4").write_text("old")

            manager = TaskManager(tmp, max_tasks=1, runner=runner)
            with self.assertRaises(TaskConflict):
                submit(manager, "movie.mp4", fmt="mp4")
            manager.close()

    def test_state_file_excludes_headers(self):
        def runner(options, on_progress=None, cancel=None):
            return options.output

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "nested" / "state.json"
            manager = TaskManager(
                tmp, max_tasks=1, state_file=state, runner=runner
            )
            record = manager.submit(
                url="https://example.com/a.m3u8",
                fmt="hls",
                output="movie",
                jobs=1,
                retries=3,
                timeout=30,
                overwrite=False,
                headers={"Cookie": "secret-cookie"},
            )
            self.assertTrue(wait_for(
                lambda: manager.get(record["id"])["state"]
                == "succeeded"
            ))
            manager.close()

            text = state.read_text("utf-8")
            payload = json.loads(text)
            self.assertEqual(payload["version"], 1)
            self.assertNotIn("secret-cookie", text)
            self.assertNotIn("headers", text)
            self.assertFalse(
                state.with_suffix(".json.part").exists()
            )

    def test_list_ordering_limit_and_state_filter(self):
        def runner(options, on_progress=None, cancel=None):
            return options.output

        with tempfile.TemporaryDirectory() as tmp:
            manager = TaskManager(tmp, max_tasks=3, runner=runner)
            ids = [
                submit(manager, name)["id"]
                for name in ("one", "two", "three")
            ]
            for task_id in ids:
                self.assertTrue(wait_for(
                    lambda tid=task_id: manager.get(tid)["state"]
                    == "succeeded"
                ))

            records, total = manager.list()
            self.assertEqual(total, 3)
            self.assertEqual(
                [r["id"] for r in records], list(reversed(ids))
            )

            page, total = manager.list(limit=1, offset=1)
            self.assertEqual(total, 3)
            self.assertEqual([r["id"] for r in page], [ids[1]])

            filtered, total = manager.list(state="succeeded")
            self.assertEqual(total, 3)

            empty, total = manager.list(state="queued")
            self.assertEqual(empty, [])
            self.assertEqual(total, 0)
            manager.close()

    def test_terminal_tasks_pruned_to_200(self):
        def runner(options, on_progress=None, cancel=None):
            return options.output

        with tempfile.TemporaryDirectory() as tmp:
            manager = TaskManager(tmp, max_tasks=16, runner=runner)
            for i in range(205):
                submit(manager, f"movie-{i}")
            self.assertTrue(wait_for(
                lambda: all(
                    task["state"] in
                    ("succeeded", "failed", "cancelled")
                    for task in manager.list(limit=500)[0]
                ),
                timeout=10,
            ))
            tasks, total = manager.list(limit=500)
            self.assertEqual(total, 200)
            self.assertEqual(len(tasks), 200)
            manager.close()


if __name__ == "__main__":
    unittest.main()
