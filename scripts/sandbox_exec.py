"""sandbox_exec - bwrap + netns によるサンドボックス実行ロジック."""

import fcntl
import fnmatch
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
NS_NAME = "ai-ns"
LOCK_FILE = "/tmp/sandbox-netns.lock"
LISTEN_ADDR = "10.200.1.1"
HOME = Path.home()
UID = os.getuid()
CACHE_DIR = Path("~/.local/share/my-tasks/.cache").expanduser()
DEFAULT_REPO = Path("~/.local/share/my-tasks").expanduser()
PROXY_PROFILES_DIR = Path("~/.local/share/my-tasks/proxy-profiles").expanduser()
CLAUDE_JSON = HOME / ".claude" / "claude.json"


# --- trust dialog 事前承認 ---------------------------------------------------

def ensure_trust_accepted(working_dir: str) -> None:
    """claude.json のワークスペース trust dialog を事前に受け入れ済みにする。

    Claude Code はインタラクティブモードで起動時、ワークスペースごとに
    trust dialog を表示する。-p モードではスキップされるが、対話セッションでは
    表示される。この関数で事前に hasTrustDialogAccepted を True にしておくことで
    sandbox 内の対話セッションでもダイアログをスキップできる。
    """
    if not CLAUDE_JSON.is_file():
        return
    try:
        with open(CLAUDE_JSON, encoding="utf-8") as f:
            data = json.load(f)
        projects = data.setdefault("projects", {})
        entry = projects.setdefault(working_dir, {})
        if entry.get("hasTrustDialogAccepted"):
            return
        entry["hasTrustDialogAccepted"] = True
        tmp = CLAUDE_JSON.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        tmp.replace(CLAUDE_JSON)
    except (json.JSONDecodeError, OSError):
        pass


# --- プロジェクト読み込み -----------------------------------------------------

def load_project(project_id: str, repo_dir: Path = DEFAULT_REPO) -> dict | None:
    path = repo_dir / "projects" / f"{project_id}.json"
    if path.is_file():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


# --- netns -------------------------------------------------------------------

def _netns_exists() -> bool:
    result = subprocess.run(["ip", "netns", "list"], capture_output=True, text=True)
    return NS_NAME in result.stdout.split()


def _netns_ready() -> bool:
    """netns が存在し、内部の nft ルールも設定済みか確認する。"""
    if not _netns_exists():
        return False
    result = subprocess.run(
        ["sudo", "ip", "netns", "exec", NS_NAME, "nft", "list", "table", "inet", "filter"],
        capture_output=True,
    )
    return result.returncode == 0


def _teardown_netns() -> None:
    """既存の netns を削除して setup-netns で再作成できるようにする。"""
    subprocess.run(["sudo", "ip", "netns", "del", NS_NAME], check=True)
    # veth ペアは netns 削除時に自動削除される


def ensure_netns() -> None:
    """ai-ns ネットワーク名前空間を準備する (ファイルロック付き)."""
    if _netns_ready():
        return

    print("ネットワーク名前空間を初期化中...")
    fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if _netns_ready():
            return
        if _netns_exists():
            print("nft ルールが消失しています。再作成します...")
            _teardown_netns()
        subprocess.run(["sudo", str(SCRIPT_DIR / "setup-netns")], check=True)
        print("ネットワーク名前空間の初期化が完了しました")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


# --- 組み込みプロファイル -----------------------------------------------------

