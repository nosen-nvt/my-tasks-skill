"""host_cmd_common - ホストコマンドのビルトイン定義・検証ロジック共通モジュール."""

import copy
import fnmatch
import os
import subprocess


# --- ビルトインホストコマンド定義 -----------------------------------------------

BUILTIN_HOST_COMMANDS: dict[str, dict] = {
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

_DEFAULT_CWD_PREFIXES = [f"/home/{os.environ.get('USER', 'nobody')}/src"]


# --- ビルトイン解決 -----------------------------------------------------------

def resolve_builtin(cmd_def: dict) -> dict:
    """builtin: true のコマンド定義をビルトイン定義で解決する。

    builtin フラグがない場合はそのまま返す。
    extra_denied_patterns で制約の追加のみ許可。
    env はマージ（プロファイル側が優先）。
    """
    if not cmd_def.get("builtin"):
        return cmd_def
    name = cmd_def["name"]
    builtin = BUILTIN_HOST_COMMANDS.get(name)
    if not builtin:
        raise ValueError(f"unknown builtin command: {name}")
    resolved = {"name": name, **copy.deepcopy(builtin)}
    extra = cmd_def.get("extra_denied_patterns", [])
    if extra:
        resolved["denied_patterns"] = resolved.get("denied_patterns", []) + list(extra)
    if "env" in cmd_def:
        resolved["env"] = {**resolved.get("env", {}), **cmd_def["env"]}
    return resolved


# --- サブコマンド抽出 ---------------------------------------------------------

def extract_git_subcommand(args: list[str]) -> str | None:
    """git のグローバルオプションをスキップしてサブコマンドを抽出する。"""
    GLOBAL_OPTS_WITH_VALUE = {
        "-C", "-c", "--git-dir", "--work-tree",
        "--namespace", "--config-env",
    }
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


# --- コマンド引数検証 ---------------------------------------------------------

def check_command_args(cmd_def: dict, args: list[str]) -> str | None:
    """コマンド引数を検証する。

    評価順序 (allowed_subcommands 定義時):
        1. サブコマンド抽出 → allowed_subcommands に含まれなければ拒否
        2. denied_patterns にマッチ → 拒否
        3. allowed_patterns を評価 (未定義 or "*" なら許可)
        4. いずれにもマッチしない → 拒否

    Returns:
        None (許可) or エラーメッセージ文字列 (拒否)。
    """
    args_str = " ".join(args)
    allowed_subcommands = cmd_def.get("allowed_subcommands")

    # 1. allowed_subcommands チェック
    if allowed_subcommands is not None:
        subcmd = extract_git_subcommand(args)
        if subcmd is None or subcmd not in allowed_subcommands:
            return f"subcommand not allowed: {subcmd}"

    # 2. denied_patterns チェック (deny-first)
    denied = cmd_def.get("denied_patterns", [])
    if any(fnmatch.fnmatch(args_str, pat) for pat in denied):
        return f"args denied by pattern: {args_str}"

    # 3. allowed_patterns チェック
    allowed = cmd_def.get("allowed_patterns")
    # allowed_subcommands が定義済みで allowed_patterns 未定義 → 許可
    if allowed is None and allowed_subcommands is not None:
        return None
    if allowed == "*":
        return None
    if isinstance(allowed, list):
        if any(fnmatch.fnmatch(args_str, pat) for pat in allowed):
            return None
    elif isinstance(allowed, str):
        if fnmatch.fnmatch(args_str, allowed):
            return None

    return f"args not allowed: {cmd_def.get('name', 'unknown')} {args_str}"


# --- cwd 検証 ----------------------------------------------------------------

def validate_cwd(cwd: str | None, cmd_def: dict,
                 extra_prefixes: list[str] | None = None) -> str | None:
    """cwd を検証する。

    require_cwd: true のコマンドは cwd 必須。
    バリデーション: 絶対パス、実在ディレクトリ、許可 prefix 内。

    Returns:
        None (OK) or エラーメッセージ文字列。
    """
    require = cmd_def.get("require_cwd", False)
    if require and not cwd:
        return "cwd is required for this command"
    if not cwd:
        return None
    if not os.path.isabs(cwd):
        return f"cwd must be absolute path: {cwd}"
    real = os.path.realpath(cwd)
    if not os.path.isdir(real):
        return f"cwd is not a directory: {cwd}"
    if require:
        prefixes = list(_DEFAULT_CWD_PREFIXES)
        if extra_prefixes:
            prefixes.extend(extra_prefixes)
        if not any(real.startswith(p) for p in prefixes):
            return f"cwd not in allowed prefix: {cwd}"
    return None


# --- コマンド env 解決 --------------------------------------------------------

def resolve_cmd_env_sync(env: dict | None) -> dict[str, str]:
    """コマンド定義の env フィールドを同期的に解決する。

    文字列値はそのまま設定。
    {"pass": "..."} 形式は pass show で解決する。
    """
    if not env:
        return {}
    resolved: dict[str, str] = {}
    for key, value in env.items():
        if isinstance(value, dict) and "pass" in value:
            proc = subprocess.run(
                ["pass", "show", value["pass"]],
                capture_output=True,
            )
            if proc.returncode == 0:
                resolved[key] = proc.stdout.decode().splitlines()[0]
        else:
            resolved[key] = str(value)
    return resolved


# --- コマンド定義マージ -------------------------------------------------------

def merge_command_defs(matching_defs: list[dict]) -> dict:
    """同名コマンドの複数定義をマージする。

    - allowed_patterns: union ("*" があれば全許可)
    - denied_patterns: union (制約は緩和しない)
    - allowed_subcommands: intersection (最も制限的)
    - allow_stdin: いずれかが True なら True
    - require_cwd: いずれかが True なら True
    - _resolved_env: マージ (後の定義が優先)
    """
    if len(matching_defs) == 1:
        return matching_defs[0]

    merged = dict(matching_defs[0])

    all_patterns: list | str = []
    allow_stdin = False
    all_denied: list[str] = []
    all_subcommands: set[str] | None = None
    merged_env: dict[str, str] = {}
    require_cwd = False

    for d in matching_defs:
        # allowed_patterns
        p = d.get("allowed_patterns", [])
        if p == "*":
            all_patterns = "*"
        elif isinstance(all_patterns, list):
            if isinstance(p, list):
                all_patterns.extend(p)
            elif isinstance(p, str):
                all_patterns.append(p)

        # allow_stdin
        if d.get("allow_stdin", False):
            allow_stdin = True

        # denied_patterns (union)
        all_denied.extend(d.get("denied_patterns", []))

        # allowed_subcommands (intersection)
        if "allowed_subcommands" in d:
            sc = set(d["allowed_subcommands"])
            all_subcommands = sc if all_subcommands is None else all_subcommands & sc

        # _resolved_env (merge)
        merged_env.update(d.get("_resolved_env", {}))

        # require_cwd
        if d.get("require_cwd"):
            require_cwd = True

    merged["allowed_patterns"] = all_patterns
    merged["allow_stdin"] = allow_stdin
    merged["require_cwd"] = require_cwd
    if all_denied:
        merged["denied_patterns"] = all_denied
    if all_subcommands is not None:
        merged["allowed_subcommands"] = list(all_subcommands)
    if merged_env:
        merged["_resolved_env"] = merged_env

    return merged
