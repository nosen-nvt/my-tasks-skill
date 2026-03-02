#!/usr/bin/env python3
"""
sync-tasks.py - タスク同期スクリプト

fetch-all.sh が出力する JSONL を受け取り、tasks/index.jsonl + tasks/{id}.md を更新する。

使い方:
  fetch-all.sh | python3 sync-tasks.py --repo ~/.local/share/my-tasks
  python3 sync-tasks.py --repo ~/.local/share/my-tasks --input /tmp/tasks.jsonl
"""

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


JST = timezone(timedelta(hours=9))


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def today_str() -> str:
    return datetime.now(JST).strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# JSONL 入力
# ---------------------------------------------------------------------------

def load_jsonl(source) -> list[dict]:
    """JSONL を読み込んでリストで返す。空行・コメント行は無視する。"""
    tasks = []
    for line_num, line in enumerate(source, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            task = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"警告: {line_num}行目の JSON パースに失敗しました: {e}", file=sys.stderr)
            continue

        if "datasource_id" not in task or "remote_id" not in task or "title" not in task:
            print(
                f"警告: {line_num}行目に必須フィールド (datasource_id, remote_id, title) がありません",
                file=sys.stderr,
            )
            continue

        tasks.append(task)
    return tasks


# ---------------------------------------------------------------------------
# タスクインデックス (index.jsonl)
# ---------------------------------------------------------------------------

