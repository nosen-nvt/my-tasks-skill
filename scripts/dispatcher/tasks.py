"""タスクファイル I/O ヘルパー。"""

import json
from pathlib import Path


def read_file(path: Path) -> str | None:
    """ファイルを読み込む。存在しない場合は None を返す。"""
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def load_task_entry(tasks_dir: Path, task_id: str) -> dict | None:
    """index.jsonl からタスクエントリを読み込む。"""
    index_path = tasks_dir / "index.jsonl"
    if not index_path.exists():
        return None
    with open(index_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("id") == task_id:
                    return entry
            except json.JSONDecodeError:
                continue
    return None


def update_task_index(tasks_dir: Path, task_id: str, updates: dict) -> bool:
    """index.jsonl の指定タスクのフィールドを更新する。"""
    index_path = tasks_dir / "index.jsonl"
    if not index_path.exists():
        return False

    lines = []
    found = False
    with open(index_path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
                if entry.get("id") == task_id:
                    entry.update(updates)
                    found = True
                lines.append(json.dumps(entry, ensure_ascii=False) + "\n")
            except json.JSONDecodeError:
                lines.append(line)

    if found:
        with open(index_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    return found


def update_task_md_status(tasks_dir: Path, task_id: str, new_status: str) -> bool:
    """タスク Markdown ファイルの Status 行を更新する。"""
    md_path = tasks_dir / f"{task_id}.md"
    if not md_path.exists():
        return False

    content = md_path.read_text(encoding="utf-8")
    lines = content.split("\n")
    updated = False
    for i, line in enumerate(lines):
        if line.startswith("- Status: "):
            lines[i] = f"- Status: {new_status}"
            updated = True
            break

    if updated:
        md_path.write_text("\n".join(lines), encoding="utf-8")
    return updated


def read_execution_prompt(repo_dir: Path, task_id: str) -> str | None:
    """tasks/{task_id}.md から実行プロンプトセクションを読み取る。"""
    md_path = repo_dir / "tasks" / f"{task_id}.md"
    if not md_path.exists():
        return None

    with open(md_path, encoding="utf-8") as f:
        content = f.read()

    marker = "## 実行プロンプト"
    idx = content.find(marker)
    if idx == -1:
        return None

    prompt_section = content[idx + len(marker):].strip()

    lines = prompt_section.split("\n")
    result_lines = []
    for line in lines:
        if line.startswith("## "):
            break
        result_lines.append(line)

    return "\n".join(result_lines).strip() or None
