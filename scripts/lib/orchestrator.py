"""Orchestrator — タスク状態遷移とアクション実行を集約するモジュール。

dashboard/app.py と将来の webhook/handler.py の両方から利用される。
"""

import asyncio
import importlib.util
import json
import uuid
from pathlib import Path
from typing import Callable, Awaitable

from .task_store import (
    IndexEntry, FeedbackItem, FeedbackGroup,
    load_index, save_index, load_task_yaml, save_task_yaml,
    update_task_yaml, now_iso,
)

# collect-feedback.py をモジュールとしてインポート
_script_dir = Path(__file__).resolve().parent.parent
_cf_spec = importlib.util.spec_from_file_location("collect_feedback", _script_dir / "collect-feedback.py")
_collect_feedback_mod = importlib.util.module_from_spec(_cf_spec)
_cf_spec.loader.exec_module(_collect_feedback_mod)


# ---------------------------------------------------------------------------
# 状態遷移定義
# ---------------------------------------------------------------------------

VALID_TRANSITIONS: dict[tuple[str, str], str] = {
    ("pending", "plan"): "planning",
    ("planning", "plan"): "planning",
    ("planning", "dispatch"): "executing",
    ("executing", "job_completed"): "in_review",
    ("in_review", "feedback"): "executing",
    ("in_review", "complete"): "done",
    ("pending", "abort"): "aborted",
    ("planning", "abort"): "aborted",
    ("executing", "abort"): "aborted",
    ("in_review", "abort"): "aborted",
}


def validate_transition(current_status: str, action: str) -> str | None:
    """遷移先ステータスを返す。無効な遷移の場合は None。"""
    return VALID_TRANSITIONS.get((current_status, action))


# ---------------------------------------------------------------------------
# ジョブ完了ハンドラ (sync — executor から直接呼ばれる)
# ---------------------------------------------------------------------------

def on_job_completed(
    repo_dir: Path,
    task_id: str,
    dispatch_id: str,
    exit_code: int,
) -> None:
    """executor がジョブ完了時に呼ぶ。executing → in_review に遷移する。"""
    if not task_id:
        return

    tasks_dir = repo_dir / "tasks"
    task_data = load_task_yaml(tasks_dir, task_id)
    if task_data is None:
        return

    new_status = validate_transition(task_data.status, "job_completed")
    if new_status is None:
        return

    task_data.status = new_status
    save_task_yaml(tasks_dir, task_id, task_data)

    index_entries = load_index(tasks_dir)
    for entry in index_entries:
        if entry.id == task_id:
            entry.status = new_status
            break
    save_index(tasks_dir, index_entries)


# ---------------------------------------------------------------------------
# Orchestrator クラス (async — dashboard / webhook から利用)
# ---------------------------------------------------------------------------

DispatcherSend = Callable[[dict], Awaitable[dict]]


