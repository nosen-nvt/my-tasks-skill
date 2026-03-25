"""tmux セッション管理ヘルパー。"""

import os
import subprocess

from .models import DEFAULT_SESSION_NAME


def detect_tmux_session(explicit: str | None) -> tuple[str, bool]:
    """tmux セッション名を検出する。

    Returns: (session_name, is_caller_session)
    """
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


def ensure_tmux_session(session_name: str, is_caller_session: bool) -> bool:
    """tmux セッションの存在を確認し、必要なら作成する。"""
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


def has_tmux_window(session_name: str, window_name: str) -> bool:
    """指定した tmux ウィンドウが存在するか確認する。"""
    result = subprocess.run(
        ["tmux", "has-session", "-t", f"{session_name}:{window_name}"],
        capture_output=True,
    )
    return result.returncode == 0
