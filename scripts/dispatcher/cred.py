"""CredentialBroker - ジョブ単位でスコープされた認証情報アクセスを提供するブローカー。"""

import asyncio
import fnmatch
import json

from .models import log


class CredentialBroker:

    def __init__(self):
        self._registry: dict[str, list[str] | str] = {}  # token -> allowed entries

    def register(self, token: str, allowed_entries: list[str] | str) -> None:
        self._registry[token] = allowed_entries
        if allowed_entries == "*":
            log.info(f"Credential broker: registered token {token[:8]}... (all entries)")
        else:
            log.info(f"Credential broker: registered token {token[:8]}... ({len(allowed_entries)} entries)")

    def revoke(self, token: str) -> None:
        if token in self._registry:
            del self._registry[token]
            log.info(f"Credential broker: revoked token {token[:8]}...")

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            data = await reader.readline()
            if not data:
                return
            request = json.loads(data.decode())
            token = request.get("token", "")
            entry = request.get("entry", "")
            operation = request.get("operation", "show")
            value = request.get("value", "")
            response = await self._process_request(token, entry, operation, value)
            writer.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
            await writer.drain()
        except Exception as e:
            log.error(f"Credential broker client error: {e}")
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

    async def _process_request(self, token: str, entry: str, operation: str = "show", value: str = "") -> dict:
        if not token or token not in self._registry:
            log.warning(f"Credential broker: invalid token {token[:8]}..." if token else "Credential broker: empty token")
            return {"ok": False, "error": "invalid token"}

        allowed = self._registry[token]
        if allowed != "*" and not any(fnmatch.fnmatch(entry, pat) for pat in allowed):
            log.warning(f"Credential broker: entry not allowed: {entry} (token {token[:8]}...)")
            return {"ok": False, "error": f"entry not allowed: {entry}"}

        try:
            if operation == "insert":
                proc = await asyncio.create_subprocess_exec(
                    "/usr/bin/pass", "insert", "--force", "--echo", entry,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate(input=value.encode())
                if proc.returncode != 0:
                    log.error(f"Credential broker: pass insert failed for {entry}: {stderr.decode().strip()}")
                    return {"ok": False, "error": "credential insert failed"}
                log.info(f"Credential broker: inserted {entry} (token {token[:8]}...)")
                return {"ok": True}
            else:
                proc = await asyncio.create_subprocess_exec(
                    "/usr/bin/pass", "show", entry,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode != 0:
                    log.error(f"Credential broker: pass failed for {entry}: {stderr.decode().strip()}")
                    return {"ok": False, "error": "credential retrieval failed"}
                log.info(f"Credential broker: served {entry} (token {token[:8]}...)")
                return {"ok": True, "value": stdout.decode()}
        except Exception as e:
            log.error(f"Credential broker: pass execution error: {e}")
            return {"ok": False, "error": "credential operation failed"}
