"""LLM によるプロジェクト自動判定。"""

import asyncio
import json
from pathlib import Path

from .models import log


async def classify_project(repo_dir: Path, prompt: str) -> str | None:
    """プロンプト内容から最適なプロジェクトを LLM で判定する。"""
    projects_dir = repo_dir / "projects"
    if not projects_dir.exists():
        return None

    projects = []
    for p in projects_dir.glob("*.json"):
        try:
            with open(p) as f:
                proj = json.load(f)
            desc = proj.get("description", "")
            wd = proj.get("working_directory", "")
            if not wd:
                continue
            projects.append(f"- {proj.get('project_id', p.stem)}: {proj.get('name', '')} - {desc}")
        except (json.JSONDecodeError, OSError):
            continue

    if not projects:
        return None

    classification_prompt = (
        "以下のタスク/依頼内容を読んで、最も適切なプロジェクトを選択してください。\n\n"
        f"# タスク内容\n{prompt}\n\n"
        f"# プロジェクト一覧\n" + "\n".join(projects) + "\n\n"
        "# 出力\nJSON で回答してください:\n"
        '{\"project_id\": \"選択したプロジェクトID\", \"confidence\": \"high\"|\"low\"}\n\n'
        "どのプロジェクトにも該当しない場合:\n"
        '{\"project_id\": null, \"reason\": \"理由\"}'
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", classification_prompt, "--output-format", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None

        result = json.loads(stdout.decode())
        if isinstance(result, list):
            for item in result:
                if item.get("type") == "result":
                    inner = json.loads(item["result"])
                    project_id = inner.get("project_id")
                    if project_id and inner.get("confidence") != "low":
                        return project_id
                    elif project_id and inner.get("confidence") == "low":
                        return project_id
                    return None
        elif isinstance(result, dict):
            project_id = result.get("project_id")
            return project_id if project_id else None
    except Exception as e:
        log.error(f"Project classification error: {e}")

    return None
