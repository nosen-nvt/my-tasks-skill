#!/usr/bin/env python3
"""
migrate-generations.py - 既存タスク YAML に generations[] フィールドを追加する

history[] がある場合は generations[] に変換する。
既に generations[] がある場合はスキップする。

使い方:
  python3 migrate-generations.py --repo ~/.local/share/my-tasks
  python3 migrate-generations.py --repo ~/.local/share/my-tasks --dry-run
"""

import argparse
import json
import sys
from pathlib import Path

from lib.task_store import (
    load_task_yaml, save_task_yaml, load_index,
    migrate_history_to_generations,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="既存タスク YAML に generations[] フィールドを追加する"
    )
    parser.add_argument(
        "--repo",
        required=True,
        metavar="PATH",
        help="タスク管理リポジトリのパス（例: ~/.local/share/my-tasks）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="実際にファイルを変更せず、変更内容のみ表示する",
    )
    args = parser.parse_args()

    repo_dir = Path(args.repo).expanduser().resolve()
    tasks_dir = repo_dir / "tasks"

    if not tasks_dir.exists():
        print(f"エラー: タスクディレクトリが見つかりません: {tasks_dir}", file=sys.stderr)
        sys.exit(1)

    index_entries = load_index(tasks_dir)
    task_ids = [e.id for e in index_entries]

    migrated = 0
    skipped = 0
    added_empty = 0

    for task_id in task_ids:
        data = load_task_yaml(tasks_dir, task_id)
        if data is None:
            continue

        if data.generations:
            skipped += 1
            continue

        if data.history:
            # history[] → generations[] 変換
            data = migrate_history_to_generations(data)
            migrated += 1
            action = f"migrated ({len(data.generations)} entries from history)"
        else:
            # generations[] フィールドは既に空で存在する（TaskData のデフォルト）
            added_empty += 1
            action = "added empty generations[]"

        if args.dry_run:
            print(f"  {task_id}: {action}")
        else:
            save_task_yaml(tasks_dir, task_id, data)
            print(f"  {task_id}: {action}")

    # レポート
    prefix = "[DRY RUN] " if args.dry_run else ""
    report = {
        "migrated": migrated,
        "added_empty": added_empty,
        "skipped": skipped,
        "total": migrated + added_empty + skipped,
    }
    print(f"\n{prefix}{json.dumps(report, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
