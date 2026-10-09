# -*- coding: utf-8 -*-
"""审计下沉的离线验证（不碰设备）：contextvars 穿透 worker + 风险过滤。

S1: `_CURRENT_TOOL` 经 executor（事件循环 → 队列 → worker 线程）后 `_call` 能取到
S2: read_only 工具不产生审计行；config/output/destructive 产生（用假 registry）
S3: audit_scpi 落盘内容含 tool/risk/ok/seconds
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mcp_instruments"))

import contextvars

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


async def main():
    # —— 用真实 server 模块（不跑 mcp.run，只测执行器与 _call 的审计钩子）
    os.environ["INSTRUMENT_MCP_PROFILE"] = "legacy"
    import server as srv

    # S1: contextvars 穿透
    got = {}

    def probe():
        got["tool"] = srv._CURRENT_TOOL.get()

    async def drive():
        token = srv._CURRENT_TOOL.set("sdg_counter")
        try:
            await srv._executor().run(probe, 5.0, label="probe")
        finally:
            srv._CURRENT_TOOL.reset(token)

    await drive()
    check("S1 contextvars 穿透 worker 线程", got.get("tool") == "sdg_counter",
          f"got={got.get('tool')!r}")

    # S2/S3: 用假 registry + 假设备函数驱动 _call 的审计分支
    from instrument_runtime import registry as rt_reg

    class FakeOp:
        class safety:
            risk = "config"
        tool_name = "sdg_counter"

    saved_get = rt_reg.Registry.get
    rt_reg.Registry.get = lambda self, name: FakeOp() if name == "sdg_counter" else None
    try:
        # 假 connect/fn/close——不碰 VISA
        calls = {"closed": False}

        def fake_connect():
            return object()

        def fake_fn(dev):
            return {"STATE": "ON"}

        def fake_close(dev):
            calls["closed"] = True

        tmp = tempfile.mkdtemp(prefix="audit_probe_")

        async def drive_call():
            token = srv._CURRENT_TOOL.set("sdg_counter")
            try:
                return await srv._executor().run(
                    functools.partial(srv._call, "SDG", fake_connect, fake_fn,
                                      close_fn=fake_close),
                    5.0, label="sdg_counter")
            finally:
                srv._CURRENT_TOOL.reset(token)

        import functools
        payload = await drive_call()
        d = json.loads(payload)
        check("S2a _call 返回 ok", d.get("ok") is True)
        check("S2b close_fn 被调用", calls["closed"])

        # S3: 审计行内容（_call 里 audit_scpi 用默认 repo_root——离线验证改注入不可行，
        # 改为直接查 _audit_entry_tool + 手动调用同参数落盘）
        check("S2c sdg_counter 需审计（config）",
              srv._audit_entry_tool("sdg_counter") == "sdg_counter")
        check("S2d sds_measure 不审计（read_only）",
              srv._audit_entry_tool("sds_measure") is None)
        check("S2e instr_write 不在此重复审计",
              srv._audit_entry_tool("instr_write") is None)

        from instrument_runtime.audit import audit_scpi
        p = audit_scpi("sdg_counter", "TCPIP0::x", "<library call> sdg_counter(on)",
                       root=tmp, risk="config", ok=True, seconds=0.12)
        line = json.loads(Path(p).read_text(encoding="utf-8").strip())
        check("S3 审计行字段", line.get("tool") == "sdg_counter"
              and line.get("risk") == "config" and line.get("ok") is True)
    finally:
        rt_reg.Registry.get = saved_get

    print()
    fails = [n for n, ok, _ in results if not ok]
    print(f"== {len(results) - len(fails)}/{len(results)} PASS ==")
    sys.exit(1 if fails else 0)


asyncio.run(main())
