# ディスパッチャー設計ドキュメント

## 概要

Unix ドメインソケット C/S アーキテクチャのジョブランナー。
サーバは systemd user service として常駐し、asyncio でジョブキューと子プロセスを管理する。
クライアントはソケット経由でジョブ投入・状態確認・制御コマンドを送信する。

## アーキテクチャ

```
┌─ ホスト ──────────────────────────────────────────────┐
│                                                        │
│  systemd user service                                  │
│  └─ dispatcher server                               │
│       ├─ asyncio.start_unix_server(SOCKET_PATH)        │
│       ├─ ジョブキュー管理（max_slots）                  │
│       ├─ fork: sandbox claude -p "..." (job 1)         │
│       ├─ fork: sandbox claude -p "..." (job 2)         │
│       └─ wait: 完了検知 → ステータス更新               │
│                                                        │
│  SOCKET_PATH = $XDG_RUNTIME_DIR/                       │
│                my-tasks-dispatch/dispatcher.sock        │
│       │                                                │
│       │ bind mount                                     │
│  ┌─ サンドボックス ──────────────────────────────────┐ │
│  │    │                                              │ │
│  │  Claude Code (スキル実行中)                       │ │
│  │    └─ dispatcher run --project bo             │ │
│  │        └─ connect(SOCKET_PATH) → ホストへ到達     │ │
│  └───────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────┘
```

## プロトコル

JSON over Unix ドメインソケット。改行区切り（1行1メッセージ）。

### リクエスト

| コマンド | フィールド | 説明 |
|---------|-----------|------|
| `dispatch` | `project_id?`, `prompt?`, `context?`, `max_runs?`, `lifecycle_id?` | ライフサイクルを開始（計画→実行→評価を自動制御） |
| `resume` | `lifecycle_id`, `project_id?`, `context_update?` | suspend 中のライフサイクルを再開 |
| `run` | `project_id`, `prompt`, `job_type?`, `sandbox_profile?` | ジョブを実行（sandbox_profile はプロジェクト設定から自動解決、上書き可） |
| `open` | `project_id`, `session?`, `sandbox_profile?` | 対話的セッションを tmux で開く |
| `status` | | 全ジョブ + ライフサイクルのステータスを返す |
| `cancel` | `dispatch_id` | キュー内ジョブを取消 |
| `kill` | `dispatch_id` | 実行中ジョブを強制停止 |
| `kill-all` | | 全ジョブを強制停止 |
| `wait` | `dispatch_id` | ジョブ完了まで接続を保持 |
| `log` | (CLI のみ) | ジョブの stdout/stderr ログを表示（ファイル直接読み取り） |

```json
{"command": "run", "project_id": "ubs-mgmt-tool", "prompt": "...", "job_type": "execute"}
{"command": "run", "project_id": "bo", "prompt": "バグを修正してください"}
{"command": "run", "project_id": "bo", "prompt": "...", "job_type": "plan", "sandbox_profile": "unrestricted"}
{"command": "open", "project_id": "ubs-mgmt-tool", "session": "main"}
{"command": "dispatch", "project_id": "bo", "prompt": "タスクタイトル", "max_runs": 3}
{"command": "resume", "lifecycle_id": "lc-1", "context_update": "更新されたコンテキスト..."}
{"command": "status"}
{"command": "cancel", "dispatch_id": "ubs-mgmt-tool-1"}
{"command": "kill", "dispatch_id": "ubs-mgmt-tool-1"}
{"command": "kill-all"}
{"command": "wait", "dispatch_id": "ubs-mgmt-tool-1"}
```

### レスポンス

```json
{"ok": true, "dispatch_id": "ubs-mgmt-tool-1", "message": "Job started"}
{"ok": true, "dispatch_id": "ubs-mgmt-tool-2", "message": "Job queued (slot full)"}
{"ok": true, "jobs": [
  {"dispatch_id": "ubs-mgmt-tool-1", "status": "running", "pid": 12345},
  {"dispatch_id": "ubs-mgmt-tool-2", "status": "queued", "pid": null}
]}
{"ok": false, "error": "Unknown dispatch_id: xyz"}
```

## サーバ (`DispatchServer`)

### ジョブライフサイクル

```
queued ──→ running ──→ done
                   └──→ failed
```

### インメモリ状態管理

サーバはジョブ状態をインメモリで管理する。永続化は行わない。
サーバ再起動時にジョブ状態は失われるが、以下の理由で許容可能:

