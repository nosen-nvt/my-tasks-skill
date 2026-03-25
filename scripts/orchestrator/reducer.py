"""Reducer — 純粋な状態遷移関数。"""

import copy

from lib.task_store import TaskData
from .models import Action


# update_field で更新可能なフィールドのホワイトリスト
UPDATABLE_FIELDS = frozenset({
    "pr_url", "branch", "dispatch_id", "session_id",
    "plan_session_id", "actions_status", "actions_error",
    "related_jobs",
})


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

        case (_, "clear_resume"):
            new.resume_requested = False

        case (_, "update_field"):
            if not action.field:
                return "update_field: field が未指定です"
            if action.field not in UPDATABLE_FIELDS:
                return f"update_field: '{action.field}' は更新不可です"
            if not hasattr(new, action.field):
                return f"update_field: '{action.field}' は存在しません"
            setattr(new, action.field, action.value)

        case _:
            return f"無効なアクション: {action.type} (現在: {task.status})"

    return new
