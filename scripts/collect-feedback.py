#!/usr/bin/env python3
"""
collect-feedback.py - タスクへのフィードバックを外部ソースから収集する

Jira コメントや Bitbucket PR コメントを取得し、タスク YAML の feedback フィールドに追記する。

使い方:
  python3 collect-feedback.py --repo ~/.local/share/my-tasks --id 20260301-001
  python3 collect-feedback.py --repo ~/.local/share/my-tasks --id 20260301-001 --source jira
  python3 collect-feedback.py --repo ~/.local/share/my-tasks --id 20260301-001 --source bitbucket
"""

import argparse
import importlib
import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

from lib.task_store import FeedbackItem, load_task_yaml, save_task_yaml

# sync-tasks.py をモジュールとしてインポート（load_datasource のみ）
_script_dir = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("sync_tasks", _script_dir / "sync-tasks.py")
_sync_tasks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sync_tasks)

load_datasource = _sync_tasks.load_datasource

JST = timezone(timedelta(hours=9))


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def resolve_jira_site(remote_id: str, site_mapping: dict) -> str | None:
    """remote_id (例: KITE-71) のプレフィックスから Jira サイトを特定する。"""
    prefix = remote_id.split("-")[0] if "-" in remote_id else remote_id
    return site_mapping.get(prefix)


def collect_jira_comments(
    remote_id: str, site: str, cursor_ts: str | None,
) -> tuple[list[dict], str | None]:
    """Jira コメントを収集する。cursor_ts 以降のコメントのみ返す。"""
    cmd = ["atl", "jira", "issue", "view", "--key", remote_id, "--json", "--site", site]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        print(f"警告: Jira コメント取得がタイムアウトしました: {remote_id}", file=sys.stderr)
        return [], cursor_ts

    if result.returncode != 0:
        print(f"警告: Jira コメント取得に失敗しました: {result.stderr.strip()}", file=sys.stderr)
        return [], cursor_ts

    try:
        issue_data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"警告: Jira レスポンスのパースに失敗しました: {e}", file=sys.stderr)
        return [], cursor_ts

    comments_raw = issue_data.get("comments") or []
    if not comments_raw:
        return [], cursor_ts

    new_feedback = []
    latest_ts = cursor_ts

    for c in comments_raw:
        created = c.get("created", "")
        if cursor_ts and created <= cursor_ts:
            continue

        new_feedback.append({
            "source": "jira_comment",
            "author": c.get("author", ""),
            "timestamp": created,
            "body": c.get("body", ""),
        })

        if latest_ts is None or created > latest_ts:
            latest_ts = created

    return new_feedback, latest_ts


def parse_pr_url(pr_url: str) -> tuple[str, str, str] | None:
    """Bitbucket PR URL をパースして (workspace, repo, pr_id) を返す。"""
    parsed = urlparse(pr_url)
    # https://bitbucket.org/{workspace}/{repo}/pull-requests/{pr_id}
    parts = parsed.path.strip("/").split("/")
    if len(parts) >= 4 and parts[2] == "pull-requests":
        return parts[0], parts[1], parts[3]
    return None


def collect_bitbucket_pr_comments(
    pr_url: str, cursor_ts: str | None,
) -> tuple[list[dict], str | None]:
    """Bitbucket PR コメントを収集する。cursor_ts 以降のコメントのみ返す。"""
    parsed = parse_pr_url(pr_url)
    if not parsed:
        print(f"警告: PR URL のパースに失敗しました: {pr_url}", file=sys.stderr)
        return [], cursor_ts

    workspace, repo, pr_id = parsed
    cmd = [
        "atl", "bitbucket", "pr", "comment",
        "--workspace", workspace, "--repo", repo, "--pr", pr_id, "--json",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        print(f"警告: Bitbucket PR コメント取得がタイムアウトしました: {pr_url}", file=sys.stderr)
        return [], cursor_ts

    if result.returncode != 0:
        print(f"警告: Bitbucket PR コメント取得に失敗しました: {result.stderr.strip()}", file=sys.stderr)
        return [], cursor_ts

    try:
        comments_raw = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"警告: Bitbucket レスポンスのパースに失敗しました: {e}", file=sys.stderr)
        return [], cursor_ts

    if not isinstance(comments_raw, list):
        comments_raw = comments_raw.get("comments", []) if isinstance(comments_raw, dict) else []

    new_feedback = []
    latest_ts = cursor_ts

    for c in comments_raw:
        created = c.get("created", "") or c.get("created_on", "")
        if cursor_ts and created <= cursor_ts:
            continue

        new_feedback.append({
            "source": "bitbucket_pr",
            "author": c.get("author", ""),
            "timestamp": created,
            "body": c.get("body", "") or c.get("content", ""),
        })

        if latest_ts is None or created > latest_ts:
            latest_ts = created

    return new_feedback, latest_ts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="タスクへのフィードバックを外部ソースから収集する"
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
        "--source",
        choices=["jira", "bitbucket", "all"],
        default="all",
        help="収集ソース（デフォルト: all）",
    )
    args = parser.parse_args()

    repo_dir = Path(args.repo).expanduser().resolve()
    tasks_dir = repo_dir / "tasks"
    datasources_dir = repo_dir / "datasources"
    task_id = args.id

    if not repo_dir.exists():
        print(f"エラー: リポジトリが見つかりません: {repo_dir}", file=sys.stderr)
        sys.exit(1)

    # タスク YAML を読み込み
    task_data = load_task_yaml(tasks_dir, task_id)
    if task_data is None:
        print(f"エラー: タスクが見つかりません: {task_id}", file=sys.stderr)
        sys.exit(1)

    collected: list[dict] = []

    # Jira コメント収集
    if args.source in ("jira", "all") and task_data.datasource_id:
        datasource = load_datasource(datasources_dir, task_data.datasource_id)
        if datasource and datasource.get("type") == "jira" and task_data.remote_id:
            site_mapping = datasource.get("site_mapping", {})
            site = resolve_jira_site(task_data.remote_id, site_mapping)
            if site:
                jira_cursor = task_data.feedback_cursor.get("jira_comment")
                new_comments, new_cursor = collect_jira_comments(task_data.remote_id, site, jira_cursor)
                for item in new_comments:
                    task_data.feedback.append(FeedbackItem.from_dict({**item, "generation": task_data.generation}))
                collected.extend(new_comments)
                if new_cursor:
                    task_data.feedback_cursor["jira_comment"] = new_cursor
            else:
                print(
                    f"警告: site_mapping にプレフィックスが見つかりません: {task_data.remote_id}",
                    file=sys.stderr,
                )

    # Bitbucket PR コメント収集
    if args.source in ("bitbucket", "all") and task_data.pr_url:
        bb_cursor = task_data.feedback_cursor.get("bitbucket_pr")
        new_comments, new_cursor = collect_bitbucket_pr_comments(task_data.pr_url, bb_cursor)
        for item in new_comments:
            task_data.feedback.append(FeedbackItem.from_dict({**item, "generation": task_data.generation}))
        collected.extend(new_comments)
        if new_cursor:
            task_data.feedback_cursor["bitbucket_pr"] = new_cursor

    # タスク YAML を更新
    save_task_yaml(tasks_dir, task_id, task_data)

    # レポート出力
    report = {
        "task_id": task_id,
        "remote_id": task_data.remote_id,
        "collected_count": len(collected),
        "total_feedback_count": len(task_data.feedback),
        "collected": collected,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
