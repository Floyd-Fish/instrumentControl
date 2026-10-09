# -*- coding: utf-8 -*-
"""核对需求 B 的技术前提（离线，不连仪器、不起服务）：

1. mcp SDK 版本是否为文档所称 1.27.0；
2. `FastMCP.__init__` 是否原生接受 host/port/streamable_http_path；
3. `mcp.settings` 字段是否可直接赋值（文档要求改 port 的写法）；
4. `mcp.run` 是否接受 transport="streamable-http"；
5. 当前工具数是否为验收标准所称 68。

import server 会执行模块级代码（启动预热线程），但不会进 `__main__`、不跑 mcp.run()。
"""
import importlib.metadata as md
import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mcp_instruments"))

from mcp.server.fastmcp import FastMCP  # noqa: E402

res = {
    "mcp_version": md.version("mcp"),
    "FastMCP_init_params": list(inspect.signature(FastMCP.__init__).parameters),
    "mcp_run_params": list(inspect.signature(FastMCP.run).parameters),
}

import server  # noqa: E402

res["tools_count"] = len(server.mcp._tool_manager.list_tools())

s = server.mcp.settings
res["settings_attrs"] = {
    k: getattr(s, k, "<absent>")
    for k in ("host", "port", "streamable_http_path", "stateless_http")
}
try:
    old = s.port
    s.port = 8765
    res["settings_port_assignable"] = (s.port == 8765)
    s.port = old          # 还原，不改默认
except Exception as e:  # noqa: BLE001
    res["settings_port_assignable"] = "FAIL %s" % e

res["has_run_transport_param"] = "transport" in res["mcp_run_params"]
res["has_init_streamable_path"] = "streamable_http_path" in res["FastMCP_init_params"]

print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
