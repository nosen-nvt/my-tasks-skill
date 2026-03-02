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
│  └─ dispatcher.py server                               │
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
│  │    └─ dispatcher.py run --task 20260301-001       │ │
│  │        └─ connect(SOCKET_PATH) → ホストへ到達     │ │
│  └───────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────┘
```

## プロトコル

JSON over Unix ドメインソケット。改行区切り（1行1メッセージ）。

### リクエスト

| コマンド | フィールド | 説明 |
|---------|-----------|------|
| `run` | `task_id?`, `project_id`, `prompt` | ジョブを実行（sandbox_mode はプロジェクト設定から自動解決） |
| `open` | `project_id`, `session?` | 対話的セッションを tmux で開く |
| `status` | | 全ジョブのステータスを返す |
| `cancel` | `dispatch_id` | キュー内ジョブを取消 |
| `kill` | `dispatch_id` | 実行中ジョブを強制停止 |
| `kill-all` | | 全ジョブを強制停止 |
| `wait` | `dispatch_id` | ジョブ完了まで接続を保持 |
| `log` | (CLI のみ) | ジョブの stdout/stderr ログを表示（ファイル直接読み取り） |

```json
{"command": "run", "task_id": "20260301-001", "project_id": "ubs-mgmt-tool", "prompt": "..."}
{"command": "run", "project_id": "bo", "prompt": "バグを修正してください"}
{"command": "open", "project_id": "ubs-mgmt-tool", "session": "main"}
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
  {"dispatch_id": "ubs-mgmt-tool-1", "task_id": "20260301-001", "status": "running", "pid": 12345},
  {"dispatch_id": "ubs-mgmt-tool-2", "task_id": "20260301-003", "status": "queued", "pid": null}
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
3. スロットに空きがあれば `execute_job()` で即実行
4. スロット満杯なら queue に追加し `queued` で応答
5. `execute_job()`:
   - プロジェクト設定から `sandbox_mode` と `working_directory` を取得
   - `$XDG_RUNTIME_DIR/my-tasks-dispatch/{dispatch_id}.log` にログファイルを作成
   - `asyncio.create_subprocess_exec("sandbox", "--mode", mode, "claude", "-p", prompt, ...)` でジョブ実行（stdout/stderr をログファイルに出力）
   - `proc.wait()` で完了検知
   - `done` or `failed` に更新
   - waiter に通知
   - `drain_queue()` で次のジョブを起動

### `open` コマンド

対話的セッションは引き続き tmux を使用する。ジョブ管理の対象外。

1. クライアントから `open` コマンドを受信
2. `projects/{project_id}.json` から `sandbox_mode` と `working_directory` を取得
3. 指定された tmux セッション（またはデフォルトセッション）にウィンドウを作成
4. ウィンドウ内で `sandbox --mode {sandbox_mode} claude --permission-mode bypassPermissions` を実行
5. ウィンドウ名: `{project_id}-interactive`

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
# タスクの実行プロンプトを index + Markdown から読み取ってジョブ投入
dispatcher.py run --task 20260301-001

# プロンプトを stdin から読み取ってジョブ投入
echo "..." | dispatcher.py run --project ubs-mgmt-tool

# 対話的セッション
dispatcher.py open --project ubs-mgmt-tool [--session main]

# ステータス確認
dispatcher.py status [--json]

# ジョブ制御
dispatcher.py cancel --id ubs-mgmt-tool-1
dispatcher.py kill --id ubs-mgmt-tool-1
dispatcher.py kill --all

# ジョブ完了待機
dispatcher.py wait --id ubs-mgmt-tool-1

# ジョブの stdout/stderr ログを表示
dispatcher.py log --id ubs-mgmt-tool-1

# サーバ起動（通常は systemd 経由）
dispatcher.py server [--max-slots 3]
```

### `run --task` の処理

1. `tasks/index.jsonl` から `task_id` に対応するエントリを取得
2. `tasks/{task_id}.md` から実行プロンプトセクションを読み取り
3. エントリの `project_id` を使用
4. サーバに `run` コマンドを送信

### `run --project` の処理

1. stdin からプロンプトを読み取り
2. サーバに `run` コマンドを送信

## tmux セッション決定

`open` コマンド時に使用する tmux セッションの決定優先順位:

1. `--session` 引数で明示指定（存在確認あり）
2. `$TMUX` 環境変数から自動検出した呼び出し元セッション
3. フォールバック: `dispatch` セッションを新規作成

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
- ネットワークモード: {restricted|unrestricted}

制約事項 (restricted モード):
- ネットワーク: GitHub/Bitbucket SSH と HTTP プロキシ経由の HTTPS のみ利用可能
- ファイル: 作業ディレクトリ内のファイルのみ変更可能

作業が完了したら、変更をコミットしてください。
プロセスの終了がジョブ完了の通知になります（シグナルファイルは不要です）。
```
