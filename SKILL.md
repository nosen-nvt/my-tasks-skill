---
name: my-tasks
description: |
  個人タスク管理スキル。複数外部データソース（JIRA、Microsoft To Do、メール等）からタスクを収集し、
  プロジェクト単位で管理する。タスクの精査・プロンプト生成・承認・実行のワークフローを提供。
  使用例:「タスクを最新化して」「プロンプトを生成して」「承認して」
  「実行して」「ディスパッチして」「ステータスを確認して」「タスクを操作して」
  「完了処理を実行して」「プロジェクトを追加して」「データソースを追加して」
  「メールをチェックして」「ジョブの状態を確認して」
  「次にやる」など、
  タスク管理リポジトリ（~/.local/share/my-tasks/）への操作を依頼された場合に使用。
---

# my-tasks スキル

個人のタスク管理リポジトリ（`~/.local/share/my-tasks/`）を Claude Code から操作するスキル。
JIRA・Microsoft To Do・メール等の外部データソースからタスクを収集し、プロジェクト単位で管理する。
タスクの精査 → プロンプト生成 → 承認 → 実行のワークフローを提供する。
設定情報（datasources/, projects/）は git 管理、タスクデータ（tasks/）はローカルのみ。

## 主要オペレーション

- **タスク収集** - 全データソースからタスクを取得し、index.jsonl + Markdown を更新
- **メールトリアージ** - メールデータソースからアクションアイテムを収集
- **dispatch** - Lifecycle を開始（CLI がタスク解決、サーバーがジョブチェーンを自動制御）
- **resume** - suspend 中の Lifecycle を再開（ユーザ入力の反映、承認）
- **ステータス確認** - Lifecycle・ジョブの状況表示
- **タスク操作** - データソース側のタスクを操作（ステータス変更等）
- **設定管理** - プロジェクト・データソースの CRUD、リポジトリ初期化

全操作の詳細手順と番号付きリストは `references/operations.md` を参照。

## 詳細リファレンス

- **アーキテクチャ・リポジトリ構成**: `references/architecture.md`
- **全スキーマと記述例**: `references/schemas.md`
- **各操作の詳細手順**: `references/operations.md`
- **ディスパッチャー設計**: `references/dispatcher-design.md`

各オペレーションの具体的なコマンドや手順は `references/operations.md` を参照。
スクリプトの引数・出力形式の詳細は `references/operations.md` 内の各セクションに記載。

## 注意事項

- 設定変更（datasources/, projects/）後に `git add . && git commit && git push` を実行する
- `tasks/` は gitignore なので commit 不要
