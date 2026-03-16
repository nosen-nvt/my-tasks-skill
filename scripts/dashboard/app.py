"""FastAPI アプリケーション。"""

import asyncio
import importlib.util
import json
import os
from pathlib import Path

import yaml
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .watcher import FileWatcher

STATIC_DIR = Path(__file__).parent / "static"

# sync-tasks.py をモジュールとしてインポート（ハイフン付きファイル名対応）
_script_dir = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("sync_tasks", _script_dir / "sync-tasks.py")
_sync_tasks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sync_tasks)

load_index = _sync_tasks.load_index
save_index = _sync_tasks.save_index
reopen_task_yaml = _sync_tasks.reopen_task_yaml


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

    # --- API ---

    @app.get("/api/projects")
    async def api_projects() -> list[dict]:
        projects = read_json_dir(repo_dir / "projects")
        tasks = read_jsonl(repo_dir / "tasks" / "index.jsonl")
        # プロジェクトごとのタスク数集計
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
        # 後方互換: .md ファイルが残っている場合
        md_path = repo_dir / "tasks" / f"{task_id}.md"
        if md_path.exists():
            content = md_path.read_text(encoding="utf-8")
            return JSONResponse({"id": task_id, "content": content})
        return JSONResponse({"error": "not found"}, status_code=404)

    @app.get("/api/lifecycles")
    async def api_lifecycles() -> list[dict]:
        return read_jsonl(dispatch_dir / "lifecycles.jsonl")

    @app.get("/api/lifecycles/{lifecycle_id}/context")
    async def api_lifecycle_context(lifecycle_id: str) -> JSONResponse:
        lifecycles = read_jsonl(dispatch_dir / "lifecycles.jsonl")
        entry = next((lc for lc in lifecycles if lc.get("lifecycle_id") == lifecycle_id), None)
        if entry is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        context_path_str = entry.get("context_path")
        if not context_path_str:
            return JSONResponse({"error": "not found"}, status_code=404)
        context_path = Path(context_path_str)
        if not context_path.exists() or context_path.stat().st_size == 0:
            return JSONResponse({"error": "not found"}, status_code=404)
        content = context_path.read_text(encoding="utf-8")
        # YAML ファイルなら構造化データとして返却
        if context_path.suffix == ".yaml":
            try:
                data = yaml.safe_load(content)
                return JSONResponse({"lifecycle_id": lifecycle_id, "context": data})
            except Exception:
                pass
        # fallback: テキストとして返却
        return JSONResponse({"lifecycle_id": lifecycle_id, "content": content})

    @app.get("/api/jobs")
    async def api_jobs(lifecycle_id: str | None = Query(default=None)) -> list[dict]:
        jobs = read_jsonl(dispatch_dir / "jobs.jsonl")
        if lifecycle_id:
            jobs = [j for j in jobs if j.get("lifecycle_id") == lifecycle_id]
        return jobs

    @app.get("/api/jobs/{dispatch_id}/log")
    async def api_job_log(dispatch_id: str, tail: int = Query(default=200)) -> JSONResponse:
        log_path = dispatch_dir / f"{dispatch_id}.log"
        if not log_path.exists():
            return JSONResponse({"error": "not found"}, status_code=404)
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        if tail and len(lines) > tail:
            lines = lines[-tail:]
        return JSONResponse({"dispatch_id": dispatch_id, "lines": lines})

    @app.get("/api/jobs/{dispatch_id}/result")
    async def api_job_result(dispatch_id: str) -> JSONResponse:
        result_path = dispatch_dir / f"{dispatch_id}.result.json"
        if not result_path.exists():
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            with open(result_path, encoding="utf-8") as f:
                data = json.load(f)
            return JSONResponse(data)
        except (json.JSONDecodeError, OSError):
            return JSONResponse({"error": "parse error"}, status_code=500)

    # --- Action endpoints ---

    def _load_task_data(tasks_dir: Path, task_id: str) -> dict:
        """YAML タスクファイルを読み込む。なければ空 dict を返す。"""
        yaml_path = tasks_dir / f"{task_id}.yaml"
        if yaml_path.exists():
            return yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        return {}

    @app.post("/api/tasks/{task_id}/dispatch")
    async def api_dispatch(task_id: str) -> JSONResponse:
        tasks_dir = repo_dir / "tasks"

        async with index_lock:
            index_entries = load_index(tasks_dir)
        entry = next((e for e in index_entries if e.get("id") == task_id), None)
        if entry is None:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        if entry.get("status") != "pending":
            return JSONResponse({"ok": False, "error": f"タスクは {entry.get('status')} 状態です（pending のみ実行可能）"})

        project_id = entry.get("project_id", "")
        generation = entry.get("generation", 1)

        task_data = _load_task_data(tasks_dir, task_id)
        lifecycle_id = f"{task_id}-g{generation}"

        try:
            result = await dispatcher_send({
                "command": "dispatch",
                "project_id": project_id,
                "prompt": task_data.get("description") or entry.get("title", ""),
                "context": task_data or None,
                "lifecycle_id": lifecycle_id,
            })
        except (ConnectionRefusedError, FileNotFoundError):
            return JSONResponse({"ok": False, "error": "ディスパッチャーが起動していません"})

        if result.get("ok"):
            async with index_lock:
                index_entries = load_index(tasks_dir)
                for e in index_entries:
                    if e.get("id") == task_id:
                        e["status"] = "in_progress"
                        break
                save_index(tasks_dir, index_entries)

        return JSONResponse(result)

    @app.post("/api/tasks/{task_id}/redispatch")
    async def api_redispatch(task_id: str) -> JSONResponse:
        tasks_dir = repo_dir / "tasks"

        async with index_lock:
            index_entries = load_index(tasks_dir)
            entry = next((e for e in index_entries if e.get("id") == task_id), None)
            if entry is None:
                return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
            if entry.get("status") not in ("done", "aborted"):
                return JSONResponse({"ok": False, "error": f"タスクは {entry.get('status')} 状態です（done/aborted のみ再実行可能）"})

            old_generation = entry.get("generation", 1)
            entry["status"] = "pending"
            entry["generation"] = old_generation + 1
            reopen_task_yaml(tasks_dir, entry)
            save_index(tasks_dir, index_entries)

        project_id = entry.get("project_id", "")
        generation = entry.get("generation", 1)

        task_data = _load_task_data(tasks_dir, task_id)
        lifecycle_id = f"{task_id}-g{generation}"

        try:
            result = await dispatcher_send({
                "command": "dispatch",
                "project_id": project_id,
                "prompt": task_data.get("description") or entry.get("title", ""),
                "context": task_data or None,
                "lifecycle_id": lifecycle_id,
            })
        except (ConnectionRefusedError, FileNotFoundError):
            return JSONResponse({"ok": False, "error": "ディスパッチャーが起動していません"})

        if result.get("ok"):
            async with index_lock:
                index_entries = load_index(tasks_dir)
                for e in index_entries:
                    if e.get("id") == task_id:
                        e["status"] = "in_progress"
                        break
                save_index(tasks_dir, index_entries)

        return JSONResponse(result)

    @app.post("/api/lifecycles/{lifecycle_id}/open-session")
    async def api_open_session(lifecycle_id: str) -> JSONResponse:
        from dispatcher.interactive_prompt import build_lifecycle_session_prompts

        lifecycles = read_jsonl(dispatch_dir / "lifecycles.jsonl")
        entry = next((lc for lc in lifecycles if lc.get("lifecycle_id") == lifecycle_id), None)
        if entry is None:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        if entry.get("status") != "suspend":
            return JSONResponse({"ok": False, "error": f"ライフサイクルは {entry.get('status')} 状態です（suspend のみ対話可能）"})

        project_id = entry.get("project_id", "")
        context_path_str = entry.get("context_path", "")
        context_path = Path(context_path_str) if context_path_str else None

        # コンテキスト YAML を読み込み
        ctx = {}
        if context_path and context_path.exists():
            try:
                ctx = yaml.safe_load(context_path.read_text(encoding="utf-8")) or {}
            except Exception:
                pass

        system_prompt, prompt = build_lifecycle_session_prompts(entry, ctx, str(dispatch_dir))

        try:
            result = await dispatcher_send({
                "command": "open",
                "project_id": project_id,
                "system_prompt": system_prompt,
                "prompt": prompt,
                "activate": True,
            })
        except (ConnectionRefusedError, FileNotFoundError):
            return JSONResponse({"ok": False, "error": "ディスパッチャーが起動していません"})
        return JSONResponse(result)

    @app.post("/api/tasks/{task_id}/open-session")
    async def api_task_open_session(task_id: str) -> JSONResponse:
        tasks_dir = repo_dir / "tasks"
        index_entries = load_index(tasks_dir)
        entry = next((e for e in index_entries if e.get("id") == task_id), None)
        if entry is None:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        if entry.get("status") not in ("pending", "in_progress"):
            return JSONResponse({"ok": False, "error": f"タスクは {entry.get('status')} 状態です（pending/in_progress のみ対話可能）"})

        try:
            result = await dispatcher_send({
                "command": "open",
                "task_id": task_id,
                "activate": True,
            })
        except (ConnectionRefusedError, FileNotFoundError):
            return JSONResponse({"ok": False, "error": "ディスパッチャーが起動していません"})
        return JSONResponse(result)

    @app.post("/api/lifecycles/{lifecycle_id}/approve")
    async def api_approve(lifecycle_id: str) -> JSONResponse:
        try:
            result = await dispatcher_send({
                "command": "resume",
                "lifecycle_id": lifecycle_id,
            })
        except (ConnectionRefusedError, FileNotFoundError):
            return JSONResponse({"ok": False, "error": "ディスパッチャーが起動していません"})
        return JSONResponse(result)

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
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c",
                f'"{fetch_script}" | python3 "{sync_script}" --repo "{repo_dir}"',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                sync_state["error"] = stderr.decode(errors="replace")[:500]
        except Exception as e:
            sync_state["error"] = str(e)
        finally:
            sync_state["running"] = False

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
