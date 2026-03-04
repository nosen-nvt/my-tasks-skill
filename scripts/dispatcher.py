#!/usr/bin/env python3
"""
dispatcher.py - Unix ドメインソケット C/S ジョブランナー

サーバ（systemd user service）:
  dispatcher.py server [--max-slots 3]

クライアント:
  echo "..." | dispatcher.py run --project bo
  dispatcher.py run --task 20260301-001
  dispatcher.py open --project bo [--session main]
  dispatcher.py status [--json]
  dispatcher.py cancel --id bo-1
  dispatcher.py kill --id bo-1
  dispatcher.py kill --all
  dispatcher.py wait --id bo-1
  dispatcher.py log --id bo-1
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

JST = timezone(timedelta(hours=9))

DEFAULT_MAX_SLOTS = 3
DEFAULT_REPO = "~/.local/share/my-tasks"
DEFAULT_SESSION_NAME = "dispatch"
DEFAULT_PROXY_PROFILES_DIR = "~/.local/share/my-tasks/proxy-profiles"
DEFAULT_SANDBOX_PROFILES_DIR = "~/.local/share/my-tasks/sandbox-profiles"
DEFAULT_CREDENTIAL_PROFILES_DIR = "~/.local/share/my-tasks/credential-profiles"

SOCKET_DIR_NAME = "my-tasks-dispatch"
SOCKET_FILE_NAME = "dispatcher.sock"
CRED_BROKER_SOCK_NAME = "cred-broker.sock"

log = logging.getLogger("dispatcher")


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def get_socket_path() -> str:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/tmp/run-{os.getuid()}")
    return os.path.join(runtime_dir, SOCKET_DIR_NAME, SOCKET_FILE_NAME)


def get_cred_broker_socket_path() -> str:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/tmp/run-{os.getuid()}")
    return os.path.join(runtime_dir, SOCKET_DIR_NAME, CRED_BROKER_SOCK_NAME)


def get_repo_dir(repo: str = DEFAULT_REPO) -> Path:
    return Path(repo).expanduser().resolve()


def load_json(path: Path) -> dict | None:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def load_project(repo_dir: Path, project_id: str) -> dict | None:
    return load_json(repo_dir / "projects" / f"{project_id}.json")


def load_proxy_profile(profile_id: str, profiles_dir: str = DEFAULT_PROXY_PROFILES_DIR) -> dict | None:
    profiles_path = Path(profiles_dir).expanduser().resolve()
    return load_json(profiles_path / f"{profile_id}.json")



BUILTIN_CREDENTIAL_PROFILES: dict[str, dict] = {
    "full-access": {
        "credential_profile_id": "full-access",
        "allowed_credentials": "*",
    },
    "none": {
        "credential_profile_id": "none",
        "allowed_credentials": [],
    },
}


BUILTIN_SANDBOX_PROFILES: dict[str, dict] = {
    "default": {
        "profile_id": "default",
        "proxy_profile": "full",
        "credential_profile": "full-access",
    },
    "unrestricted": {
        "profile_id": "unrestricted",
        "proxy_profile": None,
        "credential_profile": "full-access",
    },
}


def load_credential_profile(profile_id: str, profiles_dir: str = DEFAULT_CREDENTIAL_PROFILES_DIR) -> dict | None:
    """クレデンシャルプロファイルを解決する。ファイル → 組み込み の順。"""
    profiles_path = Path(profiles_dir).expanduser().resolve()
    result = load_json(profiles_path / f"{profile_id}.json")
    if result:
        return result
    return BUILTIN_CREDENTIAL_PROFILES.get(profile_id)


def resolve_allowed_credentials(sandbox_profile: dict) -> list[str] | str:
    """サンドボックスプロファイルから allowed_credentials を解決する。

    解決優先順位:
    1. allowed_credentials が直接存在 → そのまま返す（後方互換）
    2. credential_profile が指定 → credential profile を読み込んで返す
    3. どちらも未指定 → "*"（デフォルト全許可）
    """
    if "allowed_credentials" in sandbox_profile:
        return sandbox_profile["allowed_credentials"]

    credential_profile_id = sandbox_profile.get("credential_profile")
    if credential_profile_id:
        cred_profile = load_credential_profile(credential_profile_id)
        if not cred_profile:
            raise ValueError(f"Credential profile not found: {credential_profile_id}")
        return cred_profile["allowed_credentials"]

    return "*"


def load_sandbox_profile(profile_id: str, profiles_dir: str = DEFAULT_SANDBOX_PROFILES_DIR) -> dict | None:
    """サンドボックスプロファイルを解決する。ファイル → 組み込み の順。"""
    profiles_path = Path(profiles_dir).expanduser().resolve()
    result = load_json(profiles_path / f"{profile_id}.json")
    if result:
        return result
    return BUILTIN_SANDBOX_PROFILES.get(profile_id)


def _resolve_sandbox_profile_arg(profile_id: str, profiles_dir: str = DEFAULT_SANDBOX_PROFILES_DIR) -> str:
    """sandbox コマンドに渡す引数を決定する。ファイル存在時はパス、なければ名前。"""
    profiles_path = Path(profiles_dir).expanduser().resolve()
    file_path = profiles_path / f"{profile_id}.json"
    if file_path.is_file():
        return str(file_path)
    return profile_id


def is_inside_sandbox() -> bool:
    return os.environ.get("SANDBOX") == "1"


# ---------------------------------------------------------------------------
# Credential Broker
# ---------------------------------------------------------------------------

class CredentialBroker:
    """ジョブ単位でスコープされた認証情報アクセスを提供するブローカー。"""

    def __init__(self):
        self._registry: dict[str, list[str] | str] = {}  # token → allowed entries

    def register(self, token: str, allowed_entries: list[str] | str) -> None:
        self._registry[token] = allowed_entries
        if allowed_entries == "*":
            log.info(f"Credential broker: registered token {token[:8]}... (all entries)")
        else:
            log.info(f"Credential broker: registered token {token[:8]}... ({len(allowed_entries)} entries)")

    def revoke(self, token: str) -> None:
        if token in self._registry:
            del self._registry[token]
            log.info(f"Credential broker: revoked token {token[:8]}...")

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            data = await reader.readline()
            if not data:
                return
            request = json.loads(data.decode())
            token = request.get("token", "")
            entry = request.get("entry", "")
            operation = request.get("operation", "show")
            value = request.get("value", "")
            response = await self._process_request(token, entry, operation, value)
            writer.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
            await writer.drain()
        except Exception as e:
            log.error(f"Credential broker client error: {e}")
            try:
                writer.write(json.dumps({"ok": False, "error": "internal error"}).encode() + b"\n")
                await writer.drain()
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _process_request(self, token: str, entry: str, operation: str = "show", value: str = "") -> dict:
        if not token or token not in self._registry:
            log.warning(f"Credential broker: invalid token {token[:8]}..." if token else "Credential broker: empty token")
            return {"ok": False, "error": "invalid token"}

        allowed = self._registry[token]
        if allowed != "*" and entry not in allowed:
            log.warning(f"Credential broker: entry not allowed: {entry} (token {token[:8]}...)")
            return {"ok": False, "error": f"entry not allowed: {entry}"}

        try:
            if operation == "insert":
                proc = await asyncio.create_subprocess_exec(
                    "/usr/bin/pass", "insert", "--force", "--echo", entry,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate(input=value.encode())
                if proc.returncode != 0:
                    log.error(f"Credential broker: pass insert failed for {entry}: {stderr.decode().strip()}")
                    return {"ok": False, "error": "credential insert failed"}
                log.info(f"Credential broker: inserted {entry} (token {token[:8]}...)")
                return {"ok": True}
            else:
                proc = await asyncio.create_subprocess_exec(
                    "/usr/bin/pass", "show", entry,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode != 0:
                    log.error(f"Credential broker: pass failed for {entry}: {stderr.decode().strip()}")
                    return {"ok": False, "error": "credential retrieval failed"}
                log.info(f"Credential broker: served {entry} (token {token[:8]}...)")
                return {"ok": True, "value": stdout.decode()}
        except Exception as e:
            log.error(f"Credential broker: pass execution error: {e}")
            return {"ok": False, "error": "credential operation failed"}


# ---------------------------------------------------------------------------
# Job dataclass
# ---------------------------------------------------------------------------

@dataclass
class Job:
    dispatch_id: str
    project_id: str
    task_id: str | None
    prompt: str
    working_dir: str
    sandbox_profile_arg: str = "default"
    network_protected: bool = True
    proxy_port: int = 3128
    allowed_credentials: list[str] | str = field(default_factory=list)
    status: str = "queued"
    pid: int | None = None
    exit_code: int | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "dispatch_id": self.dispatch_id,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "status": self.status,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


# ---------------------------------------------------------------------------
# サーバ
# ---------------------------------------------------------------------------

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

    def _log_path(self, dispatch_id: str) -> Path:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/tmp/run-{os.getuid()}")
        return Path(runtime_dir) / SOCKET_DIR_NAME / f"{dispatch_id}.log"

    def generate_dispatch_id(self, project_id: str) -> str:
        self._counter.setdefault(project_id, 0)
        self._counter[project_id] += 1
        return f"{project_id}-{self._counter[project_id]}"

    def count_running(self) -> int:
        return sum(1 for j in self.jobs.values() if j.status == "running")

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
            }.get(command)

            if handler is None:
                response = {"ok": False, "error": f"Unknown command: {command}"}
            else:
                response = await handler(request)

            # wait コマンドの場合は Future を取り出してからレスポンスを送信
            future = response.pop("_future", None)

            writer.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
            await writer.drain()

            # wait コマンドの場合は Future の完了まで接続を保持
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
        task_id = request.get("task_id")
        prompt = request.get("prompt", "")

        if not project_id:
            return {"ok": False, "error": "project_id is required"}
        if not prompt:
            return {"ok": False, "error": "prompt is required"}

        project = load_project(self.repo_dir, project_id)
        if not project:
            return {"ok": False, "error": f"Project not found: {project_id}"}

        working_dir = project.get("working_directory", "")
        if not working_dir:
            return {"ok": False, "error": f"working_directory not set for project: {project_id}"}
        if not Path(working_dir).is_dir():
            return {"ok": False, "error": f"working_directory does not exist: {working_dir}"}

        profile_id = request.get("sandbox_profile") or project.get("sandbox_profile", "default")
        sandbox_profile_data = load_sandbox_profile(profile_id)
        if not sandbox_profile_data:
            return {"ok": False, "error": f"Sandbox profile not found: {profile_id}"}

        proxy_profile_id = sandbox_profile_data.get("proxy_profile")
        if proxy_profile_id:
            network_protected = True
            proxy_profile = load_proxy_profile(proxy_profile_id)
            proxy_port = proxy_profile["port"] if proxy_profile and "port" in proxy_profile else 3128
        else:
            network_protected = False
            proxy_port = 3128
        try:
            allowed_credentials = resolve_allowed_credentials(sandbox_profile_data)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        sandbox_profile_arg = _resolve_sandbox_profile_arg(profile_id)

        dispatch_id = self.generate_dispatch_id(project_id)

        job = Job(
            dispatch_id=dispatch_id,
            project_id=project_id,
            task_id=task_id,
            prompt=prompt,
            working_dir=working_dir,
            sandbox_profile_arg=sandbox_profile_arg,
            network_protected=network_protected,
            proxy_port=proxy_port,
            allowed_credentials=allowed_credentials,
        )
        self.jobs[dispatch_id] = job

        if self.count_running() < self.max_slots:
            asyncio.create_task(self.execute_job(job))
            log.info(f"Job started: {dispatch_id}")
            return {"ok": True, "dispatch_id": dispatch_id, "message": "Job started"}
        else:
            self.queue.append(job)
            log.info(f"Job queued: {dispatch_id}")
            return {"ok": True, "dispatch_id": dispatch_id, "message": "Job queued (slot full)"}

    async def cmd_open(self, request: dict) -> dict:
        project_id = request.get("project_id", "")
        session = request.get("session")

        if not project_id:
            return {"ok": False, "error": "project_id is required"}

        project = load_project(self.repo_dir, project_id)
        if not project:
            return {"ok": False, "error": f"Project not found: {project_id}"}

        working_dir = project.get("working_directory", "")
        if not working_dir:
            return {"ok": False, "error": f"working_directory not set for project: {project_id}"}
        if not Path(working_dir).is_dir():
            return {"ok": False, "error": f"working_directory does not exist: {working_dir}"}

        profile_id = request.get("sandbox_profile") or project.get("sandbox_profile", "default")
        sandbox_profile_data = load_sandbox_profile(profile_id)
        if not sandbox_profile_data:
            return {"ok": False, "error": f"Sandbox profile not found: {profile_id}"}

        proxy_profile_id = sandbox_profile_data.get("proxy_profile")
        if proxy_profile_id:
            proxy_profile = load_proxy_profile(proxy_profile_id)
            proxy_port = proxy_profile["port"] if proxy_profile and "port" in proxy_profile else 3128
        else:
            proxy_port = 3128
        sandbox_profile_arg = _resolve_sandbox_profile_arg(profile_id)

        # tmux セッション決定
        session_name, is_caller = _detect_tmux_session(session)
        if not _ensure_tmux_session(session_name, is_caller):
            return {"ok": False, "error": f"tmux session '{session_name}' not available"}

        window_name = f"{project_id}-interactive"
        env_file_arg = ""
        env_file = Path(working_dir) / ".env"
        if env_file.is_file():
            env_file_arg = f" --env-file '{env_file}'"
        cmd = f"cd '{working_dir}' && sandbox --sandbox-profile '{sandbox_profile_arg}'{env_file_arg} --proxy-port {proxy_port} -- claude --permission-mode bypassPermissions"

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

        # Future を作成して待機
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self.waiters.setdefault(dispatch_id, []).append(future)
        return {"ok": True, "waiting": True, "dispatch_id": dispatch_id, "_future": future}

    # --- ジョブ実行 ---

    async def execute_job(self, job: Job):
        job.status = "running"
        job.started_at = now_iso()

        # Credential Broker: トークン生成・登録
        cred_token = None
        if job.allowed_credentials:
            cred_token = uuid.uuid4().hex
            self.cred_broker.register(cred_token, job.allowed_credentials)

        system_prompt = self._build_system_prompt(job)
        log_path = self._log_path(job.dispatch_id)
        log_file = None
        try:
            env = {**os.environ, "SANDBOX": "1"}
            if cred_token:
                env["CRED_TOKEN"] = cred_token
                env["CRED_BROKER_SOCK"] = get_cred_broker_socket_path()

            log_file = open(log_path, "w", encoding="utf-8")
            sandbox_args = [
                "sandbox",
                "--sandbox-profile", job.sandbox_profile_arg,
            ]
            env_file = Path(job.working_dir) / ".env"
            if env_file.is_file():
                sandbox_args += ["--env-file", str(env_file)]
            sandbox_args += [
                "--proxy-port", str(job.proxy_port),
                "--",
            ]

            proc = await asyncio.create_subprocess_exec(
                *sandbox_args,
                "claude", "--permission-mode", "bypassPermissions",
                "-p", job.prompt,
                "--append-system-prompt", system_prompt,
                cwd=job.working_dir,
                env=env,
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
        await self.drain_queue()

    async def drain_queue(self):
        while self.queue and self.count_running() < self.max_slots:
            job = self.queue.pop(0)
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

    def _build_system_prompt(self, job: Job) -> str:
        network_desc = ""
        if job.network_protected:
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

        network_mode = "保護あり (netns + proxy)" if job.network_protected else "ホストネットワーク直接"
        return f"""あなたはサンドボックス環境で実行されています。

