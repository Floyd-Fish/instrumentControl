"""跨进程会话锁的**实机**端到端验证：起两个 MCP 实例先后操作同一台仪器。

验证链：
    A 实例调用工具 → 写下自己的会话锁（带 PID）
    B 实例调用同一台仪器 → 返回体里应出现 "warnings"（"另一进程正在使用同一地址…"）
    A 退出（atexit 释放锁）→ B 再调用 → 告警应消失
    （A 异常退出不留锁也无妨：TTL/pid 判据会兜住）

需要仪器可达（默认 MHO；`--kind dg` 可换 DG832）。仪器不可达时**优雅 SKIP**（退出码 2），
不把"仪器离线"误报成"锁坏了"：

    python TEST_SCRIPTS/common/verify_session_lock_live.py [--kind mho|dg]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# 本脚本调的是 legacy 具名工具（按名字直接 tools/call）。server.py 的默认档已改为
# compact，不钉住就会 "Unknown tool"。
SERVER_ENV = {**os.environ, "INSTRUMENT_MCP_PROFILE": "legacy"}

AP = argparse.ArgumentParser()
AP.add_argument("--kind", default="mho", choices=["mho", "dg"])
ARGS = AP.parse_args()

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name:62s} {str(detail)[:100]}", flush=True)
    if not ok:
        fails.append(name)


class Server:
    """一个 MCP 服务端子进程（stdio JSON-RPC），可反复调工具。"""

    def __init__(self):
        self.p = subprocess.Popen(
            [sys.executable, str(ROOT / "mcp_instruments" / "server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, cwd=str(ROOT), env=SERVER_ENV)
        self._send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "sess-lock-live", "version": "1"}}})
        self._read()
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        self._n = 10

    def _send(self, obj: dict) -> None:
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def _read(self, timeout: float = 150.0) -> dict:
        box: dict = {}
        th = threading.Thread(target=lambda: box.update(l=self.p.stdout.readline()), daemon=True)
        th.start()
        th.join(timeout)
        line = box.get("l", "")
        return json.loads(line) if line.strip() else {}

    def call(self, tool: str, args: dict | None = None) -> dict:
        self._n += 1
        self._send({"jsonrpc": "2.0", "id": self._n, "method": "tools/call",
                    "params": {"name": tool, "arguments": args or {}}})
        r = self._read()
        try:
            return json.loads(r["result"]["content"][0]["text"])
        except Exception:
            return {"ok": False, "error": f"响应异常: {str(r)[:120]}"}

    def close(self) -> None:
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.wait(timeout=15)
        except Exception:
            self.p.kill()


TOOL = {"mho": "mho_status", "dg": "dg_status"}[ARGS.kind]

print(f"§0 仪器可达性（kind={ARGS.kind}, tool={TOOL}）", flush=True)
a = Server()
first = a.call(TOOL)
if not first.get("ok"):
    print(f"  [SKIP] 仪器不可达或工具报错：{first.get('error_type')} {str(first.get('error'))[:90]}")
    a.close()
    sys.exit(2)
print(f"  设备: {first.get('resource')}")

print("\n§1 A 在用时，B 调同一台仪器 → 应带'同一地址'告警", flush=True)
b = Server()
second = b.call(TOOL)
warns = second.get("warnings") or []
check("B 的返回体里有 warnings", bool(warns), json.dumps(warns, ensure_ascii=False)[:110])
check("告警点明同一地址 + PID",
      any("同一地址" in w for w in warns) and str(a.p.pid) in " ".join(warns),
      f"A.pid={a.p.pid}")
check("B 的工具本身仍然成功（只告警、不阻塞）", second.get("ok") is True, str(second.get("ok")))

print("\n§2 A 退出后 → B 再调用应无告警", flush=True)
a.close()
time.sleep(0.8)                       # 给 atexit 释放一点时间
third = b.call(TOOL)
check("A 释放后 B 不再被告警", not (third.get("warnings") or []),
      json.dumps(third.get("warnings") or [], ensure_ascii=False)[:110])
b.close()

print(f"\n== 结果: {'全部 PASS' if not fails else f'{len(fails)} 项 FAIL'} ==")
for f in fails:
    print(f"  - {f}")
sys.exit(1 if fails else 0)
