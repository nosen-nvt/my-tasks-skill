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
| `dispatch` | `project_id?`, `prompt?`, `context?` | ライフサイクルを開始（精査→実行→評価を自動制御） |
| `resume` | `lifecycle_id`, `project_id?` | suspend 中のライフサイクルを再開 |
| `run` | `project_id`, `prompt`, `job_type?` | ジョブを実行（sandbox_profile はプロジェクト設定から自動解決） |
| `open` | `project_id`, `session?` | 対話的セッションを tmux で開く |
| `status` | | 全ジョブ + ライフサイクルのステータスを返す |
| `cancel` | `dispatch_id` | キュー内ジョブを取消 |
| `kill` | `dispatch_id` | 実行中ジョブを強制停止 |
| `kill-all` | | 全ジョブを強制停止 |
| `wait` | `dispatch_id` | ジョブ完了まで接続を保持 |
| `log` | (CLI のみ) | ジョブの stdout/stderr ログを表示（ファイル直接読み取り） |

```json
{"command": "run", "project_id": "ubs-mgmt-tool", "prompt": "...", "job_type": "execute"}
{"command": "run", "project_id": "bo", "prompt": "バグを修正してください"}
{"command": "run", "project_id": "bo", "prompt": "...", "job_type": "refine"}
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
   - `$XDG_RUNTIME_DIR/my-tasks-dispatch/{dispatch_id}.log` にログファイルを作成
   - `asyncio.create_subprocess_exec("sandbox", "--sandbox-profile", profile, "claude", "-p", prompt, ...)` でジョブ実行（stdout/stderr をログファイルに出力）
   - `proc.wait()` で完了検知
   - `done` or `failed` に更新
   - waiter に通知
   - `drain_queue()` で次のジョブを起動

### `open` コマンド

対話的セッションは引き続き tmux を使用する。ジョブ管理の対象外。

1. クライアントから `open` コマンドを受信
2. `projects/{project_id}.json` から `sandbox_profile`（デフォルト: `"default"`）と `working_directory` を取得
3. 指定された tmux セッション（またはデフォルトセッション）にウィンドウを作成
4. ウィンドウ内で `sandbox --sandbox-profile {profile} claude --permission-mode bypassPermissions` を実行
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

- `Lifecycle` dataclass: lifecycle_id, project_id, prompt, status, suspend_reason, run_count, max_runs, current_dispatch_id, timestamps
- 永続化: `$XDG_RUNTIME_DIR/my-tasks-dispatch/lifecycles.jsonl` (状態変更のたびに全件書き出し)
- Job に `lifecycle_id` フィールドを追加（Lifecycle 経由のジョブを識別）

#### コマンド

| コマンド | フィールド | 説明 |
|---------|-----------|------|
| `dispatch` | `project_id?`, `prompt?`, `context?` | ライフサイクル開始。精査→承認→実行→評価を自動制御 |
| `resume` | `lifecycle_id`, `project_id?` | suspend 中のライフサイクルを再開 |

#### ステートマシンフロー

```
dispatch → reshaping → 精査ジョブ
                          ├── scoped + auto_approve → running → 実行ジョブ → evaluating → 評価ジョブ
                          │                                                                ├── PASS → done
                          │                                                                ├── RETRY → reshaping (ループ)
                          │                                                                ├── BLOCKED → suspend
                          │                                                                └── ABORT → done
                          ├── scoped + 手動承認 → suspend (approval_required)
                          ├── needs_input → suspend (needs_input)
                          └── reshaping (問題なし) → done
```

#### 結果ファイル

ジョブは `$XDG_RUNTIME_DIR/my-tasks-dispatch/{dispatch_id}.result.json` に結果 JSON を出力。
Lifecycle ステートマシンがこのファイルを読んで次の状態を決定する（フォールバック: index.jsonl）。

#### LLM プロジェクト判定

`dispatch` 時に `project_id` 未指定の場合、`claude` CLI でプロジェクトを自動判定する。

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
# ライフサイクル開始（推奨）
dispatcher dispatch --project bo --prompt "タスクタイトル" --context-file /path/to/task.md
dispatcher dispatch --project bo --prompt "バグを修正して"

# ライフサイクル再開
dispatcher resume --id lc-1

# 対話的セッション
dispatcher open --project ubs-mgmt-tool [--session main]

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
dispatcher server [--max-slots 8]
```

