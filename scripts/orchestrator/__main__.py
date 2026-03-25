"""CLI エントリポイント + クライアント。

python3 scripts/orchestrator server      — サーバ起動
python3 scripts/orchestrator dispatch    — タスクアクション送信
python3 scripts/orchestrator run ...     — ジョブ投入 (互換)
python3 -m orchestrator server           — PYTHONPATH=scripts 経由
"""

import argparse
import asyncio
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# python3 scripts/orchestrator で直接実行した場合に相対 import を有効にする
if __package__ is None or __package__ == "":
    import warnings
    _pkg_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(_pkg_dir.parent))
    __package__ = "orchestrator"
    importlib.import_module("orchestrator")
    warnings.filterwarnings("ignore", message="__package__ != __spec__.parent")

from .models import (
    DEFAULT_MAX_SLOTS, DEFAULT_REPO, SOCKET_DIR_NAME,
    get_socket_path, is_inside_sandbox,
)


# ---------------------------------------------------------------------------
# クライアント
# ---------------------------------------------------------------------------

async def client_send(request: dict, wait_response: bool = False) -> dict:
    socket_path = get_socket_path()
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path)
    except (ConnectionRefusedError, FileNotFoundError):
        if is_inside_sandbox():
            print(
                "エラー: オーケストレーターが起動していません。\n"
                "ホスト側で起動してください: systemctl --user start my-tasks-orchestrator",
                file=sys.stderr,
            )
            sys.exit(1)
        else:
            _start_server_background()
            for _ in range(10):
                await asyncio.sleep(0.5)
                try:
                    reader, writer = await asyncio.open_unix_connection(socket_path)
                    break
                except (ConnectionRefusedError, FileNotFoundError):
                    continue
            else:
                print("エラー: サーバへの接続に失敗しました", file=sys.stderr)
                sys.exit(1)

    writer.write(json.dumps(request, ensure_ascii=False).encode() + b"\n")
    await writer.drain()

    data = await reader.readline()
    response = json.loads(data.decode())

    if wait_response and response.get("ok") and response.get("waiting"):
        data = await reader.readline()
        if data:
            response = json.loads(data.decode())

    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return response


