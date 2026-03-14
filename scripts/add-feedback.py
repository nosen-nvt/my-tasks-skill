#!/usr/bin/env python3
"""
add-feedback.py - タスクにユーザーフィードバックを手動で追加する

使い方:
  python3 add-feedback.py --repo ~/.local/share/my-tasks --id 20260301-001 --body "ログレベルも変更してください"
"""

import argparse
import importlib
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

# sync-tasks.py をモジュールとしてインポート
_script_dir = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("sync_tasks", _script_dir / "sync-tasks.py")
_sync_tasks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sync_tasks)

_dump_yaml = _sync_tasks._dump_yaml

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

    yaml_path = tasks_dir / f"{task_id}.yaml"
    if not yaml_path.exists():
        print(f"エラー: タスクが見つかりません: {task_id}", file=sys.stderr)
        sys.exit(1)

    with open(yaml_path, encoding="utf-8") as f:
        task_data = yaml.safe_load(f) or {}

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
    _dump_yaml(task_data, yaml_path)

    report = {
        "task_id": task_id,
        "feedback_item": new_item,
        "total_feedback_count": len(feedback),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