BUILTIN_PROFILES: dict[str, dict] = {
    "default": {
        "profile_id": "default",
        "proxy_profile": "full",
        "host_commands": [
            {"name": "pass", "path": "/usr/bin/pass", "allowed_patterns": "*", "allow_stdin": True},
        ],
        "extra_binds": [
            {"source": "$HOME/.nuget",  "target": "$HOME/.nuget",  "mode": "rw"},
            {"source": "$HOME/.dotnet", "target": "$HOME/.dotnet", "mode": "rw"},
            {"source": "$HOME/.azcopy", "target": "$HOME/.azcopy", "mode": "rw"},
            {"source": "$HOME/go",      "target": "$HOME/go",      "mode": "rw"},
        ],
    },
    "unrestricted": {
        "profile_id": "unrestricted",
        "proxy_profile": None,
        "host_commands": [
            {"name": "pass", "path": "/usr/bin/pass", "allowed_patterns": "*", "allow_stdin": True},
        ],
        "extra_binds": [
            {"source": "$HOME/.local",       "target": "$HOME/.local",       "mode": "rw"},
            {"source": "$HOME/.claude.json",  "target": "$HOME/.claude.json", "mode": "rw"},
            {"source": "$HOME/.cache",        "target": "$HOME/.cache",       "mode": "rw"},
            {"source": "$HOME/.volta",        "target": "$HOME/.volta",       "mode": "ro"},
            {"source": "/opt/google/chrome",  "target": "/opt/google/chrome", "mode": "ro"},
            {"source": "$HOME/go",            "target": "$HOME/go",           "mode": "rw"},
            {"source": "/tmp/playwright-cli",  "target": "/tmp/playwright-cli", "mode": "rw"},
        ],
    },
}

SANDBOX_PROFILES_DIR = Path("~/.local/share/my-tasks/sandbox-profiles").expanduser()


# --- ホストコマンド解決 -------------------------------------------------------

def resolve_host_commands(profile: dict) -> list[dict]:
    """サンドボックスプロファイルから host_commands を解決する。"""
    return profile.get("host_commands", [])


def _host_cmd_binds(host_commands: list[dict]) -> list[str]:
    """host_commands リストに基づく bwrap bind 引数を生成する。"""
    shim = str(SCRIPT_DIR / "host-cmd")
    args: list[str] = []
    seen: set[str] = set()
    for cmd in host_commands:
        name = cmd["name"]
        if name not in seen:
            seen.add(name)
            args += ["--ro-bind", shim, f"/usr/bin/{name}"]
    return args


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


def uses_network_protection(profile: dict) -> bool:
    return bool(profile.get("proxy_profile"))


def resolve_proxy_port(profile: dict) -> int | None:
    """サンドボックスプロファイルの proxy_profile からポートを解決する。"""
    proxy_profile_id = profile.get("proxy_profile")
    if not proxy_profile_id:
        return None
    pp = PROXY_PROFILES_DIR / f"{proxy_profile_id}.json"
    if pp.is_file():
        with open(pp, encoding="utf-8") as f:
            return json.load(f).get("port")
    return None


def resolve_host_forward_ports(profile: dict) -> list[int]:
    """プロファイルから host_forward_ports を取得する。"""
    return [int(p) for p in profile.get("host_forward_ports", [])]


def resolve_extra_binds(binds: list[dict]) -> list[str]:
    """extra_binds リストを bwrap 引数に変換する。"""
    args: list[str] = []
    home = str(HOME)
    for bind in binds:
        src = bind["source"].replace("$HOME", home)
        tgt = bind["target"].replace("$HOME", home)
        flag = "--ro-bind" if bind.get("mode") == "ro" else "--bind"
        if not os.path.exists(src):
            os.makedirs(src, exist_ok=True)
        args += [flag, src, tgt]
    return args


# --- プロジェクト env 解決 ----------------------------------------------------

def _resolve_env_dict(env_dict: dict | None, cache_id: str) -> Path | None:
    """env ディクショナリを同期的に解決し、キャッシュディレクトリに書き出す。

    Returns:
        生成した env ファイルのパス。env_dict が空の場合は None。
    """
    if not env_dict:
        return None

    resolved: dict[str, str] = {}
    for key, value in env_dict.items():
        if isinstance(value, dict) and "pass" in value:
            proc = subprocess.run(
                ["pass", "show", value["pass"]],
                capture_output=True,
            )
            if proc.returncode != 0:
                print(f"警告: pass show failed for {value['pass']}: {proc.stderr.decode().strip()}", file=sys.stderr)
                continue
            resolved[key] = proc.stdout.decode().splitlines()[0]
        else:
            resolved[key] = str(value)

    if not resolved:
        return None

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    env_path = CACHE_DIR / f"env-{cache_id}.env"
    env_path.write_text(
        "\n".join(f"{k}={v}" for k, v in resolved.items()) + "\n",
        encoding="utf-8",
    )
    os.chmod(env_path, 0o600)
    return env_path