def _start_server_background():
    package_dir = Path(__file__).resolve().parent
    subprocess.Popen(
        [sys.executable, str(package_dir), "server"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# ---------------------------------------------------------------------------
# CLI コマンド
# ---------------------------------------------------------------------------

def cmd_dispatch(args: argparse.Namespace) -> None:
    """タスクアクションを送信する。"""
    action: dict[str, Any] = {"type": args.action_type}
    if args.action_type == "update_field":
        if not args.field or args.value is None:
            print("エラー: update_field には --field と --value が必要です", file=sys.stderr)
            sys.exit(1)
        action["field"] = args.field
        action["value"] = args.value

    request = {
        "command": "dispatch_action",
        "task_id": args.task_id,
        "action": action,
    }
    response = asyncio.run(client_send(request))
    if response.get("ok"):
        print("OK", file=sys.stderr)
    else:
        print(f"エラー: {response.get('error', 'Unknown error')}", file=sys.stderr)
        sys.exit(1)


def cmd_run(args: argparse.Namespace) -> None:
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    else:
        prompt = sys.stdin.read().strip()

    if not prompt:
        print("エラー: プロンプトが空です（--prompt-file またはstdin からプロンプトを読み取ります）", file=sys.stderr)
        sys.exit(1)

    request: dict[str, Any] = {
        "command": "run",
        "project_id": args.project,
        "prompt": prompt,
    }
    if args.session_id:
        request["session_id"] = args.session_id
    if args.sandbox_profile:
        request["sandbox_profile"] = args.sandbox_profile
    if args.branch:
        request["branch"] = args.branch

    response = asyncio.run(client_send(request))
    if response.get("ok"):
        print(f"{response.get('message', 'OK')}: {response.get('dispatch_id', '')}", file=sys.stderr)
        if response.get("session_id"):
            print(f"  session_id: {response['session_id']}", file=sys.stderr)
    else:
        print(f"エラー: {response.get('error', 'Unknown error')}", file=sys.stderr)
        sys.exit(1)


def cmd_resume_cli(args: argparse.Namespace) -> None:
    request: dict[str, Any] = {
        "command": "resume",
        "project_id": args.project,
        "session_id": args.session_id,
        "activate": True,
    }
    if args.worktree:
        request["worktree"] = args.worktree

    response = asyncio.run(client_send(request))
    if response.get("ok"):
        print(f"{response.get('message', 'OK')}", file=sys.stderr)
    else:
        print(f"エラー: {response.get('error', 'Unknown error')}", file=sys.stderr)
        sys.exit(1)


def cmd_open(args: argparse.Namespace) -> None:
    request: dict[str, Any] = {
        "command": "open",
        "project_id": args.project,
    }
    if args.session:
        request["session"] = args.session
    if args.sandbox_profile:
        request["sandbox_profile"] = args.sandbox_profile
    if args.prompt:
        request["prompt"] = args.prompt
    if args.worktree:
        request["worktree"] = args.worktree
    if args.activate:
        request["activate"] = True

    response = asyncio.run(client_send(request))
    if response.get("ok"):
        print(f"{response.get('message', 'OK')}", file=sys.stderr)
    else:
        print(f"エラー: {response.get('error', 'Unknown error')}", file=sys.stderr)
        sys.exit(1)


def cmd_status(args: argparse.Namespace) -> None:
    request = {"command": "status"}
    response = asyncio.run(client_send(request))

    if not response.get("ok"):
        print(f"エラー: {response.get('error', 'Unknown error')}", file=sys.stderr)
        sys.exit(1)

    jobs = response.get("jobs", [])

    if args.json:
        print(json.dumps({"jobs": jobs}, ensure_ascii=False, indent=2))
        return

    if not jobs:
        print("ジョブはありません", file=sys.stderr)
        return

    print("Jobs:", file=sys.stderr)
    for job in jobs:
        pid_str = str(job.get("pid", "")) or "-"
        session = job.get("session_id", "")[:8] if job.get("session_id") else "-"
        print(f"  {job['dispatch_id']:<20} {job['status']:<10} pid={pid_str:<8} session={session}  {job.get('started_at') or '-'}")

    counts: dict[str, int] = {}
    for job in jobs:
        s = job["status"]
        counts[s] = counts.get(s, 0) + 1
    parts = []
    for s in ["running", "queued", "done", "failed"]:
        if counts.get(s, 0) > 0:
            parts.append(f"{s}={counts[s]}")
    if parts:
        print(f"\n  [{', '.join(parts)}]")


def cmd_cancel(args: argparse.Namespace) -> None:
    request = {"command": "cancel", "dispatch_id": args.id}
    response = asyncio.run(client_send(request))
    if response.get("ok"):
        print(f"キャンセル: {args.id}", file=sys.stderr)
    else:
        print(f"エラー: {response.get('error', 'Unknown error')}", file=sys.stderr)
        sys.exit(1)


def cmd_kill(args: argparse.Namespace) -> None:
    if args.all:
        request = {"command": "kill-all"}
    else:
        request = {"command": "kill", "dispatch_id": args.id}

    response = asyncio.run(client_send(request))
    if response.get("ok"):
        print(response.get("message", "OK"), file=sys.stderr)
    else:
        print(f"エラー: {response.get('error', 'Unknown error')}", file=sys.stderr)
        sys.exit(1)


def cmd_wait(args: argparse.Namespace) -> None:
    request = {"command": "wait", "dispatch_id": args.id}
    response = asyncio.run(client_send(request, wait_response=True))
    if response.get("ok"):
        print(json.dumps(response, ensure_ascii=False, indent=2))
    else:
        print(f"エラー: {response.get('error', 'Unknown error')}", file=sys.stderr)
        sys.exit(1)


def cmd_log(args: argparse.Namespace) -> None:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/tmp/run-{os.getuid()}")
    log_path = Path(runtime_dir) / SOCKET_DIR_NAME / f"{args.id}.log"
    if not log_path.exists():
        print(f"エラー: ログファイルが見つかりません: {log_path}", file=sys.stderr)
        sys.exit(1)
    print(log_path.read_text(encoding="utf-8"), end="")


def cmd_server(args: argparse.Namespace) -> None:
    from .server import run_server
    asyncio.run(run_server(max_slots=args.max_slots, repo=args.repo))


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Orchestrator — タスク管理 + ジョブ実行"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # server
    p_server = subparsers.add_parser("server", help="オーケストレーターサーバを起動する")
    p_server.add_argument(
        "--max-slots", type=int, default=DEFAULT_MAX_SLOTS,
        help=f"最大並列スロット数（デフォルト: {DEFAULT_MAX_SLOTS}）",
    )
    p_server.add_argument(
        "--repo", default=DEFAULT_REPO, metavar="PATH",
        help=f"タスク管理リポジトリのパス（デフォルト: {DEFAULT_REPO}）",
    )
    p_server.set_defaults(func=cmd_server)

    # dispatch (NEW: タスクアクション)
    p_dispatch = subparsers.add_parser("dispatch", help="タスクアクションを送信する")
    p_dispatch.add_argument("task_id", help="タスク ID")
    p_dispatch.add_argument("action_type", help="アクション種別 (plan, dispatch, request_resume, request_feedback, done, abort, update_field)")
    p_dispatch.add_argument("--field", default="", help="update_field 用のフィールド名")
    p_dispatch.add_argument("--value", default="", help="update_field 用の値")
    p_dispatch.set_defaults(func=cmd_dispatch)

    # run
    p_run = subparsers.add_parser("run", help="ジョブを投入する（プロンプトは stdin or --prompt-file）")
    p_run.add_argument("--project", required=True, help="プロジェクト ID")
    p_run.add_argument("--prompt-file", default=None, help="プロンプトファイルのパス")
    p_run.add_argument("--session-id", default=None, help="Claude Code セッション ID（省略時は自動生成）")
    p_run.add_argument("--sandbox-profile", help="サンドボックスプロファイルを上書き指定")
    p_run.add_argument("--branch", default=None, help="worktree のブランチ名")
    p_run.set_defaults(func=cmd_run)

    # resume
    p_resume = subparsers.add_parser("resume", help="完了済みセッションを再開する")
    p_resume.add_argument("--project", required=True, help="プロジェクト ID")
    p_resume.add_argument("--session-id", required=True, help="再開するセッション ID")
    p_resume.add_argument("--worktree", default=None, help="worktree パス")
    p_resume.set_defaults(func=cmd_resume_cli)

    # open
    p_open = subparsers.add_parser("open", help="対話セッションを起動する")
    p_open.add_argument("--project", required=True, help="プロジェクト ID")
    p_open.add_argument("--session", default=None, help="tmux セッション名を明示指定")
    p_open.add_argument("--sandbox-profile", help="サンドボックスプロファイルを上書き指定")
    p_open.add_argument("--prompt", default=None, help="初期プロンプトを指定")
    p_open.add_argument("--worktree", default=None, help="worktree パス")
    p_open.add_argument("--activate", action="store_true", help="作成したウィンドウをアクティブにする")
    p_open.set_defaults(func=cmd_open)

    # status
    p_status = subparsers.add_parser("status", help="ジョブ状態を表示する")
    p_status.add_argument("--json", action="store_true", help="JSON 形式で出力する")
    p_status.set_defaults(func=cmd_status)

    # cancel
    p_cancel = subparsers.add_parser("cancel", help="キューからジョブを取り消す")
    p_cancel.add_argument("--id", required=True, help="ジョブ ID")
    p_cancel.set_defaults(func=cmd_cancel)

    # kill
    p_kill = subparsers.add_parser("kill", help="ジョブを強制停止する")
    group = p_kill.add_mutually_exclusive_group(required=True)
    group.add_argument("--id", help="ジョブ ID")
    group.add_argument("--all", action="store_true", help="全ジョブを停止する")
    p_kill.set_defaults(func=cmd_kill)

    # wait
    p_wait = subparsers.add_parser("wait", help="ジョブ完了を待機する")
    p_wait.add_argument("--id", required=True, help="ジョブ ID")
    p_wait.set_defaults(func=cmd_wait)

    # log
    p_log = subparsers.add_parser("log", help="ジョブの stdout/stderr ログを表示する")
    p_log.add_argument("--id", required=True, help="ジョブ ID")
    p_log.set_defaults(func=cmd_log)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
