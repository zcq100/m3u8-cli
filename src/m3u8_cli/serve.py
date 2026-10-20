"""RESTful 下载服务。"""

import hmac
import json
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .download import DownloadOptions, download_hls
from .errors import DownloadCancelled, HLSException
from .paths import fit_output_name

KEEP_TERMINAL = 200
TERMINAL_STATES = {"succeeded", "failed", "cancelled", "interrupted"}


class RequestError(ValueError):
    pass


class TaskConflict(RuntimeError):
    pass


class TaskNotFound(KeyError):
    pass


def valid_output_name(name):
    return (
        isinstance(name, str)
        and name not in ("", ".", "..")
        and len(name) <= 200
        and "/" not in name
        and "\\" not in name
        and "\x00" not in name
        and not name.endswith((".", " "))
    )


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
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
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
        self._workers = {}
        self._closed = False

        self._load()
        self._flusher = threading.Thread(
            target=self._flush_loop, daemon=True
        )
        self._flusher.start()

    def _task_paths(self, fmt, output):
        """返回 (最终产物路径, 冲突检测键路径)。

        mp4 直接落在输出目录、缓存在系统临时目录，键即最终文件本身；
        hls 的产物是一个任务目录。
        """
        path = (self.output_dir / output).resolve()
        if fmt == "mp4":
            return path, path
        return path / "playlist.m3u8", path

    def submit(self, *, url, fmt, output, jobs, retries, timeout,
               overwrite, headers, insecure=False):
        if self._closed:
            raise RequestError("服务正在关闭")
        if fmt not in ("mp4", "hls"):
            raise RequestError(f"不支持的格式：{fmt}")
        if not valid_output_name(output):
            raise RequestError("output 必须是纯文件名")
        try:
            # 标题派生的长文件名在 Windows 上可能让派生路径超过 MAX_PATH。
            output = fit_output_name(output, self.output_dir)
        except HLSException as exc:
            raise RequestError(str(exc)) from exc

        final, task_dir = self._task_paths(fmt, output)
        key = str(task_dir)

        with self.lock:
            if fmt == "hls" and task_dir.exists() and not task_dir.is_dir():
                raise TaskConflict(
                    f"目标路径不是目录：{task_dir}"
                )
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
                "verify": not insecure,
            }
            self._cancels[record.id] = threading.Event()
            self._dispatch_locked()
            self._save_locked()
            return record.to_dict()

    def _dispatch_locked(self):
        if self._closed:
            return
        while len(self.running) < self.max_tasks and self.queue:
            task_id = self.queue.pop(0)
            task = self.tasks.get(task_id)
            if task is None or task.state != "queued":
                continue
            task.state = "running"
            task.started_at = utcnow()
            self.running.add(task_id)
            thread = threading.Thread(
                target=self._run, args=(task_id,), daemon=True
            )
            self._workers[task_id] = thread
            thread.start()

    def _run(self, task_id):
        with self.lock:
            params = self._params.pop(task_id, None)
            cancel = self._cancels.get(task_id)
        if params is None or cancel is None:
            self._finish(task_id, "failed", error="任务参数缺失")
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
            self._workers.pop(task_id, None)
            self._cancels.pop(task_id, None)
            if task is not None and task.state == "running":
                task.state = state
                task.finished_at = utcnow()
                task.error = error
                if final_path is not None:
                    task.final_path = final_path
            self._dispatch_locked()
            self._prune_locked()
            self._save_locked()

    def cancel(self, task_id):
        with self.lock:
            if self._closed:
                raise TaskConflict("服务正在关闭")
            task = self.tasks.get(task_id)
            if task is None:
                raise TaskNotFound(task_id)
            if task.state == "queued":
                self.queue.remove(task_id)
                task.state = "cancelled"
                task.finished_at = utcnow()
                self._params.pop(task_id, None)
                self._cancels.pop(task_id, None)
                self._prune_locked()
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
        temp = self.state_file.with_name(
            self.state_file.name + ".part"
        )
        try:
            temp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                "utf-8",
            )
            temp.replace(self.state_file)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            print(f"状态保存失败：{exc}")
            return
        self._dirty = False

    def _flush_loop(self):
        while not self._stop.wait(2):
            with self.lock:
                if self._dirty:
                    self._save_locked()

    def close(self, timeout=5.0):
        with self.lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            for event in self._cancels.values():
                event.set()
            for task in self.tasks.values():
                if task.state in ("queued", "running"):
                    task.state = "interrupted"
                    task.finished_at = (
                        task.finished_at or utcnow()
                    )
            self._prune_locked()
            workers = list(self._workers.values())

        deadline = time.monotonic() + timeout
        for thread in workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)

        with self.lock:
            self._save_locked()


