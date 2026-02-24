#!/usr/bin/env python3
"""
dispatcher.py - プロセス分散型ジョブランナー

各ジョブは独立した dispatcher プロセスとして実行される。中央ループやデーモンは存在しない。
状態ファイルが唯一の協調メカニズム。スロットチェック〜起動の区間を flock で排他制御する。
プロンプトは標準入力から読み取る。

使い方:
  echo "fix bug" | dispatcher.py run --project bo [--max-slots 3]
  dispatcher.py open --project bo [--command CMD]
  dispatcher.py status [--json]
  dispatcher.py cancel --id bo-2
  dispatcher.py kill --id bo-1
  dispatcher.py kill --all
"""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path


JST = timezone(timedelta(hours=9))

DEFAULT_SESSION_NAME = "dispatch"
DEFAULT_MAX_SLOTS = 3
POLL_INTERVAL = 5
DEFAULT_COMMAND = "sandbox claude --permission-mode bypassPermissions"
DEFAULT_INTERACTIVE_COMMAND = "sandbox claude --permission-mode bypassPermissions"
DEFAULT_REPO = "~/.local/share/my-tasks"


# ---------------------------------------------------------------------------
# ユーティリティ（既存から残す）
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def load_json(path: Path) -> dict | None:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def save_json(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def done_file_path(dispatch_id: str, state_dir: Path) -> Path:
    return state_dir / f".dispatch-{dispatch_id}.done"


def exit_file_path(dispatch_id: str, state_dir: Path) -> Path:
    return state_dir / f".dispatch-{dispatch_id}.exit"


def load_project(repo_dir: Path, project_id: str) -> dict | None:
    return load_json(repo_dir / "projects" / f"{project_id}.json")


def detect_tmux_session(explicit: str | None) -> tuple[str, bool]:
    """使用する tmux セッション名を決定する。

    Returns:
        (session_name, is_caller_session)
        is_caller_session=True の場合、セッションは呼び出し元のものなので kill 時にセッション全体を終了しない。
    """
    if explicit:
        result = subprocess.run(
            ["tmux", "has-session", "-t", explicit],
            capture_output=True,
        )
        if result.returncode != 0:
            print(
                f"エラー: 指定された tmux セッション '{explicit}' が存在しません",
                file=sys.stderr,
            )
            sys.exit(1)
        return (explicit, True)

    if os.environ.get("TMUX"):
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#S"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return (result.stdout.strip(), True)

    return (DEFAULT_SESSION_NAME, False)


def ensure_tmux_session(session_name: str, is_caller_session: bool) -> bool:
    """tmux セッションが存在しなければ作成する。呼び出し元セッションの場合は存在確認のみ。"""
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    )
    if result.returncode != 0:
        if is_caller_session:
            print(
                f"エラー: tmux セッション '{session_name}' が存在しません",
                file=sys.stderr,
            )
            return False
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name, "-n", "_control"],
            capture_output=True,
        )
        if result.returncode != 0:
            print(
                f"エラー: tmux セッション '{session_name}' の作成に失敗しました: "
                f"{result.stderr.decode().strip()}",
                file=sys.stderr,
            )
            return False
    return True


# ---------------------------------------------------------------------------
# 状態管理（新規）
# ---------------------------------------------------------------------------

def get_state_dir() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        d = Path(runtime_dir) / "my-tasks-dispatch"
    else:
        d = Path("/tmp/my-tasks-dispatch")
    d.mkdir(parents=True, exist_ok=True)
    return d


def acquire_lock(state_dir: Path) -> int:
    lock_path = state_dir / ".lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def release_lock(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


@dataclass
class DispatchItem:
    dispatch_id: str
    working_dir: str
    prompt: str
    status: str = "queued"
    pid: int | None = None
    tmux_session: str | None = None
    tmux_window: str | None = None
    exit_code: int | None = None
    started_at: str | None = None
    finished_at: str | None = None


def save_state(state_dir: Path, item: DispatchItem) -> None:
    save_json(state_dir / f"{item.dispatch_id}.json", asdict(item))


