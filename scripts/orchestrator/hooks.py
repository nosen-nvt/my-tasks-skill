"""Hook 定義と HookEvaluator。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Coroutine

from lib.task_store import TaskData


@dataclass
class Hook:
    """フック定義。"""
    id: str
    type: str               # "builtin" | "script" | "dispatch"
    when: dict              # 事前条件
    handler: Callable[..., Coroutine] | None = None  # builtin 用
    command: str = ""       # script 用
    prompt_file: str = ""   # dispatch 用


def _resolve_value(obj, field_name: str):
    """ドット区切りフィールドパスで値を取得する。"""
    parts = field_name.split(".")
    value = obj
    for part in parts:
        value = getattr(value, part, None)
        if value is None:
            return None
    return value


def _match_condition(value, condition) -> bool:
    """単一の値と条件を比較する。"""
    if isinstance(condition, dict):
        if condition.get("not_empty"):
            if not value:
                return False
        if "eq" in condition:
            if str(value) != str(condition["eq"]):
                return False
        if "not_eq" in condition:
            if str(value) == str(condition["not_eq"]):
                return False
        return True
    elif isinstance(condition, bool):
        return bool(value) == condition
    else:
        return str(value) == str(condition)


def matches(when: dict, task: TaskData) -> bool:
    """全条件がタスクデータに一致するか判定する。

    ドット区切りでネストフィールドに対応 (例: {"pr.url": {"not_empty": true}})。
    """
    for field_name, condition in when.items():
        value = _resolve_value(task, field_name)
        if not _match_condition(value, condition):
            return False
    return True


class HookEvaluator:
    """タスクデータ変更後にフックを評価・起動する。"""

    def __init__(self, builtin_hooks: list[Hook]):
        self._builtin_hooks = builtin_hooks

    def evaluate(self, task: TaskData, repo_dir: Path | None = None) -> list[Hook]:
        """条件一致するフックのリストを返す。"""
        all_hooks = list(self._builtin_hooks) + self._load_project_hooks(task.project_id, repo_dir)
        return [h for h in all_hooks if matches(h.when, task)]

    def _load_project_hooks(self, project_id: str, repo_dir: Path | None) -> list[Hook]:
        """プロジェクト設定からフック定義を読み込む。"""
        if not project_id or not repo_dir:
            return []
        project_path = repo_dir / "projects" / f"{project_id}.json"
        if not project_path.exists():
            return []
        try:
            with open(project_path, encoding="utf-8") as f:
                project = json.load(f)
        except (json.JSONDecodeError, OSError):
            return []

        hooks_raw = project.get("hooks", [])
        hooks: list[Hook] = []
        for h in hooks_raw:
            hook = Hook(
                id=h.get("id", ""),
                type=h.get("type", "script"),
                when=h.get("when", {}),
                command=h.get("command", ""),
                prompt_file=h.get("prompt_file", ""),
            )
            hooks.append(hook)
        return hooks
