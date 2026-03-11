"""Dashboard エントリポイント。"""

import argparse
import importlib
import sys
import warnings
from pathlib import Path

# python3 scripts/dashboard で直接実行した場合に相対 import を有効にする
if __package__ is None or __package__ == "":
    _pkg_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(_pkg_dir.parent))
    __package__ = "dashboard"
    importlib.import_module("dashboard")
    warnings.filterwarnings("ignore", message="__package__ != __spec__.parent")

from .app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="my-tasks ダッシュボード")
    parser.add_argument("--host", default="127.0.0.1", help="バインドアドレス")
    parser.add_argument("--port", type=int, default=8321, help="ポート番号")
    parser.add_argument("--repo", default="~/.local/share/my-tasks", help="タスクリポジトリのパス")
    parser.add_argument("--base-path", default="", help="リバースプロキシのベースパス (例: /dashboard)")
    args = parser.parse_args()

    import uvicorn
    app = create_app(args.repo, args.base_path)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
