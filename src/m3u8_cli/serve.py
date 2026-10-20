"""RESTful 下载服务。"""

import json
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .download import DownloadOptions, download_hls
from .errors import DownloadCancelled, HLSException
from .paths import derive_task_paths

KEEP_TERMINAL = 200
TERMINAL_STATES = {"succeeded", "failed", "cancelled", "interrupted"}


class RequestError(ValueError):
    pass


class TaskConflict(RuntimeError):
    pass


class TaskNotFound(KeyError):
    pass


def utcnow():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class TaskRecord:
    id: str
    url: str
    format: str
    output: str
    state: str = "queued"
    final_path: str | None = None
    created_at: str = field(default_factory=utcnow)
    started_at: str | None = None
    finished_at: str | None = None
    completed: int = 0
    total: int = 0
    error: str | None = None

    def to_dict(self):
        return {
            "id": self.id,
            "url": self.url,
            "format": self.format,
            "output": self.output,
            "state": self.state,
            "final_path": self.final_path,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "completed": self.completed,
            "total": self.total,
            "error": self.error,
        }


class TaskManager:
    def __init__(self, output_dir, max_tasks=3, default_jobs=8,
                 state_file=None, runner=None):
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_tasks = max_tasks
        self.default_jobs = default_jobs
        self.state_file = (
            Path(state_file).expanduser()
            if state_file is not None
            else self.output_dir / ".m3u8-serve-state.json"
        )
        self.runner = runner or download_hls

        self.lock = threading.RLock()
        self.tasks = {}
        self.order = []
        self.queue = []
        self.running = set()
        self._params = {}
        self._cancels = {}
        self._dirty = False
        self._stop = threading.Event()

        self._load()
        self._flusher = threading.Thread(
            target=self._flush_loop, daemon=True
        )
        self._flusher.start()

    def _task_paths(self, fmt, output):
        if fmt == "mp4":
            final, cache = derive_task_paths(self.output_dir / output)
            return final, cache.parent
        task_dir = (self.output_dir / output).resolve()
        return task_dir / "playlist.m3u8", task_dir

    def submit(self, *, url, fmt, output, jobs, retries, timeout,
               overwrite, headers):
        if (
            not output
            or Path(output).name != output
            or output in (".", "..")
        ):
            raise RequestError("output 必须是纯文件名")

        final, task_dir = self._task_paths(fmt, output)
        key = str(task_dir)

        with self.lock:
            for task in self.tasks.values():
                if task.state in ("queued", "running"):
                    _, active_dir = self._task_paths(
                        task.format, task.output
                    )
                    if str(active_dir) == key:
                        raise TaskConflict(
                            f"输出正在被任务 {task.id} 使用"
                        )
            if not overwrite and final.exists():
                raise TaskConflict(
                    f"目标已存在：{final}，使用 overwrite 覆盖"
                )

            record = TaskRecord(
                id=uuid.uuid4().hex,
                url=url,
                format=fmt,
                output=output,
            )
            self.tasks[record.id] = record
            self.order.append(record.id)
            self.queue.append(record.id)
            self._params[record.id] = {
                "url": url,
                "format": fmt,
                "output": self.output_dir / output,
                "jobs": jobs,
                "retries": retries,
                "timeout": timeout,
                "headers": dict(headers),
                "overwrite": overwrite,
            }
            self._cancels[record.id] = threading.Event()
            self._dispatch_locked()
            self._save_locked()
            return record.to_dict()

    def _dispatch_locked(self):
        while len(self.running) < self.max_tasks and self.queue:
            task_id = self.queue.pop(0)
            task = self.tasks.get(task_id)
            if task is None or task.state != "queued":
                continue
            task.state = "running"
            task.started_at = utcnow()
            self.running.add(task_id)
            threading.Thread(
                target=self._run, args=(task_id,), daemon=True
            ).start()

    def _run(self, task_id):
        with self.lock:
            params = self._params.pop(task_id, None)
            cancel = self._cancels.get(task_id)
        if params is None or cancel is None:
            return

        options = DownloadOptions(**params)

        def on_progress(completed, total):
            with self.lock:
                task = self.tasks.get(task_id)
                if task is None or task.state != "running":
                    return
                task.completed = completed
                task.total = total
                self._dirty = True

        try:
            result = self.runner(
                options, on_progress=on_progress, cancel=cancel
            )
        except DownloadCancelled:
            self._finish(task_id, "cancelled")
        except Exception as exc:
            self._finish(task_id, "failed", error=str(exc))
        else:
            self._finish(
                task_id, "succeeded", final_path=str(result)
            )

    def _finish(self, task_id, state, error=None, final_path=None):
        with self.lock:
            task = self.tasks.get(task_id)
            self.running.discard(task_id)
            self._cancels.pop(task_id, None)
            if task is not None and task.state == "running":
                task.state = state
                task.finished_at = utcnow()
                task.error = error
                if final_path is not None:
                    task.final_path = final_path
            self._dispatch_locked()
            self._save_locked()

    def cancel(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                raise TaskNotFound(task_id)
            if task.state == "queued":
                self.queue.remove(task_id)
                task.state = "cancelled"
                task.finished_at = utcnow()
                self._params.pop(task_id, None)
                self._cancels.pop(task_id, None)
                self._save_locked()
                return task.to_dict()
            if task.state == "running":
                event = self._cancels.get(task_id)
                if event is not None:
                    event.set()
                return task.to_dict()
            raise TaskConflict(f"任务已结束：{task.state}")

    def get(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                raise TaskNotFound(task_id)
            return task.to_dict()

    def list(self, state=None, limit=50, offset=0):
        with self.lock:
            records = [
                self.tasks[i].to_dict()
                for i in reversed(self.order)
                if i in self.tasks
            ]
        if state is not None:
            records = [r for r in records if r["state"] == state]
        total = len(records)
        return records[offset:offset + limit], total

    def _load(self):
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text("utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for item in data.get("tasks", []):
            try:
                task = TaskRecord(**item)
            except TypeError:
                continue
            if task.state in ("queued", "running"):
                task.state = "interrupted"
                task.finished_at = task.finished_at or utcnow()
            self.tasks[task.id] = task
            self.order.append(task.id)
        self._prune_locked()

    def _prune_locked(self):
        terminal = [
            task_id for task_id in self.order
            if self.tasks.get(task_id) is not None
            and self.tasks[task_id].state in TERMINAL_STATES
        ]
        for task_id in terminal[:-KEEP_TERMINAL]:
            self.tasks.pop(task_id, None)
            self.order.remove(task_id)
            self._params.pop(task_id, None)
            self._cancels.pop(task_id, None)

    def _save_locked(self):
        payload = {
            "version": 1,
            "tasks": [
                self.tasks[i].to_dict()
                for i in self.order
                if i in self.tasks
            ],
        }
        temp = self.state_file.with_suffix(
            self.state_file.suffix + ".part"
        )
        try:
            temp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                "utf-8",
            )
            temp.replace(self.state_file)
        except OSError as exc:
            print(f"状态保存失败：{exc}")
        self._dirty = False

    def _flush_loop(self):
        while not self._stop.wait(2):
            with self.lock:
                if self._dirty:
                    self._save_locked()

    def close(self):
        self._stop.set()
        if self._flusher.is_alive():
            self._flusher.join(timeout=1)
        with self.lock:
            self._save_locked()
