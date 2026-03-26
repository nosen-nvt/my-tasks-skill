# サンドボックス ホストコマンド移行設計

## 概要

サンドボックス内の外部通信を Host Command Broker 経由に集約し、
ネットワーク攻撃面の縮小・認証情報の隔離・proxy profile の簡素化を実現する。

### 現状の課題

- proxy profile が 3 つ (dev/office/full) あり、ドメインリストが重複しつつ微妙に異なる
- `~/.ssh` がサンドボックス内に ro マウントされており、秘密鍵が露出している
- netns の nft ルールで `:22` (SSH) をドメイン別 IP で許可しており、CDN 変更で漏れるリスクがある
- atl, msgraph, google-cli, freee-cli が直接 HTTPS 通信するため、広範なドメイン許可が必要

### 目標状態

```
                        現状                              目標
proxy ドメイン数     20-30 x 3 profiles         →   ~10 x 1 profile
~/.ssh マウント      ro mount                   →   不要（廃止）
:22 nft ルール       github/bitbucket IP 許可   →   不要（廃止）
認証情報             sandbox 内 env 変数        →   broker 側のみ
host commands        pass のみ                  →   pass, git, atl, msgraph, google-cli, freee-cli
```

## Phase 1: Host Command Broker 拡張

### 1.1 ビルトインホストコマンド

git のように制御ルールが複雑なコマンドは、プロファイル JSON に定義させるとミスのリスクが高い。
broker のコード内にビルトインとして定義し、プロファイルでは有効化のみ行う。

```python
# host_cmd.py

BUILTIN_HOST_COMMANDS = {
    "git": {
        "path": "/usr/bin/git",
        "allowed_subcommands": [
            "status", "log", "diff", "show", "blame",
            "add", "commit", "push", "pull", "fetch",
            "checkout", "switch", "branch", "merge", "rebase", "cherry-pick",
            "stash", "tag", "worktree",
            "rev-parse", "symbolic-ref", "for-each-ref", "ls-files", "ls-remote",
            "shortlog", "describe", "config", "reset", "clean", "rm",
            "init", "mv", "restore", "bisect", "grep",
        ],
        "denied_patterns": [
            "push *://*", "push *@*:*",
            "fetch *://*", "fetch *@*:*",
            "remote add *", "remote set-url *", "remote remove *", "remote rename *",
            "config remote.*",
            "-c *",
            "submodule add *",
        ],
        "env": {
            "GIT_CONFIG_NOSYSTEM": "1",
        },
        "require_cwd": True,
    },
}
```

プロファイルでの記述:

```json
{
  "host_commands": [
    {"name": "git", "builtin": true},
    {"name": "git", "builtin": true, "extra_denied_patterns": ["push *"]}
  ]
}
```

- `"builtin": true` で `BUILTIN_HOST_COMMANDS` から定義をロード
- `extra_denied_patterns` で制約の追加のみ許可（`allowed_subcommands` の追加や `denied_patterns` の削除は不可）

### 1.2 denied_patterns

`allowed_patterns` に加え、`denied_patterns` を導入する。

**評価順序** (deny-first):

```
1. denied_patterns にマッチ → 即拒否
2. allowed_patterns にマッチ → 許可
3. どちらにもマッチしない   → 拒否
```

fnmatch による文字列マッチ。対象は `" ".join(args)` (現行の allowed_patterns と同じ)。

### 1.3 allowed_subcommands

`allowed_subcommands` が定義されている場合、サブコマンドの抽出と照合を行う。
git のようにグローバルオプションがサブコマンドの前に来るケースに対応する。

```python
def extract_git_subcommand(args: list[str]) -> str | None:
    """git のグローバルオプションをスキップしてサブコマンドを抽出する。"""
    GLOBAL_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree",
                              "--namespace", "--config-env"}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in GLOBAL_OPTS_WITH_VALUE:
            i += 2
        elif arg.startswith("-"):
            i += 1
        else:
            return arg
    return None
```

**評価順序** (allowed_subcommands が定義されている場合):

```
1. サブコマンドを抽出
2. allowed_subcommands に含まれない → 即拒否
3. denied_patterns にマッチ         → 即拒否
4. allowed_patterns を評価          → マッチすれば許可
   (allowed_patterns 未定義 or "*" の場合は許可)
5. いずれにもマッチしない          → 拒否
```

### 1.4 ホストコマンド別環境変数 (env)

ホストコマンド定義に `env` フィールドを追加。
broker がコマンド実行時に `subprocess` の `env` パラメータにマージして渡す。