def resolve_project_env_sync(project: dict) -> Path | None:
    """プロジェクトの env フィールドを同期的に解決し、キャッシュディレクトリに書き出す。"""
    return _resolve_env_dict(project.get("env"), project["project_id"])


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


# --- ホストポートフォワーディング -----------------------------------------------

def _ensure_prerouting_chain() -> None:
    """nat テーブルに prerouting チェーンが無ければ作成する。"""
    result = subprocess.run(
        ["sudo", "nft", "list", "chain", "ip", "nat", "prerouting"],
        capture_output=True,
    )
    if result.returncode != 0:
        subprocess.run([
            "sudo", "nft", "add", "chain", "ip", "nat", "prerouting",
            "{ type nat hook prerouting priority -100 ; }",
        ], check=True)


def _ensure_ns_nat_chains() -> None:
    """ai-ns 内の nat テーブルと output/postrouting チェーンが無ければ作成する。"""
    result = subprocess.run(
        ["sudo", "ip", "netns", "exec", NS_NAME,
         "nft", "list", "table", "ip", "nat"],
        capture_output=True,
    )
    if result.returncode != 0:
        subprocess.run([
            "sudo", "ip", "netns", "exec", NS_NAME,
            "nft", "add", "table", "ip", "nat",
        ], check=True)
    for chain, hook, prio in [
        ("output", "output", "-100"),
        ("postrouting", "postrouting", "100"),
    ]:
        res = subprocess.run(
            ["sudo", "ip", "netns", "exec", NS_NAME,
             "nft", "list", "chain", "ip", "nat", chain],
            capture_output=True,
        )
        if res.returncode != 0:
            subprocess.run([
                "sudo", "ip", "netns", "exec", NS_NAME,
                "nft", "add", "chain", "ip", "nat", chain,
                f"{{ type nat hook {hook} priority {prio} ; }}",
            ], check=True)


def setup_host_port_forwarding(ports: list[int]) -> None:
    """ai-ns 内の 127.0.0.1:port をホスト側 localhost に透過転送する。

    3 段階の NAT で実現:
      ① ai-ns nat OUTPUT:      dst 127.0.0.1 → 10.200.1.1 (DNAT)
      ② ai-ns nat POSTROUTING: src 127.0.0.1 → 10.200.1.2 (masquerade)
      ③ ホスト nat PREROUTING:  dst 10.200.1.1 → 127.0.0.1 (DNAT)
    応答は conntrack が自動逆変換する。
    """
    _ensure_prerouting_chain()
    _ensure_ns_nat_chains()
    # route_localnet: veth 経由の 127.0.0.0/8 パケットを許可
    subprocess.run([
        "sudo", "sysctl", "-w", "net.ipv4.conf.veth-host.route_localnet=1",
    ], check=True, capture_output=True)
    subprocess.run([
        "sudo", "ip", "netns", "exec", NS_NAME,
        "sysctl", "-w", "net.ipv4.conf.veth-ai.route_localnet=1",
    ], check=True, capture_output=True)
    # ② ai-ns postrouting masquerade (src=127.0.0.1 → veth-ai IP)
    subprocess.run([
        "sudo", "ip", "netns", "exec", NS_NAME,
        "nft", "add", "rule", "ip", "nat", "postrouting",
        "ip", "saddr", "127.0.0.1", "oifname", "veth-ai",
        "masquerade",
    ], check=True)
    for port in ports:
        # ① ai-ns output DNAT (dst 127.0.0.1:port → LISTEN_ADDR:port)
        subprocess.run([
            "sudo", "ip", "netns", "exec", NS_NAME,
            "nft", "add", "rule", "ip", "nat", "output",
            "ip", "daddr", "127.0.0.1",
            "tcp", "dport", str(port),
            "dnat", "to", LISTEN_ADDR,
        ], check=True)
        # ai-ns filter output 許可
        subprocess.run([
            "sudo", "ip", "netns", "exec", NS_NAME,
            "nft", "add", "rule", "inet", "filter", "output",
            "ip", "daddr", LISTEN_ADDR,
            "tcp", "dport", str(port),
            "accept",
        ], check=True)
        # ③ ホスト側 prerouting DNAT (dst LISTEN_ADDR:port → 127.0.0.1)
        subprocess.run([
            "sudo", "nft", "add", "rule", "ip", "nat", "prerouting",
            "iifname", "veth-host",
            "tcp", "dport", str(port),
            "dnat", "to", "127.0.0.1",
        ], check=True)