def load_all_states(state_dir: Path) -> list[DispatchItem]:
    items = []
    for f in sorted(state_dir.glob("*.json")):
        if f.name.startswith("."):
            continue
        data = load_json(f)
        if data:
            items.append(DispatchItem(**data))
    return items


def generate_dispatch_id(project_id: str, state_dir: Path) -> str:
    existing_nums: list[int] = []
    prefix = f"{project_id}-"
    for f in state_dir.glob(f"{prefix}*.json"):
        name = f.stem
        suffix = name[len(prefix):]
        try:
            existing_nums.append(int(suffix))
        except ValueError:
            pass
    next_num = max(existing_nums, default=0) + 1
    return f"{project_id}-{next_num}"


def _read_exit_code(efile: Path) -> int | None:
    """終了コードファイルを読み取る（リトライなし、ロック内での使用を想定）。"""
    if efile.exists():
        try:
            content = efile.read_text().strip()
            if content:
                return int(content)
        except (ValueError, OSError):
            pass
    return None


def refresh_states(state_dir: Path) -> None:
    """running 状態のファイルを PID/sentinel チェックで更新する。ロック内で呼ぶこと。"""
    for f in list(state_dir.glob("*.json")):
        if f.name.startswith("."):
            continue
        data = load_json(f)
        if not data or data.get("status") != "running":
            continue

        item = DispatchItem(**data)

        # Sentinel ファイルチェック（主系）
        dfile = done_file_path(item.dispatch_id, state_dir)
        if dfile.exists():
            item.status = "done"
            item.exit_code = 0
            item.finished_at = now_iso()
            save_state(state_dir, item)
            if item.tmux_session and item.tmux_window:
                subprocess.run(
                    ["tmux", "kill-window", "-t", f"{item.tmux_session}:{item.tmux_window}"],
                    capture_output=True,
                )
            dfile.unlink(missing_ok=True)
            continue

        # tmux ウィンドウ存在チェック（副系）
        if item.tmux_session and item.tmux_window:
            result = subprocess.run(
                ["tmux", "list-panes", "-t", f"{item.tmux_session}:{item.tmux_window}"],
                capture_output=True,
            )
            if result.returncode == 0:
                continue  # ウィンドウ存在 = まだ実行中

        # ウィンドウ消失 → exit file を確認して done/failed を判定
        efile = exit_file_path(item.dispatch_id, state_dir)
        exit_code = _read_exit_code(efile)
        if exit_code is not None:
            item.exit_code = exit_code
            item.status = "done" if exit_code == 0 else "failed"
        else:
            item.exit_code = -1
            item.status = "failed"
        item.finished_at = now_iso()
        save_state(state_dir, item)


def count_running(state_dir: Path) -> int:
    count = 0
    for f in state_dir.glob("*.json"):
        if f.name.startswith("."):
            continue
        data = load_json(f)
        if data and data.get("status") == "running":
            count += 1
    return count


# ---------------------------------------------------------------------------
# プロンプト構築・tmux 起動
# ---------------------------------------------------------------------------

def build_prompt(prompt: str, dispatch_id: str, state_dir: Path) -> str:
    dfile = done_file_path(dispatch_id, state_dir)
    return f"""{prompt}

作業が完了したら、変更をコミットしてから次のコマンドを実行してください:
touch {dfile}"""


