"""ビルトインフック — plan_session, dispatch_job, resume_session, abort_cleanup, feedback_collector。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from lib.task_store import (
    TaskData, FeedbackItem, FeedbackGroup,
    load_task_yaml, save_task_yaml, now_iso,
)
from .models import Action, log
from .hooks import Hook
from .tmux import detect_tmux_session, ensure_tmux_session, has_tmux_window

if TYPE_CHECKING:
    from .server import OrchestratorServer

# collect-feedback.py をモジュールとしてインポート
_script_dir = Path(__file__).resolve().parent.parent
_cf_spec = importlib.util.spec_from_file_location("collect_feedback", _script_dir / "collect-feedback.py")
assert _cf_spec is not None
_collect_feedback_mod = importlib.util.module_from_spec(_cf_spec)
assert _cf_spec.loader is not None
_cf_spec.loader.exec_module(_collect_feedback_mod)


# ---------------------------------------------------------------------------
# plan_session
# ---------------------------------------------------------------------------

async def hook_plan_session(server: OrchestratorServer, task_id: str, task: TaskData) -> None:
    """status=planning 時に tmux Plan セッションを開く。"""
    session_name, is_caller = detect_tmux_session(None)
    window_name = f"plan-{task_id}"

    # 冪等性: tmux 窓が既にあればアクティブにして終了
    if has_tmux_window(session_name, window_name):
        log.info(f"plan_session: window {window_name} already exists, activating")
        subprocess.run(
            ["tmux", "select-window", "-t", f"{session_name}:{window_name}"],
            capture_output=True,
        )
        return

    if not ensure_tmux_session(session_name, is_caller):
        log.error(f"plan_session: tmux session '{session_name}' not available")
        return

    datasource = _load_datasource(server, task)

    from dashboard.interactive_prompt import build_plan_session_prompts
    system_prompt, prompt = build_plan_session_prompts(task, datasource, str(server.repo_dir))

    # Plan セッション用の session_id を生成
    plan_sid = task.plan_session_id or str(uuid.uuid4())
    if not task.plan_session_id:
        await server.dispatch_action(task_id, Action(
            type="update_field", field="plan_session_id", value=plan_sid,
        ))

    result = await server.cmd_open({
        "project_id": task.project_id,
        "system_prompt": system_prompt,
        "prompt": prompt,
        "window_name": window_name,
        "activate": True,
    })
    if result.get("ok"):
        log.info(f"plan_session: opened {window_name}")
    else:
        log.error(f"plan_session: failed to open: {result.get('error')}")


# ---------------------------------------------------------------------------
# dispatch_job
# ---------------------------------------------------------------------------

async def hook_dispatch_job(server: OrchestratorServer, task_id: str, task: TaskData) -> None:
    """status=executing 時にジョブを実行する。"""
    # 冪等性: 既に running/queued ジョブがあれば何もしない
    for job in server.jobs.values():
        if job.task_id == task_id and job.status in ("running", "queued"):
            log.info(f"dispatch_job: job {job.dispatch_id} already running/queued for {task_id}, skipping")
            return

    if not task.project_id:
        log.error(f"dispatch_job: project_id not set for task {task_id}")
        return

    # プロンプト構築: 未対応フィードバックがあればフィードバック対応プロンプト
    prompt = _build_dispatch_prompt(task)
    if not prompt:
        log.error(f"dispatch_job: no execute_prompt for task {task_id}")
        return

    # ジョブ投入
    session_id = str(uuid.uuid4())
    run_request: dict = {
        "command": "run",
        "project_id": task.project_id,
        "prompt": prompt,
        "session_id": session_id,
        "task_id": task_id,
    }

    # worktree ブランチ
    branch = task.branch
    project = None
    if not branch:
        project = _load_project(server, task.project_id)
        if project:
            worktree_config = project.get("worktree", {})
            if worktree_config.get("enabled"):
                from lib.worktree import resolve_branch_name
                template = worktree_config.get("branch_template", "task/{task_id}")
                context = {
                    "task_id": task_id,
                    "remote_id": task.remote_id or task_id,
                    "project_id": task.project_id,
                }
                branch = resolve_branch_name(template, context)

    if branch:
        run_request["branch"] = branch
        base_branch = "main"
        if not project:
            project = _load_project(server, task.project_id)
        if project:
            worktree_config = project.get("worktree", {})
            base_branch = worktree_config.get("base_branch", "main")
        run_request["base_branch"] = base_branch

    result = await server.cmd_run(run_request)
    if result.get("ok"):
        dispatch_id = result.get("dispatch_id", "")
        returned_session_id = result.get("session_id", session_id)
        # タスクデータにジョブ情報を書き込む
        task_data = load_task_yaml(server.tasks_dir, task_id)
        if task_data:
            task_data.dispatch_id = dispatch_id
            task_data.session_id = returned_session_id
            if branch:
                task_data.branch = branch
            save_task_yaml(server.tasks_dir, task_id, task_data)
        log.info(f"dispatch_job: dispatched {dispatch_id} for {task_id}")
    else:
        log.error(f"dispatch_job: failed to dispatch: {result.get('error')}")


def _build_dispatch_prompt(task: TaskData) -> str:
    """フィードバックがあればフィードバック対応プロンプト、なければ execute_prompt。"""
    base_prompt = task.execute_prompt
    if not base_prompt:
        return ""

    # 最新のフィードバックグループがあり、フィードバック対応が必要な場合
    if task.feedback and not task.feedback_requested:
        latest_group = task.feedback[-1]
        if latest_group.items:
            fb_lines = []
            for item in latest_group.items:
                fb_lines.append(f"- [{item.source}] {item.author}: {item.body}")
            fb_text = "\n".join(fb_lines)
            return f"""{base_prompt}

