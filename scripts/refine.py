#!/usr/bin/env python3
"""
refine.py - タスク精査ディスパッチャー

triaging / needs_clarification (全回答済み) のタスクを1タスク1ジョブとして
dispatcher.py に投入する。各ジョブは専用のコンテキストでタスクを精査し、
scoped 遷移時には実行プロンプトも生成する。

使い方:
  # 全 triaging タスクを精査ジョブとして投入
  python3 refine.py --repo ~/.local/share/my-tasks

  # 特定タスクのみ
  python3 refine.py --repo ~/.local/share/my-tasks --task 20260301-001

  # 全回答済み needs_clarification タスクも含める
  python3 refine.py --repo ~/.local/share/my-tasks --include-clarified

  # ドライラン（プロンプトを表示するだけで投入しない）
  python3 refine.py --repo ~/.local/share/my-tasks --dry-run
"""

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DISPATCHER = SCRIPT_DIR / "dispatcher.py"


# ---------------------------------------------------------------------------
# タスクデータ読み込み
# ---------------------------------------------------------------------------

def load_index(tasks_dir: Path) -> list[dict]:
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


def read_task_md(tasks_dir: Path, task_id: str) -> str | None:
    md_path = tasks_dir / f"{task_id}.md"
    if not md_path.exists():
        return None
    return md_path.read_text(encoding="utf-8")


def load_project(projects_dir: Path, project_id: str) -> dict | None:
    path = projects_dir / f"{project_id}.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def is_all_answered(task_md: str) -> bool:
    """未決事項セクションの全チェックボックスが [x] かどうか判定する。"""
    in_section = False
    has_items = False
    for line in task_md.split("\n"):
        if line.strip().startswith("## 未決事項"):
            in_section = True
            continue
        if in_section and line.strip().startswith("## "):
            break
        if in_section:
            if re.match(r"\s*-\s*\[\s*\]", line):
                return False  # 未回答あり
            if re.match(r"\s*-\s*\[x\]", line, re.IGNORECASE):
                has_items = True
    return has_items


# ---------------------------------------------------------------------------
# 対象タスクの抽出
# ---------------------------------------------------------------------------

def find_targets(
    index_entries: list[dict],
    tasks_dir: Path,
    *,
    task_id: str | None = None,
    include_clarified: bool = False,
) -> list[dict]:
    """精査対象のタスクを抽出する。"""
    targets = []
    for entry in index_entries:
        if task_id and entry["id"] != task_id:
            continue

        status = entry.get("status", "")

        if status == "triaging":
            targets.append(entry)
        elif status == "needs_clarification" and include_clarified:
            md = read_task_md(tasks_dir, entry["id"])
            if md and is_all_answered(md):
                targets.append(entry)

    return targets


# ---------------------------------------------------------------------------
# プロンプトテンプレート
# ---------------------------------------------------------------------------

