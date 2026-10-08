"""compact profile 校验（离线，不碰仪器）——方案 C 阶段 3 的验收判据。

断言分四组：

    S1 工具表形态：compact 恰好暴露 4 个预期工具，每个都有非空描述与 object schema。
    S2 能力不丢：registry 在两种 profile 下都是全部 68 个操作——compact 只是把
       "有哪些能力"从常驻上下文挪成按需数据，**不是砍掉能力**。
    S3 前端行为：用一个假的 run_fn 注册一套 compact 工具并真调用，验证
       search / describe / call 的返回与**入参校验**；关键点是校验失败时
       run_fn **一次都不许被调用**（参数错误必须在离开模型时拦住，不下发设备）。
    S4 成本对比：打印两种 profile 的工具定义体量与压缩比（这是本阶段的目标指标）。

用法：python TEST_SCRIPTS/common/verify_compact_profile.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mcp_instruments"))

# `import server` 必须落在 legacy 档：S2 要确认 registry 在两种 profile 下都是全部 68 个
# 操作，S4 要拿 `server.mcp` 里的 68 个工具当 legacy 基准算压缩比。server.py 的默认档
# 已改为 compact，不钉住的话 S4 会变成 "compact 对比 compact"（比值 ~0%，静默失真）。
# compact 那一侧由本脚本自己建 FastMCP 实例（见 S1），与这个环境变量无关。
os.environ["INSTRUMENT_MCP_PROFILE"] = "legacy"

fails: list[str] = []


def _ascii(text) -> str:
    return str(text).encode("ascii", "replace").decode("ascii")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {_ascii(name):56s} {_ascii(detail)[:110]}", flush=True)
    if not ok:
        fails.append(name)


def _call(mcp, tool_name, **kwargs):
    """取注册好的工具并同步执行（工具本体是 async）。"""
    tool = mcp._tool_manager.get_tool(tool_name)
    return asyncio.run(tool.fn(**kwargs))


def main() -> int:
    import server as S  # noqa: E402  legacy profile: 登记全部 68 个操作

    from instrument_runtime import get_registry
    from instrument_runtime.validate import SUPPORTED_TYPES, schema_constructs
    from mcp.server.fastmcp import FastMCP

    reg = get_registry()

    # ---------------- S1 compact 工具表形态 ----------------
    print("S1 compact surface shape")
    calls: list[tuple[str, dict]] = []
    batch_calls: list[dict] = []

    async def fake_run(op_name: str, args: dict) -> str:
        calls.append((op_name, args))
        return json.dumps({"ok": True, "model": "FAKE", "result": "stub"}, ensure_ascii=False)

    async def fake_batch(plan: dict) -> str:
        batch_calls.append(plan)
        return json.dumps({"ok": True, "status": "completed", "run_id": "FAKE"}, ensure_ascii=False)

    quick_calls: list[int] = []

    def fake_devices_quick() -> str:
        quick_calls.append(1)
        return json.dumps({"ok": True, "mode": "known", "count": 2,
                           "devices": [{"kind": "dg", "resource": "USB0::FAKE::INSTR"}],
                           "scanned": False}, ensure_ascii=False)

    mcp = FastMCP("compact-verify")
    import compact_tools

    names = compact_tools.register(mcp, registry=reg, run_fn=fake_run, run_batch_fn=fake_batch,
                                   devices_quick_fn=fake_devices_quick)
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    check("compact exposes exactly the 5 documented tools",
          set(tools) == {"instr_devices", "instr_search", "instr_describe", "instr_call",
                         "instr_batch"},
          str(sorted(tools)))
    check("register() returns the same names it registered",
          set(names) == set(tools), str(names))
    check("every compact tool has a non-empty description",
          all((t.description or "").strip() for t in tools.values()), "ok")
    check("every compact tool has an object inputSchema",
          all((t.parameters or {}).get("type") == "object" for t in tools.values()), "ok")

    # ---------------- S2 能力不丢 ----------------
    print("\nS2 all 68 operations remain available in the registry")
    check("registry holds 68 operations regardless of profile", len(reg) == 68, f"{len(reg)}")
    check("registry is not profile-dependent (server registered legacy too)",
          len(reg.by_device()) == 10, f"{len(reg.by_device())} device families")

    # schema 构造必须仍在校验器覆盖范围内——出现新构造要显式扩校验器，不许静默放过
    seen: set[str] = set()
    for op in reg:
        seen |= schema_constructs(op.schema)
    allowed = {"type", "properties", "required", "title", "default", "anyOf", "items", "enum"}
    check("schema constructs stay inside the validator's coverage",
          seen <= allowed, f"unexpected: {sorted(seen - allowed)}")
    check("validator covers all five primitive types",
          set(SUPPORTED_TYPES) == {"object", "integer", "boolean", "string", "number"},
          str(SUPPORTED_TYPES))

    # ---------------- S3 前端行为 ----------------
    print("\nS3 compact frontend behaviour (fake run_fn, no device touched)")
    r = json.loads(_call(mcp, "instr_search", query="vpp"))
    check("search finds operations for a keyword",
          r["ok"] and r["count"] > 0, f"count={r.get('count')}")
    check("search results carry id/risk/summary",
          all({"id", "risk", "summary"} <= set(x) for x in r["results"]),
          str(r["results"][:1])[:100])
    r = json.loads(_call(mcp, "instr_search", query="", limit=999))
    check("search rejects an out-of-range limit", not r["ok"] and
          r["error_type"] == "param_validation", r.get("error", "")[:80])
    r = json.loads(_call(mcp, "instr_search", query="sds", device="sds"))
    check("search honours the device filter",
          r["ok"] and all(x["device"] == "sds" for x in r["results"]),
          f"count={r.get('count')}")
    r = json.loads(_call(mcp, "instr_search", query="zzzz-nothing-matches"))
    check("search on a miss returns an empty result with a hint",
          r["ok"] and r["count"] == 0 and "hint" in r, "ok")

    # 中文查询（2026-09-23 实测缺陷回归）：原实现按空白切词，中文整句当一个词，
    # 于是中文提问恒返回 0 条命中——而使用者平时就用中文。现按 CJK 二元组切分。
    for q in ("读信号源当前状态", "波形发生器", "示波器测量", "万用表电压"):
        r = json.loads(_call(mcp, "instr_search", query=q))
        check(f"CJK query finds hits: {q}", r["ok"] and r["count"] > 0,
              f"count={r.get('count')}")
    r = json.loads(_call(mcp, "instr_search", query="信号源 输出"))
    check("mixed CJK query with a space still works",
          r["ok"] and r["count"] > 0, f"count={r.get('count')}")

    # 空查询 = 完整能力目录（2026-09-23 加）。此前空查询恒返回 0 条，而 compact 下
    # 没有别的途径枚举能力：describe 要 id、search 要关键词。
    r = json.loads(_call(mcp, "instr_search", query=""))
    devs = r.get("devices") or {}
    total = sum(len(v) for v in devs.values())
    check("empty query returns the full capability catalog",
          r.get("mode") == "catalog" and total == 68, f"total={total} devices={len(devs)}")
    check("catalog groups by device family and lists op ids only",
          all(isinstance(v, list) and all("." in i for i in v) for v in devs.values()),
          str(sorted(devs)[:4]))
    check("catalog covers every device family",
          set(devs) == {"dg832", "sds", "sdg", "dmm", "dho", "mho", "psu", "ks3458a",
                        "instr", "usb"}, str(sorted(devs)))
    r = json.loads(_call(mcp, "instr_search", query="", device="sds"))
    check("catalog honours the device filter",
          sum(len(v) for v in (r.get("devices") or {}).values()) == 13,
          str({k: len(v) for k, v in (r.get("devices") or {}).items()}))
    r = json.loads(_call(mcp, "instr_search", query="zzzz-nothing"))
    check("a miss now points at the catalog mode",
          r["ok"] and r["count"] == 0 and "空查询" in (r.get("hint") or ""),
          (r.get("hint") or "")[:60])

    r = json.loads(_call(mcp, "instr_describe", ids="sds_measure"))
    check("describe accepts a tool name and returns the parameter table",
          r["ok"] and r["count"] == 1
          and "properties" in r["operations"][0]["parameters"]
          and r["operations"][0]["safety"]["risk"] == "read_only",
          f"risk={r['operations'][0]['safety']['risk']}")
    r = json.loads(_call(mcp, "instr_describe", ids="sdg.set_wave"))
    check("describe accepts a canonical dotted id",
          r["ok"] and r["operations"][0]["tool_name"] == "sdg_set_wave",
          str(r.get("not_found")))
    r = json.loads(_call(mcp, "instr_describe", ids="sdg.set_wave,dg_protect"))
    check("describe accepts a comma-separated list", r["ok"] and r["count"] == 2, "ok")
    r = json.loads(_call(mcp, "instr_describe", ids="nope.op"))
    check("describe reports unknown ids without failing the whole call",
          r["ok"] and r["count"] == 0 and r["not_found"] == ["nope.op"], "ok")
    r = json.loads(_call(mcp, "instr_describe", ids=",".join(["a"] * 9)))
    check("describe caps the batch at 8", not r["ok"], r.get("error", "")[:70])
    check("describe surfaces the operational note for guarded ops",
          "protect_required" in json.dumps(
              json.loads(_call(mcp, "instr_describe", ids="dg_protect")), ensure_ascii=False),
          "dg_protect note present")

    # 关键：校验失败时不得触碰执行路径
    calls.clear()
    r = json.loads(_call(mcp, "instr_call", op="sds_measure", args={}))
    check("call rejects a missing required argument", not r["ok"]
          and r["error_type"] == "param_validation", r.get("error", "")[:80])
    check("rejected call never reached the executor", calls == [], str(calls))
    r = json.loads(_call(mcp, "instr_call", op="sds_measure",
                         args={"item": "PKPK", "bogus": 1}))
    check("call rejects an unknown argument", not r["ok"] and "bogus" in r.get("error", ""),
          r.get("error", "")[:80])
    check("rejected call (unknown arg) never reached the executor", calls == [], str(calls))
    r = json.loads(_call(mcp, "instr_call", op="dg_output", args={"ch": "one", "on": True}))
    check("call rejects a wrong argument type", not r["ok"], r.get("error", "")[:80])
    r = json.loads(_call(mcp, "instr_call", op="no.such.op", args={}))
    check("call rejects an unknown operation", not r["ok"]
          and r["error_type"] == "param_validation", r.get("error", "")[:70])
    check("unknown-op call never reached the executor", calls == [], str(calls))

    r = json.loads(_call(mcp, "instr_call", op="sds_measure", args={"item": "PKPK"}))
    check("valid call dispatches to the executor once",
          r["ok"] and calls == [("sds_measure", {"item": "PKPK"})], str(calls))
    calls.clear()
    r = json.loads(_call(mcp, "instr_call", op="sdg.set_wave",
                         args={"ch": 1, "wvtp": "sine", "freq_hz": 1000, "amp_v": 2.0}))
    check("canonical id dispatches to the underlying tool name",
          calls and calls[0][0] == "sdg_set_wave", str(calls))
    # instr_devices：默认走**快速路径**（配置+缓存，不扫描），full=True 才全量发现。
    calls.clear(); quick_calls.clear()
    r = json.loads(_call(mcp, "instr_devices"))
    check("instr_devices defaults to the fast (no-scan) path",
          r.get("mode") == "known" and r.get("scanned") is False and len(quick_calls) == 1,
          f"mode={r.get('mode')} scanned={r.get('scanned')}")
    check("fast path does NOT touch the executor (no scan, no BUSY)",
          calls == [], str(calls))
    r = json.loads(_call(mcp, "instr_devices", full=True))
    check("full=True dispatches to the real discovery operation",
          r["ok"] and calls and calls[-1][0] == "instr_discover", str(calls[-1:]))
    # instr_batch：前端只做形态检查并把 plan 交给注入的执行入口（preflight 在 server 侧）
    r = json.loads(_call(mcp, "instr_batch", plan={"plan_version": 1}))
    check("instr_batch hands the plan to the injected runner",
          r["ok"] and batch_calls == [{"plan_version": 1}], str(batch_calls))
    r = json.loads(_call(mcp, "instr_batch", plan="not-an-object"))
    check("instr_batch rejects a non-object plan before dispatching",
          not r["ok"] and r["error_type"] == "param_validation" and len(batch_calls) == 1,
          r.get("error", "")[:70])

    # ---------------- S4 成本对比 ----------------
    print("\nS4 tool-definition cost (the point of this phase)")
    def _wire(tool_list) -> list:
        """转成线上 tools/list 的**真实字段集**——实测（2026-09-23 dump）MCP 线格式
        带 name / description / inputSchema / outputSchema 四项；FastMCP 的 Tool 是
        pydantic 对象（含不可序列化的 fn），不能直接 json。字段与顺序都对齐，
        这样这里的字节数才与真实请求里的工具定义可比。"""
        return [{"name": t.name, "description": t.description or "",
                 "inputSchema": t.parameters or {},
                 "outputSchema": getattr(t, "output_schema", None)}
                for t in tool_list]

    def _size(tool_list) -> int:
        return len(json.dumps(_wire(tool_list), ensure_ascii=False))

    legacy_tools = _legacy_tools(S)
    legacy_bytes = _size(legacy_tools)
    compact_bytes = _size(list(tools.values()))
    ratio = 100.0 * (1 - compact_bytes / legacy_bytes)
    print(f"      legacy : {len(legacy_tools):3d} tools  {legacy_bytes:7d} chars"
          f"  ~{legacy_bytes // 3:6d} tok")
    print(f"      compact: {len(tools):3d} tools  {compact_bytes:7d} chars"
          f"  ~{compact_bytes // 3:6d} tok")
    print(f"      saved  : ~{(legacy_bytes - compact_bytes) // 3} tok per request "
          f"({ratio:.1f}% smaller)")
    check("compact is at least 90% smaller than legacy", ratio >= 90.0, f"{ratio:.1f}%")

    print(f"\n== result: {'all PASS' if not fails else f'{len(fails)} FAIL'} ==")
    for f in fails:
        print(f"  - {_ascii(f)}")
    return 1 if fails else 0


def _legacy_tools(server_module):
    """legacy 面的工具对象（本进程以 legacy profile 导入 server，故 68 个都在）。"""
    return server_module.mcp._tool_manager.list_tools()


if __name__ == "__main__":
    sys.exit(main())