# フィードバック対応 ({latest_group.collected_at})

以下のフィードバックに対応してください:

{fb_text}

変更をコミットし、PR を更新してください。"""

    return base_prompt


# ---------------------------------------------------------------------------
# resume_session
# ---------------------------------------------------------------------------

async def hook_resume_session(server: OrchestratorServer, task_id: str, task: TaskData) -> None:
    """resume_requested=true 時に tmux Resume セッションを開く。"""
    session_name, is_caller = detect_tmux_session(None)
    window_name = f"resume-{task_id}"

    # 冪等性: tmux 窓が既にあれば何もしない
    if has_tmux_window(session_name, window_name):
        log.info(f"resume_session: window {window_name} already exists, skipping")
        return

    if not task.session_id:
        log.error(f"resume_session: no session_id for task {task_id}")
        return

    if not task.project_id:
        log.error(f"resume_session: no project_id for task {task_id}")
        return

    resume_request: dict = {
        "command": "resume",
        "project_id": task.project_id,
        "session_id": task.session_id,
        "window_name": window_name,
        "activate": True,
    }

    # worktree パスの解決
    if task.branch:
        from lib.worktree import worktree_path
        project = _load_project(server, task.project_id)
        if project and project.get("working_directory"):
            wt = worktree_path(project["working_directory"], task.branch)
            if wt.is_dir():
                resume_request["worktree"] = str(wt)

    result = await server.cmd_resume(resume_request)
    if result.get("ok"):
        log.info(f"resume_session: opened {window_name}")
        # 窓をポーリングし、閉じたら clear_resume を dispatch
        asyncio.create_task(_poll_resume_window(server, task_id, session_name, window_name))
    else:
        log.error(f"resume_session: failed: {result.get('error')}")


async def _poll_resume_window(
    server: OrchestratorServer, task_id: str,
    session_name: str, window_name: str,
) -> None:
    """Resume tmux 窓を polling し、閉じたら clear_resume を dispatch。"""
    while True:
        await asyncio.sleep(5.0)
        if not has_tmux_window(session_name, window_name):
            log.info(f"resume_session: window {window_name} closed, clearing resume flag")
            await server.dispatch_action(task_id, Action(type="clear_resume"))
            return


# ---------------------------------------------------------------------------
# abort_cleanup
# ---------------------------------------------------------------------------

async def hook_abort_cleanup(server: OrchestratorServer, task_id: str, task: TaskData) -> None:
    """status=aborted 時に running ジョブを kill する。"""
    killed = False
    for job in list(server.jobs.values()):
        if job.task_id == task_id and job.status in ("running", "queued"):
            await server._kill_job(job)
            log.info(f"abort_cleanup: killed job {job.dispatch_id}")
            killed = True

    if not killed:
        log.info(f"abort_cleanup: no running jobs for {task_id}")


# ---------------------------------------------------------------------------
# feedback_collector
# ---------------------------------------------------------------------------

# フィードバック収集中のタスクを追跡（冪等性用）
_feedback_collecting: set[str] = set()


async def hook_feedback_collector(server: OrchestratorServer, task_id: str, task: TaskData) -> None:
    """feedback_requested=true 時にフィードバックを収集する。"""
    # 冪等性: 既に収集中なら何もしない
    if task_id in _feedback_collecting:
        log.info(f"feedback_collector: already collecting for {task_id}, skipping")
        return

    _feedback_collecting.add(task_id)
    try:
        collected = await asyncio.to_thread(_collect_feedback_sync, server, task_id)
        if collected == 0:
            log.info(f"feedback_collector: no new feedback for {task_id}")
        else:
            log.info(f"feedback_collector: collected {collected} items for {task_id}")

        # feedback_collected を dispatch → reducer が status=executing に遷移
        await server.dispatch_action(task_id, Action(type="feedback_collected"))
    except Exception as e:
        log.error(f"feedback_collector: error for {task_id}: {e}")
    finally:
        _feedback_collecting.discard(task_id)


def _collect_feedback_sync(server: OrchestratorServer, task_id: str) -> int:
    """フィードバック収集（同期）。collect-feedback.py のロジックを利用。"""
    task_data = load_task_yaml(server.tasks_dir, task_id)
    if not task_data:
        return 0

    collected_items: list[dict] = []

    # Jira コメント収集
    if task_data.datasource_id:
        datasource = _collect_feedback_mod.load_datasource(server.datasources_dir, task_data.datasource_id)
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
    if task_data.pr.url:
        bb_cursor = task_data.feedback_cursor.get("bitbucket_pr")
        new_comments, new_cursor = _collect_feedback_mod.collect_bitbucket_pr_comments(
            task_data.pr.url, bb_cursor,
        )
        collected_items.extend(new_comments)
        if new_cursor:
            task_data.feedback_cursor["bitbucket_pr"] = new_cursor

    # GitHub PR コメント収集
    if task_data.pr.url:
        gh_cursor = task_data.feedback_cursor.get("github_pr")
        new_comments, new_cursor = _collect_feedback_mod.collect_github_pr_comments(
            task_data.pr.url, gh_cursor,
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
        save_task_yaml(server.tasks_dir, task_id, task_data)

    return len(collected_items)


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _load_project(server: OrchestratorServer, project_id: str) -> dict | None:
    if not project_id:
        return None
    p_path = server.repo_dir / "projects" / f"{project_id}.json"
    if not p_path.exists():
        return None
    with open(p_path, encoding="utf-8") as f:
        return json.load(f)


def _load_datasource(server: OrchestratorServer, task: TaskData) -> dict | None:
    if not task.datasource_id:
        return None
    ds_path = server.datasources_dir / f"{task.datasource_id}.json"
    if not ds_path.exists():
        return None
    with open(ds_path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# ビルトインフック定義
# ---------------------------------------------------------------------------

BUILTIN_HOOKS = [
    Hook(
        id="plan_session",
        type="builtin",
        when={"status": "planning"},
        handler=hook_plan_session,
    ),
    Hook(
        id="dispatch_job",
        type="builtin",
        when={"status": "executing"},
        handler=hook_dispatch_job,
    ),
    Hook(
        id="resume_session",
        type="builtin",
        when={"resume_requested": True},
        handler=hook_resume_session,
    ),
    Hook(
        id="abort_cleanup",
        type="builtin",
        when={"status": "aborted"},
        handler=hook_abort_cleanup,
    ),
    Hook(
        id="feedback_collector",
        type="builtin",
        when={"feedback_requested": True},
        handler=hook_feedback_collector,
    ),
]
