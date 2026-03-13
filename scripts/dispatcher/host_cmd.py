"""HostCommandBroker - ホワイトリスト制御でホスト側コマンドを実行するブローカー。"""

import asyncio
import fnmatch
import json

from .models import log


class HostCommandBroker:

    def __init__(self):
        self._registry: dict[str, list[dict]] = {}  # token -> host_commands list

    def register(self, token: str, host_commands: list[dict]) -> None:
        self._registry[token] = host_commands
        names = [cmd["name"] for cmd in host_commands]
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
            response = await self._process_request(token, command, args, stdin)
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

    async def _process_request(self, token: str, command: str, args: list[str], stdin: str | None) -> dict:
        if not token or token not in self._registry:
            log.warning(f"Host command broker: invalid token {token[:8]}..." if token else "Host command broker: empty token")
            return {"ok": False, "error": "invalid token"}

        host_commands = self._registry[token]

        cmd_def = next((c for c in host_commands if c["name"] == command), None)
        if cmd_def is None:
            log.warning(f"Host command broker: command not allowed: {command} (token {token[:8]}...)")
            return {"ok": False, "error": f"command not allowed: {command}"}

        allowed_patterns = cmd_def.get("allowed_patterns", [])
        if allowed_patterns != "*":
            args_str = " ".join(args)
            if not any(fnmatch.fnmatch(args_str, pat) for pat in allowed_patterns):
                log.warning(f"Host command broker: args not allowed: {command} {args_str} (token {token[:8]}...)")
                return {"ok": False, "error": f"args not allowed: {command} {args_str}"}

        if stdin is not None and not cmd_def.get("allow_stdin", False):
            log.warning(f"Host command broker: stdin not allowed for {command} (token {token[:8]}...)")
            return {"ok": False, "error": f"stdin not allowed for {command}"}

        try:
            proc = await asyncio.create_subprocess_exec(
                cmd_def["path"], *args,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
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