- 実行中のジョブ（子プロセス）はサーバ再起動時に SIGTERM で終了する
- 過去のジョブ履歴は不要（完了したタスクは index.jsonl の status で管理）
- systemd の `Restart=on-failure` で自動復旧

### ジョブ実行フロー

1. `cmd_run`: リクエスト受信
2. `dispatch_id` を生成（`{project_id}-{連番}`）
3. スロットに空きがあり、かつ同一プロジェクトのジョブが実行中でなければ `execute_job()` で即実行
4. スロット満杯、または同一プロジェクトが実行中なら queue に追加し `queued` で応答（理由: `slot full` or `project busy`）
5. `execute_job()`:
   - プロジェクト設定から `sandbox_profile`（デフォルト: `"default"`）と `working_directory` を取得
   - `host_commands` が設定されている場合、`uuid4().hex` でトークン生成 → `HostCommandBroker.register(token, host_commands)`
   - `build_system_prompt()` でシステムプロンプトを構築
   - `$XDG_RUNTIME_DIR/my-tasks-dispatch/{dispatch_id}.log` にログファイルを作成
   - `sandbox_exec.build_exec_args()` で bwrap/netns コマンド引数を構築し、`asyncio.create_subprocess_exec()` でジョブ実行（stdout/stderr をログファイルに出力）
   - `proc.wait()` で完了検知
   - `done` or `failed` に更新
   - トークンを revoke
   - waiter に通知
   - Lifecycle 経由のジョブの場合 `lifecycle_mgr.on_job_complete()` を呼出
   - `drain_queue()` で次のジョブを起動

### `open` コマンド

対話的セッションは引き続き tmux を使用する。ジョブ管理の対象外。

1. クライアントから `open` コマンドを受信
2. `projects/{project_id}.json` から `sandbox_profile`（デフォルト: `"default"`）と `working_directory` を取得
3. 指定された tmux セッション（またはデフォルトセッション）にウィンドウを作成
4. ウィンドウ内で `sandbox --sandbox-profile '{profile}'{env_file_args} -- claude --permission-mode bypassPermissions` を実行
5. ウィンドウ名: `{project_id}`

### `log` コマンド

ジョブの stdout/stderr ログを表示する。サーバ通信不要のクライアント専用コマンド。

- ログファイルパス: `$XDG_RUNTIME_DIR/my-tasks-dispatch/{dispatch_id}.log`
- ジョブ実行時に stdout/stderr が同一ファイルに書き出される
- tmpfs 上のため OS 再起動で自動クリーンアップされる

### `wait` コマンド

ジョブ完了までソケット接続を保持する。

1. `dispatch_id` に対応する `asyncio.Future` を作成
2. ジョブが既に完了していれば即座にレスポンスを返す
3. 未完了の場合、Future の完了を待ってからレスポンスを返す

### Lifecycle ステートマシン

`dispatch` コマンドで開始される Lifecycle がタスクの全ライフサイクルを自動制御する。

#### データモデル

- `Lifecycle` dataclass: lifecycle_id, project_id, prompt, context_path, status, suspend_reason, phases (list[dict]), current_phase (int), run_count, max_runs, current_dispatch_id, timestamps
- 永続化: `$XDG_RUNTIME_DIR/my-tasks-dispatch/lifecycles.jsonl` (状態変更のたびに全件書き出し)
- Job に `lifecycle_id` フィールドを追加（Lifecycle 経由のジョブを識別）

#### コマンド

| コマンド | フィールド | 説明 |
|---------|-----------|------|
| `dispatch` | `project_id?`, `prompt?`, `context?`, `max_runs?`, `lifecycle_id?` | ライフサイクル開始。計画→承認→実行→評価を自動制御 |
| `resume` | `lifecycle_id`, `project_id?`, `context_update?` | suspend 中のライフサイクルを再開 |

#### ステータス値

`planning`, `planned`, `phase_executing`, `phase_evaluating`, `suspend`, `done`, `aborted`

#### ステートマシンフロー

```
dispatch → planning → 計画ジョブ
                        ├── planned + auto_approve → phase_executing → 実行ジョブ → phase_evaluating → 評価ジョブ
                        │                                                              ├── DONE → done
                        │                                                              ├── NEXT_PHASE → phase_executing（次フェーズ）
                        │                                                              ├── SUSPEND → suspend (agent_review)
                        │                                                              └── ABORT → aborted
                        ├── planned + 手動承認 → suspend (approval_required)
                        └── needs_input → suspend (needs_input)
```

