# ディスパッチャー設計ドキュメント

## 概要

タスクディスパッチャーは、デイリーゴールに登録されたタスクを tmux 上で複数の Claude Code セッションとして並列実行するバッチ型ツールである。粒度の小さいタスクを大量に抱える状況で、手動でのセッション立ち上げ・コンテキスト伝達の繰り返しを自動化する。

## アーキテクチャ

```
dispatcher.py start
    │
    ├── 日次ゴール読み込み (daily/YYYY-MM-DD.json)
    ├── タスク抽出・フラット化
    │     └── project_id → projects/{id}.json → working_directory 解決
    ├── tmux セッション作成 ("dispatch")
    └── ディスパッチループ
          ├── queued タスクを max_slots まで起動
          │     └── tmux new-window → sandbox -p "prompt"
          ├── PID 監視 → 終了検知
          ├── 状態ファイル保存 (ポーリング間隔: 5秒)
          └── 全タスク完了で終了 → JSON レポート出力
```

## tmux セッション構成

- **セッション名**: `dispatch`（固定）
- **コントロールウィンドウ**: `_control`（セッション作成時に自動生成）
- **タスクウィンドウ**: `{dispatch_id}`（例: `jira-UBS-101`）
  - 各ウィンドウで1つの Claude Code セッションが実行される
  - ウィンドウ名は ref の `/` を `-` に置換したもの

## ランタイム状態

### 状態ファイル

パス: `$XDG_RUNTIME_DIR/my-tasks-dispatcher.json`（フォールバック: `/tmp/my-tasks-dispatcher.json`）

詳細なスキーマは `schemas.md` のセクション6を参照。

### 通信メカニズム

メインループと他のコマンド（`stop`, `add`）は状態ファイルを介して通信する:
- `stop`: `status` を `interrupted` に変更 → メインループが次のポーリングで検知して停止
- `add`: `items` 配列に新しいアイテムを追加 → メインループが次のポーリングで検知して起動

## CLI コマンドリファレンス

### `start`

ディスパッチを開始する。

```bash
python3 dispatcher.py start --repo ~/.local/share/my-tasks [--date YYYY-MM-DD] [--max-slots N] [--command sandbox]
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--repo` | (必須) | タスク管理リポジトリのパス |
| `--date` | 今日 | 対象日付 |
| `--max-slots` | 3 | 最大並列セッション数 |
| `--command` | `sandbox` | セッション起動コマンド |

### `status`

実行状況を JSON で出力する。

```bash
python3 dispatcher.py status
```

### `add`

実行中のディスパッチにタスクを追加する。

```bash
python3 dispatcher.py add --ref jira/UBS-103 --working-dir /path/to/project [--note "補足"]
```

### `stop`

新規タスクの起動を停止する。実行中のセッションは継続する。

```bash
python3 dispatcher.py stop
```

### `kill`

tmux セッション `dispatch` ごと強制終了する。

```bash
python3 dispatcher.py kill
```

## セッション終了検知

1. `os.kill(pid, 0)` で PID 生存チェック（ポーリング間隔: 5秒）
2. PID 消失時に `/tmp/dispatch-{dispatch_id}.exit` の内容を読み取り
3. exit code 判定:
   - `0` → `done`
   - `0` 以外 → `failed`
   - exit file なし → リトライ（3回、1秒間隔）→ それでもなければ `failed` (`exit_code=-1`)

## プロンプトテンプレート

各 Claude Code セッションに渡されるプロンプト:

```
タスク {ref} に取り組んでください。

タスク管理リポジトリ: {repo_dir}
このリポジトリの tasks/{datasource_id}.json と datasources/{datasource_id}.json を参照して、
タスクの詳細情報を確認してください。
必要に応じて元データソース（JIRA、Microsoft To Do 等）からも情報を取得してください。

補足指示: {note}  ← note がある場合のみ

作業が完了したら、変更をコミットして終了してください。
```

## エラーハンドリング

| エラー | 挙動 |
|---|---|
| プロジェクト不存在 | 警告（stderr）+ 該当タスクを skip |
| working_directory 未設定 | 警告（stderr）+ 該当タスクを skip |
| working_directory がファイルシステムに存在しない | 警告（stderr）+ 該当タスクを skip |
| tmux ウィンドウ作成失敗 | 警告（stderr）+ 該当タスクを failed |
| Claude Code exit code != 0 | failed |
| PID 消失 + exit file なし | failed (exit_code=-1) |
| 既にディスパッチャー実行中 | エラー終了 |
| KeyboardInterrupt | 状態保存して終了（tmux セッションは継続） |
