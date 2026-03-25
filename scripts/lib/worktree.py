"""git worktree 操作ユーティリティ。"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger("worktree")


def branch_safe(branch: str) -> str:
    """ブランチ名をファイルシステム安全な形式に変換（/ → --）。"""
    return branch.replace("/", "--")


def worktree_path(repo_dir: str, branch: str) -> Path:
    """ワークツリーのパスを計算。

    配置: {repo_dir}/../.worktrees/{repo_name}/{branch_safe}/
    """
    repo = Path(repo_dir)
    return repo.parent / ".worktrees" / repo.name / branch_safe(branch)


def ensure_worktree(repo_dir: str, branch: str, base_branch: str = "main") -> Path:
    """ワークツリーを作成または既存を返す。

    - ワークツリーが存在する → そのパスを返す
    - ブランチが存在するがワークツリーがない → git worktree add <path> <branch>
    - ブランチもない → git worktree add -b <branch> <path> <base_branch>
    """
    wt_path = worktree_path(repo_dir, branch)

    if wt_path.is_dir():
        log.info(f"Reusing existing worktree: {wt_path}")
        return wt_path

    wt_path.parent.mkdir(parents=True, exist_ok=True)

    # ブランチが既に存在するか確認
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
        cwd=repo_dir, capture_output=True,
    )
    branch_exists = result.returncode == 0

    if branch_exists:
        cmd = ["git", "worktree", "add", str(wt_path), branch]
    else:
        subprocess.run(
            ["git", "fetch", "origin", base_branch],
            cwd=repo_dir, capture_output=True, text=True,
        )
        cmd = ["git", "worktree", "add", "-b", branch, str(wt_path), f"origin/{base_branch}"]

    result = subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {result.stderr.strip()}")

    log.info(f"Created worktree: {wt_path} (branch={branch})")
    return wt_path


def remove_worktree(repo_dir: str, branch: str) -> None:
    """git worktree remove を実行。"""
    wt_path = worktree_path(repo_dir, branch)
    if not wt_path.exists():
        return

    result = subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt_path)],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.warning(f"git worktree remove failed: {result.stderr.strip()}")
    else:
        log.info(f"Removed worktree: {wt_path}")


def push_branch(repo_dir: str, branch: str, remote: str = "origin") -> bool:
    """ブランチを push。push するものがなければ False。"""
    wt_path = worktree_path(repo_dir, branch)
    cwd = str(wt_path) if wt_path.is_dir() else repo_dir

    # リモートに差分があるか確認
    result = subprocess.run(
        ["git", "log", f"{remote}/{branch}..{branch}", "--oneline"],
        cwd=cwd, capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        # リモートブランチが無い場合も push が必要
        check = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/remotes/{remote}/{branch}"],
            cwd=cwd, capture_output=True,
        )
        if check.returncode == 0:
            log.info(f"No commits to push for {branch}")
            return False

    result = subprocess.run(
        ["git", "push", "-u", remote, branch],
        cwd=cwd, capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.warning(f"git push failed: {result.stderr.strip()}")
        return False

    log.info(f"Pushed branch: {branch}")
    return True


def create_pr_if_needed(
    repo_dir: str,
    branch: str,
    title: str,
    pr_command: str | None = None,
) -> str | None:
    """PR が存在しなければ作成し、URL を返す。既に存在すれば既存の URL を返す。"""
    wt_path = worktree_path(repo_dir, branch)
    cwd = str(wt_path) if wt_path.is_dir() else repo_dir

    if pr_command:
        # カスタム PR コマンド
        cmd = pr_command.format(branch=branch, title=title)
        result = subprocess.run(
            cmd, shell=True, cwd=cwd,
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            log.info(f"PR created/found: {url}")
            return url
        log.warning(f"PR command failed: {result.stderr.strip()}")
        return None

    # デフォルト: gh CLI
    # 既存 PR の確認
    result = subprocess.run(
        ["gh", "pr", "view", branch, "--json", "url", "--jq", ".url"],
        cwd=cwd, capture_output=True, text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        url = result.stdout.strip()
        log.info(f"PR already exists: {url}")
        return url

    # 新規作成
    result = subprocess.run(
        ["gh", "pr", "create", "--base", "main", "--head", branch,
         "--title", title, "--fill"],
        cwd=cwd, capture_output=True, text=True,
    )
    if result.returncode == 0:
        url = result.stdout.strip()
        log.info(f"PR created: {url}")
        return url

    log.warning(f"gh pr create failed: {result.stderr.strip()}")
    return None


def resolve_branch_name(template: str, context: dict) -> str:
    """テンプレートとタスクコンテキストからブランチ名を導出。

    template: "feature/{remote_id}" 等
    context: {"remote_id": "JIRA-123", "task_id": "20260323-001", ...}
    """
    try:
        return template.format(**context)
    except KeyError:
        # テンプレート変数が見つからない場合は task_id にフォールバック
        task_id = context.get("task_id", "unknown")
        log.warning(f"Branch template expansion failed for '{template}', falling back to task/{task_id}")
        return f"task/{task_id}"
