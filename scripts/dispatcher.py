#!/usr/bin/env python3
"""
dispatcher.py - タスクディスパッチャー

デイリーゴールに登録されたタスクを tmux 上で複数の Claude Code セッションとして並列実行する。

使い方:
  python3 dispatcher.py start --repo ~/.local/share/my-tasks
  python3 dispatcher.py status
  python3 dispatcher.py add --ref jira/UBS-103 --working-dir /path/to/project
  python3 dispatcher.py stop
  python3 dispatcher.py kill
"""

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path


JST = timezone(timedelta(hours=9))

TMUX_SESSION = "dispatch"
DEFAULT_MAX_SLOTS = 3
POLL_INTERVAL = 5
DEFAULT_COMMAND = "sandbox claude"


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def today_str() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d")


def state_file_path() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir) / "my-tasks-dispatcher.json"
    return Path("/tmp/my-tasks-dispatcher.json")


def exit_file_path(dispatch_id: str) -> Path:
    return Path(f"/tmp/dispatch-{dispatch_id}.exit")


@dataclass
class DispatchItem:
    dispatch_id: str
    ref: str
    working_dir: str
    note: str | None = None
    title: str | None = None
    status: str = "queued"
    tmux_window: str | None = None
    pid: int | None = None
    exit_code: int | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass
class DispatchState:
    date: str
    max_slots: int
    status: str  # running | completed | interrupted
    command: str
    started_at: str
    finished_at: str | None = None
    items: list[dict] = field(default_factory=list)


def load_json(path: Path) -> dict | None:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def save_json(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def load_state() -> dict | None:
    return load_json(state_file_path())


def save_state(state: dict) -> None:
    save_json(state_file_path(), state)


def load_project(repo_dir: Path, project_id: str) -> dict | None:
    return load_json(repo_dir / "projects" / f"{project_id}.json")


def load_task_store(repo_dir: Path, datasource_id: str) -> dict | None:
    return load_json(repo_dir / "tasks" / f"{datasource_id}.json")


def resolve_task_title(repo_dir: Path, ref: str) -> str | None:
    """ref からタスクタイトルを解決する。"""
    parts = ref.split("/", 1)
    if len(parts) != 2:
        return None
    datasource_id, remote_id = parts
    store = load_task_store(repo_dir, datasource_id)
    if not store:
        return None
    for task in store.get("tasks", []):
        if task.get("remote_id") == remote_id:
            return task.get("title")
    return None


def extract_dispatch_items(daily: dict, repo_dir: Path) -> list[DispatchItem]:
    """日次ゴールのネスト構造をフラット化し、DispatchItem リストを返す。"""
    items: list[DispatchItem] = []
    seen_refs: set[str] = set()

    for goal in daily.get("goals", []):
        project_id = goal.get("project_id")
        if not project_id:
            print(f"警告: ゴールに project_id がありません、スキップします", file=sys.stderr)
            continue

        project = load_project(repo_dir, project_id)
        if not project:
            print(
                f"警告: プロジェクト {project_id} が見つかりません、タスクを skipped にします",
                file=sys.stderr,
            )

        working_dir = project.get("working_directory") if project else None
        if not working_dir:
            print(
                f"警告: プロジェクト {project_id} に working_directory が設定されていません",
                file=sys.stderr,
            )

        for task_entry in goal.get("tasks", []):
            ref = task_entry.get("ref")
            if not ref:
                continue
            if ref in seen_refs:
                continue
            seen_refs.add(ref)

            title = resolve_task_title(repo_dir, ref)
            dispatch_id = ref.replace("/", "-")
            note = task_entry.get("note")

            if not working_dir:
                items.append(DispatchItem(
                    dispatch_id=dispatch_id,
                    ref=ref,
                    working_dir="",
                    note=note,
                    title=title,
                    status="skipped",
                ))
            elif not Path(working_dir).is_dir():
                print(
                    f"警告: 作業ディレクトリが存在しません: {working_dir} (ref={ref})",
                    file=sys.stderr,
                )
                items.append(DispatchItem(
                    dispatch_id=dispatch_id,
                    ref=ref,
                    working_dir=working_dir,
                    note=note,
                    title=title,
                    status="skipped",
                ))
            else:
                items.append(DispatchItem(
                    dispatch_id=dispatch_id,
                    ref=ref,
                    working_dir=working_dir,
                    note=note,
                    title=title,
                    status="queued",
                ))

    return items


def build_prompt(item: DispatchItem, repo_dir: Path) -> str:
    """Claude Code に渡すプロンプトを構築する。"""
    parts = item.ref.split("/", 1)
    datasource_id = parts[0] if len(parts) == 2 else "unknown"

    prompt = f"""タスク {item.ref} に取り組んでください。

タスク管理リポジトリ: {repo_dir}
このリポジトリの tasks/{datasource_id}.json と datasources/{datasource_id}.json を参照して、
タスクの詳細情報を確認してください。
必要に応じて元データソース（JIRA、Microsoft To Do 等）からも情報を取得してください。"""

    if item.note:
        prompt += f"\n\n補足指示: {item.note}"

    prompt += "\n\n作業が完了したら、変更をコミットして終了してください。"

    return prompt


def ensure_tmux_session() -> bool:
    """tmux セッションが存在しなければ作成する。"""
    result = subprocess.run(
        ["tmux", "has-session", "-t", TMUX_SESSION],
        capture_output=True,
    )
    if result.returncode != 0:
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", TMUX_SESSION, "-n", "_control"],
            capture_output=True,
        )
        if result.returncode != 0:
            print(
                f"エラー: tmux セッション '{TMUX_SESSION}' の作成に失敗しました: "
                f"{result.stderr.decode().strip()}",
                file=sys.stderr,
            )
            return False
    return True


