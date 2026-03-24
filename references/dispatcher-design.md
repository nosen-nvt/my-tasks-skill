# ディスパッチャー設計ドキュメント

## 概要

Unix ドメインソケット C/S アーキテクチャの汎用ジョブランナー。
サーバは systemd user service として常駐し、asyncio でジョブキューと子プロセスを管理する。
クライアントはソケット経由でジョブ投入・状態確認・制御コマンドを送信する。

### 責務

- ジョブの実行管理（queued → running → done/failed）
- sandbox 環境の構築（プロファイル解決、bwrap 引数構築）
- worktree の管理（作成・クリーンアップ）
- host command broker のトークン管理
- `claude` コマンドの組み立てと実行
- 対話セッションの tmux 管理

### 責務外

- プロンプトの構築（スキル側）
- タスクのステータス管理（スキル側）
- 実行結果の評価（廃止。hooks または外部エージェントで対応）

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
| `run` | `project_id`, `prompt`(stdin/file), `session_id?`, `branch?` | ジョブを実行 |
| `resume` | `project_id`, `session_id` | 完了済みセッションを tmux で再開 |
| `open` | `project_id`, `session?`, `worktree?` | 対話セッションを tmux で開く |
| `status` | | 全ジョブのステータスを返す |
| `cancel` | `dispatch_id` | キュー内ジョブを取消 |
| `kill` | `dispatch_id` | 実行中ジョブを強制停止 |
| `kill-all` | | 全ジョブを強制停止 |
| `wait` | `dispatch_id` | ジョブ完了まで接続を保持 |
| `log` | (CLI のみ) | ジョブの stdout/stderr ログを表示（ファイル直接読み取り） |

```json
{"command": "run", "project_id": "bo", "prompt": "バグを修正してください", "session_id": "a1b2c3d4-..."}
{"command": "run", "project_id": "bo", "prompt_file": "/path/to/prompt.md"}
{"command": "resume", "project_id": "bo", "session_id": "a1b2c3d4-..."}
{"command": "open", "project_id": "ubs-mgmt-tool", "session": "main"}
{"command": "open", "project_id": "bo", "worktree": "/path/to/worktree"}
{"command": "status"}
{"command": "cancel", "dispatch_id": "bo-1"}
{"command": "kill", "dispatch_id": "bo-1"}
{"command": "kill-all"}
{"command": "wait", "dispatch_id": "bo-1"}
```

### レスポンス

```json
{"ok": true, "dispatch_id": "bo-1", "session_id": "a1b2c3d4-...", "message": "Job started"}
{"ok": true, "dispatch_id": "bo-2", "message": "Job queued (slot full)"}
{"ok": true, "jobs": [
  {"dispatch_id": "bo-1", "project_id": "bo", "status": "running", "pid": 12345, "working_dir": "/tmp/wt/bo-1", "branch": "task/20260301-001"},
  {"dispatch_id": "bo-2", "project_id": "bo", "status": "queued", "pid": null}
]}
{"ok": false, "error": "Unknown dispatch_id: xyz"}
```

## サーバ (`DispatchServer`)

### ジョブステータス

```
queued → running → done
                 → failed
```

### インメモリ状態管理

サーバはジョブ状態をインメモリで管理する。永続化は行わない。
サーバ再起動時にジョブ状態は失われるが、以下の理由で許容可能:

- 実行中のジョブ（子プロセス）はサーバ再起動時に SIGTERM で終了する
- 過去のジョブ履歴は不要（完了したタスクは index.jsonl の status で管理）
- systemd の `Restart=on-failure` で自動復旧

### Job データモデル

| フィールド | 型 | 説明 |
|---|---|---|
| `dispatch_id` | string | `{project_id}-{連番}` |
| `project_id` | string | プロジェクト ID |
| `status` | string | `queued` / `running` / `done` / `failed` |
| `pid` | int? | 子プロセス PID |
| `exit_code` | int? | 終了コード |
| `working_dir` | string | 作業ディレクトリ（worktree パス） |
| `branch` | string? | worktree のブランチ名 |
| `started_at` | string? | 開始日時 |
| `finished_at` | string? | 終了日時 |

### `run` の実行フロー

