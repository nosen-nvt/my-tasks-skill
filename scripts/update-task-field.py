#!/usr/bin/env python3
"""タスク YAML のフィールドを安全に更新するヘルパースクリプト。

Plan セッションから呼び出し、yaml.dump 経由で書き込むことで
YAML 特殊文字の不正クォートを防止する。

Usage:
  # JSON パッチを stdin から適用
  python3 scripts/update-task-field.py TASK_ID <<'EOF'
  {
    "description": "タスクの説明",
    "preconditions": ["条件1", "条件2"],
    "acceptance_criteria": ["基準1: 詳細", "\"コマンド\" が成功すること"],
    "execute_prompt": "## 背景\n\n実行手順..."
  }
  EOF

  # 単一フィールドを引数で更新
  python3 scripts/update-task-field.py TASK_ID --field acceptance_criteria --json-value '["基準1", "基準2"]'
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.task_store import load_task_yaml, save_task_yaml

UPDATABLE_PLAN_FIELDS = {
    "description",
    "preconditions",
    "acceptance_criteria",
    "completion_actions",
    "execute_prompt",
}

REPO_DIR = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description="タスク YAML フィールド更新")
    parser.add_argument("task_id", help="タスク ID")
    parser.add_argument("--repo", default=str(REPO_DIR), help="リポジトリルート")
    parser.add_argument("--field", help="更新するフィールド名（単一フィールドモード）")
    parser.add_argument("--json-value", help="フィールド値 (JSON)")
    args = parser.parse_args()

    tasks_dir = Path(args.repo) / "tasks"
    task = load_task_yaml(tasks_dir, args.task_id)
    if task is None:
        print(f"error: task {args.task_id} not found", file=sys.stderr)
        sys.exit(1)

    if args.field:
        # 単一フィールドモード
        if args.field not in UPDATABLE_PLAN_FIELDS:
            print(f"error: field '{args.field}' is not updatable (allowed: {', '.join(sorted(UPDATABLE_PLAN_FIELDS))})", file=sys.stderr)
            sys.exit(1)
        if args.json_value:
            value = json.loads(args.json_value)
        else:
            value = json.loads(sys.stdin.read())
        setattr(task, args.field, value)
    else:
        # JSON パッチモード (stdin)
        patch = json.loads(sys.stdin.read())
        if not isinstance(patch, dict):
            print("error: stdin must be a JSON object", file=sys.stderr)
            sys.exit(1)
        for key, value in patch.items():
            if key not in UPDATABLE_PLAN_FIELDS:
                print(f"error: field '{key}' is not updatable (allowed: {', '.join(sorted(UPDATABLE_PLAN_FIELDS))})", file=sys.stderr)
                sys.exit(1)
            setattr(task, key, value)

    save_task_yaml(tasks_dir, args.task_id, task)

    # 保存後に roundtrip 検証
    reloaded = load_task_yaml(tasks_dir, args.task_id)
    if reloaded is None:
        print("error: roundtrip validation failed — saved YAML cannot be re-parsed", file=sys.stderr)
        sys.exit(1)

    print(f"ok: updated {args.task_id}")


if __name__ == "__main__":
    main()
