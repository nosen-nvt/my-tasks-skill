"""FastAPI アプリケーション (v2)。"""

import asyncio
import importlib.util
import json
import os
import uuid
from pathlib import Path

import yaml
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .watcher import FileWatcher
from lib.task_store import (
    IndexEntry, FeedbackItem, FeedbackGroup,
    load_index, save_index, load_task_yaml, save_task_yaml,
    update_task_yaml, now_iso,
)

STATIC_DIR = Path(__file__).parent / "static"

# collect-feedback.py をモジュールとしてインポート
_script_dir = Path(__file__).resolve().parent.parent
_cf_spec = importlib.util.spec_from_file_location("collect_feedback", _script_dir / "collect-feedback.py")
_collect_feedback_mod = importlib.util.module_from_spec(_cf_spec)
_cf_spec.loader.exec_module(_collect_feedback_mod)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    result = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return result


def read_json_dir(directory: Path) -> list[dict]:
    if not directory.is_dir():
        return []
    result = []
    for p in sorted(directory.glob("*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            data["_filename"] = p.stem
            result.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return result


def create_app(repo: str, base_path: str = "") -> FastAPI:
    repo_dir = Path(repo).expanduser().resolve()
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/run-{os.getuid()}"))
    dispatch_dir = runtime_dir / "my-tasks-dispatch"

    base_path = base_path.rstrip("/") if base_path else ""

    app = FastAPI(title="my-tasks dashboard", root_path=base_path)

    watcher: FileWatcher | None = None

    @app.on_event("startup")
    async def startup() -> None:
        nonlocal watcher
        loop = asyncio.get_event_loop()
        watcher = FileWatcher(repo_dir, runtime_dir, loop)
        watcher.start()

    @app.on_event("shutdown")
    async def shutdown() -> None:
        if watcher:
            watcher.stop()

    index_lock = asyncio.Lock()
    sync_state = {"running": False, "error": None}

    async def dispatcher_send(request: dict) -> dict:
        sock_path = dispatch_dir / "dispatcher.sock"
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        writer.write(json.dumps(request, ensure_ascii=False).encode() + b"\n")
        await writer.drain()
        data = await reader.readline()
        writer.close()
        await writer.wait_closed()
        return json.loads(data)

    # --- Read API ---

    @app.get("/api/projects")
    async def api_projects() -> list[dict]:
        projects = read_json_dir(repo_dir / "projects")
        tasks = read_jsonl(repo_dir / "tasks" / "index.jsonl")
        counts: dict[str, dict[str, int]] = {}
        for t in tasks:
            pid = t.get("project_id") or t.get("project", "")
            status = t.get("status", "")
            counts.setdefault(pid, {})
            counts[pid][status] = counts[pid].get(status, 0) + 1
        for p in projects:
            pid = p.get("id") or p.get("_filename", "")
            p["task_counts"] = counts.get(pid, {})
        return projects

    @app.get("/api/datasources")
    async def api_datasources() -> list[dict]:
        return read_json_dir(repo_dir / "datasources")

    @app.get("/api/tasks")
    async def api_tasks() -> list[dict]:
        return read_jsonl(repo_dir / "tasks" / "index.jsonl")

    @app.get("/api/tasks/{task_id}")
    async def api_task_detail(task_id: str) -> JSONResponse:
        yaml_path = repo_dir / "tasks" / f"{task_id}.yaml"
        if yaml_path.exists():
            content = yaml_path.read_text(encoding="utf-8")
            try:
                data = yaml.safe_load(content)
                if isinstance(data, dict):
                    data["id"] = task_id
                    return JSONResponse(data)
            except Exception:
                pass
            return JSONResponse({"id": task_id, "content": content})
        return JSONResponse({"error": "not found"}, status_code=404)

    @app.get("/api/jobs")
    async def api_jobs() -> list[dict]:
        return read_jsonl(dispatch_dir / "jobs.jsonl")

    @app.get("/api/jobs/{dispatch_id}/log")
    async def api_job_log(dispatch_id: str, tail: int = Query(default=200)) -> JSONResponse:
        log_path = dispatch_dir / f"{dispatch_id}.log"
        if not log_path.exists():
            return JSONResponse({"error": "not found"}, status_code=404)
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        if tail and len(lines) > tail:
            lines = lines[-tail:]
        return JSONResponse({"dispatch_id": dispatch_id, "lines": lines})

    # --- Action API ---

    def _load_datasource(datasource_id: str) -> dict | None:
        if not datasource_id:
            return None
        ds_path = repo_dir / "datasources" / f"{datasource_id}.json"
        if not ds_path.exists():
            return None
        with open(ds_path, encoding="utf-8") as f:
            return json.load(f)

    def _load_project(project_id: str) -> dict | None:
        if not project_id:
            return None
        p_path = repo_dir / "projects" / f"{project_id}.json"
        if not p_path.exists():
            return None
        with open(p_path, encoding="utf-8") as f:
            return json.load(f)

    @app.post("/api/tasks/{task_id}/plan")
    async def api_plan(task_id: str) -> JSONResponse:
        """Plan: 対話セッションを起動してタスクを計画する。"""
        tasks_dir = repo_dir / "tasks"
        index_entries = load_index(tasks_dir)
        entry = next((e for e in index_entries if e.id == task_id), None)
        if entry is None:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)

        task_data = load_task_yaml(tasks_dir, task_id)
        if task_data is None:
            return JSONResponse({"ok": False, "error": "task yaml not found"}, status_code=404)

        datasource = _load_datasource(task_data.datasource_id)

        from dashboard.interactive_prompt import build_plan_session_prompts
        system_prompt, prompt = build_plan_session_prompts(task_data, datasource, str(repo_dir))

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
            result = await dispatcher_send(request_data)
        except (ConnectionRefusedError, FileNotFoundError):
            return JSONResponse({"ok": False, "error": "ディスパッチャーが起動していません"})
        return JSONResponse(result)

    @app.post("/api/tasks/{task_id}/dispatch")
    async def api_dispatch(task_id: str) -> JSONResponse:
        """Dispatch: execute_prompt を使ってジョブを実行する。"""
        tasks_dir = repo_dir / "tasks"

        async with index_lock:
            index_entries = load_index(tasks_dir)
        entry = next((e for e in index_entries if e.id == task_id), None)
        if entry is None:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)

        task_data = load_task_yaml(tasks_dir, task_id)
        if task_data is None:
            return JSONResponse({"ok": False, "error": "task yaml not found"}, status_code=404)

        prompt = task_data.execute_prompt
        if not prompt:
            return JSONResponse({"ok": False, "error": "execute_prompt が空です。先に Plan を実行してください"})

        project_id = entry.project_id
        if not project_id:
            return JSONResponse({"ok": False, "error": "project_id が未設定です"})

        session_id = str(uuid.uuid4())

        # プロジェクト設定から worktree ブランチを導出
        branch = None
        project = _load_project(project_id)
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
        }
        if branch:
            run_request["branch"] = branch

        try:
            result = await dispatcher_send(run_request)
        except (ConnectionRefusedError, FileNotFoundError):
            return JSONResponse({"ok": False, "error": "ディスパッチャーが起動していません"})

        if result.get("ok"):
            dispatch_id = result.get("dispatch_id", "")
            returned_session_id = result.get("session_id", session_id)

            # タスクを in_progress に更新し、dispatch_id と session_id を記録
            async with index_lock:
                index_entries = load_index(tasks_dir)
                for e in index_entries:
                    if e.id == task_id:
                        e.status = "in_progress"
                        break
                save_index(tasks_dir, index_entries)

            task_data.status = "in_progress"
            task_data.dispatch_id = dispatch_id
            task_data.session_id = returned_session_id
            if branch:
                task_data.branch = branch
            save_task_yaml(tasks_dir, task_id, task_data)

        return JSONResponse(result)

    @app.post("/api/tasks/{task_id}/resume")
    async def api_resume(task_id: str) -> JSONResponse:
        """Resume: 完了済みセッションを再開する。"""
        tasks_dir = repo_dir / "tasks"
        task_data = load_task_yaml(tasks_dir, task_id)
        if task_data is None:
            return JSONResponse({"ok": False, "error": "task yaml not found"}, status_code=404)

        session_id = task_data.session_id
        if not session_id:
            return JSONResponse({"ok": False, "error": "session_id が記録されていません"})

        index_entries = load_index(tasks_dir)
        entry = next((e for e in index_entries if e.id == task_id), None)
        project_id = entry.project_id if entry else task_data.project_id
        if not project_id:
            return JSONResponse({"ok": False, "error": "project_id が未設定です"})

        resume_request: dict = {
            "command": "resume",
            "project_id": project_id,
            "session_id": session_id,
            "window_name": f"resume-{task_id}",
            "activate": True,
        }
        # branch が記録されていれば worktree パスを渡す
        if task_data.branch:
            from lib.worktree import worktree_path
            project = _load_project(project_id)
            if project and project.get("working_directory"):
                wt = worktree_path(project["working_directory"], task_data.branch)
                if wt.is_dir():
                    resume_request["worktree"] = str(wt)

        try:
            result = await dispatcher_send(resume_request)
        except (ConnectionRefusedError, FileNotFoundError):
            return JSONResponse({"ok": False, "error": "ディスパッチャーが起動していません"})
        return JSONResponse(result)

    def _collect_feedback_for_task(tasks_dir: Path, datasources_dir: Path, task_id: str) -> int:
        """タスクに対するフィードバックを外部ソースから収集する。収集件数を返す。"""
        task_data = load_task_yaml(tasks_dir, task_id)
        if not task_data:
            return 0

        collected_items: list[dict] = []

        # Jira コメント収集
        if task_data.datasource_id:
            datasource = _collect_feedback_mod.load_datasource(datasources_dir, task_data.datasource_id)
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
            save_task_yaml(tasks_dir, task_id, task_data)

        return len(collected_items)

    @app.post("/api/tasks/{task_id}/feedback")
    async def api_feedback(task_id: str) -> JSONResponse:
        """Feedback: フィードバックを収集し、対応ジョブを Dispatch する。"""
        tasks_dir = repo_dir / "tasks"

        # フィードバック収集
        collected = await asyncio.to_thread(
            _collect_feedback_for_task, tasks_dir, repo_dir / "datasources", task_id,
        )

        if collected == 0:
            return JSONResponse({"ok": True, "message": "新しいフィードバックはありません", "collected": 0})

        # タスクデータを再読み込み
        task_data = load_task_yaml(tasks_dir, task_id)
        if task_data is None:
            return JSONResponse({"ok": False, "error": "task yaml not found"}, status_code=404)

        index_entries = load_index(tasks_dir)
        entry = next((e for e in index_entries if e.id == task_id), None)
        project_id = entry.project_id if entry else task_data.project_id
        if not project_id:
            return JSONResponse({"ok": True, "message": f"フィードバック {collected} 件収集（project_id 未設定のため Dispatch はスキップ）", "collected": collected})

        # 最新のフィードバックグループ
        latest_group = task_data.feedback[-1] if task_data.feedback else None
        if not latest_group:
            return JSONResponse({"ok": True, "message": f"フィードバック {collected} 件収集", "collected": collected})

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

        # 既存の branch を再利用（Feedback は同じブランチで作業する）
        fb_run_request: dict = {
            "command": "run",
            "project_id": project_id,
            "prompt": prompt,
            "session_id": session_id,
        }
        if task_data.branch:
            fb_run_request["branch"] = task_data.branch

        try:
            result = await dispatcher_send(fb_run_request)
        except (ConnectionRefusedError, FileNotFoundError):
            return JSONResponse({"ok": False, "error": "ディスパッチャーが起動していません"})

        if result.get("ok"):
            dispatch_id = result.get("dispatch_id", "")
            returned_session_id = result.get("session_id", session_id)
            task_data.dispatch_id = dispatch_id
            task_data.session_id = returned_session_id
            save_task_yaml(tasks_dir, task_id, task_data)

        return JSONResponse({**result, "collected": collected})

    @app.post("/api/tasks/{task_id}/complete")
    async def api_complete(task_id: str) -> JSONResponse:
        tasks_dir = repo_dir / "tasks"

        async with index_lock:
            index_entries = load_index(tasks_dir)
            entry = next((e for e in index_entries if e.id == task_id), None)
            if entry is None:
                return JSONResponse({"ok": False, "error": "not found"}, status_code=404)

            entry.status = "done"
            update_task_yaml(tasks_dir, entry)
            save_index(tasks_dir, index_entries)

        return JSONResponse({"ok": True})

    @app.post("/api/tasks/{task_id}/abort")
    async def api_abort(task_id: str) -> JSONResponse:
        tasks_dir = repo_dir / "tasks"

        async with index_lock:
            index_entries = load_index(tasks_dir)
            entry = next((e for e in index_entries if e.id == task_id), None)
            if entry is None:
                return JSONResponse({"ok": False, "error": "not found"}, status_code=404)

            entry.status = "aborted"
            update_task_yaml(tasks_dir, entry)
            save_index(tasks_dir, index_entries)

        return JSONResponse({"ok": True})

    @app.post("/api/tasks/{task_id}/open-session")
    async def api_task_open_session(task_id: str) -> JSONResponse:
        tasks_dir = repo_dir / "tasks"
        index_entries = load_index(tasks_dir)
        entry = next((e for e in index_entries if e.id == task_id), None)
        if entry is None:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)

        task_data = load_task_yaml(tasks_dir, task_id)
        if task_data is None:
            from lib.task_store import TaskData
            task_data = TaskData(id=task_id)
        if not task_data.datasource_id:
            task_data.datasource_id = entry.datasource_id
        if not task_data.project_id:
            task_data.project_id = entry.project_id
        if not task_data.remote_id:
            task_data.remote_id = entry.remote_id
        datasource = _load_datasource(task_data.datasource_id)

        from dashboard.interactive_prompt import build_task_session_prompts
        system_prompt, prompt = build_task_session_prompts(task_data, datasource, str(repo_dir))

        request_data = {
            "command": "open",
            "system_prompt": system_prompt,
            "prompt": prompt,
            "window_name": task_id,
            "activate": True,
        }
        project_id = entry.project_id
        if project_id:
            request_data["project_id"] = project_id

        try:
            result = await dispatcher_send(request_data)
        except (ConnectionRefusedError, FileNotFoundError):
            return JSONResponse({"ok": False, "error": "ディスパッチャーが起動していません"})
        return JSONResponse(result)

    # --- Sync ---

    @app.post("/api/sync")
    async def api_sync() -> JSONResponse:
        if sync_state["running"]:
            return JSONResponse({"ok": False, "error": "Sync already running"})
        sync_state["running"] = True
        sync_state["error"] = None
        asyncio.create_task(_run_sync())
        return JSONResponse({"ok": True, "status": "started"})

    @app.get("/api/sync/status")
    async def api_sync_status() -> JSONResponse:
        return JSONResponse({
            "running": sync_state["running"],
            "error": sync_state["error"],
        })

    async def _run_sync() -> None:
        try:
            fetch_script = repo_dir / "scripts" / "fetch-all.sh"
            sync_script = Path(__file__).resolve().parent.parent / "sync-tasks.py"

            fetch_proc = await asyncio.create_subprocess_exec(
                "bash", str(fetch_script),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            fetch_stdout, fetch_stderr = await fetch_proc.communicate()
            if fetch_proc.returncode != 0:
                sync_state["error"] = f"fetch failed: {fetch_stderr.decode(errors='replace')[:500]}"
                return

            sync_proc = await asyncio.create_subprocess_exec(
                "python3", str(sync_script), "--repo", str(repo_dir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, sync_stderr = await sync_proc.communicate(input=fetch_stdout)
            if sync_proc.returncode != 0:
                sync_state["error"] = sync_stderr.decode(errors="replace")[:500]
        except Exception as e:
            sync_state["error"] = str(e)
        finally:
            sync_state["running"] = False

    # --- Events ---

    @app.get("/api/events")
    async def api_events() -> StreamingResponse:
        async def event_stream():
            assert watcher is not None
            client_id, queue = watcher.add_client()
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=30.0)
                        yield f"event: {event}\ndata: {{}}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                watcher.remove_client(client_id)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # --- Routines ---

    @app.get("/api/routines")
    async def api_routines() -> JSONResponse:
        routines_path = repo_dir / "routines.json"
        if not routines_path.exists():
            return JSONResponse([])
        try:
            with open(routines_path, encoding="utf-8") as f:
                return JSONResponse(json.load(f))
        except (json.JSONDecodeError, OSError):
            return JSONResponse([])

    @app.post("/api/routines/{routine_id}/open-session")
    async def api_routine_open_session(routine_id: str) -> JSONResponse:
        routines_path = repo_dir / "routines.json"
        if not routines_path.exists():
            return JSONResponse({"ok": False, "error": "routines.json not found"}, status_code=404)
        try:
            with open(routines_path, encoding="utf-8") as f:
                routines = json.load(f)
        except (json.JSONDecodeError, OSError):
            return JSONResponse({"ok": False, "error": "routines.json parse error"}, status_code=500)

        routine = next((r for r in routines if r.get("routine_id") == routine_id), None)
        if routine is None:
            return JSONResponse({"ok": False, "error": f"Routine not found: {routine_id}"}, status_code=404)

        system_prompt = ""
        sp_file = routine.get("system_prompt_file", "")
        if sp_file:
            sp_path = repo_dir / sp_file
            if sp_path.exists():
                system_prompt = sp_path.read_text(encoding="utf-8")

        request_data = {
            "command": "open",
            "system_prompt": system_prompt,
            "prompt": routine.get("prompt", ""),
            "window_name": routine_id,
            "activate": True,
        }
        project_id = routine.get("project_id")
        if project_id:
            request_data["project_id"] = project_id
        sandbox_profile = routine.get("sandbox_profile")
        if sandbox_profile:
            request_data["sandbox_profile"] = sandbox_profile

        try:
            result = await dispatcher_send(request_data)
        except (ConnectionRefusedError, FileNotFoundError):
            return JSONResponse({"ok": False, "error": "ディスパッチャーが起動していません"})
        return JSONResponse(result)

    # --- Static & Index ---

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html_path = STATIC_DIR / "index.html"
        html = html_path.read_text(encoding="utf-8")
        if base_path:
            html = html.replace("/static/style.css", f"{base_path}/static/style.css")
            html = html.replace("/static/app.js", f"{base_path}/static/app.js")
        html = html.replace(
            "</head>",
            f'<script>window.__BASE_PATH__ = "{base_path}";</script>\n</head>',
        )
        return HTMLResponse(html)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app
