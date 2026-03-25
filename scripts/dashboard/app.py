"""FastAPI アプリケーション (v3)。"""

import asyncio
import json
import os
from pathlib import Path

import yaml
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .watcher import FileWatcher

STATIC_DIR = Path(__file__).parent / "static"


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

    async def dispatcher_send(request: dict) -> dict:
        sock_path = dispatch_dir / "dispatcher.sock"
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        writer.write(json.dumps(request, ensure_ascii=False).encode() + b"\n")
        await writer.drain()
        data = await reader.readline()
        writer.close()
        await writer.wait_closed()
        return json.loads(data)

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

    sync_state = {"running": False, "error": None}

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

    ACTION_MAP = {
        "plan": "plan",
        "dispatch": "dispatch",
        "resume": "request_resume",
        "feedback": "request_feedback",
        "complete": "done",
        "abort": "abort",
    }

    @app.post("/api/tasks/{task_id}/action/{action_type}")
    async def api_action(task_id: str, action_type: str) -> JSONResponse:
        mapped = ACTION_MAP.get(action_type, action_type)
        try:
            result = await dispatcher_send({
                "command": "dispatch_action",
                "task_id": task_id,
                "action": {"type": mapped},
            })
        except (ConnectionRefusedError, FileNotFoundError):
            return JSONResponse({"ok": False, "error": "オーケストレーターが起動していません"})
        error = result.get("error", "")
        status_code = 404 if "not found" in error else 200
        return JSONResponse(result, status_code=status_code)

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
            return JSONResponse({"ok": False, "error": "オーケストレーターが起動していません"})
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