def load_index(tasks_dir: Path) -> list[dict]:
    """tasks/index.jsonl を読み込んでリストで返す。存在しない場合は空リストを返す。"""
    index_path = tasks_dir / "index.jsonl"
    if not index_path.exists():
        return []
    entries = []
    with open(index_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def save_index(tasks_dir: Path, entries: list[dict]) -> None:
    """tasks/index.jsonl に書き出す。"""
    tasks_dir.mkdir(parents=True, exist_ok=True)
    index_path = tasks_dir / "index.jsonl"
    with open(index_path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def generate_task_id(index_entries: list[dict]) -> str:
    """YYYYMMDD-NNN 形式のタスク ID を生成する。"""
    date_prefix = today_str()

    max_seq = 0
    for entry in index_entries:
        eid = entry.get("id", "")
        if eid.startswith(date_prefix + "-"):
            try:
                seq = int(eid[len(date_prefix) + 1:])
                if seq > max_seq:
                    max_seq = seq
            except ValueError:
                pass

    return f"{date_prefix}-{max_seq + 1:03d}"


# ---------------------------------------------------------------------------
# タスク Markdown
# ---------------------------------------------------------------------------

def create_task_markdown(tasks_dir: Path, entry: dict) -> None:
    """タスク実体の Markdown ファイルを生成する。"""
    task_id = entry["id"]
    md_path = tasks_dir / f"{task_id}.md"

    content = f"""# {entry['title']}

- ID: {entry['id']}
- Remote ID: {entry.get('remote_id', '')}
- Datasource: {entry['datasource_id']}
- Project: {entry.get('project_id', '')}
- Status: {entry['status']}

## 概要



## 未決事項



## 事前条件



## 達成条件



## 完了時アクション



## 実行プロンプト

"""
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(content)


def update_task_markdown_metadata(tasks_dir: Path, entry: dict) -> None:
    """タスク Markdown ファイルのメタデータ部分（タイトルとリスト）を更新する。"""
    task_id = entry["id"]
    md_path = tasks_dir / f"{task_id}.md"

    if not md_path.exists():
        create_task_markdown(tasks_dir, entry)
        return

    with open(md_path, encoding="utf-8") as f:
        content = f.read()

    lines = content.split("\n")
    new_lines = []
    for line in lines:
        if line.startswith("# ") and new_lines == []:
            new_lines.append(f"# {entry['title']}")
        elif line.startswith("- ID: "):
            new_lines.append(f"- ID: {entry['id']}")
        elif line.startswith("- Remote ID: "):
            new_lines.append(f"- Remote ID: {entry.get('remote_id', '')}")
        elif line.startswith("- Datasource: "):
            new_lines.append(f"- Datasource: {entry['datasource_id']}")
        elif line.startswith("- Project: "):
            new_lines.append(f"- Project: {entry.get('project_id', '')}")
        elif line.startswith("- Status: "):
            new_lines.append(f"- Status: {entry['status']}")
        else:
            new_lines.append(line)

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(new_lines))


def delete_task(tasks_dir: Path, task_id: str) -> None:
    """index エントリの削除は呼び出し側で行う。ここでは .md ファイルの削除のみ。"""
    md_path = tasks_dir / f"{task_id}.md"
    md_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# データソース読み込み
# ---------------------------------------------------------------------------

def load_datasource(datasources_dir: Path, datasource_id: str) -> dict | None:
    """datasources/{datasource_id}.json を読み込む。存在しない場合は None を返す。"""
    ds_path = datasources_dir / f"{datasource_id}.json"
    if ds_path.exists():
        with open(ds_path, encoding="utf-8") as f:
            return json.load(f)
    return None


def resolve_project(project_key: str | None, project_mapping: dict) -> str | None:
    """project_key から project_id を解決する。マッチしない場合は None を返す。"""
    if project_key is None:
        return None
    return project_mapping.get(project_key)


# ---------------------------------------------------------------------------
# データソース処理
# ---------------------------------------------------------------------------

def process_datasource(
    datasource_id: str,
    incoming_tasks: list[dict],
    tasks_dir: Path,
    datasources_dir: Path,
    index_entries: list[dict],
) -> dict:
    """
    1データソース分のタスクを処理する。

    index_entries はインプレースで変更される（新規追加・消失削除）。

    Returns:
        {
            "added": [...],
            "updated": [...],
            "vanished": [...],
            "auto_assigned": [...],
            "needs_review": [...],
        }
    """
    datasource = load_datasource(datasources_dir, datasource_id)
    project_mapping = datasource.get("project_mapping", {}) if datasource else {}

    # 既存インデックスから当該データソースのエントリを取得
    existing_entries = {
        e["remote_id"]: e for e in index_entries
        if e.get("datasource_id") == datasource_id and e.get("remote_id")
    }

    incoming_ids = {t["remote_id"] for t in incoming_tasks}

    added = []
    updated = []
    auto_assigned = []
    needs_review = []

    for t in incoming_tasks:
        remote_id = t["remote_id"]

        if remote_id in existing_entries:
            # 既存タスク: title 変更があれば更新
            entry = existing_entries[remote_id]
            if entry.get("title") != t["title"]:
                entry["title"] = t["title"]
                update_task_markdown_metadata(tasks_dir, entry)
                updated.append({
                    "datasource_id": datasource_id,
                    "remote_id": remote_id,
                    "title": t["title"],
                    "id": entry["id"],
                })
        else:
            # 新規タスク
            task_id = generate_task_id(index_entries)
            project_id = resolve_project(t.get("project_key"), project_mapping) or ""

            new_entry = {
                "id": task_id,
                "remote_id": remote_id,
                "datasource_id": datasource_id,
                "title": t["title"],
                "status": "pending",
                "project_id": project_id,
            }
            index_entries.append(new_entry)
            create_task_markdown(tasks_dir, new_entry)

            added.append({
                "datasource_id": datasource_id,
                "remote_id": remote_id,
                "title": t["title"],
                "id": task_id,
                "project_key": t.get("project_key"),
            })

            if project_id:
                auto_assigned.append({
                    "id": task_id,
                    "title": t["title"],
                    "project_id": project_id,
                    "project_key": t.get("project_key"),
                })
            else:
                needs_review.append({
                    "id": task_id,
                    "title": t["title"],
                    "project_key": t.get("project_key"),
                })

    PROTECTED_STATUSES = {"deferred"}

    # 消失タスク = 既存にあって incoming にないもの
    vanished = []
    vanished_ids = set()
    for remote_id, entry in existing_entries.items():
        if remote_id not in incoming_ids:
            if entry.get("status") in PROTECTED_STATUSES:
                continue  # deferred タスクはデータソースから消えても保持
            vanished.append({
                "datasource_id": datasource_id,
                "remote_id": remote_id,
                "title": entry.get("title", ""),
                "id": entry["id"],
            })
            vanished_ids.add(entry["id"])
            delete_task(tasks_dir, entry["id"])

    # index_entries から消失タスクを除去
    index_entries[:] = [e for e in index_entries if e.get("id") not in vanished_ids]

    return {
        "added": added,
        "updated": updated,
        "vanished": vanished,
        "auto_assigned": auto_assigned,
        "needs_review": needs_review,
    }


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="fetch-all.sh の JSONL 出力を受け取り、tasks/index.jsonl + tasks/*.md を更新する"
    )
    parser.add_argument(
        "--repo",
        required=True,
        metavar="PATH",
        help="タスク管理リポジトリのパス（例: ~/.local/share/my-tasks）",
    )
    parser.add_argument(
        "--input",
        metavar="FILE",
        default=None,
        help="JSONL ファイルのパス（省略時は stdin から読み込む）",
    )
    args = parser.parse_args()

    repo_dir = Path(args.repo).expanduser().resolve()
    tasks_dir = repo_dir / "tasks"
    datasources_dir = repo_dir / "datasources"

    if not repo_dir.exists():
        print(f"エラー: リポジトリが見つかりません: {repo_dir}", file=sys.stderr)
        sys.exit(1)

    # JSONL を読み込む
    if args.input:
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"エラー: 入力ファイルが見つかりません: {input_path}", file=sys.stderr)
            sys.exit(1)
        with open(input_path, encoding="utf-8") as f:
            all_incoming = load_jsonl(f)
    else:
        all_incoming = load_jsonl(sys.stdin)

    if not all_incoming:
        print("警告: 入力タスクが0件です。JSONL が空または全行エラーです。", file=sys.stderr)

    # datasource_id ごとにグループ化
    by_datasource: dict[str, list[dict]] = {}
    for task in all_incoming:
        ds_id = task["datasource_id"]
        by_datasource.setdefault(ds_id, []).append(task)

    # 既存 index を読み込み
    index_entries = load_index(tasks_dir)

    # index に存在するが今回の JSONL に含まれないデータソースも処理
    # （全タスクが完了した場合、JSONL に出現しなくなる）
    for entry in index_entries:
        ds_id = entry.get("datasource_id")
        if ds_id and ds_id not in by_datasource:
            by_datasource[ds_id] = []

    # 集計結果
    total_added = []
    total_updated = []
    total_vanished = []
    total_auto_assigned = []
    total_needs_review = []

    for datasource_id, incoming_tasks in by_datasource.items():
        result = process_datasource(
            datasource_id, incoming_tasks, tasks_dir, datasources_dir, index_entries
        )
        total_added.extend(result["added"])
        total_updated.extend(result["updated"])
        total_vanished.extend(result["vanished"])
        total_auto_assigned.extend(result["auto_assigned"])
        total_needs_review.extend(result["needs_review"])

    # index を保存
    save_index(tasks_dir, index_entries)

    # レポートを JSON で出力
    report = {
        "summary": {
            "added": len(total_added),
            "updated": len(total_updated),
            "vanished": len(total_vanished),
            "auto_assigned": len(total_auto_assigned),
            "needs_review": len(total_needs_review),
        },
        "added": total_added,
        "updated": total_updated,
        "vanished": total_vanished,
        "auto_assigned": total_auto_assigned,
        "needs_review": total_needs_review,
    }

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
