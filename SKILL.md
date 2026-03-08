---
name: my-tasks
description: |
  個人タスク管理スキル。複数外部データソース（JIRA、Microsoft To Do、メール等）からタスクを収集し、
  プロジェクト単位で管理する。タスクの精査・プロンプト生成・承認・実行のワークフローを提供。
  使用例:「タスクを最新化して」「タスクを精査して」「プロンプトを生成して」「承認して」
  「実行して」「ディスパッチして」「ステータスを確認して」「タスクを操作して」
  「完了処理を実行して」「プロジェクトを追加して」「データソースを追加して」
  「メールをチェックして」「ジョブの状態を確認して」
  「これを精査して」「次にやる」など、
  タスク管理リポジトリ（~/.local/share/my-tasks/）への操作を依頼された場合に使用。
---

# my-tasks スキル

個人のタスク管理リポジトリ（`~/.local/share/my-tasks/`）を Claude Code から操作するスキル。
JIRA・Microsoft To Do・メール等の外部データソースからタスクを収集し、プロジェクト単位で管理する。
タスクの精査 → プロンプト生成 → 承認 → 実行のワークフローを提供する。
設定情報（datasources/, projects/）は git 管理、タスクデータ（tasks/）はローカルのみ。

## オペレーション一覧

1. **タスク収集** - 全データソースからタスクを取得し、index.jsonl + Markdown を更新
2. **メールトリアージ** - メールデータソースからアクションアイテムを収集
3. **タスク精査** - refine.py で1タスク1ジョブとして精査をディスパッチ（scoped 遷移時にプロンプトも同時生成）
4. **プロンプト再生成** - scoped タスクの実行プロンプトを再生成（通常は操作3で自動生成）
5. **プロンプト承認** - 生成されたプロンプトを確認し approved に遷移
6. **タスク実行** - approved タスクをディスパッチャー経由で実行
7. **ステータス確認** - タスク一覧、ジョブ状況の表示
8. **タスク操作** - データソース側のタスクを操作（ステータス変更等）
9. **完了確認・完了時アクション** - reshaping タスク（run_count > 0）の結果確認 → done 遷移 + 後処理実行
10. **設定管理** - プロジェクト・データソースの CRUD、リポジトリ初期化
11. **精査対象選択** - pending タスクを reshaping にする（精査対象として選択）

## 詳細リファレンス

- **アーキテクチャ・リポジトリ構成**: `references/architecture.md`
- **全スキーマと記述例**: `references/schemas.md`
- **各操作の詳細手順**: `references/operations.md`
- **ディスパッチャー設計**: `references/dispatcher-design.md`

## sync-tasks.py の使い方

タスク収集の中核ロジック。JSONL 入力から `tasks/index.jsonl` + `tasks/*.md` を生成・更新する。

```bash
# 基本的な使い方（fetch-all.sh の出力を直接パイプ）
~/.local/share/my-tasks/scripts/fetch-all.sh \
  | python3 ~/.claude/skills/my-tasks/scripts/sync-tasks.py \
    --repo ~/.local/share/my-tasks

# ファイルから読み込む場合
python3 ~/.claude/skills/my-tasks/scripts/sync-tasks.py \
  --repo ~/.local/share/my-tasks \
  --input /tmp/tasks.jsonl

# 特定データソースのみ処理（他データソースのタスクには影響しない）
python3 ~/.claude/skills/my-tasks/scripts/sync-tasks.py \
  --repo ~/.local/share/my-tasks \
  --input /tmp/tasks.jsonl \
  --datasource jira,ms-todo

# メールトリアージ時（メール系データソースのみ指定）
python3 ~/.claude/skills/my-tasks/scripts/sync-tasks.py \
  --repo ~/.local/share/my-tasks \
  --input /tmp/mail-triage.jsonl \
  --datasource mail-outlook,mail-gmail-nvt,mail-gmail-qzl
```

### 引数

| 引数 | 必須 | 説明 |
|---|---|---|
| `--repo PATH` | Yes | タスク管理リポジトリのパス |
| `--input FILE` | No | JSONL ファイルのパス（省略時は stdin） |
| `--datasource IDS` | No | 処理対象のデータソース ID（カンマ区切り）。未指定時は入力 JSONL に含まれるデータソースのみ処理 |

### sync_mode による動作の違い

datasource 設定の `sync_mode` フィールドに応じて同期動作が異なる:

| sync_mode | 消失検出 | done タスクの GC | 用途 |
|---|---|---|---|
| `full`（デフォルト） | あり | なし | JIRA、To Do 等の状態型データソース |
| `append` | なし | あり（sync 時に除去） | メール等のイベント型データソース |

### 出力（JSON レポート）

```json
{
  "summary": {
    "added": 3,
    "updated": 1,
    "vanished": 1,
    "gc": 2,
    "auto_assigned": 2,
    "needs_review": 1
  },
  "added": [...],
  "updated": [...],
  "vanished": [...],
  "gc": [...],
  "auto_assigned": [...],
  "needs_review": [...]
}
```