# --- bwrap 引数構築 -----------------------------------------------------------

def _base_binds() -> list[str]:
    """全モード共通の OS レベルバインド."""
    return [
        "--ro-bind", "/etc", "/etc",
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", "/run",
    ]


def _my_tasks_binds() -> list[str]:
    """my-tasks データディレクトリのバインド（設定ディレクトリは ro で保護）."""
    base = f"{HOME}/.local/share/my-tasks"
    return [
        "--bind", base, base,
        "--ro-bind-try", f"{base}/projects", f"{base}/projects",
        "--ro-bind-try", f"{base}/sandbox-profiles", f"{base}/sandbox-profiles",
        "--ro-bind-try", f"{base}/proxy-profiles", f"{base}/proxy-profiles",
    ]


def _socket_binds() -> list[str]:
    """dispatcher ソケットバインド."""
    d = f"/run/user/{UID}/my-tasks-dispatch"
    os.makedirs(d, exist_ok=True)
    return ["--bind", d, d]


def _env_args(env_file_args: list[str], broker_args: list[str]) -> list[str]:
    return [
        "--clearenv",
        *env_file_args,
        *broker_args,
        "--setenv", "HOME", str(HOME),
        "--setenv", "TERM", "xterm-256color",
        "--setenv", "COLORTERM", "truecolor",
        "--setenv", "XDG_RUNTIME_DIR", f"/run/user/{UID}",
        "--setenv", "SANDBOX", "1",
    ]


# --- 組み込み Host Command Broker ---------------------------------------------

class EmbeddedHostCommandBroker:
    """HOST_CMD_TOKEN 未設定時に自動起動するフォールバック Host Command Broker."""

    def __init__(self, sock_path: str, token: str, host_commands: list[dict]):
        self._sock_path = sock_path
        self._token = token
        self._host_commands = host_commands
        self._running = False
        self._server_sock: socket.socket | None = None

    def start(self) -> None:
        os.makedirs(os.path.dirname(self._sock_path), exist_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self._sock_path)
        os.chmod(self._sock_path, 0o600)
        srv.listen(4)
        srv.settimeout(1.0)
        self._server_sock = srv
        self._running = True
        t = threading.Thread(target=self._serve, daemon=True)
        t.start()

    def _serve(self) -> None:
        srv = self._server_sock
        assert srv is not None
        while self._running:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            if not data:
                return
            request = json.loads(data.decode())
            token = request.get("token", "")
            command = request.get("command", "")
            args = request.get("args", [])
            stdin = request.get("stdin")
            response = self._process(token, command, args, stdin)
            conn.sendall(json.dumps(response, ensure_ascii=False).encode() + b"\n")
        except Exception:
            try:
                conn.sendall(json.dumps({"ok": False, "error": "internal error"}).encode() + b"\n")
            except Exception:
                pass
        finally:
            conn.close()

    def _process(self, token: str, command: str, args: list[str], stdin: str | None) -> dict:
        if not token or token != self._token:
            return {"ok": False, "error": "invalid token"}

        matching_defs = [c for c in self._host_commands if c["name"] == command]
        if not matching_defs:
            return {"ok": False, "error": f"command not allowed: {command}"}

        # 同名コマンドの allowed_patterns をマージ
        cmd_def = matching_defs[0]
        all_patterns: list | str = []
        allow_stdin = False
        for d in matching_defs:
            allow_stdin = allow_stdin or d.get("allow_stdin", False)
            p = d.get("allowed_patterns", [])
            if p == "*":
                all_patterns = "*"
                break
            if isinstance(all_patterns, list):
                all_patterns.extend(p)

        if all_patterns != "*":
            args_str = " ".join(args)
            if not any(fnmatch.fnmatch(args_str, pat) for pat in all_patterns):
                return {"ok": False, "error": f"args not allowed: {command} {args_str}"}

        if stdin is not None and not allow_stdin:
            return {"ok": False, "error": f"stdin not allowed for {command}"}

        try:
            proc = subprocess.run(
                [cmd_def["path"], *args],
                input=stdin.encode() if stdin is not None else None,
                capture_output=True,
            )
            return {
                "ok": True,
                "exit_code": proc.returncode,
                "stdout": proc.stdout.decode(),
                "stderr": proc.stderr.decode(),
            }
        except Exception:
            return {"ok": False, "error": "execution failed"}

    def stop(self) -> None:
        self._running = False
        if self._server_sock:
            self._server_sock.close()
        try:
            os.unlink(self._sock_path)
        except FileNotFoundError:
            pass