TRIAGE_TEMPLATE = """\
あなたはタスク精査エージェントです。以下のタスクを精査し、ステータスを遷移させてください。

# 対象タスク

ファイル: `{tasks_dir}/{task_id}.md`

```markdown
{task_md}
```

# プロジェクト情報

- プロジェクトID: {project_id}
- プロジェクト名: {project_name}
- 説明: {project_description}
- 作業ディレクトリ: {working_directory}

# 指示

1. タスクの内容を分析してください。必要に応じて作業ディレクトリ配下のソースコードを調査してください。

2. 以下の観点で未決事項を洗い出してください:
   - タスクの目的・スコープが明確か
   - 実装方針に曖昧さがないか
   - 事前条件・依存関係が特定されているか
   - 達成条件がAIエージェント自身でローカル検証可能か（ファイル確認、テスト実行、ビルド成功など）

3. 判定:
   - **未決事項がある場合**: `## 未決事項` セクションにチェックボックス形式で質問を記載し、ステータスを `needs_clarification` に遷移
   - **未決事項がない場合**: `## 概要`、`## 事前条件`、`## 達成条件`、`## 完了時アクション` を記載し、ステータスを `scoped` に遷移。さらに `## 実行プロンプト` セクションに実行プロンプトを生成

4. 達成条件のルール（重要）:
   - このタスクは AI エージェントが実装・実行する前提です
   - 達成条件は AI エージェント自身がローカルで検証可能な内容にしてください
   - OK: ファイル内容の確認、YAML/JSON のパース検証、テスト実行結果、ビルド成功
   - NG: ブラウザでの手動確認、外部サービスの目視確認など

5. 実行プロンプト生成時の注意（scoped の場合）:
   - 実行プロンプトは、別の AI エージェントがこのタスクを実行するための指示書です
   - 背景・目的、具体的な作業手順、達成条件を含めてください
   - 実行エージェントはタスク md の他のセクションを読まない前提で、自己完結した内容にしてください

# ファイル更新

以下の2つのファイルを更新してください:

1. `{tasks_dir}/{task_id}.md`: 上記の精査結果を反映
2. `{tasks_dir}/index.jsonl`: 該当タスク（id="{task_id}"）の status フィールドを更新

index.jsonl は JSONL 形式（1行1タスク）です。該当行のみ status を変更し、他の行は変更しないでください。
"""

RECLARIFY_TEMPLATE = """\
あなたはタスク精査エージェントです。以下のタスクは質問への回答が完了しています。回答を踏まえて精査を完了させてください。

# 対象タスク

ファイル: `{tasks_dir}/{task_id}.md`

```markdown
{task_md}
```

# プロジェクト情報

- プロジェクトID: {project_id}
- プロジェクト名: {project_name}
- 説明: {project_description}
- 作業ディレクトリ: {working_directory}

# 指示

1. `## 未決事項` セクションの回答内容を確認してください。必要に応じて作業ディレクトリ配下のソースコードを調査してください。

2. 回答を踏まえて以下を実施:
   - `## 概要`、`## 事前条件`、`## 達成条件`、`## 完了時アクション` を記載（既存内容があれば更新）
   - ステータスを `scoped` に遷移
   - `## 実行プロンプト` セクションに実行プロンプトを生成

3. 達成条件のルール（重要）:
   - このタスクは AI エージェントが実装・実行する前提です
   - 達成条件は AI エージェント自身がローカルで検証可能な内容にしてください
   - OK: ファイル内容の確認、YAML/JSON のパース検証、テスト実行結果、ビルド成功
   - NG: ブラウザでの手動確認、外部サービスの目視確認など

4. 実行プロンプト生成の注意:
   - 実行プロンプトは、別の AI エージェントがこのタスクを実行するための指示書です
   - 背景・目的、具体的な作業手順、達成条件を含めてください
   - 実行エージェントはタスク md の他のセクションを読まない前提で、自己完結した内容にしてください

# ファイル更新

以下の2つのファイルを更新してください:

1. `{tasks_dir}/{task_id}.md`: 上記の精査結果を反映
2. `{tasks_dir}/index.jsonl`: 該当タスク（id="{task_id}"）の status フィールドを `scoped` に更新

index.jsonl は JSONL 形式（1行1タスク）です。該当行のみ status を変更し、他の行は変更しないでください。
"""


def build_prompt(
    entry: dict,
    task_md: str,
    project: dict,
    tasks_dir: Path,
) -> str:
    status = entry.get("status", "")
    template = RECLARIFY_TEMPLATE if status == "needs_clarification" else TRIAGE_TEMPLATE

    return template.format(
        tasks_dir=tasks_dir,
        task_id=entry["id"],
        task_md=task_md,
        project_id=entry.get("project_id", ""),
        project_name=project.get("name", ""),
        project_description=project.get("description", ""),
        working_directory=project.get("working_directory", ""),
    )


# ---------------------------------------------------------------------------
# ディスパッチ
# ---------------------------------------------------------------------------

