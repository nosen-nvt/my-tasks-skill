"""テスト用フィクスチャ (v2)。"""

import asyncio
import json
import os
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dispatcher.models import Job, SOCKET_DIR_NAME
from dispatcher.executor import ExecutorMixin
from dispatcher.host_cmd import HostCommandBroker


class StubServer(ExecutorMixin):
    """テスト用の DispatchServer サブクラス。"""

    def __init__(self, max_slots: int, repo_dir: Path, runtime_dir: Path):
        self.max_slots = max_slots
        self.repo_dir = repo_dir
        self.runtime_dir = runtime_dir
        self.jobs: dict[str, Job] = {}
        self.queue: list[Job] = []
        self.waiters: dict[str, list[asyncio.Future]] = {}
        self._counter: dict[str, int] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self.host_cmd_broker = HostCommandBroker()

        # execute_job の完了を待つための dict
        self._execute_futures: dict[str, asyncio.Future] = {}

    def _log_path(self, dispatch_id: str) -> Path:
        return self.runtime_dir / SOCKET_DIR_NAME / f"{dispatch_id}.log"

    def _jobs_path(self) -> Path:
        return self.runtime_dir / SOCKET_DIR_NAME / "jobs.jsonl"

    def _save_jobs(self) -> None:
        path = self._jobs_path()
        with open(path, "w", encoding="utf-8") as f:
            for job in self.jobs.values():
                f.write(json.dumps(job.to_dict(), ensure_ascii=False) + "\n")

    def generate_dispatch_id(self, project_id: str) -> str:
        self._counter.setdefault(project_id, 0)
        self._counter[project_id] += 1
        return f"{project_id}-{self._counter[project_id]}"

    def count_running(self) -> int:
        return sum(1 for j in self.jobs.values() if j.status == "running")

    def running_working_dirs(self) -> set[str]:
        return {j.working_dir for j in self.jobs.values() if j.status == "running"}

    async def execute_job(self, job: Job):
        """テスト用: サブプロセスを起動せず、即座に running にする。"""
        job.status = "running"
        job.started_at = "2026-01-01T00:00:00+09:00"
        self._save_jobs()

        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._execute_futures[job.dispatch_id] = future

        exit_code = await future

        job.exit_code = exit_code
        job.status = "done" if exit_code == 0 else "failed"
        job.finished_at = "2026-01-01T00:01:00+09:00"
        self._save_jobs()

        self._notify_waiters(job)
        await self.drain_queue()

    async def complete_job(self, dispatch_id: str, exit_code: int = 0):
        """テスト用: 指定ジョブを完了させる。"""
        future = self._execute_futures.get(dispatch_id)
        if future and not future.done():
            future.set_result(exit_code)

    def write_log(self, dispatch_id: str, content: str):
        """テスト用: ログファイルを書き出す。"""
        path = self._log_path(dispatch_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)


DUMMY_PROJECT = {
    "name": "test-project",
    "description": "Test project for concurrent dispatch",
    "working_directory": "/tmp/test-working-dir",
    "sandbox_profile": "default",
}


@pytest.fixture
def tmp_runtime(tmp_path):
    """一時ディレクトリを XDG_RUNTIME_DIR として設定する。"""
    socket_dir = tmp_path / SOCKET_DIR_NAME
    socket_dir.mkdir(parents=True)

    old_env = os.environ.get("XDG_RUNTIME_DIR")
    os.environ["XDG_RUNTIME_DIR"] = str(tmp_path)
    yield tmp_path
    if old_env is None:
        del os.environ["XDG_RUNTIME_DIR"]
    else:
        os.environ["XDG_RUNTIME_DIR"] = old_env


@pytest.fixture
def stub_server(tmp_runtime, tmp_path):
    """StubServer インスタンスを作成する。"""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "projects").mkdir()
    (repo_dir / "tasks").mkdir()

    working_dir = tmp_path / "workdir"
    working_dir.mkdir()

    project = DUMMY_PROJECT.copy()
    project["working_directory"] = str(working_dir)

    server = StubServer(
        max_slots=8,
        repo_dir=repo_dir,
        runtime_dir=tmp_runtime,
    )

    return server, project
