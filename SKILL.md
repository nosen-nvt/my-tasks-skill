---
name: my-tasks
description: |
  個人タスク管理スキル。複数外部データソース（JIRA、Microsoft To Do、メール等）からタスクを収集し、
  プロジェクト単位で管理する。Plan（対話的精査）→ Dispatch（ジョブ実行）→ Resume/Feedback のワークフローを提供。
  使用例:「タスクを最新化して」「Plan して」「Dispatch して」「Resume して」
  「フィードバックを収集して」「ステータスを確認して」「タスクを操作して」
  「完了処理を実行して」「プロジェクトを追加して」「データソースを追加して」
  「メールをチェックして」「ジョブの状態を確認して」
  「次にやる」など、
  タスク管理リポジトリ（~/.local/share/my-tasks/）への操作を依頼された場合に使用。
---

# my-tasks スキル

個人のタスク管理リポジトリ（`~/.local/share/my-tasks/`）を Claude Code から操作するスキル。
JIRA・Microsoft To Do・メール等の外部データソースからタスクを収集し、プロジェクト単位で管理する。
Plan（対話的精査）→ Dispatch（ジョブ実行）→ Resume / Feedback のワークフローを提供する。
タスク YAML が唯一の真実の源（Single Source of Truth）。
設定情報（datasources/, projects/）は git 管理、タスクデータ（tasks/）はローカルのみ。

## 主要オペレーション

- **タスク収集** - 全データソースからタスクを取得し、index.jsonl + YAML を更新
- **メールトリアージ** - メールデータソースからアクションアイテムを収集
- **Plan** - 対話セッションでタスクを精査・計画し、execute_prompt を生成
- **Dispatch** - execute_prompt をオーケストレーターの run に渡してジョブ実行
- **Resume** - 完了済みセッションを再開し、軽微な修正を対話的に実施
- **Feedback** - フィードバック収集（PR コメント等）→ 対応ジョブ Dispatch
- **ステータス確認** - ジョブの状況表示、タスク一覧
- **タスク操作** - データソース側のタスクを操作（ステータス変更等）
- **設定管理** - プロジェクト・データソースの CRUD、リポジトリ初期化
- **対話セッション** - ジョブ管理の対象外で対話セッションを起動

全操作の詳細手順と番号付きリストは `references/operations.md` を参照。

## 詳細リファレンス

- **アーキテクチャ・リポジトリ構成**: `references/architecture.md`
- **全スキーマと記述例**: `references/schemas.md`
- **各操作の詳細手順**: `references/operations.md`
- **オーケストレーター設計**: `references/orchestrator-design.md`

各オペレーションの具体的なコマンドや手順は `references/operations.md` を参照。
スクリプトの引数・出力形式の詳細は `references/operations.md` 内の各セクションに記載。

## 注意事項

- 設定変更（datasources/, projects/）後に `git add . && git commit && git push` を実行する
- `tasks/` は gitignore なので commit 不要
