# -*- coding: utf-8 -*-
"""MCP 单例通用调用器：连 streamable-http 单例，调**一个**工具并打印结果。

用途：验收 / 调试时用 MCP 的真实调用路径（与 OpenCode、DSH 客户端相同）验证单个工具，
不必每次写脚本。

用法：
    python mcp_call_tool.py instr_discover
    python mcp_call_tool.py sds_measure --args '{"item":"FREQ"}'
    python mcp_call_tool.py psu_status --url http://127.0.0.1:8765/mcp

只读友好：本脚本自身不做任何过滤，调什么由调用方决定 —— 调用写类工具前请自行确认授权。
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mcp import ClientSession                                  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client   # noqa: E402

DEFAULT_URL = "http://127.0.0.1:8765/mcp"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tool", help="MCP 工具名，如 instr_discover / sds_status")
    ap.add_argument("--args", default="{}", help="JSON 对象形式的工具参数")
    ap.add_argument("--url", default=DEFAULT_URL)
    a = ap.parse_args()
    params = json.loads(a.args)

    async with streamablehttp_client(a.url) as (read, write, _):   # noqa: F841
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"# server={init.serverInfo.name} v{init.serverInfo.version} "
                  f"tool={a.tool} args={json.dumps(params, ensure_ascii=False)}")
            result = await session.call_tool(a.tool, params)
            text = "".join(c.text for c in result.content if hasattr(c, "text"))
            print(text)
            try:
                ok = json.loads(text).get("ok")
            except Exception:  # noqa: BLE001
                ok = None
            return 0 if ok is not False else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
