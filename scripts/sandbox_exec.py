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

CREDENTIAL_PROFILES_DIR = Path("~/.local/share/my-tasks/credential-profiles").expanduser()

BUILTIN_CREDENTIAL_PROFILES: dict[str, dict] = {
    "full-access": {
        "credential_profile_id": "full-access",
        "allowed_credentials": "*",
    },
    "none": {
        "credential_profile_id": "none",
        "allowed_credentials": [],
    },
}


BUILTIN_PROFILES: dict[str, dict] = {
    "default": {
        "profile_id": "default",
        "proxy_profile": "full",
        "credential_profile": "full-access",
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
        "credential_profile": "full-access",
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


# --- クレデンシャルプロファイル解決 -------------------------------------------

def resolve_credential_profile(profile_id: str) -> dict | None:
    """クレデンシャルプロファイルを解決する。ファイル → 組み込み の順。"""
    sp = CREDENTIAL_PROFILES_DIR / f"{profile_id}.json"
    if sp.is_file():
        with open(sp, encoding="utf-8") as f:
            return json.load(f)
    return BUILTIN_CREDENTIAL_PROFILES.get(profile_id)


def resolve_allowed_credentials(profile: dict) -> list[str] | str:
    """サンドボックスプロファイルから allowed_credentials を解決する。

    解決優先順位:
    1. allowed_credentials が直接存在 → そのまま返す（後方互換）
    2. credential_profile が指定 → credential profile を読み込んで返す
    3. どちらも未指定 → "*"（デフォルト全許可）
    """
    if "allowed_credentials" in profile:
        return profile["allowed_credentials"]

    credential_profile_id = profile.get("credential_profile")
    if credential_profile_id:
        cred_profile = resolve_credential_profile(credential_profile_id)
        if not cred_profile:
            raise FileNotFoundError(f"Credential profile not found: {credential_profile_id}")
        return cred_profile["allowed_credentials"]

    return "*"


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


def resolve_host_forward_ports(profile: dict) -> list[int]:
    """プロファイルから host_forward_ports を取得する。"""
    return [int(p) for p in profile.get("host_forward_ports", [])]


def resolve_extra_binds(profile: dict) -> list[str]:
    args: list[str] = []
    home = str(HOME)
    for bind in profile.get("extra_binds", []):
        src = bind["source"].replace("$HOME", home)
        tgt = bind["target"].replace("$HOME", home)
        flag = "--ro-bind" if bind.get("mode") == "ro" else "--bind"
        if not os.path.exists(src):
            os.makedirs(src, exist_ok=True)
        args += [flag, src, tgt]
    return args


# --- プロジェクト env 解決 ----------------------------------------------------

def resolve_project_env_sync(project: dict) -> Path | None:
    """プロジェクトの env フィールドを同期的に解決し、キャッシュディレクトリに書き出す。

    Returns:
        生成した env ファイルのパス。env フィールドが無い場合は None。
    """
    env_dict = project.get("env")
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
    env_path = CACHE_DIR / f"env-{project['project_id']}.env"
    env_path.write_text(
        "\n".join(f"{k}={v}" for k, v in resolved.items()) + "\n",
        encoding="utf-8",
    )
    os.chmod(env_path, 0o600)
    return env_path


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
    """dispatcher ソケットバインド."""
    d = f"/run/user/{UID}/my-tasks-dispatch"
    os.makedirs(d, exist_ok=True)
    return ["--bind", d, d]


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


# --- 組み込み Credential Broker -----------------------------------------------

class EmbeddedCredBroker:
    """CRED_TOKEN 未設定時に自動起動するフォールバック Credential Broker."""

    def __init__(self, sock_path: str, token: str, allowed: list[str] | str):
        self._sock_path = sock_path
        self._token = token
        self._allowed = allowed
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
            entry = request.get("entry", "")
            operation = request.get("operation", "show")
            value = request.get("value", "")
            response = self._process(token, entry, operation, value)
            conn.sendall(json.dumps(response, ensure_ascii=False).encode() + b"\n")
        except Exception:
            try:
                conn.sendall(json.dumps({"ok": False, "error": "internal error"}).encode() + b"\n")
            except Exception:
                pass
        finally:
            conn.close()

    def _process(self, token: str, entry: str, operation: str = "show", value: str = "") -> dict:
        if not token or token != self._token:
            return {"ok": False, "error": "invalid token"}
        if self._allowed != "*" and not any(fnmatch.fnmatch(entry, pat) for pat in self._allowed):
            return {"ok": False, "error": f"entry not allowed: {entry}"}
        try:
            if operation == "insert":
                proc = subprocess.run(
                    ["/usr/bin/pass", "insert", "--force", "--echo", entry],
                    input=value.encode(),
                    capture_output=True,
                )
                if proc.returncode != 0:
                    return {"ok": False, "error": "credential insert failed"}
                return {"ok": True}
            else:
                proc = subprocess.run(
                    ["/usr/bin/pass", "show", entry],
                    capture_output=True,
                )
                if proc.returncode != 0:
                    return {"ok": False, "error": "credential retrieval failed"}
                return {"ok": True, "value": proc.stdout.decode()}
        except Exception:
            return {"ok": False, "error": "credential operation failed"}

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
    cred_args: list[str],
    command: list[str],
    host_forward_ports: list[int] | None = None,
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
        *profile_binds,
        "--bind", work, work,
        "--bind", f"{HOME}/.local/share/my-tasks", f"{HOME}/.local/share/my-tasks",
        "--chdir", work,
        *_env_args(env_file_args, cred_args),
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
        *profile_binds,
        "--bind", work, work,
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
    working_dir: str | None = None,
) -> None:
    """サンドボックスを構築し、exec で置き換える."""
    work = working_dir or os.getcwd()

    profile = resolve_profile(sandbox_profile)
    network_protected = uses_network_protection(profile)
    profile_binds = resolve_extra_binds(profile)

    if command is None:
        command = [f"{HOME}/.local/bin/claude", "--permission-mode", "bypassPermissions"]

    env_file_args = load_env_files(env_files or [])
    cred_args = _cred_env()

    broker = None
    if not os.environ.get("CRED_TOKEN"):
        allowed = resolve_allowed_credentials(profile)
        if allowed:
            sock_path = f"/run/user/{UID}/my-tasks-dispatch/cred-broker-{os.getpid()}.sock"
            token = uuid.uuid4().hex
            broker = EmbeddedCredBroker(sock_path, token, allowed)
            broker.start()
            cred_args = ["--setenv", "CRED_TOKEN", token, "--setenv", "CRED_BROKER_SOCK", sock_path]

    host_forward_ports = resolve_host_forward_ports(profile)

    if network_protected:
        ensure_netns()
        if host_forward_ports:
            setup_host_port_forwarding(host_forward_ports)
        exec_args = build_netns_args(work, proxy_port, profile_binds, env_file_args, cred_args, command, host_forward_ports)
    else:
        exec_args = build_host_network_args(work, profile_binds, env_file_args, cred_args, command)

    if broker:
        proc = subprocess.Popen(exec_args)
        signal.signal(signal.SIGTERM, lambda *_: proc.terminate())
        signal.signal(signal.SIGINT, lambda *_: proc.send_signal(signal.SIGINT))
        proc.wait()
        broker.stop()
        sys.exit(proc.returncode)
    else:
        os.execvp(exec_args[0], exec_args)
