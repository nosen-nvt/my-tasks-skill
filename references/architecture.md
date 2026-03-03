# アーキテクチャリファレンス

## タスク管理リポジトリの場所

```
~/.local/share/my-tasks/
```

このリポジトリは git で管理され、複数マシン間の同期手段として利用する。
Claude Code（エージェント）が読み書きする前提で設計されており、人間が直接編集することは想定しない。

## ディレクトリ構成

```
~/.local/share/my-tasks/
├── .git/
├── .gitignore                       # tasks/ を除外
├── datasources/                     # git 管理
│   ├── jira.json
│   ├── ms-todo.json
│   ├── mail-outlook.json
│   ├── mail-gmail-nvt.json
│   └── mail-gmail-qzl.json
├── projects/                        # git 管理
│   ├── bo.json
│   └── ubs-mgmt-tool.json
├── tasks/                           # gitignored
│   ├── index.jsonl                  # タスクインデックス
│   └── {task_id}.md                 # タスク実体（Markdown）
└── scripts/                         # git 管理
    ├── fetch-all.sh
    ├── fetch-jira.sh
    ├── fetch-ms-todo.sh
    ├── fetch-mail-outlook.sh
    └── fetch-mail-gmail.sh
```

### git 管理ポリシー

- `datasources/` と `projects/` は git 管理（設定情報）
- `tasks/` は `.gitignore` で除外（タスクデータはローカルのみ、再収集で復元可能）
- `scripts/` は git 管理（収集スクリプト）

## タスクの参照方法

タスクは `id`（`YYYYMMDD-NNN` 形式）で一意に識別する。

**例**:
- `20260301-001` → `tasks/index.jsonl` 内のエントリ + `tasks/20260301-001.md`

タスク情報の取得:
1. `tasks/index.jsonl` から `id` が一致する行を取得（サマリ情報）
2. `tasks/{id}.md` を読み込み（詳細・プロンプト等）

## タスクのステータスフロー

```
pending → needs_clarification → scoped → approved → running → done
  ↕              └→ done (manual)                          └→ failed
deferred
```

- `pending`: データソースから取り込まれた初期状態
- `deferred`: 精査を先送りしたタスク（pending からのみ遷移可、取り消しで pending に戻る）
- `needs_clarification`: 質問が生成されたが未回答の項目がある
- `scoped`: 前提条件・達成条件が明確で、実行プロンプトが生成済み
- `approved`: ユーザがプロンプトを承認し、実行待ち
- `running`: ディスパッチャーで実行中
- `done` / `failed`: 完了 / 失敗

## git 操作ポリシー

リポジトリへの変更を伴うすべての操作完了後に自動で以下を実行する:

```bash
cd ~/.local/share/my-tasks
git add .
git commit -m "{コミットメッセージ}"
git push
```

**注意**: `tasks/` は gitignore なので commit 対象外。

### コミットメッセージ規約

| 操作 | メッセージ例 |
|------|-------------|
| タスク収集 | `sync: update tasks from all datasources` |
| データソース追加 | `feat: add datasource jira` |
| プロジェクト追加 | `feat: add project ubs-mgmt-tool` |
| タスク操作 | `task: operate on 20260301-001` |
| 設定変更 | `config: update project bo` |
| リポジトリ初期化 | `init: initialize my-tasks repository` |

## fetch-all.sh の構造

各データソースの収集スクリプトを順に呼び出し、すべての結果を stdout に JSONL 形式で出力する。

```bash
#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

bash "$SCRIPT_DIR/fetch-jira.sh"
bash "$SCRIPT_DIR/fetch-ms-todo.sh"
```

新しいデータソースを追加した際はこのファイルに呼び出しを追記する。

## ディスパッチャー

Unix ドメインソケット C/S アーキテクチャのジョブランナー。
サーバは systemd user service として常駐し、asyncio でジョブキューと子プロセスを管理する。

### ソケットパス

`$XDG_RUNTIME_DIR/my-tasks-dispatch/dispatcher.sock`

### コマンド

| コマンド | 説明 |
|---------|------|
| `run` | ジョブを投入（タスク ID またはプロジェクト ID + プロンプト指定） |
| `open` | 対話的セッションを tmux で開く |
| `status` | 全ジョブのステータスを返す |
| `cancel` | キュー内ジョブを取消 |
| `kill` | 実行中ジョブを強制停止 |
| `kill-all` | 全ジョブを強制停止 |
| `wait` | ジョブ完了まで接続を保持 |

詳細は `dispatcher-design.md` を参照。

## サンドボックス

ジョブ実行時のプロセス隔離を提供する。プロファイルベースで構成を管理する。

| プロファイル | モード | ネットワーク | 用途 |
|-------------|--------|-------------|------|
| `default` | restricted | ai-ns namespace（制限付き） | 通常のコーディングタスク |
| `unrestricted` | unrestricted | ホストネットワーク直接 | ブラウザオートメーション、外部 API 連携 |

プロジェクト定義の `sandbox_profile` フィールドでプロファイルを指定する（デフォルト: `"default"`）。
モードはプロファイルの `proxy_profile` フィールドから自動決定される（設定あり → restricted、null → unrestricted）。

## sync-tasks.py の役割

タスク収集処理の中核を担う Python スクリプト。

**呼び出し方**:
```bash
# fetch-all.sh の出力を直接パイプ
~/.local/share/my-tasks/scripts/fetch-all.sh | python3 ~/.claude/skills/my-tasks/scripts/sync-tasks.py --repo ~/.local/share/my-tasks

# ファイルから読み込む場合
python3 ~/.claude/skills/my-tasks/scripts/sync-tasks.py --repo ~/.local/share/my-tasks --input /tmp/tasks.jsonl
```
