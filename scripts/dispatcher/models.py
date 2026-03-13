"""定数・データクラス・ユーティリティ。"""

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))

DEFAULT_MAX_SLOTS = 8
DEFAULT_REPO = "~/.local/share/my-tasks"
DEFAULT_SESSION_NAME = "dispatch"

SOCKET_DIR_NAME = "my-tasks-dispatch"
SOCKET_FILE_NAME = "dispatcher.sock"
HOST_CMD_BROKER_SOCK_NAME = "host-cmd-broker.sock"

log = logging.getLogger("dispatcher")


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def get_socket_path() -> str:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/tmp/run-{os.getuid()}")
    return os.path.join(runtime_dir, SOCKET_DIR_NAME, SOCKET_FILE_NAME)


def get_host_cmd_broker_socket_path() -> str:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/tmp/run-{os.getuid()}")
    return os.path.join(runtime_dir, SOCKET_DIR_NAME, HOST_CMD_BROKER_SOCK_NAME)


def get_repo_dir(repo: str = DEFAULT_REPO):
    from pathlib import Path
    return Path(repo).expanduser().resolve()


def is_inside_sandbox() -> bool:
    return os.environ.get("SANDBOX") == "1"


@dataclass
class Lifecycle:
    lifecycle_id: str
    project_id: str
    prompt: str
    context_path: str = ""
    status: str = "reshaping"
    suspend_reason: str | None = None
    run_count: int = 0
    max_runs: int = 5
    current_dispatch_id: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "lifecycle_id": self.lifecycle_id,
            "project_id": self.project_id,
            "prompt": self.prompt,
            "context_path": self.context_path,
            "status": self.status,
            "suspend_reason": self.suspend_reason,
            "run_count": self.run_count,
            "max_runs": self.max_runs,
            "current_dispatch_id": self.current_dispatch_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class Job:
    dispatch_id: str
    project_id: str
    prompt: str
    working_dir: str
    job_type: str = "execute"  # "execute" | "evaluate" | "refine"
    sandbox_profile_id: str = "default"
    env_files: list[str] = field(default_factory=list)
    host_commands: list[dict] = field(default_factory=list)
    extra_binds: list[dict] = field(default_factory=list)
    lifecycle_id: str | None = None
    run: int | None = None
    status: str = "queued"
    pid: int | None = None
    exit_code: int | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "dispatch_id": self.dispatch_id,
            "project_id": self.project_id,
            "job_type": self.job_type,
            "lifecycle_id": self.lifecycle_id,
            "run": self.run,
            "status": self.status,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