def launch_in_tmux(item: DispatchItem, command: str, session_name: str, state_dir: Path) -> int | None:
    """tmux ウィンドウで Claude Code セッションを起動し、pane PID を返す。"""
    full_prompt = build_prompt(item.prompt, item.dispatch_id, state_dir)
    efile = exit_file_path(item.dispatch_id, state_dir)
    dfile = done_file_path(item.dispatch_id, state_dir)

    for f in (efile, dfile):
        if f.exists():
            f.unlink()

    escaped_prompt = full_prompt.replace("'", "'\"'\"'")
    system_suffix = f"タスク完了時に必ず touch {dfile} を実行してください。これによりディスパッチャーがタスクの完了を検知します。"
    escaped_system = system_suffix.replace("'", "'\"'\"'")
    shell_cmd = f"cd '{item.working_dir}' && {command} --append-system-prompt '{escaped_system}' '{escaped_prompt}'; echo $? > '{efile}'"

    result = subprocess.run(
        ["tmux", "new-window", "-d", "-t", session_name, "-n", item.dispatch_id, "bash", "-c", shell_cmd],
        capture_output=True,
    )
    if result.returncode != 0:
        print(
            f"警告: tmux ウィンドウ作成に失敗しました ({item.dispatch_id}): "
            f"{result.stderr.decode().strip()}",
            file=sys.stderr,
        )
        return None

    pid_result = subprocess.run(
        ["tmux", "list-panes", "-t", f"{session_name}:{item.dispatch_id}", "-F", "#{pane_pid}"],
        capture_output=True,
        text=True,
    )
    if pid_result.returncode != 0 or not pid_result.stdout.strip():
        print(
            f"警告: PID 取得に失敗しました ({item.dispatch_id})",
            file=sys.stderr,
        )
        return None

    try:
        return int(pid_result.stdout.strip().split("\n")[0])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 待機ループ（子プロセス用）
# ---------------------------------------------------------------------------

def _wait_and_launch(state_dir: Path, item: DispatchItem, max_slots: int, command: str, session: str) -> None:
    while True:
        time.sleep(POLL_INTERVAL)
        fd = acquire_lock(state_dir)
        try:
            # cancel チェック（ファイル削除 = キャンセル）
            if not (state_dir / f"{item.dispatch_id}.json").exists():
                sys.exit(0)
            refresh_states(state_dir)
            if count_running(state_dir) < max_slots:
                pid = launch_in_tmux(item, command, session, state_dir)
                if pid is None:
                    item.status = "failed"
                    item.exit_code = -1
                    item.finished_at = now_iso()
                    save_state(state_dir, item)
                    return
                item.status = "running"
                item.pid = pid
                item.tmux_session = session
                item.tmux_window = item.dispatch_id
                item.started_at = now_iso()
                save_state(state_dir, item)
                return
        finally:
            release_lock(fd)


# ---------------------------------------------------------------------------
# CLI コマンド
# ---------------------------------------------------------------------------

def resolve_repo(args: argparse.Namespace) -> Path:
    return Path(args.repo).expanduser().resolve()


def cmd_run(args: argparse.Namespace) -> None:
    prompt = sys.stdin.read().strip()
    if not prompt:
        print("エラー: プロンプトが空です（stdin からプロンプトを読み取ります）", file=sys.stderr)
        sys.exit(1)

    repo_dir = resolve_repo(args)
    project = load_project(repo_dir, args.project)
    if not project:
        print(
            f"エラー: プロジェクト '{args.project}' が見つかりません: "
            f"{repo_dir / 'projects' / f'{args.project}.json'}",
            file=sys.stderr,
        )
        sys.exit(1)

    working_dir = project.get("working_directory")
    if not working_dir:
        print(f"エラー: プロジェクト '{args.project}' に working_directory が設定されていません", file=sys.stderr)
        sys.exit(1)

    if not Path(working_dir).is_dir():
        print(f"エラー: 作業ディレクトリが存在しません: {working_dir}", file=sys.stderr)
        sys.exit(1)

    session_name, is_caller = detect_tmux_session(args.session)
    if not ensure_tmux_session(session_name, is_caller):
        sys.exit(1)

    state_dir = get_state_dir()

    # ロック取得 → スロットチェック → 起動 or キュー投入
    fd = acquire_lock(state_dir)
    try:
        refresh_states(state_dir)
        dispatch_id = generate_dispatch_id(args.project, state_dir)
        running = count_running(state_dir)
        item = DispatchItem(dispatch_id=dispatch_id, working_dir=working_dir, prompt=prompt)

        if running < args.max_slots:
            pid = launch_in_tmux(item, args.command, session_name, state_dir)
            if pid is None:
                print("エラー: tmux ウィンドウの起動に失敗しました", file=sys.stderr)
                sys.exit(1)
            item.status = "running"
            item.pid = pid
            item.tmux_session = session_name
            item.tmux_window = dispatch_id
            item.started_at = now_iso()
            save_state(state_dir, item)
            print(f"起動: {dispatch_id}", file=sys.stderr)
            return

        # スロット満杯: queued 状態で保存（ロック内で書き込み、ID 重複を防止）
        save_state(state_dir, item)
    finally:
        release_lock(fd)

    # fork してバックグラウンドで待機、親は即座に返却
    child_pid = os.fork()
    if child_pid > 0:
        print(f"キュー追加: {dispatch_id} (スロット待機中)", file=sys.stderr)
        return

    # 子プロセス
    os.setsid()
    _wait_and_launch(state_dir, item, args.max_slots, args.command, session_name)
    sys.exit(0)