```json
{
  "name": "atl",
  "path": "/home/nosen/.local/bin/atl",
  "allowed_patterns": "*",
  "env": {
    "ATL_SITE": "novshi-tech.atlassian.net",
    "ATL_TOKEN": {"pass": "atlassian/api-token"}
  }
}
```

- 文字列値はそのまま設定
- `{"pass": "..."}` 形式は broker 起動時に `pass show` で解決してキャッシュ
- サンドボックス内からは env の値を参照・変更できない（セキュリティ上の利点）
- ビルトインの `env` とプロファイルの `env` はマージ（プロファイル側が優先）

### 1.5 cwd サポート

host-cmd クライアント (shim) がリクエストに `cwd` を含め、
broker が検証した上で `subprocess.run(cwd=...)` で実行する。

**プロトコル拡張:**

```json
{"token": "...", "command": "git", "args": ["status"], "cwd": "/home/nosen/src/github.com/nosen-nvt/foo"}
```

**host-cmd shim の変更:**

```python
request = {"token": token, "command": command, "args": args}
if stdin_data:
    request["stdin"] = stdin_data
# cwd を常に送信
request["cwd"] = os.getcwd()
```

**broker 側のバリデーション:**

`require_cwd: true` のコマンドは cwd 必須。バリデーションルール:

- パスが絶対パスであること
- パスが実在するディレクトリであること
- パスが許可された prefix のいずれかに含まれること

許可 prefix はデフォルトで `["/home/{user}/src"]`。
プロファイルで `cwd_allowed_prefixes` を追加可能。

### 1.6 変更対象ファイル

| ファイル | 変更内容 |
|---------|---------|
| `scripts/orchestrator/host_cmd.py` | ビルトイン定義、denied_patterns、allowed_subcommands、env、cwd の処理 |
| `scripts/sandbox_exec.py` | `EmbeddedHostCommandBroker` に同等の変更。ビルトイン解決ロジック |
| `scripts/host-cmd` | リクエストに `cwd` を追加 |

### 1.7 テスト観点

- ビルトインコマンドの解決（builtin: true → BUILTIN 定義がロードされること）
- denied_patterns が allowed_patterns より先に評価されること
- git サブコマンド抽出（`-C /path status` → `status`、`-c key=val push` → denied）
- `extra_denied_patterns` がビルトインの denied に追加されること
- env の pass 解決
- cwd バリデーション（正常系・パストラバーサル・存在しないパス）

---

## Phase 2: git ホストコマンド化

### 2.1 目的

- `~/.ssh` のサンドボックス内マウントを廃止
- netns nft ルールから `:22` 許可を削除
- git 操作の認証をホスト側に完全に隔離

### 2.2 サンドボックスプロファイル変更

全プロファイルに git ビルトインを追加:

```json
{
  "host_commands": [
    {"name": "git", "builtin": true},
    ...
  ]
}
```

組み込みプロファイル (`BUILTIN_PROFILES`) にも追加:

```python
BUILTIN_PROFILES = {
    "default": {
        ...
        "host_commands": [
            {"name": "git", "builtin": True},
            {"name": "pass", "path": "/usr/bin/pass", "allowed_patterns": "*", "allow_stdin": True},
        ],
    },
    ...
}
```

### 2.3 sandbox_exec.py の変更

`build_netns_args()` から以下を削除:

```python
# 削除
"--ro-bind-try", f"{HOME}/.ssh", f"{HOME}/.ssh",
```

### 2.4 setup-netns の変更

末尾の SSH 許可ルールを削除:

```bash
# 削除
for domain in github.com bitbucket.org; do
  ips=$(dig +short "$domain" A 2>/dev/null || true)
  ...
done
```

### 2.5 proxy profile の変更

git は SSH 経由のため proxy profile の変更は不要。
ただし `.github.com` / `.bitbucket.org` は GitHub API (gh コマンド等) でも使うため、
ドメインリストからの削除は Phase 3 以降で判断する。

### 2.6 移行手順

1. Phase 1 の broker 拡張が完了していることを確認
2. 全サンドボックスプロファイル + BUILTIN_PROFILES に git を追加
3. sandbox 内で `git status`, `git log`, `git commit`, `git push` の動作確認
4. `git remote add evil ...` / `git push https://evil.com/...` が拒否されることを確認
5. `~/.ssh` マウントを削除
6. sandbox 内で `ssh` コマンドが使えないことを確認（git は broker 経由なので影響なし）
7. `setup-netns` から SSH ルールを削除
8. netns を再作成して nft ルール反映を確認
9. 全体の回帰テスト（複数プロジェクトでジョブ実行）