def launch_in_tmux(item: DispatchItem, repo_dir: Path, command: str) -> int | None:
    """tmux ウィンドウで Claude Code セッションを起動し、pane PID を返す。"""
    prompt = build_prompt(item, repo_dir)
    efile = exit_file_path(item.dispatch_id)

    # 既存の exit file を削除
    if efile.exists():
        efile.unlink()

    escaped_prompt = prompt.replace("'", "'\"'\"'")
    shell_cmd = f"cd '{item.working_dir}' && {command} -p '{escaped_prompt}'; echo $? > '{efile}'"

    result = subprocess.run(
        ["tmux", "new-window", "-t", TMUX_SESSION, "-n", item.dispatch_id, "bash", "-c", shell_cmd],
        capture_output=True,
    )
    if result.returncode != 0:
        print(
            f"警告: tmux ウィンドウ作成に失敗しました (ref={item.ref}): "
            f"{result.stderr.decode().strip()}",
            file=sys.stderr,
        )
        return None

    # pane PID を取得
    pid_result = subprocess.run(
        ["tmux", "list-panes", "-t", f"{TMUX_SESSION}:{item.dispatch_id}", "-F", "#{pane_pid}"],
        capture_output=True,
        text=True,
    )
    if pid_result.returncode != 0 or not pid_result.stdout.strip():
        print(
            f"警告: PID 取得に失敗しました (ref={item.ref})",
            file=sys.stderr,
        )
        return None

    try:
        return int(pid_result.stdout.strip().split("\n")[0])
    except ValueError:
        return None


def is_pid_alive(pid: int) -> bool:
    """PID が生存しているかチェックする。"""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def check_session(item_dict: dict) -> None:
    """実行中アイテムの PID 生存チェックと終了コード読み取りを行う。"""
    if item_dict["status"] != "running":
        return
    pid = item_dict.get("pid")
    if pid is None:
        return

    if is_pid_alive(pid):
        return

    # PID 消失 — 終了コードファイルを読み取り
    dispatch_id = item_dict["dispatch_id"]
    efile = exit_file_path(dispatch_id)

    exit_code = _read_exit_code(efile)

    if exit_code is not None:
        item_dict["exit_code"] = exit_code
        item_dict["status"] = "done" if exit_code == 0 else "failed"
        item_dict["finished_at"] = now_iso()
    else:
        # exit file がまだ書き込まれていない可能性があるので、次回再チェック
        # ここではまだ status を変えない
        pass