MAX_BODY = 1024 * 1024
MAX_HEADERS_BYTES = 16 * 1024


def _int_field(payload, name, default, minimum, maximum):
    value = payload.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise RequestError(f"{name} 必须是整数")
    if value < minimum or value > maximum:
        raise RequestError(
            f"{name} 必须在 {minimum} 到 {maximum} 之间"
        )
    return value


def parse_task_request(payload, default_jobs=8):
    """校验 POST /api/tasks 的请求体并返回 submit 参数。"""
    if not isinstance(payload, dict):
        raise RequestError("请求体必须是 JSON 对象")

    url = payload.get("url")
    if not isinstance(url, str) or not url:
        raise RequestError("url 不能为空")
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        parts.port
    except ValueError as exc:
        raise RequestError("url 无效") from exc
    if (
        parts.scheme not in ("http", "https")
        or not parts.netloc
        or not hostname
    ):
        raise RequestError("url 必须是 http 或 https 地址")
    if any(
        ch.isspace() or ord(ch) < 32 for ch in hostname
    ):
        raise RequestError("url 无效")

    fmt = payload.get("format", "mp4")
    if fmt not in ("mp4", "hls"):
        raise RequestError("format 必须是 mp4 或 hls")

    output = payload.get("output")
    if output is None:
        output = "output" if fmt == "hls" else "output.mp4"
    if not valid_output_name(output):
        raise RequestError("output 必须是纯文件名")

    overwrite = payload.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise RequestError("overwrite 必须是布尔值")

    insecure = payload.get("insecure", False)
    if not isinstance(insecure, bool):
        raise RequestError("insecure 必须是布尔值")

    headers = payload.get("headers", {})
    if not isinstance(headers, dict) or not all(
        isinstance(k, str) and isinstance(v, str)
        for k, v in headers.items()
    ):
        raise RequestError("headers 必须是字符串映射")
    for name, value in headers.items():
        if any(
            ch in name or ch in value
            for ch in ("\r", "\n", "\x00")
        ):
            raise RequestError("headers 包含非法字符")
    if sum(
        len(k) + len(v) for k, v in headers.items()
    ) > MAX_HEADERS_BYTES:
        raise RequestError("headers 过大")

    return {
        "url": url,
        "fmt": fmt,
        "output": output,
        "jobs": _int_field(payload, "jobs", default_jobs, 1, 64),
        "retries": _int_field(payload, "retries", 3, 0, 10),
        "timeout": _int_field(payload, "timeout", 30, 1, 600),
        "overwrite": overwrite,
        "insecure": insecure,
        "headers": headers,
    }