def cmd_status(args: argparse.Namespace) -> None:
    state_dir = get_state_dir()

    fd = acquire_lock(state_dir)
    try:
        refresh_states(state_dir)
    finally:
        release_lock(fd)

    items = load_all_states(state_dir)
    if not items:
        print("ジョブはありません", file=sys.stderr)
        return

    if args.json:
        print(json.dumps([asdict(i) for i in items], ensure_ascii=False, indent=2))
    else:
        for item in items:
            pid_str = str(item.pid) if item.pid else "-"
            print(f"  {item.dispatch_id:<20} {item.status:<10} pid={pid_str:<8} {item.started_at or '-'}")

        counts: dict[str, int] = {}
        for item in items:
            counts[item.status] = counts.get(item.status, 0) + 1
        parts = []
        for s in ["running", "queued", "done", "failed"]:
            if counts.get(s, 0) > 0:
                parts.append(f"{s}={counts[s]}")
        if parts:
            print(f"\n  [{', '.join(parts)}]")


def cmd_cancel(args: argparse.Namespace) -> None:
    state_dir = get_state_dir()
    state_file = state_dir / f"{args.id}.json"

    if not state_file.exists():
        print(f"エラー: ジョブが見つかりません: {args.id}", file=sys.stderr)
        sys.exit(1)

    data = load_json(state_file)
    if not data:
        print(f"エラー: 状態ファイルの読み込みに失敗しました: {args.id}", file=sys.stderr)
        sys.exit(1)
    if data.get("status") != "queued":
        print(f"エラー: キュー状態のジョブのみキャンセルできます (現在: {data.get('status')})", file=sys.stderr)
        sys.exit(1)

    state_file.unlink()
    print(f"キャンセル: {args.id}", file=sys.stderr)


def _kill_item(state_dir: Path, item: DispatchItem) -> None:
    """running ジョブを強制停止する。"""
    if item.tmux_session and item.tmux_window:
        subprocess.run(
            ["tmux", "kill-window", "-t", f"{item.tmux_session}:{item.tmux_window}"],
            capture_output=True,
        )
    item.status = "failed"
    item.exit_code = -1
    item.finished_at = now_iso()
    save_state(state_dir, item)


def cmd_kill(args: argparse.Namespace) -> None:
    state_dir = get_state_dir()

    if args.all:
        items = load_all_states(state_dir)
        killed = 0
        for item in items:
            if item.status == "running":
                _kill_item(state_dir, item)
                killed += 1
            elif item.status == "queued":
                (state_dir / f"{item.dispatch_id}.json").unlink(missing_ok=True)
                killed += 1
        print(f"{killed} 個のジョブを停止しました", file=sys.stderr)
    else:
        state_file = state_dir / f"{args.id}.json"
        if not state_file.exists():
            print(f"エラー: ジョブが見つかりません: {args.id}", file=sys.stderr)
            sys.exit(1)

        data = load_json(state_file)
        if not data:
            print(f"エラー: 状態ファイルの読み込みに失敗しました: {args.id}", file=sys.stderr)
            sys.exit(1)
        item = DispatchItem(**data)

        if item.status == "queued":
            state_file.unlink()
            print(f"キャンセル: {args.id}", file=sys.stderr)
        elif item.status == "running":
            _kill_item(state_dir, item)
            print(f"強制停止: {args.id}", file=sys.stderr)
        else:
            print(f"ジョブは既に終了しています: {args.id} (status={item.status})", file=sys.stderr)