#### `resume` のルーティング

| `suspend_reason` | 動作 |
|---|---|
| `needs_input` | コンテキスト YAML のフェーズ進捗を確認し分岐（下記参照） |
| `approval_required` | status → `phase_executing`、`dispatch_execute()` で実行を開始 |
| `agent_review` | status → `planning`、`dispatch_plan()` で計画を再実行 |
| `project_confirmation` | `project_id` を更新（指定時）、status → `planning`、`dispatch_plan()` で計画を再実行 |

#### `needs_input` からの resume（フェーズ進捗対応）

対話セッション中に質問応答だけでなく作業自体が進行するケース（特に事務作業）に対応する。

| コンテキスト状態 | 動作 |
|---|---|
| フェーズなし or 進捗なし | 通常の reclarify フロー（`PLAN_RECLARIFY_TEMPLATE`） |
| 全フェーズ完了 | `done` に遷移（タスク完了） |
| 一部フェーズ完了 | `PLAN_RESUME_PROGRESS_TEMPLATE` で残フェーズの計画を生成。完了済みフェーズは保持し、`current_phase` を最初の pending に設定 |

#### ランタイムファイル

| ファイル | パス | 説明 |
|---|---|---|
| ジョブログ | `$XDG_RUNTIME_DIR/my-tasks-dispatch/{dispatch_id}.log` | stdout/stderr 出力 |
| 結果 JSON | `$XDG_RUNTIME_DIR/my-tasks-dispatch/{dispatch_id}.result.json` | ジョブ出力の構造化結果 |
| コンテキスト | `$XDG_RUNTIME_DIR/my-tasks-dispatch/{lifecycle_id}.context.yaml` | タスク YAML（dispatch 時に作成、計画ジョブが更新） |
| Lifecycle 状態 | `$XDG_RUNTIME_DIR/my-tasks-dispatch/lifecycles.jsonl` | 全 Lifecycle の永続化（状態変更のたびに全件書き出し） |

Lifecycle ステートマシンが結果 JSON を読んで次の状態を決定する。

#### LLM プロジェクト判定

`dispatch` 時に `project_id` 未指定の場合、`claude` CLI でプロジェクトを自動判定する。

### SIGTERM ハンドラ

全子プロセスに SIGTERM を送信してからサーバを終了する。

### ログ

- 標準出力にログを出力（systemd の journal に自動収集される）
- ログ形式: `{timestamp} {level} {message}`

### 定期クリーンアップ

バックグラウンドタスクとして 30 分間隔で実行。完了済み Lifecycle に関連する古いファイル（ログ、結果 JSON、コンテキスト YAML）を削除する。

- 対象: `$XDG_RUNTIME_DIR/my-tasks-dispatch/` 配下の `.log`, `.result.json`, `.context.yaml`
- 条件: 最終更新から 6 時間以上経過 かつ 対応する Lifecycle が `done` 状態
- コンテキストファイルはアクティブな Lifecycle のものはスキップ

## クライアント

### サーバ接続

```python
async def client_send(request: dict) -> dict:
    try:
        reader, writer = await asyncio.open_unix_connection(SOCKET_PATH)
    except (ConnectionRefusedError, FileNotFoundError):
        if is_inside_sandbox():
            raise RuntimeError("Dispatch server is not running on the host.")
        else:
            start_server_background()
            reader, writer = await retry_connect()
    # ...
```

サンドボックス内でサーバが未起動の場合はエラーを返す。
ホスト環境ではサーバをバックグラウンド起動してリトライする。

### CLI コマンド

```bash
# ライフサイクル開始（推奨）
dispatcher dispatch --project bo --prompt "タスクタイトル" --context-file /path/to/task.md
dispatcher dispatch --project bo --prompt "タスクタイトル" --context-file /path/to/task.md --lifecycle-id "20260312-001-g1"
dispatcher dispatch --project bo --prompt "バグを修正して"

# ライフサイクル再開
dispatcher resume --id lc-1
dispatcher resume --id lc-1 --context-file /path/to/updated-task.md
dispatcher resume --id lc-1 --project new-project-id

# ジョブを直接投入（プロンプトは stdin）
echo "バグを修正して" | dispatcher run --project bo [--job-type execute] [--sandbox-profile unrestricted]

# 対話的セッション
dispatcher open --project ubs-mgmt-tool [--session main] [--sandbox-profile unrestricted]

# ステータス確認
dispatcher status [--json]

# ジョブ制御
dispatcher cancel --id ubs-mgmt-tool-1
dispatcher kill --id ubs-mgmt-tool-1
dispatcher kill --all

# ジョブ完了待機
dispatcher wait --id ubs-mgmt-tool-1

# ジョブの stdout/stderr ログを表示
dispatcher log --id ubs-mgmt-tool-1

# サーバ起動（通常は systemd 経由）
dispatcher server [--max-slots 8] [--repo ~/.local/share/my-tasks]
```

