# -*- coding: utf-8 -*-
"""E2E 验收：常驻 streamable-http 单例（对应当前运行中的实例）。

验收标准映射（MCP_REMOTE_SINGLETON_REQ_20261008_0220.md §7）：
  1. HTTP 握手 —— initialize 成功且 serverInfo.name == "instruments"
  2. 工具目录 —— tools/list == 68
  4. 内存     —— 常驻实例私有提交 < 100 MB（需求 A 生效后预期 ~50-90 MB）

留痕：TEST_DATA/common/mcp_http_e2e_<ts>.json
"""
import asyncio
import ctypes
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mcp import ClientSession                                  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client   # noqa: E402

URL = "http://127.0.0.1:8765/mcp"
PID_FILE = str(ROOT / "mcp_instruments" / "run" / "mcp_server.pid")

# v2dev 合并后（1b92cf8）默认 profile=compact（5 工具；68 操作都在 registry）。
# legacy=68。断言随环境变量 INSTRUMENT_MCP_PROFILE 或服务端默认自动适配。
def _expected_tools() -> int:
    import os
    prof = (os.environ.get("INSTRUMENT_MCP_PROFILE") or "compact").strip().lower()
    return 68 if prof == "legacy" else 5


class PMC(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def private_commit_mb(pid: int):
    PROCESS_QUERY_INFORMATION = 0x0400
    k = ctypes.windll.kernel32
    h = k.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        return None
    try:
        p = PMC()
        p.cb = ctypes.sizeof(PMC)
        if not ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(p), p.cb):
            return None
        return round(p.PagefileUsage / 1048576, 1)
    finally:
        k.CloseHandle(h)


async def main() -> None:
    out = {}
    with open(PID_FILE, encoding="utf-8") as f:
        pid = int(f.read().strip())
    out["pid"] = pid
    out["private_commit_mb"] = private_commit_mb(pid)

    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            out["serverInfo"] = {"name": init.serverInfo.name,
                                 "version": init.serverInfo.version}
            tools = await session.list_tools()
            out["tools_count"] = len(tools.tools)

    out["verdict_1_handshake"] = out["serverInfo"]["name"] == "instruments"
    _exp = _expected_tools()
    out["expected_tools"] = _exp
    out["verdict_2_tools_count"] = out["tools_count"] == _exp
    out["verdict_4_mem_lt_100mb"] = (out.get("private_commit_mb") or 1e9) < 100

    print(json.dumps(out, ensure_ascii=False, indent=2))
    ts = time.strftime("%Y%m%d_%H%M%S")
    dest = str(ROOT / "TEST_DATA" / "common" / f"mcp_http_e2e_{ts}.json")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("saved:", dest)
    if not all((out["verdict_1_handshake"], out["verdict_2_tools_count"], out["verdict_4_mem_lt_100mb"])):
        sys.exit(1)


asyncio.run(main())
