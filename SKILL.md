---
name: my-tasks
description: |
  個人タスク管理スキル。複数外部データソース（JIRA、Microsoft To Do等）からタスクを収集し、
  プロジェクト・マイルストーン単位で管理する。
  使用例:「タスクを最新化して」「今日のゴールを設定して」
  「プロジェクトの状況を確認して」「データソースを追加して」「プロジェクトを追加して」
  「タスクを操作して（データソース側）」など、タスク管理リポジトリ（~/.local/share/my-tasks/）
  への操作を依頼された場合に使用。
---

# my-tasks スキル

個人のタスク管理リポジトリ（`~/.local/share/my-tasks/`）を Claude Code から操作するスキル。
JIRA・Microsoft To Do 等の外部データソースからタスクを収集し、プロジェクト・マイルストーン単位で整理・管理する。
タスク管理リポジトリは git で管理され、すべての変更操作後に自動で commit + push する。

## 利用可能な操作

1. **リポジトリ初期化** - `~/.local/share/my-tasks/` を新規作成、またはリモートからクローン
2. **データソース追加** - JIRA・Microsoft To Do 等の新しいデータソースを登録
3. **プロジェクト追加** - 新しいプロジェクトを作成（`_default` マイルストーン付き）
4. **マイルストーン追加** - 既存プロジェクトにマイルストーンを追加
5. **タスク最新化** - 全データソースからタスクを取得し、タスクストアを更新
6. **日次ゴール設定** - 今日取り組むタスクを選定して日次ゴールファイルを作成
7. **タスク操作（データソース側）** - ステータス変更・担当者変更・新規作成等をデータソースに反映
8. **参照系** - プロジェクト状況確認・日次ゴール確認・タスク検索

## 詳細リファレンス

- **アーキテクチャ・リポジトリ構成**: `references/architecture.md`
- **全 JSON スキーマと記述例**: `references/schemas.md`
- **各操作の詳細手順**: `references/operations.md`

## sync-tasks.py の使い方

タスク最新化の中核ロジックは `scripts/sync-tasks.py` に実装されている。

```bash
# 基本的な使い方（fetch-all.sh の出力を直接パイプ）
~/.local/share/my-tasks/scripts/fetch-all.sh \
  | python3 ~/.claude/skills/my-tasks/scripts/sync-tasks.py \
    --repo ~/.local/share/my-tasks

# ファイルから読み込む場合
python3 ~/.claude/skills/my-tasks/scripts/sync-tasks.py \
  --repo ~/.local/share/my-tasks \
  --input /tmp/tasks.jsonl
```

### 出力（JSON レポート）

```json
{
  "summary": {
    "added": 3,
    "updated": 5,
    "vanished": 1,
    "auto_assigned": 2,
    "needs_review": 1
  },
  "added": [...],
  "updated": [...],
  "vanished": [...],
  "auto_assigned": [...],
  "needs_review": [...]
}
```

- `vanished`: ストアから削除したタスク（プロジェクトと日次ゴールの参照も削除済み）
- `auto_assigned`: `datasources/*.json` の `project_mapping` で自動割り当て成功したタスク（`projects/*.json` の `_default` マイルストーンに追加済み）
- `needs_review`: プロジェクトが特定できなかったタスク → ユーザーに提案・確認が必要

## 注意事項

- すべての書き込み操作後に `git add . && git commit && git push` を実行する