# --- モード別 bwrap 引数 ------------------------------------------------------

def build_netns_args(
    work: str,
    proxy_port: int,
    profile_binds: list[str],
    env_file_args: list[str],
    broker_args: list[str],
    command: list[str],
    host_forward_ports: list[int] | None = None,
    host_cmd_binds: list[str] | None = None,
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
        *(host_cmd_binds or []),
        "--ro-bind", "/opt/microsoft/powershell", "/opt/microsoft/powershell",
        "--ro-bind", "/opt/google/chrome", "/opt/google/chrome",
        "--ro-bind", resolv.name, "/run/systemd/resolve/stub-resolv.conf",
        *_socket_binds(),
        "--tmpfs", str(HOME),
        "--dir", f"{HOME}/.local/bin",
        "--dir", f"{HOME}/.local/share",
        "--dir", f"{HOME}/.local/state",
        "--dir", f"{HOME}/.cache",
        "--ro-bind-try", f"{HOME}/.gitconfig", f"{HOME}/.gitconfig",
        "--ro-bind-try", f"{HOME}/.ssh", f"{HOME}/.ssh",
        "--ro-bind", f"{HOME}/src", f"{HOME}/src",
        "--bind", f"{HOME}/.claude", f"{HOME}/.claude",
        "--ro-bind-try", f"{HOME}/.local/bin", f"{HOME}/.local/bin",
        "--ro-bind-try", f"{HOME}/.local/share/claude", f"{HOME}/.local/share/claude",
        "--ro-bind-try", f"{HOME}/.local/share/go", f"{HOME}/.local/share/go",
        "--bind-try", f"{HOME}/.local/state/claude", f"{HOME}/.local/state/claude",
        "--bind-try", f"{HOME}/.cache", f"{HOME}/.cache",
        "--bind-try", f"{HOME}/.claude.json", f"{HOME}/.claude.json",
        # シェル設定
        "--ro-bind-try", f"{HOME}/.bashrc", f"{HOME}/.bashrc",
        "--ro-bind-try", f"{HOME}/.profile", f"{HOME}/.profile",
        "--ro-bind-try", f"{HOME}/.inputrc", f"{HOME}/.inputrc",
        # 開発ツールチェーン (ro ベースライン — プロファイルで rw 上書き可)
        "--ro-bind-try", f"{HOME}/.volta", f"{HOME}/.volta",
        "--ro-bind-try", f"{HOME}/.bun", f"{HOME}/.bun",
        "--ro-bind-try", f"{HOME}/.npm", f"{HOME}/.npm",
        "--ro-bind-try", f"{HOME}/.dotnet", f"{HOME}/.dotnet",
        "--ro-bind-try", f"{HOME}/.nuget", f"{HOME}/.nuget",
        "--ro-bind-try", f"{HOME}/.bicep", f"{HOME}/.bicep",
        "--ro-bind-try", f"{HOME}/.templateengine", f"{HOME}/.templateengine",
        "--ro-bind-try", f"{HOME}/go", f"{HOME}/go",
        "--ro-bind-try", f"{HOME}/.config/git", f"{HOME}/.config/git",
        "--ro-bind-try", f"{HOME}/.config/go", f"{HOME}/.config/go",
        "--ro-bind-try", f"{HOME}/.config/uv", f"{HOME}/.config/uv",
        *profile_binds,
        *(["--bind", work, work] if work != str(HOME) else []),
        *_my_tasks_binds(),
        "--chdir", work,
        *_env_args(env_file_args, broker_args),
        "--setenv", "PATH", f"/usr/bin:{HOME}/.local/bin:{HOME}/go/bin:{HOME}/.bun/bin:{HOME}/.volta/bin",
        "--setenv", "http_proxy", f"http://{LISTEN_ADDR}:{proxy_port}",
        "--setenv", "https_proxy", f"http://{LISTEN_ADDR}:{proxy_port}",
        *(["--setenv", "no_proxy", f"{LISTEN_ADDR},localhost,127.0.0.1"] if host_forward_ports else []),
        "--setenv", "SANDBOX_HOST", LISTEN_ADDR,
        "--setenv", "WORKDIR", work,
        "--setenv", "GOOGLE_CHAT_WEBHOOK_URL", os.environ.get("GOOGLE_CHAT_WEBHOOK_URL", ""),
        "--cap-drop", "ALL",
        *command,
    ]


