"""watchdog ベースのファイル監視。"""

import asyncio
import logging
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent

log = logging.getLogger("dashboard.watcher")


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: "FileWatcher"):
        self._watcher = watcher

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        name = Path(event.src_path).name
        if name == "index.jsonl":
            self._watcher._push("tasks_updated")
        elif name == "jobs.jsonl":
            self._watcher._push("jobs_updated")
        elif name == "lifecycles.jsonl":
            self._watcher._push("lifecycles_updated")

    def on_created(self, event: FileSystemEvent) -> None:
        self.on_modified(event)


class FileWatcher:
    def __init__(self, repo_dir: Path, runtime_dir: Path, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._clients: dict[int, asyncio.Queue[str]] = {}
        self._next_id = 0
        self._observer = Observer()

        tasks_dir = repo_dir / "tasks"
        dispatch_dir = runtime_dir / "my-tasks-dispatch"

        handler = _Handler(self)
        if tasks_dir.is_dir():
            self._observer.schedule(handler, str(tasks_dir), recursive=False)
            log.info(f"Watching: {tasks_dir}")
        if dispatch_dir.is_dir():
            self._observer.schedule(handler, str(dispatch_dir), recursive=False)
            log.info(f"Watching: {dispatch_dir}")

    def start(self) -> None:
        self._observer.start()

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join()

    def add_client(self) -> tuple[int, asyncio.Queue[str]]:
        client_id = self._next_id
        self._next_id += 1
        queue: asyncio.Queue[str] = asyncio.Queue()
        self._clients[client_id] = queue
        return client_id, queue

    def remove_client(self, client_id: int) -> None:
        self._clients.pop(client_id, None)

    def push(self, event_name: str) -> None:
        """Send an event to all connected SSE clients (thread-safe)."""
        for queue in self._clients.values():
            self._loop.call_soon_threadsafe(queue.put_nowait, event_name)

    def _push(self, event_name: str) -> None:
        self.push(event_name)
