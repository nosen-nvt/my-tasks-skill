# ディスパッチャー設計ドキュメント

## 概要

プロセス分散型ジョブランナー。各ジョブは独立した dispatcher プロセスとして実行される。
中央ループやデーモンは存在しない。状態ファイルが唯一の協調メカニズム。
スロットチェック〜起動の区間を `flock` で排他制御する。プロンプトは標準入力から読み取る。

## アーキテクチャ

```
echo "fix bug" | dispatcher.py run --project bo
  │
  ├── flock 取得 → running ファイルを数える
  ├── スロット空き → tmux ウィンドウを起動、状態ファイルを書く、flock 解放、終了
  └── スロット満杯 → 状態ファイルを書く、flock 解放 → fork してバックグラウンドで待機、親は即座に返却
                       └── flock 取得 → スロット空き → 起動 → flock 解放
```

## 状態管理

### 状態ディレクトリ

`$XDG_RUNTIME_DIR/my-tasks-dispatch/`（フォールバック: `/tmp/my-tasks-dispatch/`）

### ロックファイル

`{state_dir}/.lock` — `flock` によるファイルロック。スロットチェック〜起動の区間を排他制御する。

### 状態ファイル（1ジョブ = 1ファイル）

`{state_dir}/{dispatch_id}.json`:

詳細なスキーマは `schemas.md` のセクション6を参照。

### dispatch_id の生成

`{project_id}-{連番}` 形式。連番は状態ディレクトリ内の既存ファイルから最大値+1で生成する。
例: `bo-1`, `bo-2`, `ubs-mgmt-tool-1`

### 遅延更新

完了検知はポーリングではなく「遅延」で行う。`status` コマンドや `run` の待機ループが
PID 生存チェック + sentinel ファイル検知でステータスを更新する。

## tmux セッション構成

ディスパッチャーは以下の優先順位で使用する tmux セッションを決定する:

1. `--session` 引数で明示指定されたセッション（存在確認あり）
2. `$TMUX` 環境変数から自動検出した呼び出し元セッション
3. フォールバック: `dispatch` セッションを新規作成

呼び出し元セッション（優先順位 1, 2）を使用する場合:
- セッションの新規作成は行わない（存在確認のみ）

フォールバック（優先順位 3）の場合:
- `dispatch` セッションを新規作成する
- **コントロールウィンドウ**: `_control`（セッション作成時に自動生成）

**タスクウィンドウ**: `{dispatch_id}`（例: `bo-1`）
  - 各ウィンドウで1つの Claude Code セッションが実行される

## CLI コマンドリファレンス

### `run`

ジョブを投入する。プロンプトは stdin から読み取る。

```bash
echo "バグを修正してください" | dispatcher.py run --project bo [--max-slots 3]

# ヒアドキュメントで複数行プロンプト
dispatcher.py run --project bo <<'EOF'
ログイン画面のバグを修正してください。
エラーメッセージが表示されない問題です。
EOF
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--project` | (必須) | プロジェクトID |
| `--repo` | `~/.local/share/my-tasks` | タスク管理リポジトリのパス |
| `--max-slots` | 3 | 最大並列スロット数 |
| `--command` | `sandbox claude --permission-mode bypassPermissions` | セッション起動コマンド |
| `--session` | 自動検出 | tmux セッション名を明示指定 |

### `open`

プロジェクトの作業ディレクトリで対話セッションを起動する。ジョブ管理（状態ファイル、スロット管理、完了検知）は一切行わない。

```bash
dispatcher.py open --project bo
dispatcher.py open --project bo --command "sandbox claude"
dispatcher.py open --project bo --session main
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--project` | (必須) | プロジェクトID |
| `--repo` | `~/.local/share/my-tasks` | タスク管理リポジトリのパス |
| `--command` | `claude` | 起動コマンド |
| `--session` | 自動検出 | tmux セッション名を明示指定 |

ウィンドウ名は `{project_id}-interactive` となる。

### `status`

状態を表示する（遅延更新を実行してから表示）。

```bash
dispatcher.py status [--json]
```

### `cancel`

キューからジョブを取り消す。

```bash
dispatcher.py cancel --id bo-2
```

### `kill`

実行中ジョブを強制停止する。

```bash
dispatcher.py kill --id bo-1
dispatcher.py kill --all
```

## セッション終了検知

二段階の完了検知メカニズムを持つ:

### Sentinel ファイル検知（主系）

1. Claude がタスク完了時に `touch {state_dir}/.dispatch-{dispatch_id}.done` を実行
2. `status` コマンドや待機ループが `.done` ファイルを検知
3. `tmux kill-window` で tmux ウィンドウを終了 → `done` (exit_code=0) として記録

### PID 監視（副系・フォールバック）

1. `os.kill(pid, 0)` で PID 生存チェック
2. PID 消失時に `{state_dir}/.dispatch-{dispatch_id}.exit` の内容を読み取り
3. exit code 判定:
   - `0` → `done`
   - `0` 以外 → `failed`
   - exit file なし → `failed` (`exit_code=-1`)

### シグナルファイルの配置場所

すべてのシグナルファイル（`.done`、`.exit`）は状態ディレクトリ（`{state_dir}`）内に配置される。
これにより、サンドボックス（bwrap）内からディスパッチャーを実行する場合でも、
呼び出し側が `working_dir` への書き込み権限を持たない環境でシグナルファイルの操作が正常に動作する。

## プロンプト構築

stdin から読み取ったプロンプトに完了通知の指示を追加する:

```
{stdin から読み取ったプロンプト}

作業が完了したら、変更をコミットしてから次のコマンドを実行してください:
touch {state_dir}/.dispatch-{dispatch_id}.done
```

さらに `--append-system-prompt` でシステムプロンプトにも同様の指示を注入する（冗長化）。

## 排他制御

`flock` によるファイルロックで以下の区間を排他制御する:

- **run コマンド**: スロットチェック → dispatch_id 生成 → 状態ファイル書き込み → (起動)
- **待機ループ**: スロットチェック → 起動 → 状態ファイル更新
- **status コマンド**: refresh_states（遅延更新）

## エラーハンドリング

| エラー | 挙動 |
|---|---|
| プロジェクト不存在 | エラー終了 |
| working_directory 未設定 | エラー終了 |
| working_directory がファイルシステムに存在しない | エラー終了 |
| tmux ウィンドウ作成失敗 | エラー終了（run）/ failed（待機ループ） |
| プロンプトが空 | エラー終了 |
| PID 消失 + exit file なし | failed (exit_code=-1) |