def build_host_network_args(
    work: str,
    profile_binds: list[str],
    env_file_args: list[str],
    broker_args: list[str],
    command: list[str],
    host_cmd_binds: list[str] | None = None,
) -> list[str]:
    return [
        "bwrap",
        "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--die-with-parent",
        *_base_binds(),
        *(host_cmd_binds or []),
        "--ro-bind", "/run/systemd/resolve", "/run/systemd/resolve",
        *_socket_binds(),
        "--bind", f"{HOME}/.claude", f"{HOME}/.claude",
        "--ro-bind", f"{HOME}/src/github.com/nosen-nvt/my-tasks-skill",
                     f"{HOME}/src/github.com/nosen-nvt/my-tasks-skill",
        *profile_binds,
        *(["--bind", work, work] if work != str(HOME) else []),
        "--chdir", work,
        *_env_args(env_file_args, broker_args),
        "--setenv", "PATH", f"/usr/bin:{HOME}/.local/bin:{HOME}/.volta/bin:{HOME}/go/bin",
        "--setenv", "WORKDIR", work,
        "--cap-drop", "ALL",
        *command,
    ]


# --- 公開 API ----------------------------------------------------------------

def resolve_project_sandbox_params(
    project: dict,
    *,
    sandbox_profile_override: str | None = None,
) -> tuple[str, list[str], list[dict], list[dict]]:
    """プロジェクト dict からサンドボックス実行に必要なパラメータを解決する。

    Returns:
        (sandbox_profile_id, env_files, host_commands, extra_binds)
        host_commands と extra_binds はプロファイルとプロジェクトの定義をマージした結果。
    """
    sandbox_profile_id = sandbox_profile_override or project.get("sandbox_profile", "default")
    profile = resolve_profile(sandbox_profile_id)

    env_files: list[str] = []
    profile_env_file = _resolve_env_dict(profile.get("env"), f"profile-{sandbox_profile_id}")
    if profile_env_file:
        env_files.append(str(profile_env_file))
    project_env_file = resolve_project_env_sync(project)
    if project_env_file:
        env_files.append(str(project_env_file))
    working_dir = project.get("working_directory", "")
    if working_dir:
        dotenv = Path(working_dir) / ".env"
        if dotenv.is_file():
            env_files.append(str(dotenv))

    host_commands = resolve_host_commands(profile) + project.get("host_commands", [])
    extra_binds = profile.get("extra_binds", []) + project.get("extra_binds", [])
    return sandbox_profile_id, env_files, host_commands, extra_binds


