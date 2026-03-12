"""FastAPI アプリケーション。"""

import asyncio
import json
import os
from pathlib import Path

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
        md_path = repo_dir / "tasks" / f"{task_id}.md"
        if not md_path.exists():
            return JSONResponse({"error": "not found"}, status_code=404)
        content = md_path.read_text(encoding="utf-8")
        return JSONResponse({"id": task_id, "content": content})

    @app.get("/api/lifecycles")
    async def api_lifecycles() -> list[dict]:
        return read_jsonl(dispatch_dir / "lifecycles.jsonl")

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
