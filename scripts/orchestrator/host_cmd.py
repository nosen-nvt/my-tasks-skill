"""HostCommandBroker - ホワイトリスト制御でホスト側コマンドを実行するブローカー。"""

import asyncio
import json
import os

from .models import log

from host_cmd_common import (
    check_command_args,
    merge_command_defs,
    resolve_cmd_env_sync,
    validate_cwd,
)


class HostCommandBroker:

    def __init__(self):
        self._registry: dict[str, list[dict]] = {}  # token -> host_commands list

    def register(self, token: str, host_commands: list[dict]) -> None:
        # env を事前解決して _resolved_env に格納
        resolved_commands: list[dict] = []
        for cmd in host_commands:
            cmd = dict(cmd)
            if cmd.get("env"):
                cmd["_resolved_env"] = resolve_cmd_env_sync(cmd["env"])
            resolved_commands.append(cmd)
        self._registry[token] = resolved_commands
        names = [cmd["name"] for cmd in resolved_commands]
        log.info(f"Host command broker: registered token {token[:8]}... (commands: {', '.join(names)})")

    def revoke(self, token: str) -> None:
        if token in self._registry:
            del self._registry[token]
            log.info(f"Host command broker: revoked token {token[:8]}...")

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            data = await reader.readline()
            if not data:
                return
            request = json.loads(data.decode())
            token = request.get("token", "")
            command = request.get("command", "")
            args = request.get("args", [])
            stdin = request.get("stdin")
            cwd = request.get("cwd")
            response = await self._process_request(token, command, args, stdin, cwd)
            writer.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
            await writer.drain()
        except Exception as e:
            log.error(f"Host command broker client error: {e}")
            try:
                writer.write(json.dumps({"ok": False, "error": "internal error"}).encode() + b"\n")
                await writer.drain()
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _process_request(self, token: str, command: str, args: list[str],
                               stdin: str | None, cwd: str | None) -> dict:
        if not token or token not in self._registry:
            log.warning(f"Host command broker: invalid token {token[:8]}..." if token else "Host command broker: empty token")
            return {"ok": False, "error": "invalid token"}

        host_commands = self._registry[token]

        matching_defs = [c for c in host_commands if c["name"] == command]
        if not matching_defs:
            log.warning(f"Host command broker: command not allowed: {command} (token {token[:8]}...)")
            return {"ok": False, "error": f"command not allowed: {command}"}

        merged = merge_command_defs(matching_defs)

        # cwd 検証
        cwd_error = validate_cwd(cwd, merged)
        if cwd_error:
            log.warning(f"Host command broker: cwd validation failed: {cwd_error} (token {token[:8]}...)")
            return {"ok": False, "error": cwd_error}

        # 引数検証 (denied_patterns → allowed_subcommands → allowed_patterns)
        args_error = check_command_args(merged, args)
        if args_error:
            log.warning(f"Host command broker: {args_error} (token {token[:8]}...)")
            return {"ok": False, "error": args_error}

        # stdin 検証
        if stdin is not None and not merged.get("allow_stdin", False):
            log.warning(f"Host command broker: stdin not allowed for {command} (token {token[:8]}...)")
            return {"ok": False, "error": f"stdin not allowed for {command}"}

        # 実行環境の構築
        exec_env = None
        resolved_env = merged.get("_resolved_env", {})
        if resolved_env:
            exec_env = os.environ.copy()
            exec_env.update(resolved_env)

        exec_cwd = cwd if cwd else None

        try:
            proc = await asyncio.create_subprocess_exec(
                merged["path"], *args,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=exec_env,
                cwd=exec_cwd,
            )
            stdout, stderr = await proc.communicate(input=stdin.encode() if stdin is not None else None)
            log.info(f"Host command broker: executed {command} {' '.join(args)} (token {token[:8]}...) exit={proc.returncode}")
            return {
                "ok": True,
                "exit_code": proc.returncode,
                "stdout": stdout.decode(),
                "stderr": stderr.decode(),
            }
        except Exception as e:
            log.error(f"Host command broker: execution error: {e}")
            return {"ok": False, "error": "execution failed"}
