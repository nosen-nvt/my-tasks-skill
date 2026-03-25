"""ジョブ実行 Mixin。"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sandbox_exec

from .models import Job, Action, SOCKET_DIR_NAME, log, now_iso, get_host_cmd_broker_socket_path
from .prompt import build_system_prompt

if TYPE_CHECKING:
    from .host_cmd import HostCommandBroker


class ExecutorMixin:
    """OrchestratorServer にジョブ実行機能を提供する Mixin。"""

    # ホストクラスが提供する属性
    jobs: dict[str, Job]
    queue: list[Job]
    waiters: dict[str, list[asyncio.Future]]
    _processes: dict[str, asyncio.subprocess.Process]
    max_slots: int
    repo_dir: Path
    host_cmd_broker: HostCommandBroker

    def _log_path(self, dispatch_id: str) -> Path: raise NotImplementedError
    def generate_dispatch_id(self, project_id: str) -> str: raise NotImplementedError
    def count_running(self) -> int: raise NotImplementedError
    def running_working_dirs(self) -> set[str]: raise NotImplementedError
    def _save_jobs(self) -> None: raise NotImplementedError

    # OrchestratorServer が提供する dispatch_action
    async def dispatch_action(self, task_id: str, action: Action) -> dict:
        raise NotImplementedError

    def _dispatch_dir(self) -> Path:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/tmp/run-{os.getuid()}")
        return Path(runtime_dir) / SOCKET_DIR_NAME

    async def _poll_exit_file(self, dispatch_id: str, interval: float = 3.0) -> int:
        """job-wrapper が書き出す .exit ファイルをポーリングする。"""
        exit_file = self._dispatch_dir() / f"{dispatch_id}.exit"
        while True:
            await asyncio.sleep(interval)
            if exit_file.is_file():
                try:
                    return int(exit_file.read_text().strip())
                except (ValueError, OSError):
                    continue

    def _cleanup_sentinels(self, dispatch_id: str):
        for suffix in (".pid", ".exit"):
            sentinel = self._dispatch_dir() / f"{dispatch_id}{suffix}"
            sentinel.unlink(missing_ok=True)

    async def execute_job(self, job: Job):
        job.status = "running"
        job.started_at = now_iso()
        self._save_jobs()

        broker_token = None
        if job.host_commands:
            broker_token = uuid.uuid4().hex
            self.host_cmd_broker.register(broker_token, job.host_commands)

        system_prompt = build_system_prompt(
            working_dir=job.working_dir,
            sandbox_profile_id=job.sandbox_profile_id,
            host_commands=job.host_commands,
        )
        log_path = self._log_path(job.dispatch_id)
        log_file = None
        try:
            broker_env: dict[str, str] | None = None
            if broker_token:
                broker_env = {"HOST_CMD_TOKEN": broker_token, "HOST_CMD_BROKER_SOCK": get_host_cmd_broker_socket_path()}

            command = [
                "claude", "--dangerously-skip-permissions",
                "-p", job.prompt,
                "--append-system-prompt", system_prompt,
            ]
            if job.session_id:
                command.extend(["--session-id", job.session_id])

            exec_args = sandbox_exec.build_exec_args(
                sandbox_profile=job.sandbox_profile_id,
                env_files=job.env_files,
                broker_env=broker_env,
                host_commands=job.host_commands,
                extra_binds=job.extra_binds or None,
                command=command,
                working_dir=job.working_dir,
                dispatch_id=job.dispatch_id,
            )

            log_file = open(log_path, "w", encoding="utf-8")
            proc = await asyncio.create_subprocess_exec(
                *exec_args,
                cwd=job.working_dir,
                stdout=log_file,
                stderr=log_file,
            )
            job.pid = proc.pid
            self._processes[job.dispatch_id] = proc
            self._save_jobs()
            log.info(f"Job executing: {job.dispatch_id} pid={proc.pid} log={log_path}")

            # Primary: proc.wait(). Fallback: poll .exit file from job-wrapper.
            wait_task = asyncio.create_task(proc.wait())
            poll_task = asyncio.create_task(self._poll_exit_file(job.dispatch_id))

            done, _pending = await asyncio.wait(
                {wait_task, poll_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if wait_task in done:
                exit_code = wait_task.result()
                poll_task.cancel()
            else:
                exit_code = poll_task.result()
                log.warning(
                    f"Job {job.dispatch_id}: exit file detected (code={exit_code}) "
                    f"but outer process still alive. Terminating."
                )
                try:
                    await asyncio.wait_for(wait_task, timeout=5.0)
                except asyncio.TimeoutError:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        proc.kill()
                wait_task.cancel()

            job.exit_code = exit_code
            job.status = "done" if exit_code == 0 else "failed"
        except Exception as e:
            log.error(f"Job execution error: {job.dispatch_id}: {e}")
            job.status = "failed"
            job.exit_code = -1
        finally:
            if broker_token:
                self.host_cmd_broker.revoke(broker_token)
            if log_file:
                log_file.close()
            job.finished_at = now_iso()
            self._processes.pop(job.dispatch_id, None)
            self._cleanup_sentinels(job.dispatch_id)
            self._save_jobs()
            log.info(f"Job finished: {job.dispatch_id} status={job.status} exit_code={job.exit_code}")

            # タスク状態遷移: dispatch_action で job_completed を発行
            if job.task_id:
                try:
                    asyncio.create_task(
                        self.dispatch_action(job.task_id, Action(type="job_completed"))
                    )
                    log.info(f"Task {job.task_id}: job_completed action dispatched")
                except Exception as e:
                    log.error(f"dispatch_action error for task {job.task_id}: {e}")

        self._notify_waiters(job)
        await self.drain_queue()

    async def drain_queue(self):
        while self.queue and self.count_running() < self.max_slots:
            running_dirs = self.running_working_dirs()
            idx = next((i for i, j in enumerate(self.queue) if j.working_dir not in running_dirs), None)
            if idx is None:
                break
            job = self.queue.pop(idx)
            asyncio.create_task(self.execute_job(job))

    async def _kill_job(self, job: Job):
        if job.status == "queued":
            self.queue = [j for j in self.queue if j.dispatch_id != job.dispatch_id]
            del self.jobs[job.dispatch_id]
            self._save_jobs()
            return

        if job.status == "running":
            proc = self._processes.get(job.dispatch_id)
            if proc and proc.returncode is None:
                try:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        proc.kill()
                except ProcessLookupError:
                    pass
            job.status = "failed"
            job.exit_code = -1
            job.finished_at = now_iso()
            self._processes.pop(job.dispatch_id, None)
            self._cleanup_sentinels(job.dispatch_id)
            self._save_jobs()
            self._notify_waiters(job)

    def _notify_waiters(self, job: Job):
        waiters = self.waiters.pop(job.dispatch_id, [])
        result = {"ok": True, "dispatch_id": job.dispatch_id, "status": job.status, "exit_code": job.exit_code}
        for future in waiters:
            if not future.done():
                future.set_result(result)
