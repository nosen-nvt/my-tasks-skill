"""sandbox_exec - bwrap + netns によるサンドボックス実行ロジック."""

import fcntl
import json
import os
import subprocess
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
NS_NAME = "ai-ns"
LOCK_FILE = "/tmp/sandbox-netns.lock"
LISTEN_ADDR = "10.200.1.1"
HOME = Path.home()
UID = os.getuid()


# --- netns -------------------------------------------------------------------

def _netns_exists() -> bool:
    result = subprocess.run(["ip", "netns", "list"], capture_output=True, text=True)
    return NS_NAME in result.stdout.split()


def ensure_netns() -> None:
    """ai-ns ネットワーク名前空間が無ければ作成する (ファイルロック付き)."""
    if _netns_exists():
        return

    print("ネットワーク名前空間を初期化中...")
    fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if _netns_exists():
            return
        subprocess.run(["sudo", str(SCRIPT_DIR / "setup-netns")], check=True)
        print("ネットワーク名前空間の初期化が完了しました")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


# --- 組み込みプロファイル -----------------------------------------------------

BUILTIN_PROFILES: dict[str, dict] = {
    "default": {
        "profile_id": "default",
        "proxy_profile": "dev",
        "allowed_credentials": "*",
        "extra_binds": [
            {"source": "$HOME/.nuget",  "target": "$HOME/.nuget",  "mode": "rw"},
            {"source": "$HOME/.dotnet", "target": "$HOME/.dotnet", "mode": "rw"},
            {"source": "$HOME/.azcopy", "target": "$HOME/.azcopy", "mode": "rw"},
            {"source": "$HOME/.cache",  "target": "$HOME/.cache",  "mode": "rw"},
            {"source": "$HOME/go",      "target": "$HOME/go",      "mode": "rw"},
        ],
    },
    "unrestricted": {
        "profile_id": "unrestricted",
        "proxy_profile": None,
        "allowed_credentials": "*",
        "extra_binds": [
            {"source": "$HOME/.local",       "target": "$HOME/.local",       "mode": "rw"},
            {"source": "$HOME/.claude.json",  "target": "$HOME/.claude.json", "mode": "rw"},
            {"source": "$HOME/.cache",        "target": "$HOME/.cache",       "mode": "rw"},
            {"source": "$HOME/.volta",        "target": "$HOME/.volta",       "mode": "ro"},
            {"source": "/opt/google/chrome",  "target": "/opt/google/chrome", "mode": "ro"},
            {"source": "$HOME/go",            "target": "$HOME/go",           "mode": "rw"},
        ],
    },
}

SANDBOX_PROFILES_DIR = Path("~/.local/share/my-tasks/sandbox-profiles").expanduser()


# --- プロファイル解決 ---------------------------------------------------------

def resolve_profile(name_or_path: str) -> dict:
    """プロファイルを解決する。ファイルパス → sandbox-profiles/{name}.json → 組み込み の順。"""
    # ファイルパスとして存在すればそのまま読み込み
    p = Path(name_or_path)
    if p.is_file():
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    # sandbox-profiles ディレクトリから検索
    sp = SANDBOX_PROFILES_DIR / f"{name_or_path}.json"
    if sp.is_file():
        with open(sp, encoding="utf-8") as f:
            return json.load(f)

    # 組み込みプロファイル
    if name_or_path in BUILTIN_PROFILES:
        return BUILTIN_PROFILES[name_or_path]

    raise FileNotFoundError(f"サンドボックスプロファイルが見つかりません: {name_or_path}")


def resolve_mode(profile: dict) -> str:
    return "restricted" if profile.get("proxy_profile") else "unrestricted"


def resolve_extra_binds(profile: dict) -> list[str]:
    args: list[str] = []
    home = str(HOME)
    for bind in profile.get("extra_binds", []):
        src = bind["source"].replace("$HOME", home)
        tgt = bind["target"].replace("$HOME", home)
        flag = "--ro-bind" if bind.get("mode") == "ro" else "--bind"
        args += [flag, src, tgt]
    return args


# --- env ファイル読み込み -----------------------------------------------------

def load_env_files(paths: list[str]) -> list[str]:
    args: list[str] = []
    for path in paths:
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"env ファイルが見つかりません: {path}")
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            args += ["--setenv", key, value]
    return args


# --- bwrap 引数構築 -----------------------------------------------------------

def _base_binds() -> list[str]:
    """restricted / unrestricted 共通の OS レベルバインド."""
    return [
        "--ro-bind", "/etc", "/etc",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", str(SCRIPT_DIR / "pass-shim"), "/usr/bin/pass",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", "/run",
    ]


def _socket_binds() -> list[str]:
    """dispatcher / communicator / playwright のソケットバインド."""
    args: list[str] = []
    for name in ("my-tasks-dispatch", "communicator"):
        d = f"/run/user/{UID}/{name}"
        os.makedirs(d, exist_ok=True)
        args += ["--bind", d, d]
    pw_dir = "/tmp/playwright-cli"
    os.makedirs(pw_dir, exist_ok=True)
    args += ["--bind", pw_dir, pw_dir]
    return args