def make_handler(manager, token):
    """创建绑定到指定 TaskManager 与 token 的请求处理器。"""
    if not token:
        raise ValueError("token 不能为空")
    valid_states = {
        "queued", "running", "succeeded",
        "failed", "cancelled", "interrupted",
    }

    class ApiHandler(BaseHTTPRequestHandler):
        server_version = "m3u8-cli"

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Authorization, Content-Type",
            )
            self.send_header(
                "Access-Control-Allow-Methods",
                "GET, POST, DELETE, OPTIONS",
            )

        def _send_json(self, status, payload):
            body = json.dumps(
                payload, ensure_ascii=False
            ).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header(
                    "Content-Type",
                    "application/json; charset=utf-8",
                )
                self.send_header(
                    "Content-Length", str(len(body))
                )
                self._cors()
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def _authorized(self):
            header = self.headers.get("Authorization", "")
            scheme, _, value = header.partition(" ")
            if scheme.lower() != "bearer":
                return False
            try:
                return hmac.compare_digest(
                    value.strip(), token
                )
            except TypeError:
                return False

        def _require_auth(self):
            if not self._authorized():
                self.close_connection = True
                self._send_json(401, {"error": "未授权"})
                return False
            return True

        def _path_parts(self):
            return urlsplit(self.path).path.rstrip("/").split("/")

        def _invalid_path(self):
            return any(
                ord(ch) < 32 or ord(ch) == 127
                for ch in self.path
            )

        def do_OPTIONS(self):
            if self._invalid_path():
                self.close_connection = True
                self._send_json(400, {"error": "无效的请求路径"})
                return
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if not self._require_auth():
                return
            if self._invalid_path():
                self.close_connection = True
                self._send_json(400, {"error": "无效的请求路径"})
                return
            try:
                self._handle_get()
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})

        def _handle_get(self):
            parts = self._path_parts()

            if parts == ["", "api", "tasks"]:
                query = parse_qs(urlsplit(self.path).query)
                state = query.get("state", [None])[0]
                if state is not None and state not in valid_states:
                    self._send_json(400, {"error": "无效的 state"})
                    return
                try:
                    limit = int(query.get("limit", ["50"])[0])
                    offset = int(query.get("offset", ["0"])[0])
                except ValueError:
                    self._send_json(
                        400, {"error": "limit/offset 必须是整数"}
                    )
                    return
                if limit < 1 or limit > 500 or offset < 0:
                    self._send_json(
                        400, {"error": "limit/offset 越界"}
                    )
                    return

                tasks, total = manager.list(state, limit, offset)
                self._send_json(
                    200, {"tasks": tasks, "total": total}
                )
                return

            if len(parts) == 4 and parts[:3] == ["", "api", "tasks"]:
                try:
                    task = manager.get(parts[3])
                except TaskNotFound:
                    self._send_json(404, {"error": "任务不存在"})
                    return
                self._send_json(200, {"task": task})
                return

            self._send_json(404, {"error": "未找到路径"})

        def do_POST(self):
            if not self._require_auth():
                return
            if self._invalid_path():
                self.close_connection = True
                self._send_json(400, {"error": "无效的请求路径"})
                return
            try:
                self._handle_post()
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})

        def _handle_post(self):
            if self._path_parts() != ["", "api", "tasks"]:
                self.close_connection = True
                self._send_json(404, {"error": "未找到路径"})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_json(
                    400, {"error": "无效的 Content-Length"}
                )
                return
            if length <= 0:
                self._send_json(400, {"error": "请求体不能为空"})
                return
            if length > MAX_BODY:
                self.close_connection = True
                self._send_json(413, {"error": "请求体过大"})
                return

            try:
                raw = self.rfile.read(length)
            except OSError:
                self.close_connection = True
                self._send_json(
                    400, {"error": "读取请求体失败"}
                )
                return
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self._send_json(
                    400, {"error": "请求体不是有效 JSON"}
                )
                return

            try:
                params = parse_task_request(
                    payload, manager.default_jobs
                )
                task = manager.submit(**params)
            except RequestError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except TaskConflict as exc:
                self._send_json(409, {"error": str(exc)})
                return

            self._send_json(202, {"task": task})

        def do_DELETE(self):
            if not self._require_auth():
                return
            if self._invalid_path():
                self.close_connection = True
                self._send_json(400, {"error": "无效的请求路径"})
                return
            try:
                parts = self._path_parts()
                if len(parts) != 4 or parts[:3] != ["", "api", "tasks"]:
                    self._send_json(404, {"error": "未找到路径"})
                    return
                try:
                    task = manager.cancel(parts[3])
                except TaskNotFound:
                    self._send_json(404, {"error": "任务不存在"})
                    return
                except TaskConflict as exc:
                    self._send_json(409, {"error": str(exc)})
                    return
                self._send_json(200, {"task": task})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})

        def log_message(self, format, *args):
            print(f"[serve] {self.address_string()} {format % args}")

    return ApiHandler


def run_serve(args):
    """启动 HTTP 服务，直到 Ctrl+C。"""
    output_dir = Path(args.output_dir).expanduser().resolve()
    if output_dir.exists() and not output_dir.is_dir():
        raise HLSException(f"输出路径不是目录：{output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    token = args.token or secrets.token_urlsafe(24)
    state_file = (
        Path(args.state_file).expanduser()
        if args.state_file
        else output_dir / ".m3u8-serve-state.json"
    )
    manager = TaskManager(
        output_dir,
        max_tasks=args.max_tasks,
        default_jobs=args.jobs,
        state_file=state_file,
    )
    server = ThreadingHTTPServer(
        (args.host, args.port), make_handler(manager, token)
    )
    server.daemon_threads = True

    print(
        f"监听地址：http://{args.host}:{server.server_address[1]}",
        flush=True,
    )
    print(f"输出目录：{output_dir}", flush=True)
    print(f"API token：{token}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        manager.close()
