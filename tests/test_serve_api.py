import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

import requests

from m3u8_cli.serve import TaskManager, make_handler


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = TaskManager(
            self.tmp.name,
            max_tasks=2,
            default_jobs=2,
            runner=lambda options, on_progress=None, cancel=None:
                options.output,
        )
        self.token = "test-token"
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(self.manager, self.token),
        )
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        self.base = (
            f"http://127.0.0.1:{self.server.server_address[1]}"
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.manager.close()
        self.tmp.cleanup()

    def auth(self):
        return {"Authorization": f"Bearer {self.token}"}

    def test_requires_token(self):
        response = requests.get(f"{self.base}/api/tasks")
        self.assertEqual(response.status_code, 401)

    def test_options_preflight(self):
        response = requests.options(f"{self.base}/api/tasks")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            response.headers["Access-Control-Allow-Origin"], "*"
        )

    def test_create_list_and_get_task(self):
        response = requests.post(
            f"{self.base}/api/tasks",
            headers=self.auth(),
            json={
                "url": "https://example.com/a.m3u8",
                "format": "hls",
                "output": "movie",
            },
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task"]["id"]

        self.assertTrue(wait_for(
            lambda: requests.get(
                f"{self.base}/api/tasks/{task_id}",
                headers=self.auth(),
            ).json()["task"]["state"] == "succeeded"
        ))

        listing = requests.get(
            f"{self.base}/api/tasks", headers=self.auth()
        ).json()
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["tasks"][0]["id"], task_id)

    def test_invalid_url_rejected(self):
        response = requests.post(
            f"{self.base}/api/tasks",
            headers=self.auth(),
            json={"url": "ftp://example.com/a.m3u8"},
        )
        self.assertEqual(response.status_code, 400)

    def test_invalid_output_rejected(self):
        response = requests.post(
            f"{self.base}/api/tasks",
            headers=self.auth(),
            json={
                "url": "https://example.com/a.m3u8",
                "output": "../escape.mp4",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_existing_target_conflicts(self):
        from pathlib import Path

        target = Path(self.tmp.name) / "movie"
        target.mkdir()
        (target / "playlist.m3u8").write_text("old")

        response = requests.post(
            f"{self.base}/api/tasks",
            headers=self.auth(),
            json={
                "url": "https://example.com/a.m3u8",
                "format": "hls",
                "output": "movie",
            },
        )
        self.assertEqual(response.status_code, 409)

    def test_delete_terminal_task_conflicts(self):
        response = requests.post(
            f"{self.base}/api/tasks",
            headers=self.auth(),
            json={
                "url": "https://example.com/a.m3u8",
                "format": "hls",
                "output": "movie",
            },
        )
        task_id = response.json()["task"]["id"]
        self.assertTrue(wait_for(
            lambda: requests.get(
                f"{self.base}/api/tasks/{task_id}",
                headers=self.auth(),
            ).json()["task"]["state"] == "succeeded"
        ))

        response = requests.delete(
            f"{self.base}/api/tasks/{task_id}", headers=self.auth()
        )
        self.assertEqual(response.status_code, 409)

    def test_unknown_task_not_found(self):
        response = requests.get(
            f"{self.base}/api/tasks/missing", headers=self.auth()
        )
        self.assertEqual(response.status_code, 404)

    def test_oversized_body_rejected(self):
        response = requests.post(
            f"{self.base}/api/tasks",
            headers=self.auth(),
            data=b"x" * (1024 * 1024 + 1),
        )
        self.assertEqual(response.status_code, 413)


if __name__ == "__main__":
    unittest.main()
