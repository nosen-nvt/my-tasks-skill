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
├── proxy-profiles/                  # git 管理
│   ├── dev.json
│   ├── full.json
│   └── office.json
├── sandbox-profiles/                # git 管理
│   ├── restricted-default.json
│   └── unrestricted-browser.json
├── tasks/                           # gitignored
│   ├── index.jsonl                  # タスクインデックス
│   └── {task_id}.yaml               # タスク実体（YAML）
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
- `20260301-001` → `tasks/index.jsonl` 内のエントリ + `tasks/20260301-001.yaml`

タスク情報の取得:
1. `tasks/index.jsonl` から `id` が一致する行を取得（サマリ情報）
2. `tasks/{id}.yaml` を読み込み（詳細・プロンプト等）

## ステータスモデル

タスクのステータスはタスク YAML が唯一の真実の源（Single Source of Truth）。

### タスクステータス

```
pending → in_progress → done
                      → aborted
```

- `pending`: データソースから取り込まれた初期状態、または Plan/Dispatch 待ち
- `in_progress`: ジョブ実行中、Plan 中、Feedback 待ちなど、作業が進行中の状態
- `done`: 完了
- `aborted`: 中止

### 再オープン

done タスクと同じ `remote_id` がデータソースから再出現した場合、新規タスクとして作成する。

### ジョブステータス（ディスパッチャー側）

ディスパッチャーはジョブの実行状態をインメモリで管理する。タスク管理の知識を持たない。

```
queued → running → done
                 → failed
```

### タスクとジョブの連携

| イベント | タスクステータス遷移 |
|---|---|
| Dispatch 時（スキル側） | `pending` → `in_progress` |
| ジョブ完了後（スキル側が判断） | `in_progress` → `done` or `aborted` |

タスクステータスの管理は全てスキル側が行う。ディスパッチャーはジョブの実行と完了通知のみを担う。

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

Unix ドメインソケット C/S アーキテクチャの汎用ジョブランナー。
サーバは systemd user service として常駐し、asyncio でジョブキューと子プロセスを管理する。
プロンプト構築はスキル側が行い、ディスパッチャーは実行環境（sandbox / worktree）の構築とジョブ実行を担う。

### ソケットパス

- `$XDG_RUNTIME_DIR/my-tasks-dispatch/dispatcher.sock` — ジョブ管理
- `$XDG_RUNTIME_DIR/my-tasks-dispatch/host-cmd-broker.sock` — ホストコマンドブローカー（認証情報取得含む）

### コマンド

| コマンド | 説明 |
|---------|------|
| `run` | プロンプトを受け取りジョブを実行（session_id 指定可） |
| `resume` | 完了済みセッションを tmux で再開（session_id 指定） |
| `open` | 対話的セッションを tmux で開く（worktree 指定可） |
| `status` | 全ジョブのステータスを返す |
| `cancel` | キュー内ジョブを取消 |
| `kill` | 実行中ジョブを強制停止 |
| `kill-all` | 全ジョブを強制停止 |
| `wait` | ジョブ完了まで接続を保持 |
| `log` | ジョブログを表示（CLI のみ） |

詳細は `dispatcher-design.md` を参照。

## サンドボックス

ジョブ実行時のプロセス隔離を提供する。プロファイルベースで構成を管理する。
sandbox は純粋なプロセス隔離ツールであり、何を実行するかは呼び出し側が決める。

```bash
# 実行するコマンドを明示的に指定
sandbox --sandbox-profile restricted-default --working-dir /path/to/repo -- claude -p "..."
sandbox --sandbox-profile restricted-default --working-dir /path/to/repo -- bash -c "npm test"
```

| プロファイル | ネットワーク保護 | ネットワーク | 用途 |
|-------------|---------------|-------------|------|
| `default` | あり | ai-ns namespace + proxy 経由 | 通常のコーディングタスク |
| `unrestricted` | なし | ホストネットワーク直接 | ブラウザオートメーション、外部 API 連携 |

プロジェクト定義の `sandbox_profile` フィールドでプロファイルを指定する（デフォルト: `"default"`）。
ネットワーク保護の有無はプロファイルの `proxy_profile` フィールドから自動決定される（設定あり → 保護あり、null → 保護なし）。

## sync-tasks.py の役割

タスク収集処理の中核を担う Python スクリプト。

**呼び出し方**:
```bash
# fetch-all.sh の出力を直接パイプ
~/.local/share/my-tasks/scripts/fetch-all.sh | python3 ~/.claude/skills/my-tasks/scripts/sync-tasks.py --repo ~/.local/share/my-tasks

# ファイルから読み込む場合
python3 ~/.claude/skills/my-tasks/scripts/sync-tasks.py --repo ~/.local/share/my-tasks --input /tmp/tasks.jsonl
```

## オーケストレーション

タスクに対する操作は以下の4つで構成される。全てスキル側（対話セッション or ダッシュボード）が起点。

| 操作 | 説明 | 起点 |
|------|------|------|
| Plan | 対話セッションでタスクを精査・計画 | ダッシュボード → `open` |
| Dispatch | `execute_prompt` をディスパッチャーの `run` に渡す | ダッシュボード or スキル |
| Resume | 完了済みセッションを再開し軽微な修正 | ダッシュボード → `resume` |
| Feedback | フィードバック収集 → 対応ジョブ Dispatch | ダッシュボード or スキル |