class Orchestrator:
    """タスクのアクション実行と状態遷移を管理する。"""

    def __init__(self, repo_dir: Path, dispatcher_send: DispatcherSend):
        self.repo_dir = repo_dir
        self.tasks_dir = repo_dir / "tasks"
        self.datasources_dir = repo_dir / "datasources"
        self._dispatcher_send = dispatcher_send
        self._index_lock = asyncio.Lock()

    # --- ヘルパー ---

    def _load_project(self, project_id: str) -> dict | None:
        if not project_id:
            return None
        p_path = self.repo_dir / "projects" / f"{project_id}.json"
        if not p_path.exists():
            return None
        with open(p_path, encoding="utf-8") as f:
            return json.load(f)

    def _load_datasource(self, datasource_id: str) -> dict | None:
        if not datasource_id:
            return None
        ds_path = self.datasources_dir / f"{datasource_id}.json"
        if not ds_path.exists():
            return None
        with open(ds_path, encoding="utf-8") as f:
            return json.load(f)

    def _get_entry(self, index_entries: list[IndexEntry], task_id: str) -> IndexEntry | None:
        return next((e for e in index_entries if e.id == task_id), None)

    async def _update_status(self, task_id: str, action: str) -> tuple[str | None, str]:
        """ステータスを遷移させる。(new_status, error_message) を返す。"""
        async with self._index_lock:
            index_entries = load_index(self.tasks_dir)
            entry = self._get_entry(index_entries, task_id)
            if entry is None:
                return None, "not found"

            new_status = validate_transition(entry.status, action)
            if new_status is None:
                return None, f"無効な遷移: {entry.status} → ({action})"

            entry.status = new_status
            update_task_yaml(self.tasks_dir, entry)
            save_index(self.tasks_dir, index_entries)

        return new_status, ""

    # --- アクション ---

    async def plan(self, task_id: str) -> dict:
        """Plan: 対話セッションを起動してタスクを計画する。"""
        index_entries = load_index(self.tasks_dir)
        entry = self._get_entry(index_entries, task_id)
        if entry is None:
            return {"ok": False, "error": "not found"}

        new_status = validate_transition(entry.status, "plan")
        if new_status is None:
            return {"ok": False, "error": f"Plan は {entry.status} 状態では実行できません"}

        task_data = load_task_yaml(self.tasks_dir, task_id)
        if task_data is None:
            return {"ok": False, "error": "task yaml not found"}

        datasource = self._load_datasource(task_data.datasource_id)

        from dashboard.interactive_prompt import build_plan_session_prompts
        system_prompt, prompt = build_plan_session_prompts(task_data, datasource, str(self.repo_dir))

        request_data = {
            "command": "open",
            "system_prompt": system_prompt,
            "prompt": prompt,
            "window_name": f"plan-{task_id}",
            "activate": True,
        }
        if entry.project_id:
            request_data["project_id"] = entry.project_id

        try:
            result = await self._dispatcher_send(request_data)
        except (ConnectionRefusedError, FileNotFoundError):
            return {"ok": False, "error": "ディスパッチャーが起動していません"}

        if result.get("ok"):
            new_status, err = await self._update_status(task_id, "plan")
            if err:
                return {"ok": False, "error": err}

        return result

    async def dispatch(self, task_id: str) -> dict:
        """Dispatch: execute_prompt を使ってジョブを実行する。"""
        async with self._index_lock:
            index_entries = load_index(self.tasks_dir)
        entry = self._get_entry(index_entries, task_id)
        if entry is None:
            return {"ok": False, "error": "not found"}

        new_status = validate_transition(entry.status, "dispatch")
        if new_status is None:
            return {"ok": False, "error": f"Dispatch は {entry.status} 状態では実行できません"}

        task_data = load_task_yaml(self.tasks_dir, task_id)
        if task_data is None:
            return {"ok": False, "error": "task yaml not found"}

        prompt = task_data.execute_prompt
        if not prompt:
            return {"ok": False, "error": "execute_prompt が空です。先に Plan を実行してください"}

        project_id = entry.project_id
        if not project_id:
            return {"ok": False, "error": "project_id が未設定です"}

        session_id = str(uuid.uuid4())

        # プロジェクト設定から worktree ブランチを導出
        branch = None
        project = self._load_project(project_id)
        if project:
            worktree_config = project.get("worktree", {})
            if worktree_config.get("enabled"):
                from lib.worktree import resolve_branch_name
                template = worktree_config.get("branch_template", "task/{task_id}")
                context = {
                    "task_id": task_id,
                    "remote_id": task_data.remote_id or task_id,
                    "project_id": project_id,
                }
                branch = resolve_branch_name(template, context)

        run_request: dict = {
            "command": "run",
            "project_id": project_id,
            "prompt": prompt,
            "session_id": session_id,
            "task_id": task_id,
        }
        if branch:
            run_request["branch"] = branch

        try:
            result = await self._dispatcher_send(run_request)
        except (ConnectionRefusedError, FileNotFoundError):
            return {"ok": False, "error": "ディスパッチャーが起動していません"}

        if result.get("ok"):
            dispatch_id = result.get("dispatch_id", "")
            returned_session_id = result.get("session_id", session_id)

            async with self._index_lock:
                index_entries = load_index(self.tasks_dir)
                for e in index_entries:
                    if e.id == task_id:
                        e.status = "executing"
                        break
                save_index(self.tasks_dir, index_entries)

            task_data.status = "executing"
            task_data.dispatch_id = dispatch_id
            task_data.session_id = returned_session_id
            if branch:
                task_data.branch = branch
            save_task_yaml(self.tasks_dir, task_id, task_data)

        return result

    async def resume(self, task_id: str) -> dict:
        """Resume: 完了済みセッションを再開する（状態遷移なし）。"""
        task_data = load_task_yaml(self.tasks_dir, task_id)
        if task_data is None:
            return {"ok": False, "error": "task yaml not found"}

        if task_data.status != "in_review":
            return {"ok": False, "error": f"Resume は in_review 状態でのみ実行できます（現在: {task_data.status}）"}

        session_id = task_data.session_id
        if not session_id:
            return {"ok": False, "error": "session_id が記録されていません"}

        index_entries = load_index(self.tasks_dir)
        entry = self._get_entry(index_entries, task_id)
        project_id = entry.project_id if entry else task_data.project_id
        if not project_id:
            return {"ok": False, "error": "project_id が未設定です"}

        resume_request: dict = {
            "command": "resume",
            "project_id": project_id,
            "session_id": session_id,
            "window_name": f"resume-{task_id}",
            "activate": True,
        }
        if task_data.branch:
            from lib.worktree import worktree_path
            project = self._load_project(project_id)
            if project and project.get("working_directory"):
                wt = worktree_path(project["working_directory"], task_data.branch)
                if wt.is_dir():
                    resume_request["worktree"] = str(wt)

        try:
            result = await self._dispatcher_send(resume_request)
        except (ConnectionRefusedError, FileNotFoundError):
            return {"ok": False, "error": "ディスパッチャーが起動していません"}
        return result

    async def feedback(self, task_id: str) -> dict:
        """Feedback: フィードバックを収集し、対応ジョブを Dispatch する。"""
        index_entries = load_index(self.tasks_dir)
        entry = self._get_entry(index_entries, task_id)
        if entry is None:
            return {"ok": False, "error": "not found"}

        task_data = load_task_yaml(self.tasks_dir, task_id)
        if task_data is None:
            return {"ok": False, "error": "task yaml not found"}

        new_status = validate_transition(task_data.status, "feedback")
        if new_status is None:
            return {"ok": False, "error": f"Feedback は {task_data.status} 状態では実行できません"}

        # フィードバック収集
        collected = await asyncio.to_thread(
            self._collect_feedback, task_id,
        )

        if collected == 0:
            return {"ok": True, "message": "新しいフィードバックはありません", "collected": 0}

        # タスクデータを再読み込み
        task_data = load_task_yaml(self.tasks_dir, task_id)
        if task_data is None:
            return {"ok": False, "error": "task yaml not found"}

        project_id = entry.project_id
        if not project_id:
            return {"ok": True, "message": f"フィードバック {collected} 件収集（project_id 未設定のため Dispatch はスキップ）", "collected": collected}

        # 最新のフィードバックグループ
        latest_group = task_data.feedback[-1] if task_data.feedback else None
        if not latest_group:
            return {"ok": True, "message": f"フィードバック {collected} 件収集", "collected": collected}

        # フィードバック対応プロンプトの生成
        fb_lines = []
        for item in latest_group.items:
            fb_lines.append(f"- [{item.source}] {item.author}: {item.body}")
        fb_text = "\n".join(fb_lines)

        base_prompt = task_data.execute_prompt or task_data.description or task_data.title
        prompt = f"""{base_prompt}

# フィードバック対応 ({latest_group.collected_at})

以下のフィードバックに対応してください:

{fb_text}

変更をコミットし、PR を更新してください。"""

        session_id = str(uuid.uuid4())

        fb_run_request: dict = {
            "command": "run",
            "project_id": project_id,
            "prompt": prompt,
            "session_id": session_id,
            "task_id": task_id,
        }
        if task_data.branch:
            fb_run_request["branch"] = task_data.branch

        try:
            result = await self._dispatcher_send(fb_run_request)
        except (ConnectionRefusedError, FileNotFoundError):
            return {"ok": False, "error": "ディスパッチャーが起動していません"}

        if result.get("ok"):
            dispatch_id = result.get("dispatch_id", "")
            returned_session_id = result.get("session_id", session_id)

            task_data.status = "executing"
            task_data.dispatch_id = dispatch_id
            task_data.session_id = returned_session_id
            save_task_yaml(self.tasks_dir, task_id, task_data)

            async with self._index_lock:
                index_entries = load_index(self.tasks_dir)
                for e in index_entries:
                    if e.id == task_id:
                        e.status = "executing"
                        break
                save_index(self.tasks_dir, index_entries)

        return {**result, "collected": collected}

    async def complete(self, task_id: str) -> dict:
        """Done: in_review → done。"""
        new_status, err = await self._update_status(task_id, "complete")
        if err:
            return {"ok": False, "error": err}
        return {"ok": True}

    async def abort(self, task_id: str) -> dict:
        """Abort: any → aborted。executing の場合はジョブも kill。"""
        task_data = load_task_yaml(self.tasks_dir, task_id)
        if task_data is None:
            return {"ok": False, "error": "task yaml not found"}

        # executing の場合、ジョブを kill
        if task_data.status == "executing" and task_data.dispatch_id:
            try:
                await self._dispatcher_send({
                    "command": "kill",
                    "dispatch_id": task_data.dispatch_id,
                })
            except (ConnectionRefusedError, FileNotFoundError):
                pass

        new_status, err = await self._update_status(task_id, "abort")
        if err:
            return {"ok": False, "error": err}
        return {"ok": True}

    # --- フィードバック収集（sync） ---

    def _collect_feedback(self, task_id: str) -> int:
        task_data = load_task_yaml(self.tasks_dir, task_id)
        if not task_data:
            return 0

        collected_items: list[dict] = []

        # Jira コメント収集
        if task_data.datasource_id:
            datasource = _collect_feedback_mod.load_datasource(self.datasources_dir, task_data.datasource_id)
            if datasource and datasource.get("type") == "jira" and task_data.remote_id:
                site_mapping = datasource.get("site_mapping", {})
                site = _collect_feedback_mod.resolve_jira_site(task_data.remote_id, site_mapping)
                if site:
                    jira_cursor = task_data.feedback_cursor.get("jira_comment")
                    new_comments, new_cursor = _collect_feedback_mod.collect_jira_comments(
                        task_data.remote_id, site, jira_cursor,
                    )
                    collected_items.extend(new_comments)
                    if new_cursor:
                        task_data.feedback_cursor["jira_comment"] = new_cursor

        # Bitbucket PR コメント収集
        if task_data.pr_url:
            bb_cursor = task_data.feedback_cursor.get("bitbucket_pr")
            new_comments, new_cursor = _collect_feedback_mod.collect_bitbucket_pr_comments(
                task_data.pr_url, bb_cursor,
            )
            collected_items.extend(new_comments)
            if new_cursor:
                task_data.feedback_cursor["bitbucket_pr"] = new_cursor

        # GitHub PR コメント収集
        if task_data.pr_url:
            gh_cursor = task_data.feedback_cursor.get("github_pr")
            new_comments, new_cursor = _collect_feedback_mod.collect_github_pr_comments(
                task_data.pr_url, gh_cursor,
            )
            collected_items.extend(new_comments)
            if new_cursor:
                task_data.feedback_cursor["github_pr"] = new_cursor

        if collected_items:
            group = FeedbackGroup(
                collected_at=now_iso(),
                items=[FeedbackItem.from_dict(item) for item in collected_items],
            )
            task_data.feedback.append(group)
            save_task_yaml(self.tasks_dir, task_id, task_data)

        return len(collected_items)