def build_exec_args(
    *,
    sandbox_profile: str = "default",
    proxy_port: int | None = None,
    env_files: list[str] | None = None,
    broker_env: dict[str, str] | None = None,
    host_commands: list[dict] | None = None,
    extra_binds: list[dict] | None = None,
    command: list[str] | None = None,
    working_dir: str | None = None,
    dispatch_id: str | None = None,
) -> list[str]:
    """bwrap 実行コマンド引数を構築して返す。netns の準備も行う。"""
    work = working_dir or os.getcwd()

    profile = resolve_profile(sandbox_profile)
    network_protected = uses_network_protection(profile)
    profile_binds = resolve_extra_binds(
        extra_binds if extra_binds is not None else profile.get("extra_binds", [])
    )

    if command is None:
        command = [f"{HOME}/.local/bin/claude", "--dangerously-skip-permissions"]

    env_file_args = load_env_files(env_files or [])

    broker_args: list[str] = []
    if broker_env:
        for k, v in broker_env.items():
            broker_args += ["--setenv", k, v]

    hc_binds = _host_cmd_binds(host_commands or [])

    if dispatch_id:
        dispatch_dir = f"/run/user/{UID}/my-tasks-dispatch"
        command = [str(SCRIPT_DIR / "job-wrapper"), dispatch_dir, dispatch_id] + command

    if proxy_port is None:
        proxy_port = resolve_proxy_port(profile) or 3128

    host_forward_ports = resolve_host_forward_ports(profile)

    if network_protected:
        ensure_netns()
        if host_forward_ports:
            setup_host_port_forwarding(host_forward_ports)
        return build_netns_args(work, proxy_port, profile_binds, env_file_args, broker_args, command, host_forward_ports, hc_binds)
    else:
        return build_host_network_args(work, profile_binds, env_file_args, broker_args, command, hc_binds)


def run(
    *,
    proxy_port: int | None = None,
    sandbox_profile: str = "default",
    env_files: list[str] | None = None,
    host_commands: list[dict] | None = None,
    extra_binds: list[dict] | None = None,
    command: list[str] | None = None,
    working_dir: str | None = None,
) -> None:
    """サンドボックスを構築し、exec で置き換える."""
    ensure_trust_accepted(working_dir or os.getcwd())
    profile = resolve_profile(sandbox_profile)
    if host_commands is None:
        host_commands = resolve_host_commands(profile)

    # dispatcher 経由でない場合: 組み込み Host Command Broker を起動
    broker_env: dict[str, str] | None = None
    broker = None
    if not os.environ.get("HOST_CMD_TOKEN"):
        if host_commands:
            sock_path = f"/run/user/{UID}/my-tasks-dispatch/host-cmd-broker-{os.getpid()}.sock"
            token = uuid.uuid4().hex
            broker = EmbeddedHostCommandBroker(sock_path, token, host_commands)
            broker.start()
            broker_env = {"HOST_CMD_TOKEN": token, "HOST_CMD_BROKER_SOCK": sock_path}
    else:
        broker_env = {k: os.environ[k] for k in ("HOST_CMD_TOKEN", "HOST_CMD_BROKER_SOCK") if k in os.environ}

    exec_args = build_exec_args(
        sandbox_profile=sandbox_profile,
        proxy_port=proxy_port,
        env_files=env_files,
        broker_env=broker_env,
        host_commands=host_commands,
        extra_binds=extra_binds,
        command=command,
        working_dir=working_dir,
    )

    if broker:
        proc = subprocess.Popen(exec_args)
        signal.signal(signal.SIGTERM, lambda *_: proc.terminate())
        signal.signal(signal.SIGINT, lambda *_: proc.send_signal(signal.SIGINT))
        proc.wait()
        broker.stop()
        sys.exit(proc.returncode)
    else:
        os.execvp(exec_args[0], exec_args)