def _env_args(env_file_args: list[str], cred_args: list[str]) -> list[str]:
    return [
        "--clearenv",
        *env_file_args,
        *cred_args,
        "--setenv", "HOME", str(HOME),
        "--setenv", "TERM", "xterm-256color",
        "--setenv", "COLORTERM", "truecolor",
        "--setenv", "XDG_RUNTIME_DIR", f"/run/user/{UID}",
        "--setenv", "SANDBOX", "1",
    ]


def _cred_env() -> list[str]:
    args: list[str] = []
    for var in ("CRED_TOKEN", "CRED_BROKER_SOCK"):
        val = os.environ.get(var)
        if val:
            args += ["--setenv", var, val]
    return args


# --- モード別 bwrap 引数 ------------------------------------------------------

def build_restricted_args(
    work: str,
    proxy_port: int,
    profile_binds: list[str],
    env_file_args: list[str],
    cred_args: list[str],
    command: list[str],
) -> list[str]:
    resolv = tempfile.NamedTemporaryFile(
        prefix="sandbox-resolv.", dir="/tmp", mode="w", delete=False,
    )
    resolv.write(f"nameserver {LISTEN_ADDR}\n")
    resolv.close()

    return [
        "sudo", "ip", "netns", "exec", NS_NAME,
        "sudo", "-u", os.environ["USER"],
        "bwrap",
        "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--die-with-parent",
        *_base_binds(),
        "--ro-bind", "/opt/microsoft/powershell", "/opt/microsoft/powershell",
        "--ro-bind", "/opt/google/chrome", "/opt/google/chrome",
        "--ro-bind", resolv.name, "/run/systemd/resolve/stub-resolv.conf",
        *_socket_binds(),
        "--bind", str(HOME), str(HOME),
        "--ro-bind", f"{HOME}/src", f"{HOME}/src",
        "--bind", f"{HOME}/.claude", f"{HOME}/.claude",
        "--bind", work, work,
        "--bind", f"{HOME}/.local/share/my-tasks", f"{HOME}/.local/share/my-tasks",
        *profile_binds,
        "--chdir", work,
        *_env_args(env_file_args, cred_args),
        "--setenv", "PATH", f"/usr/bin:{HOME}/.local/bin:{HOME}/go/bin:{HOME}/.bun/bin:{HOME}/.volta/bin",
        "--setenv", "http_proxy", f"http://{LISTEN_ADDR}:{proxy_port}",
        "--setenv", "https_proxy", f"http://{LISTEN_ADDR}:{proxy_port}",
        "--setenv", "WORKDIR", work,
        "--setenv", "GOOGLE_CHAT_WEBHOOK_URL", os.environ.get("GOOGLE_CHAT_WEBHOOK_URL", ""),
        "--cap-drop", "ALL",
        *command,
    ]


def build_unrestricted_args(
    work: str,
    profile_binds: list[str],
    env_file_args: list[str],
    cred_args: list[str],
    command: list[str],
) -> list[str]:
    return [
        "bwrap",
        "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--die-with-parent",
        *_base_binds(),
        "--ro-bind", "/run/systemd/resolve", "/run/systemd/resolve",
        *_socket_binds(),
        "--bind", f"{HOME}/.claude", f"{HOME}/.claude",
        "--ro-bind", f"{HOME}/src/github.com/nosen-nvt/my-tasks-skill",
                     f"{HOME}/src/github.com/nosen-nvt/my-tasks-skill",
        "--bind", work, work,
        *profile_binds,
        "--chdir", work,
        *_env_args(env_file_args, cred_args),
        "--setenv", "PATH", f"/usr/bin:{HOME}/.local/bin:{HOME}/.volta/bin:{HOME}/go/bin",
        "--setenv", "WORKDIR", work,
        "--cap-drop", "ALL",
        *command,
    ]


# --- 公開 API ----------------------------------------------------------------

def run(
    *,
    proxy_port: int = 3128,
    sandbox_profile: str = "default",
    env_files: list[str] | None = None,
    command: list[str] | None = None,
) -> None:
    """サンドボックスを構築し、exec で置き換える."""
    work = os.getcwd()

    profile = resolve_profile(sandbox_profile)
    mode = resolve_mode(profile)
    profile_binds = resolve_extra_binds(profile)

    if command is None:
        command = [f"{HOME}/.local/bin/claude", "--permission-mode", "bypassPermissions"]

    env_file_args = load_env_files(env_files or [])
    cred_args = _cred_env()

    if mode == "restricted":
        ensure_netns()
        exec_args = build_restricted_args(work, proxy_port, profile_binds, env_file_args, cred_args, command)
    else:
        exec_args = build_unrestricted_args(work, profile_binds, env_file_args, cred_args, command)

    os.execvp(exec_args[0], exec_args)
