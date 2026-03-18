#!/usr/bin/env python3
"""
reopen-task.py - done/aborted タスクを手動で再オープンする

使い方:
  python3 reopen-task.py --repo ~/.local/share/my-tasks --id 20260301-001
"""

import argparse
import json
import sys
from pathlib import Path

from lib.task_store import load_index, save_index, reopen_task_yaml


def main() -> None:
    parser = argparse.ArgumentParser(
        description="done/aborted タスクを手動で再オープンする"
    )
    parser.add_argument(
        "--repo",
        required=True,
        metavar="PATH",
        help="タスク管理リポジトリのパス（例: ~/.local/share/my-tasks）",
    )
    parser.add_argument(
        "--id",
        required=True,
        metavar="TASK_ID",
        help="再オープンするタスクの ID",
    )
    args = parser.parse_args()

    repo_dir = Path(args.repo).expanduser().resolve()
    tasks_dir = repo_dir / "tasks"
    task_id = args.id

    if not repo_dir.exists():
        print(f"エラー: リポジトリが見つかりません: {repo_dir}", file=sys.stderr)
        sys.exit(1)

    # index を読み込み
    index_entries = load_index(tasks_dir)

    # 対象タスクを検索
    entry = None
    for e in index_entries:
        if e.get("id") == task_id:
            entry = e
            break

    if entry is None:
        print(f"エラー: タスクが見つかりません: {task_id}", file=sys.stderr)
        sys.exit(1)

    if entry.get("status") not in ("done", "aborted"):
        print(
            f"エラー: タスクは {entry.get('status')} 状態です。done または aborted のタスクのみ再オープンできます",
            file=sys.stderr,
        )
        sys.exit(1)

    # 再オープン
    old_generation = entry.get("generation", 1)
    entry["status"] = "pending"
    entry["generation"] = old_generation + 1

    reopen_task_yaml(tasks_dir, entry)

    # index を保存
    save_index(tasks_dir, index_entries)

    # レポート出力
    report = {
        "id": task_id,
        "remote_id": entry.get("remote_id", ""),
        "title": entry.get("title", ""),
        "old_generation": old_generation,
        "new_generation": entry["generation"],
        "status": entry["status"],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
