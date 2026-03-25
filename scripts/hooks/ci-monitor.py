#!/usr/bin/env python3
"""ci-monitor.py — GitHub Actions の実行を監視し、失敗時に自動修正を起動する

プロジェクト設定の hooks で以下のように登録する:
  {
    "id": "ci_monitor",
    "type": "script",
    "when": {"status": "in_review", "pr.url": {"not_empty": true}, "pr.ci_status": ""},
    "command": ".../scripts/hooks/ci-monitor.py --task-id {task_id} --pr-url {pr.url} --branch {branch}"
  }
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
ORCHESTRATOR_DIR = SCRIPT_DIR.parent / "orchestrator"

# scripts/ を sys.path に追加して lib をインポート
sys.path.insert(0, str(SCRIPT_DIR.parent))
from lib.task_store import (
    FeedbackGroup, FeedbackItem, load_task_yaml, save_task_yaml, now_iso,
)

# --- 定数 ---
MAX_CI_RETRIES = 5
CI_POLL_INTERVAL = 30       # 秒
CI_INITIAL_DELAY = 15       # 秒（Actions 登録待ち）
CI_TIMEOUT = 3600            # 秒（1 ラウンド最大監視時間）
CI_NO_CHECKS_GRACE = 180    # 秒（チェック未登録の猶予期間）
MAX_ERROR_LOG_LEN = 4000    # エラーログ最大長

DEFAULT_REPO = Path("~/.local/share/my-tasks").expanduser()


# ---------------------------------------------------------------------------
# GitHub PR URL パーサー
# ---------------------------------------------------------------------------

def parse_github_pr_url(pr_url: str) -> tuple[str, str, str] | None:
    """GitHub PR URL をパースして (owner, repo, pr_number) を返す。"""
    parsed = urlparse(pr_url)
    if parsed.hostname not in ("github.com", "www.github.com"):
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) >= 4 and parts[2] == "pull":
        return parts[0], parts[1], parts[3]
    return None


# ---------------------------------------------------------------------------
# Orchestrator CLI ヘルパー
# ---------------------------------------------------------------------------

def dispatch(task_id: str, action_type: str, field: str = "", value: str = "") -> bool:
    """orchestrator dispatch コマンドを実行する。"""
    cmd = [sys.executable, str(ORCHESTRATOR_DIR), "dispatch", task_id, action_type]
    if field:
        cmd.extend(["--field", field])
    if value:
        cmd.extend(["--value", value])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"dispatch error: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------------------
# GitHub Actions ステータス取得
# ---------------------------------------------------------------------------

def poll_pr_checks(nwo: str, pr_number: str) -> str:
    """gh pr view で PR のチェック状態を取得する。

    Returns: "success" | "failure" | "running" | "pending" | "no_checks"
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "view", pr_number,
             "--repo", nwo,
             "--json", "statusCheckRollup"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        print("gh pr view timeout", file=sys.stderr)
        return "pending"

    if result.returncode != 0:
        print(f"gh pr view failed: {result.stderr.strip()[:200]}", file=sys.stderr)
        return "pending"

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("gh pr view: JSON parse error", file=sys.stderr)
        return "pending"

    checks = data.get("statusCheckRollup") or []
    if not checks:
        return "no_checks"

    has_pending = False
    for check in checks:
        status = (check.get("status") or "").upper()
        conclusion = (check.get("conclusion") or "").upper()
        state = (check.get("state") or "").upper()

        # StatusContext 形式
        if state:
            if state in ("FAILURE", "ERROR"):
                return "failure"
            if state == "PENDING":
                has_pending = True
            continue

        # CheckRun 形式
        if status in ("QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "PENDING"):
            has_pending = True
        elif status == "COMPLETED":
            if conclusion in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"):
                return "failure"
            # SUCCESS, SKIPPED, NEUTRAL は OK
        else:
            has_pending = True

    return "running" if has_pending else "success"


def get_failed_run_ids(nwo: str, pr_number: str) -> list[str]:
    """statusCheckRollup から失敗した run ID を抽出する。"""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", pr_number,
             "--repo", nwo,
             "--json", "statusCheckRollup"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return []

    if result.returncode != 0:
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    checks = data.get("statusCheckRollup") or []
    run_ids: list[str] = []

    for check in checks:
        conclusion = (check.get("conclusion") or "").upper()
        state = (check.get("state") or "").upper()

        is_failed = conclusion in ("FAILURE", "TIMED_OUT", "CANCELLED") or state in ("FAILURE", "ERROR")
        if not is_failed:
            continue

        details_url = check.get("detailsUrl", "")
        if "/actions/runs/" in details_url:
            run_id = details_url.split("/actions/runs/")[1].split("/")[0]
            if run_id not in run_ids:
                run_ids.append(run_id)

    return run_ids


def get_failed_run_log(nwo: str, run_id: str) -> str:
    """失敗した run のエラーログを取得する。"""
    try:
        result = subprocess.run(
            ["gh", "run", "view", run_id,
             "--repo", nwo,
             "--log-failed"],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return ""

    if result.returncode != 0:
        return ""

    return result.stdout


# ---------------------------------------------------------------------------
# メインロジック
# ---------------------------------------------------------------------------

def check_retry_limit(tasks_dir: Path, task_id: str) -> bool:
    """リトライ上限チェック。超過していれば True。"""
    task = load_task_yaml(tasks_dir, task_id)
    if task is None:
        return True

    ci_failure_count = sum(
        1 for g in task.feedback
        for item in g.items
        if item.source == "github_actions"
    )
    return ci_failure_count >= MAX_CI_RETRIES


def check_task_still_in_review(tasks_dir: Path, task_id: str) -> bool:
    """タスクがまだ in_review かチェック。"""
    task = load_task_yaml(tasks_dir, task_id)
    return task is not None and task.status == "in_review"


def handle_ci_failure(
    tasks_dir: Path, task_id: str,
    nwo: str, pr_number: str,
) -> None:
    """CI 失敗時: エラーログ取得 → feedback 書き込み → ci_failed dispatch。"""
    # エラーログ取得
    error_log = ""
    run_ids = get_failed_run_ids(nwo, pr_number)
    if run_ids:
        error_log = get_failed_run_log(nwo, run_ids[0])

    # feedback 書き込み
    task = load_task_yaml(tasks_dir, task_id)
    if task is None:
        return

    body = f"GitHub Actions CI failed.\n\n"
    if error_log:
        if len(error_log) > MAX_ERROR_LOG_LEN:
            error_log = error_log[-MAX_ERROR_LOG_LEN:]
            body += f"```\n... (truncated)\n{error_log}\n```"
        else:
            body += f"```\n{error_log}\n```"
    else:
        body += "Failed to retrieve error logs. Check the Actions tab manually."

    group = FeedbackGroup(
        collected_at=now_iso(),
        items=[FeedbackItem(
            source="github_actions",
            body=body,
            author="ci_monitor",
            timestamp=now_iso(),
        )],
    )
    task.feedback.append(group)
    save_task_yaml(tasks_dir, task_id, task)

    # ci_status を failure に更新してから ci_failed dispatch
    dispatch(task_id, "update_field", field="pr.ci_status", value="failure")
    dispatch(task_id, "ci_failed")


def main() -> None:
    parser = argparse.ArgumentParser(description="GitHub Actions CI 監視スクリプト")
    parser.add_argument("--task-id", required=True, help="タスク ID")
    parser.add_argument("--pr-url", required=True, help="PR URL")
    parser.add_argument("--branch", default="", help="ブランチ名")
    parser.add_argument("--repo", default=str(DEFAULT_REPO), help="タスク管理リポジトリパス")
    args = parser.parse_args()

    task_id = args.task_id
    pr_url = args.pr_url
    tasks_dir = Path(args.repo).expanduser().resolve() / "tasks"

    # GitHub PR のみ対象
    parsed = parse_github_pr_url(pr_url)
    if parsed is None:
        print(f"Not a GitHub PR, skipping: {pr_url}", file=sys.stderr)
        dispatch(task_id, "update_field", field="pr.ci_status", value="success")
        return

    owner, repo, pr_number = parsed
    nwo = f"{owner}/{repo}"
    print(f"Monitoring CI for {nwo}#{pr_number} (task={task_id})", file=sys.stderr)

    # リトライ上限チェック
    if check_retry_limit(tasks_dir, task_id):
        print(f"Max CI retries ({MAX_CI_RETRIES}) reached for {task_id}", file=sys.stderr)
        dispatch(task_id, "update_field", field="pr.ci_status", value="failure")
        return

    # ci_status を pending に設定（再トリガー防止）
    dispatch(task_id, "update_field", field="pr.ci_status", value="pending")

    # 初回遅延（Actions 登録待ち）
    time.sleep(CI_INITIAL_DELAY)

    start_time = time.monotonic()
    last_status = "pending"

    while True:
        # タイムアウトチェック
        elapsed = time.monotonic() - start_time
        if elapsed > CI_TIMEOUT:
            print(f"CI monitoring timeout for {task_id} after {elapsed:.0f}s", file=sys.stderr)
            dispatch(task_id, "update_field", field="pr.ci_status", value="failure")
            return

        # タスク状態チェック
        if not check_task_still_in_review(tasks_dir, task_id):
            print(f"Task {task_id} no longer in_review, stopping", file=sys.stderr)
            return

        # チェック状態取得
        status = poll_pr_checks(nwo, pr_number)

        if status == "no_checks":
            if elapsed < CI_NO_CHECKS_GRACE:
                print(f"No checks yet for {task_id}, waiting...", file=sys.stderr)
                time.sleep(CI_POLL_INTERVAL)
                continue
            else:
                print(f"No checks found for {task_id} after grace period, treating as success", file=sys.stderr)
                dispatch(task_id, "update_field", field="pr.ci_status", value="success")
                return

        if status in ("pending", "running"):
            if last_status != status:
                dispatch(task_id, "update_field", field="pr.ci_status", value="running")
                last_status = status
            time.sleep(CI_POLL_INTERVAL)
            continue

        if status == "success":
            print(f"CI passed for {task_id}", file=sys.stderr)
            dispatch(task_id, "update_field", field="pr.ci_status", value="success")
            return

        if status == "failure":
            print(f"CI failed for {task_id}", file=sys.stderr)
            handle_ci_failure(tasks_dir, task_id, nwo, pr_number)
            return

        # 不明なステータス
        print(f"Unknown status '{status}' for {task_id}", file=sys.stderr)
        time.sleep(CI_POLL_INTERVAL)


if __name__ == "__main__":
    main()