### `run` の処理

1. stdin からプロンプトを読み取り
2. サーバに `run` コマンドを送信（`--project` 必須）

## tmux セッション決定

`open` コマンド時に使用する tmux セッションの決定優先順位:

1. `--session` 引数で明示指定（存在確認あり）
2. `$TMUX` 環境変数から自動検出した呼び出し元セッション
3. `tmux list-clients` から非デフォルトのクライアントセッションを検出
4. フォールバック: `dispatch` セッションを新規作成

## エラーハンドリング

| エラー | 挙動 |
|---|---|
| プロジェクト不存在 | エラーレスポンス |
| working_directory 未設定 | エラーレスポンス |
| working_directory がファイルシステムに存在しない | エラーレスポンス |
| プロンプトが空 | エラーレスポンス |
| 不明な dispatch_id | エラーレスポンス |
| サーバ未起動（サンドボックス内） | エラー終了 |
| サーバ未起動（ホスト） | バックグラウンド起動してリトライ |

## 追加システムプロンプト

ジョブ実行時に `--append-system-prompt` で注入する情報:

```
あなたはサンドボックス環境で実行されています。

実行環境:
- 作業ディレクトリ: {working_directory}
- ネットワーク: {保護あり (netns + proxy)|ホストネットワーク直接}

制約事項 (ネットワーク保護あり):
- ネットワーク: GitHub/Bitbucket SSH と HTTP プロキシ経由の HTTPS のみ利用可能
- ファイル: 作業ディレクトリ内のファイルのみ変更可能

認証情報:
- `pass show <entry>` で以下の認証情報を取得できます:
  - {host_commands の pass コマンドの allowed_patterns から抽出}
（allowed_patterns が "*" の場合: 「全ての認証情報を取得できます」と表示）

ホストコマンド:
- 以下のコマンドがホスト側で実行されます:
  - {host_commands の pass 以外のコマンド一覧}

結果ファイル:
ジョブ完了時、以下のパスに結果 JSON を書き出してください:
  {result_path}

計画ジョブの結果フォーマット:
  {"next_status": "planned"} — 計画完了、実行可能
  {"next_status": "needs_input"} — ユーザへの質問あり

評価ジョブの結果フォーマット:
  {"verdict": "DONE", "phase_summary": "..."} — 全フェーズ完了
  {"verdict": "NEXT_PHASE", "phase_summary": "..."} — 次フェーズへ進行
  {"verdict": "SUSPEND", "phase_summary": "..."} — エージェントレビューが必要
  {"verdict": "ABORT", "phase_summary": "..."} — 実行不可能

作業が完了したら、変更をコミットしてください。
プロセスの終了がジョブ完了の通知になります（シグナルファイルは不要です）。
```

- 認証情報セクションは `host_commands` に `pass` コマンドが含まれる場合のみ追加される
- ホストコマンドセクションは `host_commands` に `pass` 以外のコマンドが含まれる場合のみ追加される
- 結果ファイルセクションは `job_type` が `plan` または `evaluate` の場合のみ追加される
- `execute` ジョブには結果ファイルセクションは付与されない

## Host Command Broker

### 概要

サンドボックス内のジョブがホスト側の特定コマンドを安全に実行するための汎用ブローカー。
`pass show` による認証情報取得もこの仕組みで実現する（旧 Credential Broker を統合済み）。

### アーキテクチャ

```
┌─ ホスト ──────────────────────────────────────────────┐
│                                                        │
│  dispatcher server                                     │
│    ├─ DispatchServer       (dispatcher.sock)           │
│    └─ HostCommandBroker    (host-cmd-broker.sock)      │
│         ├─ token registry: {token → [host_commands]}   │
│         └─ ホワイトリスト + パターンマッチで実行       │
│                                                        │
│  ┌─ サンドボックス ──────────────────────────────────┐ │
│  │                                                    │ │
│  │  pass show <entry>                                 │ │
│  │    → /usr/bin/pass (host-cmd シム)                 │ │
│  │      → connect(host-cmd-broker.sock)               │ │
│  │        → HostCommandBroker (ホスト側)              │ │
│  │          → /usr/bin/pass show <entry> (実体)       │ │
│  │                                                    │ │
│  │  az pipelines run ...                              │ │
│  │    → /usr/bin/az (host-cmd シム)                   │ │
│  │      → connect(host-cmd-broker.sock)               │ │
│  │        → HostCommandBroker (ホスト側)              │ │
│  │          → /usr/bin/az pipelines run ... (実体)    │ │
│  │                                                    │ │
│  └────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────┘
```

