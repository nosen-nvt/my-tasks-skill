"""DispatchServer コア。"""

import asyncio
import json
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sandbox_exec

from .models import (
    Job, Lifecycle, log, now_iso,
    SOCKET_DIR_NAME,
    get_socket_path, get_host_cmd_broker_socket_path, get_repo_dir,
)
from .host_cmd import HostCommandBroker
from .lifecycle import LifecycleManager
from .tmux import detect_tmux_session, ensure_tmux_session
from .project_classifier import classify_project
from .executor import ExecutorMixin


def markdown_to_context_yaml(md_content: str, prompt: str, project_id: str) -> dict:
    """タスク markdown を YAML コンテキスト dict に変換する。"""
    import re

    def extract_section(content: str, heading: str) -> str:
        pattern = rf"^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)"
        m = re.search(pattern, content, re.MULTILINE | re.DOTALL)
        return m.group(1).strip() if m else ""

    def extract_list(content: str, heading: str) -> list[str]:
        text = extract_section(content, heading)
        if not text:
            return []
        items = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("- "):
                items.append(line[2:].strip())
        return items

    # メタデータ抽出 (YAML frontmatter 的なものがあれば)
    meta = {"task_id": "", "remote_id": "", "datasource_id": "", "project_id": project_id, "generation": 1}

    description = extract_section(md_content, "概要") or prompt
    preconditions = extract_list(md_content, "事前条件")
    acceptance_criteria = extract_list(md_content, "達成条件")
    open_questions = extract_list(md_content, "未決事項")
    completion_actions = extract_list(md_content, "完了時アクション")
    execute_prompt = extract_section(md_content, "実行プロンプト")

    # 実行履歴から previous_generations を構築
    previous_generations: list[dict] = []
    history = extract_section(md_content, "実行履歴")
    if history:
        previous_generations.append({"summary": history})

    return {
        "meta": meta,
        "description": description,
        "preconditions": preconditions,
        "acceptance_criteria": acceptance_criteria,
        "open_questions": open_questions,
        "completion_actions": completion_actions,
        "phases": [],
        "execute_prompt": execute_prompt,
        "previous_generations": previous_generations,
    }