### 2.7 ロールバック手順

問題発生時は以下で即座に戻せる:

1. プロファイルから `git` ホストコマンドを削除
2. `~/.ssh` マウントを復元
3. `setup-netns` の SSH ルールを復元
4. `sudo ip netns del ai-ns` → sandbox 再起動で netns 再作成

---

## Phase 3: 外部通信ツールのホストコマンド化

### 3.1 対象ツール

| ツール | 用途 | proxy 経由で使うドメイン |
|--------|------|------------------------|
| `atl` | Jira/Bitbucket 操作 | `.atlassian.net`, `.atlassian.com` |
| `msgraph` | Microsoft 365 操作 | `.microsoft.com`, `login.microsoftonline.com`, `.core.windows.net`, `.sharepoint.com`, `.msauth.net`, `.msftauth.net`, `.live.com` |
| `google-cli` | Google Workspace 操作 | `.google.com`, `.googleapis.com`, `accounts.google.com` |
| `freee-cli` | freee 操作 | `.freee.co.jp` |
| `board-cli` | Board 操作 | `the-board.jp`, `.the-board.jp` |

### 3.2 ホストコマンド定義

これらは git と違い制御ルールが単純なので、ビルトインにする必要はない。
プロファイル定義で十分。

```json
{
  "name": "atl",
  "path": "/home/nosen/.local/bin/atl",
  "allowed_patterns": "*",
  "env": {
    "ATL_SITE": "novshi-tech.atlassian.net"
  }
}
```

```json
{
  "name": "msgraph",
  "path": "/home/nosen/.local/bin/msgraph",
  "allowed_patterns": "*",
  "env": {}
}
```

```json
{
  "name": "google",
  "path": "/home/nosen/.local/bin/google",
  "allowed_patterns": "*",
  "env": {}
}
```

```json
{
  "name": "freee",
  "path": "/home/nosen/.local/bin/freee",
  "allowed_patterns": "*",
  "env": {}
}
```

```json
{
  "name": "board",
  "path": "/home/nosen/.local/bin/board",
  "allowed_patterns": "*",
  "env": {}
}
```

### 3.3 認証情報の扱い

これらのツールはそれぞれ独自のトークンストア（ファイルベース）を持つ。
ホストコマンド化すると、トークンストアはホスト側に存在するため、
サンドボックス内へのバインドマウントが不要になる。

ただし、トークンストアの場所が `$HOME` 配下のデフォルトパスであれば、
broker 側のプロセスはホストの `$HOME` を使うので自動的に解決される。
カスタムパスの場合は `env` で指定する。

### 3.4 プロファイル別の定義

現在の sandbox-profiles の対応表:

| プロファイル | 追加するホストコマンド |
|-------------|---------------------|
| `dev-nvt` | atl, google |
| `dev-khi` | atl, google |
| `office-nvt` | atl, msgraph, google, freee, board |
| `office-qzl` | atl, msgraph, google, freee, board |
| `office-ubs` | msgraph, google |

### 3.5 移行手順

各ツールごとに段階的に移行する。ツール単位の手順:

1. プロファイルにホストコマンド定義を追加
2. sandbox 内で当該ツールの基本操作を確認
3. 問題なければ、proxy profile から対応ドメインを削除（次フェーズまで保留でも可）
4. 次のツールへ

推奨順序: `atl` → `freee` → `board` → `google` → `msgraph`
（利用頻度が低い・影響範囲が小さいものから）

### 3.6 extra_binds の見直し

ホストコマンド化に伴い、ツールのランタイムファイル（トークンストア等）の
バインドマウントが不要になるケースを確認し、`extra_binds` を削減する。

---

## Phase 4: Proxy Profile 統一

### 4.1 統一後のプロファイル

Phase 3 完了後、proxy で許可が必要なドメインは以下のみ:

```json
{
  "profile_id": "default",
  "port": 3128,
  "listen_address": "10.200.1.1",
  "allowed_domains": [
    "api.anthropic.com",
    "platform.claude.com",
    ".claude.ai",
    "registry.npmjs.org",
    "api.nuget.org",
    "pypi.org",
    "files.pythonhosted.org",
    ".docker.io",
    "auth.docker.io",
    "proxy.golang.org",
    "sum.golang.org",
    "dev.azure.com"
  ]
}
```

