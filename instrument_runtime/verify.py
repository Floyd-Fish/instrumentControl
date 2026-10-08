"""执行后验证——多段回读的字段配名、错误队列排空。

方案 C 阶段 2：从 `mcp_instruments/server.py` 抽出。两者都是**与设备类型无关**的
通用验证步骤，任何 frontend（MCP / CLI / 代码运行时）都应做同样的检查，故归入
runtime 层。`pair_readback` 是纯函数；`drain_errors` 只依赖连接对象的 `query()`
（鸭子类型），因此本模块不 import pyvisa，可离线测试。
"""
from __future__ import annotations

import re

__all__ = ["ERR_CLEAN_RE", "pair_readback", "drain_errors"]

ERR_CLEAN_RE = re.compile(r"^\+?0\s*,")


def pair_readback(cmd: str | None, resp: str | None) -> list[dict] | None:
    """多段回读的**字段配名**：':C1:SCALe?;:C1:OFFSet?' → [{cmd,value}, {cmd,value}]。

    现场教训（docs/tool_optimization_20260915.md §P2-6）：`;` 串联的多条回读原先
    只把响应原样拼成 "a;b" 返回——没有字段名，调用方要自己数字段、**写错顺序不报错**。
    这里按查询段与返回段一一配对；段数不符时如实说明（不硬配）。
    """
    if not cmd or resp is None or ";" not in cmd:
        return None
    units = [u.strip() for u in cmd.split(";") if u.strip()]
    vals = [v.strip() for v in resp.split(";")]
    if len(units) != len(vals):
        return [{"cmd": cmd, "value": resp,
                 "note": f"段数不符（查询 {len(units)} 段 / 返回 {len(vals)} 段），未配对名"}]
    return [{"cmd": u, "value": v} for u, v in zip(units, vals)]


def drain_errors(c) -> list[str]:
    """排空 SYST:ERR? 队列（铁律2），上限 20 条防死循环。

    `c` 只需提供 `query(str) -> str`。
    """
    errs: list[str] = []
    for _ in range(20):
        try:
            r = c.query("SYST:ERR?").strip()
        except Exception as e:
            return errs + [f"(SYST:ERR? 查询失败: {type(e).__name__})"]
        if ERR_CLEAN_RE.match(r) or "no error" in r.lower():
            return errs
        errs.append(r)
    return errs + ["(错误队列超过 20 条，停止排空)"]