### ソケットパス

`$XDG_RUNTIME_DIR/my-tasks-dispatch/host-cmd-broker.sock`

dispatcher.sock と同じディレクトリに配置。sandbox で既に bind-mount 済みのため追加設定不要。

### host-cmd シム（busybox パターン）

サンドボックス内の `/usr/bin/{name}` に `scripts/host-cmd` を `--ro-bind` する。
`host-cmd` は `argv[0]` のベースネームでコマンド名を判別し、ブローカーに転送する。

```
/usr/bin/pass  →  host-cmd (argv[0]="pass")  →  broker に {"command": "pass", "args": [...]}
/usr/bin/az    →  host-cmd (argv[0]="az")    →  broker に {"command": "az", "args": [...]}
```

### JSON プロトコル

改行区切りの JSON line。

**リクエスト:**

```json
{"token": "<job-token>", "command": "pass", "args": ["show", "jira/api-token"]}
{"token": "<job-token>", "command": "az", "args": ["pipelines", "run", "--name", "build"]}
{"token": "<job-token>", "command": "pass", "args": ["insert", "-m", "entry"], "stdin": "secret-value"}
```

**レスポンス（成功）:**

```json
{"ok": true, "exit_code": 0, "stdout": "<output>", "stderr": ""}
```

**レスポンス（エラー）:**

```json
{"ok": false, "error": "command not allowed: foo"}
{"ok": false, "error": "args not allowed: az pipelines delete build"}
{"ok": false, "error": "invalid token"}
{"ok": false, "error": "stdin not allowed for az"}
{"ok": false, "error": "execution failed"}
```

### ライフサイクル

1. **ジョブ開始**: `host_commands` が設定されている場合、`uuid4().hex` でトークン生成 → `HostCommandBroker.register(token, host_commands)`
2. **ジョブ実行中**: 環境変数 `HOST_CMD_TOKEN` と `HOST_CMD_BROKER_SOCK` がサンドボックスに渡される
3. **コマンド実行**: サンドボックス内で `pass show <entry>` → host-cmd シム → broker socket → `/usr/bin/pass show <entry>` (ホスト側)
4. **ジョブ終了**: `HostCommandBroker.revoke(token)` でトークン無効化

### ホストコマンド定義

サンドボックスプロファイルとプロジェクト定義の `host_commands` フィールドで、ジョブが使用可能なコマンドを定義する。両者はマージされる（プロファイル + プロジェクト）。

```json
{
  "profile_id": "restricted-default",
  "proxy_profile": "dev",
  "host_commands": [
    {"name": "pass", "path": "/usr/bin/pass", "allowed_patterns": "*", "allow_stdin": true}
  ]
}
```

```json
{
  "project_id": "ubs-mgmt-tool",
  "host_commands": [
    {"name": "az", "path": "/usr/bin/az", "allowed_patterns": ["pipelines run *", "pipelines runs show *"]}
  ]
}
```

### 組み込み Host Command Broker

`sandbox` CLI から直接実行する場合（dispatcher 経由でない場合）、`HOST_CMD_TOKEN` 環境変数が未設定なら
`EmbeddedHostCommandBroker` が自動起動し、同一プロセス内でブローカーを提供する。

### セキュリティ特性

- **コマンドホワイトリスト**: 各ジョブは `host_commands` に列挙されたコマンドのみ実行可能
- **引数パターンマッチ**: `allowed_patterns` で許可する引数パターンを fnmatch で制御（`"*"` で全許可）
- **stdin 制御**: `allow_stdin: true` が明示されたコマンドのみ stdin を受け付ける
- **トークン有効期限**: ジョブ終了時に自動 revoke
- **並行安全**: 各ジョブが固有トークンを持つため、複数ジョブ並行でも問題なし
- **ホスト側影響なし**: host-cmd シムは bwrap の `--ro-bind` でサンドボックス内のみに適用
- **ログ**: トークンは先頭 8 文字のみ記録（`token[:8]...`）
