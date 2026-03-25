"""Reducer — 純粋な状態遷移関数。"""

import copy

from lib.task_store import TaskData
from .models import Action


# update_field で更新可能なフィールドのホワイトリスト
# ドット区切りでネストフィールドに対応 (例: "pr.url")
UPDATABLE_FIELDS = frozenset({
    "pr.url", "pr.ci_status",
    "branch", "dispatch_id", "session_id",
    "plan_session_id", "related_jobs",
})


def _set_nested(obj, field_path: str, value) -> str | None:
    """ドット区切りフィールドパスで値を設定する。エラー時はメッセージを返す。"""
    parts = field_path.split(".")
    target = obj
    for part in parts[:-1]:
        if not hasattr(target, part):
            return f"update_field: '{field_path}' は存在しません"
        target = getattr(target, part)
    leaf = parts[-1]
    if not hasattr(target, leaf):
        return f"update_field: '{field_path}' は存在しません"
    setattr(target, leaf, value)
    return None


def reduce(task: TaskData, action: Action) -> TaskData | str:
    """(state, action) → new_state。

    成功時は更新済み TaskData（コピー）を返す。
    失敗時はエラーメッセージ文字列を返す。
    """
    new = copy.copy(task)

    match (task.status, action.type):
        # --- ダッシュボード操作 ---
        case ("pending", "plan"):
            new.status = "planning"

        case ("planning", "plan"):
            pass  # planning のまま（再精査）

        case ("planning", "dispatch"):
            new.status = "executing"

        case ("in_review", "request_resume"):
            new.resume_requested = True

        case ("in_review", "request_feedback"):
            new.feedback_requested = True

        case ("in_review", "done"):
            new.status = "done"

        case (_, "abort"):
            new.status = "aborted"

        # --- フックからのアクション ---
        case ("executing", "job_completed"):
            new.status = "in_review"

        case ("in_review", "feedback_collected"):
            new.status = "executing"
            new.feedback_requested = False

        case ("in_review", "ci_failed"):
            new.status = "executing"
            new.pr.ci_status = ""

        case (_, "clear_resume"):
            new.resume_requested = False

        case (_, "update_field"):
            if not action.field:
                return "update_field: field が未指定です"
            if action.field not in UPDATABLE_FIELDS:
                return f"update_field: '{action.field}' は更新不可です"
            err = _set_nested(new, action.field, action.value)
            if err:
                return err

        case _:
            return f"無効なアクション: {action.type} (現在: {task.status})"

    return new
