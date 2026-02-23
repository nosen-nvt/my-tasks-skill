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
├── datasources/           # データソース定義（1ファイル = 1データソース）
│   ├── jira.json
│   └── ms-todo.json
├── projects/              # プロジェクト定義（1ファイル = 1プロジェクト）
│   ├── project-a.json
│   └── project-b.json
├── tasks/                 # タスクストア（1ファイル = 1データソース）
│   ├── jira.json
│   └── ms-todo.json
├── daily/                 # 日次ゴール（1ファイル = 1日）
│   ├── 2026-02-18.json
│   └── ...
└── scripts/               # 収集スクリプト
    ├── fetch-all.sh        # 全データソース一括取得
    ├── fetch-jira.sh
    └── fetch-ms-todo.sh
```

## タスクの参照方法

タスクは `datasource_id/remote_id` の複合キーで参照する。

**形式**: `{datasource_id}/{remote_id}`

**例**:
- `jira/UBS-101` → `datasources/jira.json` の `datasource_id` + `tasks/jira.json` 内の `remote_id: "UBS-101"`
- `ms-todo/abc123` → `datasources/ms-todo.json` の `datasource_id` + `tasks/ms-todo.json` 内の `remote_id: "abc123"`

タスクの参照を解決する手順:
1. スラッシュで分割して `datasource_id` と `remote_id` を取得
2. `tasks/{datasource_id}.json` を読み込み
3. `tasks` 配列から `remote_id` が一致するエントリを取得

## git 操作ポリシー

リポジトリへの変更を伴うすべての操作完了後に自動で以下を実行する:

```bash
cd ~/.local/share/my-tasks
git add .
git commit -m "{コミットメッセージ}"
git push
```

### コミットメッセージ規約

| 操作 | メッセージ例 |
|------|-------------|
| タスク最新化 | `sync: update tasks from all datasources` |
| 日次ゴール設定 | `daily: set goals for 2026-02-18` |
| データソース追加 | `feat: add datasource jira` |
| プロジェクト追加 | `feat: add project ubs-mgmt-tool` |
| マイルストーン追加 | `feat: add milestone v1-release to ubs-mgmt-tool` |
| タスク操作 | `task: operate on jira/UBS-101` |
| リポジトリ初期化 | `init: initialize my-tasks repository` |

git は手軽な分散DBとして利用しており、コミット粒度を細かく意識する必要はない。

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

プロセス分散型ジョブランナー。各ジョブは独立した dispatcher プロセスとして tmux 上で並列実行される。
中央ループやデーモンは存在しない。

### 状態ディレクトリ

`$XDG_RUNTIME_DIR/my-tasks-dispatch/`（フォールバック: `/tmp/my-tasks-dispatch/`）

1ジョブ = 1状態ファイル（`{dispatch_id}.json`）。`flock` による排他制御でスロット管理を行う。

### シグナルファイル

各セッションのシグナルファイルは `working_dir` 内に配置される:
- `.dispatch-{dispatch_id}.done` — Claude がタスク完了時に作成する sentinel ファイル
- `.dispatch-{dispatch_id}.exit` — シェルが終了コードを書き込むファイル

詳細は `dispatcher-design.md` を参照。

## sync-tasks.py の役割

タスク最新化処理の中核を担う Python スクリプト。
詳細は `../scripts/sync-tasks.py` を参照。

**呼び出し方**:
```bash
# fetch-all.sh の出力を直接パイプ
~/.local/share/my-tasks/scripts/fetch-all.sh | python3 ~/.claude/skills/my-tasks/scripts/sync-tasks.py --repo ~/.local/share/my-tasks

# ファイルから読み込む場合
python3 ~/.claude/skills/my-tasks/scripts/sync-tasks.py --repo ~/.local/share/my-tasks --input /tmp/tasks.jsonl
```