### `run` の処理

1. stdin からプロンプトを読み取り
2. サーバに `run` コマンドを送信（`--project` 必須）

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
- ネットワーク: {保護あり (netns + proxy)|ホストネットワーク直接}

制約事項 (ネットワーク保護あり):
- ネットワーク: GitHub/Bitbucket SSH と HTTP プロキシ経由の HTTPS のみ利用可能
- ファイル: 作業ディレクトリ内のファイルのみ変更可能

認証情報:
- `cred-get <entry>` または `pass show <entry>` で以下の認証情報を取得できます:
  - {allowed_credentials のエントリ一覧}

作業が完了したら、変更をコミットしてください。
プロセスの終了がジョブ完了の通知になります（シグナルファイルは不要です）。
```

（認証情報セクションは `allowed_credentials` が設定されている場合のみ追加される）

## Credential Broker

### 概要

サンドボックス内のジョブが `~/.password-store` や `~/.gnupg` に直接アクセスすることを防ぎ、
プロジェクト単位でスコープされた認証情報アクセスを提供する仕組み。

### アーキテクチャ

```
┌─ ホスト ──────────────────────────────────────────────┐
│                                                        │
│  dispatcher server                                  │
│    ├─ DispatchServer (dispatcher.sock)                 │
│    └─ CredentialBroker (cred-broker.sock)              │
│         ├─ token registry: {token → [entries]}         │
│         └─ /usr/bin/pass show <entry> で取得           │
│                                                        │
│  ┌─ サンドボックス ──────────────────────────────────┐ │
│  │                                                    │ │
│  │  pass show <entry>                                 │ │
│  │    → /usr/bin/pass (pass-shim)                     │ │
│  │      → cred-get <entry>                            │ │
│  │        → connect(cred-broker.sock)                 │ │
│  │          → CredentialBroker (ホスト側)              │ │
│  │            → /usr/bin/pass show <entry> (実体)     │ │
│  │                                                    │ │
│  └────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────┘
```

### ソケットパス

`$XDG_RUNTIME_DIR/my-tasks-dispatch/cred-broker.sock`

dispatcher.sock と同じディレクトリに配置。sandbox で既に bind-mount 済みのため追加設定不要。

### JSON プロトコル

改行区切りの JSON line。

**リクエスト:**

```json
{"token": "<job-token>", "entry": "jira/api-token"}
```

**レスポンス（成功）:**

```json
{"ok": true, "value": "<credential-value>"}
```

**レスポンス（エラー）:**

```json
{"ok": false, "error": "entry not allowed: secret/other"}
```

### ライフサイクル

1. **ジョブ開始**: `allowed_credentials` が設定されている場合、`uuid4().hex` でトークン生成 → `CredentialBroker.register(token, entries)`
2. **ジョブ実行中**: 環境変数 `CRED_TOKEN` と `CRED_BROKER_SOCK` がサンドボックスに渡される
3. **認証情報取得**: サンドボックス内で `pass show <entry>` → pass-shim → cred-get → broker socket → `/usr/bin/pass show <entry>` (ホスト側)
4. **ジョブ終了**: `CredentialBroker.revoke(token)` でトークン無効化

### cred-get CLI

サンドボックス内で使用する軽量クライアント。

```bash
# 直接呼び出し
cred-get jira/api-token

# pass 互換（pass-shim 経由で自動委譲）
pass show jira/api-token
pass jira/api-token
```

### プロジェクト設定

サンドボックスプロファイルの `allowed_credentials` フィールドで、そのプロジェクトのジョブがアクセス可能な `pass` エントリを指定する。

```json
{
  "profile_id": "restricted-default",
  "proxy_profile": "dev",
  "credential_profile": "full-access"
}
```

### セキュリティ特性

- **スコープ制限**: 各ジョブは自プロジェクトの `allowed_credentials` に列挙されたエントリのみ取得可能
- **トークン有効期限**: ジョブ終了時に自動 revoke
- **並行安全**: 各ジョブが固有トークンを持つため、複数ジョブ並行でも問題なし
- **ホスト側影響なし**: pass-shim は bwrap の `--ro-bind` でサンドボックス内のみに適用。ホスト側の `/usr/bin/pass` は変更されない
- **ログ**: トークンは先頭 8 文字のみ記録（`token[:8]...`）
