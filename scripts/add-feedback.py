#!/usr/bin/env python3
"""
add-feedback.py - タスクにユーザーフィードバックを手動で追加する

使い方:
  python3 add-feedback.py --repo ~/.local/share/my-tasks --id 20260301-001 --body "ログレベルも変更してください"
"""

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from lib.task_store import load_task_yaml, save_task_yaml

JST = timezone(timedelta(hours=9))


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="タスクにユーザーフィードバックを手動で追加する"
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
        help="対象タスクの ID",
    )
    parser.add_argument(
        "--body",
        required=True,
        help="フィードバック内容",
    )
    args = parser.parse_args()

    repo_dir = Path(args.repo).expanduser().resolve()
    tasks_dir = repo_dir / "tasks"
    task_id = args.id

    if not repo_dir.exists():
        print(f"エラー: リポジトリが見つかりません: {repo_dir}", file=sys.stderr)
        sys.exit(1)

    task_data = load_task_yaml(tasks_dir, task_id)
    if task_data is None:
        print(f"エラー: タスクが見つかりません: {task_id}", file=sys.stderr)
        sys.exit(1)

    generation = task_data.get("generation", 1)

    feedback = task_data.get("feedback", [])
    if not isinstance(feedback, list):
        feedback = []

    new_item = {
        "source": "user",
        "timestamp": now_iso(),
        "body": args.body,
        "generation": generation,
    }
    feedback.append(new_item)

    task_data["feedback"] = feedback
    save_task_yaml(tasks_dir, task_id, task_data)

    report = {
        "task_id": task_id,
        "feedback_item": new_item,
        "total_feedback_count": len(feedback),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