def _read_exit_code(efile: Path, retries: int = 3, delay: float = 1.0) -> int | None:
    """終了コードファイルを読み取る。リトライ付き。"""
    for i in range(retries):
        if efile.exists():
            try:
                content = efile.read_text().strip()
                if content:
                    return int(content)
            except (ValueError, OSError):
                pass
        if i < retries - 1:
            time.sleep(delay)
    return None


def check_all_sessions(items: list[dict]) -> None:
    """全実行中アイテムのセッション状態をチェックする。"""
    for item in items:
        check_session(item)


def count_by_status(items: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        s = item["status"]
        counts[s] = counts.get(s, 0) + 1
    return counts


def print_status_line(items: list[dict]) -> None:
    """ステータス行を stderr に出力する。"""
    counts = count_by_status(items)
    parts = []
    for s in ["running", "queued", "done", "failed", "skipped"]:
        if counts.get(s, 0) > 0:
            parts.append(f"{s}={counts[s]}")
    print(f"  [{', '.join(parts)}]", file=sys.stderr)


def handle_stale_running(items: list[dict]) -> None:
    """PID 消失かつ exit file なしのアイテムを最終チェックして failed にする。"""
    for item in items:
        if item["status"] != "running":
            continue
        pid = item.get("pid")
        if pid is None or is_pid_alive(pid):
            continue

        efile = exit_file_path(item["dispatch_id"])
        exit_code = _read_exit_code(efile, retries=1, delay=0)
        if exit_code is not None:
            item["exit_code"] = exit_code
            item["status"] = "done" if exit_code == 0 else "failed"
        else:
            item["exit_code"] = -1
            item["status"] = "failed"
        item["finished_at"] = now_iso()


def run_dispatcher(repo_dir: Path, date: str, max_slots: int, command: str) -> None:
    """メインディスパッチループ。"""
    # 既に実行中かチェック
    existing = load_state()
    if existing and existing.get("status") == "running":
        print("エラー: ディスパッチャーが既に実行中です", file=sys.stderr)
        print(f"  状態ファイル: {state_file_path()}", file=sys.stderr)
        print("  'stop' で停止するか 'kill' で強制終了してください", file=sys.stderr)
        sys.exit(1)

    # 日次ゴールを読み込み
    daily_path = repo_dir / "daily" / f"{date}.json"
    daily = load_json(daily_path)
    if not daily:
        print(f"エラー: 日次ゴールが見つかりません: {daily_path}", file=sys.stderr)
        sys.exit(1)

    # タスク抽出
    items = extract_dispatch_items(daily, repo_dir)
    if not items:
        print("ディスパッチ対象のタスクがありません", file=sys.stderr)
        sys.exit(0)

    queued = [i for i in items if i.status == "queued"]
    skipped = [i for i in items if i.status == "skipped"]

    print(f"ディスパッチ開始: {len(queued)} タスク (スキップ: {len(skipped)})", file=sys.stderr)

    # tmux セッション確保
    if queued and not ensure_tmux_session():
        sys.exit(1)

    # 状態初期化
    state: dict = {
        "date": date,
        "max_slots": max_slots,
        "status": "running",
        "command": command,
        "started_at": now_iso(),
        "finished_at": None,
        "items": [asdict(i) for i in items],
    }
    save_state(state)

    try:
        _dispatch_loop(state, repo_dir, command)
    except KeyboardInterrupt:
        print("\n中断されました。状態を保存します。", file=sys.stderr)
        state["status"] = "interrupted"
        state["finished_at"] = now_iso()
        # 残りの running を最終チェック
        handle_stale_running(state["items"])
        save_state(state)
        _print_report(state)
        sys.exit(130)

    # 完了
    state["status"] = "completed"
    state["finished_at"] = now_iso()
    save_state(state)
    _print_report(state)


def _dispatch_loop(state: dict, repo_dir: Path, command: str) -> None:
    """キューからタスクを起動し、完了を監視するループ。"""
    items = state["items"]
    max_slots = state["max_slots"]

    while True:
        # 状態ファイルを再読み込み（stop/add コマンド対応）
        current = load_state()
        if current:
            # stop コマンドによる停止要求
            if current.get("status") != "running":
                print("停止要求を受信しました", file=sys.stderr)
                state["status"] = current["status"]
                state["items"] = current["items"]
                return

            # add コマンドによるアイテム追加
            if len(current.get("items", [])) > len(items):
                items = current["items"]
                state["items"] = items

            # max_slots の変更
            max_slots = current.get("max_slots", max_slots)
            state["max_slots"] = max_slots

        # セッション状態チェック
        check_all_sessions(items)

        # 新規タスク起動
        running_count = sum(1 for i in items if i["status"] == "running")
        for item in items:
            if running_count >= max_slots:
                break
            if item["status"] != "queued":
                continue

            pid = launch_in_tmux(
                DispatchItem(**{k: item[k] for k in DispatchItem.__dataclass_fields__}),
                repo_dir,
                command,
            )
            if pid is not None:
                item["status"] = "running"
                item["tmux_window"] = item["dispatch_id"]
                item["pid"] = pid
                item["started_at"] = now_iso()
                running_count += 1
                title_str = f" ({item['title']})" if item.get("title") else ""
                print(f"起動: {item['ref']}{title_str}", file=sys.stderr)
            else:
                item["status"] = "failed"
                item["exit_code"] = -1
                item["finished_at"] = now_iso()

        save_state(state)

        # 完了判定
        counts = count_by_status(items)
        if counts.get("queued", 0) == 0 and counts.get("running", 0) == 0:
            # 全アイテムの最終チェック
            handle_stale_running(items)
            save_state(state)
            return

        print_status_line(items)
        time.sleep(POLL_INTERVAL)


def _print_report(state: dict) -> None:
    """最終レポートを JSON で出力する。"""
    items = state["items"]
    counts = count_by_status(items)

    report = {
        "date": state["date"],
        "status": state["status"],
        "started_at": state["started_at"],
        "finished_at": state["finished_at"],
        "summary": counts,
        "items": [
            {
                "ref": i["ref"],
                "title": i.get("title"),
                "status": i["status"],
                "exit_code": i.get("exit_code"),
                "started_at": i.get("started_at"),
                "finished_at": i.get("finished_at"),
            }
            for i in items
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


def cmd_start(args: argparse.Namespace) -> None:
    repo_dir = Path(args.repo).expanduser().resolve()
    if not repo_dir.exists():
        print(f"エラー: リポジトリが見つかりません: {repo_dir}", file=sys.stderr)
        sys.exit(1)

    date = args.date or today_str()
    run_dispatcher(repo_dir, date, args.max_slots, args.command)


def cmd_status(_args: argparse.Namespace) -> None:
    state = load_state()
    if not state:
        print("ディスパッチャーは実行されていません", file=sys.stderr)
        sys.exit(1)

    # 実行中の場合はセッション状態を更新
    if state.get("status") == "running":
        check_all_sessions(state["items"])
        save_state(state)

    _print_report(state)


def cmd_add(args: argparse.Namespace) -> None:
    state = load_state()
    if not state:
        print("エラー: ディスパッチャーが実行されていません", file=sys.stderr)
        sys.exit(1)
    if state.get("status") != "running":
        print("エラー: ディスパッチャーは実行中ではありません", file=sys.stderr)
        sys.exit(1)

    ref = args.ref
    dispatch_id = ref.replace("/", "-")

    # 重複チェック
    for item in state.get("items", []):
        if item["ref"] == ref:
            print(f"エラー: {ref} は既にディスパッチリストに含まれています", file=sys.stderr)
            sys.exit(1)

    working_dir = str(Path(args.working_dir).expanduser().resolve())
    if not Path(working_dir).is_dir():
        print(f"エラー: 作業ディレクトリが存在しません: {working_dir}", file=sys.stderr)
        sys.exit(1)

    new_item = asdict(DispatchItem(
        dispatch_id=dispatch_id,
        ref=ref,
        working_dir=working_dir,
        note=args.note,
        title=None,
        status="queued",
    ))

    state.setdefault("items", []).append(new_item)
    save_state(state)
    print(f"追加しました: {ref}", file=sys.stderr)


def cmd_stop(_args: argparse.Namespace) -> None:
    state = load_state()
    if not state:
        print("ディスパッチャーは実行されていません", file=sys.stderr)
        sys.exit(1)
    if state.get("status") != "running":
        print("ディスパッチャーは実行中ではありません", file=sys.stderr)
        sys.exit(1)

    # queued タスクを skipped に変更
    for item in state.get("items", []):
        if item["status"] == "queued":
            item["status"] = "skipped"
            item["finished_at"] = now_iso()

    state["status"] = "interrupted"
    state["finished_at"] = now_iso()
    save_state(state)
    print("停止要求を送信しました。実行中のセッションは継続します。", file=sys.stderr)


def cmd_kill(_args: argparse.Namespace) -> None:
    state = load_state()
    if not state:
        print("ディスパッチャーは実行されていません", file=sys.stderr)
        sys.exit(1)

    # tmux セッションを kill
    result = subprocess.run(
        ["tmux", "kill-session", "-t", TMUX_SESSION],
        capture_output=True,
    )
    if result.returncode == 0:
        print(f"tmux セッション '{TMUX_SESSION}' を終了しました", file=sys.stderr)
    else:
        print(
            f"警告: tmux セッションの終了に失敗しました: {result.stderr.decode().strip()}",
            file=sys.stderr,
        )

    # running アイテムを failed に
    for item in state.get("items", []):
        if item["status"] == "running":
            item["status"] = "failed"
            item["exit_code"] = -1
            item["finished_at"] = now_iso()
        elif item["status"] == "queued":
            item["status"] = "skipped"
            item["finished_at"] = now_iso()

    state["status"] = "interrupted"
    state["finished_at"] = now_iso()
    save_state(state)
    print("ディスパッチャーを強制終了しました", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="タスクディスパッチャー — デイリーゴールのタスクを並列実行する"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # start
    p_start = subparsers.add_parser("start", help="ディスパッチを開始する")
    p_start.add_argument(
        "--repo", required=True, metavar="PATH",
        help="タスク管理リポジトリのパス",
    )
    p_start.add_argument(
        "--date", metavar="YYYY-MM-DD", default=None,
        help="対象日付（デフォルト: 今日）",
    )
    p_start.add_argument(
        "--max-slots", type=int, default=DEFAULT_MAX_SLOTS,
        help=f"最大並列セッション数（デフォルト: {DEFAULT_MAX_SLOTS}）",
    )
    p_start.add_argument(
        "--command", default=DEFAULT_COMMAND,
        help=f"セッション起動コマンド（デフォルト: {DEFAULT_COMMAND}）",
    )
    p_start.set_defaults(func=cmd_start)

    # status
    p_status = subparsers.add_parser("status", help="実行状況を表示する")
    p_status.set_defaults(func=cmd_status)

    # add
    p_add = subparsers.add_parser("add", help="実行中のディスパッチにタスクを追加する")
    p_add.add_argument("--ref", required=True, help="タスク参照（例: jira/UBS-103）")
    p_add.add_argument("--working-dir", required=True, help="作業ディレクトリ")
    p_add.add_argument("--note", default=None, help="補足指示")
    p_add.set_defaults(func=cmd_add)

    # stop
    p_stop = subparsers.add_parser("stop", help="新規タスクの起動を停止する")
    p_stop.set_defaults(func=cmd_stop)

    # kill
    p_kill = subparsers.add_parser("kill", help="tmux セッションごと強制終了する")
    p_kill.set_defaults(func=cmd_kill)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
