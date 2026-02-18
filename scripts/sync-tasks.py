#!/usr/bin/env python3
"""
sync-tasks.py - タスク最新化スクリプト

fetch-all.sh が出力する JSONL を受け取り、tasks/*.json（タスクストア）を更新する。

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


def load_task_store(tasks_dir: Path, datasource_id: str) -> dict:
    """tasks/{datasource_id}.json を読み込む。存在しない場合は空のストアを返す。"""
    store_path = tasks_dir / f"{datasource_id}.json"
    if store_path.exists():
        with open(store_path, encoding="utf-8") as f:
            return json.load(f)
    return {
        "datasource_id": datasource_id,
        "updated_at": now_iso(),
        "tasks": [],
    }


def save_task_store(tasks_dir: Path, store: dict) -> None:
    """tasks/{datasource_id}.json に書き込む。"""
    tasks_dir.mkdir(parents=True, exist_ok=True)
    store_path = tasks_dir / f"{store['datasource_id']}.json"
    with open(store_path, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)
        f.write("\n")


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


def process_datasource(
    datasource_id: str,
    incoming_tasks: list[dict],
    tasks_dir: Path,
    datasources_dir: Path,
) -> dict:
    """
    1データソース分のタスクを処理する。

    Returns:
        {
            "added": [...],      # 新規タスク
            "vanished": [...],   # 消失タスク（ストアから削除）
            "vanished_refs": [...],   # 消失タスクの ref リスト（cleanup 用）
            "auto_assigned": [...],   # project_mapping で自動割り当て成功
            "needs_review": [...],    # プロジェクト特定できず → 要確認
        }
    """
    store = load_task_store(tasks_dir, datasource_id)
    datasource = load_datasource(datasources_dir, datasource_id)
    project_mapping = datasource.get("project_mapping", {}) if datasource else {}

    # 旧ストアの ID セット（消失検出・自動割り当て判定に使う）
    existing_ids: set[str] = {t["remote_id"] for t in store.get("tasks", [])}
    # 旧ストアのタイトル（消失レポート用）
    existing_titles: dict[str, str] = {t["remote_id"]: t.get("title", "") for t in store.get("tasks", [])}

    # incoming から新しいタスクリストを直接構築（洗い替え）
    new_tasks = []
    added = []
    auto_assigned = []
    needs_review = []

    for t in incoming_tasks:
        remote_id = t["remote_id"]
        task_entry = {
            "remote_id": remote_id,
            "title": t["title"],
            "due_date": t.get("due_date"),
            "url": t.get("url"),
            "project_key": t.get("project_key"),
        }
        new_tasks.append(task_entry)

        # 新規タスクのみ自動割り当て対象
        if remote_id not in existing_ids:
            added.append({
                "datasource_id": datasource_id,
                "remote_id": remote_id,
                "title": task_entry["title"],
                "project_key": task_entry["project_key"],
            })
            project_id = resolve_project(task_entry["project_key"], project_mapping)
            ref = f"{datasource_id}/{remote_id}"
            if project_id:
                auto_assigned.append({
                    "ref": ref,
                    "title": task_entry["title"],
                    "project_id": project_id,
                    "milestone_id": "_default",
                    "project_key": task_entry["project_key"],
                })
            else:
                needs_review.append({
                    "ref": ref,
                    "title": task_entry["title"],
                    "project_key": task_entry["project_key"],
                })

    # 消失タスク = 旧ストアにあって incoming にないもの
    incoming_ids = {t["remote_id"] for t in incoming_tasks}
    vanished = [
        {"datasource_id": datasource_id, "remote_id": rid, "title": existing_titles[rid]}
        for rid in existing_ids - incoming_ids
    ]
    vanished_refs = [f"{datasource_id}/{rid}" for rid in existing_ids - incoming_ids]

    # 洗い替え保存
    store["tasks"] = new_tasks
    store["updated_at"] = now_iso()
    save_task_store(tasks_dir, store)

    return {
        "added": added,
        "vanished": vanished,
        "vanished_refs": vanished_refs,
        "auto_assigned": auto_assigned,
        "needs_review": needs_review,
    }


def cleanup_vanished_refs(repo_dir: Path, vanished_refs: list[str]) -> None:
    """消失タスクへの参照を projects と daily から削除する。"""
    if not vanished_refs:
        return
    refs_to_remove = set(vanished_refs)

    # projects/*.json のマイルストーンタスク参照をクリーンアップ
    projects_dir = repo_dir / "projects"
    if projects_dir.exists():
        for project_path in projects_dir.glob("*.json"):
            with open(project_path, encoding="utf-8") as f:
                project = json.load(f)
            changed = False
            for milestone in project.get("milestones", []):
                original = milestone.get("tasks", [])
                filtered = [t for t in original if t.get("ref") not in refs_to_remove]
                if len(filtered) != len(original):
                    milestone["tasks"] = filtered
                    changed = True
            if changed:
                with open(project_path, "w", encoding="utf-8") as f:
                    json.dump(project, f, ensure_ascii=False, indent=2)
                    f.write("\n")

    # daily/*.json のゴールタスク参照をクリーンアップ
    daily_dir = repo_dir / "daily"
    if daily_dir.exists():
        for daily_path in daily_dir.glob("*.json"):
            with open(daily_path, encoding="utf-8") as f:
                daily = json.load(f)
            changed = False
            new_goals = []
            for goal in daily.get("goals", []):
                original = goal.get("tasks", [])
                filtered = [t for t in original if t.get("ref") not in refs_to_remove]
                if len(filtered) != len(original):
                    goal["tasks"] = filtered
                    changed = True
                # tasks が空になったゴールエントリも除外
                if filtered:
                    new_goals.append(goal)
                else:
                    changed = True
            if changed:
                daily["goals"] = new_goals
                with open(daily_path, "w", encoding="utf-8") as f:
                    json.dump(daily, f, ensure_ascii=False, indent=2)
                    f.write("\n")


def apply_auto_assignments(repo_dir: Path, auto_assigned: list[dict]) -> None:
    """
    自動割り当て成功タスクをプロジェクトの _default マイルストーンに追加する。
    既に含まれているタスクはスキップする。
    """
    projects_dir = repo_dir / "projects"
    if not projects_dir.exists():
        return

    # project_id ごとにグループ化
    by_project: dict[str, list[dict]] = {}
    for item in auto_assigned:
        pid = item["project_id"]
        by_project.setdefault(pid, []).append(item)

    for project_id, items in by_project.items():
        project_path = projects_dir / f"{project_id}.json"
        if not project_path.exists():
            print(
                f"警告: プロジェクト {project_id} が見つかりません（{project_path}）",
                file=sys.stderr,
            )
            continue

        with open(project_path, encoding="utf-8") as f:
            project = json.load(f)

        # 既存の全タスク参照を収集
        existing_refs: set[str] = set()
        for milestone in project.get("milestones", []):
            for task in milestone.get("tasks", []):
                existing_refs.add(task["ref"])

        # _default マイルストーンを見つける（なければ末尾に作成）
        default_milestone = None
        for milestone in project.get("milestones", []):
            if milestone["milestone_id"] == "_default":
                default_milestone = milestone
                break

        if default_milestone is None:
            default_milestone = {
                "milestone_id": "_default",
                "name": "未分類",
                "goal": "",
                "due_date": None,
                "tasks": [],
            }
            project.setdefault("milestones", []).append(default_milestone)

        # 新規タスクを _default に追加
        added_count = 0
        for item in items:
            ref = item["ref"]
            if ref not in existing_refs:
                default_milestone.setdefault("tasks", []).append({"ref": ref})
                existing_refs.add(ref)
                added_count += 1

        if added_count > 0:
            with open(project_path, "w", encoding="utf-8") as f:
                json.dump(project, f, ensure_ascii=False, indent=2)
                f.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="fetch-all.sh の JSONL 出力を受け取り、tasks/*.json を更新する"
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

    # tasks/*.json に存在するが今回の JSONL に含まれないデータソースも処理
    # （全タスクが完了した場合、JSONL に出現しなくなる）
    if tasks_dir.exists():
        for task_file in tasks_dir.glob("*.json"):
            ds_id = task_file.stem
            if ds_id not in by_datasource:
                by_datasource[ds_id] = []

    # 集計結果
    total_added = []
    total_vanished = []
    total_vanished_refs = []
    total_auto_assigned = []
    total_needs_review = []

    for datasource_id, incoming_tasks in by_datasource.items():
        result = process_datasource(
            datasource_id, incoming_tasks, tasks_dir, datasources_dir
        )
        total_added.extend(result["added"])
        total_vanished.extend(result["vanished"])
        total_vanished_refs.extend(result["vanished_refs"])
        total_auto_assigned.extend(result["auto_assigned"])
        total_needs_review.extend(result["needs_review"])

    # 自動割り当て成功タスクをプロジェクトに反映
    if total_auto_assigned:
        apply_auto_assignments(repo_dir, total_auto_assigned)

    # 消失タスクの参照を projects と daily からクリーンアップ
    if total_vanished_refs:
        cleanup_vanished_refs(repo_dir, total_vanished_refs)

    # レポートを JSON で出力
    report = {
        "summary": {
            "added": len(total_added),
            "vanished": len(total_vanished),
            "auto_assigned": len(total_auto_assigned),
            "needs_review": len(total_needs_review),
        },
        "added": total_added,
        "vanished": total_vanished,
        "auto_assigned": total_auto_assigned,
        "needs_review": total_needs_review,
    }

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