実行環境:
- 作業ディレクトリ: {job.working_dir}
- ネットワーク: {network_mode}
{network_desc}{cred_desc}

作業が完了したら、変更をコミットしてください。
プロセスの終了がジョブ完了の通知になります（シグナルファイルは不要です）。"""

    async def cleanup_old_logs(self):
        """6時間以上経過したログファイルを定期的に削除する。"""
        max_age = 6 * 3600
        while True:
            await asyncio.sleep(1800)  # 30分間隔
            try:
                log_dir = self._log_path("_").parent
                now = datetime.now().timestamp()
                for p in log_dir.glob("*.log"):
                    if now - p.stat().st_mtime > max_age:
                        p.unlink()
                        log.info(f"Cleaned up old log: {p.name}")
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
# tmux ヘルパー（サーバ側 open コマンド用）
# ---------------------------------------------------------------------------

def _detect_tmux_session(explicit: str | None) -> tuple[str, bool]:
    if explicit:
        result = subprocess.run(
            ["tmux", "has-session", "-t", explicit],
            capture_output=True,
        )
        if result.returncode != 0:
            return (explicit, True)  # 存在しないが is_caller=True で ensure で弾く
        return (explicit, True)

    if os.environ.get("TMUX"):
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#S"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return (result.stdout.strip(), True)

    # サーバ環境（TMUX 未設定）: アタッチ中のクライアントセッションを検出
    result = subprocess.run(
        ["tmux", "list-clients", "-F", "#{client_session}"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        sessions = [s.strip() for s in result.stdout.strip().split("\n") if s.strip()]
        # デフォルト(dispatch)以外のセッションを優先
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
    logging.basicConfig(
        level=logging.INFO,
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

    # 既存ソケットを削除
    if os.path.exists(socket_path):
        os.unlink(socket_path)

    cred_broker_path = get_cred_broker_socket_path()
    if os.path.exists(cred_broker_path):
        os.unlink(cred_broker_path)

    server = DispatchServer(max_slots=max_slots, repo_dir=repo_dir)

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


# ---------------------------------------------------------------------------
# クライアント
# ---------------------------------------------------------------------------

async def client_send(request: dict, wait_response: bool = False) -> dict:
    """サーバにリクエストを送信し、レスポンスを受信する。"""
    socket_path = get_socket_path()
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path)
    except (ConnectionRefusedError, FileNotFoundError):
        if is_inside_sandbox():
            print(
                "エラー: ディスパッチサーバが起動していません。\n"
                "ホスト側で起動してください: systemctl --user start my-tasks-dispatcher",
                file=sys.stderr,
            )
            sys.exit(1)
        else:
            # ホスト環境: サーバをバックグラウンド起動
            _start_server_background()
            for _ in range(10):
                await asyncio.sleep(0.5)
                try:
                    reader, writer = await asyncio.open_unix_connection(socket_path)
                    break
                except (ConnectionRefusedError, FileNotFoundError):
                    continue
            else:
                print("エラー: サーバへの接続に失敗しました", file=sys.stderr)
                sys.exit(1)

    writer.write(json.dumps(request, ensure_ascii=False).encode() + b"\n")
    await writer.drain()

    data = await reader.readline()
    response = json.loads(data.decode())

    # wait コマンドの場合、2つ目のレスポンスを待つ
    if wait_response and response.get("ok") and response.get("waiting"):
        data = await reader.readline()
        if data:
            response = json.loads(data.decode())

    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return response


def _start_server_background():
    """サーバをバックグラウンドで起動する。"""
    script_path = os.path.abspath(__file__)
    subprocess.Popen(
        [sys.executable, script_path, "server"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# ---------------------------------------------------------------------------
# タスク情報の読み取り
# ---------------------------------------------------------------------------

def load_task_info(repo_dir: Path, task_id: str) -> dict | None:
    """index.jsonl からタスク情報を取得する。"""
    index_path = repo_dir / "tasks" / "index.jsonl"
    if not index_path.exists():
        return None
    with open(index_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("id") == task_id:
                    return entry
            except json.JSONDecodeError:
                continue
    return None


def read_execution_prompt(repo_dir: Path, task_id: str) -> str | None:
    """tasks/{task_id}.md から実行プロンプトセクションを読み取る。"""
    md_path = repo_dir / "tasks" / f"{task_id}.md"
    if not md_path.exists():
        return None

    with open(md_path, encoding="utf-8") as f:
        content = f.read()

    # "## 実行プロンプト" セクションを抽出
    marker = "## 実行プロンプト"
    idx = content.find(marker)
    if idx == -1:
        return None

    prompt_section = content[idx + len(marker):].strip()

    # 次のセクション（## で始まる行）があればそこまで
    lines = prompt_section.split("\n")
    result_lines = []
    for line in lines:
        if line.startswith("## "):
            break
        result_lines.append(line)

    return "\n".join(result_lines).strip() or None


# ---------------------------------------------------------------------------
# CLI コマンド
# ---------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> None:
    if args.task:
        # --task モード: index.jsonl + .md から情報を取得
        repo_dir = get_repo_dir(args.repo)
        task_info = load_task_info(repo_dir, args.task)
        if not task_info:
            print(f"エラー: タスクが見つかりません: {args.task}", file=sys.stderr)
            sys.exit(1)

        project_id = task_info.get("project_id", "")
        if not project_id:
            print(f"エラー: タスク {args.task} に project_id が設定されていません", file=sys.stderr)
            sys.exit(1)

        prompt = read_execution_prompt(repo_dir, args.task)
        if not prompt:
            print(f"エラー: タスク {args.task} に実行プロンプトがありません", file=sys.stderr)
            sys.exit(1)

        request = {
            "command": "run",
            "project_id": project_id,
            "task_id": args.task,
            "prompt": prompt,
        }
    else:
        # --project モード: stdin からプロンプト
        prompt = sys.stdin.read().strip()
        if not prompt:
            print("エラー: プロンプトが空です（stdin からプロンプトを読み取ります）", file=sys.stderr)
            sys.exit(1)

        request = {
            "command": "run",
            "project_id": args.project,
            "prompt": prompt,
        }

    if args.sandbox_profile:
        request["sandbox_profile"] = args.sandbox_profile

    response = asyncio.run(client_send(request))
    if response.get("ok"):
        print(f"{response.get('message', 'OK')}: {response.get('dispatch_id', '')}", file=sys.stderr)
    else:
        print(f"エラー: {response.get('error', 'Unknown error')}", file=sys.stderr)
        sys.exit(1)


def cmd_open(args: argparse.Namespace) -> None:
    request: dict[str, Any] = {
        "command": "open",
        "project_id": args.project,
    }
    if args.session:
        request["session"] = args.session
    if args.sandbox_profile:
        request["sandbox_profile"] = args.sandbox_profile

    response = asyncio.run(client_send(request))
    if response.get("ok"):
        print(f"{response.get('message', 'OK')}", file=sys.stderr)
    else:
        print(f"エラー: {response.get('error', 'Unknown error')}", file=sys.stderr)
        sys.exit(1)


def cmd_status(args: argparse.Namespace) -> None:
    request = {"command": "status"}
    response = asyncio.run(client_send(request))

    if not response.get("ok"):
        print(f"エラー: {response.get('error', 'Unknown error')}", file=sys.stderr)
        sys.exit(1)

    jobs = response.get("jobs", [])
    if not jobs:
        print("ジョブはありません", file=sys.stderr)
        return

    if args.json:
        print(json.dumps(jobs, ensure_ascii=False, indent=2))
    else:
        for job in jobs:
            pid_str = str(job.get("pid", "")) or "-"
            task_str = job.get("task_id", "") or "-"
            print(f"  {job['dispatch_id']:<20} {job['status']:<10} pid={pid_str:<8} task={task_str:<16} {job.get('started_at') or '-'}")

        counts: dict[str, int] = {}
        for job in jobs:
            s = job["status"]
            counts[s] = counts.get(s, 0) + 1
        parts = []
        for s in ["running", "queued", "done", "failed"]:
            if counts.get(s, 0) > 0:
                parts.append(f"{s}={counts[s]}")
        if parts:
            print(f"\n  [{', '.join(parts)}]")


def cmd_cancel(args: argparse.Namespace) -> None:
    request = {"command": "cancel", "dispatch_id": args.id}
    response = asyncio.run(client_send(request))
    if response.get("ok"):
        print(f"キャンセル: {args.id}", file=sys.stderr)
    else:
        print(f"エラー: {response.get('error', 'Unknown error')}", file=sys.stderr)
        sys.exit(1)


def cmd_kill(args: argparse.Namespace) -> None:
    if args.all:
        request = {"command": "kill-all"}
    else:
        request = {"command": "kill", "dispatch_id": args.id}

    response = asyncio.run(client_send(request))
    if response.get("ok"):
        print(response.get("message", "OK"), file=sys.stderr)
    else:
        print(f"エラー: {response.get('error', 'Unknown error')}", file=sys.stderr)
        sys.exit(1)


def cmd_wait(args: argparse.Namespace) -> None:
    request = {"command": "wait", "dispatch_id": args.id}
    response = asyncio.run(client_send(request, wait_response=True))
    if response.get("ok"):
        print(json.dumps(response, ensure_ascii=False, indent=2))
    else:
        print(f"エラー: {response.get('error', 'Unknown error')}", file=sys.stderr)
        sys.exit(1)


def cmd_log(args: argparse.Namespace) -> None:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/tmp/run-{os.getuid()}")
    log_path = Path(runtime_dir) / SOCKET_DIR_NAME / f"{args.id}.log"
    if not log_path.exists():
        print(f"エラー: ログファイルが見つかりません: {log_path}", file=sys.stderr)
        sys.exit(1)
    print(log_path.read_text(encoding="utf-8"), end="")


def cmd_server(args: argparse.Namespace) -> None:
    asyncio.run(run_server(max_slots=args.max_slots, repo=args.repo))


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unix ドメインソケット C/S ジョブランナー"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # server
    p_server = subparsers.add_parser("server", help="ディスパッチサーバを起動する")
    p_server.add_argument(
        "--max-slots", type=int, default=DEFAULT_MAX_SLOTS,
        help=f"最大並列スロット数（デフォルト: {DEFAULT_MAX_SLOTS}）",
    )
    p_server.add_argument(
        "--repo", default=DEFAULT_REPO, metavar="PATH",
        help=f"タスク管理リポジトリのパス（デフォルト: {DEFAULT_REPO}）",
    )
    p_server.set_defaults(func=cmd_server)

    # run
    p_run = subparsers.add_parser("run", help="ジョブを投入する")
    run_group = p_run.add_mutually_exclusive_group(required=True)
    run_group.add_argument("--task", help="タスク ID（index.jsonl + .md からプロンプトを読み取る）")
    run_group.add_argument("--project", help="プロジェクト ID（プロンプトは stdin から）")
    p_run.add_argument(
        "--repo", default=DEFAULT_REPO, metavar="PATH",
        help=f"タスク管理リポジトリのパス（デフォルト: {DEFAULT_REPO}）",
    )
    p_run.add_argument("--sandbox-profile", help="サンドボックスプロファイルを上書き指定")
    p_run.set_defaults(func=cmd_run)

    # open
    p_open = subparsers.add_parser("open", help="対話セッションを起動する")
    p_open.add_argument("--project", required=True, help="プロジェクト ID")
    p_open.add_argument("--session", default=None, help="tmux セッション名を明示指定")
    p_open.add_argument("--sandbox-profile", help="サンドボックスプロファイルを上書き指定")
    p_open.set_defaults(func=cmd_open)

    # status
    p_status = subparsers.add_parser("status", help="ジョブ状態を表示する")
    p_status.add_argument("--json", action="store_true", help="JSON 形式で出力する")
    p_status.set_defaults(func=cmd_status)

    # cancel
    p_cancel = subparsers.add_parser("cancel", help="キューからジョブを取り消す")
    p_cancel.add_argument("--id", required=True, help="ジョブ ID")
    p_cancel.set_defaults(func=cmd_cancel)

    # kill
    p_kill = subparsers.add_parser("kill", help="ジョブを強制停止する")
    group = p_kill.add_mutually_exclusive_group(required=True)
    group.add_argument("--id", help="ジョブ ID")
    group.add_argument("--all", action="store_true", help="全ジョブを停止する")
    p_kill.set_defaults(func=cmd_kill)

    # wait
    p_wait = subparsers.add_parser("wait", help="ジョブ完了を待機する")
    p_wait.add_argument("--id", required=True, help="ジョブ ID")
    p_wait.set_defaults(func=cmd_wait)

    # log
    p_log = subparsers.add_parser("log", help="ジョブの stdout/stderr ログを表示する")
    p_log.add_argument("--id", required=True, help="ジョブ ID")
    p_log.set_defaults(func=cmd_log)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
