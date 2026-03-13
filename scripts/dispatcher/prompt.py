"""システムプロンプト構築。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sandbox_exec

from .models import Job


def build_system_prompt(job: Job, result_path: Path) -> str:
    """ジョブ用のシステムプロンプトを構築する。"""
    profile = sandbox_exec.resolve_profile(job.sandbox_profile_id)
    network_protected = sandbox_exec.uses_network_protection(profile)

    network_desc = ""
    if network_protected:
        network_desc = """
制約事項 (ネットワーク保護あり):
- ネットワーク: GitHub/Bitbucket SSH と HTTP プロキシ経由の HTTPS のみ利用可能
- ファイル: 作業ディレクトリ内のファイルのみ変更可能"""

    cred_desc = ""
    pass_cmd = next((c for c in job.host_commands if c["name"] == "pass"), None)
    if pass_cmd:
        allowed_patterns = pass_cmd.get("allowed_patterns", [])
        if allowed_patterns == "*":
            cred_desc = """

認証情報:
- `pass show <entry>` で全ての認証情報を取得できます"""
        else:
            show_entries = [pat[5:] for pat in allowed_patterns if pat.startswith("show ")]
            if show_entries:
                entries = "\n".join(f"  - {e}" for e in show_entries)
                cred_desc = f"""

認証情報:
- `pass show <entry>` で以下の認証情報を取得できます:
{entries}"""

    other_cmds = [c for c in job.host_commands if c["name"] != "pass"]
    host_cmd_desc = ""
    if other_cmds:
        cmd_lines = []
        for cmd in other_cmds:
            patterns = cmd.get("allowed_patterns", [])
            if patterns == "*":
                cmd_lines.append(f"  - `{cmd['name']}` (全引数パターン許可)")
            else:
                cmd_lines.append(f"  - `{cmd['name']}` (許可パターン: {', '.join(patterns)})")
        host_cmd_desc = "\n\nホストコマンド:\n- 以下のコマンドがホスト側で実行されます:\n" + "\n".join(cmd_lines)

    result_desc = ""
    if job.job_type == "refine":
        result_desc = f"""

結果ファイル:
ジョブ完了時、以下のパスに結果 JSON を書き出してください:
  {result_path}

精査ジョブの結果フォーマット:
  {{"next_status": "scoped"}} — 精査完了、実行可能
  {{"next_status": "needs_input"}} — ユーザへの質問あり
  {{"next_status": "reshaping"}} — 再精査後、問題なし（完了確認待ち）"""
    elif job.job_type == "evaluate":
        result_desc = f"""

結果ファイル:
ジョブ完了時、以下のパスに結果 JSON を書き出してください:
  {result_path}

評価ジョブの結果フォーマット:
  {{"verdict": "PASS", "summary": "..."}} — 達成条件すべて満たされている
  {{"verdict": "RETRY", "summary": "..."}} — 再実行で修正可能
  {{"verdict": "BLOCKED", "summary": "..."}} — ユーザ入力が必要
  {{"verdict": "ABORT", "summary": "..."}} — 実行不可能"""

    network_mode = "保護あり (netns + proxy)" if network_protected else "ホストネットワーク直接"
    return f"""あなたはサンドボックス環境で実行されています。

実行環境:
- 作業ディレクトリ: {job.working_dir}
- ネットワーク: {network_mode}
{network_desc}{cred_desc}{host_cmd_desc}{result_desc}

作業が完了したら、変更をコミットしてください。
プロセスの終了がジョブ完了の通知になります（シグナルファイルは不要です）。"""
