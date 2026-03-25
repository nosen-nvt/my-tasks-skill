#!/usr/bin/env python3
"""
migrate-status-v3.py - in_progress → in_review マイグレーション

v2 の in_progress タスクを v3 の in_review に変換する。
サービス再起動前に実行すること。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.task_store import load_index, save_index, load_task_yaml, save_task_yaml


def main():
    parser = argparse.ArgumentParser(description="in_progress → in_review マイグレーション")
    parser.add_argument("--repo", required=True, help="タスク管理リポジトリのパス")
    parser.add_argument("--dry-run", action="store_true", help="変更せず影響を表示のみ")
    args = parser.parse_args()

    repo_dir = Path(args.repo).expanduser().resolve()
    tasks_dir = repo_dir / "tasks"

    if not tasks_dir.exists():
        print(f"エラー: {tasks_dir} が見つかりません", file=sys.stderr)
        sys.exit(1)

    index_entries = load_index(tasks_dir)
    migrated = []

    for entry in index_entries:
        if entry.status == "in_progress":
            migrated.append(entry.id)
            if not args.dry_run:
                entry.status = "in_review"
                task_data = load_task_yaml(tasks_dir, entry.id)
                if task_data:
                    task_data.status = "in_review"
                    save_task_yaml(tasks_dir, entry.id, task_data)

    if not args.dry_run:
        save_index(tasks_dir, index_entries)

    if migrated:
        action = "変換予定" if args.dry_run else "変換済み"
        print(f"{len(migrated)} 件の in_progress タスクを in_review に{action}:")
        for tid in migrated:
            print(f"  - {tid}")
    else:
        print("マイグレーション対象のタスクはありません。")


if __name__ == "__main__":
    main()
