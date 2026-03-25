"""定数・データクラス・ユーティリティ。"""

import logging
import os
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))

DEFAULT_MAX_SLOTS = 8
DEFAULT_REPO = "~/.local/share/my-tasks"
DEFAULT_SESSION_NAME = "dispatch"

SOCKET_DIR_NAME = "my-tasks-dispatch"
SOCKET_FILE_NAME = "dispatcher.sock"
HOST_CMD_BROKER_SOCK_NAME = "host-cmd-broker.sock"

log = logging.getLogger("orchestrator")


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
class Job:
    dispatch_id: str
    project_id: str
    prompt: str
    working_dir: str
    sandbox_profile_id: str = "default"
    env_files: list[str] = dataclass_field(default_factory=list)
    host_commands: list[dict] = dataclass_field(default_factory=list)
    extra_binds: list[dict] = dataclass_field(default_factory=list)
    session_id: str = ""
    task_id: str = ""
    status: str = "queued"
    pid: int | None = None
    exit_code: int | None = None
    branch: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "dispatch_id": self.dispatch_id,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "status": self.status,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "branch": self.branch,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass
class Action:
    """Reducer が受け取るアクション。"""
    type: str
    field: str = ""
    value: str = ""
    payload: dict = dataclass_field(default_factory=dict)
