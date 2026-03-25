"""OrchestratorServer — Reducer + Hook Evaluator + Job Executor を統合した常駐プロセス。"""

import asyncio
import json
import os
import shlex
import signal
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sandbox_exec

from .models import (
    Job, Action, log, now_iso,
    SOCKET_DIR_NAME,
    get_socket_path, get_host_cmd_broker_socket_path, get_repo_dir,
)
from .host_cmd import HostCommandBroker
from .tmux import detect_tmux_session, ensure_tmux_session
from .executor import ExecutorMixin
from .reducer import reduce
from .hooks import HookEvaluator
from .builtin_hooks import BUILTIN_HOOKS
from lib.worktree import ensure_worktree
from lib.task_store import load_task_yaml, save_task_yaml, update_index_status


class OrchestratorServer(ExecutorMixin):
    def __init__(self, max_slots: int, repo_dir: Path):
        self.max_slots = max_slots
        self.repo_dir = repo_dir
        self.tasks_dir = repo_dir / "tasks"
        self.datasources_dir = repo_dir / "datasources"
        self.jobs: dict[str, Job] = {}
        self.queue: list[Job] = []
        self.waiters: dict[str, list[asyncio.Future]] = {}
        self._counter: dict[str, int] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self.host_cmd_broker = HostCommandBroker()

        # Orchestrator 固有
        self._dispatch_lock = asyncio.Lock()
        self._hook_evaluator = HookEvaluator(BUILTIN_HOOKS)
        self._hook_depth = 0
        self._MAX_HOOK_DEPTH = 5

    # --- パス ---

    def _log_path(self, dispatch_id: str) -> Path:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/tmp/run-{os.getuid()}")
        return Path(runtime_dir) / SOCKET_DIR_NAME / f"{dispatch_id}.log"

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
                if data.get("status") in ("running", "queued"):
                    data["status"] = "failed"
                    if not data.get("finished_at"):
                        data["finished_at"] = now_iso()
                job = Job(
                    dispatch_id=dispatch_id,
                    project_id=data["project_id"],
                    prompt="",
                    working_dir="",
                    session_id=data.get("session_id", ""),
                    task_id=data.get("task_id", ""),
                    status=data["status"],
                    pid=data.get("pid"),
                    exit_code=data.get("exit_code"),
                    branch=data.get("branch"),
                    started_at=data.get("started_at"),
                    finished_at=data.get("finished_at"),
                )
                self.jobs[dispatch_id] = job
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

    def running_working_dirs(self) -> set[str]:
        return {j.working_dir for j in self.jobs.values() if j.status == "running"}

    # --- dispatch_action (コアエントリポイント) ---

    async def dispatch_action(self, task_id: str, action: Action) -> dict:
        """Reducer → 永続化 → フック評価。"""
        async with self._dispatch_lock:
            task_data = load_task_yaml(self.tasks_dir, task_id)
            if task_data is None:
                return {"ok": False, "error": "task not found"}

            new_data = reduce(task_data, action)
            if isinstance(new_data, str):
                return {"ok": False, "error": new_data}

            # 永続化
            save_task_yaml(self.tasks_dir, task_id, new_data)
            if new_data.status != task_data.status:
                update_index_status(self.tasks_dir, task_id, new_data.status)

        # フック評価（ロック外で実行 — フックが dispatch_action を呼ぶため）
        self._hook_depth += 1
        try:
            if self._hook_depth <= self._MAX_HOOK_DEPTH:
                await self._evaluate_and_fire_hooks(task_id, new_data)
            else:
                log.warning(f"Hook depth limit reached ({self._MAX_HOOK_DEPTH}) for task {task_id}")
        finally:
            self._hook_depth -= 1

        return {"ok": True}

    async def _evaluate_and_fire_hooks(self, task_id: str, task_data) -> None:
        """条件一致するフックを起動する。"""
        matched = self._hook_evaluator.evaluate(task_data, self.repo_dir)
        for hook in matched:
            if hook.type == "builtin" and hook.handler:
                log.info(f"Firing builtin hook: {hook.id} for task {task_id}")
                asyncio.create_task(self._run_builtin_hook(hook, task_id, task_data))
            elif hook.type == "script":
                log.info(f"Firing script hook: {hook.id} for task {task_id}")
                asyncio.create_task(self._run_script_hook(hook, task_id, task_data))
            elif hook.type == "dispatch":
                log.info(f"Firing dispatch hook: {hook.id} for task {task_id}")
                asyncio.create_task(self._run_dispatch_hook(hook, task_id, task_data))

    async def _run_builtin_hook(self, hook, task_id: str, task_data) -> None:
        try:
            await hook.handler(self, task_id, task_data)
        except Exception as e:
            log.error(f"Builtin hook {hook.id} error: {e}")

    def _expand_hook_command(self, template: str, task_id: str, task_data) -> str:
        """フックコマンドテンプレートを展開する。値は shlex.quote でエスケープ。"""
        raw: dict[str, str] = {
            "task_id": task_id,
            "branch": task_data.branch or "",
            "title": task_data.title or "",
            "project_id": task_data.project_id or "",
            "remote_id": task_data.remote_id or "",
            "pr.url": task_data.pr.url or "",
            "pr.ci_status": task_data.pr.ci_status or "",
        }
        if task_data.project_id:
            p_path = self.repo_dir / "projects" / f"{task_data.project_id}.json"
            if p_path.exists():
                try:
                    with open(p_path, encoding="utf-8") as f:
                        project = json.load(f)
                    raw["working_directory"] = project.get("working_directory", "")
                except (json.JSONDecodeError, OSError):
                    pass

        result = template
        for key, val in raw.items():
            result = result.replace(f"{{{key}}}", shlex.quote(str(val)))
        return result

    async def _run_script_hook(self, hook, task_id: str, task_data) -> None:
        """ホスト側でシェルスクリプトを実行する。"""
        try:
            cmd = self._expand_hook_command(hook.command, task_id, task_data)
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            log.info(f"Script hook {hook.id} exited with {proc.returncode}")
            if proc.returncode != 0:
                log.error(f"Script hook {hook.id} stderr: {stderr.decode(errors='replace')[:500]}")
        except Exception as e:
            log.error(f"Script hook {hook.id} error: {e}")

    async def _run_dispatch_hook(self, hook, task_id: str, task_data) -> None:
        """AI ジョブをサンドボックスで実行する。"""
        try:
            # プロンプトファイル読み込み + テンプレート展開
            prompt_path = self.repo_dir / hook.prompt_file
            if not prompt_path.exists():
                log.error(f"Dispatch hook {hook.id}: prompt file not found: {hook.prompt_file}")
                return
            prompt_template = prompt_path.read_text(encoding="utf-8")
            prompt = prompt_template
            for attr in ("branch", "dispatch_id", "session_id"):
                prompt = prompt.replace(f"{{{attr}}}", str(getattr(task_data, attr, "")))
            prompt = prompt.replace("{pr.url}", task_data.pr.url)
            prompt = prompt.replace("{pr.ci_status}", task_data.pr.ci_status)

            if not task_data.project_id:
                log.error(f"Dispatch hook {hook.id}: no project_id for task {task_id}")
                return

            run_request = {
                "project_id": task_data.project_id,
                "prompt": prompt,
                "session_id": str(__import__("uuid").uuid4()),
                "task_id": task_id,
            }
            if task_data.branch:
                run_request["branch"] = task_data.branch

            result = await self.cmd_run(run_request)
            if result.get("ok"):
                dispatch_id = result.get("dispatch_id", "")
                # related_jobs に追加
                td = load_task_yaml(self.tasks_dir, task_id)
                if td and dispatch_id not in td.related_jobs:
                    td.related_jobs.append(dispatch_id)
                    save_task_yaml(self.tasks_dir, task_id, td)
                log.info(f"Dispatch hook {hook.id}: dispatched {dispatch_id}")
            else:
                log.error(f"Dispatch hook {hook.id}: failed: {result.get('error')}")
        except Exception as e:
            log.error(f"Dispatch hook {hook.id} error: {e}")

    # --- コマンドハンドラ ---

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            data = await reader.readline()
            if not data:
                return
            request = json.loads(data.decode())
            command = request.get("command", "")

            handler = {
                "dispatch_action": self.cmd_dispatch_action,
                "run": self.cmd_run,
                "resume": self.cmd_resume,
                "open": self.cmd_open,
                "status": self.cmd_status,
                "cancel": self.cmd_cancel,
                "kill": self.cmd_kill,
                "kill-all": self.cmd_kill_all,
                "wait": self.cmd_wait,
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

    async def cmd_dispatch_action(self, request: dict) -> dict:
        task_id = request.get("task_id", "")
        action_data = request.get("action", {})
        action = Action(
            type=action_data.get("type", ""),
            field=action_data.get("field", ""),
            value=action_data.get("value", ""),
            payload=action_data.get("payload", {}),
        )
        return await self.dispatch_action(task_id, action)

    async def cmd_run(self, request: dict) -> dict:
        project_id = request.get("project_id", "")
        prompt = request.get("prompt", "")
        session_id = request.get("session_id", "")
        task_id = request.get("task_id", "")

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

        if not session_id:
            import uuid
            session_id = str(uuid.uuid4())

        branch = request.get("branch")
        if branch:
            try:
                wt_path = ensure_worktree(working_dir, branch)
            except RuntimeError as e:
                return {"ok": False, "error": f"worktree error: {e}"}
            extra_binds = list(extra_binds) + [
                {"source": f"{working_dir}/.git", "target": f"{working_dir}/.git", "mode": "rw"},
            ]
            working_dir = str(wt_path)

        job = Job(
            dispatch_id=dispatch_id,
            project_id=project_id,
            prompt=prompt,
            working_dir=working_dir,
            sandbox_profile_id=sandbox_profile_id,
            env_files=env_files,
            host_commands=host_commands,
            extra_binds=extra_binds,
            session_id=session_id,
            task_id=task_id,
            branch=branch,
        )
        self.jobs[dispatch_id] = job
        self._save_jobs()

        if self.count_running() < self.max_slots and working_dir not in self.running_working_dirs():
            asyncio.create_task(self.execute_job(job))
            log.info(f"Job started: {dispatch_id}")
            return {"ok": True, "dispatch_id": dispatch_id, "session_id": session_id, "message": "Job started"}
        else:
            self.queue.append(job)
            reason = "working_dir busy" if working_dir in self.running_working_dirs() else "slot full"
            log.info(f"Job queued ({reason}): {dispatch_id}")
            return {"ok": True, "dispatch_id": dispatch_id, "session_id": session_id, "message": f"Job queued ({reason})"}

    def _register_broker_for_tmux(self, host_cmds: list[dict]) -> str:
        """host_commands をブローカーに登録し、tmux コマンド用の export 文字列を返す。

        コマンドが無い場合は空文字列を返す。
        """
        if not host_cmds:
            return ""
        broker_token = uuid.uuid4().hex
        self.host_cmd_broker.register(broker_token, host_cmds)
        broker_sock = get_host_cmd_broker_socket_path()
        return f"export HOST_CMD_TOKEN={broker_token} HOST_CMD_BROKER_SOCK={broker_sock}; "

    async def cmd_resume(self, request: dict) -> dict:
        """完了済みセッションを tmux で再開する。"""
        project_id = request.get("project_id", "")
        session_id = request.get("session_id", "")

        if not session_id:
            return {"ok": False, "error": "session_id is required"}

        if project_id:
            project = sandbox_exec.load_project(project_id, self.repo_dir)
            if not project:
                return {"ok": False, "error": f"Project not found: {project_id}"}
            home_dir = str(Path.home())
            candidate = project.get("working_directory", "")
            working_dir = candidate if candidate and Path(candidate).is_dir() else home_dir
            try:
                sandbox_profile_id, env_files, host_cmds, _extra_binds = \
                    sandbox_exec.resolve_project_sandbox_params(
                        project, sandbox_profile_override=request.get("sandbox_profile"),
                    )
            except (FileNotFoundError, ValueError) as e:
                return {"ok": False, "error": str(e)}
        else:
            return {"ok": False, "error": "project_id is required"}

        worktree = request.get("worktree")
        if worktree and Path(worktree).is_dir():
            working_dir = worktree

        sandbox_exec.ensure_trust_accepted(working_dir)

        window_name = request.get("window_name", f"resume-{project_id}")
        session_name_req = request.get("session")
        session_name, is_caller = detect_tmux_session(session_name_req)
        if not ensure_tmux_session(session_name, is_caller):
            return {"ok": False, "error": f"tmux session '{session_name}' not available"}

        broker_env_str = self._register_broker_for_tmux(host_cmds)
        env_file_args = "".join(f" --env-file '{ef}'" for ef in env_files)
        cmd = f"{broker_env_str}cd '{working_dir}' && sandbox --sandbox-profile '{sandbox_profile_id}'{env_file_args} -- claude --dangerously-skip-permissions --resume {shlex.quote(session_id)}"
        log.info(f"Resume command: {cmd}")

        result = subprocess.run(
            ["tmux", "new-window", "-d", "-t", session_name, "-n", window_name, cmd],
            capture_output=True,
        )
        if result.returncode != 0:
            return {"ok": False, "error": f"tmux window creation failed: {result.stderr.decode().strip()}"}

        activate = request.get("activate", False)
        if activate:
            subprocess.run(
                ["tmux", "select-window", "-t", f"{session_name}:{window_name}"],
                capture_output=True,
            )

        log.info(f"Resumed session: {session_name}:{window_name} session_id={session_id}")
        return {"ok": True, "message": f"Resumed {session_name}:{window_name}", "window": window_name}

    async def cmd_open(self, request: dict) -> dict:
        project_id = request.get("project_id", "")
        system_prompt = request.get("system_prompt")
        session = request.get("session")
        host_cmds: list[dict] = []

        if project_id:
            project = sandbox_exec.load_project(project_id, self.repo_dir)
            if not project:
                return {"ok": False, "error": f"Project not found: {project_id}"}
            home_dir = str(Path.home())
            candidate = project.get("working_directory", "")
            working_dir = candidate if candidate and Path(candidate).is_dir() else home_dir
            try:
                sandbox_profile_id, env_files, host_cmds, _extra_binds = \
                    sandbox_exec.resolve_project_sandbox_params(
                        project, sandbox_profile_override=request.get("sandbox_profile"),
                    )
            except (FileNotFoundError, ValueError) as e:
                return {"ok": False, "error": str(e)}
        else:
            working_dir = str(Path.home())
            sandbox_profile_id = request.get("sandbox_profile") or "default"
            env_files = []

        worktree = request.get("worktree")
        if worktree and Path(worktree).is_dir():
            working_dir = worktree

        sandbox_exec.ensure_trust_accepted(working_dir)

        window_name = request.get("window_name", project_id or "session")

        session_name, is_caller = detect_tmux_session(session)
        if not ensure_tmux_session(session_name, is_caller):
            return {"ok": False, "error": f"tmux session '{session_name}' not available"}

        broker_env_str = self._register_broker_for_tmux(host_cmds)
        env_file_args = "".join(f" --env-file '{ef}'" for ef in env_files)
        prompt = request.get("prompt")
        prompt_arg = f" {shlex.quote(prompt)}" if prompt else ""
        system_prompt_arg = f" --append-system-prompt {shlex.quote(system_prompt)}" if system_prompt else ""
        cmd = f"{broker_env_str}cd '{working_dir}' && sandbox --sandbox-profile '{sandbox_profile_id}'{env_file_args} -- claude --dangerously-skip-permissions{system_prompt_arg}{prompt_arg}"

        result = subprocess.run(
            ["tmux", "new-window", "-d", "-t", session_name, "-n", window_name, cmd],
            capture_output=True,
        )
        if result.returncode != 0:
            return {"ok": False, "error": f"tmux window creation failed: {result.stderr.decode().strip()}"}

        activate = request.get("activate", False)
        if activate:
            sel = subprocess.run(
                ["tmux", "select-window", "-t", f"{session_name}:{window_name}"],
                capture_output=True,
            )
            if sel.returncode != 0:
                log.warning(f"select-window failed: {sel.stderr.decode().strip()}")

        log.info(f"Opened interactive session: {session_name}:{window_name}")
        return {"ok": True, "message": f"Opened {session_name}:{window_name}", "window": window_name}

    async def cmd_status(self, _request: dict) -> dict:
        jobs = [j.to_dict() for j in self.jobs.values()]
        return {"ok": True, "jobs": jobs}

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

    # --- クリーンアップ・シャットダウン ---

    async def cleanup_old_logs(self):
        job_max_age = 24 * 3600
        log_max_age = 6 * 3600
        while True:
            await asyncio.sleep(1800)
            try:
                log_dir = self._log_path("_").parent
                now = datetime.now().timestamp()
                for p in list(log_dir.glob("*.log")) + list(log_dir.glob("*.pid")) + list(log_dir.glob("*.exit")):
                    if now - p.stat().st_mtime > log_max_age:
                        p.unlink()
                        log.info(f"Cleaned up old file: {p.name}")

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

    server = OrchestratorServer(max_slots=max_slots, repo_dir=repo_dir)
    server._load_jobs()

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(_shutdown(server, srv, broker_srv)))
    loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(_shutdown(server, srv, broker_srv)))

    srv = await asyncio.start_unix_server(server.handle_client, path=socket_path)
    os.chmod(socket_path, 0o600)

    broker_srv = await asyncio.start_unix_server(server.host_cmd_broker.handle_client, path=broker_path)
    os.chmod(broker_path, 0o600)

    log.info(f"Orchestrator server started: socket={socket_path} host_cmd_broker={broker_path} max_slots={max_slots} repo={repo_dir}")

    asyncio.create_task(server.cleanup_old_logs())

    async with srv, broker_srv:
        await srv.serve_forever()


async def _shutdown(server: OrchestratorServer, srv, broker_srv):
    await server.shutdown()
    srv.close()
    broker_srv.close()
