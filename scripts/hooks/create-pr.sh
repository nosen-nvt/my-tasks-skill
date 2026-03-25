#!/bin/bash
# create-pr.sh — ブランチを push し PR を作成、orchestrator に pr.url を通知
#
# Usage: create-pr.sh --task-id ID --repo-dir DIR --branch BRANCH --title TITLE [--base BASE]
#
# プロジェクト設定の hooks で以下のように登録する:
#   {
#     "id": "auto_create_pr",
#     "type": "script",
#     "when": {"status": "in_review", "branch": {"not_empty": true}, "pr.url": ""},
#     "command": ".../scripts/hooks/create-pr.sh --task-id {task_id} --repo-dir {working_directory} --branch {branch} --title {title}"
#   }

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ORCHESTRATOR_DIR="$(dirname "$SCRIPT_DIR")/orchestrator"

# --- 引数パース ---
TASK_ID=""
REPO_DIR=""
BRANCH=""
TITLE=""
BASE_BRANCH="main"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --task-id)   TASK_ID="$2";      shift 2 ;;
        --repo-dir)  REPO_DIR="$2";     shift 2 ;;
        --branch)    BRANCH="$2";       shift 2 ;;
        --title)     TITLE="$2";        shift 2 ;;
        --base)      BASE_BRANCH="$2";  shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$TASK_ID" || -z "$REPO_DIR" || -z "$BRANCH" || -z "$TITLE" ]]; then
    echo "Usage: create-pr.sh --task-id ID --repo-dir DIR --branch BRANCH --title TITLE [--base BASE]" >&2
    exit 1
fi

# --- ワークツリーパス解決 ---
REPO_NAME="$(basename "$REPO_DIR")"
BRANCH_SAFE="${BRANCH//\//-}"
WT_PATH="$(dirname "$REPO_DIR")/.worktrees/${REPO_NAME}/${BRANCH_SAFE}"

if [[ -d "$WT_PATH" ]]; then
    CWD="$WT_PATH"
else
    CWD="$REPO_DIR"
fi

# --- push ---
echo "Pushing branch ${BRANCH} ..." >&2
git -C "$CWD" push -u origin "$BRANCH" 2>&1 >&2 || true

# --- PR 確認・作成 ---
PR_URL=""

# 既存 PR の確認 (gh は cwd からリポジトリを検出)
PR_URL="$(cd "$CWD" && gh pr view "$BRANCH" --json url --jq '.url' 2>/dev/null || true)"

if [[ -z "$PR_URL" ]]; then
    echo "Creating PR: ${TITLE} ..." >&2
    PR_URL="$(cd "$CWD" && gh pr create \
        --base "$BASE_BRANCH" \
        --head "$BRANCH" \
        --title "$TITLE" \
        --fill 2>&1)"
fi

if [[ -z "$PR_URL" ]]; then
    echo "Failed to create or find PR" >&2
    exit 1
fi

echo "PR URL: ${PR_URL}" >&2

# --- orchestrator に pr.url を通知 ---
python3 "$ORCHESTRATOR_DIR" dispatch "$TASK_ID" update_field \
    --field pr.url --value "$PR_URL"