async def dispatch_one(
    entry: dict,
    prompt: str,
    *,
    sandbox_profile: str | None = None,
    repo: str,
) -> dict:
    """dispatcher.py run --project にプロンプトを渡してジョブを投入する。"""
    cmd = [
        sys.executable, str(DISPATCHER),
        "run",
        "--project", entry["project_id"],
        "--repo", repo,
    ]
    if sandbox_profile:
        cmd += ["--sandbox-profile", sandbox_profile]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate(input=prompt.encode("utf-8"))
    stderr_text = stderr.decode("utf-8").strip()

    if proc.returncode == 0:
        # stderr にディスパッチ結果が出る（例: "Job started: bo-1"）
        return {"ok": True, "task_id": entry["id"], "message": stderr_text}
    else:
        return {"ok": False, "task_id": entry["id"], "error": stderr_text}


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

async def async_main(args: argparse.Namespace) -> None:
    repo_dir = Path(args.repo).expanduser().resolve()
    tasks_dir = repo_dir / "tasks"
    projects_dir = repo_dir / "projects"

    if not repo_dir.exists():
        print(f"エラー: リポジトリが見つかりません: {repo_dir}", file=sys.stderr)
        sys.exit(1)

    index_entries = load_index(tasks_dir)
    targets = find_targets(
        index_entries,
        tasks_dir,
        task_id=args.task,
        include_clarified=args.include_clarified,
    )

    if not targets:
        print("対象タスクがありません", file=sys.stderr)
        return

    dispatched = []
    skipped = []

    for entry in targets:
        task_id = entry["id"]
        project_id = entry.get("project_id", "")

        # プロジェクト未割り当て
        if not project_id:
            skipped.append({"task_id": task_id, "reason": "project_id が未設定"})
            continue

        project = load_project(projects_dir, project_id)
        if not project:
            skipped.append({"task_id": task_id, "reason": f"プロジェクト {project_id} が見つかりません"})
            continue

        # manual プロジェクト（working_directory なし）はスキップ
        if not project.get("working_directory"):
            skipped.append({"task_id": task_id, "reason": f"manual プロジェクト ({project_id})"})
            continue

        task_md = read_task_md(tasks_dir, task_id)
        if not task_md:
            skipped.append({"task_id": task_id, "reason": "タスク md が見つかりません"})
            continue

        prompt = build_prompt(entry, task_md, project, tasks_dir)

        if args.dry_run:
            print(f"--- {task_id} ({entry.get('status')}) ---", file=sys.stderr)
            print(prompt)
            print()
            dispatched.append({"task_id": task_id, "dry_run": True})
            continue

        result = await dispatch_one(
            entry, prompt,
            sandbox_profile=args.sandbox_profile,
            repo=args.repo,
        )
        dispatched.append(result)

        if result["ok"]:
            print(f"投入: {task_id} - {result.get('message', '')}", file=sys.stderr)
        else:
            print(f"失敗: {task_id} - {result.get('error', '')}", file=sys.stderr)

    # レポート出力
    report = {
        "dispatched": len([d for d in dispatched if d.get("ok", d.get("dry_run"))]),
        "failed": len([d for d in dispatched if not d.get("ok") and not d.get("dry_run")]),
        "skipped": len(skipped),
        "details": dispatched,
        "skipped_details": skipped,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="タスク精査ジョブをディスパッチャーに投入する"
    )
    parser.add_argument(
        "--repo",
        default="~/.local/share/my-tasks",
        metavar="PATH",
        help="タスク管理リポジトリのパス（デフォルト: ~/.local/share/my-tasks）",
    )
    parser.add_argument(
        "--task",
        metavar="ID",
        default=None,
        help="特定タスク ID のみ処理",
    )
    parser.add_argument(
        "--include-clarified",
        action="store_true",
        help="全回答済みの needs_clarification タスクも対象にする",
    )
    parser.add_argument(
        "--sandbox-profile",
        metavar="PROFILE",
        default=None,
        help="サンドボックスプロファイルを上書き指定",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="投入せずにプロンプトを表示するだけ",
    )
    args = parser.parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
