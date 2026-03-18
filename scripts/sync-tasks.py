#!/usr/bin/env python3
"""
sync-tasks.py - タスク同期スクリプト

fetch-all.sh が出力する JSONL を受け取り、tasks/index.jsonl + tasks/{id}.yaml を更新する。

使い方:
  fetch-all.sh | python3 sync-tasks.py --repo ~/.local/share/my-tasks
  python3 sync-tasks.py --repo ~/.local/share/my-tasks --input /tmp/tasks.jsonl
"""

import argparse
import json
import sys
from pathlib import Path

from lib.task_store import (
    IndexEntry,
    load_index, save_index, generate_task_id,
    create_task_yaml, update_task_yaml, reopen_task_yaml, delete_task,
)


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



# --- タスク操作関数は lib.task_store に移動済み ---


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
    index_entries: list[IndexEntry],
) -> dict:
    """
    1データソース分のタスクを処理する。

    index_entries はインプレースで変更される（新規追加・消失削除）。

    datasource 設定の sync_mode に応じて動作を切り替える:
    - "full": 消失検出を実施（消失タスクはインデックスと Markdown から削除）
    - "append": 消失検出をスキップ、done タスクの GC を実施

    Returns:
        {
            "added": [...],
            "updated": [...],
            "reopened": [...],
            "vanished": [...],
            "gc": [...],
            "project_assigned": [...],
            "project_unassigned": [...],
        }
    """
    datasource = load_datasource(datasources_dir, datasource_id)
    project_mapping = datasource.get("project_mapping", {}) if datasource else {}
    sync_mode = datasource.get("sync_mode", "full") if datasource else "full"

    # append モードの GC: done エントリを除去し .md を削除
    # ただし incoming に含まれる remote_id は再オープン対象なのでスキップ
    incoming_remote_ids = {t["remote_id"] for t in incoming_tasks}
    gc = []
    if sync_mode == "append":
        gc_ids = set()
        for entry in index_entries:
            if (entry.datasource_id == datasource_id
                    and entry.status == "done"
                    and entry.remote_id not in incoming_remote_ids):
                gc.append({
                    "datasource_id": datasource_id,
                    "remote_id": entry.remote_id,
                    "title": entry.title,
                    "id": entry.id,
                })
                gc_ids.add(entry.id)
                delete_task(tasks_dir, entry.id)
        if gc_ids:
            index_entries[:] = [e for e in index_entries
                                if e.id not in gc_ids]

    # 既存インデックスから当該データソースのエントリを取得
    existing_entries = {
        e.remote_id: e for e in index_entries
        if e.datasource_id == datasource_id and e.remote_id
    }

    incoming_ids = {t["remote_id"] for t in incoming_tasks}

    added = []
    updated = []
    reopened = []
    project_assigned = []
    project_unassigned = []

    for t in incoming_tasks:
        remote_id = t["remote_id"]

        if remote_id in existing_entries:
            entry = existing_entries[remote_id]

            if entry.status == "done":
                # 再オープン: 履歴を保持したまま pending に戻す
                entry.status = "pending"
                entry.generation = entry.generation + 1
                if entry.title != t["title"]:
                    entry.title = t["title"]
                reopen_task_yaml(tasks_dir, entry)
                reopened.append({
                    "datasource_id": datasource_id,
                    "remote_id": remote_id,
                    "title": entry.title,
                    "id": entry.id,
                    "generation": entry.generation,
                })
            elif entry.title != t["title"]:
                # 既存タスク: title 変更があれば更新
                entry.title = t["title"]
                update_task_yaml(tasks_dir, entry)
                updated.append({
                    "datasource_id": datasource_id,
                    "remote_id": remote_id,
                    "title": t["title"],
                    "id": entry.id,
                })
        else:
            # 新規タスク
            task_id = generate_task_id(index_entries, tasks_dir)
            project_id = resolve_project(t.get("project_key"), project_mapping) or ""

            new_entry = IndexEntry(
                id=task_id,
                remote_id=remote_id,
                datasource_id=datasource_id,
                title=t["title"],
                status="pending",
                project_id=project_id,
                run_count=0,
                generation=1,
            )
            index_entries.append(new_entry)
            create_task_yaml(tasks_dir, new_entry)

            added.append({
                "datasource_id": datasource_id,
                "remote_id": remote_id,
                "title": t["title"],
                "id": task_id,
                "project_key": t.get("project_key"),
            })

            if project_id:
                project_assigned.append({
                    "id": task_id,
                    "title": t["title"],
                    "project_id": project_id,
                    "project_key": t.get("project_key"),
                })
            else:
                project_unassigned.append({
                    "id": task_id,
                    "title": t["title"],
                    "project_key": t.get("project_key"),
                })

    # 消失検出は full モードのみ
    vanished = []
    if sync_mode == "full":
        vanished_ids = set()
        for remote_id, entry in existing_entries.items():
            if remote_id not in incoming_ids:
                vanished.append({
                    "datasource_id": datasource_id,
                    "remote_id": remote_id,
                    "title": entry.title,
                    "id": entry.id,
                })
                vanished_ids.add(entry.id)
                delete_task(tasks_dir, entry.id)

        # index_entries から消失タスクを除去
        index_entries[:] = [e for e in index_entries
                            if e.id not in vanished_ids]

    return {
        "added": added,
        "updated": updated,
        "reopened": reopened,
        "vanished": vanished,
        "gc": gc,
        "project_assigned": project_assigned,
        "project_unassigned": project_unassigned,
    }


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="fetch-all.sh の JSONL 出力を受け取り、tasks/index.jsonl + tasks/*.yaml を更新する"
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
    parser.add_argument(
        "--datasource",
        metavar="IDS",
        default=None,
        help="処理対象のデータソース ID（カンマ区切り）。未指定時は入力 JSONL に含まれるデータソースのみ処理",
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

    # --datasource 指定時: 指定されたデータソースのみ処理対象とする
    if args.datasource:
        target_ds_ids = {s.strip() for s in args.datasource.split(",")}
        # 指定されたが JSONL に含まれないデータソースは空リストで追加
        for ds_id in target_ds_ids:
            if ds_id not in by_datasource:
                by_datasource[ds_id] = []
        # 指定外のデータソースを除外
        by_datasource = {k: v for k, v in by_datasource.items()
                         if k in target_ds_ids}

    # 既存 index を読み込み
    index_entries = load_index(tasks_dir)

    # --datasource 未指定時: index に存在する full sync mode のデータソースを
    # by_datasource に追加する（fetch 結果が0件でも消失検出を実行するため）
    if not args.datasource:
        ds_ids_in_index = {e.datasource_id for e in index_entries
                           if e.datasource_id}
        for ds_id in ds_ids_in_index:
            if ds_id not in by_datasource:
                ds = load_datasource(datasources_dir, ds_id)
                if ds and ds.get("sync_mode", "full") == "full":
                    by_datasource[ds_id] = []

    # 集計結果
    total_added = []
    total_updated = []
    total_reopened = []
    total_vanished = []
    total_gc = []
    total_project_assigned = []
    total_project_unassigned = []

    for datasource_id, incoming_tasks in by_datasource.items():
        result = process_datasource(
            datasource_id, incoming_tasks, tasks_dir, datasources_dir, index_entries
        )
        total_added.extend(result["added"])
        total_updated.extend(result["updated"])
        total_reopened.extend(result["reopened"])
        total_vanished.extend(result["vanished"])
        total_gc.extend(result["gc"])
        total_project_assigned.extend(result["project_assigned"])
        total_project_unassigned.extend(result["project_unassigned"])

    # 全 datasource 横断の GC: done タスクを除去（manual 等、同期対象外も含む）
    gc_all_ids = set()
    for entry in index_entries:
        if entry.status == "done":
            total_gc.append({
                "datasource_id": entry.datasource_id,
                "remote_id": entry.remote_id,
                "title": entry.title,
                "id": entry.id,
            })
            gc_all_ids.add(entry.id)
            delete_task(tasks_dir, entry.id)
    if gc_all_ids:
        index_entries[:] = [e for e in index_entries
                            if e.id not in gc_all_ids]

    # index を保存
    save_index(tasks_dir, index_entries)

    # レポートを JSON で出力
    report = {
        "summary": {
            "added": len(total_added),
            "updated": len(total_updated),
            "reopened": len(total_reopened),
            "vanished": len(total_vanished),
            "gc": len(total_gc),
            "project_assigned": len(total_project_assigned),
            "project_unassigned": len(total_project_unassigned),
        },
        "added": total_added,
        "updated": total_updated,
        "reopened": total_reopened,
        "vanished": total_vanished,
        "gc": total_gc,
        "project_assigned": total_project_assigned,
        "project_unassigned": total_project_unassigned,
    }

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