def cmd_open(args: argparse.Namespace) -> None:
    repo_dir = resolve_repo(args)
    project = load_project(repo_dir, args.project)
    if not project:
        print(
            f"エラー: プロジェクト '{args.project}' が見つかりません: "
            f"{repo_dir / 'projects' / f'{args.project}.json'}",
            file=sys.stderr,
        )
        sys.exit(1)

    working_dir = project.get("working_directory")
    if not working_dir:
        print(f"エラー: プロジェクト '{args.project}' に working_directory が設定されていません", file=sys.stderr)
        sys.exit(1)

    if not Path(working_dir).is_dir():
        print(f"エラー: 作業ディレクトリが存在しません: {working_dir}", file=sys.stderr)
        sys.exit(1)

    session_name, is_caller = detect_tmux_session(args.session)
    if not ensure_tmux_session(session_name, is_caller):
        sys.exit(1)

    window_name = f"{args.project}-interactive"
    shell_cmd = f"cd '{working_dir}' && {args.command}"

    result = subprocess.run(
        ["tmux", "new-window", "-d", "-t", session_name, "-n", window_name, "bash", "-c", shell_cmd],
        capture_output=True,
    )
    if result.returncode != 0:
        print(
            f"エラー: tmux ウィンドウ作成に失敗しました: {result.stderr.decode().strip()}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"起動: {session_name}:{window_name} ({working_dir})", file=sys.stderr)


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="プロセス分散型ジョブランナー — タスクを tmux 上で並列実行する"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # run
    p_run = subparsers.add_parser("run", help="ジョブを投入する（プロンプトは stdin から）")
    p_run.add_argument("--project", required=True, help="プロジェクトID")
    p_run.add_argument(
        "--repo", default=DEFAULT_REPO, metavar="PATH",
        help=f"タスク管理リポジトリのパス（デフォルト: {DEFAULT_REPO}）",
    )
    p_run.add_argument(
        "--max-slots", type=int, default=DEFAULT_MAX_SLOTS,
        help=f"最大並列スロット数（デフォルト: {DEFAULT_MAX_SLOTS}）",
    )
    p_run.add_argument(
        "--command", default=DEFAULT_COMMAND,
        help=f"セッション起動コマンド（デフォルト: {DEFAULT_COMMAND}）",
    )
    p_run.add_argument(
        "--session", default=None, metavar="SESSION_NAME",
        help="tmux セッション名を明示指定",
    )
    p_run.set_defaults(func=cmd_run)

    # open
    p_open = subparsers.add_parser("open", help="プロジェクトの作業ディレクトリで対話セッションを起動する")
    p_open.add_argument("--project", required=True, help="プロジェクトID")
    p_open.add_argument(
        "--repo", default=DEFAULT_REPO, metavar="PATH",
        help=f"タスク管理リポジトリのパス（デフォルト: {DEFAULT_REPO}）",
    )
    p_open.add_argument(
        "--command", default=DEFAULT_INTERACTIVE_COMMAND,
        help=f"起動コマンド（デフォルト: {DEFAULT_INTERACTIVE_COMMAND}）",
    )
    p_open.add_argument(
        "--session", default=None, metavar="SESSION_NAME",
        help="tmux セッション名を明示指定",
    )
    p_open.set_defaults(func=cmd_open)

    # status
    p_status = subparsers.add_parser("status", help="状態を表示する")
    p_status.add_argument("--json", action="store_true", help="JSON 形式で出力する")
    p_status.set_defaults(func=cmd_status)

    # cancel
    p_cancel = subparsers.add_parser("cancel", help="キューからジョブを取り消す")
    p_cancel.add_argument("--id", required=True, help="ジョブID")
    p_cancel.set_defaults(func=cmd_cancel)

    # kill
    p_kill = subparsers.add_parser("kill", help="ジョブを強制停止する")
    group = p_kill.add_mutually_exclusive_group(required=True)
    group.add_argument("--id", help="ジョブID")
    group.add_argument("--all", action="store_true", help="全ジョブを停止する")
    p_kill.set_defaults(func=cmd_kill)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
