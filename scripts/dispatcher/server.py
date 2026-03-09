"""DispatchServer コア。"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sandbox_exec

from .models import (
    Job, Lifecycle, log, now_iso,
    DEFAULT_SESSION_NAME, SOCKET_DIR_NAME,
    get_socket_path, get_cred_broker_socket_path, get_repo_dir,
)
from .cred import CredentialBroker
from .lifecycle import LifecycleManager


class DispatchServer:
    def __init__(self, max_slots: int, repo_dir: Path):
        self.max_slots = max_slots
        self.repo_dir = repo_dir
        self.jobs: dict[str, Job] = {}
        self.queue: list[Job] = []
        self.waiters: dict[str, list[asyncio.Future]] = {}
        self._counter: dict[str, int] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self.cred_broker = CredentialBroker()

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
            sandbox_profile_id, env_files, allowed_credentials = \
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
            allowed_credentials=allowed_credentials,
        )
        self.jobs[dispatch_id] = job

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
            sandbox_profile_id, env_files, _ = \
                sandbox_exec.resolve_project_sandbox_params(
                    project, sandbox_profile_override=request.get("sandbox_profile"),
                )
        except (FileNotFoundError, ValueError) as e:
            return {"ok": False, "error": str(e)}

        session_name, is_caller = _detect_tmux_session(session)
        if not _ensure_tmux_session(session_name, is_caller):
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
            project_id = await self._classify_project(prompt)
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
            lifecycle_id=mgr.generate_id(),
            project_id=project_id,
            prompt=prompt,
            max_runs=max_runs,
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        mgr.lifecycles[lc.lifecycle_id] = lc
        if context:
            mgr._init_context(lc, context)
        mgr._save()

        await mgr.dispatch_refine(lc)

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
            Path(lc.context_path).write_text(context_update, encoding="utf-8")

        prev_reason = lc.suspend_reason

        if lc.suspend_reason == "needs_input":
            mgr._update_status(lc, "reshaping")
            await mgr.dispatch_refine(lc, prev_suspend_reason=prev_reason)
        elif lc.suspend_reason == "approval_required":
            mgr._update_status(lc, "running")
            await mgr.dispatch_execute(lc)
        elif lc.suspend_reason == "project_confirmation":
            new_project_id = request.get("project_id")
            if new_project_id:
                lc.project_id = new_project_id
            mgr._update_status(lc, "reshaping")
            await mgr.dispatch_refine(lc)
        else:
            return {"ok": False, "error": f"Unknown suspend reason: {lc.suspend_reason}"}

        return {
            "ok": True,
            "lifecycle_id": lc.lifecycle_id,
            "dispatch_id": lc.current_dispatch_id,
            "message": f"Lifecycle resumed: {lc.lifecycle_id}",
        }

    # --- ジョブ実行 ---

    async def execute_job(self, job: Job):
        job.status = "running"
        job.started_at = now_iso()

        cred_token = None
        if job.allowed_credentials:
            cred_token = uuid.uuid4().hex
            self.cred_broker.register(cred_token, job.allowed_credentials)

        system_prompt = self._build_system_prompt(job)
        log_path = self._log_path(job.dispatch_id)
        log_file = None
        try:
            cred_env: dict[str, str] | None = None
            if cred_token:
                cred_env = {"CRED_TOKEN": cred_token, "CRED_BROKER_SOCK": get_cred_broker_socket_path()}

            command = [
                "claude", "--permission-mode", "bypassPermissions",
                "-p", job.prompt,
                "--append-system-prompt", system_prompt,
            ]

            exec_args = sandbox_exec.build_exec_args(
                sandbox_profile=job.sandbox_profile_id,
                env_files=job.env_files,
                cred_env=cred_env,
                command=command,
                working_dir=job.working_dir,
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
            log.info(f"Job executing: {job.dispatch_id} pid={proc.pid} log={log_path}")

            exit_code = await proc.wait()
            job.exit_code = exit_code
            job.status = "done" if exit_code == 0 else "failed"
        except Exception as e:
            log.error(f"Job execution error: {job.dispatch_id}: {e}")
            job.status = "failed"
            job.exit_code = -1
        finally:
            if cred_token:
                self.cred_broker.revoke(cred_token)
            if log_file:
                log_file.close()
            job.finished_at = now_iso()
            self._processes.pop(job.dispatch_id, None)
            log.info(f"Job finished: {job.dispatch_id} status={job.status} exit_code={job.exit_code}")

        self._notify_waiters(job)

        if job.lifecycle_id:
            await self.lifecycle_mgr.on_job_complete(job)

        await self.drain_queue()

    async def drain_queue(self):
        while self.queue and self.count_running() < self.max_slots:
            running_projects = self.running_project_ids()
            idx = next((i for i, j in enumerate(self.queue) if j.project_id not in running_projects), None)
            if idx is None:
                break
            job = self.queue.pop(idx)
            asyncio.create_task(self.execute_job(job))

    async def _kill_job(self, job: Job):
        if job.status == "queued":
            self.queue = [j for j in self.queue if j.dispatch_id != job.dispatch_id]
            del self.jobs[job.dispatch_id]
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
            self._notify_waiters(job)

    def _notify_waiters(self, job: Job):
        waiters = self.waiters.pop(job.dispatch_id, [])
        result = {"ok": True, "dispatch_id": job.dispatch_id, "status": job.status, "exit_code": job.exit_code}
        for future in waiters:
            if not future.done():
                future.set_result(result)

    # --- 内部ディスパッチ ---

    async def _dispatch_internal(
        self, project_id: str, prompt: str, job_type: str,
        lifecycle_id: str | None = None, dispatch_id_hint: str | None = None,
    ) -> str:
        project = sandbox_exec.load_project(project_id, self.repo_dir)
        if not project:
            log.error(f"Internal dispatch: project not found: {project_id}")
            return ""

        working_dir = project.get("working_directory", "")
        if not working_dir or not Path(working_dir).is_dir():
            log.error(f"Internal dispatch: invalid working_directory for project: {project_id}")
            return ""

        try:
            sandbox_profile_id, env_files, allowed_credentials = \
                sandbox_exec.resolve_project_sandbox_params(project)
        except (FileNotFoundError, ValueError) as e:
            log.error(f"Internal dispatch: sandbox params error: {e}")
            return ""

        dispatch_id = dispatch_id_hint or self.generate_dispatch_id(project_id)

        job = Job(
            dispatch_id=dispatch_id,
            project_id=project_id,
            prompt=prompt,
            working_dir=working_dir,
            job_type=job_type,
            sandbox_profile_id=sandbox_profile_id,
            env_files=env_files,
            allowed_credentials=allowed_credentials,
            lifecycle_id=lifecycle_id,
        )
        self.jobs[dispatch_id] = job

        if self.count_running() < self.max_slots and project_id not in self.running_project_ids():
            asyncio.create_task(self.execute_job(job))
            log.info(f"Internal dispatch: {job_type} job started: {dispatch_id}")
        else:
            self.queue.append(job)
            log.info(f"Internal dispatch: {job_type} job queued: {dispatch_id}")

        return dispatch_id

    def _build_system_prompt(self, job: Job) -> str:
        profile = sandbox_exec.resolve_profile(job.sandbox_profile_id)
        network_protected = sandbox_exec.uses_network_protection(profile)

        network_desc = ""
        if network_protected:
            network_desc = """
