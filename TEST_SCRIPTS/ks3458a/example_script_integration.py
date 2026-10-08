"""3458A 脚本集成示例：① 直接用库（推荐） ② 走 MCP（stdio JSON-RPC）。

两种方式调的是**同一份代码**（MCP 工具内部就是 `keysight_3458a` 库），区别：

| 方式 | 适合 | 代价 |
|---|---|---|
| ① 直接用库 | 批量采集/长脚本/需要精细控制（突发、逐点 CSV、异常处理） | 自己负责会话生命周期（`with`/`close`）；**没有** MCP 的设备锁与跨进程互斥 |
| ② 走 MCP | 想和 AI 共用一把锁、复用安全门（`confirm`）、不关心实现细节 | 每个脚本要起一个 MCP 子进程（~1 s）；返回值是 JSON 字符串 |

用法::

    python example_script_integration.py --dry-run            # **不连设备**，只打印将要做什么
    python example_script_integration.py              # ① 库：读 3 次 + 状态回读（只读）
    python example_script_integration.py --via mcp    # ② MCP：ks3458a_status + ks3458a_read
    python example_script_integration.py --burst      # ①库 + 100 点 SINT 突发（**会改设备配置**，
                                                     #   库会自动 PRESET NORM 恢复）

⚠ 运行时**要求设备空闲**：库里接了 `common/session_lock` 辅助锁，若别的进程/MCP 会话
正在用同一地址，`connect()` 会直接拒绝并列出占用者（同一台表两个会话会静默串台）。
确认对方已死才用 `connect(force=True)`。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dh1766_control" / "src"))

RESOURCE = "GPIB0::9::INSTR"          # 或 "sicl:gpib0,9" / "visa://<host>/GPIB0::9::INSTR"
SERVER = ROOT / "mcp_instruments" / "server.py"

# 示例的 MCP 分支按名字调 legacy 具名工具（ks3458a_*）。server.py 的默认档已改为
# compact，不显式指定就会 tools/call 失败。
SERVER_ENV = {**os.environ, "INSTRUMENT_MCP_PROFILE": "legacy"}

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="ascii", errors="replace")
    except Exception:
        pass


def dry_run(do_burst: bool) -> None:
    """**不连设备**：打印将要下发的命令序列（写脚本时先跑这个确认意图）。"""
    print("== --dry-run（不连设备）==")
    print("  ① 库: DMM3458A(%r).connect(recover='auto')" % RESOURCE)
    print("     → prepare_for_read: END ALWAYS / INBUF ON / TRIG AUTO")
    print("     → state(): ID? ERRSTR? TARM? TRIG? NRDGS? NPLC? APER? FUNC? RANGE? ARANGE?"
          " AZERO? MEM? INBUF? END? OFORMAT? MFORMAT? ISCALE? TEMP?")
    print("     → read_dcv() x3 / read_avg(3)：TARM SGL,1（失败会自动 recover+重试一次）")
    if do_burst:
        print("     → read_burst(100, 1e-4, dcv_range=0.1, data_format='SINT')："
              "PRESET DIG / MFORMAT SINT / OFORMAT SINT / TIMER / MEM OFF / NRDGS 100 /"
              " TRIG AUTO / ISCALE? / TARM SYN → 读 202 字节；收尾后 PRESET NORM 恢复")
        print("     → 恢复后 configure_dcv(0.1, 10) 把档位/NPLC 设回")
    print("  ② MCP: 起 server.py 子进程 → initialize → tools/list → "
          "call_tool('ks3458a_status'|'ks3458a_read'|'ks3458a_read_avg')")
    print("  占用保护: connect() 前查 common/session_lock，有他人占用则拒绝（force=True 可强抢）")


def via_library(do_burst: bool = False) -> None:
    """① 直接用库：连接 → 状态回读 → 读数 →（可选）突发 → 关会话。"""
    from keysight_3458a import DMM3458A, is_error_clear

    # 上下文管理器 = connect(recover="auto") + close()（close 前会 TARM HOLD 收尾）
    try:
        with DMM3458A(RESOURCE, timeout_s=20.0) as d:
            print("== ① 直接用库 ==")
            print("  ID?      :", d.idn())                       # 3458A 没有 *IDN?
            st = d.state()                                        # FUNC?/RANGE?/ARANGE?/TRIG? …
            print("  device   :", {k: st[k] for k in
                                   ("function", "range_v", "nplc", "trig", "arange",
                                    "inbuf", "end", "oformat", "mformat")})
            print("  TEMP?    :", st.get("temperature_c"), "C")
            print("  ERRSTR?  :", d.error_string(),
                  "clean=", is_error_clear(d.error_string()))

            for i in range(3):                                    # 单次读数：TARM SGL,1
                print(f"  read #{i+1}  : {d.read_dcv():.9e} V")
            print(f"  read_avg3: {d.read_avg(3):.9e} V")

            if do_burst:
                b = d.read_burst(100, sample_interval_s=1e-4, dcv_range=0.1,
                                 data_format="SINT")               # >120% 档位信号改用 "DINT"
                sm = b["summary"]
                print(f"  burst    : n={len(b['values'])} bytes={sm['bytes_read']} "
                      f"fmt={sm['data_format']} mean={sm['mean']:.3e} sd={sm['stddev']:.2e}")
                print(f"             已自动 PRESET NORM 恢复: {sm['restored_to_preset_norm']}")
                d.configure_dcv(0.1, 10.0)                        # 突发恢复会改档位/NPLC → 显式设回
    except Exception as exc:                                      # noqa: BLE001
        # 现场最常见两种：① 设备被别人占用（库会明确说"占用"）② 驱动/地址问题
        hint = ""
        try:
            from keysight_3458a.driver_check import check_gpib_driver

            info = check_gpib_driver()
            if not info.get("ok"):
                hint = f"\n  驱动预检查: {info.get('verdict')} — {info.get('message')}"
        except Exception:                                          # noqa: BLE001
            pass
        print(f"连接/读数失败: {type(exc).__name__}: {exc}{hint}")
        print("（设备可能正被占用：等对方释放，或确认对方已死再用 connect(force=True)）")


async def via_mcp() -> None:
    """② 走 MCP：起 server.py 子进程，走标准 stdio JSON-RPC，调同名工具。"""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    print("== ② 走 MCP（stdio）==")
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)],
                                   cwd=str(ROOT), env=SERVER_ENV)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            ks = sorted(t.name for t in tools.tools if t.name.startswith("ks3458a_"))
            print(f"  tools: {len(tools.tools)} 个，其中 ks3458a_* {len(ks)} 个")

            def payload(res) -> dict:
                """CallToolResult → 解析后的 dict（服务器统一回 JSON 文本）。"""
                text = "".join(getattr(c, "text", "") for c in res.content)
                return json.loads(text)

            # 注意：工具参数用**关键字**传（服务器端 schema 决定），resource 缺省走解析层
            st = payload(await session.call_tool("ks3458a_status", {}))
            print("  ks3458a_status ok=", st.get("ok"),
                  "idn=", st.get("idn"), "range=", (st.get("device") or {}).get("range_v"),
                  "trig=", (st.get("device") or {}).get("trig"))
            r = payload(await session.call_tool("ks3458a_read", {}))
            print("  ks3458a_read  ->", r)
            r2 = payload(await session.call_tool("ks3458a_read_avg", {"n": 3}))
            print("  ks3458a_read_avg(n=3) ->", r2)


def main() -> int:
    args = set(sys.argv[1:])
    if "--dry-run" in args:
        dry_run(do_burst="--burst" in args)
    elif "--via" in args and "mcp" in args:
        asyncio.run(via_mcp())
    else:
        via_library(do_burst="--burst" in args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