- `auto_assigned`: `project_mapping` で自動割り当て成功したタスク
- `needs_review`: プロジェクトが特定できなかったタスク → ユーザーに確認が必要
- `gc`: GC で除去された done タスク（append モード）

## refine.py の使い方

タスク精査を1タスク1ジョブとしてディスパッチャーに投入するスクリプト。
各ジョブは専用のコンテキストでタスクを精査し、scoped 遷移時には実行プロンプトも同時に生成する。
`run_count > 0` のタスクは再精査として実行履歴を踏まえたプロンプト修正を行う。

```bash
# 全 reshaping タスクを精査ジョブとして投入
python3 ~/.claude/skills/my-tasks/scripts/refine.py \
  --repo ~/.local/share/my-tasks

# 特定タスクのみ
python3 ~/.claude/skills/my-tasks/scripts/refine.py \
  --repo ~/.local/share/my-tasks \
  --task 20260301-001

# 全回答済み needs_clarification タスクも含める
python3 ~/.claude/skills/my-tasks/scripts/refine.py \
  --repo ~/.local/share/my-tasks \
  --include-clarified

# ドライラン（プロンプトを表示するだけで投入しない）
python3 ~/.claude/skills/my-tasks/scripts/refine.py \
  --repo ~/.local/share/my-tasks \
  --dry-run
```

### 引数

| 引数 | 必須 | 説明 |
|---|---|---|
| `--repo PATH` | No | タスク管理リポジトリのパス（デフォルト: `~/.local/share/my-tasks`） |
| `--task ID` | No | 特定タスク ID のみ処理 |
| `--include-clarified` | No | 全回答済みの `needs_clarification` タスクも対象にする |
| `--sandbox-profile PROFILE` | No | サンドボックスプロファイルを上書き指定 |
| `--dry-run` | No | 投入せずにプロンプトを表示するだけ |

### 出力（JSON レポート）

```json
{
  "dispatched": 2,
  "failed": 0,
  "skipped": 1,
  "details": [...],
  "skipped_details": [{"task_id": "20260301-003", "reason": "manual プロジェクト (manual)"}]
}
```

### 対象タスクとプロンプト

| ステータス | 対象条件 | ジョブの動作 |
|---|---|---|
| `reshaping`（`run_count=0`） | 常に対象 | 初回精査 → `needs_clarification` or `scoped`（+ 実行プロンプト生成） |
| `reshaping`（`run_count>0`） | 常に対象 | 再精査（実行履歴を踏まえてプロンプト修正） → `scoped`（+ 実行プロンプト再生成）。問題なければユーザーに完了確認を促す |
| `needs_clarification` | `--include-clarified` 指定時、かつ全チェックボックスが `[x]` | 再精査 → `scoped`（+ 実行プロンプト生成） |

### 注意

- manual プロジェクト（`working_directory` なし）はスキップされる。メインセッションで直接処理すること
- 各ジョブはプロジェクトの作業ディレクトリで実行されるため、ソースコードの調査が可能
- ジョブは `tasks/{id}.md` と `tasks/index.jsonl` を直接更新する

---

## dispatcher.py の使い方

Unix ドメインソケット C/S ジョブランナー。サーバは systemd user service として常駐。

```bash
# タスク ID 指定でジョブ投入（index.jsonl + .md からプロンプトを自動読み取り）
python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py run --task 20260301-001

# プロジェクト ID + stdin プロンプト指定
echo "バグを修正してください" | python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py run --project bo

# 対話セッション（ジョブ管理の対象外）
python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py open --project bo
python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py open --project bo --session main

# プロファイルを上書きしてジョブ投入（例: unrestricted で調査タスクを実行）
python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py run --task 20260301-001 --sandbox-profile unrestricted

# open でも同様
python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py open --project bo --sandbox-profile unrestricted

# ステータス確認
python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py status
python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py status --json

# ジョブ制御
python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py cancel --id bo-1
python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py kill --id bo-1
python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py kill --all

# ジョブ完了待機（run とは別途実行）
python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py wait --id bo-1

# 実行して完了まで待機（run は非同期なので、dispatch_id を取り出して wait に渡す）
# run の出力例（stderr）: "Job started: bo-1"
DISP=$(python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py run --task 20260301-001 2>&1 | grep -oP '[\w-]+-\d+$')
python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py wait --id "$DISP"

# ジョブの stdout/stderr ログを表示
python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py log --id bo-1

# サーバ起動（通常は systemd 経由）
python3 ~/.claude/skills/my-tasks/scripts/dispatcher.py server [--max-slots 8]
```

### 前提条件

- プロジェクト定義に `working_directory` が設定されていること
- サーバが起動していること（systemd user service または手動起動）

詳細は `references/dispatcher-design.md` を参照。

## 注意事項

- 設定変更（datasources/, projects/）後に `git add . && git commit && git push` を実行する
- `tasks/` は gitignore なので commit 不要