制約事項 (ネットワーク保護あり):
- ネットワーク: GitHub/Bitbucket SSH と HTTP プロキシ経由の HTTPS のみ利用可能
- ファイル: 作業ディレクトリ内のファイルのみ変更可能"""

        cred_desc = ""
        if job.allowed_credentials:
            if job.allowed_credentials == "*":
                cred_desc = """

認証情報:
- `cred-get <entry>` または `pass show <entry>` で全ての認証情報を取得できます"""
            else:
                entries = "\n".join(f"  - {e}" for e in job.allowed_credentials)
                cred_desc = f"""

認証情報:
- `cred-get <entry>` または `pass show <entry>` で以下の認証情報を取得できます:
{entries}"""

        result_path = self._result_path(job.dispatch_id)
        result_desc = ""
        if job.job_type == "refine":
            result_desc = f"""

結果ファイル:
ジョブ完了時、以下のパスに結果 JSON を書き出してください:
  {result_path}

精査ジョブの結果フォーマット:
  {{"next_status": "scoped"}} — 精査完了、実行可能
  {{"next_status": "needs_input"}} — ユーザへの質問あり
  {{"next_status": "reshaping"}} — 再精査後、問題なし（完了確認待ち）"""
        elif job.job_type == "evaluate":
            result_desc = f"""

結果ファイル:
ジョブ完了時、以下のパスに結果 JSON を書き出してください:
  {result_path}

評価ジョブの結果フォーマット:
  {{"verdict": "PASS", "summary": "..."}} — 達成条件すべて満たされている
  {{"verdict": "RETRY", "summary": "..."}} — 再実行で修正可能
  {{"verdict": "BLOCKED", "summary": "..."}} — ユーザ入力が必要
  {{"verdict": "ABORT", "summary": "..."}} — 実行不可能"""

        network_mode = "保護あり (netns + proxy)" if network_protected else "ホストネットワーク直接"
        return f"""あなたはサンドボックス環境で実行されています。

実行環境:
- 作業ディレクトリ: {job.working_dir}
- ネットワーク: {network_mode}
{network_desc}{cred_desc}{result_desc}

