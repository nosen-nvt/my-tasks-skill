"""CLI エントリポイント + クライアント。

python3 scripts/dispatcher server   — パッケージ実行
python3 -m dispatcher server        — PYTHONPATH=scripts 経由
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

# python3 scripts/dispatcher で直接実行した場合に相対 import を有効にする
if __package__ is None or __package__ == "":
    import warnings
    _pkg_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(_pkg_dir.parent))
    __package__ = "dispatcher"
    importlib.import_module("dispatcher")
    warnings.filterwarnings("ignore", message="__package__ != __spec__.parent")

from .models import (
    DEFAULT_MAX_SLOTS, DEFAULT_REPO, SOCKET_DIR_NAME,
    get_socket_path, get_repo_dir, is_inside_sandbox,
)
from . import tasks as task_helpers


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
                "エラー: ディスパッチサーバが起動していません。\n"
                "ホスト側で起動してください: systemctl --user start my-tasks-dispatcher",
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

def cmd_run(args: argparse.Namespace) -> None:
    if args.task:
        repo_dir = get_repo_dir(args.repo)
        task_info = task_helpers.load_task_entry(repo_dir / "tasks", args.task)
        if not task_info:
            print(f"エラー: タスクが見つかりません: {args.task}", file=sys.stderr)
            sys.exit(1)

        project_id = task_info.get("project_id", "")
        if not project_id:
            print(f"エラー: タスク {args.task} に project_id が設定されていません", file=sys.stderr)
            sys.exit(1)

        prompt = task_helpers.read_execution_prompt(repo_dir, args.task)
        if not prompt:
            print(f"エラー: タスク {args.task} に実行プロンプトがありません", file=sys.stderr)
            sys.exit(1)

        request = {
            "command": "run",
            "project_id": project_id,
            "task_id": args.task,
            "job_type": "execute",
            "prompt": prompt,
        }
    else:
        prompt = sys.stdin.read().strip()
        if not prompt:
            print("エラー: プロンプトが空です（stdin からプロンプトを読み取ります）", file=sys.stderr)
            sys.exit(1)

        request = {
            "command": "run",
            "project_id": args.project,
            "prompt": prompt,
        }
        if args.task_id:
            request["task_id"] = args.task_id
        if args.job_type:
            request["job_type"] = args.job_type

    if args.sandbox_profile:
        request["sandbox_profile"] = args.sandbox_profile

    response = asyncio.run(client_send(request))
    if response.get("ok"):
        print(f"{response.get('message', 'OK')}: {response.get('dispatch_id', '')}", file=sys.stderr)
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
    lifecycles = response.get("lifecycles", [])

    if args.json:
        print(json.dumps({"jobs": jobs, "lifecycles": lifecycles}, ensure_ascii=False, indent=2))
        return

    active_lcs = [lc for lc in lifecycles if lc.get("status") != "done"]
    if active_lcs:
        print("Lifecycles:", file=sys.stderr)
        for lc in active_lcs:
            task_str = lc.get("task_id", "") or "-"
            reason = f" ({lc['suspend_reason']})" if lc.get("suspend_reason") else ""
            print(f"  {lc['lifecycle_id']:<10} {lc['status']:<12}{reason}  project={lc['project_id']:<12} task={task_str:<16} runs={lc.get('run_count', 0)}/{lc.get('max_runs', 5)}")
        print(file=sys.stderr)

    if not jobs and not active_lcs:
        print("ジョブはありません", file=sys.stderr)
        return

    if jobs:
        print("Jobs:", file=sys.stderr)
        for job in jobs:
            pid_str = str(job.get("pid", "")) or "-"
            task_str = job.get("task_id", "") or "-"
            jtype = job.get("job_type", "execute")
            print(f"  {job['dispatch_id']:<20} {job['status']:<10} {jtype:<10} pid={pid_str:<8} task={task_str:<16} {job.get('started_at') or '-'}")

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


def cmd_dispatch_cli(args: argparse.Namespace) -> None:
    request: dict[str, Any] = {"command": "dispatch"}
    if args.task:
        request["task_id"] = args.task
    if args.project:
        request["project_id"] = args.project
    if args.prompt:
        request["prompt"] = args.prompt
    elif not args.task and not sys.stdin.isatty():
        request["prompt"] = sys.stdin.read().strip()

    response = asyncio.run(client_send(request))
    if response.get("ok"):
        print(f"{response.get('message', 'OK')}", file=sys.stderr)
        if response.get("dispatch_id"):
            print(f"  dispatch_id: {response['dispatch_id']}", file=sys.stderr)
    else:
        print(f"エラー: {response.get('error', 'Unknown error')}", file=sys.stderr)
        sys.exit(1)


def cmd_resume_cli(args: argparse.Namespace) -> None:
    request: dict[str, Any] = {"command": "resume", "lifecycle_id": args.id}
    if args.project:
        request["project_id"] = args.project

    response = asyncio.run(client_send(request))
    if response.get("ok"):
        print(f"{response.get('message', 'OK')}", file=sys.stderr)
    else:
        print(f"エラー: {response.get('error', 'Unknown error')}", file=sys.stderr)
        sys.exit(1)


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
        description="Unix ドメインソケット C/S ジョブランナー"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # server
    p_server = subparsers.add_parser("server", help="ディスパッチサーバを起動する")
    p_server.add_argument(
        "--max-slots", type=int, default=DEFAULT_MAX_SLOTS,
        help=f"最大並列スロット数（デフォルト: {DEFAULT_MAX_SLOTS}）",
    )
    p_server.add_argument(
        "--repo", default=DEFAULT_REPO, metavar="PATH",
        help=f"タスク管理リポジトリのパス（デフォルト: {DEFAULT_REPO}）",
    )
    p_server.set_defaults(func=cmd_server)

    # run
    p_run = subparsers.add_parser("run", help="ジョブを投入する")
    run_group = p_run.add_mutually_exclusive_group(required=True)
    run_group.add_argument("--task", help="タスク ID（index.jsonl + .md からプロンプトを読み取る）")
    run_group.add_argument("--project", help="プロジェクト ID（プロンプトは stdin から）")
    p_run.add_argument(
        "--repo", default=DEFAULT_REPO, metavar="PATH",
        help=f"タスク管理リポジトリのパス（デフォルト: {DEFAULT_REPO}）",
    )
    p_run.add_argument("--task-id", help="タスク ID（--project モードでオーケストレーション用に指定）")
    p_run.add_argument("--job-type", default=None, help="ジョブタイプ: execute, evaluate, refine")
    p_run.add_argument("--sandbox-profile", help="サンドボックスプロファイルを上書き指定")
    p_run.set_defaults(func=cmd_run)

    # open
    p_open = subparsers.add_parser("open", help="対話セッションを起動する")
    p_open.add_argument("--project", required=True, help="プロジェクト ID")
    p_open.add_argument("--session", default=None, help="tmux セッション名を明示指定")
    p_open.add_argument("--sandbox-profile", help="サンドボックスプロファイルを上書き指定")
    p_open.set_defaults(func=cmd_open)

    # dispatch
    p_dispatch = subparsers.add_parser("dispatch", help="ライフサイクルを開始する")
    p_dispatch.add_argument("--task", help="タスク ID")
    p_dispatch.add_argument("--project", help="プロジェクト ID")
    p_dispatch.add_argument("--prompt", help="プロンプト（省略時は stdin）")
    p_dispatch.set_defaults(func=cmd_dispatch_cli)

    # resume
    p_resume = subparsers.add_parser("resume", help="suspend 中のライフサイクルを再開する")
    p_resume.add_argument("--id", required=True, help="ライフサイクル ID")
    p_resume.add_argument("--project", default=None, help="プロジェクト ID（project_confirmation 時に指定）")
    p_resume.set_defaults(func=cmd_resume_cli)

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