1. リクエスト受信（project_id + prompt + session_id?）
2. プロジェクト設定から `sandbox_profile`、`working_directory`、`host_commands` 等を解決
3. `dispatch_id` を生成（`{project_id}-{連番}`）
4. `session_id` が未指定の場合は UUID を生成
5. スロットに空きがあり、かつ同一プロジェクトのジョブが実行中でなければ即実行、そうでなければ queue に追加
6. worktree を作成（git リポジトリの場合）
7. `host_commands` が設定されている場合、`uuid4().hex` でトークン生成 → `HostCommandBroker.register(token, host_commands)`
8. 環境情報の system prompt を構築（sandbox 制約、認証情報、ホストコマンド等）
9. `$XDG_RUNTIME_DIR/my-tasks-dispatch/{dispatch_id}.log` にログファイルを作成
10. `sandbox ... -- claude -p "{prompt}" --session-id "{session_id}" --append-system-prompt "{環境情報}"` を subprocess で実行
11. 完了時に exit code + session_id を返却
12. トークンを revoke
13. waiter に通知
14. `drain_queue()` で次のジョブを起動

### プロンプトの受け渡し

プロンプトが大きくなる可能性があるため、標準入力とファイル渡しの2方式をサポートする。

```bash
# 標準入力
echo "{prompt}" | dispatcher run --project bo

# ファイル
dispatcher run --project bo --prompt-file /path/to/prompt.md

# session_id を指定（Resume 用のセッション追跡）
echo "{prompt}" | dispatcher run --project bo --session-id "a1b2c3d4-..."

# branch を指定
echo "{prompt}" | dispatcher run --project bo --branch "task/20260301-001"
```

### `resume` の実行フロー

1. リクエスト受信（project_id + session_id）
2. プロジェクト設定から sandbox_profile 等を解決
3. tmux セッションにウィンドウを作成
4. `sandbox ... -- claude --resume "{session_id}"` を実行（対話モード）

### `open` の実行フロー

1. リクエスト受信（project_id + session? + worktree?）
2. プロジェクト設定から sandbox_profile 等を解決
3. tmux セッションにウィンドウを作成
4. worktree 指定がある場合はそのパスを working_directory として使用
5. `sandbox ... -- claude` を実行

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

### SIGTERM ハンドラ

全子プロセスに SIGTERM を送信してからサーバを終了する。

### ログ

- 標準出力にログを出力（systemd の journal に自動収集される）
- ログ形式: `{timestamp} {level} {message}`

### 定期クリーンアップ

バックグラウンドタスクとして 30 分間隔で実行。古いログファイルを削除する。

- 対象: `$XDG_RUNTIME_DIR/my-tasks-dispatch/` 配下の `.log` ファイル
- 条件: 最終更新から 6 時間以上経過

### ランタイムファイル

| ファイル | パス | 説明 |
|---|---|---|
| ジョブログ | `$XDG_RUNTIME_DIR/my-tasks-dispatch/{dispatch_id}.log` | stdout/stderr 出力 |

## 追加システムプロンプト

ジョブ実行時に `--append-system-prompt` で注入する環境情報:

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

作業が完了したら、変更をコミットしてください。
```

- 認証情報セクションは `host_commands` に `pass` コマンドが含まれる場合のみ追加される
- ホストコマンドセクションは `host_commands` に `pass` 以外のコマンドが含まれる場合のみ追加される

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
# ジョブ実行（プロンプトは stdin）
echo "バグを修正して" | dispatcher run --project bo
echo "バグを修正して" | dispatcher run --project bo --session-id "a1b2c3d4-..."

# ジョブ実行（プロンプトはファイル）
dispatcher run --project bo --prompt-file /path/to/prompt.md

# ジョブ実行（ブランチ指定）
echo "バグを修正して" | dispatcher run --project bo --branch "task/20260301-001"

# セッション再開（tmux で対話モード）
dispatcher resume --project bo --session-id "a1b2c3d4-..."

# 対話的セッション
dispatcher open --project ubs-mgmt-tool [--session main]
dispatcher open --project bo --worktree /path/to/worktree

# ステータス確認
dispatcher status [--json]

# ジョブ制御
dispatcher cancel --id bo-1
dispatcher kill --id bo-1
dispatcher kill --all

# ジョブ完了待機
dispatcher wait --id bo-1

# ジョブの stdout/stderr ログを表示
dispatcher log --id bo-1

# サーバ起動（通常は systemd 経由）
dispatcher server [--max-slots 8] [--repo ~/.local/share/my-tasks]
```

## tmux セッション決定

`open` / `resume` コマンド時に使用する tmux セッションの決定優先順位:

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

## Host Command Broker

### 概要

サンドボックス内のジョブがホスト側の特定コマンドを安全に実行するための汎用ブローカー。
`pass show` による認証情報取得もこの仕組みで実現する。

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

### トークンライフサイクル

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