class DispatchServer(ExecutorMixin):
    def __init__(self, max_slots: int, repo_dir: Path):
        self.max_slots = max_slots
        self.repo_dir = repo_dir
        self.jobs: dict[str, Job] = {}
        self.queue: list[Job] = []
        self.waiters: dict[str, list[asyncio.Future]] = {}
        self._counter: dict[str, int] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self.host_cmd_broker = HostCommandBroker()

        self.lifecycle_mgr = LifecycleManager(
            repo_dir=repo_dir,
            dispatch_fn=self._dispatch_internal,
            generate_dispatch_id_fn=self.generate_dispatch_id,
            result_path_fn=self._result_path,
            log_path_fn=self._log_path,
        )

    # --- パス ---

    def _log_path(self, dispatch_id: str) -> Path:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/tmp/run-{os.getuid()}")
        return Path(runtime_dir) / SOCKET_DIR_NAME / f"{dispatch_id}.log"

    def _result_path(self, dispatch_id: str) -> Path:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/tmp/run-{os.getuid()}")
        return Path(runtime_dir) / SOCKET_DIR_NAME / f"{dispatch_id}.result.json"

    def _read_result(self, dispatch_id: str) -> dict | None:
        path = self._result_path(dispatch_id)
        if not path.exists():
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    # --- jobs.jsonl 永続化 ---

    def _jobs_path(self) -> Path:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/tmp/run-{os.getuid()}")
        return Path(runtime_dir) / SOCKET_DIR_NAME / "jobs.jsonl"

    def _save_jobs(self) -> None:
        path = self._jobs_path()
        with open(path, "w", encoding="utf-8") as f:
            for job in self.jobs.values():
                f.write(json.dumps(job.to_dict(), ensure_ascii=False) + "\n")

    def _load_jobs(self) -> None:
        path = self._jobs_path()
        if not path.exists():
            return
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                dispatch_id = data["dispatch_id"]
                # running/queued ジョブは再起動時に failed にフォールバック
                if data.get("status") in ("running", "queued"):
                    data["status"] = "failed"
                    if not data.get("finished_at"):
                        data["finished_at"] = now_iso()
                job = Job(
                    dispatch_id=dispatch_id,
                    project_id=data["project_id"],
                    prompt="",
                    working_dir="",
                    job_type=data.get("job_type", "execute"),
                    lifecycle_id=data.get("lifecycle_id"),
                    run=data.get("run"),
                    status=data["status"],
                    pid=data.get("pid"),
                    exit_code=data.get("exit_code"),
                    started_at=data.get("started_at"),
                    finished_at=data.get("finished_at"),
                )
                self.jobs[dispatch_id] = job
                # _counter 復元: dispatch_id = "{project_id}-{seq}"
                parts = dispatch_id.rsplit("-", 1)
                if len(parts) == 2:
                    try:
                        seq = int(parts[1])
                        project_id = parts[0]
                        self._counter[project_id] = max(self._counter.get(project_id, 0), seq)
                    except ValueError:
                        pass
        log.info(f"Loaded {len(self.jobs)} jobs from jobs.jsonl")
        self._save_jobs()

    # --- ジョブ管理 ---

    def generate_dispatch_id(self, project_id: str) -> str:
        self._counter.setdefault(project_id, 0)
        self._counter[project_id] += 1
        return f"{project_id}-{self._counter[project_id]}"

    def count_running(self) -> int:
        return sum(1 for j in self.jobs.values() if j.status == "running")

    def running_project_ids(self) -> set[str]:
        return {j.project_id for j in self.jobs.values() if j.status == "running"}

    # --- コマンドハンドラ ---

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            data = await reader.readline()
            if not data:
                return
            request = json.loads(data.decode())
            command = request.get("command", "")

            handler = {
                "run": self.cmd_run,
                "open": self.cmd_open,
                "status": self.cmd_status,
                "cancel": self.cmd_cancel,
                "kill": self.cmd_kill,
                "kill-all": self.cmd_kill_all,
                "wait": self.cmd_wait,
                "dispatch": self.cmd_dispatch,
                "resume": self.cmd_resume,
            }.get(command)

            if handler is None:
                response = {"ok": False, "error": f"Unknown command: {command}"}
            else:
                response = await handler(request)

            future = response.pop("_future", None)

            writer.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
            await writer.drain()

            if command == "wait" and response.get("ok") and response.get("waiting"):
                if future:
                    result = await future
                    writer.write(json.dumps(result, ensure_ascii=False).encode() + b"\n")
                    await writer.drain()
        except Exception as e:
            log.error(f"Client handler error: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def cmd_run(self, request: dict) -> dict:
        project_id = request.get("project_id", "")
        prompt = request.get("prompt", "")
        job_type = request.get("job_type", "execute")

        if not project_id:
            return {"ok": False, "error": "project_id is required"}
        if not prompt:
            return {"ok": False, "error": "prompt is required"}

        project = sandbox_exec.load_project(project_id, self.repo_dir)
        if not project:
            return {"ok": False, "error": f"Project not found: {project_id}"}

        working_dir = project.get("working_directory", "")
        if not working_dir:
            return {"ok": False, "error": f"working_directory not set for project: {project_id}"}
        if not Path(working_dir).is_dir():
            return {"ok": False, "error": f"working_directory does not exist: {working_dir}"}

        try:
            sandbox_profile_id, env_files, host_commands, extra_binds = \
                sandbox_exec.resolve_project_sandbox_params(
                    project, sandbox_profile_override=request.get("sandbox_profile"),
                )
        except (FileNotFoundError, ValueError) as e:
            return {"ok": False, "error": str(e)}

        dispatch_id = self.generate_dispatch_id(project_id)

        job = Job(
            dispatch_id=dispatch_id,
            project_id=project_id,
            prompt=prompt,
            working_dir=working_dir,
            job_type=job_type,
            sandbox_profile_id=sandbox_profile_id,
            env_files=env_files,
            host_commands=host_commands,
            extra_binds=extra_binds,
        )
        self.jobs[dispatch_id] = job
        self._save_jobs()

        if self.count_running() < self.max_slots and project_id not in self.running_project_ids():
            asyncio.create_task(self.execute_job(job))
            log.info(f"Job started: {dispatch_id}")
            return {"ok": True, "dispatch_id": dispatch_id, "message": "Job started"}
        else:
            self.queue.append(job)
            reason = "project busy" if project_id in self.running_project_ids() else "slot full"
            log.info(f"Job queued ({reason}): {dispatch_id}")
            return {"ok": True, "dispatch_id": dispatch_id, "message": f"Job queued ({reason})"}

    async def cmd_open(self, request: dict) -> dict:
        project_id = request.get("project_id", "")
        session = request.get("session")

        if not project_id:
            return {"ok": False, "error": "project_id is required"}

        project = sandbox_exec.load_project(project_id, self.repo_dir)
        if not project:
            return {"ok": False, "error": f"Project not found: {project_id}"}

        working_dir = project.get("working_directory", "")
        if not working_dir:
            return {"ok": False, "error": f"working_directory not set for project: {project_id}"}
        if not Path(working_dir).is_dir():
            return {"ok": False, "error": f"working_directory does not exist: {working_dir}"}

        try:
            sandbox_profile_id, env_files, _host_cmds, _extra_binds = \
                sandbox_exec.resolve_project_sandbox_params(
                    project, sandbox_profile_override=request.get("sandbox_profile"),
                )
        except (FileNotFoundError, ValueError) as e:
            return {"ok": False, "error": str(e)}

        session_name, is_caller = detect_tmux_session(session)
        if not ensure_tmux_session(session_name, is_caller):
            return {"ok": False, "error": f"tmux session '{session_name}' not available"}

        window_name = project_id
        env_file_args = "".join(f" --env-file '{ef}'" for ef in env_files)
        cmd = f"cd '{working_dir}' && sandbox --sandbox-profile '{sandbox_profile_id}'{env_file_args} -- claude --permission-mode bypassPermissions"

        result = subprocess.run(
            ["tmux", "new-window", "-d", "-t", session_name, "-n", window_name, "bash", "-c", cmd],
            capture_output=True,
        )
        if result.returncode != 0:
            return {"ok": False, "error": f"tmux window creation failed: {result.stderr.decode().strip()}"}

        log.info(f"Opened interactive session: {session_name}:{window_name}")
        return {"ok": True, "message": f"Opened {session_name}:{window_name}", "window": window_name}

    async def cmd_status(self, _request: dict) -> dict:
        jobs = [j.to_dict() for j in self.jobs.values()]
        lifecycles = [lc.to_dict() for lc in self.lifecycle_mgr.lifecycles.values()]
        return {"ok": True, "jobs": jobs, "lifecycles": lifecycles}

    async def cmd_cancel(self, request: dict) -> dict:
        dispatch_id = request.get("dispatch_id", "")
        job = self.jobs.get(dispatch_id)
        if not job:
            return {"ok": False, "error": f"Unknown dispatch_id: {dispatch_id}"}
        if job.status != "queued":
            return {"ok": False, "error": f"Can only cancel queued jobs (current: {job.status})"}

        self.queue = [j for j in self.queue if j.dispatch_id != dispatch_id]
        del self.jobs[dispatch_id]
        self._save_jobs()
        log.info(f"Job cancelled: {dispatch_id}")
        return {"ok": True, "dispatch_id": dispatch_id, "message": "Job cancelled"}

    async def cmd_kill(self, request: dict) -> dict:
        dispatch_id = request.get("dispatch_id", "")
        job = self.jobs.get(dispatch_id)
        if not job:
            return {"ok": False, "error": f"Unknown dispatch_id: {dispatch_id}"}

        await self._kill_job(job)
        log.info(f"Job killed: {dispatch_id}")
        return {"ok": True, "dispatch_id": dispatch_id, "message": "Job killed"}

    async def cmd_kill_all(self, _request: dict) -> dict:
        killed = 0
        for job in list(self.jobs.values()):
            if job.status in ("running", "queued"):
                await self._kill_job(job)
                killed += 1
        self.queue.clear()
        log.info(f"All jobs killed: {killed}")
        return {"ok": True, "message": f"{killed} jobs killed"}

    async def cmd_wait(self, request: dict) -> dict:
        dispatch_id = request.get("dispatch_id", "")
        job = self.jobs.get(dispatch_id)
        if not job:
            return {"ok": False, "error": f"Unknown dispatch_id: {dispatch_id}"}

        if job.status in ("done", "failed"):
            return {"ok": True, "dispatch_id": dispatch_id, "status": job.status, "exit_code": job.exit_code}

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self.waiters.setdefault(dispatch_id, []).append(future)
        return {"ok": True, "waiting": True, "dispatch_id": dispatch_id, "_future": future}

    # --- dispatch / resume コマンド ---

    async def cmd_dispatch(self, request: dict) -> dict:
        project_id = request.get("project_id")
        prompt = request.get("prompt", "")
        context = request.get("context", "")
        max_runs = request.get("max_runs")

        if not project_id:
            project_id = await classify_project(self.repo_dir, prompt)
            if not project_id:
                return {"ok": False, "error": "プロジェクトを判定できませんでした。--project を指定してください"}

        project = sandbox_exec.load_project(project_id, self.repo_dir)
        if not project:
            return {"ok": False, "error": f"Project not found: {project_id}"}
        if not project.get("working_directory"):
            return {"ok": False, "error": f"manual project: {project_id}"}

        if max_runs is None:
            orchestration = project.get("orchestration", {})
            max_runs = orchestration.get("max_runs_per_generation", 5)

        mgr = self.lifecycle_mgr
        lc = Lifecycle(
            lifecycle_id=request.get("lifecycle_id") or mgr.generate_id(),
            project_id=project_id,
            prompt=prompt,
            max_runs=max_runs,
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        mgr.lifecycles[lc.lifecycle_id] = lc

        # YAML コンテキスト生成
        if context:
            # markdown が渡された場合は YAML に変換
            context_data = markdown_to_context_yaml(context, prompt, project_id)
        else:
            context_data = {
                "meta": {
                    "task_id": "",
                    "remote_id": "",
                    "datasource_id": "",
                    "project_id": project_id,
                    "generation": 1,
                },
                "description": prompt,
                "preconditions": [],
                "acceptance_criteria": [],
                "open_questions": [],
                "completion_actions": [],
                "phases": [],
                "execute_prompt": "",
                "previous_generations": [],
            }
        mgr._init_context_yaml(lc, context_data)
        mgr._save()

        await mgr.dispatch_plan(lc)

        return {
            "ok": True,
            "lifecycle_id": lc.lifecycle_id,
            "dispatch_id": lc.current_dispatch_id,
            "message": f"Lifecycle started: {lc.lifecycle_id}",
        }

    async def cmd_resume(self, request: dict) -> dict:
        lifecycle_id = request.get("lifecycle_id")
        context_update = request.get("context_update")
        mgr = self.lifecycle_mgr
        lc = mgr.lifecycles.get(lifecycle_id) if lifecycle_id else None
        if not lc:
            return {"ok": False, "error": f"Lifecycle not found: {lifecycle_id}"}
        if lc.status != "suspend":
            return {"ok": False, "error": f"Lifecycle is not suspended (current: {lc.status})"}

        if context_update and lc.context_path:
            # YAML として書き込み
            import yaml
            try:
                data = yaml.safe_load(context_update)
                if isinstance(data, dict):
                    mgr._update_context_yaml(lc, data)
                else:
                    Path(lc.context_path).write_text(context_update, encoding="utf-8")
            except yaml.YAMLError:
                Path(lc.context_path).write_text(context_update, encoding="utf-8")

        prev_reason = lc.suspend_reason

        if lc.suspend_reason == "needs_input":
            mgr._update_status(lc, "planning")
            await mgr.dispatch_plan(lc, prev_suspend_reason=prev_reason)
        elif lc.suspend_reason == "approval_required":
            mgr._update_status(lc, "phase_executing")
            await mgr.dispatch_execute(lc)
        elif lc.suspend_reason == "agent_review":
            mgr._update_status(lc, "planning")
            await mgr.dispatch_plan(lc)
        elif lc.suspend_reason == "project_confirmation":
            new_project_id = request.get("project_id")
            if new_project_id:
                lc.project_id = new_project_id
            mgr._update_status(lc, "planning")
            await mgr.dispatch_plan(lc)
        else:
            return {"ok": False, "error": f"Unknown suspend reason: {lc.suspend_reason}"}

        return {
            "ok": True,
            "lifecycle_id": lc.lifecycle_id,
            "dispatch_id": lc.current_dispatch_id,
            "message": f"Lifecycle resumed: {lc.lifecycle_id}",
        }

    # --- クリーンアップ・シャットダウン ---

    async def cleanup_old_logs(self):
        max_age = 6 * 3600
        job_max_age = 24 * 3600
        while True:
            await asyncio.sleep(1800)
            try:
                log_dir = self._log_path("_").parent
                now = datetime.now().timestamp()
                done_lc_ids = {lc.lifecycle_id for lc in self.lifecycle_mgr.lifecycles.values() if lc.status == "done"}
                for p in list(log_dir.glob("*.log")) + list(log_dir.glob("*.result.json")) + list(log_dir.glob("*.context.yaml")) + list(log_dir.glob("*.context.md")) + list(log_dir.glob("*.pid")) + list(log_dir.glob("*.exit")):
                    if (p.suffix in (".yaml", ".md")) and p.stem.replace(".context", "") not in done_lc_ids:
                        continue
                    if now - p.stat().st_mtime > max_age:
                        p.unlink()
                        log.info(f"Cleaned up old file: {p.name}")

                # 完了済みジョブのプルーニング (24時間経過)
                pruned = []
                for dispatch_id, job in list(self.jobs.items()):
                    if job.status in ("done", "failed") and job.finished_at:
                        try:
                            finished = datetime.fromisoformat(job.finished_at).timestamp()
                            if now - finished > job_max_age:
                                pruned.append(dispatch_id)
                        except (ValueError, OSError):
                            pass
                for dispatch_id in pruned:
                    del self.jobs[dispatch_id]
                    log.info(f"Pruned old job: {dispatch_id}")
                if pruned:
                    self._save_jobs()
            except Exception as e:
                log.error(f"Log cleanup error: {e}")

    async def shutdown(self):
        log.info("Shutting down...")
        for dispatch_id, proc in list(self._processes.items()):
            if proc.returncode is None:
                log.info(f"Terminating job: {dispatch_id}")
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass


# ---------------------------------------------------------------------------
# サーバ起動
# ---------------------------------------------------------------------------

async def run_server(max_slots: int, repo: str):
    import logging as _logging

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    repo_dir = get_repo_dir(repo)
    if not repo_dir.exists():
        log.error(f"Repository not found: {repo_dir}")
        sys.exit(1)

    socket_path = get_socket_path()
    socket_dir = os.path.dirname(socket_path)
    os.makedirs(socket_dir, exist_ok=True)

    if os.path.exists(socket_path):
        os.unlink(socket_path)

    broker_path = get_host_cmd_broker_socket_path()
    if os.path.exists(broker_path):
        os.unlink(broker_path)

    server = DispatchServer(max_slots=max_slots, repo_dir=repo_dir)
    server.lifecycle_mgr._load()
    log.info(f"Loaded {len(server.lifecycle_mgr.lifecycles)} lifecycles ({sum(1 for lc in server.lifecycle_mgr.lifecycles.values() if lc.status == 'suspend')} suspended)")
    server._load_jobs()

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(_shutdown(server, srv, broker_srv)))
    loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(_shutdown(server, srv, broker_srv)))

    srv = await asyncio.start_unix_server(server.handle_client, path=socket_path)
    os.chmod(socket_path, 0o600)

    broker_srv = await asyncio.start_unix_server(server.host_cmd_broker.handle_client, path=broker_path)
    os.chmod(broker_path, 0o600)

    log.info(f"Dispatch server started: socket={socket_path} host_cmd_broker={broker_path} max_slots={max_slots} repo={repo_dir}")

    asyncio.create_task(server.cleanup_old_logs())

    async with srv, broker_srv:
        await srv.serve_forever()


async def _shutdown(server: DispatchServer, srv, broker_srv):
    await server.shutdown()
    srv.close()
    await srv.wait_closed()
    broker_srv.close()
    await broker_srv.wait_closed()
    asyncio.get_event_loop().stop()
