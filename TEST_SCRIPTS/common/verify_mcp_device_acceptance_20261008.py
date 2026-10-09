# -*- coding: utf-8 -*-
"""通过 MCP 单例验收仪器（只读）。

连接路径 = **streamable-http**（与 OpenCode / DSH 客户端完全相同），所以本脚本一次
回答两个问题：① MCP 服务是否正常工作；② 各台仪器是否可达、能否给出有效读数。

**只读口径**：只调用名字含 `status` 的工具（各设备的只读快照）。不调用任何
`auto_scale` / `set_*` / `output` / `write` / `power_cycle` 类工具 —— 不改变任何设备状态。
3458A 用 `ks3458a_status`（非 SCPI 表，走 GPIB）。

留痕：TEST_DATA/common/mcp_device_acceptance_<ts>.json
"""
import asyncio
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


def brief(text: str, limit: int = 220) -> str:
    """把工具返回压成一行摘要（保留 ok/error_type/关键字段）。"""
    try:
        d = json.loads(text)
    except Exception:  # noqa: BLE001
        return (text or "")[:limit]
    if not isinstance(d, dict):
        return json.dumps(d, ensure_ascii=False)[:limit]
    head = {"ok": d.get("ok")}
    if d.get("ok") is False:
        head["error_type"] = d.get("error_type")
        head["error"] = str(d.get("error"))[:160]
    else:
        res = d.get("result")
        if isinstance(res, dict):
            keys = ("model", "idn", "resource", "serial", "firmware", "mode",
                    "output", "voltage_v", "current_a", "value", "reading",
                    "channels", "items", "points", "safe")
            head["result"] = {k: res[k] for k in keys if k in res} or \
                             {"keys": list(res.keys())[:10]}
        else:
            head["result"] = str(res)[:160]
    return json.dumps(head, ensure_ascii=False, default=str)[:limit * 2]


async def main() -> int:
    out = {"url": URL, "ts": time.strftime("%Y%m%d_%H%M%S"), "tools": [], "calls": []}
    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            out["server"] = {"name": init.serverInfo.name, "version": init.serverInfo.version}
            tools = await session.list_tools()
            out["tools"] = sorted(t.name for t in tools.tools)

            status_tools = [t for t in out["tools"] if t.endswith("_status")]
            print(f"server={out['server']['name']} v{out['server']['version']} | "
                  f"tools={len(out['tools'])} | status 工具 {len(status_tools)} 个\n")
            print("tool list:", ", ".join(out["tools"]), "\n")

            for name in status_tools:
                t0 = time.time()
                try:
                    r = await session.call_tool(name, {})
                    text = "".join(c.text for c in r.content if hasattr(c, "text"))
                    ok = ('"ok": true' in text) or ('"ok":true' in text)
                    entry = {"tool": name, "ok": ok, "seconds": round(time.time() - t0, 2),
                             "brief": brief(text)}
                except Exception as e:  # noqa: BLE001
                    entry = {"tool": name, "ok": False, "seconds": round(time.time() - t0, 2),
                             "brief": f"{type(e).__name__}: {e}"}
                out["calls"].append(entry)
                mark = "PASS" if entry["ok"] else "FAIL"
                print(f"[{mark}] {entry['tool']:<28} {entry['seconds']:>6.2f}s  {entry['brief']}")

    okn = sum(1 for c in out["calls"] if c["ok"])
    out["summary"] = {"total": len(out["calls"]), "ok": okn,
                      "failed": [c["tool"] for c in out["calls"] if not c["ok"]]}
    print(f"\n=== 汇总: {okn}/{len(out['calls'])} PASS ===")
    if out["summary"]["failed"]:
        print("失败:", ", ".join(out["summary"]["failed"]))

    dest_dir = str(ROOT / "TEST_DATA" / "common")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"mcp_device_acceptance_{out['ts']}.json")
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("saved:", dest)
    return 0 if okn == len(out["calls"]) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