作業が完了したら、変更をコミットしてください。
プロセスの終了がジョブ完了の通知になります（シグナルファイルは不要です）。"""

    # --- LLM プロジェクト判定 ---

    async def _classify_project(self, prompt: str) -> str | None:
        projects_dir = self.repo_dir / "projects"
        if not projects_dir.exists():
            return None

        projects = []
        for p in projects_dir.glob("*.json"):
            try:
                with open(p) as f:
                    proj = json.load(f)
                desc = proj.get("description", "")
                wd = proj.get("working_directory", "")
                if not wd:
                    continue
                projects.append(f"- {proj.get('project_id', p.stem)}: {proj.get('name', '')} - {desc}")
            except (json.JSONDecodeError, OSError):
                continue

        if not projects:
            return None

        classification_prompt = (
            "以下のタスク/依頼内容を読んで、最も適切なプロジェクトを選択してください。\n\n"
            f"# タスク内容\n{prompt}\n\n"
            f"# プロジェクト一覧\n" + "\n".join(projects) + "\n\n"
            "# 出力\nJSON で回答してください:\n"
            '{\"project_id\": \"選択したプロジェクトID\", \"confidence\": \"high\"|\"low\"}\n\n'
            "どのプロジェクトにも該当しない場合:\n"
            '{\"project_id\": null, \"reason\": \"理由\"}'
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "-p", classification_prompt, "--output-format", "json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return None

            result = json.loads(stdout.decode())
            if isinstance(result, list):
                for item in result:
                    if item.get("type") == "result":
                        inner = json.loads(item["result"])
                        project_id = inner.get("project_id")
                        if project_id and inner.get("confidence") != "low":
                            return project_id
                        elif project_id and inner.get("confidence") == "low":
                            return project_id
                        return None
            elif isinstance(result, dict):
                project_id = result.get("project_id")
                return project_id if project_id else None
        except Exception as e:
            log.error(f"Project classification error: {e}")

        return None

    # --- クリーンアップ・シャットダウン ---

    async def cleanup_old_logs(self):
        max_age = 6 * 3600
        while True:
            await asyncio.sleep(1800)
            try:
                log_dir = self._log_path("_").parent
                now = datetime.now().timestamp()
                done_lc_ids = {lc.lifecycle_id for lc in self.lifecycle_mgr.lifecycles.values() if lc.status == "done"}
                for p in list(log_dir.glob("*.log")) + list(log_dir.glob("*.result.json")) + list(log_dir.glob("*.context.md")):
                    if p.suffix == ".md" and p.stem.replace(".context", "") not in done_lc_ids:
                        continue
                    if now - p.stat().st_mtime > max_age:
                        p.unlink()
                        log.info(f"Cleaned up old file: {p.name}")
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
# tmux ヘルパー
# ---------------------------------------------------------------------------

def _detect_tmux_session(explicit: str | None) -> tuple[str, bool]:
    if explicit:
        result = subprocess.run(
            ["tmux", "has-session", "-t", explicit],
            capture_output=True,
        )
        if result.returncode != 0:
            return (explicit, True)
        return (explicit, True)

    if os.environ.get("TMUX"):
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#S"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return (result.stdout.strip(), True)

    result = subprocess.run(
        ["tmux", "list-clients", "-F", "#{client_session}"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        sessions = [s.strip() for s in result.stdout.strip().split("\n") if s.strip()]
        non_default = [s for s in sessions if s != DEFAULT_SESSION_NAME]
        if non_default:
            return (non_default[0], True)

    return (DEFAULT_SESSION_NAME, False)


def _ensure_tmux_session(session_name: str, is_caller_session: bool) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    )
    if result.returncode != 0:
        if is_caller_session:
            return False
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name, "-n", "_control"],
            capture_output=True,
        )
        return result.returncode == 0
    return True


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

    cred_broker_path = get_cred_broker_socket_path()
    if os.path.exists(cred_broker_path):
        os.unlink(cred_broker_path)

    server = DispatchServer(max_slots=max_slots, repo_dir=repo_dir)
    server.lifecycle_mgr._load()
    log.info(f"Loaded {len(server.lifecycle_mgr.lifecycles)} lifecycles ({sum(1 for lc in server.lifecycle_mgr.lifecycles.values() if lc.status == 'suspend')} suspended)")

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(_shutdown(server, srv, cred_srv)))
    loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(_shutdown(server, srv, cred_srv)))

    srv = await asyncio.start_unix_server(server.handle_client, path=socket_path)
    os.chmod(socket_path, 0o600)

    cred_srv = await asyncio.start_unix_server(server.cred_broker.handle_client, path=cred_broker_path)
    os.chmod(cred_broker_path, 0o600)

    log.info(f"Dispatch server started: socket={socket_path} cred_broker={cred_broker_path} max_slots={max_slots} repo={repo_dir}")

    asyncio.create_task(server.cleanup_old_logs())

    async with srv, cred_srv:
        await srv.serve_forever()


async def _shutdown(server: DispatchServer, srv, cred_srv):
    await server.shutdown()
    srv.close()
    await srv.wait_closed()
    cred_srv.close()
    await cred_srv.wait_closed()
    asyncio.get_event_loop().stop()