### 4.2 廃止対象

- `proxy-profiles/full.json` — 廃止
- `proxy-profiles/office.json` — 廃止
- `proxy-profiles/dev.json` — `default` に統合

### 4.3 sandbox-profiles の変更

全プロファイルの `proxy_profile` を統一:

```
dev-nvt.json:    "proxy_profile": "dev"     →  "proxy_profile": "default"
dev-khi.json:    "proxy_profile": "dev"     →  "proxy_profile": "default"
office-nvt.json: "proxy_profile": "office"  →  "proxy_profile": "default"
office-qzl.json: "proxy_profile": "office"  →  "proxy_profile": "default"
office-ubs.json: "proxy_profile": "office"  →  "proxy_profile": "default"
```

### 4.4 BUILTIN_PROFILES の変更

`default` プロファイルの `proxy_profile` を `"default"` に変更（現在は `"full"`）。

### 4.5 setup-netns の変更

複数ポートの動的収集ロジックが不要になる:

```bash
# 変更前: proxy-profiles から全ポートを収集して nft ルールに追加
PROXY_PORTS=()
for profile_json in "$PROFILES_DIR"/*.json; do ...

# 変更後: 単一ポート
PROXY_PORT=3128
```

### 4.6 コード変更

- `sandbox_exec.py` の `PROXY_PROFILES_DIR` 参照を簡素化（1 プロファイルのみ前提）
- `resolve_proxy_port()` はプロファイル参照を維持（将来の拡張余地）するが、実質は常に同じ値を返す

### 4.7 移行手順

1. Phase 3 完了を確認。proxy 経由で通信するツールが残っていないことを確認
2. `default` proxy profile を作成（上記のドメインリスト）
3. 全 sandbox-profiles の `proxy_profile` を `default` に変更
4. BUILTIN_PROFILES の `proxy_profile` を `default` に変更
5. sandbox で各プロファイルの動作確認
   - Anthropic API 接続
   - パッケージインストール (npm, pip, dotnet, go)
   - git push/pull (broker 経由なので proxy 不要だが念のため)
6. 旧 proxy profile (dev/office/full) を削除
7. setup-netns を簡素化
8. netns 再作成

### 4.8 ロールバック

旧 proxy profile ファイルを git から復元し、sandbox-profiles の proxy_profile を戻す。

---

## 横断的考慮事項

### EmbeddedHostCommandBroker との同期

`sandbox_exec.py` の `EmbeddedHostCommandBroker` と `orchestrator/host_cmd.py` の
`HostCommandBroker` は同等の処理ロジックを持つ。Phase 1 の拡張は両方に適用する必要がある。

共通ロジックの抽出を検討:

```
scripts/
  host_cmd_common.py     ← ビルトイン定義、サブコマンド抽出、パターン評価、env 解決
  orchestrator/
    host_cmd.py          ← async 版 broker (HostCommandBroker)、import host_cmd_common
  sandbox_exec.py        ← sync 版 broker (EmbeddedHostCommandBroker)、import host_cmd_common
  host-cmd               ← shim (変更: cwd 送信)
```

### パフォーマンス

broker 経由のオーバーヘッドは Unix socket + JSON シリアライズ。
1 リクエストあたり ~1ms 程度であり、CLI ツールの実行時間（数百ms〜数秒）に比べて無視できる。

ただし `git clone` 等で大量の stdout が発生する場合、
現行のプロトコル（全出力をメモリに載せて JSON で返す）はメモリ効率が悪い。
Phase 2 の時点では `git clone` はサンドボックス内で直接実行されることは稀
（worktree で作業するため）なので、当面は問題にならない。
将来的にストリーミング対応が必要になった場合は別途設計する。

### セキュリティモデルの変化

```
            現状                          移行後
            ----                          ------
ネットワーク  proxy allowlist で制御       proxy (最小) + broker (ツール単位)
ファイル      bwrap bind mount で制御     変更なし
認証情報      sandbox 内 env + mount      broker 側のみ（sandbox 内に露出しない）
コマンド      sandbox 内で直接実行        broker 経由でホスト側実行
```

信頼境界が「proxy のドメインリスト」から「broker のコマンド定義」に移動する。
broker の定義ミスが直接セキュリティホールになるため:

- ビルトインコマンド（git 等）はコードレビュー対象
- カスタムコマンドは `allowed_patterns` 必須（`"*"` も許容するが明示的に）
- `denied_patterns` は緩和不可（extra_denied で追加のみ）
